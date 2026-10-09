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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from collegue.pilot import w4_business as business
from collegue.pilot import w5_business_ownership as ownership
from collegue.pilot import w5_business_policy as fixture_policy
from collegue.pilot import w5_business_spec as spec_publication
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
#: Ciblage des passes par la liste blanche de l'auto-merge de Phase 5 (réglage PRODUIT ``AUTO_MERGE_PATH_ALLOWLIST``, ici RESSERRÉ par rapport
#: au défaut ``docs/**,**/*.md,**/*.rst`` — jamais élargi). R04 ne peut fusionner QUE le runbook cible ; R05 que les documents de l'incident.
#: Un modèle qui sort de sa cible voit sa PR refusée par la vraie politique (non fusionnée, support d'incident préservé).
R04_ALLOWLIST = (R04_DOC,)
INCIDENT_ALLOWLIST = INCIDENT_DOCS
#: Identifiants d'exemple FACTICES (AWS documentation) : motif d'une clé d'accès et d'un secret de 40 caractères.
FAKE_CREDENTIAL_LINE = re.compile(
    r"AKIA[0-9A-Z]{16}|(?:secret[_ ]?access[_ ]?key|SECRET_ACCESS_KEY)\s*[=:]\s*\S{40}", re.I
)

#: Seul fichier de la graine que le socle peut MODIFIER (décision manager : aligner les dépendances sur celles, auditées, de l'image).
ALLOWED_SEED_MODIFICATIONS = ("requirements.txt",)
#: Ajouts autorisés au socle : le contrôle (workflow), des documents d'exemple et des listes de dépendances racine. Rien d'autre :
#: aucune implémentation métier (``app/``, ``tests/``, code Python…), aucun fichier inconnu.
_ADDED_ALLOWED = (
    re.compile(r"\.github/workflows/[A-Za-z0-9._-]+\.ya?ml"),
    re.compile(r"\.github/CODEOWNERS"),
    re.compile(r"ci/requirements-approved\.lock"),
    re.compile(r"docs/[A-Za-z0-9._-]+\.md"),
    re.compile(r"requirements[A-Za-z0-9._-]*\.(txt|in)"),
)
CODEOWNERS_PATH = ".github/CODEOWNERS"
APPROVED_LOCK_PATH = "ci/requirements-approved.lock"
WORKFLOW_PATH = fixture_policy.CAMPAIGN_WORKFLOW_PATH
#: Contrôles OBLIGATOIRES du socle : le workflow (déclenché par ``pull_request``, jamais ``pull_request_target`` qui s'exécute depuis la
#: branche par défaut, sans workflow), le signal de revue des chemins protégés (CODEOWNERS, information seulement) et le verrou haché de la pile approuvée.
REQUIRED_ADDED = (WORKFLOW_PATH, CODEOWNERS_PATH, APPROVED_LOCK_PATH)
PROTECTED_PREFIXES = [".github/", "ci/"]
CHECK_WORKFLOW_KEYS = frozenset({"workflow", "triggers", "job", "candidate_execution", "dependency_source"})
CODE_OWNER = re.compile(r"@[A-Za-z0-9][A-Za-z0-9-]*(/[A-Za-z0-9._-]+)?")
_PIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*(\[[A-Za-z0-9_,.\- ]+\])?==[A-Za-z0-9_.!+\-]+")
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


def _manifest_shape(manifest: Mapping[str, Any]) -> Tuple[str, Dict[str, str], List[str]]:
    """Forme FERMÉE du manifeste : ``(bootstrap_sha, approved_files, modified_seed_files)``.

    ``approved_files`` (chemin → sha256) couvre les ajouts ET la version approuvée des fichiers de graine modifiés, listés dans
    ``modified_seed_files`` (seul ``requirements.txt`` est admis). Aucune implémentation métier n'est admissible."""
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
    modified = manifest.get("modified_seed_files", [])
    if (
        not isinstance(modified, list)
        or any(m not in ALLOWED_SEED_MODIFICATIONS for m in modified)
        or len(set(modified)) != len(modified)
    ):
        raise RuntimeError(
            f"manifeste de bootstrap : modified_seed_files doit être inclus dans {list(ALLOWED_SEED_MODIFICATIONS)} (vu {modified!r})"
        )
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
        if path in modified:
            continue
        if path in FIXTURE_SEED_FILES:
            raise RuntimeError(
                f"manifeste de bootstrap : {path!r} appartient à la graine immuable (non déclaré modifié)"
            )
        if not any(rule.fullmatch(path) for rule in _ADDED_ALLOWED):
            raise RuntimeError(
                f"manifeste de bootstrap : {path!r} n'est ni un contrôle, ni un document, ni une liste de dépendances"
            )
    missing = [path for path in modified if path not in approved]
    if missing:
        raise RuntimeError(f"manifeste de bootstrap : version approuvée absente pour {missing}")
    added = manifest.get("added_files")
    if added is not None and sorted(added) != sorted(set(approved) - set(modified)):
        raise RuntimeError("manifeste de bootstrap : added_files ≠ approved_files − modified_seed_files")
    _manifest_contract(manifest, approved, modified)
    return bootstrap_sha, dict(approved), list(modified)


def _manifest_contract(manifest: Mapping[str, Any], approved: Mapping[str, str], modified: Sequence[str]) -> None:
    """Forme fermée des déclarations de contrôle (``check_workflow``, ``protected_prefixes``, ``code_owner``, hachages des fichiers de
    graine modifiés). ``check_producer`` n'existe plus : ``pull_request_target`` s'exécute depuis la branche par défaut (la graine,
    immuable et sans workflow) et ne se déclencherait donc jamais sur une base éphémère."""
    if "check_producer" in manifest:
        raise RuntimeError(
            "manifeste de bootstrap : check_producer est obsolète (pull_request_target ne se déclenche pas depuis la graine) ; "
            "le contrôle est décrit par check_workflow"
        )
    if manifest.get("protected_prefixes") != PROTECTED_PREFIXES:
        raise RuntimeError(f"manifeste de bootstrap : protected_prefixes doit valoir {PROTECTED_PREFIXES} ")
    owner = manifest.get("code_owner")
    if not isinstance(owner, str) or not CODE_OWNER.fullmatch(owner):
        raise RuntimeError("manifeste de bootstrap : code_owner (@propriétaire) requis")
    missing = [path for path in REQUIRED_ADDED if path not in approved or path in modified]
    if missing:
        raise RuntimeError(f"manifeste de bootstrap : contrôle(s) approuvé(s) absent(s) des ajouts : {missing}")
    hashes = manifest.get("modified_seed_hashes")
    if not isinstance(hashes, dict) or sorted(hashes) != sorted(modified):
        raise RuntimeError("manifeste de bootstrap : modified_seed_hashes doit décrire exactement modified_seed_files")
    for path, pair in hashes.items():
        if (
            not isinstance(pair, dict)
            or set(pair) != {"seed_sha256", "approved_sha256"}
            or not all(isinstance(v, str) and _SHA256.fullmatch(v) for v in pair.values())
            or pair["approved_sha256"] != approved[path]
            or pair["seed_sha256"] == pair["approved_sha256"]
        ):
            raise RuntimeError(
                f"manifeste de bootstrap : modified_seed_hashes[{path!r}] incohérent avec approved_files"
            )
    declared = manifest.get("check_workflow")
    if not isinstance(declared, dict) or set(declared) != CHECK_WORKFLOW_KEYS:
        raise RuntimeError(
            f"manifeste de bootstrap : check_workflow : clés exactes attendues {sorted(CHECK_WORKFLOW_KEYS)}"
        )
    expectations = {
        "workflow": WORKFLOW_PATH,
        "job": REQUIRED_CHECK,
        "dependency_source": APPROVED_LOCK_PATH,
    }
    for key, value in expectations.items():
        if declared.get(key) != value:
            raise RuntimeError(
                f"manifeste de bootstrap : check_workflow.{key} doit valoir {value!r} (vu {declared.get(key)!r})"
            )
    if sorted(declared.get("triggers") or []) != ["pull_request", "push"]:
        raise RuntimeError("manifeste de bootstrap : check_workflow.triggers doit valoir ['pull_request', 'push']")
    if not isinstance(declared.get("candidate_execution"), str) or not declared["candidate_execution"].strip():
        raise RuntimeError("manifeste de bootstrap : check_workflow.candidate_execution requis")


def requirements_violations(text: str) -> List[str]:
    """Une liste de dépendances du socle ne contient que des versions ÉPINGLÉES (``nom==version``, empreintes admises) : aucune
    source externe (URL, VCS, chemin), aucune option d'index, aucune inclusion."""
    problems: List[str] = []
    logical = text.replace("\\\n", " ").splitlines()
    for line in logical:
        content = line.split("#", 1)[0].strip()
        if not content:
            continue
        head, *rest = content.split()
        if not _PIN.fullmatch(head.split(";")[0]):
            problems.append(f"dépendance non épinglée ou source externe : {head!r}")
            continue
        for token in rest:
            if not (re.fullmatch(r"--hash=sha256:[0-9a-f]{64}", token) or token.startswith(";") or token.endswith(";")):
                if "://" in token or token.startswith(("-", "@", "git+", "file:")):
                    problems.append(f"option ou source non admise : {token!r}")
    return problems


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
    bootstrap_sha, approved, modified = _manifest_shape(manifest)
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
    if changed_seed != sorted(modified):
        raise RuntimeError(
            f"le socle modifie des fichiers de la graine hors de ce que le manifeste déclare : {changed_seed} ≠ {sorted(modified)}"
        )
    added = sorted(set(approved) - set(modified))
    extra = sorted(set(boot_tree) - set(seed_tree))
    if extra != added:
        raise RuntimeError(
            f"contenu du socle ≠ fichiers approuvés (en trop : {sorted(set(extra) - set(added))} ; "
            f"manquants : {sorted(set(added) - set(extra))})"
        )
    seed_hashes: Dict[str, str] = {}
    texts: Dict[str, str] = {}
    for path, expected in sorted(approved.items()):
        text = _file_text(clients, owner, repo, path, bootstrap_sha)
        if text is None:
            raise RuntimeError(f"fichier approuvé introuvable dans le socle : {path}")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != expected:
            raise RuntimeError(f"octets du socle ≠ sha256 approuvé : {path}")
        previous = _file_text(clients, owner, repo, path, FIXTURE_SEED_SHA)
        texts[path] = text
        if path in modified:
            if previous is None or hashlib.sha256(previous.encode("utf-8")).hexdigest() == expected:
                raise RuntimeError(f"fichier de graine déclaré modifié mais identique à la graine : {path}")
            seed_hashes[path] = hashlib.sha256(previous.encode("utf-8")).hexdigest()
            if seed_hashes[path] != manifest["modified_seed_hashes"][path]["seed_sha256"]:
                raise RuntimeError(
                    f"modified_seed_hashes[{path!r}] : le sha256 de la graine déclaré ne correspond pas à la graine"
                )
            if path == "requirements.txt" and requirements_violations(text):
                raise RuntimeError("requirements.txt du socle : " + " ; ".join(requirements_violations(text)))
        elif previous is not None:
            raise RuntimeError(f"fichier approuvé déjà présent dans la graine : {path}")
    owner_text = str(manifest["code_owner"])
    _validate_code_owners(texts[CODEOWNERS_PATH], owner_text)
    lock_problems = lock_violations(texts[APPROVED_LOCK_PATH])
    if lock_problems:
        raise RuntimeError("verrou approuvé : " + " ; ".join(lock_problems))
    uncovered = requirements_outside_lock(texts.get("requirements.txt", ""), texts[APPROVED_LOCK_PATH])
    if uncovered:
        raise RuntimeError(
            "requirements.txt du socle demande des dépendances hors du verrou approuvé : " + ", ".join(uncovered)
        )
    produced = _validate_check_workflow(manifest["check_workflow"], texts[WORKFLOW_PATH])
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
    ruleset_facts = _validate_ruleset_rules(clients, owner, repo, probe, manifest)
    _validate_actions_readable(clients, owner, repo)
    evidence = {
        "bootstrap_sha": bootstrap_sha,
        "bootstrap_tree": commit.tree_sha,
        "main_tip": main_tip,
        "approved_files": sorted(approved),
        "modified_seed_files": sorted(modified),
        "seed_sha256_of_modified": seed_hashes,
        "approved_sha256_of_modified": {path: approved[path] for path in modified},
        "check_workflow": manifest["check_workflow"],
        "code_owner": owner_text,
        **ruleset_facts,
        "protected_prefixes": list(PROTECTED_PREFIXES),
        "approved_stack": approved_stack(texts.get("requirements.txt", "")),
        "workflow_jobs": sorted(produced),
        "ruleset_id": ruleset.id,
        "required_check": REQUIRED_CHECK,
        "check_app_id": manifest["check_app_id"],
        "strict_sources": list(policy.strict_sources),
    }
    step.evidence.update(evidence)
    return evidence


def _validate_code_owners(text: str, code_owner: str) -> None:
    """CODEOWNERS attribue ``.github/`` ET ``ci/`` au propriétaire déclaré. C'est un SIGNAL de revue (information), pas une barrière :
    la protection des contrôles est la garde de publication et de fusion du produit."""
    rules: Dict[str, List[str]] = {}
    for line in text.splitlines():
        content = line.split("#", 1)[0].strip()
        if content:
            pattern, *owners = content.split()
            rules[pattern] = owners
    for prefix in ("/.github/", "/ci/"):
        if rules.get(prefix) != [code_owner]:
            raise RuntimeError(f"CODEOWNERS : {prefix} doit appartenir à {code_owner} (vu {rules.get(prefix)!r})")
    if any(owners != [code_owner] for owners in rules.values()):
        raise RuntimeError("CODEOWNERS : un chemin est attribué à un autre propriétaire que le propriétaire déclaré")


_LOCK_LINE = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.\-]*)(\[[A-Za-z0-9_,.\- ]+\])?==([A-Za-z0-9_.!+\-]+)")


def _normal(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def lock_violations(text: str) -> List[str]:
    """Le verrou approuvé ne contient que des versions épinglées ET hachées (``--hash=sha256:``), sans source externe."""
    problems = requirements_violations(text)
    entries = [
        entry.split("#", 1)[0].strip()
        for entry in text.replace("\\\n", " ").splitlines()
        if entry.split("#", 1)[0].strip()
    ]
    if not entries:
        problems.append("verrou vide")
    for entry in entries:
        if "--hash=sha256:" not in entry:
            problems.append(f"entrée non hachée : {entry.split()[0]!r}")
    return problems


def approved_stack(requirements: str) -> List[str]:
    """``nom==version`` épinglés du ``requirements.txt`` approuvé (annoncés au codeur, qui travaille HORS LIGNE)."""
    stack: List[str] = []
    for entry in requirements.splitlines():
        head = entry.split("#", 1)[0].strip().split(" ")[0].split(";")[0]
        if head and _LOCK_LINE.fullmatch(head):
            stack.append(head)
    return stack


def requirements_outside_lock(requirements: str, lock: str) -> List[str]:
    """Dépendances de ``requirements.txt`` (épinglées) absentes du verrou approuvé, ou à une autre version."""
    locked: Dict[str, str] = {}
    for entry in lock.replace("\\\n", " ").splitlines():
        head = entry.split("#", 1)[0].strip().split(" ")[0].split(";")[0]
        found = _LOCK_LINE.fullmatch(head) if head else None
        if found:
            locked[_normal(found.group(1))] = found.group(3)
    outside: List[str] = []
    for entry in requirements.splitlines():
        head = entry.split("#", 1)[0].strip().split(" ")[0].split(";")[0]
        found = _LOCK_LINE.fullmatch(head) if head else None
        if found and locked.get(_normal(found.group(1))) != found.group(3):
            outside.append(head)
    return outside


def _validate_check_workflow(declared: Mapping[str, Any], text: str) -> List[str]:
    """Le workflow approuvé qui produit ``Fixture tests`` est celui décrit, lu sur l'arbre réel (octets approuvés par ailleurs).

    Exigences : déclencheurs EXACTEMENT ``pull_request`` (sur les bases de campagne) et ``push`` — jamais ``pull_request_target`` (il
    s'exécute depuis la branche par défaut, ici sans workflow) ni ``workflow_run`` ; aucun secret ; permissions de lecture seule ;
    un job ``Fixture tests`` sans condition ni ``continue-on-error`` ; extractions sans identifiants conservés. Cette lecture ne
    suffit PAS à faire confiance au workflow d'une PR : la garde de fusion compare les arbres réels et la provenance du job."""
    import yaml

    document = yaml.safe_load(text) or {}
    triggers = document.get("on", document.get(True))
    if not isinstance(triggers, dict) or set(triggers) != {"pull_request", "push"}:
        raise RuntimeError("check_workflow : déclencheurs exactement pull_request et push (jamais pull_request_target)")
    branches = (triggers.get("pull_request") or {}).get("branches") or []
    if not any(str(item).startswith(fixture_policy.CAMPAIGN_BASE_PREFIX) for item in branches):
        raise RuntimeError("check_workflow : pull_request doit cibler les bases de campagne (collegue-business/…)")
    if "secrets." in text:
        raise RuntimeError("check_workflow : le workflow référence un secret")
    if document.get("permissions") != {"contents": "read"}:
        raise RuntimeError(f"check_workflow : permissions non minimales ({document.get('permissions')!r})")
    jobs = [
        job
        for job in (document.get("jobs") or {}).values()
        if isinstance(job, dict) and job.get("name") == REQUIRED_CHECK
    ]
    if len(jobs) != 1:
        raise RuntimeError(f"check_workflow : exactement un job nommé {REQUIRED_CHECK!r} (vu {len(jobs)})")
    job = jobs[0]
    if any(
        key in job for key in ("if", "continue-on-error", "permissions")
    ):  # `if: false` est un booléen FAUX : tester la clé
        raise RuntimeError(
            "check_workflow : le job porte une condition, un continue-on-error ou des permissions propres"
        )
    for step in job.get("steps") or []:
        if "continue-on-error" in step or "if" in step:
            raise RuntimeError("check_workflow : une étape porte une condition ou continue-on-error")
        if (
            str(step.get("uses", "")).startswith("actions/checkout")
            and (step.get("with") or {}).get("persist-credentials") is not False
        ):
            raise RuntimeError("check_workflow : une extraction conserve ses identifiants")
    return [REQUIRED_CHECK]


def _validate_ruleset_rules(
    clients: Any, owner: str, repo: str, branch: str, manifest: Mapping[str, Any]
) -> Dict[str, Any]:
    """Règles du ruleset applicable à une base de campagne.

    * BLOQUANT : le check requis n'est pas exempté à la création (``do_not_enforce_on_create`` faux) — observé : une base créée depuis la
      graine, sans check, est refusée par le serveur ;
    * INFORMATIF seulement : ``require_code_owner_review``. Observé en C47 : avec 0 approbation requise, GitHub a FUSIONNÉ des PR qui
      modifiaient le workflow, CODEOWNERS et le verrou. Le drapeau n'est donc PAS une protection et n'est jamais compté comme telle ; la
      protection est la garde du produit (publication et fusion). Sa valeur est consignée, sans conclusion de sécurité."""
    getter = getattr(clients.branches, "get_branch_rules", None)
    if not callable(getter):
        raise IncompleteValidation("lecture des règles de branche impossible : règles du ruleset non établies")
    try:
        rules = list(getter(owner, repo, branch))
    except Exception as exc:  # noqa: BLE001
        raise IncompleteValidation(f"règles de branche illisibles ({type(exc).__name__})") from exc
    own = [r for r in rules if getattr(r, "ruleset_id", None) == manifest["ruleset_id"]]
    statuses = [r for r in own if r.type == "required_status_checks"]
    if not statuses or any((r.parameters or {}).get("do_not_enforce_on_create") is not False for r in statuses):
        raise RuntimeError("ruleset : le check requis est exempté à la création (do_not_enforce_on_create)")
    reviews = [r for r in own if r.type == "pull_request"]
    return {
        "code_owner_review_flag": bool(reviews)
        and all((r.parameters or {}).get("require_code_owner_review") is True for r in reviews),
        "code_owner_review_is_a_protection": False,
    }


def _validate_actions_readable(clients: Any, owner: str, repo: str) -> None:
    """Le jeton lit les jobs et exécutions Actions : sans cela la provenance du check requis ne peut être établie et toute fusion de
    la campagne serait refusée (fail-closed) APRÈS les dépenses de planification. Lecture seule, avant tout lancement."""
    reader = getattr(getattr(clients, "prs", None), "list_workflow_runs", None)
    if not callable(reader):
        raise IncompleteValidation("lecture des exécutions Actions non disponible : provenance du check non vérifiable")
    try:
        reader(owner, repo, limit=1)
    except Exception as exc:  # noqa: BLE001
        raise IncompleteValidation(
            f"le jeton ne lit pas les exécutions Actions ({type(exc).__name__}) : la provenance du check requis ne pourra pas "
            "être établie (permission « Actions : lecture » requise)"
        ) from exc


def verify_fixture_controls_intact(
    clients: Any, manifest: Mapping[str, Any], ref: str, *, label: str
) -> Dict[str, str]:
    """Les contrôles de la fixture (``.github/`` et ``ci/``) sont INCHANGÉS sur ``ref`` par rapport au commit de socle : un BUILD
    ou une amélioration ne peut pas réécrire le check qui le juge. Comparaison des objets Git réels (arbres), jamais des fichiers
    d'une liste. Échec ⇒ ``RuntimeError`` ; lecture impossible ⇒ validation incomplète (jamais une réussite)."""
    owner, _, repo = FIXTURE_REPOSITORY.partition("/")
    bootstrap_sha, _approved, _modified = _manifest_shape(manifest)
    branches = clients.branches
    try:
        tip = ref if _SHA.fullmatch(ref or "") else str(branches.get_branch_sha(owner, repo, ref)).lower()
        trusted_tree = branches.get_git_commit(owner, repo, bootstrap_sha).tree_sha
        head_tree = branches.get_git_commit(owner, repo, tip).tree_sha
    except Exception as exc:  # noqa: BLE001
        raise IncompleteValidation(f"arbres de contrôle illisibles ({label}) : {type(exc).__name__}") from exc
    try:
        fixture_policy.assert_controls_untouched(branches, owner, repo, base_tree=trusted_tree, head_tree=head_tree)
    except fixture_policy.PolicyRefusal as refused:
        if refused.kind == "unavailable":
            raise IncompleteValidation(f"contrôles de la fixture illisibles ({label}) : {refused.reason}") from refused
        raise RuntimeError(f"contrôle de la fixture altéré ({label}) : {refused.reason}") from refused
    return fixture_policy.protected_objects(branches, owner, repo, head_tree)


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


# ── activation : scope durable de la campagne puis qualification des deux modèles ───────────────────────────────────────────


QUALIFICATION_CAPABILITIES = ("text", "json", "tools")
#: Rôle de chaque identité officielle dans la qualification d'A : le 31B sert tous les rôles par défaut, le 26B le codeur seul.
QUALIFICATION_ROLES = {MODEL_PRIMARY: "default", MODEL_CODER_FALLBACK: "coder"}
GOOGLE_DESTINATION = OFFICIAL_GOOGLE_HOST + "/v1beta"
#: Marge de dérive d'horloge entre le registre (échéance absolue durable) et ce processus.
DEADLINE_SKEW_SECONDS = 5.0


def broker_qualifier() -> Callable[..., Any]:
    """API publique de qualification des deux modèles du lot A : ``await collegue.broker.qualify_models(settings, ledger, scope_key)``
    (équivalent de ``BrokerService.qualify_models(scope_key)``), qui rend un ``QualificationReport``.

    Absente ⇒ validation incomplète explicite : aucun canari réel, aucune estimation de remplacement, aucun lancement."""
    try:
        from collegue.broker import qualify_models  # type: ignore[attr-defined]
    except ImportError as exc:
        raise IncompleteValidation(
            "API publique de qualification des deux modèles (collegue.broker.qualify_models, lot A) absente : aucun canari "
            "réel, aucune estimation de remplacement, aucun lancement"
        ) from exc
    return qualify_models


def _qualification_facts(report: Any) -> Dict[str, Any]:
    """Détails de CHAQUE capacité de CHAQUE modèle (réussis ou refusés), sans secret, pour le rapport de campagne."""
    to_dict = getattr(report, "to_dict", None)
    if callable(to_dict):
        try:
            return dict(to_dict())
        except Exception:  # noqa: BLE001 - on retombe sur la lecture directe des champs
            pass
    return {
        "scope_key": getattr(report, "scope_key", None),
        "ok": getattr(report, "ok", None),
        "reason": getattr(report, "reason", None),
        "models": [
            {
                "model": getattr(m, "model", None),
                "role": getattr(m, "role", None),
                "ok": getattr(m, "ok", None),
                "capabilities": [
                    {
                        "capability": getattr(c, "capability", None),
                        "ok": getattr(c, "ok", None),
                        "detail": getattr(c, "detail", None),
                        "request_id": getattr(c, "request_id", None),
                        "tokens": getattr(c, "tokens", None),
                    }
                    for c in getattr(m, "capabilities", ()) or ()
                ],
            }
            for m in getattr(report, "models", ()) or ()
        ],
    }


def _qualification_problems(report: Any, scope_key: str, *, now: datetime) -> Tuple[List[str], Optional[float]]:
    """Contrôle EXPLICITE du contrat final d'A (``QualificationReport``) : jamais « un mapping accepté » qui n'établit rien.

    Retourne ``(problèmes, secondes restantes de l'échéance absolue durable)``."""
    problems: List[str] = []
    if isinstance(report, Mapping) or not all(
        hasattr(report, name)
        for name in ("ok", "models", "deadline_at", "scope_key", "consumed_tokens", "blocked", "destination")
    ):
        return [f"rapport de qualification inattendu ({type(report).__name__} : QualificationReport d'A requis)"], None
    if report.scope_key != scope_key:
        problems.append(f"scope qualifié {report.scope_key!r} ≠ {scope_key!r}")
    if report.ok is not True:
        problems.append(f"qualification non réussie : {getattr(report, 'reason', '') or 'raison absente'}")
    if report.blocked:
        problems.append("scope bloqué par le registre (usage inconnu)")
    if (
        not isinstance(report.consumed_tokens, int)
        or isinstance(report.consumed_tokens, bool)
        or report.consumed_tokens <= 0
    ):
        problems.append("aucune consommation établie par le registre : les canaris n'ont pas traversé le pipeline réel")
    if GOOGLE_DESTINATION not in str(report.destination) and OFFICIAL_GOOGLE_HOST not in str(report.destination):
        problems.append(f"destination non native Google ({report.destination!r})")
    by_model = {m.model: m for m in report.models}
    if sorted(by_model) != sorted(QUALIFICATION_ROLES) or len(report.models) != len(QUALIFICATION_ROLES):
        problems.append(f"identités qualifiées {sorted(by_model)} ≠ {sorted(QUALIFICATION_ROLES)}")
    for model, role in QUALIFICATION_ROLES.items():
        qualified = by_model.get(model)
        if qualified is None:
            continue
        if qualified.role != role:
            problems.append(f"{model} qualifié pour le rôle {qualified.role!r} (attendu {role!r})")
        if qualified.ok is not True:
            problems.append(f"{model} non qualifié")
        capabilities = {c.capability: c for c in qualified.capabilities}
        if sorted(capabilities) != sorted(QUALIFICATION_CAPABILITIES) or len(qualified.capabilities) != len(
            QUALIFICATION_CAPABILITIES
        ):
            problems.append(f"{model} : capacités {sorted(capabilities)} ≠ {sorted(QUALIFICATION_CAPABILITIES)}")
        for name, capability in capabilities.items():
            if capability.ok is not True:
                problems.append(f"{model}/{name} refusé : {capability.detail}")
            if capability.request_id != f"qualify:{scope_key}:{model}:{name}":
                problems.append(f"{model}/{name} : identité durable inattendue {capability.request_id!r}")
    remaining: Optional[float] = None
    deadline = report.deadline_at
    if not isinstance(deadline, datetime) or deadline.tzinfo is None:
        problems.append("échéance absolue durable absente ou sans fuseau : fenêtre de 900 s non établie")
    else:
        remaining = (deadline - now).total_seconds()
        ceiling = float(business.CAMPAIGN_BOUNDS.max_seconds) + DEADLINE_SKEW_SECONDS
        if remaining <= 0:
            problems.append("échéance globale déjà dépassée à la fin de la qualification")
        elif remaining > ceiling:
            problems.append(
                f"échéance durable à {remaining:.0f} s, au-delà de la fenêtre de la campagne ({ceiling:.0f} s)"
            )
    return problems, remaining


def activate_budget(
    env: Mapping[str, str],
    campaign_id: str,
    report: CampaignReport,
    *,
    qualify: Optional[Callable[..., Any]] = None,
    manager_factory: Optional[Callable[[], Any]] = None,
    on_remaining: Optional[Callable[[float], None]] = None,
    now: Optional[Callable[[], datetime]] = None,
) -> None:
    """Ouvre le scope DURABLE ``planning:cycle:<campagne>`` (2 USD / 250 000 tokens, strict) puis qualifie les deux Gemma sur CE scope
    — avant toute création distante et toute planification. Le brouillon public reprend ensuite ce même cycle (``--cycle-id``) :
    même ligne, même solde, même horloge. Un refus ou une ambiguïté arrête la campagne (aucun repli estimé, aucun nouvel essai).

    Contrat d'A consommé explicitement (``await qualify_models(settings, ledger, scope_key) -> QualificationReport``) : les DEUX
    identités, leurs trois capacités (texte, JSON, outils), la destination native, la consommation, l'absence de blocage et
    l'ÉCHÉANCE ABSOLUE durable sont contrôlées ; les détails de chaque capacité sont conservés dans le rapport, y compris en refus.
    La spec d'un canari relancé ne réémet pas (identités durables d'A) : un nouvel essai exige un nouvel identifiant de campagne."""
    from collegue.state.budget_ledger import BudgetRefused

    bounds = business.CAMPAIGN_BOUNDS
    scope_key = f"planning:cycle:{campaign_id}"
    ledger = (manager_factory or default_manager_factory(env))().budget_ledger
    ledger.create_planning_scope(
        max_cost_usd=bounds.max_cost_usd, max_tokens=bounds.max_tokens, strict=True, scope_key=scope_key
    )
    context = report.facts.setdefault("launch", {})
    context["scope_key"] = scope_key  # lisible dès maintenant, même si aucun projet n'aboutit
    settings = business.effective_settings(env)

    def _capture_registry() -> None:
        """Dépense et blocage lisibles par scope dès les canaris, que la qualification réussisse ou non."""
        try:
            snapshot = ledger.snapshot(scope_key)
            report.facts["qualification_registry"] = {
                "scope": scope_key,
                "consumed_tokens": snapshot.consumed_tokens,
                "reserved_tokens": snapshot.reserved_tokens,
                "unknown_tokens": snapshot.unknown_tokens,
                "blocked_reason": snapshot.blocked_reason,
            }
        except Exception as exc:  # noqa: BLE001 - lecture impossible : consignée, jamais un zéro inventé
            report.facts["qualification_registry"] = {"unreadable": f"{type(exc).__name__} : dépense non établie"}

    try:
        result = (qualify or broker_qualifier())(settings, ledger, scope_key)
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
    except BudgetRefused as refusal:
        _capture_registry()
        raise BudgetStop(f"qualification des modèles refusée par le registre ({refusal.code}) : {refusal}") from refusal
    except IncompleteValidation:
        raise
    except Exception as exc:  # noqa: BLE001 - refus / ambiguïté du transport : aucun repli estimé, aucun nouvel essai
        _capture_registry()
        raise IncompleteValidation(f"qualification des modèles impossible ({type(exc).__name__}) : {exc}") from exc
    _capture_registry()
    report.facts["qualification"] = _qualification_facts(result) if not isinstance(result, Mapping) else dict(result)
    problems, remaining = _qualification_problems(
        result, scope_key, now=(now or (lambda: datetime.now(timezone.utc)))()
    )
    if problems:
        blocked = bool(getattr(result, "blocked", False))
        message = (
            "qualification des deux modèles non établie par le pipeline réel : "
            + " ; ".join(problems)
            + " (aucune estimation de remplacement, aucun nouvel essai)"
        )
        raise (BudgetStop(message) if blocked else IncompleteValidation(message))
    report.facts["qualification"]["remaining_seconds"] = remaining
    if remaining is not None and on_remaining is not None:
        on_remaining(float(remaining))
        context["global_deadline_remaining_s"] = float(remaining)


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

    run_pass: Callable[
        ..., Awaitable[Any]
    ]  # (context, *, improve, agent, path_allowlist) -> ProjectRunResult (entrée publique)
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
    #: Checks REQUIS sur la base protégée (en production : ``Fixture tests`` de l'application réelle ; en test : ceux du pont).
    required_checks: Tuple[str, ...] = (REQUIRED_CHECK,)
    controls_ref: Optional[Callable[[Mapping[str, Any]], str]] = None
    #: Registre d'appartenance durable (voir ``w5_business_ownership``) : ``record_owned(project_id=…, event=…, **champs)``. Sans registre
    #: (tests de phase sans nettoyage) : aucun effet.
    record_owned: Callable[..., None] = lambda **fields: None

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
    result = _drive(services, services.run_pass(context, improve=True, agent=None, path_allowlist=R04_ALLOWLIST), "R04")
    improvement = getattr(result, "improvement", None)
    step.evidence.update(entry="run_project_from_settings(improve=True)", auto_merge_path_allowlist=list(R04_ALLOWLIST),
                         stop_reason=getattr(result, "stop_reason", None),
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
    services.record_owned(
        project_id=context["project_id"], event="improve_pr", pr_number=item.pr_number, head_sha=item.head_sha,
        merge_sha=info.merge_commit_sha,
    )  # fmt: skip
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
    entry_tip, entry_tree = _tip_and_tree(
        services, context
    )  # à la reprise : la base contient DÉJÀ l'incident, pas la préimage
    tip0, tree0 = entry_tip, entry_tree
    pending = manager.get_phase5_incident(project_id)
    resumed = pending is not None
    improvement, promoted = None, []
    if pending is not None:
        # Reprise après crash : l'incident durable est réconcilié AVANT toute nouvelle passe (aucune seconde injection, aucune
        # seconde dépense). Un incident déjà « recovered » (acquittement non fait) se juge directement.
        if pending.state != "recovered":
            outcome = _drive(services, services.resume_incident(context), "reprise R05")
            step.evidence["resumed_before_pass"] = {
                "found": outcome.found,
                "stop_reason": outcome.stop_reason,
                "reason": outcome.reason,
            }
        step.evidence["incident_state_at_entry"] = pending.state
    else:
        result = _drive(
            services, services.run_pass(context, improve=True, agent=agent, path_allowlist=INCIDENT_ALLOWLIST), "R05"
        )
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
    if incident is not None and incident.state in {
        "merge_pending",
        "health_pending",
        "revert_pending",
        "revert_in_progress",
    }:
        raise IncompleteValidation(
            f"incident Phase 5 durable ENCORE ACTIF (état {incident.state}) : le rollback n'est pas prouvé ; il se poursuit à la reprise "
            "(un lease de revert non expiré retarde celle-ci — il dure au moins 3600 s côté produit)"
        )
    if incident is None or incident.state != "recovered":
        raise AssertionError(
            f"incident durable non récupéré (état {getattr(incident, 'state', None)}) : intervention humaine"
        )
    if incident.health_command != business.health_command():
        raise AssertionError("la santé de Phase 5 n'est pas la sonde métier indépendante")
    merge_sha = str(incident.merge_sha)
    tip1, tree1 = _tip_and_tree(services, context)
    branches, owner, repo = services.clients.branches, services.owner, services.repo
    # Ancre AUTORITAIRE de l'état d'avant l'incident : celle que Phase 5 a persistée avant sa première écriture (valable aussi
    # à la reprise, quand la base courante contient déjà la fusion de l'incident).
    tip0 = str(incident.base_sha_before_merge).lower()
    tree0 = branches.get_git_commit(owner, repo, tip0).tree_sha
    merge_commit = branches.get_git_commit(owner, repo, merge_sha)
    tip_commit = branches.get_git_commit(owner, repo, tip1)
    # Le checkpoint SAIN n'est jamais lu sur la base courante (qui contient déjà l'incident à la reprise) : trois témoins
    # indépendants doivent désigner le même commit — l'ancre durable de Phase 5, le premier parent de la fusion de l'incident
    # et la base livrée par R04. Un désaccord ne se tranche pas : une préimage erronée ne prouverait aucune restauration.
    delivered = (context.get("r04") or {}).get("tip")
    witnesses = {
        "ancre durable de Phase 5": tip0,
        "premier parent de la fusion de l'incident": (
            str(merge_commit.parents[0]).lower() if merge_commit.parents else None
        ),
        "base livrée par R04": str(delivered).lower() if delivered else None,
    }
    if len(set(witnesses.values())) != 1 or None in witnesses.values():
        raise AssertionError(f"checkpoint sain incohérent entre ses témoins durables : {witnesses}")
    if (context.get("r04") or {}).get("tree") not in (None, tree0):
        raise AssertionError("l'arbre du checkpoint sain diffère de l'arbre livré par R04")
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
    missing = [name for name in services.required_checks if states.get(name, (None,))[0] != "success"]
    wanted = (services.manifest or {}).get("check_app_id")
    if not checks.complete or missing or (wanted is not None and states.get(REQUIRED_CHECK, (None, None))[1] != wanted):
        raise AssertionError(
            f"la PR de revert n'a pas ses checks requis réels verts (manquants ou rouges : {missing} ; vus : {states})"
        )
    observation = services.verify_tip(context, tip1)
    if observation.status != "passed" or not observation.checks.get("write:legal_notice_present"):
        raise AssertionError(
            f"santé indépendante non rétablie sur la base restaurée ({observation.status}: {observation.failed})"
        )
    _check_controls(services, context, "après R05")
    # Attribution durable AVANT l'acquittement (l'incident résolu est supprimé de la base d'état) : PR de l'incident, PR de revert et leurs fusions.
    services.record_owned(
        project_id=project_id, event="incident_pr", pr_number=incident.source_pr_number, head_sha=incident.source_head_sha,
        merge_sha=merge_sha,
    )  # fmt: skip
    services.record_owned(
        project_id=project_id, event="revert_pr", pr_number=revert_pr.number, head_sha=revert_pr.head_sha,
        head_branch=revert_branch, merge_sha=revert_pr.merge_commit_sha,
    )  # fmt: skip
    from collegue.state.manager import Phase5IncidentConflictError

    def acknowledge(revision: int) -> bool:
        """Acquittement CAS : une révision périmée est REFUSÉE (conflit), jamais appliquée."""
        try:
            return bool(manager.acknowledge_phase5_incident(project_id, expected_revision=revision))
        except Phase5IncidentConflictError:
            return False

    stale = acknowledge(incident.revision + 1000)
    acknowledged = acknowledge(incident.revision)
    again = acknowledge(incident.revision)
    if stale or not acknowledged or again or manager.get_phase5_incident(project_id) is not None:
        raise AssertionError(f"acquittement CAS incorrect (périmé={stale}, réel={acknowledged}, rejeu={again})")
    manager.record_decision(project_id, "Incident Phase 5 de la campagne inspecté et acquitté (CAS).")
    recovery = _drive(services, services.resume_incident(context), "reprise après acquittement")
    if recovery.found or not recovery.continue_loop:
        raise AssertionError("la reprise n'est pas libre après l'acquittement")
    context["r05"] = {"incident_pr": incident.source_pr_number, "merge_sha": merge_sha, "revert_pr": revert_pr.number}
    step.evidence.update(
        incident_state="recovered", incident_pr=incident.source_pr_number, merge_sha=merge_sha, revert_pr=revert_pr.number,
        revert_pr_merge_sha=revert_pr.merge_commit_sha, revert_checks=states, healthy_checkpoint=witnesses, tip_at_entry=entry_tip, tip_before=tip0, tip_after=tip1, tree_before=tree0,
        tree_after=tree1, tree_at_incident=merge_commit.tree_sha, health_command_is_independent_probe=True,
        health_after=observation.status, acknowledged_revision=incident.revision, cas_stale_rejected=not stale,
        replay_rejected=not again, recovery_found=recovery.found, recovery_continue=recovery.continue_loop, resumed=resumed,
    )  # fmt: skip


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


def production_run_pass(
    *, owner: str, repo: str, env: Optional[Mapping[str, str]] = None
) -> Callable[..., Awaitable[Any]]:
    """Entrée PUBLIQUE du produit pour R04/R05 : aucune injection de client, de sandbox, de relecteur ni de mesure — tout vient de
    la configuration effective (relais budgétaire, vrai GitHub). Seul ``agent`` (R05) est fourni, et il est signalé.

    ``path_allowlist`` CIBLE la passe sans toucher à la politique : le réglage produit ``AUTO_MERGE_PATH_ALLOWLIST`` est resserré à
    la cible de la phase (jamais élargi), via un ``Settings`` construit depuis l'environnement validé + cette seule différence."""

    async def run(
        context: Mapping[str, Any], *, improve: bool, agent: Optional[Any] = None, path_allowlist: Sequence[str] = ()
    ) -> Any:
        from collegue.pilot import run_project_from_settings

        kwargs: Dict[str, Any] = {"agent": agent} if agent is not None else {}
        if path_allowlist:
            if env is None:
                raise IncompleteValidation(
                    "environnement validé absent : le ciblage de la passe ne peut pas être appliqué"
                )
            kwargs["settings_obj"] = business.effective_settings(
                {**env, "AUTO_MERGE_PATH_ALLOWLIST": ",".join(path_allowlist)}
            )
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
    config: Any = None,
) -> PhaseServices:
    """Câblage RÉEL : entrée publique du produit, vrais clients GitHub, sonde métier en conteneur, état durable de la campagne."""
    import shutil

    owner, _, repo = FIXTURE_REPOSITORY.partition("/")
    manager = default_manager_factory(env)
    run_pass = production_run_pass(owner=owner, repo=repo, env=env)

    async def resume(context: Mapping[str, Any]) -> ResumeOutcome:
        # Reprise PUBLIQUE sans génération : ``improve=False`` ne lance rien ; la barrière Phase 5 du produit réconcilie l'incident.
        result = await run_pass(context, improve=False, agent=None, path_allowlist=INCIDENT_ALLOWLIST)
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

    def record_owned(*, project_id: int, event: str, **fields: Any) -> None:
        if config is None:
            return
        plan = getattr(manager().get_project(int(project_id)), "approved_plan_hash", None)
        identity = ownership.identity_of(owner, repo, config.base_branch, project_id=int(project_id), plan_hash=plan)
        ownership.append_event(config.manifest_path, identity, event, **fields)

    return PhaseServices(
        run_pass=run_pass, clients=clients, manager=manager, resume_incident=resume, verify_tip=verify_tip, owner=owner,
        repo=repo, deadline_monotonic=deadline_monotonic, clock=clock, manifest=manifest, record_owned=record_owned,
    )  # fmt: skip


# ── lancement sur base PROTÉGÉE : SPEC par PR, nettoyage de ce que le nightly ne connaît pas ─────────────────────────────────────


def materialize_spec_for_launch(
    *, clients: Any, config: Any, env: Mapping[str, str], project_id: int, deadline: Callable[[], float]
) -> Any:
    """Matérialise la SPEC approuvée par une PR sous les protections réelles (voir :mod:`w5_business_spec`) ; l'échéance globale
    atteinte est un arrêt budget, tout autre refus un échec explicite AVANT les BUILD."""
    try:
        return spec_publication.materialize_approved_spec(
            clients=clients, owner=config.owner, repo=config.repo, manager=default_manager_factory(env)(), project_id=project_id,
            manifest_path=config.manifest_path, trust_manifest_path=str(env.get(fixture_policy.TRUST_ANCHOR_ENV, "") or "") or None,
            deadline_monotonic=deadline,
        )  # fmt: skip
    except spec_publication.SpecDeadline as exc:
        raise BudgetStop(str(exc)) from exc


def cleanup_campaign_resources(
    report: CampaignReport, *, clients: Any, config: Any, env: Mapping[str, str]
) -> Dict[str, Any]:
    """Avant le nettoyage nightly, SANS mémoire du run : ne traite que ce que l'état DURABLE de cette campagne désigne (preuves de livraison
    du projet dans la base d'état, registre d'appartenance à côté du manifeste), recoupé avec GitHub. Une ressource inconnue, étrangère ou
    ambiguë est conservée et rend le nettoyage incomplet — jamais supprimable pour obtenir un nettoyage vert."""
    from collegue.pilot.nightly_e2e import _load_manifest

    manager = None
    if str(env.get("STATE_DATABASE_URL", "") or ""):
        manager = default_manager_factory(env)()
    manifest = _load_manifest(config.manifest_path)
    project_id = getattr(manifest, "project_id", None)
    out: Dict[str, Any] = {
        "spec": spec_publication.cleanup_spec_resources(clients, config.owner, config.repo, config.manifest_path)
    }
    _events, owned = spec_publication._owned_view(
        manager, project_id, config.owner, config.repo, config.base_branch, config.manifest_path
    )
    out["residual_pull_requests"] = spec_publication.close_residual_pull_requests(
        clients, config.owner, config.repo, config.base_branch, owned
    )
    anchor_rows = None
    manifest_path = str(env.get(fixture_policy.TRUST_ANCHOR_ENV, "") or "")
    if manifest_path:
        anchor_rows = fixture_policy.load_trust_anchor(clients.branches, config.owner, config.repo, manifest_path).rows
    out["merged_heads"] = spec_publication.reconcile_merged_heads(
        clients, config, manager=manager, project_id=project_id
    )
    out["base"] = spec_publication.advance_recorded_base(
        clients, config, anchor_rows=anchor_rows, manager=manager, project_id=project_id
    )
    return out


__all__ = [
    "materialize_spec_for_launch",
    "cleanup_campaign_resources",
    "DeterministicIncidentAgent",
    "PhaseServices",
    "check_gemma_models",
    "check_broker_selection",
    "claim_campaign_identity",
    "activate_budget",
    "broker_qualifier",
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
