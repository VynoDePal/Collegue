"""Assemblage de l'exécuteur d'une issue de bout en bout (E5, epic #362).

``execute_issue`` enchaîne les briques E1→E4 en un point d'entrée **isolé** :

    prepare_workspace (E2) → run_issue (E2) → run_quality_gate (E3) → open_pr (E4)

et **synchronise l'état** de la tâche : ``todo → in_progress`` au démarrage,
``→ in_review`` quand la PR est ouverte. **Jamais** ``done`` automatiquement : le
merge reste **humain**, et la **CI gate** le merge (la PR est ouverte mais ne peut
être mergée qu'avec la CI verte + approbation — sémantique GitHub, pas gérée ici).

**Fail-closed** : si l'agent ne produit aucun diff, ou si le gate qualité ne passe
pas, on **s'arrête** — aucune PR, l'état **ne dépasse pas** ``in_progress``.

``dry_run=True`` (défaut) : pipeline complet jusqu'à un **aperçu** de PR, **sans
aucune écriture** (ni GitHub, ni transition d'état) — utile pour visualiser ce qui
serait fait.

Module **isolé** : non importé par ``app.py``. Le pilote (Phase 3) appellera
``execute_issue`` sur le graphe de tâches (en respectant dépendances + budget) et
prendra en charge le déplacement de carte de board (il détient le mapping des
items du board) — délibérément hors périmètre ici.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Mapping, Optional

from collegue.executor.agent import AgentResult, CodeAgent, IssueSpec
from collegue.executor.command import CommandRunner
from collegue.executor.pr import PrClients, PrResult, open_pr
from collegue.executor.quality_gate import QualityReport, Reviewer, run_quality_gate
from collegue.executor.runner import ExecutionResult, capture_diff, run_issue
from collegue.executor.workspace import Workspace, apply_seed_diff, prepare_workspace
from collegue.sandbox.executor import TIMEOUT_NOTE

logger = logging.getLogger(__name__)

TASK_STATUS_TODO = "todo"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_IN_REVIEW = "in_review"

# Étapes possibles d'arrêt/aboutissement du pipeline.
STAGE_RUN = "run"  # exécution de l'agent (diff)
STAGE_GATE = "gate"  # gate qualité (tests + revue)
STAGE_PR = "pr"  # ouverture de PR

# Raisons d'échec portées par l'outcome (#421). Un ``success=False`` indifférencié
# rendait le no-op de l'agent (souvent transitoire, ex. fenêtre 503 du provider)
# indiscernable d'un vrai échec : ni retry intelligent, ni post-mortem possibles.
REASON_NO_OP = "no_op"  # l'agent a tourné sans erreur mais n'a produit AUCUN diff
REASON_AGENT_ERROR = "agent_error"  # le process agent a échoué (exit ≠ 0 / timeout)
REASON_GATE_FAILED = "gate_failed"  # tests rouges ou revue bloquante
REASON_ENGINE_ERROR = "engine_error"  # exception d'infrastructure interceptée (#435)


def log_tail(text: str, limit: int = 2000) -> str:
    """Dernier segment (borné) d'un log — journalisable sans inonder l'audit (#421)."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


# Signatures d'aléa d'INFRASTRUCTURE (réseau, dépôt de paquets, 5xx fournisseur)
# dans un feedback d'échec (#459). Heuristique volontairement étroite : ces
# motifs n'apparaissent pas dans un diagnostic fonctionnel.
_INFRA_NOISE_SIGNATURES = (
    "ReadTimeoutError",
    "ReadTimeout",
    "ConnectTimeoutError",
    "ConnectTimeout",
    "ConnectionError",
    "ConnectionResetError",
    "NewConnectionError",
    "Temporary failure in name resolution",
    "Connection refused",
    "502 Server Error",
    "503 Server Error",
    "504 Server Error",
    # Kill du conteneur sandbox au timeout (#461) : quand pip pend sur PyPI, le
    # conteneur peut être tué avant d'imprimer un traceback réseau — la note du
    # sandbox est alors le seul indice, et ce n'est pas un diagnostic actionnable.
    TIMEOUT_NOTE,
)

# #498 : un crash du process AGENT (coder OpenHands) AVANT tout appel LLM a une
# signature nette — traceback d'import dans les logs (image/runner cassé, ex.
# lmnr 0.7.53 incompatible au faux départ FacNor v5) ET 0 token consommé. C'est
# un aléa d'INFRASTRUCTURE (cause globale, indépendante de la tâche), pas un
# échec fonctionnel : il ne doit pas décompter le budget de tentatives.
_IMPORT_CRASH_SIGNATURES = (
    "ModuleNotFoundError",
    "ImportError",
)


def is_infra_noise(feedback: str) -> bool:
    """Vrai si ``feedback`` ressemble à un aléa d'infrastructure (#459).

    Un timeout PyPI pendant le gate produit un traceback réseau SANS ligne
    FAILED : ré-injecté tel quel, il ÉCRASE le diagnostic actionnable de la
    tentative précédente (cas réel FacNor v3 : « email-validator manquant »
    éclipsé par du bruit urllib3 — requeue opérateur nécessaire). Une ligne
    FAILED/ERROR présente = diagnostic fonctionnel, jamais classé bruit.
    """
    text = feedback or ""
    if not text:
        return False
    # « FAILED  » / « ERROR  » avec espace : les formes pytest. (pip écrit
    # « ERROR: » avec deux-points — c'est justement du bruit d'install à classer.)
    if any(line.strip().startswith(("FAILED ", "ERROR ")) for line in text.splitlines()):
        return False
    return any(signature in text for signature in _INFRA_NOISE_SIGNATURES)


def is_infra_gate_failure(outcome: "ExecutionOutcome") -> bool:
    """Vrai si un échec de gate est imputable à un aléa d'infrastructure (#477).

    Deux chemins :

    - le diagnostic court (:func:`failure_feedback`) porte une signature réseau
      (cas nominal #459/#461 : traceback pip sans ligne pytest) ;
    - l'installation des dépendances a échoué (``deps_install_failed``, #439)
      **et** la sortie complète du gate contient une signature réseau. La
      cascade « pip timeout → ModuleNotFoundError à la collecte » produit des
      lignes ``ERROR `` d'apparence fonctionnelle qui désarmaient la grâce #461
      alors que la cause première était un aléa PyPI/DNS (cas réel FacNor v4 :
      échec terminal de la tâche 6 sur un pur ReadTimeoutError pip).

    Un échec d'install SANS signature réseau (requirements invalide, paquet
    inexistant) reste fonctionnel : c'est précisément ce que la passe #439
    doit sanctionner. Et un gate rouge à tests VERTS (revue bloquante,
    adéquation #437) n'est jamais gracié : le verdict est fonctionnel même si
    l'install a connu un aléa réseau en chemin (les deux cas cibles — timeout
    pip, cascade de collecte — ont toujours des tests rouges).
    """
    if outcome.reason != REASON_GATE_FAILED:
        return False
    report = outcome.quality_report
    if report is not None and report.tests_passed:
        # Gate rouge à tests VERTS (revue bloquante, adéquation #437,
        # require_test_changes) : verdict fonctionnel — même si la queue de
        # sortie charrie un aléa réseau d'install, il n'est pas la cause.
        return False
    if is_infra_noise(failure_feedback(outcome)):
        return True
    if report is not None and getattr(report, "deps_install_failed", False):
        output = report.test_output or ""
        return any(signature in output for signature in _INFRA_NOISE_SIGNATURES)
    return False


def is_infra_agent_crash(outcome: "ExecutionOutcome") -> bool:
    """Vrai si un ``agent_error`` est un crash d'IMPORT pré-LLM (#498).

    Signature : ``reason == agent_error`` ET 0 token consommé (aucun appel LLM
    utile) ET un traceback d'import (``ModuleNotFoundError``/``ImportError``)
    dans les logs de l'agent. C'est un aléa d'infrastructure GLOBAL (image/runner
    sandbox cassé, ex. faux départ FacNor v5 : lmnr 0.7.53 incompatible) — gracié
    comme un aléa de gate (#461), borné par ``MAX_INFRA_GATE_GRACE`` côté pilote.

    Un ``agent_error`` FONCTIONNEL (l'agent a appelé le LLM puis échoué) consomme
    des tokens → jamais classé crash d'infra.
    """
    if outcome.reason != REASON_AGENT_ERROR:
        return False
    result = getattr(outcome.execution, "agent_result", None)
    if result is None:
        return False
    if int(getattr(result, "total_tokens", 0) or 0) > 0:
        return False
    logs = getattr(result, "logs", "") or ""
    return any(sig in logs for sig in _IMPORT_CRASH_SIGNATURES)


def agent_crash_signature(logs: str) -> str:
    """Identité STABLE d'un crash d'import pour la détection de crash-loop (#498).

    Hacher la queue brute des logs serait fragile : codes ANSI, bannière/version
    OpenHands, warnings horodatés et chemins de workspace ``/tmp/collegue-exec-…``
    randomisés font varier les octets à chaque crash → deux crashs de la MÊME
    cause produiraient des hash différents et le fail-fast ne tirerait jamais. On
    isole donc la (dernière) ligne d'exception d'import — ``ModuleNotFoundError:
    No module named 'lmnr'`` — qui ne porte ni PID ni adresse ni chemin variable.
    À défaut, repli sur les lignes d'import du traceback, sinon la queue bornée.
    """
    lines = [ln.strip() for ln in (logs or "").splitlines() if ln.strip()]
    crash_lines = [ln for ln in lines if ln.startswith(_IMPORT_CRASH_SIGNATURES)]
    if crash_lines:
        return crash_lines[-1]
    import_lines = [ln for ln in lines if any(sig in ln for sig in _IMPORT_CRASH_SIGNATURES)]
    if import_lines:
        return import_lines[-1]
    return log_tail(logs, 1000)


# #478 : marqueur de troncature du short summary pytest (ASCII « ... » — distinct
# du « … » de log_tail, qui est un autre chemin).
_PYTEST_TRUNCATION = "..."


def _detruncate_summary_line(line: str, output: str) -> str:
    """Restitue le diagnostic complet d'une ligne de short summary tronquée (#478).

    En non-tty, pytest borne « FAILED nodeid - message » à COLUMNS (80 par
    défaut) et tronque avec « ... » — le nom du paquet manquant disparaissait du
    feedback (cas réel FacNor v4 : « requires the httpx pack... », 3 cycles
    brûlés à deviner + requeues opérateur). Le message ENTIER vit dans les
    lignes ``E   …`` du traceback de la même sortie : on l'y reprend (préfixe
    tronqué → première ligne E qui le contient). Best-effort : sans
    correspondance, la ligne tronquée est relayée telle quelle.
    """
    if not line.endswith(_PYTEST_TRUNCATION):
        return line
    head, sep, message = line.partition(" - ")
    prefix = message[: -len(_PYTEST_TRUNCATION)].strip()
    if not sep or not prefix:
        return line
    for raw in output.splitlines():
        candidate = raw.strip()
        if candidate.startswith("E ") and prefix in candidate:
            # Borné : une ligne E géante ne doit pas manger le budget [:700]
            # du feedback et masquer les autres lignes FAILED.
            return f"{head} - {candidate[1:].strip()[:300]}"
    return line


def _summary_line_path(line: str) -> str:
    """Chemin du fichier de test d'une ligne de short summary pytest (#507).

    Forme : ``FAILED <path>::<test> - <msg>`` ou ``ERROR <path> - <msg>`` — le
    path est le token qui suit ``FAILED ``/``ERROR ``, borné au premier ``::``
    (nodeid) puis à l'espace (path nu « ERROR p - m »). Best-effort : chaîne vide
    si non reconnaissable (le label sera alors omis).
    """
    for prefix in ("FAILED ", "ERROR "):
        if line.startswith(prefix):
            token = line[len(prefix) :].lstrip()
            token = token.split("::", 1)[0].split(" ", 1)[0]
            return token.strip()
    return ""


def _label_failure_line(line: str, changed: frozenset[str]) -> str:
    """Étiquette une ligne FAILED/ERROR selon la PROVENANCE du test (#507).

    Croise le fichier de test en échec avec le périmètre du diff de la tentative
    (``files_changed``) : un test que le diff N'A PAS touché et qui casse = une
    RÉGRESSION sur l'existant (le coder doit corriger SON code, pas le test).
    Étiquette posée en SUFFIXE — jamais en préfixe : :func:`is_infra_noise` et
    :func:`is_infra_gate_failure` testent ``startswith("FAILED "/"ERROR ")`` ;
    un préfixe reclasserait à tort un échec fonctionnel en bruit infra et le
    gracierait (#461). Best-effort : sans périmètre connu ou path illisible, la
    ligne est relayée inchangée (pas de label spéculatif).
    """
    if not changed:
        return line
    path = _summary_line_path(line)
    if not path:
        return line
    # Match tolérant au sous-répertoire : en monorepo, un GATE_TEST_COMMAND du type
    # « cd backend && pytest » émet des nodeids relatifs au sous-dir (`tests/x.py`)
    # alors que files_changed (git --name-only) est TOUJOURS racine-relatif
    # (`backend/tests/x.py`). Le path pytest est donc un suffixe (frontière `/`) du
    # path git. On n'autorise que ce sens (pytest plus court) : appeler une vraie
    # régression « test de la tâche » est sans danger (la ligne FAILED reste
    # relayée), tandis que l'inverse — étiqueter à tort RÉGRESSION un test que
    # l'agent vient d'ajouter — lui ordonnerait de ne pas le corriger.
    if path in changed or any(c.endswith("/" + path) for c in changed):
        return f"{line} [tests de la tâche]"
    return (
        f"{line} [RÉGRESSION tests pré-existants — ton diff a cassé l'existant : "
        "ne modifie pas ces tests, corrige ton code]"
    )


def failure_feedback(outcome: "ExecutionOutcome") -> str:
    """Synthèse **courte et actionnable** d'un échec, pour la tentative suivante (#424).

    Priorité aux lignes ``FAILED``/``ERROR`` de pytest : c'est exactement ce dont
    l'agent a besoin pour corriger la cause. Un feedback verbeux (sortie brute)
    NOIE l'agent au lieu de l'aider — constaté en run réel (FacNor, task 4 :
    feedback bruité → time-out de 40 min ; lignes FAILED seules → convergence).
    À défaut de lignes de tests, queue bornée de la sortie de tests puis des logs
    agent (échec au stage ``run``).

    Exception d'infrastructure (#435, ``outcome.error``) : c'est ELLE le motif —
    les logs agent (potentiellement ceux d'une exécution réussie, si la panne est
    survenue à l'ouverture de PR) seraient un feedback trompeur.

    Adéquation refusée (#437) : les tests sont VERTS — le motif utile est la
    justification du contrôle (« la feature n'est pas implémentée »), pas la
    sortie des tests.

    Fichiers parasites bloquants (#508) : quand la garde bloquante a fait rougir le
    gate, le motif utile est la liste des fichiers à RETIRER — la sortie des tests
    (souvent verte) masquerait cette consigne (run v6 : `server.log` jamais signalé,
    tâche racine bloquée 3 tentatives).

    Provenance (#507) : chaque ligne FAILED/ERROR est étiquetée selon que le
    fichier de test appartient ou non au diff de la tentative (``files_changed``)
    — le coder distingue ainsi une RÉGRESSION qu'il a introduite sur des tests
    pré-existants d'un défaut de sa propre feature.
    """
    if outcome.error:
        return log_tail(outcome.error, 400)
    report = outcome.quality_report
    if report is not None and getattr(report, "adequacy_implemented", None) is False:
        justification = getattr(report, "adequacy_justification", "") or "le diff n'implémente pas l'issue"
        return ("ADÉQUATION REFUSÉE — le diff ne réalise pas l'issue : " + justification)[:700]
    if report is not None and getattr(report, "adequacy_tests_assert", None) is False:
        # #499 : feature présente, tests VERTS, mais un critère chiffrable n'est
        # asserté par aucun test. La sortie pytest (verte) serait un feedback
        # trompeur — le motif UTILE est le critère non couvert, pour que l'agent
        # ajoute l'assertion au lieu de boucler sans converger (cf. #424).
        justification = getattr(report, "adequacy_tests_justification", "") or "un critère chiffrable n'est pas testé"
        return (
            "COUVERTURE DE TEST INSUFFISANTE (#499) — un critère chiffrable de l'issue n'est asserté par "
            "aucun test : " + justification + ". Ajoute une assertion sur la VALEUR/le CALCUL attendu "
            "(pas seulement un code HTTP 200)."
        )[:700]
    removed = tuple(getattr(report, "requirements_removed", ()) or ()) if report is not None else ()
    if removed:
        # #482 : le motif utile est la liste NOMINATIVE des lignes perdues —
        # c'est elle que la tentative suivante doit ré-ajouter telles quelles
        # (la sortie des tests, souvent VERTE ici, serait un feedback trompeur).
        return (
            "REQUIREMENTS APPEND-ONLY (#482) — lignes de requirements.txt présentes sur la base et "
            "SUPPRIMÉES par ton diff : " + " ; ".join(removed[:10]) + ". Ré-ajoute-les telles quelles "
            "(n'en supprime aucune) et conserve le reste de ton travail."
        )[:700]
    if report is not None and getattr(report, "forbidden_files_blocking", False):
        # #508 : le gate est rouge PARCE QUE le diff committe des fichiers parasites
        # (garde bloquante opt-in). Sans cette branche, failure_feedback retombait
        # sur la sortie des tests — souvent VERTE, terminée par du bruit pip — et
        # l'agent ne savait JAMAIS qu'il fallait retirer ces fichiers (run v6 : la
        # tâche racine a brûlé ses 3 tentatives sur un `server.log` jamais signalé).
        forbidden = tuple(getattr(report, "forbidden_files", ()) or ())
        return (
            "FICHIERS PARASITES COMMITTÉS (#508) — ton diff ajoute des fichiers qui n'ont rien à "
            "faire dans le livrable (artefacts d'exécution / secrets / bases locales / dépendances "
            "vendorées) et le gate les REFUSE : " + " ; ".join(forbidden[:10]) + ". Retire-les du "
            "commit (git rm --cached) et ajoute leurs motifs au .gitignore ; conserve le reste de ton travail."
        )[:700]
    if outcome.quality_report is not None and outcome.quality_report.test_output:
        output = outcome.quality_report.test_output
        # « FAILED  » / « ERROR  » avec espace : les formes du short summary
        # pytest, alignées sur is_infra_noise (#477). Sans l'espace, la ligne
        # pip « ERROR: Exception: » (deux-points) était relayée comme diagnostic
        # « fonctionnel » — inactionnable — et le traceback réseau (ReadTimeout…)
        # était jeté : la grâce #461 ne voyait jamais la signature infra.
        fails = [line.strip() for line in output.splitlines() if line.strip().startswith(("FAILED ", "ERROR "))]
        if fails:
            # #478 : filet — le diagnostic complet est repris du traceback quand
            # le short summary a été tronqué à la largeur du terminal.
            # #507 : ORDRE crucial — dé-troncature D'ABORD (son `.endswith("...")`
            # doit voir la ligne brute), étiquetage de provenance ENSUITE.
            changed = frozenset(getattr(outcome.execution, "files_changed", ()) or ())
            labelled = (_label_failure_line(_detruncate_summary_line(line, output), changed) for line in fails[:6])
            return " ; ".join(labelled)[:700]
        return log_tail(output, 400)
    return log_tail(outcome.execution.agent_result.logs, 400)


@dataclass
class ExecutionOutcome:
    """Résultat de bout en bout de l'exécution d'une issue."""

    success: bool  # le pipeline est allé jusqu'à la PR (gate passé)
    stage: str  # dernière étape atteinte : run | gate | pr
    workspace: Optional[Workspace]  # None si l'exception a frappé avant la préparation (#435)
    execution: ExecutionResult
    quality_report: Optional[QualityReport] = None
    pr: Optional[PrResult] = None
    final_status: Optional[str] = None  # statut de tâche effectivement écrit (None si dry_run / pas de manager)
    reason: Optional[str] = None  # raison d'échec (no_op | agent_error | gate_failed | engine_error), None si succès
    error: Optional[str] = None  # exception d'infrastructure interceptée (#435), None sinon


def _set_status(manager, task_id, status: str, *, enabled: bool) -> Optional[str]:
    """Transition d'état si activée (réel + manager + task_id). Retourne le statut écrit."""
    if not enabled or manager is None or task_id is None:
        return None
    manager.update_task_status(task_id, status)
    return status


async def execute_issue(
    issue: IssueSpec,
    repo_source: str,
    ctx,
    *,
    agent: CodeAgent,
    owner: str,
    repo: str,
    base: str = "main",
    sandbox=None,
    reviewer: Optional[Reviewer] = None,
    runner: Optional[CommandRunner] = None,
    clients: Optional[PrClients] = None,
    manager: Optional[object] = None,
    task_id: Optional[int] = None,
    project_id: Optional[int] = None,
    dry_run: bool = True,
    seed_diff: Optional[str] = None,
    gate_options: Optional[Mapping[str, object]] = None,
) -> ExecutionOutcome:
    """Exécute une issue de bout en bout (workspace → agent → tests+revue → PR).

    Renvoie un :class:`ExecutionOutcome`. En cas d'arrêt fail-closed (aucun diff,
    ou gate non passé), ``success=False`` et aucune PR n'est ouverte ; l'état ne
    dépasse pas ``in_progress``. ``dry_run`` (défaut) n'écrit rien et n'effectue
    aucune transition d'état.

    **Barrière d'exception (#435)** : une exception d'infrastructure pendant le
    traitement (``WorkspaceError`` au clone, erreur réseau GitHub à l'ouverture de
    PR, bug ponctuel d'un adaptateur) ne remonte PLUS crue — elle est convertie en
    outcome ``failed`` (``reason="engine_error"``, ``stage`` = étape atteinte,
    ``error`` = exception) qui entre dans le chemin retry existant du pilote
    (#420/#424). Une panne ponctuelle d'UNE tâche ne tue plus le run entier alors
    que le reste du DAG est exécutable. Les ``BaseException`` (annulation asyncio,
    arrêt process) propagent, elles, normalement.

    ``seed_diff`` (#436) : diff d'une tentative précédente à RÉ-APPLIQUER sur le
    clone neuf avant l'agent (mémoire de retry — réparation incrémentale au lieu
    de régénération complète). Best-effort : un seed inapplicable est ignoré
    (clone vierge, comportement historique).

    ``gate_options`` (#438) : kwargs additionnels transmis tels quels à
    :func:`run_quality_gate` (``test_command``, ``frontend_gate``…) — c'est le
    canal de configuration du gate par projet/runtime, sans coupler l'exécuteur
    à la config.
    """
    persist = not dry_run  # les transitions d'état n'ont lieu qu'en exécution réelle
    final_status: Optional[str] = None
    stage = STAGE_RUN
    workspace: Optional[Workspace] = None
    execution: Optional[ExecutionResult] = None
    report: Optional[QualityReport] = None

    try:
        workspace = prepare_workspace(repo_source, issue)
        if seed_diff and apply_seed_diff(workspace, seed_diff):
            logger.info("issue #%s : workspace réensemencé avec la meilleure tentative (#436)", issue.number)
        final_status = _set_status(manager, task_id, TASK_STATUS_IN_PROGRESS, enabled=persist) or final_status

        # E2 : exécution de l'agent + capture du diff (l'état est piloté ici, pas par run_issue).
        execution = run_issue(agent, workspace, issue, runner=runner)
        if not execution.changed:
            # #421 : distinguer le no-op (agent OK, zéro diff — souvent transitoire)
            # de l'erreur du process agent (exit ≠ 0) — la couche retry en dépend.
            reason = REASON_NO_OP if execution.agent_result.success else REASON_AGENT_ERROR
            return ExecutionOutcome(
                success=False,
                stage=STAGE_RUN,
                workspace=workspace,
                execution=execution,
                final_status=final_status,
                reason=reason,
            )

        # E3 : gate qualité (fail-closed).
        stage = STAGE_GATE
        report = await run_quality_gate(
            workspace.path,
            execution.diff,
            ctx,
            sandbox=sandbox,
            reviewer=reviewer,
            issue=issue,
            **dict(gate_options or {}),
        )
        if getattr(report, "requirements_added", ()):
            # #481 : le gate a amendé requirements.txt (remédiation déterministe)
            # — recapturer le diff autoritatif, sinon la PR (open_pr pousse
            # files_changed) et la mémoire de retry (#436, best_diff) partiraient
            # SANS le correctif (récidive du bug livré). Stage borné à
            # requirements.txt : le gate écrit des artefacts dans le workspace
            # monté (node_modules, __pycache__, fichiers du smoke) qu'un add -A
            # global embarquerait dans la PR. Une WorkspaceError ici est
            # absorbée par la barrière #435 (engine_error, retentable).
            diff, files_changed = capture_diff(workspace, runner=runner, paths=("requirements.txt",))
            execution = replace(execution, diff=diff, files_changed=files_changed, changed=bool(files_changed))
        if not report.passed:
            return ExecutionOutcome(
                success=False,
                stage=STAGE_GATE,
                workspace=workspace,
                execution=execution,
                quality_report=report,
                final_status=final_status,
                reason=REASON_GATE_FAILED,
            )

        # E4 : ouverture de PR (dry_run respecté).
        stage = STAGE_PR
        pr = open_pr(
            workspace,
            report,
            issue,
            owner,
            repo,
            files_changed=execution.files_changed,
            base=base,
            clients=clients,
            dry_run=dry_run,
            manager=manager,
            project_id=project_id,
        )
        final_status = _set_status(manager, task_id, TASK_STATUS_IN_REVIEW, enabled=persist) or final_status
    except Exception as exc:  # barrière volontairement large (#435) — fail-closed, retentable
        error = f"{type(exc).__name__}: {exc}"
        logger.exception(
            "exception d'infrastructure pendant l'issue #%s (stage=%s) — convertie en échec retentable (#435)",
            issue.number,
            stage,
        )
        if execution is None:
            # L'agent n'a jamais tourné (panne au clone / à la transition d'état) :
            # résultat synthétique pour que l'outcome reste exploitable partout.
            execution = ExecutionResult(
                agent_result=AgentResult(success=False, logs=f"[engine] exception avant l'agent — {error}"),
                changed=False,
                diff="",
                files_changed=(),
                success=False,
            )
        return ExecutionOutcome(
            success=False,
            stage=stage,
            workspace=workspace,
            execution=execution,
            quality_report=report,
            final_status=final_status,
            reason=REASON_ENGINE_ERROR,
            error=error,
        )

    return ExecutionOutcome(
        success=True,
        stage=STAGE_PR,
        workspace=workspace,
        execution=execution,
        quality_report=report,
        pr=pr,
        final_status=final_status,
    )
