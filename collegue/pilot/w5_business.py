"""Campagne réelle W5 : socle de bootstrap prouvé, identité consommée, modèles imposés, R04 (amélioration) et R05 (incident).

Complète ``w4_business`` (rapport, préflight, BUILD, vérification métier) sans le dupliquer. Ce module ne contient AUCUN appel de
modèle : il ordonne des appels PUBLICS du produit (``run_project_from_settings``) et JUGE leurs effets sur l'état durable et sur
GitHub. Il n'injecte aucun verdict de revue, de mesure ou de fusion : si une garde refuse la contribution, ce refus est conservé
et l'étape est déclarée non exercée / incomplète, jamais réussie.

* **Socle** : le manifeste de bootstrap n'est jamais une preuve à lui seul (:func:`validate_bootstrap_manifest` relit l'identité du
  dépôt, l'immuabilité de ``main``, l'ascendance, les objets Git réels et le check requis de son application réelle).
* **Identité de campagne** : :func:`claim_campaign_identity` crée DURABLEMENT une référence côté GitHub avant toute création
  distante ; un identifiant consommé ne donne jamais un nouvel essai gratuit.
* **R04** : amélioration LIVRÉE (fusionnée par Phase 5) avec gain mesuré par le vrai scan, contrats rejoués, documents distincts du
  support d'incident.
* **R05** : incident DÉTERMINISTE explicitement signalé (:class:`DeterministicIncidentAgent`, aucun modèle) puis contrôles réels :
  santé rouge, vraie PR de revert avec checks, fusion, arbre restauré, incident ``recovered``, acquittement CAS, reprise.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from collegue.pilot import w4_business as business
from collegue.pilot.w4_business import (
    BASE_BRANCH_PREFIX,
    FIXTURE_REPOSITORY,
    FIXTURE_REPOSITORY_ID,
    FIXTURE_ROOT_BRANCH,
    FIXTURE_SEED_FILES,
    FIXTURE_SEED_SHA,
    LEGAL_NOTICE,
    MODEL_CODER_FALLBACK,
    MODEL_PRIMARY,
    BudgetStop,
    CampaignReport,
    IncompleteValidation,
    Step,
)

# ── constantes du contrat commun W5 ───────────────────────────────────────────────────────────────────────────────────────

ROLES = ("PLANNER", "QA", "REVIEWER", "CODER")
BOOTSTRAP_SCHEMA = "collegue-fixture-bootstrap/1"
REQUIRED_CHECK = "Fixture tests"
RULESET_BRANCH_PATTERN = f"refs/heads/{BASE_BRANCH_PREFIX}/*"
BOOTSTRAP_MANIFEST_ENV = "W5_BOOTSTRAP_MANIFEST"
CLAIM_PREFIX = "collegue-business-claims/"
OFFICIAL_GOOGLE_HOST = "generativelanguage.googleapis.com"

#: Documents d'EXEMPLE fournis par le socle (contenu factice publié dans les documentations des fournisseurs). Distincts :
#: R04 en retire les identifiants, R05 garde les siens pour que son gain reste mesurable après R04.
R04_DOC = "docs/runbook-ops.md"
R05_DOC = "docs/deploiement.md"
HEADER_DOC = "docs/export_header.md"
SOCLE_DOCS = (R04_DOC, R05_DOC)
INCIDENT_DOCS = (R05_DOC, HEADER_DOC)
#: Identifiants d'exemple FACTICES (AWS documentation) : motif d'une clé d'accès et d'un secret de 40 caractères.
FAKE_CREDENTIAL_LINE = re.compile(
    r"AKIA[0-9A-Z]{16}|(?:secret[_ ]?access[_ ]?key|SECRET_ACCESS_KEY)\s*[=:]\s*\S{40}", re.I
)

_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SAFE_PATH = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*")


# ── modèles imposés et relais budgétaire ──────────────────────────────────────────────────────────────────────────────────


def _setting(settings: Any, name: str) -> str:
    value = getattr(settings, name, None)
    if value is None:
        return ""
    reveal = getattr(value, "get_secret_value", None)
    return str(reveal() if callable(reveal) else value).strip()


def check_gemma_models(settings: Any, step: Step) -> None:
    """Gemma 4 31B pour TOUS les rôles ; repli du CODEUR seulement 26B ; destination Google (jamais reclassée)."""
    problems: List[str] = []
    provider, model = _setting(settings, "LLM_PROVIDER").lower(), _setting(settings, "LLM_MODEL")
    if provider != "gemini":
        problems.append(f"LLM_PROVIDER doit valoir 'gemini' (vu {provider!r})")
    if model != MODEL_PRIMARY:
        problems.append(f"LLM_MODEL doit valoir {MODEL_PRIMARY!r} (vu {model!r})")
    for role in ROLES:
        role_provider, role_model = (
            _setting(settings, f"LLM_PROVIDER_{role}").lower(),
            _setting(settings, f"LLM_MODEL_{role}"),
        )
        if role_provider not in ("", "gemini"):
            problems.append(f"LLM_PROVIDER_{role} détourne le rôle vers {role_provider!r}")
        if role_model not in ("", MODEL_PRIMARY):
            problems.append(f"LLM_MODEL_{role} doit valoir {MODEL_PRIMARY!r} (vu {role_model!r})")
    for name in ("LLM_BASE_URL",) + tuple(f"LLM_BASE_URL_{role}" for role in ROLES):
        endpoint = _setting(settings, name)
        if endpoint and OFFICIAL_GOOGLE_HOST not in endpoint:
            problems.append(f"{name} ne vise pas l'endpoint officiel Google")
    fallbacks = [item.strip() for item in _setting(settings, "CODER_FALLBACK_MODELS").split(",") if item.strip()]
    if fallbacks != [MODEL_CODER_FALLBACK]:
        problems.append(f"CODER_FALLBACK_MODELS doit valoir exactement {MODEL_CODER_FALLBACK!r} (vu {fallbacks!r})")
    for substitute in ("CODER_SUBSCRIPTION",):
        if str(getattr(settings, substitute, False)).strip().lower() in {"true", "1", "yes", "on"}:
            problems.append(f"{substitute} est un mode de substitution interdit")
    step.evidence.update(primary=MODEL_PRIMARY, coder_fallback=MODEL_CODER_FALLBACK, roles=list(ROLES))
    if problems:
        raise IncompleteValidation("modèles imposés non respectés : " + " ; ".join(problems))


def broker_capability_proof() -> Callable[[Any], Mapping[str, Any]]:
    """Preuve de capacité du transport RÉELLEMENT instancié, fournie par le lot A (``collegue.broker.capability_proof``).

    Absente ⇒ validation incomplète explicite : aucune compatibilité n'est déduite d'une classe, d'un nom ou d'un flag."""
    try:
        from collegue.broker import capability_proof  # type: ignore[attr-defined]
    except ImportError as exc:
        raise IncompleteValidation(
            "interface publique du relais budgétaire (collegue.broker.capability_proof, lot A) absente : la compatibilité du "
            "transport choisi ne peut pas être établie avant la campagne"
        ) from exc
    return capability_proof


def check_broker_selection(
    settings: Any, step: Step, *, proof: Optional[Callable[[Any], Mapping[str, Any]]] = None
) -> None:
    """Le relais budgétaire est sélectionné ET sa capacité effective est prouvée par l'interface publique d'A."""
    transport = _setting(settings, "LLM_TRANSPORT").lower()
    step.evidence["configured_transport"] = transport
    if transport != "budget_broker":
        raise IncompleteValidation(
            f"LLM_TRANSPORT doit valoir 'budget_broker' (vu {transport!r}) : aucun transport direct"
        )
    capability = dict((proof or broker_capability_proof())(settings))
    step.evidence["capability"] = {
        k: v for k, v in capability.items() if "key" not in k.lower() and "secret" not in k.lower()
    }
    if capability.get("transport") != "budget_broker" or capability.get("accepted") is not True:
        raise IncompleteValidation(
            "capacité du relais budgétaire non établie par l'interface d'A : "
            + str(capability.get("reason") or "accepted != True")
        )


# ── manifeste de bootstrap : jamais une preuve à lui seul ─────────────────────────────────────────────────────────────────


def load_bootstrap_manifest(path: str) -> Dict[str, Any]:
    """Lit le manifeste fourni par l'appelant (JSON). Son contenu est ensuite VÉRIFIÉ par l'API, jamais cru."""
    if not path:
        raise IncompleteValidation(f"{BOOTSTRAP_MANIFEST_ENV} absent : aucun socle approuvé à valider")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IncompleteValidation(f"manifeste de bootstrap illisible ({type(exc).__name__})") from exc
    if not isinstance(data, dict):
        raise IncompleteValidation("manifeste de bootstrap malformé (objet JSON attendu)")
    return data


def _manifest_shape(manifest: Mapping[str, Any]) -> Tuple[str, Dict[str, str]]:
    expected = {
        "schema": BOOTSTRAP_SCHEMA,
        "repository": FIXTURE_REPOSITORY,
        "repository_id": FIXTURE_REPOSITORY_ID,
        "seed_sha": FIXTURE_SEED_SHA,
        "required_check": REQUIRED_CHECK,
        "branch_pattern": RULESET_BRANCH_PATTERN,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise RuntimeError(f"manifeste de bootstrap : {key} doit valoir {value!r} (vu {manifest.get(key)!r})")
    bootstrap_sha = str(manifest.get("bootstrap_sha") or "").lower()
    if not _SHA.fullmatch(bootstrap_sha):
        raise RuntimeError("manifeste de bootstrap : bootstrap_sha invalide")
    for key in ("check_app_id", "ruleset_id"):
        if not isinstance(manifest.get(key), int) or isinstance(manifest.get(key), bool) or manifest[key] <= 0:
            raise RuntimeError(f"manifeste de bootstrap : {key} doit être un entier positif")
    approved = manifest.get("approved_files")
    if not isinstance(approved, dict) or not approved:
        raise RuntimeError("manifeste de bootstrap : approved_files (chemin → sha256) requis")
    for path, digest in approved.items():
        if (
            not isinstance(path, str)
            or not _SAFE_PATH.fullmatch(path)
            or path.startswith(".")
            and not path.startswith(".github/")
        ):
            raise RuntimeError(f"manifeste de bootstrap : chemin approuvé invalide {path!r}")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise RuntimeError(f"manifeste de bootstrap : sha256 invalide pour {path!r}")
        if path in FIXTURE_SEED_FILES:
            raise RuntimeError(f"manifeste de bootstrap : {path!r} appartient à la graine immuable")
    return bootstrap_sha, dict(approved)


def read_tree_blobs(clients: Any, owner: str, repo: str, tree_sha: str) -> Dict[str, Tuple[str, str]]:
    """``chemin → (mode, sha de blob)`` de l'arbre Git RÉEL (Git Data API, récursif). Tout objet qui n'est pas un fichier régulier
    (lien, sous-module) ou un arbre tronqué est refusé : le contenu n'est jamais déduit d'une liste déclarative.

    Utilise ``get_git_tree`` du client s'il existe ; sinon la route REST en lecture seule (besoin publié : voir
    ``reports/w5-b-interfaces.md``)."""
    getter = getattr(clients.branches, "get_git_tree", None)
    if callable(getter):
        data = getter(owner, repo, tree_sha, recursive=True)
    else:
        data = clients.branches._api_get(f"/repos/{owner}/{repo}/git/trees/{tree_sha}", {"recursive": "1"})
    if not isinstance(data, dict) or data.get("truncated") or not isinstance(data.get("tree"), list):
        raise IncompleteValidation("arbre Git du socle tronqué ou illisible : contenu exact non établi")
    blobs: Dict[str, Tuple[str, str]] = {}
    for entry in data["tree"]:
        if entry.get("type") == "tree":
            continue
        if entry.get("type") != "blob" or entry.get("mode") != "100644":
            raise RuntimeError(
                f"objet non régulier dans le socle : {entry.get('path')!r} ({entry.get('type')}/{entry.get('mode')})"
            )
        blobs[str(entry["path"])] = (str(entry["mode"]), str(entry["sha"]))
    return blobs


def _file_text(clients: Any, owner: str, repo: str, path: str, ref: str) -> Optional[str]:
    try:
        return str(clients.files.get_file_content(owner, repo, path, branch=ref)["content"])
    except Exception as exc:  # noqa: BLE001 - 404 = absent ; toute autre erreur = preuve non établie
        if getattr(exc, "status_code", None) == 404 or "404" in str(exc) or "Not Found" in str(exc):
            return None
        raise IncompleteValidation(f"lecture de {path}@{ref[:12]} impossible ({type(exc).__name__})") from exc


def validate_bootstrap_manifest(
    manifest: Mapping[str, Any], clients: Any, step: Step, *, run_tag: str
) -> Dict[str, Any]:
    """Valide le socle par l'API : le manifeste décrit, l'API prouve.

    Contrôles : forme fermée du manifeste ; identité du dépôt ; ``main`` toujours égal à la graine (immuable) ; ``bootstrap_sha``
    descendant DIRECT de la graine ; arbre Git réel = graine inchangée + EXACTEMENT les fichiers approuvés (octets hachés relus) ;
    workflow du check requis présent parmi les fichiers approuvés ; ruleset actif et check requis associé à l'application
    déclarée, appliqué à l'acteur sur le motif de branche de la campagne. Retourne les preuves relues (consignées)."""
    bootstrap_sha, approved = _manifest_shape(manifest)
    owner, _, repo = FIXTURE_REPOSITORY.partition("/")
    info = clients.repos.get_repo(owner, repo)
    if (
        int(getattr(info, "id", 0) or 0) != FIXTURE_REPOSITORY_ID
        or str(getattr(info, "full_name", "")).lower() != FIXTURE_REPOSITORY.lower()
    ):
        raise RuntimeError("identité du dépôt fixture incorrecte")
    if getattr(info, "default_branch", None) != FIXTURE_ROOT_BRANCH or bool(getattr(info, "is_private", True)):
        raise RuntimeError("le dépôt fixture doit être public avec main pour branche par défaut")
    main_tip = str(clients.branches.get_branch_sha(owner, repo, FIXTURE_ROOT_BRANCH) or "").lower()
    if main_tip != FIXTURE_SEED_SHA:
        raise RuntimeError("main n'est plus la graine immuable : le socle ne doit jamais modifier main")
    commit = clients.branches.get_git_commit(owner, repo, bootstrap_sha)
    if list(commit.parents) != [FIXTURE_SEED_SHA]:
        raise RuntimeError("le commit de bootstrap n'est pas un descendant direct de la graine")
    relation = clients.branches.compare_commits(owner, repo, FIXTURE_SEED_SHA, bootstrap_sha)
    if relation.status != "ahead" or relation.ahead_by != 1:
        raise RuntimeError(
            f"ascendance du bootstrap incorrecte (statut {relation.status!r}, ahead_by {relation.ahead_by})"
        )
    seed_tree = read_tree_blobs(
        clients, owner, repo, clients.branches.get_git_commit(owner, repo, FIXTURE_SEED_SHA).tree_sha
    )
    boot_tree = read_tree_blobs(clients, owner, repo, commit.tree_sha)
    if sorted(seed_tree) != sorted(FIXTURE_SEED_FILES):
        raise RuntimeError("la graine ne contient pas exactement ses huit fichiers connus")
    changed_seed = sorted(path for path in seed_tree if boot_tree.get(path) != seed_tree[path])
    if changed_seed:
        raise RuntimeError(f"le socle modifie des fichiers de la graine : {changed_seed}")
    extra = sorted(set(boot_tree) - set(seed_tree))
    if extra != sorted(approved):
        raise RuntimeError(
            f"contenu du socle ≠ fichiers approuvés (en trop : {sorted(set(extra) - set(approved))} ; "
            f"manquants : {sorted(set(approved) - set(extra))})"
        )
    for path, expected in sorted(approved.items()):
        text = _file_text(clients, owner, repo, path, bootstrap_sha)
        if text is None:
            raise RuntimeError(f"fichier approuvé introuvable dans le socle : {path}")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != expected:
            raise RuntimeError(f"octets du socle ≠ sha256 approuvé : {path}")
        if _file_text(clients, owner, repo, path, FIXTURE_SEED_SHA) is not None:
            raise RuntimeError(f"fichier approuvé déjà présent dans la graine : {path}")
    workflow = [path for path in approved if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml"))]
    if not workflow:
        raise RuntimeError("aucun workflow approuvé ne produit le check requis")
    produced = _workflow_jobs(clients, owner, repo, workflow, bootstrap_sha)
    if REQUIRED_CHECK not in produced:
        raise RuntimeError(f"aucun workflow approuvé ne produit le check requis {REQUIRED_CHECK!r}")
    ruleset = clients.branches.get_ruleset(owner, repo, int(manifest["ruleset_id"]))
    if ruleset.enforcement != "active" or ruleset.target != "branch":
        raise RuntimeError(f"ruleset {ruleset.id} non actif sur des branches (enforcement {ruleset.enforcement!r})")
    from collegue.pilot.merge_policy import MergeRefused, discover_server_policy

    probe = f"{BASE_BRANCH_PREFIX}/{run_tag}"
    try:
        policy = discover_server_policy(clients, owner, repo, probe)
    except MergeRefused as refused:
        raise IncompleteValidation(f"protections W3 non établies sur {probe!r} : {refused.reason}") from refused
    matching = [
        c for c in policy.required_checks if c.context == REQUIRED_CHECK and c.app_id == manifest["check_app_id"]
    ]
    if not matching or not policy.strict_sources:
        raise RuntimeError(
            f"le check requis {REQUIRED_CHECK!r} n'est pas associé à l'application {manifest['check_app_id']} "
            f"(checks effectifs : {[(c.context, c.app_id) for c in policy.required_checks]}) ou la règle « à jour » manque"
        )
    evidence = {
        "bootstrap_sha": bootstrap_sha,
        "bootstrap_tree": commit.tree_sha,
        "main_tip": main_tip,
        "approved_files": sorted(approved),
        "workflow_jobs": sorted(produced),
        "ruleset_id": ruleset.id,
        "required_check": REQUIRED_CHECK,
        "check_app_id": manifest["check_app_id"],
        "strict_sources": list(policy.strict_sources),
    }
    step.evidence.update(evidence)
    return evidence


def _workflow_jobs(clients: Any, owner: str, repo: str, paths: Sequence[str], ref: str) -> List[str]:
    """Noms des jobs (donc des checks) que produisent les workflows approuvés, dédéclenchés par ``pull_request``."""
    import yaml

    names: List[str] = []
    for path in paths:
        document = yaml.safe_load(_file_text(clients, owner, repo, path, ref) or "") or {}
        triggers = document.get("on", document.get(True))  # YAML lit `on` comme un booléen
        triggered = (
            triggers
            if isinstance(triggers, (list, tuple))
            else list(triggers)
            if isinstance(triggers, dict)
            else [triggers]
        )
        if "pull_request" not in triggered:
            continue
        for key, job in (document.get("jobs") or {}).items():
            names.append(str((job or {}).get("name") or key))
    return names


def verify_fixture_controls_intact(
    clients: Any, manifest: Mapping[str, Any], ref: str, *, label: str
) -> Dict[str, str]:
    """Les contrôles de la fixture (workflows approuvés) sont INCHANGÉS sur ``ref`` : un BUILD ou une amélioration ne peut pas
    réécrire le check qui le juge. Échec ⇒ ``RuntimeError`` (jamais une réussite)."""
    owner, _, repo = FIXTURE_REPOSITORY.partition("/")
    _bootstrap, approved = _manifest_shape(manifest)
    seen: Dict[str, str] = {}
    for path, expected in sorted(approved.items()):
        if not path.startswith(".github/"):
            continue
        text = _file_text(clients, owner, repo, path, ref)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else "absent"
        seen[path] = digest
        if digest != expected:
            raise RuntimeError(f"contrôle de la fixture altéré ({label}) : {path} ({digest[:12]} ≠ {expected[:12]})")
    return seen


# ── identité de campagne consommée ────────────────────────────────────────────────────────────────────────────────────────


def claim_ref(campaign_id: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,39}", campaign_id or ""):
        raise IncompleteValidation(f"identifiant de campagne invalide : {campaign_id!r}")
    return f"{CLAIM_PREFIX}{campaign_id}"


def _branch_or_none(clients: Any, owner: str, repo: str, branch: str) -> Optional[str]:
    try:
        return str(clients.branches.get_branch_sha(owner, repo, branch))
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "status_code", None) == 404 or "404" in str(exc) or "Not Found" in str(exc):
            return None
        raise IncompleteValidation(f"lecture de la revendication impossible ({type(exc).__name__})") from exc


def check_campaign_identity_unused(clients: Any, campaign_id: str, step: Step) -> None:
    """Préflight (lecture seule) : l'identifiant n'a jamais été revendiqué."""
    owner, _, repo = FIXTURE_REPOSITORY.partition("/")
    ref = claim_ref(campaign_id)
    step.evidence["claim_ref"] = ref
    if _branch_or_none(clients, owner, repo, ref) is not None:
        raise IncompleteValidation(
            f"l'identifiant de campagne {campaign_id!r} est déjà consommé : un nouvel essai exige un nouvel identifiant, "
            "jamais un essai gratuit sur un budget et un projet neufs"
        )


def claim_campaign_identity(clients: Any, campaign_id: str, report: CampaignReport) -> str:
    """REVENDIQUE durablement l'identifiant (référence ``collegue-business-claims/<id>`` sur la graine) AVANT toute création
    distante. Création EXCLUSIVE : une revendication existante refuse. Jamais supprimée par le nettoyage."""
    owner, _, repo = FIXTURE_REPOSITORY.partition("/")
    ref = claim_ref(campaign_id)
    try:
        clients.branches.create_branch(owner, repo, ref, from_branch=FIXTURE_ROOT_BRANCH)
    except Exception as exc:  # noqa: BLE001 - existe déjà (422) ou GitHub indisponible : dans les deux cas, aucun lancement
        if _branch_or_none(clients, owner, repo, ref) is not None:
            raise IncompleteValidation(
                f"identifiant de campagne {campaign_id!r} déjà consommé : lancement refusé"
            ) from exc
        raise IncompleteValidation(
            f"revendication de l'identifiant impossible ({type(exc).__name__}) : lancement refusé"
        ) from exc
    report.facts["campaign_identity_claim"] = {"ref": ref, "durable": True, "deleted_by_cleanup": False}
    return ref


def revalidate_and_claim(clients: Any, env: Mapping[str, str], campaign_id: str, report: CampaignReport) -> str:
    """Juste avant la première création distante : le socle est REVALIDÉ par l'API (le préflight est plus ancien), puis
    l'identifiant est revendiqué. Un échec ici ne laisse AUCUNE ressource à nettoyer."""
    manifest = load_bootstrap_manifest(str(env.get(BOOTSTRAP_MANIFEST_ENV, "") or ""))
    run_tag = f"{env.get('GITHUB_RUN_ID', '0')}-{env.get('GITHUB_RUN_ATTEMPT', '0')}"
    step = Step("launch-revalidation", "Socle revalidé avant la création de la base")
    validate_bootstrap_manifest(manifest, clients, step, run_tag=run_tag)
    report.facts["bootstrap_revalidated"] = step.evidence
    return claim_campaign_identity(clients, campaign_id, report)


# ── incident DÉTERMINISTE (explicitement signalé) ─────────────────────────────────────────────────────────────────────────


@dataclass
class DeterministicIncidentAgent:
    """Codeur D'INJECTION : aucun modèle, aucune dépense. Applique le patch documentaire de faible risque de l'incident contrôlé
    (retire les identifiants d'exemple du document d'incident et la mention légale de l'en-tête d'export).

    L'injection est la SEULE partie déterministe de R05 : mesure, revue, politique de fusion, checks, santé indépendante, revert,
    acquittement et reprise sont ceux de production. Si le patch n'a aucun effet (la mention n'est pas où l'on l'attend), le
    résultat est « aucun diff » et R05 se termine incomplet : on n'invente rien."""

    # Capacité budgétaire déclarée au garde de worker : double SANS dépense (aucun appel de modèle) — c'est exactement sa définition.
    budget_enforcement = "test-double"
    announced = "INJECTION DÉTERMINISTE (aucun modèle) : patch documentaire de faible risque"
    calls: int = 0
    touched: List[str] = field(default_factory=list)

    def implement_issue(self, workspace: Any, issue: Any) -> Any:
        from collegue.executor import AgentResult

        self.calls += 1
        root = Path(str(getattr(workspace, "path", workspace)))
        changed: List[str] = []
        header = root / HEADER_DOC
        if header.is_file():
            kept = [
                line
                for line in header.read_text(encoding="utf-8").splitlines(keepends=True)
                if LEGAL_NOTICE not in line
            ]
            if "".join(kept) != header.read_text(encoding="utf-8"):
                header.write_text("".join(kept), encoding="utf-8")
                changed.append(HEADER_DOC)
        deploy = root / R05_DOC
        if deploy.is_file():
            kept = [
                line
                for line in deploy.read_text(encoding="utf-8").splitlines(keepends=True)
                if not FAKE_CREDENTIAL_LINE.search(line)
            ]
            if "".join(kept) != deploy.read_text(encoding="utf-8"):
                deploy.write_text("".join(kept), encoding="utf-8")
                changed.append(R05_DOC)
        self.touched = changed
        return AgentResult(
            success=bool(changed), files_changed=tuple(changed), summary=self.announced, cost_authoritative=True
        )


# ── services des phases R04 / R05 ─────────────────────────────────────────────────────────────────────────────────────────


@dataclass
class PhaseServices:
    """Dépendances des phases. En production : l'entrée publique du produit et les vrais clients GitHub ; en test : les mêmes
    fonctions avec un faux fournisseur et des clients de test (jamais un verdict injecté)."""

    run_pass: Callable[..., Awaitable[Any]]  # (context, *, improve, agent) -> ProjectRunResult (entrée publique)
    clients: Any
    manager: Callable[[], Any]
    resume_incident: Callable[
        [Mapping[str, Any]], Awaitable[Any]
    ]  # (context) -> objet found / continue_loop / stop_reason / reason
    verify_tip: Callable[[Mapping[str, Any], str], Any]  # (contexte, sha) -> BusinessObservation (sonde indépendante)
    owner: str
    repo: str
    base_branch: Callable[[Mapping[str, Any]], str] = lambda context: str(context["base_branch"])
    deadline_monotonic: Optional[float] = None
    clock: Callable[[], float] = None  # type: ignore[assignment]
    incident_agent: Any = None
    manifest: Optional[Mapping[str, Any]] = None
    controls_ref: Optional[Callable[[Mapping[str, Any]], str]] = None

    def remaining(self) -> Optional[float]:
        import time

        if self.deadline_monotonic is None:
            return None
        return self.deadline_monotonic - (self.clock or time.monotonic)()


def _drive(services: PhaseServices, coroutine: Awaitable[Any], label: str) -> Any:
    """Exécute une passe publique sous l'échéance globale restante (aucune remise à zéro, aucune grâce)."""
    remaining = services.remaining()
    if remaining is not None and remaining <= 0:
        raise BudgetStop(f"échéance globale atteinte avant {label} : aucune nouvelle génération")

    async def runner() -> Any:
        return await asyncio.wait_for(coroutine, timeout=remaining)

    try:
        return asyncio.run(runner())
    except asyncio.TimeoutError:
        raise BudgetStop(
            f"échéance globale atteinte pendant {label} : passe interrompue (état durable conservé)"
        ) from None


def _refusals(improvement: Any) -> str:
    rows = [f"{name}: {reason}" for name, reason in list(getattr(improvement, "rejected", []) or [])]
    return " | ".join(rows) if rows else "aucun motif consigné"


def _changed_files(clients: Any, owner: str, repo: str, number: int) -> List[str]:
    return sorted(
        str(getattr(item, "filename", getattr(item, "path", item)))
        for item in clients.prs.get_pr_files(owner, repo, number)
    )


def _tip_and_tree(services: PhaseServices, context: Mapping[str, Any]) -> Tuple[str, str]:
    base = services.base_branch(context)
    tip = str(services.clients.branches.get_branch_sha(services.owner, services.repo, base)).lower()
    return tip, services.clients.branches.get_git_commit(services.owner, services.repo, tip).tree_sha


def _check_controls(services: PhaseServices, context: Mapping[str, Any], label: str) -> None:
    if services.manifest is None:
        return
    ref = services.controls_ref(context) if services.controls_ref else services.base_branch(context)
    verify_fixture_controls_intact(services.clients, services.manifest, ref, label=label)


def run_improvement_phase(report: CampaignReport, context: Dict[str, Any], services: PhaseServices) -> None:
    """R04 — amélioration RÉELLE par l'entrée publique IMPROVE (le vrai modèle écrit le correctif) : livrée = fusionnée."""
    step = report.step("R04-improvement")
    _check_controls(services, context, "avant R04")
    tip_before, tree_before = _tip_and_tree(services, context)
    result = _drive(services, services.run_pass(context, improve=True, agent=None), "R04")
    improvement = getattr(result, "improvement", None)
    step.evidence.update(entry="run_project_from_settings(improve=True)", stop_reason=getattr(result, "stop_reason", None),
                         project_status=getattr(result, "project_status", None), tip_before=tip_before)  # fmt: skip
    if improvement is None:
        raise IncompleteValidation(
            f"amélioration non lancée (arrêt du produit : {getattr(result, 'stop_reason', None)})"
        )
    promoted = [item for item in improvement.promoted if not item.reverted]
    if not promoted:
        raise IncompleteValidation(
            f"aucune amélioration promue ({improvement.stop_reason}) — refus conservés : {_refusals(improvement)}"
        )
    item = promoted[0]
    proof = item.proof
    if proof is None or not proof.passed or proof.phase != "improve" or not proof.contracts_required:
        raise AssertionError("amélioration promue sans preuve IMPROVE durable complète")
    delivered = [o for o in proof.oracles if o.role == "delivered"]
    tasks = len(services.manager().get_tasks(context["project_id"]))
    if len(delivered) != tasks or any(o.candidate.status != "green" for o in delivered):
        raise AssertionError(f"contrats livrés non tous rejoués verts ({len(delivered)}/{tasks})")
    if not item.delta > 0 or not (improvement.final_score or 0) > (improvement.initial_score or 0):
        raise AssertionError("aucun gain mesuré par le vrai scan")
    files = _changed_files(services.clients, services.owner, services.repo, item.pr_number)
    if not files or any(not (path.startswith("docs/") and path.endswith(".md")) for path in files):
        raise IncompleteValidation(f"la PR d'amélioration n'est pas purement documentaire : {files}")
    if R04_DOC not in files or set(files) & set(INCIDENT_DOCS):
        raise IncompleteValidation(f"documents de R04 ≠ {R04_DOC} seul (support d'incident préservé) : {files}")
    info = services.clients.prs.get_pr(services.owner, services.repo, item.pr_number)
    if not (item.auto_merged and info.merged and info.merge_commit_sha):
        raise IncompleteValidation(
            f"PR #{item.pr_number} promue mais NON fusionnée par Phase 5 : amélioration non livrée "
            f"({getattr(improvement, 'stop_reason', None)} — {_refusals(improvement)})"
        )
    tip_after, tree_after = _tip_and_tree(services, context)
    relation = services.clients.branches.compare_commits(
        services.owner, services.repo, info.merge_commit_sha, tip_after
    )
    if relation.status not in {"ahead", "identical"} or tree_after == tree_before:
        raise AssertionError("la base ne contient pas l'amélioration fusionnée")
    _check_controls(services, context, "après R04")
    context["r04"] = {
        "pr_number": item.pr_number,
        "merge_sha": info.merge_commit_sha,
        "tip": tip_after,
        "tree": tree_after,
    }
    step.evidence.update(
        pr_number=item.pr_number, head_sha=item.head_sha, merge_sha=info.merge_commit_sha, dimension=item.dimension,
        delta=round(float(item.delta), 6), score_before=improvement.initial_score, score_after=improvement.final_score,
        proof_id=proof.proof_id, contracts_replayed=len(delivered), files=files, tip_after=tip_after, tree_after=tree_after,
    )  # fmt: skip


def run_incident_phase(report: CampaignReport, context: Dict[str, Any], services: PhaseServices) -> None:
    """R05 — incident contrôlé (injection DÉTERMINISTE signalée) puis rollback Phase 5 prouvé, acquittement CAS et reprise."""
    step = report.step("R05-incident-rollback")
    if "r04" not in context:
        raise IncompleteValidation("R04 non livrée : l'incident n'est pas exercé (rollback non exercé)")
    agent = services.incident_agent or DeterministicIncidentAgent()
    step.evidence["injection"] = {"deterministic": True, "agent": type(agent).__name__, "model_calls": 0, "files": list(INCIDENT_DOCS),
                                  "note": getattr(agent, "announced", "")}  # fmt: skip
    manager, project_id = services.manager(), context["project_id"]
    tip0, tree0 = _tip_and_tree(services, context)
    pending = manager.get_phase5_incident(project_id)
    resumed = pending is not None and pending.state != "recovered"
    if (
        resumed
    ):  # reprise après crash : l'incident durable est réconcilié AVANT toute nouvelle passe (aucune seconde injection)
        outcome = _drive(services, services.resume_incident(context), "reprise R05")
        step.evidence["resumed_before_pass"] = {
            "found": outcome.found,
            "stop_reason": outcome.stop_reason,
            "reason": outcome.reason,
        }
        improvement, promoted = None, []
    else:
        result = _drive(services, services.run_pass(context, improve=True, agent=agent), "R05")
        improvement = getattr(result, "improvement", None)
        step.evidence.update(stop_reason=getattr(result, "stop_reason", None), tip_before=tip0, tree_before=tree0)
        if improvement is None:
            raise IncompleteValidation(
                f"passe d'incident non lancée (arrêt du produit : {getattr(result, 'stop_reason', None)})"
            )
        promoted = list(improvement.promoted)
        if getattr(agent, "calls", 1) and not getattr(agent, "touched", True):
            raise IncompleteValidation(
                "l'injection n'a eu aucun effet (mention légale absente de l'en-tête) : rollback non exercé"
            )
        if not promoted:
            raise IncompleteValidation(
                f"une garde a refusé la contribution d'incident ({improvement.stop_reason}) : rollback NON exercé — {_refusals(improvement)}"
            )
        if improvement.stop_reason != "auto_revert_recovered":
            hard = improvement.stop_reason in {"post_merge_guard_failed", "auto_revert_pending", "auto_revert_base_moved",
                                              "auto_revert_publish_failed", "auto_revert_merge_failed", "auto_revert_health_failed"}  # fmt: skip
            message = f"le rollback n'a pas abouti ({improvement.stop_reason}) : {_refusals(improvement)}"
            raise AssertionError(message) if hard else IncompleteValidation(message + " — rollback non exercé")
    incident = manager.get_phase5_incident(project_id)
    if incident is None or incident.state != "recovered":
        raise AssertionError(f"incident durable non récupéré (état {getattr(incident, 'state', None)})")
    if incident.health_command != business.health_command():
        raise AssertionError("la santé de Phase 5 n'est pas la sonde métier indépendante")
    merge_sha = str(incident.merge_sha)
    tip1, tree1 = _tip_and_tree(services, context)
    branches, owner, repo = services.clients.branches, services.owner, services.repo
    merge_commit = branches.get_git_commit(owner, repo, merge_sha)
    tip_commit = branches.get_git_commit(owner, repo, tip1)
    if merge_commit.tree_sha == tree0:
        raise AssertionError("la fusion de l'incident n'a rien changé (aucune régression observable)")
    if tip1 == tip0 or tip1 == merge_sha or tree1 != tree0 or list(tip_commit.parents) != [merge_sha]:
        raise AssertionError("arbre non restauré par un commit de revert distinct de la fusion de l'incident")
    from collegue.executor.revert import REVERT_BRANCH_PREFIX

    revert_branch = f"{REVERT_BRANCH_PREFIX}{merge_sha[:12]}"
    revert_pr = services.clients.prs.find_pr_by_head(
        owner, repo, revert_branch, base=services.base_branch(context), state="all"
    )
    if revert_pr is None or not revert_pr.merged or not revert_pr.head_sha:
        raise AssertionError("aucune PR de revert fusionnée (le rollback doit passer par une vraie PR)")
    checks = services.clients.prs.get_commit_check_details(owner, repo, revert_pr.head_sha)
    states = {c.name: (c.state, getattr(c, "app_id", None)) for c in checks.checks}
    wanted = (services.manifest or {}).get("check_app_id")
    ok = checks.complete and states.get(business_check_name()) and states[business_check_name()][0] == "success"
    if not ok or (wanted is not None and states[business_check_name()][1] != wanted):
        raise AssertionError(f"la PR de revert n'a pas de check requis réel vert ({states})")
    observation = services.verify_tip(context, tip1)
    if observation.status != "passed" or not observation.checks.get("write:legal_notice_present"):
        raise AssertionError(
            f"santé indépendante non rétablie sur la base restaurée ({observation.status}: {observation.failed})"
        )
    _check_controls(services, context, "après R05")
    stale = manager.acknowledge_phase5_incident(project_id, expected_revision=incident.revision + 1000)
    acknowledged = manager.acknowledge_phase5_incident(project_id, expected_revision=incident.revision)
    again = manager.acknowledge_phase5_incident(project_id, expected_revision=incident.revision)
    if stale or not acknowledged or again or manager.get_phase5_incident(project_id) is not None:
        raise AssertionError(f"acquittement CAS incorrect (périmé={stale}, réel={acknowledged}, rejeu={again})")
    manager.record_decision(project_id, "Incident Phase 5 de la campagne inspecté et acquitté (CAS).")
    recovery = _drive(services, services.resume_incident(context), "reprise après acquittement")
    if recovery.found or not recovery.continue_loop:
        raise AssertionError("la reprise n'est pas libre après l'acquittement")
    context["r05"] = {"incident_pr": incident.source_pr_number, "merge_sha": merge_sha, "revert_pr": revert_pr.number}
    step.evidence.update(
        incident_state="recovered", incident_pr=incident.source_pr_number, merge_sha=merge_sha, revert_pr=revert_pr.number,
        revert_pr_merge_sha=revert_pr.merge_commit_sha, revert_checks=states, tip_before=tip0, tip_after=tip1, tree_before=tree0,
        tree_after=tree1, tree_at_incident=merge_commit.tree_sha, health_command_is_independent_probe=True,
        health_after=observation.status, acknowledged_revision=incident.revision, cas_stale_rejected=not stale,
        replay_rejected=not again, recovery_found=recovery.found, recovery_continue=recovery.continue_loop, resumed=resumed,
    )  # fmt: skip


def business_check_name() -> str:
    return REQUIRED_CHECK


# ── contrôles de préflight W5 (branchés par ``run_preflight(extra_checks=…)``) ───────────────────────────────────────────


def w5_preflight_checks(
    env: Mapping[str, str],
    *,
    settings: Any,
    clients: Any,
    campaign_id: str,
    run_tag: str,
    broker_proof: Optional[Callable[[Any], Mapping[str, Any]]] = None,
) -> List[Tuple[str, str, Callable[[Step], None]]]:
    def models(step: Step) -> None:
        if settings is None:
            raise IncompleteValidation("configuration effective illisible : modèles non vérifiables")
        check_gemma_models(settings, step)

    def broker(step: Step) -> None:
        if settings is None:
            raise IncompleteValidation("configuration effective illisible : relais non vérifiable")
        check_broker_selection(settings, step, proof=broker_proof)

    def bootstrap(step: Step) -> None:
        manifest = load_bootstrap_manifest(str(env.get(BOOTSTRAP_MANIFEST_ENV, "") or ""))
        validate_bootstrap_manifest(manifest, clients, step, run_tag=run_tag)

    def identity(step: Step) -> None:
        check_campaign_identity_unused(clients, campaign_id, step)

    return [
        (
            "P09-gemma-models",
            "Gemma 4 31B pour tous les rôles ; repli du codeur seulement 26B ; destination Google",
            models,
        ),
        (
            "P10-budget-broker",
            "Relais budgétaire sélectionné avec preuve de capacité du transport réellement instancié",
            broker,
        ),
        (
            "P11-bootstrap",
            "Socle de bootstrap prouvé par l'API (graine immuable, ascendance, contenu exact, check requis)",
            bootstrap,
        ),
        ("P12-campaign-identity", "Identifiant de campagne jamais consommé (revendication durable absente)", identity),
    ]


def phase_callables(
    services: PhaseServices,
) -> Tuple[Callable[[CampaignReport, Dict[str, Any]], None], Callable[[CampaignReport, Dict[str, Any]], None]]:
    return (
        lambda report, context: run_improvement_phase(report, context, services),
        lambda report, context: run_incident_phase(report, context, services),
    )


def default_manager_factory(env: Mapping[str, str]) -> Callable[[], Any]:
    def build() -> Any:
        from collegue.state import ProjectStateManager

        return ProjectStateManager.from_url(str(env["STATE_DATABASE_URL"]))

    return build


@dataclass(frozen=True)
class ResumeOutcome:
    """Issue de la reprise Phase 5 (mêmes attributs que ``Phase5ResumeOutcome``)."""

    found: bool
    continue_loop: bool
    stop_reason: Optional[str]
    reason: str


PHASE5_PENDING_STOPS = frozenset(
    {"phase5_incident_pending", "auto_revert_pending", "auto_revert_base_moved", "auto_revert_publish_failed",
     "auto_revert_merge_failed", "auto_revert_health_failed", "post_merge_guard_failed"}
)  # fmt: skip


def production_run_pass(*, owner: str, repo: str) -> Callable[..., Awaitable[Any]]:
    """Entrée PUBLIQUE du produit pour R04/R05 : aucune injection de client, de sandbox, de relecteur ni de mesure — tout vient de
    la configuration effective (relais budgétaire, vrai GitHub). Seul ``agent`` (R05) est fourni, et il est signalé."""

    async def run(context: Mapping[str, Any], *, improve: bool, agent: Optional[Any] = None) -> Any:
        from collegue.pilot import run_project_from_settings

        kwargs: Dict[str, Any] = {"agent": agent} if agent is not None else {}
        return await run_project_from_settings(
            int(context["project_id"]), str(context["operator_checkout"]), owner=owner, repo=repo,
            base=str(context["base_branch"]), dry_run=False, max_iterations=None, improve=improve, **kwargs,
        )  # fmt: skip

    return run


def production_services(
    *,
    env: Mapping[str, str],
    adapter: Any,
    clients: Any,
    manifest: Mapping[str, Any],
    image: str,
    deadline_monotonic: float,
    clock: Optional[Callable[[], float]] = None,
) -> PhaseServices:
    """Câblage RÉEL : entrée publique du produit, vrais clients GitHub, sonde métier en conteneur, état durable de la campagne."""
    import shutil

    owner, _, repo = FIXTURE_REPOSITORY.partition("/")
    manager = default_manager_factory(env)
    run_pass = production_run_pass(owner=owner, repo=repo)

    async def resume(context: Mapping[str, Any]) -> ResumeOutcome:
        # Reprise PUBLIQUE sans génération : ``improve=False`` ne lance rien ; la barrière Phase 5 du produit réconcilie l'incident.
        result = await run_pass(context, improve=False, agent=None)
        incident = manager().get_phase5_incident(int(context["project_id"]))
        stop = str(getattr(result, "stop_reason", "") or "")
        pending = incident is not None or stop in PHASE5_PENDING_STOPS
        return ResumeOutcome(
            found=pending, continue_loop=not pending, stop_reason=stop, reason=f"arrêt du produit : {stop}"
        )

    def verify_tip(context: Mapping[str, Any], sha: str) -> Any:
        checkout = adapter.clone(sha)  # le clone vérifie que HEAD == sha
        try:
            return business.verify_business_checkout(
                checkout, python="python", image=image, deadline_monotonic=deadline_monotonic, clock=clock
            )
        finally:
            shutil.rmtree(os.path.dirname(checkout), ignore_errors=True)

    return PhaseServices(
        run_pass=run_pass, clients=clients, manager=manager, resume_incident=resume, verify_tip=verify_tip, owner=owner,
        repo=repo, deadline_monotonic=deadline_monotonic, clock=clock, manifest=manifest,
    )  # fmt: skip


__all__ = [
    "DeterministicIncidentAgent",
    "PhaseServices",
    "check_gemma_models",
    "check_broker_selection",
    "claim_campaign_identity",
    "revalidate_and_claim",
    "check_campaign_identity_unused",
    "validate_bootstrap_manifest",
    "verify_fixture_controls_intact",
    "run_improvement_phase",
    "run_incident_phase",
    "w5_preflight_checks",
    "phase_callables",
    "production_run_pass",
    "production_services",
    "ResumeOutcome",
]
