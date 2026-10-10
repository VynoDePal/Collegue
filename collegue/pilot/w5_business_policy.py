"""Politique de fusion de la fixture de campagne (W5) : contrôles protégés et provenance du check requis.

Ce module ne fait QUE des lectures GitHub et ne décide jamais d'une fusion : ``merge_policy`` l'appelle dans le chemin COMMUN
(BUILD, drain, reprise, Phase 5 et checks du revert) et traduit ses refus en ``MergeRefused``. Il ne dépend pas de
``merge_policy`` (aucun import circulaire) et n'importe rien de ``scripts/``.

**Identification (sans drapeau).** La politique s'applique à un candidat si, et seulement si, le dépôt est la fixture de campagne
(:data:`CAMPAIGN_REPOSITORY`) ET la branche de base est une base éphémère de campagne (:data:`CAMPAIGN_BASE_PREFIX`). Ces deux
valeurs viennent du projet lui-même (dépôt cible et branche de base de la PR), pas d'un réglage : aucune variable d'environnement,
aucun paramètre ne la désactive. Les installations hors campagne (autres dépôts, autres bases) sont inchangées. Pour la campagne,
l'absence du check requis dans les protections serveur est un REFUS (la politique n'est pas « absente = ignorée »).

**Contrôles protégés.** ``.github/`` ET ``ci/`` (workflows, CODEOWNERS, verrou de la pile approuvée) doivent être IDENTIQUES entre la
base de confiance et la tête : comparaison des entrées racine de l'arbre Git réel (type + SHA, qui couvre tout le sous-arbre), jamais
une liste de fichiers. Une lecture impossible ou tronquée refuse.

**Publication.** La même politique s'applique AVANT la première écriture distante (``executor.pr.open_pr``, chemin commun BUILD /
IMPROVE / reprises) : le contenu testé (arbre Git du contrôle local), la base testée, la base distante et le socle de confiance (commit de
bootstrap du manifeste du lancement, descendant direct de la graine) doivent avoir les MÊMES objets sous ``.github/`` et ``ci/`` ; le payload
réellement envoyé ne peut pas viser ces chemins. Voir :func:`assert_publication_clean`.

**Provenance du check.** Un check de bon nom et de bonne application ne prouve rien : une contribution peut demander un jeton Actions
en écriture et publier un check par l'API des checks, y compris sur une AUTRE tête. Le check ``Fixture tests`` doit donc être un
job RÉEL de l'exécution du workflow approuvé sur la tête attendue : ``check-run.id`` → ``actions/jobs/{id}`` (404 pour un check publié
par l'API) → ``actions/runs/{run_id}`` ; dépôt, tête, chemin du workflow, événement et succès sont comparés.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

CAMPAIGN_REPOSITORY = "VynoDePal/collegue-e2e-fixture"
CAMPAIGN_BASE_PREFIX = "collegue-business/"
CAMPAIGN_CHECK = "Fixture tests"
#: Application GitHub Actions : seule source admise du check requis (un job Actions, jamais un tiers).
CAMPAIGN_CHECK_APP_ID = 15368
CAMPAIGN_WORKFLOW_PATH = ".github/workflows/fixture-tests.yml"
#: Événements du workflow approuvé admis pour une PR (``push`` ne sert qu'à la branche du socle, jamais à une fusion).
CAMPAIGN_RUN_EVENTS = ("pull_request",)
PROTECTED_ROOTS = (".github", "ci")


class PolicyRefusal(Exception):
    """Refus de la politique de campagne ; ``kind`` ∈ {"controls", "provenance", "unavailable", "pending", "missing"}."""

    def __init__(self, reason: str, *, kind: str):
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


def applies(owner: str, repo: str, base: str) -> bool:
    """Le candidat vise-t-il la fixture de campagne, sur une base éphémère de campagne ?"""
    return f"{owner}/{repo}".lower() == CAMPAIGN_REPOSITORY.lower() and str(base).startswith(CAMPAIGN_BASE_PREFIX)


def _tree_entries(branches: Any, owner: str, repo: str, tree_sha: str) -> List[Dict[str, Any]]:
    """Entrées racine de l'arbre ; toute lecture impossible, tronquée ou malformée lève ``PolicyRefusal(unavailable)``."""
    try:
        getter = getattr(branches, "get_git_tree", None)
        if callable(getter):
            data = getter(owner, repo, tree_sha)
        else:
            data = branches._api_get(f"/repos/{owner}/{repo}/git/trees/{tree_sha}", {})
        entries = data["tree"]
        if data.get("truncated") or not isinstance(entries, list):
            raise ValueError("arbre tronqué ou malformé")
        return entries
    except Exception as exc:  # noqa: BLE001 - un arbre illisible ne prouve rien : fail-closed
        raise PolicyRefusal(f"arbre Git {str(tree_sha)[:12]} illisible: {exc}", kind="unavailable") from exc


def protected_objects(branches: Any, owner: str, repo: str, tree_sha: str) -> Dict[str, str]:
    """``racine → type:sha`` pour chaque racine protégée présente (absente = non listée)."""
    found: Dict[str, str] = {}
    for entry in _tree_entries(branches, owner, repo, tree_sha):
        if entry.get("path") in PROTECTED_ROOTS:
            found[str(entry["path"])] = f"{entry.get('type')}:{str(entry.get('sha')).lower()}"
    return found


def assert_controls_untouched(branches: Any, owner: str, repo: str, *, base_tree: str, head_tree: str) -> None:
    """Refuse toute tête dont ``.github/`` ou ``ci/`` diffère de la base de confiance (ajout, modification, suppression, renommage)."""
    trusted = protected_objects(branches, owner, repo, base_tree)
    proposed = protected_objects(branches, owner, repo, head_tree)
    changed = sorted(root for root in PROTECTED_ROOTS if trusted.get(root) != proposed.get(root))
    if changed:
        detail = "; ".join(f"{r}/: {trusted.get(r, 'absent')} → {proposed.get(r, 'absent')}" for r in changed)
        raise PolicyRefusal(
            "la contribution modifie les contrôles de la fixture ("
            + ", ".join(f"{r}/" for r in changed)
            + f") par rapport à la base de confiance ({detail}) : un contrôle ne se juge pas lui-même",
            kind="controls",
        )


def _status_code(exc: BaseException) -> Optional[int]:
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


@dataclass(frozen=True)
class Provenance:
    check_run_id: int
    job_id: int
    run_id: int
    workflow_path: str
    event: str


def verify_check_provenance(
    prs: Any, owner: str, repo: str, head_sha: str, observations: Iterable[Any]
) -> List[Provenance]:
    """Le check requis de la tête est un job réel du workflow approuvé exécuté pour CETTE tête (lectures seules, fail-closed).

    Refuse : check absent, check sans identifiant, identifiant qui n'est pas un job (404), API indisponible, job d'une autre tête
    ou d'un autre nom, exécution d'un autre dépôt, d'un autre fichier de workflow ou d'un autre événement, ou non réussie."""
    head = str(head_sha).lower()
    candidates = [
        o
        for o in observations
        if getattr(o, "name", None) == CAMPAIGN_CHECK
        and getattr(o, "app_id", None) == CAMPAIGN_CHECK_APP_ID
        and getattr(o, "kind", "check_run") == "check_run"
    ]
    if not candidates:
        raise PolicyRefusal(
            f"aucun check-run {CAMPAIGN_CHECK!r} de l'application {CAMPAIGN_CHECK_APP_ID} sur la tête", kind="missing"
        )
    proven: List[Provenance] = []
    full_name = f"{owner}/{repo}".lower()
    for observation in candidates:
        check_id = getattr(observation, "check_run_id", None)
        if not isinstance(check_id, int) or isinstance(check_id, bool) or check_id <= 0:
            raise PolicyRefusal(
                "le check requis n'expose pas d'identifiant de check-run : provenance non établie", kind="provenance"
            )
        try:
            job = prs.get_workflow_job(owner, repo, check_id)
        except Exception as exc:  # noqa: BLE001
            if _status_code(exc) == 404:
                raise PolicyRefusal(
                    f"le check-run {check_id} n'est pas un job d'une exécution de workflow (publié par l'API des checks ?)",
                    kind="provenance",
                ) from exc
            raise PolicyRefusal(f"lecture du job {check_id} impossible: {exc}", kind="unavailable") from exc
        if job.id != check_id or str(job.head_sha).lower() != head or job.name != CAMPAIGN_CHECK:
            raise PolicyRefusal(
                f"le job {job.id} n'est pas le job {CAMPAIGN_CHECK!r} de la tête {head[:12]} (tête {str(job.head_sha)[:12]}, "
                f"nom {job.name!r})",
                kind="provenance",
            )
        try:
            run = prs.get_workflow_run(owner, repo, job.run_id)
        except Exception as exc:  # noqa: BLE001
            kind = "provenance" if _status_code(exc) == 404 else "unavailable"
            raise PolicyRefusal(f"lecture de l'exécution {job.run_id} impossible: {exc}", kind=kind) from exc
        path = str(run.path).split("@", 1)[0]
        problems: List[str] = []
        if run.id != job.run_id:
            problems.append("exécution ≠ exécution du job")
        if str(run.head_sha).lower() != head:
            problems.append(f"tête de l'exécution {str(run.head_sha)[:12]} ≠ {head[:12]}")
        if path != CAMPAIGN_WORKFLOW_PATH:
            problems.append(f"workflow {path!r} ≠ {CAMPAIGN_WORKFLOW_PATH!r}")
        if run.event not in CAMPAIGN_RUN_EVENTS:
            problems.append(f"événement {run.event!r} non admis")
        if str(run.repository).lower() != full_name or str(run.head_repository or "").lower() != full_name:
            problems.append(f"dépôt de l'exécution {run.repository!r}/{run.head_repository!r} ≠ {full_name!r}")
        if problems:
            raise PolicyRefusal("exécution du check non conforme : " + " ; ".join(problems), kind="provenance")
        if run.status != "completed" or job.status != "completed":
            raise PolicyRefusal("exécution du workflow approuvé non terminée", kind="pending")
        if run.conclusion != "success" or job.conclusion != "success":
            raise PolicyRefusal(
                f"exécution du workflow approuvé non réussie (job {job.conclusion!r}, exécution {run.conclusion!r})",
                kind="provenance",
            )
        proven.append(Provenance(check_id, job.id, run.id, path, run.event))
    return proven


# ── publication : aucune écriture distante qui touche un contrôle (garde COMMUN de ``executor.pr.open_pr``) ───────────────────

#: Racine de confiance IMMUABLE de la campagne : la graine ``main`` du dépôt fixture (le socle en est le descendant direct).
CAMPAIGN_SEED_SHA = "8e3691d8e4f311e00d620c9c2ca2d9edbd8b136a"
#: Le manifeste du socle approuvé est fourni par l'ENVIRONNEMENT du lancement (workflow de confiance), jamais par la contribution.
TRUST_ANCHOR_ENV = "W5_BOOTSTRAP_MANIFEST"
BOOTSTRAP_SCHEMA = "collegue-fixture-bootstrap/1"
REGULAR_MODES = ("100644", "100755")
_HEX40 = re.compile(r"[0-9a-f]{40}")

Rows = Dict[str, Tuple[str, str]]  # chemin Git -> (mode, sha d'objet)


def _segments(path: str) -> List[str]:
    cleaned = unicodedata.normalize("NFKC", str(path)).replace("\\", "/")
    return [segment for segment in cleaned.split("/") if segment not in ("", ".")]


def is_protected_path(path: str) -> bool:
    """Le chemin est-il (ou se dissimule-t-il sous) un contrôle ? Normalisation NFKC, séparateurs, ``./``, casse, espaces et points
    terminaux : ``CI/x``, ``.GitHub/w.yml``, ``ｃｉ/x``, ``./ci/x``, ``ci./x`` sont des contrôles. ``..`` est traité comme protégé
    (un chemin non normalisé ne se juge pas, il se refuse)."""
    segments = _segments(path)
    if not segments:
        return False
    if ".." in segments:
        return True
    head = segments[0].strip().rstrip(". ").casefold()
    return head in PROTECTED_ROOTS


def select_protected(rows: Mapping[str, Tuple[str, str]]) -> Rows:
    return {path: value for path, value in rows.items() if is_protected_path(path)}


def read_remote_rows(branches: Any, owner: str, repo: str, tree_sha: str) -> Rows:
    """Objets (blobs, liens, sous-modules) de l'arbre Git DISTANT, récursif.

    Panne de l'API ⇒ ``transient`` (refus RETENTABLE : rien n'a été écrit) ; arbre tronqué ou malformé ⇒ ``unavailable`` (refus définitif :
    une relecture ne prouverait pas davantage)."""
    try:
        getter = getattr(branches, "get_git_tree", None)
        if callable(getter):
            data = getter(owner, repo, tree_sha, recursive=True)
        else:
            data = branches._api_get(f"/repos/{owner}/{repo}/git/trees/{tree_sha}", {"recursive": "1"})
    except Exception as exc:  # noqa: BLE001 - un arbre illisible ne prouve rien : fail-closed
        raise PolicyRefusal(f"arbre Git distant {str(tree_sha)[:12]} illisible: {exc}", kind="transient") from exc
    try:
        entries = data["tree"]
        if data.get("truncated") or not isinstance(entries, list):
            raise ValueError("arbre tronqué ou malformé")
        rows: Rows = {}
        for entry in entries:
            if entry.get("type") == "tree":
                continue
            rows[str(entry["path"])] = (str(entry["mode"]), str(entry["sha"]).lower())
        return rows
    except Exception as exc:  # noqa: BLE001
        raise PolicyRefusal(f"arbre Git distant {str(tree_sha)[:12]} illisible: {exc}", kind="unavailable") from exc


def describe_differences(expected: Mapping[str, Tuple[str, str]], observed: Mapping[str, Tuple[str, str]]) -> List[str]:
    out: List[str] = []
    for path in sorted(set(expected) | set(observed)):
        before, after = expected.get(path), observed.get(path)
        if before == after:
            continue
        kind = "ajouté" if before is None else "supprimé" if after is None else "modifié"
        out.append(f"{path} ({kind})")
    return out


@dataclass(frozen=True)
class TrustAnchor:
    bootstrap_sha: str
    tree_sha: str
    rows: Rows


def load_trust_anchor(branches: Any, owner: str, repo: str, manifest_path: Optional[str]) -> TrustAnchor:
    """Socle de confiance : le commit de bootstrap DÉCLARÉ par le manifeste du lancement, relu et validé par l'API.

    Exigences : manifeste lisible et de forme attendue (schéma, dépôt, graine, préfixes protégés), commit descendant DIRECT de la
    graine immuable, arbre portant le workflow approuvé. Le manifeste complet (octets, ruleset…) est validé par le préflight de la
    campagne ; ici on n'en tire que l'ancre des contrôles. Absence ou incohérence ⇒ refus (jamais « pas d'ancre = pas de garde »)."""
    if not manifest_path:
        raise PolicyRefusal(
            f"socle de confiance non fourni ({TRUST_ANCHOR_ENV} absent) : la publication sur la fixture est refusée",
            kind="unavailable",
        )
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyRefusal(
            f"manifeste du socle illisible ({type(exc).__name__}) : publication refusée", kind="unavailable"
        ) from exc
    bootstrap = str(manifest.get("bootstrap_sha") or "").lower() if isinstance(manifest, dict) else ""
    problems = []
    if not isinstance(manifest, dict) or manifest.get("schema") != BOOTSTRAP_SCHEMA:
        problems.append("schéma")
    elif str(manifest.get("repository", "")).lower() != CAMPAIGN_REPOSITORY.lower():
        problems.append("dépôt")
    elif str(manifest.get("seed_sha", "")).lower() != CAMPAIGN_SEED_SHA:
        problems.append("graine")
    elif manifest.get("protected_prefixes") != [f"{root}/" for root in PROTECTED_ROOTS]:
        problems.append("préfixes protégés")
    elif not _HEX40.fullmatch(bootstrap):
        problems.append("bootstrap_sha")
    if problems:
        raise PolicyRefusal(
            "manifeste du socle incohérent (" + ", ".join(problems) + ") : publication refusée", kind="controls"
        )
    try:
        commit = branches.get_git_commit(owner, repo, bootstrap)
    except Exception as exc:  # noqa: BLE001
        raise PolicyRefusal(f"commit du socle {bootstrap[:12]} illisible: {exc}", kind="transient") from exc
    if list(commit.parents) != [CAMPAIGN_SEED_SHA]:
        raise PolicyRefusal(
            "le commit du socle n'est pas un descendant direct de la graine immuable : publication refusée",
            kind="controls",
        )
    rows = select_protected(read_remote_rows(branches, owner, repo, commit.tree_sha))
    if CAMPAIGN_WORKFLOW_PATH not in rows:
        raise PolicyRefusal(
            "le socle de confiance ne porte pas le workflow approuvé : publication refusée", kind="controls"
        )
    return TrustAnchor(bootstrap_sha=bootstrap, tree_sha=commit.tree_sha, rows=rows)


def assert_publication_clean(
    *,
    anchor: Mapping[str, Tuple[str, str]],
    remote_base: Mapping[str, Tuple[str, str]],
    local_base: Mapping[str, Tuple[str, str]],
    tested: Mapping[str, Tuple[str, str]],
    payload_paths: Sequence[str],
) -> None:
    """Quatre témoins DOIVENT coïncider sur les contrôles protégés : le socle de confiance, la base distante, la base locale testée et le
    contenu testé (qui sera publié). Puis aucun chemin du payload réellement envoyé ne peut viser un contrôle. Toute différence, tout
    objet irrégulier (lien, sous-module, mode inattendu) ou chemin non normalisé refuse — avant toute écriture distante."""
    for label, rows in (("base distante", remote_base), ("base testée", local_base), ("contenu testé", tested)):
        diff = describe_differences(anchor, rows)
        if diff:
            subject = (
                "la livraison modifie les contrôles" if rows is tested else f"la {label} diverge du socle de confiance"
            )
            raise PolicyRefusal(
                f"{subject} de la fixture (.github/, ci/) : {'; '.join(diff[:10])}"
                + (" …" if len(diff) > 10 else "")
                + " — un contrôle ne se publie pas par la voie qu'il juge",
                kind="controls",
            )
    irregular = sorted(path for path, (mode, _sha) in tested.items() if mode not in REGULAR_MODES)
    if irregular:
        raise PolicyRefusal(
            "objet irrégulier (lien ou sous-module) sous les contrôles de la fixture (.github/, ci/) : "
            + ", ".join(irregular[:10]),
            kind="controls",
        )
    hit = sorted(path for path in payload_paths if is_protected_path(path))
    if hit:
        raise PolicyRefusal(
            "le payload à publier vise les contrôles de la fixture (.github/, ci/) : " + ", ".join(hit[:10]),
            kind="controls",
        )


def assert_remote_head_clean(
    branches: Any, owner: str, repo: str, *, head_tree_sha: str, anchor: Mapping[str, Tuple[str, str]]
) -> None:
    """Relecture DISTANTE d'une tête (PR préexistante, ou tête publiée) : ses contrôles sont ceux du socle, même si elle a déjà été
    publiée avant — une publication antérieure ne rend rien sûr."""
    head = select_protected(read_remote_rows(branches, owner, repo, head_tree_sha))
    diff = describe_differences(anchor, head)
    if diff:
        raise PolicyRefusal(
            "la tête distante modifie les contrôles de la fixture (.github/, ci/) : " + "; ".join(diff[:10]),
            kind="controls",
        )
