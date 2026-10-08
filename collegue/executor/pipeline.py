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
from typing import Mapping, Optional, Tuple

from collegue.executor.agent import AgentResult, CodeAgent, IssueSpec
from collegue.executor.command import CommandRunner
from collegue.executor.contracts import (
    ContractError,
    fresh_preimage_workspace,
    has_sealed_contracts,
    load_delivered_contracts,
    project_requires_contracts,
)
from collegue.executor.delivery_proof import (
    PHASE_BUILD,
    DeliveryProof,
    DeliveryProofError,
    DeliveryRemoteError,
    ProofDraft,
    TestedContent,
    describe_refusal,
    seal_tested_content,
    verify_tested_content,
)
from collegue.executor.pr import (
    DeliveryDriftError,
    DeliverySnapshot,
    PrClients,
    PrResult,
    assert_deliverable,
    assert_representable,
    capture_delivery_snapshot,
    open_pr,
    verify_delivery_snapshot,
)
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


# #507 (suivi v6) : bruit pip NON-fatal (exit 0). Un conflit de version avec une
# dépendance de l'IMAGE sandbox (openhands & co — hors périmètre du livrable) est
# signalé par pip SANS bloquer ; ce bruit NOIE le feedback de repli (run v6 : la
# tâche racine a brûlé 3 tentatives, le coder empilant des pins inutiles au lieu de
# voir le vrai motif). On le retire du diagnostic RELAYÉ au coder. Sûr vis-à-vis de
# la grâce #461 : AUCUNE de ces signatures n'est une signature réseau
# (_INFRA_NOISE_SIGNATURES), et on ne touche JAMAIS ``report.test_output``.
_PIP_NOISE_SIGNATURES = (
    "pip's dependency resolver does not currently take into account",
    "[notice] A new release of pip is available",
    "[notice] To update, run:",
    "WARNING: The script ",
    "Consider adding this directory to PATH",
    "Defaulting to user installation because normal site-packages is not writeable",
)


def filter_pip_noise(output: str) -> str:
    """Retire les lignes de bruit pip NON-fatal d'un diagnostic (#507).

    Cible : avertissements du resolver, notices de mise à jour pip, warnings de
    PATH, et la ligne de conflit ``X requires Y, but you have Z which is
    incompatible.`` (deps de l'image, hors livrable). Ne retire AUCUNE signature
    réseau (#461) ni ligne pytest ``FAILED``/``ERROR``.
    """
    if not output:
        return output
    kept = []
    for line in output.splitlines():
        stripped = line.strip()
        if any(sig in stripped for sig in _PIP_NOISE_SIGNATURES):
            continue
        if "but you have" in stripped and "incompatible" in stripped:  # conflit de version pip
            continue
        kept.append(line)
    return "\n".join(kept)


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
    if report is not None and getattr(report, "adequacy_error", None):
        return (
            "CONTRÔLE D'ADÉQUATION INDISPONIBLE — le gate fail-closed n'a pas pu confirmer que le diff "
            "réalise l'issue : " + str(report.adequacy_error)
        )[:700]
    if report is not None and (
        getattr(report, "acceptance_passed", None) is False or getattr(report, "acceptance_error", None)
    ):
        # §4.7 : les tests ordinaires peuvent être verts alors que l'oracle QA
        # indépendant est rouge. Sa sortie est distincte de ``test_output`` ;
        # sans cette branche, le diagnostic persisté relayait le mauvais succès
        # pytest et masquait totalement la cause (nightly réel #598).
        error = str(getattr(report, "acceptance_error", "") or "").strip()
        output = str(getattr(report, "acceptance_output", "") or "")
        failures = [line.strip() for line in output.splitlines() if line.strip().startswith(("FAILED ", "ERROR "))]
        detail = " ; ".join(failures[:6])
        if not detail:
            detail = error or log_tail(filter_pip_noise(output), 500).strip()
        if not detail:
            detail = "l'oracle QA a renvoyé un exit code non nul sans diagnostic exploitable"
        return (
            "ORACLE D'ACCEPTATION REFUSÉ (§4.7) — les tests du projet peuvent être verts, mais la preuve "
            "indépendante du contrat a échoué : " + detail
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
    if report is not None and getattr(report, "review_blocking", False):
        # #503 suivi v8 : le gate est rouge PARCE QUE la revue experte a HARD-BLOQUÉ
        # (findings CRITIQUES de sécurité — ex. IDOR, jetons falsifiables). Sans cette
        # branche, failure_feedback retombait sur la sortie des tests — souvent VERTE —
        # et le coder ne savait JAMAIS pourquoi le gate échouait (run v8, tâche 1 : 1
        # tentative perdue, le verdict critical-security du reviewer étant invisible).
        # On NOMME les failles pour que la tentative suivante les corrige (cf. #508/#482).
        findings = tuple(getattr(report, "review_findings", ()) or ())
        blockers = [
            f for f in findings if getattr(f, "severity", "") == "critical" and getattr(f, "category", "") == "security"
        ] or list(findings)[:6]
        named = " ; ".join(f"[{f.severity}/{f.category}] {f.title}" for f in blockers[:6])
        return (
            "REVUE EXPERTE BLOQUANTE — la revue a trouvé des failles que le gate REFUSE "
            "(les tests sont VERTS mais ne les couvrent pas) : " + named + ". Corrige ces "
            "failles dans le code livré (n'ignore pas la sécurité) ; conserve le reste de ton travail."
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
        # #507 : pas de ligne pytest exploitable → on relaie la queue, MAIS nettoyée
        # du bruit pip non-fatal (conflit avec les deps de l'image, notices). Si tout
        # était du bruit, on le DIT au lieu de relayer un diagnostic trompeur. La
        # signature réseau (#461) survit au filtre → la grâce reste armée.
        cleaned = filter_pip_noise(output)
        if not cleaned.strip():
            return (
                "Gate rouge sans diagnostic pytest exploitable — la sortie ne contenait que du bruit "
                "d'installation pip non bloquant (conflit avec des dépendances de l'image, hors livrable)."
            )
        return log_tail(cleaned, 400)
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
    # Vague 3 : preuve de livraison liée à la tête distante vérifiée et PERSISTÉE (None en dry-run / échec). La source
    # d'autorité reste l'état durable (``load_delivery_proof``) ; cette référence n'est qu'une commodité.
    proof: Optional[DeliveryProof] = None
    tested_content: Optional[TestedContent] = None


def expected_contract_task_ids(manager, project_id, issue: IssueSpec) -> Tuple[bool, frozenset, str]:
    """Contrats que la preuve BUILD doit avoir rejoués d'après l'ÉTAT DURABLE : ``(exigés, ids de tâches, motif)``.

    Exigés dès que ``Project.acceptance_tests_required`` ou qu'une tâche livrée porte un oracle ; les ids sont le
    contrat COURANT (``issue.source_task_id``) et TOUS les contrats déjà livrés. Un état illisible ou un contrat livré
    invérifiable donne ``(True, ∅, motif)`` : l'exigence reste, la preuve sera refusée. L'état prévaut sur le booléen
    du rapport du gate et sur la présence d'un checker : un défaut de câblage est refusé, pas deviné.
    """
    if manager is None or project_id is None:
        return False, frozenset(), ""
    try:
        required = project_requires_contracts(manager, project_id) or has_sealed_contracts(manager, project_id)
        if not required:
            return False, frozenset(), ""
        current = getattr(issue, "source_task_id", None)
        delivered = load_delivered_contracts(manager, project_id, exclude_task_id=current)
    except ContractError as exc:
        return True, frozenset(), str(exc)
    ids = {c.task_id for c in delivered}
    if isinstance(current, int) and not isinstance(current, bool):
        ids.add(current)
    return True, frozenset(ids), ""


def _contracts_verdict(
    report: QualityReport, *, expected_ids: frozenset = frozenset(), state_error: str = ""
) -> Tuple[bool, str]:
    """Verdict « contrats scellés » d'après les preuves structurées du gate (jamais un simple booléen)."""
    if state_error:
        return False, f"contrats exigés mais invérifiables dans l'état durable : {state_error}"
    if report.acceptance_passed is not True or report.acceptance_error:
        return False, str(report.acceptance_error or "contrats d'acceptation en échec")
    evidence = tuple(report.acceptance_evidence)
    if not evidence:
        return False, "aucune preuve d'oracle (contrats scellés exigés)"
    current = [e for e in evidence if e.role == "current"]
    if len(current) != 1:
        return False, "le contrat courant n'a pas été rejoué exactement une fois"
    if current[0].expected_preimage != "red-assertion":
        return False, "preuve négative non exigée/absente pour le contrat courant"
    failing = [e for e in evidence if not e.passed]
    if failing:
        return False, f"contrat {failing[0].role} de la tâche {failing[0].task_id} : {failing[0].reason}"
    missing = sorted(expected_ids - {e.task_id for e in evidence})
    if missing:
        return False, f"contrat(s) des tâches {missing} exigés par l'état durable mais non rejoués par le gate"
    return True, f"{len(evidence)} contrat(s) rejoué(s), même SHA-256 sur la préimage (rouge) et le candidat (vert)"


def draft_from_report(
    report: QualityReport,
    content: TestedContent,
    snapshot: DeliverySnapshot,
    *,
    phase: str = PHASE_BUILD,
    state_contracts: Tuple[bool, frozenset, str] = (False, frozenset(), ""),
) -> ProofDraft:
    """Verdicts de la preuve de livraison BUILD d'après le rapport du gate (contrainte requise ⇒ verdict requis).

    ``state_contracts`` (``expected_contract_task_ids``) : l'exigence de contrats vient de l'ÉTAT durable ; elle
    prévaut sur ``report.contracts_required`` (un gate sans checker câblé ne peut pas produire une preuve sans contrats).
    """
    state_required, expected_ids, state_error = state_contracts
    draft = ProofDraft(
        phase=phase,
        content=content,
        contracts_required=bool(getattr(report, "contracts_required", False)) or state_required,
        delivered_paths=snapshot.paths,
    )
    draft.add(
        "content_integrity", True, "arbre Git complet inchangé depuis le scellement, résidus non livrables retirés"
    )
    draft.add("tests", bool(report.tests_passed), f"code de sortie {report.test_exit_code}")
    review_ok = not report.review_blocking and not report.review_error
    review_reason = report.review_error or ("revue bloquante" if report.review_blocking else "revue non bloquante")
    draft.add("review", review_ok, str(review_reason))
    if report.adequacy_implemented is not None or report.adequacy_error:
        adequacy_ok = report.adequacy_implemented is not False and not report.adequacy_error
        draft.add("adequacy", adequacy_ok, report.adequacy_error or report.adequacy_justification or "")
    if draft.contracts_required:
        draft.oracles.extend(report.acceptance_evidence)
        ok, reason = _contracts_verdict(report, expected_ids=expected_ids, state_error=state_error)
        if state_required and not getattr(report, "contracts_required", False):
            ok, reason = (
                False,
                "le projet exige des contrats scellés mais le gate n'en a rejoué aucun (checker non câblé)",
            )
        draft.add("contracts", ok, reason)
    elif report.acceptance_passed is not None or report.acceptance_error:
        draft.add(
            "acceptance",
            report.acceptance_passed is True and not report.acceptance_error,
            str(report.acceptance_error or ""),
        )
    draft.add("gate", bool(report.passed), "verdict global du gate qualité")
    return draft


def _refusal_text(exc: Exception) -> str:
    """Motif d'un refus de publication, sans doubler le préfixe déjà porté par le message."""
    text = str(exc)
    return text if text.startswith("LIVRAISON REFUSÉE") else f"LIVRAISON REFUSÉE : {text}"


def _proof_refusal(draft: ProofDraft) -> str:
    return describe_refusal(draft)


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

    **Frontière Git (vague 1)** : le workspace créé ici est un workspace *géré*
    (métadonnées Git de contrôle hors montage, cf. :mod:`collegue.executor.git_boundary`).
    Toutes les opérations git hôte qui suivent l'exécution du code non fiable — seed,
    capture, recapture après remédiation — passent par la frontière ; le ``.git`` du
    workspace n'est jamais lu. ``runner`` (défaut ``None`` = production) est donc
    REFUSÉ sur un workspace géré : il est réservé aux fixtures non gérées des tests
    unitaires de ``run_issue``/``capture_diff``, pas à ce pipeline.
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

        # #582 : figer AVANT toute exécution du code projet les octets qui
        # pourront être livrés. Les tests/reviewers ne doivent jamais pouvoir
        # modifier après coup ce que ``open_pr`` enverra à GitHub.
        delivery_snapshot = capture_delivery_snapshot(
            workspace,
            execution.files_changed,
            diff=execution.diff,
        )

        # Vague 3 : (1) tout format NON représentable (binaire, lien) est refusé AVANT les contrôles ; (2) le contenu
        # testé est figé (arbre Git complet) et les résidus non livrables (fichiers ignorés/untracked que des tests
        # pourraient exploiter sans qu'ils soient livrés) sont retirés — les contrôles tournent sur ce que la
        # livraison contient réellement.
        stage = STAGE_GATE
        try:
            assert_deliverable(delivery_snapshot)
            content = seal_tested_content(workspace.path)
            assert_representable(content)
        except DeliveryProofError as exc:
            return ExecutionOutcome(
                success=False,
                stage=STAGE_GATE,
                workspace=workspace,
                execution=execution,
                final_status=final_status,
                reason=REASON_GATE_FAILED,
                error=str(exc),
            )

        # E3 : gate qualité (fail-closed).
        gate_kwargs = dict(gate_options or {})
        checker = gate_kwargs.get("acceptance_checker")
        if checker is not None and hasattr(checker, "bind_preimage"):
            # Preuve négative du contrat courant : même oracle (SHA-256) sur la préimage connue = clone neuf à la base
            # testée (jamais le workspace de l'agent).
            gate_kwargs["acceptance_checker"] = checker.bind_preimage(
                lambda: fresh_preimage_workspace(repo_source, issue, content.base_sha)
            )
        report = await run_quality_gate(
            workspace.path,
            execution.diff,
            ctx,
            sandbox=sandbox,
            reviewer=reviewer,
            issue=issue,
            **gate_kwargs,
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
            # La remédiation déterministe est la SEULE mutation autorisée du
            # snapshot initial. Toute dérive d'un autre fichier est bloquante.
            try:
                verify_delivery_snapshot(
                    workspace,
                    delivery_snapshot,
                    ignored_paths=("requirements.txt",),
                )
                verify_tested_content(workspace.path, content, allowed_paths=("requirements.txt",))
            except DeliveryDriftError as exc:
                return ExecutionOutcome(
                    success=False,
                    stage=STAGE_GATE,
                    workspace=workspace,
                    execution=execution,
                    quality_report=report,
                    final_status=final_status,
                    reason=REASON_GATE_FAILED,
                    error=f"INTÉGRITÉ DU LIVRABLE REFUSÉE : {exc}",
                )

            added_requirements = tuple(report.requirements_added)
            diff, files_changed = capture_diff(workspace, runner=runner, paths=("requirements.txt",))
            execution = replace(execution, diff=diff, files_changed=files_changed, changed=bool(files_changed))
            delivery_snapshot = capture_delivery_snapshot(
                workspace,
                execution.files_changed,
                diff=execution.diff,
            )
            try:
                assert_deliverable(delivery_snapshot)
                # Nouvel arbre testé : seuls les chemins de la remédiation entrent dans l'index de contrôle ; les
                # sorties écrites par le gate (node_modules, bases du smoke…) restent hors arbre et sont purgées.
                content = seal_tested_content(workspace.path, only_paths=("requirements.txt",))
                assert_representable(content)
            except DeliveryProofError as exc:
                return ExecutionOutcome(
                    success=False,
                    stage=STAGE_GATE,
                    workspace=workspace,
                    execution=execution,
                    quality_report=report,
                    final_status=final_status,
                    reason=REASON_GATE_FAILED,
                    error=str(exc),
                )

            # #582 : le premier reviewer/checker a vu l'ancien diff. Si le gate
            # était vert, rejouer le gate COMPLET sur le diff réellement livrable
            # (remédiation désactivée pour garantir une seule convergence bornée).
            if report.passed:
                recheck_kwargs = dict(gate_kwargs)
                recheck_kwargs["fix_missing_requirements"] = False
                report = await run_quality_gate(
                    workspace.path,
                    execution.diff,
                    ctx,
                    sandbox=sandbox,
                    reviewer=reviewer,
                    issue=issue,
                    **recheck_kwargs,
                )
                report.requirements_added = added_requirements
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

        try:
            verify_delivery_snapshot(workspace, delivery_snapshot)
            # Arbre COMPLET (fichiers de base compris, pas seulement le diff) : un test/une fixture qui modifie un
            # fichier suivi après le début du gate INVALIDE la preuve au lieu de certifier le contenu antérieur.
            verify_tested_content(workspace.path, content)
        except DeliveryDriftError as exc:
            # Le gate a exécuté du code non fiable en RW. Même avec un verdict
            # vert, une mutation post-snapshot invalide le contrat testé/revu.
            return ExecutionOutcome(
                success=False,
                stage=STAGE_GATE,
                workspace=workspace,
                execution=execution,
                quality_report=report,
                final_status=final_status,
                reason=REASON_GATE_FAILED,
                error=f"INTÉGRITÉ DU LIVRABLE REFUSÉE : {exc}",
            )

        draft = draft_from_report(
            report,
            content,
            delivery_snapshot,
            # Aperçu (dry_run) : aucune lecture d'état (le pilote garantit qu'un dry-run ne relit jamais la base) ; la
            # livraison RÉELLE applique l'exigence de contrats de l'état durable avant toute publication.
            state_contracts=(
                (False, frozenset(), "") if dry_run else expected_contract_task_ids(manager, project_id, issue)
            ),
        )
        if not draft.passed:
            return ExecutionOutcome(
                success=False,
                stage=STAGE_GATE,
                workspace=workspace,
                execution=execution,
                quality_report=report,
                final_status=final_status,
                reason=REASON_GATE_FAILED,
                error=_proof_refusal(draft),
            )

        # E4 : ouverture de PR (dry_run respecté).
        stage = STAGE_PR
        try:
            pr = open_pr(
                workspace,
                report,
                issue,
                owner,
                repo,
                files_changed=execution.files_changed,
                snapshot=delivery_snapshot,
                base=base,
                clients=clients,
                dry_run=dry_run,
                manager=manager,
                project_id=project_id,
                draft=draft,
            )
        except DeliveryRemoteError:
            raise  # distant illisible : panne d'infrastructure retentable (barrière #435), pas un défaut du code livré
        except DeliveryProofError as exc:
            # Refus de PUBLICATION (base déplacée, arbre distant différent, PR existante de révision différente,
            # preuve non persistable) : ce n'est pas une panne d'infrastructure, c'est un refus explicite.
            return ExecutionOutcome(
                success=False,
                stage=STAGE_PR,
                workspace=workspace,
                execution=execution,
                quality_report=report,
                final_status=final_status,
                reason=REASON_GATE_FAILED,
                error=_refusal_text(exc),
                tested_content=content,
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
        proof=pr.proof,
        tested_content=content,
    )
