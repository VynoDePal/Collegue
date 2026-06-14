"""Project Driver — pilote du moteur autonome (F3, epic #373, brief §7 Phase 3).

Assemble F1 (ordonnanceur) + F2 (budget-temps) + l'exécuteur Phase 2
(``execute_issue``) : tant que le budget/deadline tiennent et qu'il reste des
tâches prêtes, exécuter la prochaine, checkpointer, et basculer en mode
amélioration quand le MVP est atteint.

Boucle **séquentielle et bornée** : chaque itération traite une tâche ``todo`` et
la marque ``in_review`` (succès), la **re-file** ``todo`` avec backoff si l'échec
est encore retentable (``max_task_attempts``, #420), ou la marque ``failed``
(terminal). La terminaison reste garantie : chaque tâche consomme au plus
``max_task_attempts`` itérations (backstop ``max_iterations`` par sécurité).

``dry_run`` (défaut) : pipeline complet jusqu'aux **aperçus** de PR, **sans aucune
écriture** (ni GitHub ni état) ; la progression est simulée via un *overlay* en
mémoire (les statuts des objets ``Task`` détachés sont modifiés en RAM, pas en DB).

**Jamais** ``done`` automatiquement (le merge humain le fera). Reprise : l'état
persisté en DB (tâches déjà ``in_review``) est repris naturellement ; les
checkpoints C7 numérotent la progression.

Module **isolé** : non importé par ``app.py`` (F4 câblera).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from collegue.executor.agent import IssueSpec
from collegue.executor.openhands_agent import coder_pricing_resolvable, estimate_cost_usd
from collegue.executor.pipeline import (
    agent_crash_signature,
    execute_issue,
    failure_feedback,
    is_infra_agent_crash,
    is_infra_gate_failure,
    is_infra_noise,
    log_tail,
)
from collegue.executor.workspace import branch_for_issue, cleanup_workspace, sweep_stale_temp_clones
from collegue.pilot.audit import (
    BUDGET_EVENT,
    CHECKPOINT_SAVED,
    COST_PRICING_UNRESOLVED,
    COST_UNKNOWN,
    GATE_DECISION,
    OVERLAY_REFRESHED,
    PR_OPENED,
    RUN_STOP,
    TASK_FAILED,
    TASK_RECONCILED,
    TASK_RETRY,
    TASK_STARTED,
    USAGE_LOST,
    CostSource,
    NullAuditLog,
    RunAuditLog,
)
from collegue.pilot.budget import ACTION_DEADLINE, ACTION_PAUSED_BUDGET, BudgetTimeController
from collegue.pilot.resume import persist_run_start
from collegue.pilot.scheduler import (
    SATISFIED_STATUSES,
    SATISFIED_STATUSES_STRICT,
    next_task,
    ready_tasks,
    remaining_tasks,
)
from collegue.sandbox.executor import TIMEOUT_NOTE

# Statut projet une fois le MVP construit (le moteur d'amélioration = Phase 4).
PROJECT_STATUS_IMPROVING = "improving"
TASK_STATUS_TODO = "todo"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_IN_REVIEW = "in_review"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_MERGED = "merged"

# Raisons d'arrêt du pilote.
STOP_COMPLETED = "completed"  # plus rien à construire → MVP atteint
STOP_PAUSED_BUDGET = "paused_budget"
STOP_DEADLINE = "deadline_reached"
STOP_BLOCKED = "blocked"  # graphe coincé (dépendance échouée)
STOP_AWAITING_MERGE = "awaiting_merge"  # mode strict (#411) : tout est prêt SAUF des merges humains
STOP_SAFETY_CAP = "safety_cap"  # garde-fou anti-boucle
STOP_SANDBOX_BROKEN = "sandbox_broken"  # crash-loop d'import agent : image/runner cassé (#498)


class CostPricingUnresolvedError(RuntimeError):
    """Refus de démarrer : prix coder non résolvable et REQUIRE_COST_PRICING (#502)."""


# Retry au niveau tâche (#420). Le défaut du MODULE reste 1 (= pas de retry,
# comportement historique — module isolé, aucun changement sans opt-in) ; le
# runtime assemblé (F4) passe ``TASK_MAX_ATTEMPTS`` (défaut config : 3) pour que
# le chemin autonome réel soit, lui, résilient aux échecs transitoires.
DEFAULT_MAX_TASK_ATTEMPTS = 1
DEFAULT_RETRY_BACKOFF_SECONDS = 15.0
RETRY_BACKOFF_CAP_SECONDS = 90.0

# Intégration sérielle en mode strict (#434) : nombre max de PRs « en vol »
# (tâches ``in_review``) avant de s'arrêter ``awaiting_merge``. À 1, chaque tâche
# sœur se construit sur la précédente MERGÉE — des PRs sœurs ouvertes depuis la
# même base ne peuvent plus entrer en conflit entre elles (merge 405 irrécupérable).
DEFAULT_MAX_INFLIGHT_REVIEWS = 1

# Mémoire de la meilleure tentative (#436). Un diff tronqué ne s'appliquerait
# plus (git apply échouerait) : au-delà de ce plafond, on ne mémorise pas.
MAX_BEST_DIFF_CHARS = 200_000

# #482 : consigne append-only sur requirements.txt, injectée dans tout prompt de
# nouvelle tentative — en réparation, l'agent a déjà RÉGÉNÉRÉ le fichier au moins
# une fois (cas réel v4 : lignes de main perdues → « No module named 'jose' » en
# venv nu, 12 tests verts par ailleurs).
REQUIREMENTS_APPEND_ONLY_RULE = (
    "Règle stricte : requirements.txt est APPEND-ONLY — ne le régénère jamais, ne supprime "
    "ni ne remplace AUCUNE ligne existante ; ajoute uniquement les dépendances manquantes, "
    "chacune ÉPINGLÉE à une version (== X.Y.Z, ou bornes >=A,<B) — une dépendance nue casse "
    "l'installation vierge (passlib non contraint + bcrypt résolu librement → register en 500)."
)
_PASSED_RE = re.compile(r"(\d+) passed")
_FAILED_RE = re.compile(r"(\d+) failed")


# #459 : suffixe « aléa infra » accroché au diagnostic préservé — remplacé à
# chaque nouvel aléa (jamais empilé), pour que last_error reste borné.
_INFRA_NOISE_SUFFIX_RE = re.compile(r" \(\+ aléa infra[^)]*\)$")


def _attempt_score(outcome) -> Optional[Tuple[int, int]]:
    """Score de tests d'une tentative : ``(verts, -rouges)`` — comparable, ou None.

    ``None`` quand la sortie de tests est absente ou illisible (erreur de
    collection : 0 collecté) : un état qui ne collecte même pas n'est PAS un
    candidat « meilleure tentative » (c'est l'anti-exemple de la task 7 FacNor —
    l'ImportError n'aurait jamais dû remplacer le 26/27).
    """
    report = getattr(outcome, "quality_report", None)
    output = getattr(report, "test_output", "") if report is not None else ""
    if not output:
        return None
    passed_m = _PASSED_RE.search(output)
    failed_m = _FAILED_RE.search(output)
    passed = int(passed_m.group(1)) if passed_m else 0
    failed = int(failed_m.group(1)) if failed_m else 0
    if passed == 0 and failed == 0:
        return None
    return (passed, -failed)


logger = logging.getLogger(__name__)


@dataclass
class TaskOutcome:
    """Résultat de l'exécution d'une tâche par le pilote."""

    task_id: int
    title: str
    success: bool
    stage: str
    pr_number: Optional[int] = None


@dataclass
class ProjectRunResult:
    """Bilan d'un run du pilote."""

    stop_reason: str
    iterations: int
    processed: List[TaskOutcome] = field(default_factory=list)
    project_status: Optional[str] = None  # statut projet final écrit (ex. improving), sinon None
    improvement: Optional[object] = None  # ImprovementResult si le mode improving a été enchaîné (H5)
    # #440 : tâches encore ``in_review`` AU MOMENT du stop (ids) — du travail
    # TERMINÉ et validé (gate vert, PR ouverte) qui n'atteint `main` qu'après un
    # merge. Au ``STOP_DEADLINE`` en particulier, l'appelant DOIT drainer ces
    # reviews (passe de merge/réconciliation) au lieu de les découvrir en
    # post-mortem : deadline = « ne plus DÉMARRER de travail », pas « abandonner
    # le travail déjà validé ».
    pending_reviews: List[int] = field(default_factory=list)

    @property
    def opened_prs(self) -> List[int]:
        return [t.pr_number for t in self.processed if t.pr_number is not None]


def _issue_from_task(task, by_id=None) -> IssueSpec:
    """Construit l'``IssueSpec`` exécutable depuis une tâche persistée.

    Le numéro est celui de l'issue GitHub si la tâche est synchronisée
    (``issue_number``), sinon l'id de tâche (branche/``Closes`` cohérents).

    ``by_id`` (id → tâche) permet d'injecter un **contexte inter-tâches** (#412) :
    on liste les **dépendances déjà construites** pour que l'agent bâtisse sur
    l'existant (le code des dépendances est dans le dépôt) au lieu de coder depuis
    la seule consigne de l'issue → cohérence entre tâches.

    Retry (#424) : si la tâche a déjà échoué (``attempt_count`` > 0), le motif du
    dernier échec (``last_error``, synthèse crisp de :func:`failure_feedback`) est
    ré-injecté dans le contexte — sans lui, un retry rejoue la même consigne à
    l'identique et reproduit le même bug (retry « aveugle », gaspillage pur).
    Le contexte passe par ``IssueSpec.to_prompt`` → inline-isé (anti-injection).
    """
    parts = []
    deps = [by_id[d] for d in (task.depends_on or []) if by_id and d in by_id]
    # #411 : ne pas mentir à l'agent. Une dépendance `in_review` (PR ouverte, non
    # mergée) n'est PAS dans le clone (`main`) — la présenter comme « déjà
    # construite » pousserait l'agent à chercher du code absent.
    pending = [d for d in deps if getattr(d, "status", None) == "in_review"]
    built = [d for d in deps if d not in pending]
    if built:
        titres = ", ".join(f"« {d.title} »" for d in built)
        parts.append(
            f"Cette tâche dépend de tâches déjà construites : {titres}. "
            "Inspecte le dépôt existant et réutilise leur code, modèles et conventions "
            "(ne recrée pas ce qui existe)."
        )
    if pending:
        titres = ", ".join(f"« {d.title} »" for d in pending)
        parts.append(
            f"Attention : cette tâche dépend aussi de {titres}, dont la PR n'est PAS encore "
            "mergée — leur code peut être ABSENT du dépôt cloné. Reste cohérent avec leur "
            "périmètre (mêmes conventions et interfaces) sans dupliquer leur travail."
        )
    last_error = getattr(task, "last_error", None)
    attempts = int(getattr(task, "attempt_count", 0) or 0)
    best_diff = getattr(task, "best_diff", None)
    if best_diff:  # présence suffisante (#461 : une grâce peut laisser attempts=0)
        # #436 : le workspace du retry est RÉENSEMENCÉ avec la meilleure tentative
        # → consigne en mode réparation CIBLÉE (pas de régénération complète, qui
        # sous variance LLM détruit du travail quasi-abouti — oscillation).
        best_passed = int(getattr(task, "best_passed", 0) or 0)
        best_failed = int(getattr(task, "best_failed", 0) or 0)
        best_feedback = getattr(task, "best_feedback", None)
        msg = (
            "IMPORTANT : le dépôt cloné contient DÉJÀ (en modifications locales non commitées) "
            f"le travail de ta meilleure tentative précédente : {best_passed} test(s) verts, "
            f"{best_failed} en échec. Mode RÉPARATION CIBLÉE : "
        )
        if best_failed > 0 and best_feedback:
            msg += f"corrige UNIQUEMENT ces échecs : {best_feedback}. "
        elif last_error:
            msg += (
                f"cet état passait les tests ; la tentative a échoué pour une autre raison ({last_error}) — "
                "relivre-le en corrigeant cette cause. "
            )
        msg += "Ne réécris PAS ce qui passe déjà, ne repars pas de zéro."
        parts.append(msg)
    elif attempts > 0 and last_error:
        parts.append(
            "ATTENTION : ta tentative précédente sur CETTE tâche a ÉCHOUÉ. "
            f"Détail de l'échec : {last_error}. "
            "Analyse et corrige d'abord cette cause précise au lieu de régénérer le même code."
        )
    if best_diff or attempts > 0:
        # #482 : prompts de réparation/retry seulement — le prompt initial reste inchangé.
        parts.append(REQUIREMENTS_APPEND_ONLY_RULE)
    return IssueSpec(
        number=task.issue_number or task.id,
        title=task.title,
        body=task.acceptance or "",
        context=" ".join(parts),
    )


def reconcile_in_review_tasks(tasks, manager, clients, *, owner: str, repo: str, audit=None) -> int:
    """Réaligne les tâches ``in_review`` sur l'état RÉEL de leur PR GitHub (#442).

    Entre deux runs, des PRs sont mergées ou fermées **hors moteur** (opérateur,
    autre outil) : l'état persisté diverge alors silencieusement de la vérité
    GitHub — un redémarrage repartirait d'un état faux (MVP jamais « complet »,
    dépendants stricts bloqués ``awaiting_merge`` à tort). Pour chaque tâche
    ``in_review`` (PR retrouvée par sa branche déterministe) :

    - PR **mergée** → statut ``merged`` (terminal) ;
    - PR **fermée sans merge** → redo (``todo`` + feedback #424, même canal que
      le conflit #434) ;
    - PR encore ouverte / introuvable / GitHub injoignable → état conservé
      (**best-effort** : la réconciliation n'invente rien et ne tue pas le run).

    Mute les objets ``tasks`` (overlay) ET persiste via ``manager``. Retourne le
    nombre de tâches réalignées.
    """
    audit = audit or NullAuditLog()
    reconciled = 0
    for task in tasks:
        if task.status != TASK_STATUS_IN_REVIEW:
            continue
        branch = branch_for_issue(task.issue_number or task.id)
        try:
            pr = clients.prs.find_pr_by_head(owner, repo, branch, state="all")
        except Exception as exc:  # noqa: BLE001 - best-effort : GitHub down ≠ run mort
            logger.warning("réconciliation #442 : PR introuvable pour la tâche %s (%s) — état conservé", task.id, exc)
            continue
        if pr is None:
            continue
        if getattr(pr, "merged", False):
            task.status = TASK_STATUS_MERGED
            manager.update_task_status(task.id, TASK_STATUS_MERGED)
            audit.record(TASK_RECONCILED, task_id=task.id, pr_number=pr.number, outcome="merged")
            logger.info("tâche %s : PR #%s mergée hors-run → statut merged (#442)", task.id, pr.number)
            reconciled += 1
        elif getattr(pr, "state", "") == "closed":
            message = (
                f"[reconcile] ta PR #{pr.number} a été FERMÉE sans merge en dehors du run — "
                "son contenu n'est PAS dans le dépôt : repars du dépôt à jour et relivre la tâche."
            )
            fields = requeue_task_for_redo(manager, task.id, message=message)
            for key, value in fields.items():
                setattr(task, key, value)  # overlay mémoire aligné (#460)
            audit.record(TASK_RECONCILED, task_id=task.id, pr_number=pr.number, outcome="closed_requeued")
            logger.info("tâche %s : PR #%s fermée sans merge → redo (#442)", task.id, pr.number)
            reconciled += 1
    return reconciled


# #461 : aléas d'infrastructure pendant le GATE (timeout PyPI dans la passe
# d'installabilité, 5xx) — nombre max d'échecs non décomptés du budget de
# tentatives FONCTIONNELLES, par tâche et par run. Borné : si l'infra reste
# rouge, on recommence à décompter (pas de boucle infinie sur PyPI down).
MAX_INFRA_GATE_GRACE = 2

# #498 : N crashs agent CONSÉCUTIFS au traceback IDENTIQUE (hash des logs) =
# image/runner sandbox cassé, cause GLOBALE — inutile de gracier puis brûler le
# budget tâche par tâche. Fail-fast du run (stop_reason=sandbox_broken). Seuil
# bas car la signature est sûre (crash d'import, 0 token consommé).
MAX_AGENT_CRASH_LOOP = 3

# #466 : statuts pour lesquels un workspace d'échec conservé (#443) ne sert
# plus (la tâche a abouti) — purgé au balayage de démarrage, quel que soit le
# segment qui l'avait conservé.
_KEPT_WORKSPACE_DONE_STATUSES = frozenset({"in_review", "merged", "done"})

# #460 : marqueur du préfixe posé par requeue_task_for_redo dans best_feedback —
# détecté pour ne jamais empiler deux motifs de redo (le précédent est consommé).
_REQUEUE_FEEDBACK_MARKER = " (échecs connus de la meilleure tentative : "


def requeue_task_for_redo(manager, task_id: int, *, message: str, attempt_count: int = 1) -> dict:
    """Re-file une tâche pour un REDO complet après un événement externe (#434).

    Cas nominal : la PR d'une tâche ``in_review`` est devenue non mergeable
    (conflit avec ``main`` — :class:`~collegue.tools.github_commands.PRNotMergeableError`).
    L'orchestrateur ferme la PR puis appelle ce helper : la tâche repart ``todo``
    avec ``attempt_count``/``last_error`` posés pour que le canal feedback (#424)
    ré-injecte ``message`` dans le prompt de la nouvelle tentative (« ta PR était
    en conflit, repars du dépôt à jour »).

    ``attempt_count=1`` (défaut) : un conflit d'infrastructure ne consomme pas le
    budget de retries fonctionnels — on garde juste le minimum (> 0) pour que le
    feedback soit injecté.

    #460 : le message est AUSSI posé dans ``best_feedback`` — en mode réparation
    (#436, ``best_diff`` présent et échecs connus), le prompt n'utilise QUE
    ``best_feedback`` : un message laissé dans le seul ``last_error`` serait
    éclipsé et n'atteindrait jamais l'agent (constaté en run réel FacNor v3 :
    l'opérateur a dû écrire ``best_feedback`` à la main en base). L'existant est
    conservé en suffixe (les échecs connus restent visibles).

    Retourne les champs posés, pour que l'appelant aligne son overlay mémoire.
    """
    fields = {
        "status": TASK_STATUS_TODO,
        "attempt_count": max(1, int(attempt_count)),
        "last_error": str(message),
    }
    task = manager.get_task(task_id) if hasattr(manager, "get_task") else None
    previous_feedback = getattr(task, "best_feedback", None) if task is not None else None
    if previous_feedback and _REQUEUE_FEEDBACK_MARKER in previous_feedback:
        # Dé-nidifier : un requeue précédent a déjà préfixé — son motif est
        # consommé, seul le diagnostic d'origine reste pertinent (borné, même
        # convention que le suffixe « aléa infra » de #459).
        previous_feedback = previous_feedback.split(_REQUEUE_FEEDBACK_MARKER, 1)[1][:-1]
    if previous_feedback:
        fields["best_feedback"] = f"{message}{_REQUEUE_FEEDBACK_MARKER}{previous_feedback})"
    else:
        fields["best_feedback"] = str(message)
    manager.update_task(task_id, **fields)
    return fields


# #480 : champs réalignés depuis la base avant tout verdict terminal. Le statut
# pilote l'ordonnancement ; les autres portent le feedback d'un requeue externe
# (requeue_task_for_redo, #460) jusqu'au prompt de la tentative suivante.
# `kept_workspace` est volontairement EXCLU : le dict mémoire du run courant
# (#443) fait foi — un réalignement croisé pourrait re-pointer un chemin purgé.
_OVERLAY_REFRESH_FIELDS = (
    "status",
    "attempt_count",
    "last_error",
    "best_diff",
    "best_passed",
    "best_failed",
    "best_feedback",
)


def refresh_tasks_from_db(tasks, manager, project_id: int) -> int:
    """Réaligne l'overlay mémoire sur l'état persisté (#480) ; retourne le nb réaligné.

    L'overlay (objets détachés chargés une fois en tête de :func:`run_project`)
    devient PÉRIMÉ quand l'état bouge HORS de la boucle — requeue opérateur
    pendant le run (:func:`requeue_task_for_redo`, usage documenté #460) : la
    mémoire voit ``failed`` là où la base dit ``todo`` → verdict ``blocked`` à
    tort, la même seconde qu'une PR ouverte (run FacNor v4 : PR #108 abandonnée,
    2 relances opérateur — sans humain, run mort à 1/10). Mutation EN PLACE des
    objets : ``tasks_by_id``, ``pending_reviews`` et le contexte inter-tâches
    (#412) référencent ces instances. Best-effort : base injoignable ≠ run mort
    (l'overlay courant fait foi).
    """
    try:
        fresh = {t.id: t for t in manager.get_tasks(project_id)}
    except Exception as exc:  # noqa: BLE001 - best-effort, même convention que reconcile (#442)
        logger.warning("relecture base avant verdict (#480) impossible : %s — overlay conservé", exc)
        return 0
    realigned = 0
    for task in tasks:
        db_task = fresh.get(task.id)
        if db_task is None:
            continue
        changed = False
        for key in _OVERLAY_REFRESH_FIELDS:
            value = getattr(db_task, key, None)
            if getattr(task, key, None) != value:
                setattr(task, key, value)
                changed = True
        if changed:
            realigned += 1
    return realigned


async def run_project(
    project_id: int,
    repo_source: str,
    ctx,
    *,
    agent,
    owner: str,
    repo: str,
    manager,
    base: str = "main",
    budget: Optional[BudgetTimeController] = None,
    sandbox=None,
    reviewer=None,
    runner=None,
    clients=None,
    dry_run: bool = True,
    max_iterations: Optional[int] = None,
    audit: Optional[RunAuditLog] = None,
    cost_source: Optional[CostSource] = None,
    improve: bool = False,
    run_improvement_fn=None,
    max_task_attempts: int = DEFAULT_MAX_TASK_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    sleep_fn=None,
    require_merged_deps: bool = False,
    max_inflight_reviews: int = DEFAULT_MAX_INFLIGHT_REVIEWS,
    gate_options=None,
    reconcile_reviews: bool = True,
    cleanup_workspaces: bool = True,
    require_cost_pricing: bool = False,
) -> ProjectRunResult:
    """Pilote un projet : chaîne ``execute_issue`` sur les tâches prêtes sous budget.

    S'arrête quand : plus de tâche prête (``completed`` → bascule ``improving``),
    budget/deadline atteint, graphe bloqué, ou garde-fou ``max_iterations``.
    ``dry_run`` (défaut) ne persiste rien. Retourne un :class:`ProjectRunResult`.

    ``audit`` (H4) : journal d'audit du run, **non intrusif** — défaut
    :class:`NullAuditLog` (no-op) → comportement inchangé si non fourni.
    ``cost_source`` (H4) : callable ``() -> (usd, tokens)`` cumulés ; si fourni, le
    coût **par tâche** est échantillonné (delta) et alimente le ledger du run.
    ``None`` (défaut) → pas d'échantillonnage (``collegue.pilot.audit`` fournit
    ``default_process_cost_source`` à brancher par le runtime).

    ``improve`` (H5) : si vrai et le MVP est construit (réel), enchaîne le moteur
    d'amélioration (Phase 4) **sous le budget restant** et attache l'``ImprovementResult``
    à ``result.improvement``. Défaut faux → comportement inchangé.
    ``run_improvement_fn`` : injection de test ; défaut = ``collegue.improve.run_improvement``.

    ``max_task_attempts`` (#420) : tentatives max par tâche. À 1 (défaut du module),
    tout échec est terminal (comportement historique). Au-delà, un échec re-file la
    tâche ``todo`` avec un backoff linéaire ``retry_backoff_seconds × tentative``
    (plafonné à ``RETRY_BACKOFF_CAP_SECONDS``) tant que le plafond n'est pas
    atteint — un aléa transitoire (503, no-op) ne fige plus tout le DAG. Le compteur
    est persisté (``tasks.attempt_count``) → le plafond survit aux redémarrages.
    ``sleep_fn`` : injection de test (défaut ``asyncio.sleep``).

    ``require_merged_deps`` (#411) : si vrai, une dépendance ``in_review`` (PR non
    mergée) ne débloque PAS ses dépendants — leur clone (``main``) ne contiendrait
    pas son code. Quand seuls des merges humains manquent, le run s'arrête
    ``awaiting_merge`` (relancer après merge reprend naturellement). À faux
    (défaut historique), le démarrage d'un dépendant sur dépendance non mergée est
    SIGNALÉ (audit ``unmerged_deps`` + warning) au lieu d'être silencieux.

    ``gate_options`` (#438) : kwargs transmis tels quels au gate qualité via
    ``execute_issue`` (``test_command``, ``frontend_gate``…) — configuration du
    gate par projet sans coupler le pilote à la config.

    ``reconcile_reviews`` (#442, défaut vrai) : au démarrage d'un run RÉEL (avec
    ``clients``), réaligne chaque tâche ``in_review`` sur l'état GitHub de sa PR
    (mergée → ``merged`` ; fermée sans merge → redo) — le redémarrage après des
    merges manuels (post-deadline notamment) redevient idempotent.

    ``cleanup_workspaces`` (#443, défaut vrai) : le pilote détruit les clones
    ``/tmp/collegue-exec-*`` en fin de tâche — succès : suppression immédiate ;
    échec : on ne conserve que le DERNIER workspace de la tâche (debug), les
    précédents sont purgés (et celui d'une tâche finalement réussie aussi).
    Sans ça, un clone par tentative s'accumule jusqu'à l'erreur disque (233 Mo
    sur 7 h au run v2 — fuite linéaire). ``False`` : tout conserver (debug).

    ``max_inflight_reviews`` (#434, mode strict uniquement) : plafond de tâches
    ``in_review`` (PR ouverte non mergée) avant de s'arrêter ``awaiting_merge``.
    À 1 (défaut), l'intégration est SÉRIELLE : des tâches sœurs (indépendantes
    entre elles) ne sont plus construites depuis la même base — la base de
    chacune inclut le merge de la précédente, donc plus de PRs sœurs en conflit
    (merge 405 irrécupérable sans opérateur). Augmenter rétablit N PRs en vol
    (au risque documenté de #434). Ignoré hors mode strict (comportement
    historique inchangé : ``in_review`` débloque déjà les dépendants).
    """
    budget = budget or BudgetTimeController()
    audit = audit or NullAuditLog()
    # #495 : accumulateur CODER-SEUL (disjoint du MetricsCollector serveur) câblé
    # au budget — sans ça MAX_COST_USD ne mordait jamais sur la dépense coder,
    # pourtant majoritaire (18 M tokens au v4). Câblé ici car run_project est le
    # seul point où `budget` est en scope sur TOUS les chemins (runtime ET
    # harness FacNor qui construit son propre controller).
    coder_totals = {"usd": 0.0, "tokens": 0}
    _attach = getattr(budget, "attach_extra_totals", None)
    if callable(_attach):
        _attach(lambda: (coder_totals["usd"], coder_totals["tokens"]))
    try:
        max_task_attempts = max(1, int(max_task_attempts))
    except (TypeError, ValueError):
        max_task_attempts = DEFAULT_MAX_TASK_ATTEMPTS
    try:
        backoff = float(retry_backoff_seconds)
    except (TypeError, ValueError):
        backoff = DEFAULT_RETRY_BACKOFF_SECONDS
    if not (backoff > 0 and backoff < float("inf")):  # nan/inf/négatif → pas d'attente
        backoff = 0.0
    try:
        max_inflight_reviews = max(1, int(max_inflight_reviews))
    except (TypeError, ValueError):
        max_inflight_reviews = DEFAULT_MAX_INFLIGHT_REVIEWS
    sleep = sleep_fn or asyncio.sleep
    tasks = manager.get_tasks(project_id)  # objets détachés : overlay mutable en mémoire
    tasks_by_id = {t.id: t for t in tasks}  # pour le contexte inter-tâches (#412)

    # Ancrage du début de run (réel) : persiste le ``started_at`` pour qu'une reprise
    # reconstruise une deadline ABSOLUE (sinon elle glisse à chaque redémarrage). La
    # reconstruction du contrôleur depuis cette valeur est faite par le runtime (F4).
    # ``getattr`` : tolère un budget factice (tests) sans ``started_at``.
    started_at = getattr(budget, "started_at", None)
    if not dry_run and started_at is not None:
        persist_run_start(manager, project_id, started_at)

    # Coût par run : on échantillonne le cumul process avant/après chaque tâche et on
    # enregistre le delta (le ledger ignore les deltas nuls/aberrants).
    sample_cost = cost_source is not None
    last_usd, last_tokens = cost_source() if sample_cost else (0.0, 0)

    # Reprise : une tâche laissée `in_progress` est un reliquat d'un run interrompu
    # (crash / pause budget en plein execute_issue). La boucle étant SÉQUENTIELLE,
    # aucune tâche n'est réellement « en cours » au démarrage → on la repasse `todo`
    # pour la re-tenter (et éviter qu'un `in_progress` coincé soit pris pour un MVP
    # « terminé »). Persisté en réel ; overlay seul en dry_run.
    for task in tasks:
        if task.status == TASK_STATUS_IN_PROGRESS:
            task.status = TASK_STATUS_TODO
            if not dry_run:
                manager.update_task_status(task.id, TASK_STATUS_TODO)

    # #442 : réconciliation GitHub→état (réel uniquement, clients requis). Des PRs
    # ont pu être mergées/fermées HORS moteur depuis le dernier run : sans
    # réalignement, le redémarrage n'est pas idempotent.
    if reconcile_reviews and not dry_run and clients is not None:
        reconcile_in_review_tasks(tasks, manager, clients, owner=owner, repo=repo, audit=audit)

    # Avec retries, chaque tâche peut consommer jusqu'à max_task_attempts itérations.
    cap = (
        max_iterations
        if max_iterations is not None
        else len(tasks) * (max(2, max_task_attempts) + MAX_INFRA_GATE_GRACE) + 5
    )

    # Reprise : repartir du numéro d'itération du dernier checkpoint (l'état des
    # tâches en DB fournit la vraie reprise — les tâches terminées sont ignorées).
    latest = manager.get_latest_checkpoint(project_id) if not dry_run else None
    iteration = latest.iteration if latest is not None else 0

    processed: List[TaskOutcome] = []
    stop_reason = STOP_COMPLETED
    # #443 : dernier workspace CONSERVÉ par tâche (échec → debug). Toute nouvelle
    # tentative purge le précédent — au plus UN clone par tâche échouée survit.
    kept_workspaces: dict = {}
    # #461 : aléas infra du gate non décomptés (task_id → nombre), borné par
    # MAX_INFRA_GATE_GRACE — un timeout PyPI ne brûle plus une tentative saine.
    infra_gate_grace: dict = {}
    # #498 : compteur de crashs agent au traceback IDENTIQUE (hash → nombre
    # consécutif) — N d'affilée ⇒ fail-fast (image cassée). Réarmé dès qu'un
    # hash change ou qu'une issue NON-crash survient.
    agent_crash_loop: dict = {}
    # #484 : signal « coût inconnu » émis au plus UNE fois par run/segment
    # (anti-bruit — le flag en mémoire ne survit pas aux restarts, comme #443).
    cost_unknown_signaled = False

    # #466 : balayage de démarrage. (a) Les workspaces conservés PERSISTÉS des
    # tâches désormais abouties — le dict en mémoire de #443 repartait vide à
    # chaque restart, les clones des segments précédents re-fuyaient. (b) Les
    # clones de revert orphelins trop vieux (rien ne les référence).
    if cleanup_workspaces and not dry_run:
        for t in tasks:
            kept = getattr(t, "kept_workspace", None)
            if kept and getattr(t, "status", None) in _KEPT_WORKSPACE_DONE_STATUSES:
                cleanup_workspace(kept)
                t.kept_workspace = None
                manager.update_task(t.id, kept_workspace=None)
        swept = sweep_stale_temp_clones()
        if swept:
            logger.info("balayage démarrage : %d clone(s) collegue-revert-* périmé(s) supprimé(s)", swept)

    # #502 : visibilité du coût AU DÉMARRAGE (avant la boucle, en réel seulement).
    # Si aucun prix de secours coder n'est configuré et que litellm ne mappe pas
    # le modèle, le ledger $ restera à 0 et MAX_COST_USD sera inopérant côté
    # coder — l'opérateur doit le savoir maintenant, pas en post-mortem.
    if not dry_run and not coder_pricing_resolvable():
        logger.warning(
            "COÛT $ DU RUN POTENTIELLEMENT INCONNU : aucun prix de secours coder "
            "(LLM_PRICE_PROMPT_PER_1M / LLM_PRICE_COMPLETION_PER_1M). Si litellm ne mappe pas le "
            "modèle coder, le ledger $ restera à 0 et MAX_COST_USD sera INOPÉRANT côté coder. "
            "Configurez LLM_PRICE_*_PER_1M, ou REQUIRE_COST_PRICING=true pour refuser de démarrer."
        )
        audit.record(COST_PRICING_UNRESOLVED, iteration=iteration, dry_run=dry_run)
        if require_cost_pricing:
            raise CostPricingUnresolvedError(
                "REQUIRE_COST_PRICING=true et aucun prix coder (LLM_PRICE_*_PER_1M) résolvable : "
                "run refusé (ledger $ aveugle)."
            )

    while True:
        if len(processed) >= cap:
            stop_reason = STOP_SAFETY_CAP
            break

        decision = budget.should_continue()
        if not decision.ok:
            stop_reason = STOP_PAUSED_BUDGET if decision.action == ACTION_PAUSED_BUDGET else STOP_DEADLINE
            if decision.action not in (ACTION_PAUSED_BUDGET, ACTION_DEADLINE):  # défensif
                stop_reason = decision.action
            audit.record(BUDGET_EVENT, iteration=iteration, action=decision.action, reason=decision.reason)
            break

        dep_satisfied = SATISFIED_STATUSES_STRICT if require_merged_deps else SATISFIED_STATUSES
        task = next_task(tasks, satisfied=dep_satisfied)
        if task is None and not dry_run:
            # #480 : avant TOUT verdict terminal, relire la base — un requeue
            # externe (#460) a pu remettre des tâches `todo` que l'overlay voit
            # encore `failed` ; conclure `blocked` sur la seule mémoire éteint
            # le run (et abandonne les PR in_review validées) à tort. Jamais en
            # dry_run : rien n'y est persisté, l'overlay est la SEULE vérité.
            realigned = refresh_tasks_from_db(tasks, manager, project_id)
            if realigned:
                logger.info("verdict terminal : %d tâche(s) réalignée(s) depuis la base (#480)", realigned)
                audit.record(OVERLAY_REFRESHED, iteration=iteration, realigned=realigned)
                task = next_task(tasks, satisfied=dep_satisfied)
        if task is None:
            # Plus aucune tâche prête. En séquentiel, plus aucun reliquat
            # `in_progress` (remis à `todo` au démarrage) : s'il reste des tâches
            # non terminées, c'est soit (mode strict, #411) un graphe seulement en
            # attente de merges humains (des tâches seraient prêtes au sens
            # historique : rien d'autre ne manque) → `awaiting_merge` (un nouveau
            # run après merge reprend naturellement) ; soit un graphe coincé
            # (dépendance échouée) → bloqué ; sinon, tout est construit → MVP.
            if not remaining_tasks(tasks):
                stop_reason = STOP_COMPLETED
            elif require_merged_deps and ready_tasks(tasks):
                stop_reason = STOP_AWAITING_MERGE
            else:
                stop_reason = STOP_BLOCKED
            break

        # #434 (mode strict) : borner les PRs « en vol ». Une tâche est prête, mais
        # si ``max_inflight_reviews`` tâches sont déjà ``in_review``, la démarrer la
        # construirait sur une base qui n'inclut PAS ces PRs → dès que l'une merge,
        # les PRs sœurs touchant les mêmes fichiers deviennent non mergeables (405),
        # sans aucune issue moteur. On s'arrête ``awaiting_merge`` : après le(s)
        # merge(s) humains, un nouveau run repart d'une base à jour.
        if require_merged_deps:
            inflight = sum(1 for t in tasks if t.status == TASK_STATUS_IN_REVIEW)
            if inflight >= max_inflight_reviews:
                logger.info(
                    "mode strict : %d PR(s) en vol (plafond %d) — arrêt awaiting_merge "
                    "avant la tâche %s « %s » (#434, intégration sérielle)",
                    inflight,
                    max_inflight_reviews,
                    task.id,
                    task.title,
                )
                stop_reason = STOP_AWAITING_MERGE
                break

        # #411 (mode historique) : démarrer un dépendant alors qu'une dépendance est
        # `in_review` signifie que son clone (`main`) ne contient PAS le code de la
        # dépendance — on le SIGNALE (audit + warning) au lieu de le taire.
        unmerged_deps = [
            d
            for d in (task.depends_on or [])
            if d in tasks_by_id and getattr(tasks_by_id[d], "status", None) == "in_review"
        ]
        started_detail = {"task_id": task.id, "title": task.title}
        if unmerged_deps:
            started_detail["unmerged_deps"] = unmerged_deps
            logger.warning(
                "tâche %s « %s » démarrée alors que ses dépendances %s sont in_review (PR non "
                "mergée) : leur code n'est pas dans le clone — incohérence possible (#411). "
                "DEPS_REQUIRE_MERGED=true pour exiger le merge.",
                task.id,
                task.title,
                unmerged_deps,
            )
        audit.record(TASK_STARTED, iteration=iteration + 1, **started_detail)
        # #436 : au retry, réensemencer le workspace avec la meilleure tentative
        # (le diff survit en DB → la mémoire traverse aussi les redémarrages).
        # #436/#461 : la PRÉSENCE de best_diff suffit (un aléa infra gracié,
        # #461, peut laisser attempt_count à 0 alors qu'une tentative verte est
        # mémorisée — la régénérer de zéro gaspillerait le travail).
        seed = getattr(task, "best_diff", None) or None
        outcome = await execute_issue(
            _issue_from_task(task, tasks_by_id),
            repo_source,
            ctx,
            agent=agent,
            owner=owner,
            repo=repo,
            base=base,
            sandbox=sandbox,
            reviewer=reviewer,
            runner=runner,
            clients=clients,
            manager=manager,
            task_id=task.id,
            project_id=project_id,
            dry_run=dry_run,
            seed_diff=seed,
            gate_options=gate_options,
        )
        iteration += 1
        if sample_cost:
            cur_usd, cur_tokens = cost_source()
            audit.record_cost(usd=cur_usd - last_usd, tokens=int(cur_tokens - last_tokens), iteration=iteration)
            last_usd, last_tokens = cur_usd, cur_tokens
        # #441 : canal CODER — majoritaire en dépense et invisible du
        # MetricsCollector serveur (process distinct dans le sandbox). L'usage
        # auto-déclaré par l'agent alimente le même ledger (les deux canaux
        # s'additionnent : ils ne comptent jamais les mêmes appels).
        agent_result = outcome.execution.agent_result
        coder_prompt = int(getattr(agent_result, "prompt_tokens", 0) or 0)
        coder_completion = int(getattr(agent_result, "completion_tokens", 0) or 0)
        coder_tokens = coder_prompt + coder_completion
        coder_usd = float(getattr(agent_result, "cost_usd", 0.0) or 0.0)
        if coder_tokens and coder_usd <= 0:
            # #484 : modèle non mappé litellm → cost_usd=0 malgré des tokens.
            # Prix de secours configurés (LLM_PRICE_*_PER_1M) : coût estimé ;
            # sinon coût INCONNU — signalé une fois par run/segment au lieu
            # d'un 0 silencieux qui ressemble à un run gratuit.
            coder_usd = estimate_cost_usd(coder_prompt, coder_completion)
            if coder_usd <= 0 and not cost_unknown_signaled:
                cost_unknown_signaled = True
                audit.record(
                    COST_UNKNOWN,
                    iteration=iteration,
                    task_id=task.id,
                    tokens=coder_tokens,
                    reason="cost_usd nul malgré des tokens (modèle non mappé litellm ?) et aucun prix "
                    "LLM_PRICE_*_PER_1M configuré — plafond MAX_COST_USD inopérant pour le canal coder",
                )
        if coder_tokens or coder_usd:
            audit.record_cost(usd=coder_usd, tokens=coder_tokens, iteration=iteration)
            # #495 : alimente l'accumulateur coder-seul lu par le budget dur.
            # Même garde que RunCostLedger.add — un coder_usd aberrant (négatif,
            # NaN/inf) ne doit pas fausser ni empoisonner le total budgétaire.
            if math.isfinite(coder_usd) and coder_usd > 0:
                coder_totals["usd"] += coder_usd
            if coder_tokens > 0:
                coder_totals["tokens"] += coder_tokens
        elif TIMEOUT_NOTE in (getattr(agent_result, "logs", "") or ""):
            # #464 : tentative tuée de l'extérieur (timeout sandbox) AVANT toute
            # ligne [collegue-usage] — la dépense (la tentative la plus longue,
            # donc la plus coûteuse) est invisible du ledger : perte journalisée
            # au lieu d'un trou silencieux (budget sous-estimé en silence).
            audit.record(USAGE_LOST, iteration=iteration, task_id=task.id, reason="sandbox_timeout")
        pr_number = outcome.pr.number if outcome.pr is not None else None
        audit.record(
            GATE_DECISION,
            iteration=iteration,
            task_id=task.id,
            success=outcome.success,
            stage=outcome.stage,
            reason=outcome.reason,
        )
        if pr_number is not None:
            audit.record(PR_OPENED, iteration=iteration, task_id=task.id, pr_number=pr_number, dry_run=dry_run)
        processed.append(
            TaskOutcome(
                task_id=task.id,
                title=task.title,
                success=outcome.success,
                stage=outcome.stage,
                pr_number=pr_number,
            )
        )

        # #443 : hygiène des clones /tmp. Succès → le workspace ne sert plus (la
        # PR est ouverte) : suppression, y compris celui d'un échec précédent de
        # la même tâche. Échec → on garde CE workspace pour le debug et on purge
        # le précédent de la tâche (au plus un clone conservé par tâche échouée).
        if cleanup_workspaces:
            if outcome.success:
                cleanup_workspace(outcome.workspace)
                # #466 : le chemin PERSISTÉ couvre les clones conservés par un
                # segment précédent (le dict mémoire repartait vide au restart).
                previous = kept_workspaces.pop(task.id, None) or getattr(task, "kept_workspace", None)
                if previous:
                    cleanup_workspace(previous)
                if getattr(task, "kept_workspace", None):
                    task.kept_workspace = None
                    if not dry_run:
                        manager.update_task(task.id, kept_workspace=None)
            elif outcome.workspace is not None:
                previous = kept_workspaces.get(task.id) or getattr(task, "kept_workspace", None)
                if previous and previous != outcome.workspace.path:
                    cleanup_workspace(previous)
                kept_workspaces[task.id] = outcome.workspace.path
                task.kept_workspace = outcome.workspace.path
                if not dry_run:
                    manager.update_task(task.id, kept_workspace=outcome.workspace.path)

        # Overlay en mémoire (+ persistance en réel). Succès → in_review (déjà
        # persisté par execute_issue). Échec : tant qu'il reste des tentatives
        # (#420), la tâche est RE-FILÉE `todo` avec backoff — un aléa transitoire
        # (503, no-op) ne fige plus le DAG ; sinon `failed` (terminal, fail-closed).
        # Dans les deux cas la cause SURVIT (#421) : raison différenciée + extraits
        # bornés des logs agent / sortie des tests, audités (persistés via decisions)
        # et stockés dans tasks.last_error.
        if outcome.success:
            task.status = TASK_STATUS_IN_REVIEW
            agent_crash_loop = {}  # #498 : un succès réarme la détection de crash-loop
        else:
            detail = {"task_id": task.id, "stage": outcome.stage, "reason": outcome.reason}
            if getattr(outcome, "error", None):  # exception d'infrastructure (#435)
                detail["error"] = log_tail(outcome.error, 600)
            agent_tail = log_tail(outcome.execution.agent_result.logs)
            if agent_tail:
                detail["agent_log_tail"] = agent_tail
            if outcome.quality_report is not None and outcome.quality_report.test_output:
                detail["test_output_tail"] = log_tail(outcome.quality_report.test_output, 1000)

            # Synthèse CRISP (#424) : lignes FAILED/ERROR de pytest en priorité —
            # c'est ce qui sera ré-injecté dans le prompt de la tentative suivante.
            diagnostic = failure_feedback(outcome)

            # #498 : détection de crash-loop agent. Un crash d'import pré-LLM
            # (0 token, image/runner cassé) qui se rejoue à l'IDENTIQUE (même
            # hash de logs) sur plusieurs tâches/tentatives = cause GLOBALE —
            # inutile de gracier puis brûler tâche par tâche : on arrête le run
            # avec un diagnostic clair (rebuild d'image). Le `break` sort AVANT
            # d'incrémenter attempts/persister un échec : la tâche reste
            # retentable au prochain run (après rebuild).
            agent_crash = is_infra_agent_crash(outcome)
            if agent_crash:
                crash_logs = outcome.execution.agent_result.logs or ""
                # Identité STABLE (ligne d'exception, pas la queue brute) : les
                # logs réels portent ANSI/timestamps/chemins variables qui
                # casseraient un hash de la queue → fail-fast jamais déclenché.
                crash_sig = hashlib.sha1(agent_crash_signature(crash_logs).encode("utf-8")).hexdigest()
                streak = agent_crash_loop.get(crash_sig, 0) + 1
                agent_crash_loop = {crash_sig: streak}  # un hash différent réarme à zéro
                if streak >= MAX_AGENT_CRASH_LOOP:
                    stop_reason = STOP_SANDBOX_BROKEN
                    audit.record(
                        TASK_FAILED,
                        iteration=iteration,
                        attempt=int(getattr(task, "attempt_count", 0) or 0),
                        max_attempts=max_task_attempts,
                        sandbox_broken=True,
                        crash_streak=streak,
                        **detail,
                    )
                    logger.error(
                        "crash-loop agent : %d crashs d'import IDENTIQUES (image/sandbox cassé ?) — "
                        "arrêt du run (sandbox_broken). Reconstruire l'image puis relancer.",
                        streak,
                    )
                    break
            else:
                agent_crash_loop = {}  # toute issue NON-crash réarme le compteur

            # #461 : « le livrable ne s'installe pas » (fonctionnel — le but de la
            # passe #439) et « PyPI n'a pas répondu » (infra transitoire)
            # produisaient le même gate_failed décompté du budget : chaque
            # micro-coupure réseau rapprochait une tâche SAINE de l'échec
            # terminal. Un gate rouge au diagnostic purement infra ne consomme
            # pas de tentative fonctionnelle (grâce bornée par tâche). La
            # classification (#477) couvre aussi la cascade « install en échec
            # réseau → ModuleNotFoundError de collecte » via deps_install_failed.
            # #498 : un crash d'import pré-LLM est gracié comme un aléa de gate.
            infra_gate_failure = is_infra_gate_failure(outcome) or agent_crash
            grace_used = int(infra_gate_grace.get(task.id, 0))
            graced = infra_gate_failure and grace_used < MAX_INFRA_GATE_GRACE
            if graced:
                infra_gate_grace[task.id] = grace_used + 1
                attempts = int(getattr(task, "attempt_count", 0) or 0)  # NON décompté
                detail["infra_grace"] = grace_used + 1
            else:
                attempts = int(getattr(task, "attempt_count", 0) or 0) + 1
            task.attempt_count = attempts
            last_error = f"[{outcome.stage}/{outcome.reason}] tentative {attempts}/{max_task_attempts}"
            if graced:
                last_error += " (aléa infra non décompté)"
            if diagnostic:
                last_error += " — " + diagnostic
            # #459 : un aléa d'infrastructure (timeout PyPI, 5xx) n'écrase pas un
            # diagnostic actionnable — sinon l'échec terminal (et le redo) hérite
            # du feedback le MOINS utile de la série. Le suffixe est REMPLACÉ,
            # jamais empilé (borné sur les séries d'aléas).
            previous_error = str(getattr(task, "last_error", None) or "")
            # On ne classifie que la partie DIAGNOSTIC (après « — ») : le préfixe
            # « [stage/reason] tentative N/M » casserait le garde FAILED-en-tête
            # de is_infra_noise et désarmerait la protection pour tout projet web
            # dont les échecs de tests mentionnent ConnectionError.
            previous_diag = previous_error.split(" — ", 1)[-1]
            # #477 : la cascade d'install graciée porte un diagnostic d'apparence
            # fonctionnelle (ModuleNotFoundError fantôme) — la classification du
            # GATE fait foi, pas la forme du texte : elle aussi préserve.
            infra_diag = infra_gate_failure or is_infra_noise(diagnostic)
            if diagnostic and infra_diag and previous_error and not is_infra_noise(previous_diag):
                base = _INFRA_NOISE_SUFFIX_RE.sub("", previous_error)
                last_error = f"{base} (+ aléa infra à la tentative {attempts} : [{outcome.stage}/{outcome.reason}])"
            task.last_error = last_error

            # #436 : mémoriser la MEILLEURE tentative (diff + score + échecs). Le
            # prochain essai réensemence son workspace avec cet état (réparation
            # ciblée) — et une tentative PIRE ne remplace jamais une meilleure
            # (anti-oscillation : un 26/27 vert ne doit plus être jeté).
            best_fields = {}
            score = _attempt_score(outcome)
            diff = getattr(outcome.execution, "diff", "") or ""
            if score is not None and outcome.execution.changed and 0 < len(diff) <= MAX_BEST_DIFF_CHARS:
                current = (
                    (int(task.best_passed or 0), -int(task.best_failed or 0))
                    if getattr(task, "best_passed", None) is not None
                    else None
                )
                if current is None or score > current:
                    best_fields = {
                        "best_diff": diff,
                        "best_passed": score[0],
                        "best_failed": -score[1],
                        "best_feedback": diagnostic or None,
                    }
                    for key, value in best_fields.items():
                        setattr(task, key, value)  # overlay mémoire (dry_run inclus)

            if attempts < max_task_attempts:
                task.status = TASK_STATUS_TODO
                if not dry_run:
                    manager.update_task(
                        task.id, status=TASK_STATUS_TODO, attempt_count=attempts, last_error=last_error, **best_fields
                    )
                # max(attempts, 1) : une grâce #461 laisse attempts à 0 — sans
                # plancher, le retry repartirait SANS backoff contre l'infra qui
                # vient précisément de timeouter.
                delay = min(backoff * max(attempts, 1), RETRY_BACKOFF_CAP_SECONDS) if backoff > 0 else 0.0
                audit.record(
                    TASK_RETRY,
                    iteration=iteration,
                    attempt=attempts,
                    max_attempts=max_task_attempts,
                    backoff_seconds=delay,
                    **detail,
                )
                logger.warning(
                    "tâche %s « %s » re-tentée (%d/%d, stage=%s, reason=%s, backoff=%.0fs)",
                    task.id,
                    task.title,
                    attempts,
                    max_task_attempts,
                    outcome.stage,
                    outcome.reason,
                    delay,
                )
                if delay > 0 and not dry_run:  # l'aperçu (dry_run) n'attend pas
                    await sleep(delay)
            else:
                task.status = TASK_STATUS_FAILED
                if not dry_run:
                    manager.update_task(
                        task.id, status=TASK_STATUS_FAILED, attempt_count=attempts, last_error=last_error, **best_fields
                    )
                audit.record(
                    TASK_FAILED, iteration=iteration, attempt=attempts, max_attempts=max_task_attempts, **detail
                )
                logger.warning(
                    "tâche %s « %s » en échec TERMINAL (%d/%d, stage=%s, reason=%s)",
                    task.id,
                    task.title,
                    attempts,
                    max_task_attempts,
                    outcome.stage,
                    outcome.reason,
                )
        if not dry_run:
            # Checkpoint C7 : le numéro d'itération sert à la reprise ; ``state_json``
            # est un instantané d'audit (la reprise effective lit les statuts en DB).
            manager.save_checkpoint(
                project_id,
                iteration,
                state_json={"processed_task_ids": [t.task_id for t in processed]},
            )
            audit.record(CHECKPOINT_SAVED, iteration=iteration)

    audit.record(RUN_STOP, iteration=iteration, reason=stop_reason, iterations=len(processed))

    project_status: Optional[str] = None
    # MVP atteint : run terminé (``STOP_COMPLETED`` implique qu'aucune tâche n'a échoué
    # — sinon ``STOP_BLOCKED``) et le projet a des tâches, toutes satisfaites. Vrai aussi
    # à la REPRISE d'un MVP déjà construit (tâches déjà ``in_review`` en DB → ``processed``
    # vide) : on se base donc sur ``len(tasks) > 0`` et non sur ``processed`` (sinon une
    # reprise ne basculerait jamais en amélioration — le cas même que H5 doit couvrir).
    # ``len(tasks) > 0`` exclut le projet vide (anti-vacuité). Réel uniquement.
    mvp_built = stop_reason == STOP_COMPLETED and len(tasks) > 0 and not dry_run
    if mvp_built:
        manager.update_project(project_id, status=PROJECT_STATUS_IMPROVING)
        project_status = PROJECT_STATUS_IMPROVING

    # Mode `improving` (H5) : MVP construit → enchaîne le moteur d'amélioration
    # (Phase 4) sous le MÊME budget (donc le budget restant). Opt-in (``improve``) et
    # réel uniquement. Import paresseux → ``collegue.improve`` n'est pas tiré au simple
    # import du pilote.
    improvement = None
    if mvp_built and improve:
        run_imp = run_improvement_fn or _default_run_improvement
        improvement = await run_imp(
            project_id,
            repo_source,
            ctx,
            agent=agent,
            owner=owner,
            repo=repo,
            manager=manager,
            budget=budget,
            sandbox=sandbox,
            reviewer=reviewer,
            clients=clients,
            runner=runner,
            base=base,
            dry_run=dry_run,
        )

    return ProjectRunResult(
        stop_reason=stop_reason,
        iterations=len(processed),
        processed=processed,
        project_status=project_status,
        improvement=improvement,
        # #440 : exposer le travail validé encore en attente de merge — la boucle
        # s'arrête toujours ENTRE deux tâches (jamais en plein milieu), donc cette
        # photo est exacte quel que soit le motif d'arrêt (deadline incluse).
        pending_reviews=[t.id for t in tasks if t.status == TASK_STATUS_IN_REVIEW],
    )


async def _default_run_improvement(*args, **kwargs):
    """Adaptateur paresseux vers ``collegue.improve.run_improvement`` (Phase 4).

    Import différé : garde ``collegue.improve`` hors de l'import du pilote (isolation).
    """
    from collegue.improve import run_improvement

    return await run_improvement(*args, **kwargs)
