"""Smoke nightly réel du cycle produit sur un dépôt fixture GitHub dédié.

Ce module orchestre les *vraies* commandes publiques du produit dans des
processus séparés : ``plan draft`` → ``plan approve`` → ``plan sync --execute``
→ RUN ``--execute``. Toutes les écritures visent une branche de base éphémère ;
la branche par défaut du dépôt fixture est épinglée et ne doit jamais bouger.

Le cleanup est une commande autonome et idempotente afin que le workflow puisse
la relancer avec ``if: always()`` après une erreur du processus principal. Les
secrets restent dans l'environnement : ils ne sont écrits ni dans les argv, ni
dans le manifeste durable du smoke.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from collegue.tools.base import ToolExecutionError
from collegue.tools.github_commands import (
    BranchCommands,
    FileCommands,
    IssueCommands,
    LabelCommands,
    PRCommands,
    RepoCommands,
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_RE = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/([A-Za-z0-9._-]{1,100})$")
_RUN_COMPONENT_RE = re.compile(r"^[1-9][0-9]*$")
_GEMINI_API_MODEL_RE = re.compile(r"^(?:gemini-[a-z0-9][a-z0-9._-]*|gemma-4-(?:31b-it|26b-a4b-it))$")
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FIXTURE_SENTINEL = "COLLEGUE_NIGHTLY_FIXTURE_V1\n"
_DEFAULT_MARKER_PATH = ".collegue-nightly-fixture"
_EXEC_MARKER = "<!-- collegue-exec:"
_TASK_MARKER = "<!-- collegue-task:"
_NIGHTLY_LABEL_COLOR = "1d76db"

# Contrat volontairement minuscule : un seul changement observable et un seul
# test. Les chemins font partie du problème car le planner ne voit pas le dépôt.
FIXTURE_PROBLEM = (
    "Sur le dépôt fixture Python existant, réalise exactement UNE tâche atomique. "
    "Ajoute dans `app/main.py` une route GET `/nightly` qui renvoie exactement "
    'le JSON {"status": "ok", "source": "collegue"}, puis ajoute ou '
    "mets à jour `tests/test_app.py` pour vérifier le code HTTP 200 et ce JSON exact. "
    "Ne modifie aucun autre comportement et n'ajoute aucune dépendance."
)


class NightlyE2EError(RuntimeError):
    """Le smoke ne peut pas prouver son contrat sans risque ou sans ambiguïté."""


def _required(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name, "") or "").strip()
    if not value:
        raise NightlyE2EError(f"variable requise absente: {name}")
    return value


def _is_true(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def _is_explicit_false(value: Any) -> bool:
    return str(value or "").strip().lower() in {"0", "false", "no", "off"}


def _validate_run_environment(values: Mapping[str, str]) -> None:
    """Valide les seules protections nécessaires au chemin coûteux ``run``.

    ``cleanup`` ne doit jamais dépendre d'une clé LLM ou d'un modèle valide : il
    doit pouvoir finaliser les ressources même si le préflight du run a échoué.
    """
    required_truthy = ("GATE_ACCEPTANCE_TESTS", "GATE_SMOKE_RUN", "REQUIRE_COST_PRICING")
    for name in required_truthy:
        if not _is_true(values.get(name)):
            raise NightlyE2EError(f"{name} doit être activé pour le smoke")
    for name in ("BUILD_AUTO_MERGE", "AUTO_MERGE_ENABLED"):
        if not _is_explicit_false(values.get(name)):
            raise NightlyE2EError(f"{name} doit être explicitement false pour le smoke")
    if str(values.get("BUDGET_EXHAUSTED_ACTION", "pause")).strip().lower() != "pause":
        raise NightlyE2EError("BUDGET_EXHAUSTED_ACTION doit valoir pause")
    if _required(values, "GATE_SMOKE_PATHS") != "/nightly":
        raise NightlyE2EError("GATE_SMOKE_PATHS doit cibler exactement /nightly")

    numeric_bounds = {
        "MAX_COST_USD": 10.0,
        "LLM_CALL_TIMEOUT": 600.0,
        "COLLEGUE_RUN_DEADLINE_SECONDS": 3600.0,
        "COLLEGUE_NIGHTLY_COMMAND_TIMEOUT_SECONDS": 3600.0,
        "SANDBOX_TIMEOUT": 1800.0,
    }
    for name, maximum in numeric_bounds.items():
        try:
            value = float(_required(values, name))
        except ValueError as exc:
            raise NightlyE2EError(f"{name} doit être numérique, fini et positif") from exc
        if not math.isfinite(value) or value <= 0 or value > maximum:
            raise NightlyE2EError(f"{name} doit être fini dans ]0, {maximum:g}]")
    try:
        token_budget = int(_required(values, "MAX_TOKENS_BUDGET"))
    except ValueError as exc:
        raise NightlyE2EError("MAX_TOKENS_BUDGET doit être un entier positif") from exc
    if token_budget <= 0 or token_budget > 2_000_000:
        raise NightlyE2EError("MAX_TOKENS_BUDGET doit être dans [1, 2000000]")
    try:
        attempts = int(_required(values, "TASK_MAX_ATTEMPTS"))
    except ValueError as exc:
        raise NightlyE2EError("TASK_MAX_ATTEMPTS doit être un entier") from exc
    if attempts != 1:
        raise NightlyE2EError("TASK_MAX_ATTEMPTS doit valoir 1")

    provider = _required(values, "LLM_PROVIDER").lower()
    if provider != "gemini":
        raise NightlyE2EError("le premier smoke nightly exige le provider global gemini")
    global_model = _required(values, "LLM_MODEL").lower()
    from collegue.monitoring.pricing import has_explicit_pricing, is_explicitly_free

    effective_models: dict[Optional[str], str] = {}
    for role in (None, "CODER", "QA", "REVIEWER", "PLANNER"):
        suffix = f"_{role}" if role else ""
        label = f"LLM_MODEL{suffix}"
        model = str(values.get(label, "") or "").strip().lower() or global_model
        # Les noms préfixés ``openai/...``/``models/...`` sont refusés : le coder
        # OpenHands route LiteLLM à partir de ce préfixe et pourrait envoyer la
        # clé Gemini au mauvais provider.
        if _GEMINI_API_MODEL_RE.fullmatch(model) is None:
            raise NightlyE2EError(f"{label} effectif doit être un modèle Gemini API non préfixé autorisé")
        if not has_explicit_pricing(model, provider=provider):
            raise NightlyE2EError(f"{label} effectif n'a pas de tarif explicite dans la grille")
        effective_models[role] = model
        if role:
            role_provider = str(values.get(f"LLM_PROVIDER_{role}", "") or "").strip().lower()
            if role_provider and role_provider != provider:
                raise NightlyE2EError(f"LLM_PROVIDER_{role} doit être vide ou égal au provider global")

    prices: list[float] = []
    for name in ("LLM_PRICE_PROMPT_PER_1M", "LLM_PRICE_COMPLETION_PER_1M"):
        try:
            price = float(_required(values, name))
        except ValueError as exc:
            raise NightlyE2EError(f"{name} doit être un nombre fini positif ou nul") from exc
        if not math.isfinite(price) or price < 0:
            raise NightlyE2EError(f"{name} doit être un nombre fini positif ou nul")
        prices.append(price)
    if not any(price > 0 for price in prices) and not is_explicitly_free(effective_models["CODER"], provider=provider):
        raise NightlyE2EError(
            "au moins un prix LLM doit être strictement positif, sauf pour un modèle coder explicitement gratuit"
        )
    _required(values, "LLM_API_KEY")
    if not os.path.isabs(os.path.expanduser(_required(values, "COLLEGUE_HOME"))):
        raise NightlyE2EError("COLLEGUE_HOME doit être absolu pour rendre le budget durable")
    if _required(values, "SANDBOX_IMAGE") != "collegue-sandbox-openhands:ci":
        raise NightlyE2EError("SANDBOX_IMAGE doit viser l'image OpenHands construite par le job")


@dataclass(frozen=True)
class NightlyConfig:
    """Configuration fermée du smoke, dérivée uniquement de l'environnement CI."""

    token: str = field(repr=False)
    repository: str
    repository_id: int
    root_branch: str
    seed_sha: str
    run_id: str
    run_attempt: str
    manifest_path: str
    marker_path: str = _DEFAULT_MARKER_PATH

    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        *,
        action: str = "run",
    ) -> "NightlyConfig":
        values = os.environ if env is None else env
        if action not in {"run", "cleanup"}:
            raise NightlyE2EError(f"action nightly inconnue: {action!r}")
        if not _is_true(values.get("COLLEGUE_NIGHTLY_E2E")):
            raise NightlyE2EError("COLLEGUE_NIGHTLY_E2E doit valoir true/1 (opt-in explicite)")

        repository = _required(values, "INTEGRATION_FIXTURE_REPOSITORY")
        match = _REPOSITORY_RE.fullmatch(repository)
        if match is None:
            raise NightlyE2EError("INTEGRATION_FIXTURE_REPOSITORY doit être owner/repo")
        source_repository = str(values.get("GITHUB_REPOSITORY", "") or "").strip().lower()
        if source_repository and repository.lower() == source_repository:
            raise NightlyE2EError("le dépôt fixture doit être distinct du dépôt Collègue")

        try:
            repository_id = int(_required(values, "INTEGRATION_FIXTURE_REPOSITORY_ID"))
        except ValueError as exc:
            raise NightlyE2EError("INTEGRATION_FIXTURE_REPOSITORY_ID doit être un entier") from exc
        if repository_id <= 0:
            raise NightlyE2EError("INTEGRATION_FIXTURE_REPOSITORY_ID doit être positif")

        root_branch = _required(values, "INTEGRATION_FIXTURE_ROOT_BRANCH")
        seed_sha = _required(values, "INTEGRATION_FIXTURE_SEED_SHA").lower()
        if _SHA_RE.fullmatch(seed_sha) is None:
            raise NightlyE2EError("INTEGRATION_FIXTURE_SEED_SHA doit être un SHA Git complet")

        run_id = _required(values, "GITHUB_RUN_ID")
        run_attempt = _required(values, "GITHUB_RUN_ATTEMPT")
        if _RUN_COMPONENT_RE.fullmatch(run_id) is None or _RUN_COMPONENT_RE.fullmatch(run_attempt) is None:
            raise NightlyE2EError("GITHUB_RUN_ID/GITHUB_RUN_ATTEMPT doivent être des entiers positifs")

        manifest_path = _required(values, "COLLEGUE_NIGHTLY_MANIFEST")
        if not os.path.isabs(os.path.expanduser(manifest_path)):
            raise NightlyE2EError("COLLEGUE_NIGHTLY_MANIFEST doit être un chemin absolu")
        marker_path = str(values.get("INTEGRATION_FIXTURE_MARKER_PATH", _DEFAULT_MARKER_PATH) or "").strip()
        if (
            not marker_path
            or marker_path.startswith(("/", ".//"))
            or "\\" in marker_path
            or ".." in marker_path.split("/")
            or any(part in {"", "."} for part in marker_path.split("/"))
        ):
            raise NightlyE2EError("INTEGRATION_FIXTURE_MARKER_PATH doit être un chemin relatif sûr")

        _required(values, "GITHUB_TOKEN")
        state_url = _required(values, "STATE_DATABASE_URL")
        if not state_url.startswith("sqlite:////"):
            raise NightlyE2EError("STATE_DATABASE_URL du smoke doit viser une SQLite absolue et éphémère")
        if action == "run":
            _validate_run_environment(values)

        return cls(
            token=_required(values, "GITHUB_TOKEN"),
            repository=repository,
            repository_id=repository_id,
            root_branch=root_branch,
            seed_sha=seed_sha,
            run_id=run_id,
            run_attempt=run_attempt,
            manifest_path=os.path.expanduser(manifest_path),
            marker_path=marker_path,
        )

    @property
    def owner(self) -> str:
        return self.repository.split("/", 1)[0]

    @property
    def repo(self) -> str:
        return self.repository.split("/", 1)[1]

    @property
    def tag(self) -> str:
        return f"{self.run_id}-{self.run_attempt}"

    @property
    def base_branch(self) -> str:
        return f"collegue-nightly/{self.tag}"

    @property
    def issue_label(self) -> str:
        return f"collegue-nightly-{self.tag}"


@dataclass
class NightlyManifest:
    version: int
    repository: str
    repository_id: int
    root_branch: str
    root_sha: str
    base_branch: str
    issue_label: str
    base_creation_started: bool = False
    base_created: bool = False
    label_creation_started: bool = False
    label_created: bool = False
    label_deleted: bool = False
    base_sha: Optional[str] = None
    project_id: Optional[int] = None
    plan_hash: Optional[str] = None
    issue_numbers: list[int] = field(default_factory=list)
    pr_numbers: list[int] = field(default_factory=list)
    head_shas: dict[str, str] = field(default_factory=dict)
    head_creation_started: bool = False
    expected_head_branch: Optional[str] = None

    @classmethod
    def for_config(cls, config: NightlyConfig) -> "NightlyManifest":
        return cls(
            version=1,
            repository=config.repository,
            repository_id=config.repository_id,
            root_branch=config.root_branch,
            root_sha=config.seed_sha,
            base_branch=config.base_branch,
            issue_label=config.issue_label,
        )


@dataclass(frozen=True)
class NightlyClients:
    repos: Any
    files: Any
    branches: Any
    prs: Any
    issues: Any
    labels: Any

    @classmethod
    def real(cls, token: str) -> "NightlyClients":
        return cls(
            repos=RepoCommands(token=token),
            files=FileCommands(token=token),
            branches=BranchCommands(token=token),
            prs=PRCommands(token=token),
            issues=IssueCommands(token=token),
            labels=LabelCommands(token=token),
        )


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


def _subprocess_runner(argv: Sequence[str], *, cwd: Optional[str] = None) -> CommandResult:
    try:
        timeout = float(os.environ.get("COLLEGUE_NIGHTLY_COMMAND_TIMEOUT_SECONDS", "2700"))
    except ValueError:
        timeout = 2700.0
    completed = subprocess.run(
        list(argv),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="" if completed.stderr.endswith("\n") else "\n")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _json_result(result: CommandResult, *, accepted_codes: tuple[int, ...] = (0,)) -> dict[str, Any]:
    if result.returncode not in accepted_codes:
        raise NightlyE2EError(
            f"commande produit échouée (code {result.returncode}): {(result.stderr or result.stdout)[-1000:]}"
        )
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise NightlyE2EError(f"sortie JSON produit invalide: {result.stdout[-1000:]}") from exc
    if not isinstance(payload, dict):
        raise NightlyE2EError("la sortie JSON produit doit être un objet")
    return payload


def _write_manifest(path: str, manifest: NightlyManifest) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(manifest), handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_manifest(path: str) -> Optional[NightlyManifest]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise NightlyE2EError(f"manifeste nightly illisible: {exc}") from exc
    try:
        return NightlyManifest(**raw)
    except (TypeError, ValueError) as exc:
        raise NightlyE2EError("manifeste nightly malformé") from exc


def _validate_manifest(manifest: NightlyManifest, config: NightlyConfig) -> None:
    if manifest.version != 1:
        raise NightlyE2EError(f"version de manifeste nightly inconnue: {manifest.version!r}")
    if (
        manifest.repository != config.repository
        or manifest.repository_id != config.repository_id
        or manifest.root_branch != config.root_branch
        or manifest.root_sha != config.seed_sha
        or manifest.base_branch != config.base_branch
        or manifest.issue_label != config.issue_label
    ):
        raise NightlyE2EError("le manifeste ne correspond pas à ce run/repository/seed")
    if not isinstance(manifest.base_creation_started, bool):
        raise NightlyE2EError("base_creation_started invalide dans le manifeste")
    if not isinstance(manifest.base_created, bool):
        raise NightlyE2EError("base_created invalide dans le manifeste")
    if not isinstance(manifest.label_creation_started, bool):
        raise NightlyE2EError("label_creation_started invalide dans le manifeste")
    if not isinstance(manifest.label_created, bool):
        raise NightlyE2EError("label_created invalide dans le manifeste")
    if not isinstance(manifest.label_deleted, bool):
        raise NightlyE2EError("label_deleted invalide dans le manifeste")
    if (manifest.label_created or manifest.label_deleted) and not manifest.label_creation_started:
        raise NightlyE2EError("état de propriété du label incohérent dans le manifeste")
    if manifest.base_sha is not None and _SHA_RE.fullmatch(str(manifest.base_sha)) is None:
        raise NightlyE2EError("base_sha invalide dans le manifeste")
    if manifest.project_id is not None and (
        not isinstance(manifest.project_id, int) or isinstance(manifest.project_id, bool) or manifest.project_id <= 0
    ):
        raise NightlyE2EError("project_id invalide dans le manifeste")
    if manifest.plan_hash is not None and _SHA256_RE.fullmatch(str(manifest.plan_hash)) is None:
        raise NightlyE2EError("plan_hash invalide dans le manifeste")
    for label, numbers in (("issues", manifest.issue_numbers), ("PR", manifest.pr_numbers)):
        if (
            not isinstance(numbers, list)
            or len(numbers) != len(set(numbers))
            or any(not isinstance(number, int) or isinstance(number, bool) or number <= 0 for number in numbers)
        ):
            raise NightlyE2EError(f"numéros {label} invalides dans le manifeste")
    if not isinstance(manifest.head_shas, dict):
        raise NightlyE2EError("head_shas invalide dans le manifeste")
    for branch, sha in manifest.head_shas.items():
        if not re.fullmatch(r"collegue/issue-[1-9][0-9]*", str(branch)) or _SHA_RE.fullmatch(str(sha)) is None:
            raise NightlyE2EError("preuve de branche head invalide dans le manifeste")
    if not isinstance(manifest.head_creation_started, bool):
        raise NightlyE2EError("head_creation_started invalide dans le manifeste")
    if (
        manifest.expected_head_branch is not None
        and re.fullmatch(r"collegue/issue-[1-9][0-9]*", str(manifest.expected_head_branch)) is None
    ):
        raise NightlyE2EError("branche head attendue invalide dans le manifeste")
    if manifest.head_creation_started and manifest.expected_head_branch is None:
        raise NightlyE2EError("intent de création head sans branche attendue")
    if manifest.head_creation_started:
        expected_issue = int(str(manifest.expected_head_branch).rsplit("-", 1)[1])
        if expected_issue not in manifest.issue_numbers:
            raise NightlyE2EError("intent de création head absent des issues du manifeste")


class NightlyE2ERunner:
    """Orchestrateur injectable : réseau réel en nightly, doubles en tests."""

    def __init__(
        self,
        config: NightlyConfig,
        *,
        clients: Optional[NightlyClients] = None,
        command_runner: Callable[..., CommandResult] = _subprocess_runner,
    ):
        self.config = config
        self.clients = clients or NightlyClients.real(config.token)
        self.command_runner = command_runner

    def _guard_fixture(self, *, require_seed: bool) -> str:
        cfg = self.config
        repo = self.clients.repos.get_repo(cfg.owner, cfg.repo)
        if int(getattr(repo, "id", 0) or 0) != cfg.repository_id:
            raise NightlyE2EError("identité immuable du dépôt fixture incorrecte")
        if str(getattr(repo, "full_name", "")).lower() != cfg.repository.lower():
            raise NightlyE2EError("coordonnée du dépôt fixture incorrecte")
        if bool(getattr(repo, "is_private", True)):
            raise NightlyE2EError("le dépôt fixture doit être public (clone sans secret)")
        if getattr(repo, "default_branch", None) != cfg.root_branch:
            raise NightlyE2EError("branche par défaut du dépôt fixture inattendue")
        marker = self.clients.files.get_file_content(cfg.owner, cfg.repo, cfg.marker_path, branch=cfg.root_branch)
        if marker.get("content") != _FIXTURE_SENTINEL:
            raise NightlyE2EError("sentinelle du dépôt fixture absente ou invalide")
        root_sha = self.clients.branches.get_branch_sha(cfg.owner, cfg.repo, cfg.root_branch)
        if _SHA_RE.fullmatch(str(root_sha or "").lower()) is None:
            raise NightlyE2EError("SHA racine du dépôt fixture invérifiable")
        if require_seed and root_sha.lower() != cfg.seed_sha:
            raise NightlyE2EError("le commit seed de la fixture a bougé — zéro écriture")
        return root_sha.lower()

    def _branch_sha_or_none(self, branch: str) -> Optional[str]:
        """Retourne ``None`` uniquement pour une absence GitHub 404 confirmée."""
        try:
            return self.clients.branches.get_branch_sha(self.config.owner, self.config.repo, branch)
        except ToolExecutionError as exc:
            if getattr(exc, "status_code", 0) == 404:
                return None
            raise

    def _base_owner_message(self) -> str:
        cfg = self.config
        return f"collegue-nightly: base owner run={cfg.tag} repo_id={cfg.repository_id} seed={cfg.seed_sha}"

    def _is_owned_base_commit(self, sha: str) -> bool:
        """Prouve que ``sha`` est le commit marqueur créé pour ce run exact."""
        try:
            root = self.clients.branches.get_git_commit(
                self.config.owner,
                self.config.repo,
                self.config.seed_sha,
            )
            candidate = self.clients.branches.get_git_commit(
                self.config.owner,
                self.config.repo,
                sha,
            )
        except Exception:  # noqa: BLE001 - preuve distante absente => refus
            return False
        return (
            list(candidate.parents) == [self.config.seed_sha]
            and candidate.tree_sha == root.tree_sha
            and getattr(candidate, "message", None) == self._base_owner_message()
        )

    def _create_owned_base(self, manifest: NightlyManifest) -> str:
        """Crée/reprend la base via un commit propriétaire parent+tree+message."""
        cfg = self.config
        current = self._branch_sha_or_none(cfg.base_branch)
        if current is not None:
            raise NightlyE2EError("la branche de base nightly existe avant son intent de création")
        root = self.clients.branches.get_git_commit(cfg.owner, cfg.repo, cfg.seed_sha)
        manifest.base_creation_started = True
        _write_manifest(cfg.manifest_path, manifest)
        try:
            created = self.clients.branches.ensure_commit_branch(
                cfg.owner,
                cfg.repo,
                cfg.base_branch,
                parent_sha=cfg.seed_sha,
                tree_sha=root.tree_sha,
                message=self._base_owner_message(),
            )
            owned_sha = str(getattr(created, "commit_sha", "") or "").lower()
        except Exception:  # noqa: BLE001 - réconcilier une réponse POST perdue
            owned_sha = str(self._branch_sha_or_none(cfg.base_branch) or "").lower()
            if not owned_sha or not self._is_owned_base_commit(owned_sha):
                raise
        if _SHA_RE.fullmatch(owned_sha) is None or not self._is_owned_base_commit(owned_sha):
            raise NightlyE2EError("commit propriétaire de la base nightly non confirmé")
        manifest.base_created = True
        manifest.base_sha = owned_sha
        _write_manifest(cfg.manifest_path, manifest)
        return owned_sha

    def _authorize_head_creation(self, manifest: NightlyManifest, issue_number: int) -> str:
        """Checkpoint l'absence de la branche code avant de lancer l'exécuteur."""
        branch = f"collegue/issue-{int(issue_number)}"
        if self._branch_sha_or_none(branch) is not None:
            raise NightlyE2EError("branche head nightly présente avant son intent de création")
        manifest.head_creation_started = True
        manifest.expected_head_branch = branch
        _write_manifest(self.config.manifest_path, manifest)
        return branch

    def _head_chain_is_owned(self, issue_number: int, head_sha: str, base_sha: str) -> bool:
        """Prouve une branche head sans PR par sa chaîne bornée jusqu'à la base."""
        current = str(head_sha or "").lower()
        base = str(base_sha or "").lower()
        if _SHA_RE.fullmatch(current) is None or _SHA_RE.fullmatch(base) is None:
            return False
        prefix = f"collegue: issue #{int(issue_number)} — "
        for _ in range(32):
            if current == base:
                return True
            try:
                commit = self.clients.branches.get_git_commit(
                    self.config.owner,
                    self.config.repo,
                    current,
                )
            except Exception:  # noqa: BLE001 - preuve incomplète => conservation
                return False
            message = str(getattr(commit, "message", "") or "")
            path = message[len(prefix) :] if message.startswith(prefix) else ""
            parts = path.split("/")
            if (
                list(getattr(commit, "parents", ()) or ()) == []
                or len(commit.parents) != 1
                or not path
                or "\n" in path
                or "\\" in path
                or path.startswith("/")
                or any(part in {"", ".", ".."} for part in parts)
            ):
                return False
            current = str(commit.parents[0]).lower()
        return False

    def _run_product(self, *args: str, accepted_codes: tuple[int, ...] = (0,), cwd: Optional[str] = None) -> dict:
        result = self.command_runner([sys.executable, "-m", "collegue.pilot", *args], cwd=cwd)
        return _json_result(result, accepted_codes=accepted_codes)

    def _manager(self):
        from collegue.config import Settings
        from collegue.state import ProjectStateManager

        settings_obj = Settings()
        return ProjectStateManager.from_url(settings_obj.STATE_DATABASE_URL)

    def _label_owner_description(self) -> str:
        """Marqueur distant compact qui lie le label à ce run exact."""
        cfg = self.config
        material = f"{cfg.repository_id}:{cfg.repository.lower()}:{cfg.tag}:{cfg.seed_sha}"
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
        return f"collegue-nightly-owner:{digest}"

    def _labels_named_for_run(self) -> list[Any]:
        target = self.config.issue_label.lower()
        return [
            label
            for label in self.clients.labels.list_labels(self.config.owner, self.config.repo)
            if str(getattr(label, "name", "")).lower() == target
        ]

    def _owned_label_or_none(self) -> Optional[Any]:
        """Retourne uniquement le label portant la preuve exacte de ce run."""
        matches = self._labels_named_for_run()
        if not matches:
            return None
        if len(matches) != 1:
            raise NightlyE2EError("plusieurs labels correspondent au nom unique du run")
        label = matches[0]
        if (
            getattr(label, "name", None) != self.config.issue_label
            or str(getattr(label, "color", "")).lower() != _NIGHTLY_LABEL_COLOR
            or getattr(label, "description", None) != self._label_owner_description()
        ):
            raise NightlyE2EError("label du run présent sans preuve de propriété exacte")
        return label

    def _create_owned_label(self, manifest: NightlyManifest) -> None:
        """Crée le label après un constat d'absence, avec intent durable avant POST."""
        if manifest.label_creation_started:
            raise NightlyE2EError("création du label nightly déjà engagée")
        if self._labels_named_for_run():
            raise NightlyE2EError("le label unique du run existe déjà ; refus de l'adopter")

        # Le checkpoint précède impérativement la mutation distante. Si le POST
        # réussit mais que sa réponse se perd, cleanup peut reconnaître exactement
        # le label créé et le supprimer sans adopter un label antérieur au run.
        manifest.label_creation_started = True
        _write_manifest(self.config.manifest_path, manifest)
        description = self._label_owner_description()
        try:
            created = self.clients.labels.ensure_label(
                self.config.owner,
                self.config.repo,
                self.config.issue_label,
                color=_NIGHTLY_LABEL_COLOR,
                description=description,
            )
            if (
                getattr(created, "name", None) != self.config.issue_label
                or str(getattr(created, "color", "")).lower() != _NIGHTLY_LABEL_COLOR
                or getattr(created, "description", None) != description
            ):
                raise NightlyE2EError("réponse de création du label sans preuve de propriété exacte")
        except Exception:  # noqa: BLE001 - réconcilier une réponse POST perdue
            recovered = self._owned_label_or_none()
            if recovered is None:
                raise

        # Confirmation distante y compris lorsque ensure_label a retourné sans
        # erreur : aucun état "owned" n'est écrit sur la seule foi de la réponse.
        if self._owned_label_or_none() is None:
            raise NightlyE2EError("création du label nightly non confirmée")
        manifest.label_created = True
        _write_manifest(self.config.manifest_path, manifest)

    def _inspect_draft(self, project_id: int, plan_hash: str) -> tuple[int, str, str]:
        manager = self._manager()
        project = manager.get_project(project_id)
        tasks = manager.get_tasks(project_id)
        if project is None or len(tasks) != 1:
            raise NightlyE2EError(f"le draft nightly doit contenir exactement une tâche (vu {len(tasks)})")
        target = dict(project.plan_sync_config or {})
        expected = {
            "owner": self.config.owner,
            "repo": self.config.repo,
            "labels": [self.config.issue_label],
            "milestone_title": None,
            "board_title": None,
            "spec_filename": "SPEC.md",
            "base_branch": self.config.base_branch,
        }
        if target != expected:
            raise NightlyE2EError("la cible durable du draft ne correspond pas à la fixture éphémère")
        task = tasks[0]
        source = str(getattr(task, "acceptance_test_source", "") or "")
        provenance = getattr(task, "acceptance_test_provenance", None) or {}
        digest = str(getattr(task, "acceptance_test_sha256", "") or "")
        if (
            not source
            or _SHA256_RE.fullmatch(digest) is None
            or hashlib.sha256(source.encode("utf-8")).hexdigest() != digest
            or provenance.get("role") != "qa"
        ):
            raise NightlyE2EError("oracle QA plan-time absent, non hashé ou de provenance incorrecte")
        if not _SHA256_RE.fullmatch(plan_hash):
            raise NightlyE2EError("hash du plan invalide")
        print("<<<COLLEGUE_NIGHTLY_ORACLE_BEGIN>>>", file=sys.stderr)
        for line in source.rstrip().splitlines():
            # Neutralise la syntaxe de commande des logs GitHub Actions sans
            # altérer l'artefact réellement persisté/exécuté.
            safe_line = " " + line if line.startswith("::") else line
            print(safe_line, file=sys.stderr)
        print("<<<COLLEGUE_NIGHTLY_ORACLE_END>>>", file=sys.stderr)
        return int(task.id), str(project.spec or ""), digest

    def _clone_base(self, base_sha: str) -> str:
        parent = tempfile.mkdtemp(prefix="collegue-nightly-source-")
        destination = os.path.join(parent, "fixture")
        url = f"https://github.com/{self.config.repository}.git"
        result = self.command_runner(
            [
                "git",
                "clone",
                "--quiet",
                "--branch",
                self.config.base_branch,
                "--single-branch",
                "--depth",
                "1",
                url,
                destination,
            ],
            cwd=parent,
        )
        if result.returncode != 0:
            shutil.rmtree(parent, ignore_errors=True)
            raise NightlyE2EError(f"clone public de la fixture échoué: {(result.stderr or result.stdout)[-1000:]}")
        remote = self.command_runner(["git", "remote", "get-url", "origin"], cwd=destination)
        head = self.command_runner(["git", "rev-parse", "HEAD"], cwd=destination)
        if remote.returncode != 0 or remote.stdout.strip() != url:
            shutil.rmtree(parent, ignore_errors=True)
            raise NightlyE2EError("origin du clone fixture inattendu")
        if head.returncode != 0 or head.stdout.strip().lower() != base_sha.lower():
            shutil.rmtree(parent, ignore_errors=True)
            raise NightlyE2EError("HEAD local de la fixture différent du SPEC synchronisé")
        return destination

    def run(self) -> dict[str, Any]:
        cfg = self.config
        root_sha = self._guard_fixture(require_seed=True)
        try:
            self._manager().list_projects()
        except Exception as exc:  # noqa: BLE001 - préflight avant toute écriture distante
            raise NightlyE2EError(f"schéma STATE_DATABASE_URL non migré ou inaccessible: {exc}") from exc
        if _load_manifest(cfg.manifest_path) is not None:
            raise NightlyE2EError("un manifeste existe déjà pour ce run ; lancer cleanup avant toute reprise")
        manifest = NightlyManifest.for_config(cfg)
        manifest.root_sha = root_sha
        _write_manifest(cfg.manifest_path, manifest)
        source_path: Optional[str] = None
        run_error: Optional[BaseException] = None
        try:
            owned_base_sha = self._create_owned_base(manifest)

            draft = self._run_product(
                "plan",
                "draft",
                "--name",
                f"Nightly {cfg.tag}",
                "--problem",
                FIXTURE_PROBLEM,
                "--owner",
                cfg.owner,
                "--repo",
                cfg.repo,
                "--base",
                cfg.base_branch,
                "--labels",
                cfg.issue_label,
                "--milestone",
                "",
                "--spec-filename",
                "SPEC.md",
                "--deadline-hours",
                "0.5",
                "--format",
                "json",
            )
            project_id = int(draft.get("project_id") or 0)
            plan_hash = str(draft.get("plan_hash") or "")
            if draft.get("action") != "draft" or int(draft.get("task_count") or 0) != 1 or project_id <= 0:
                raise NightlyE2EError("contrat JSON du draft inattendu")
            task_id, spec, oracle_sha256 = self._inspect_draft(project_id, plan_hash)
            manifest.project_id = project_id
            manifest.plan_hash = plan_hash
            _write_manifest(cfg.manifest_path, manifest)

            approved = self._run_product(
                "plan",
                "approve",
                "--project-id",
                str(project_id),
                "--expected-plan-hash",
                plan_hash,
                "--format",
                "json",
            )
            if (
                approved.get("action") != "approve"
                or int(approved.get("project_id") or 0) != project_id
                or int(approved.get("task_count") or 0) != 1
                or approved.get("plan_hash") != plan_hash
            ):
                raise NightlyE2EError("l'approbation n'a pas scellé le hash attendu")

            if self.clients.branches.get_branch_sha(cfg.owner, cfg.repo, cfg.base_branch) != owned_base_sha:
                raise NightlyE2EError("la base éphémère a bougé avant plan sync")

            # Le planner réutilise un label existant. Le driver crée donc lui-même
            # ce label, après preuve d'absence et checkpoint d'intent durable, afin
            # de ne jamais adopter/supprimer un label antérieur au run.
            self._create_owned_label(manifest)

            synced = self._run_product(
                "plan",
                "sync",
                "--project-id",
                str(project_id),
                "--execute",
                "--format",
                "json",
            )
            issues = list(synced.get("issues") or [])
            issue_numbers = sorted({int(item.get("issue_number") or 0) for item in issues if item.get("issue_number")})
            if (
                synced.get("action") != "sync"
                or int(synced.get("project_id") or 0) != project_id
                or int(synced.get("task_count") or 0) != 1
                or synced.get("plan_hash") != plan_hash
                or len(issue_numbers) != 1
                or int(issues[0].get("task_id") or 0) != task_id
            ):
                raise NightlyE2EError("la synchronisation doit créer exactement une issue")
            remote_issues = self.clients.issues.list_issues(
                cfg.owner,
                cfg.repo,
                state="open",
                limit=100,
                labels=cfg.issue_label,
            )
            if [int(issue.number) for issue in remote_issues] != issue_numbers:
                raise NightlyE2EError("corrélation GitHub ambiguë: le label du run ne désigne pas une issue unique")
            manifest.issue_numbers = issue_numbers
            manifest.base_sha = self.clients.branches.get_branch_sha(cfg.owner, cfg.repo, cfg.base_branch)
            if _SHA_RE.fullmatch(str(manifest.base_sha or "")) is None:
                raise NightlyE2EError("SHA de base après sync invalide")
            _write_manifest(cfg.manifest_path, manifest)

            stored_spec = self.clients.files.get_file_content(cfg.owner, cfg.repo, "SPEC.md", branch=cfg.base_branch)
            if stored_spec.get("content") != spec:
                raise NightlyE2EError("SPEC.md distant différent du contrat approuvé")

            self._authorize_head_creation(manifest, issue_numbers[0])

            source_path = self._clone_base(manifest.base_sha)
            if self.clients.branches.get_branch_sha(cfg.owner, cfg.repo, cfg.base_branch) != manifest.base_sha:
                raise NightlyE2EError("la base éphémère a bougé entre le clone et le RUN")
            run_result = self.command_runner(
                [
                    sys.executable,
                    "-m",
                    "collegue.pilot",
                    "--project-id",
                    str(project_id),
                    "--repo-source",
                    source_path,
                    "--owner",
                    cfg.owner,
                    "--repo",
                    cfg.repo,
                    "--base",
                    cfg.base_branch,
                    "--execute",
                    "--format",
                    "json",
                ]
            )
            run_payload = _json_result(run_result, accepted_codes=(1,))
            opened_prs = [int(number) for number in list(run_payload.get("opened_prs") or [])]
            processed = list(run_payload.get("processed") or [])
            if (
                run_payload.get("stop_reason") != "awaiting_merge"
                or int(run_payload.get("project_id") or 0) != project_id
                or int(run_payload.get("iterations") or 0) != 1
                or len(opened_prs) != 1
                or len(processed) != 1
                or int(processed[0].get("task_id") or 0) != task_id
                or processed[0].get("success") is not True
                or processed[0].get("stage") != "pr"
                or int(processed[0].get("pr_number") or 0) != opened_prs[0]
                or processed[0].get("acceptance_passed") is not True
                or processed[0].get("acceptance_error") not in {None, ""}
                or processed[0].get("acceptance_oracle_sha256") != oracle_sha256
                or list(run_payload.get("pending_reviews") or []) != [task_id]
            ):
                raise NightlyE2EError("le RUN réel n'a pas abouti à une unique PR ouverte et validée")

            issue_number = issue_numbers[0]
            pr = self.clients.prs.get_pr(cfg.owner, cfg.repo, opened_prs[0])
            if (
                pr.state != "open"
                or pr.merged
                or pr.base_branch != cfg.base_branch
                or pr.base_sha != manifest.base_sha
                or pr.head_branch != f"collegue/issue-{issue_number}"
                or not pr.head_sha
                or f"<!-- collegue-exec:{issue_number} -->" not in str(pr.body or "")
                or "<!-- collegue-diff-sha256:" not in str(pr.body or "")
                or f"sha256:{oracle_sha256}" not in str(pr.body or "")
                or int(pr.changed_files or 0) < 1
            ):
                raise NightlyE2EError("la PR distante ne prouve pas le livrable attendu")
            remote_head = self.clients.branches.get_branch_sha(cfg.owner, cfg.repo, pr.head_branch)
            if remote_head != pr.head_sha:
                raise NightlyE2EError("la branche distante ne correspond pas au head SHA de la PR")
            if self.clients.branches.get_branch_sha(cfg.owner, cfg.repo, cfg.base_branch) != manifest.base_sha:
                raise NightlyE2EError("la base éphémère a bougé pendant la création de PR")

            manifest.pr_numbers = opened_prs
            manifest.head_shas[pr.head_branch] = pr.head_sha
            _write_manifest(cfg.manifest_path, manifest)
            return {
                "status": "passed",
                "project_id": project_id,
                "plan_hash": plan_hash,
                "issue_number": issue_number,
                "pr_number": opened_prs[0],
                "base_branch": cfg.base_branch,
                "head_sha": pr.head_sha,
                "acceptance_passed": True,
                "acceptance_oracle_sha256": oracle_sha256,
            }
        except BaseException as exc:  # cleanup garanti, y compris KeyboardInterrupt
            run_error = exc
            raise
        finally:
            if source_path:
                shutil.rmtree(os.path.dirname(source_path), ignore_errors=True)
            try:
                self.cleanup()
            except Exception as cleanup_exc:
                if run_error is None:
                    raise
                print(f"cleanup nightly également échoué: {cleanup_exc}", file=sys.stderr)

    def _task_correlations(self, manifest: NightlyManifest) -> dict[int, int]:
        """Retourne ``issue GitHub → task id`` depuis le plan durable exact."""
        if manifest.project_id is None or manifest.plan_hash is None:
            return {}
        manager = self._manager()
        project = manager.get_project(manifest.project_id)
        if project is None or project.approved_plan_hash != manifest.plan_hash:
            raise NightlyE2EError("plan durable du manifeste absent ou d'empreinte différente")
        return {
            int(task.issue_number): int(task.id)
            for task in manager.get_tasks(manifest.project_id)
            if task.issue_number is not None
        }

    def _approved_task_ids(self, manifest: NightlyManifest) -> set[int]:
        """Ensemble autoritatif des tâches du plan approuvé, issues ou non."""
        if manifest.project_id is None or manifest.plan_hash is None:
            return set()
        manager = self._manager()
        project = manager.get_project(manifest.project_id)
        if project is None or project.approved_plan_hash != manifest.plan_hash:
            raise NightlyE2EError("plan durable du manifeste absent ou d'empreinte différente")
        return {int(task.id) for task in manager.get_tasks(manifest.project_id)}

    def _recover_spec_commit(self, manifest: NightlyManifest, current_sha: str) -> bool:
        """Réconcilie le seul mouvement légitime oublié : le commit de SPEC.md."""
        if (
            not manifest.base_created
            or manifest.base_sha is None
            or manifest.project_id is None
            or manifest.plan_hash is None
        ):
            return False
        try:
            commit = self.clients.branches.get_git_commit(self.config.owner, self.config.repo, current_sha)
            if list(commit.parents) != [manifest.base_sha]:
                return False
            manager = self._manager()
            project = manager.get_project(manifest.project_id)
            if project is None or project.approved_plan_hash != manifest.plan_hash:
                return False
            target = dict(project.plan_sync_config or {})
            if (
                target.get("owner") != self.config.owner
                or target.get("repo") != self.config.repo
                or target.get("base_branch") != self.config.base_branch
                or target.get("spec_filename") != "SPEC.md"
            ):
                return False
            remote = self.clients.files.get_file_content(
                self.config.owner,
                self.config.repo,
                "SPEC.md",
                branch=self.config.base_branch,
            )
            return remote.get("content") == project.spec
        except Exception:  # noqa: BLE001 - preuve absente => aucune suppression
            return False

    def cleanup(self) -> dict[str, Any]:
        """Cleanup en phases : inventaire → validation → fermeture → refs."""
        cfg = self.config
        # Dépôt fixture statique : si le seed a bougé, aucune mutation de cleanup
        # n'est assez certaine. On laisse les ancres pour inspection.
        current_root = self._guard_fixture(require_seed=True)
        loaded_manifest = _load_manifest(cfg.manifest_path)
        manifest = loaded_manifest or NightlyManifest.for_config(cfg)
        _validate_manifest(manifest, cfg)

        closed_prs: list[int] = []
        closed_issues: list[int] = []
        owns_label = manifest.label_creation_started
        task_by_issue = self._task_correlations(manifest) if owns_label else {}
        approved_task_ids = self._approved_task_ids(manifest) | set(task_by_issue.values()) if owns_label else set()

        # Phase 1 — inventaire complet. Une panne ici interdit toute mutation.
        try:
            discovered_prs = self.clients.prs.list_prs(
                cfg.owner, cfg.repo, state="open", limit=100, base=cfg.base_branch
            )
            if not owns_label:
                owned_label = None
                discovered_issues = []
            elif manifest.label_deleted:
                if self._labels_named_for_run():
                    raise NightlyE2EError("le label nightly a réapparu après son checkpoint de suppression")
                owned_label = None
                discovered_issues = []
            else:
                owned_label = self._owned_label_or_none()
                discovered_issues = (
                    self.clients.issues.list_issues(
                        cfg.owner,
                        cfg.repo,
                        state="open",
                        limit=100,
                        labels=cfg.issue_label,
                    )
                    if owned_label is not None
                    else []
                )
        except Exception as exc:  # noqa: BLE001 - frontière distante fail-closed
            raise NightlyE2EError(f"inventaire GitHub incomplet, aucune suppression: {exc}") from exc

        label_remote_present = owned_label is not None
        if label_remote_present and not manifest.label_created:
            # Récupération d'un crash/perte de réponse entre le POST et le
            # checkpoint final. L'intent durable et le marqueur distant concordent.
            manifest.label_created = True
            _write_manifest(cfg.manifest_path, manifest)

        issue_numbers = (
            set(manifest.issue_numbers) | set(task_by_issue) | {int(issue.number) for issue in discovered_issues}
            if owns_label
            else set()
        )
        pr_numbers = set(manifest.pr_numbers) | {int(pr.number) for pr in discovered_prs}

        # Lire la base avant de valider les PR. Une base absente est normale lors
        # d'un second finalizer ; une base présente sans preuve ne sera pas supprimée.
        base_current = self._branch_sha_or_none(cfg.base_branch)
        if base_current is not None and not manifest.base_created:
            if not manifest.base_creation_started or not self._is_owned_base_commit(base_current):
                raise NightlyE2EError("branche de base présente sans preuve propriétaire du run")
            manifest.base_created = True
            manifest.base_sha = base_current
            _write_manifest(cfg.manifest_path, manifest)
        if base_current is not None and manifest.base_sha != base_current:
            if not self._recover_spec_commit(manifest, base_current):
                raise NightlyE2EError("branche de base nightly déplacée sans preuve du commit SPEC")
            manifest.base_sha = base_current
            _write_manifest(cfg.manifest_path, manifest)

        # Phase 2 — validation de corrélation, toujours sans mutation.
        validated_issues: dict[int, Any] = {}
        issue_numbers_by_task: dict[int, list[int]] = {}
        for number in sorted(issue_numbers):
            issue = self.clients.issues.get_issue(cfg.owner, cfg.repo, number)
            markers = [
                int(value) for value in re.findall(r"<!-- collegue-task:([1-9][0-9]*) -->", str(issue.body or ""))
            ]
            persisted_task_id = task_by_issue.get(number)
            task_id = markers[0] if len(markers) == 1 else None
            plan_marker = (
                f"<!-- collegue-plan:{manifest.plan_hash};project:{manifest.project_id};task:{task_id} -->"
                if manifest.plan_hash is not None and manifest.project_id is not None and task_id is not None
                else None
            )
            issue_labels = list(issue.labels or [])
            has_exact_label = cfg.issue_label in issue_labels
            if (
                issue.state not in {"open", "closed"}
                or (not has_exact_label and (label_remote_present or issue.state == "open"))
                or (has_exact_label and not label_remote_present)
                or task_id is None
                or task_id not in approved_task_ids
                or (persisted_task_id is not None and persisted_task_id != task_id)
                or (plan_marker is not None and plan_marker not in str(issue.body or ""))
            ):
                raise NightlyE2EError(f"issue #{number} non corrélée exactement au plan/run")
            validated_issues[number] = issue
            task_by_issue[number] = task_id
            issue_numbers_by_task.setdefault(task_id, []).append(number)

        # Perte de réponse POST : le label+marqueur atomiques permettent de
        # récupérer le numéro. Ne persister que si la correspondance est unique ;
        # des doublons sont tous nettoyables mais ne doivent pas devenir arbitraires.
        if manifest.project_id is not None:
            recoveries = [
                (task_id, numbers[0])
                for task_id, numbers in issue_numbers_by_task.items()
                if len(numbers) == 1 and numbers[0] not in set(manifest.issue_numbers)
            ]
            if recoveries:
                manager = self._manager()
                for task_id, issue_number in recoveries:
                    if not manager.update_task(task_id, issue_number=issue_number):
                        raise NightlyE2EError(f"réconciliation locale impossible pour la tâche {task_id}")
        correlated_issue_numbers = sorted(validated_issues)
        if correlated_issue_numbers != sorted(manifest.issue_numbers):
            manifest.issue_numbers = correlated_issue_numbers
            _write_manifest(cfg.manifest_path, manifest)

        validated_prs: dict[int, Any] = {}
        branch_heads: dict[str, str] = {}
        for number in sorted(pr_numbers):
            pr = self.clients.prs.get_pr(cfg.owner, cfg.repo, number)
            head_match = re.fullmatch(r"collegue/issue-([1-9][0-9]*)", str(pr.head_branch or ""))
            issue_number = int(head_match.group(1)) if head_match else None
            expected_marker = f"<!-- collegue-exec:{issue_number} -->" if issue_number is not None else None
            if (
                pr.merged
                or pr.base_branch != cfg.base_branch
                or not pr.head_sha
                or issue_number not in validated_issues
                or expected_marker not in str(pr.body or "")
                or (base_current is not None and pr.base_sha != base_current)
            ):
                raise NightlyE2EError(f"PR #{number} non corrélée exactement au plan/run/base")
            recorded = manifest.head_shas.get(pr.head_branch)
            if recorded is not None and recorded != pr.head_sha:
                raise NightlyE2EError(f"head SHA de la PR #{number} différent du manifeste")
            validated_prs[number] = pr
            branch_heads[pr.head_branch] = pr.head_sha

        # Une branche peut avoir été créée/poussée puis le POST de PR échouer.
        # L'intent a été checkpointé avant le RUN ; on n'adopte son SHA que si
        # toute sa chaîne de commits rejoint la base exacte avec les messages de
        # publication déterministes de l'exécuteur.
        head_proof_changed = False
        for issue_number in sorted(validated_issues):
            branch = f"collegue/issue-{issue_number}"
            current = self._branch_sha_or_none(branch)
            if current is None:
                continue
            recorded = branch_heads.get(branch) or manifest.head_shas.get(branch)
            if recorded is not None:
                if current != recorded:
                    raise NightlyE2EError(f"branche {branch} déplacée depuis la preuve")
                continue
            if (
                not manifest.head_creation_started
                or manifest.expected_head_branch != branch
                or manifest.base_sha is None
                or not self._head_chain_is_owned(issue_number, current, manifest.base_sha)
            ):
                raise NightlyE2EError(f"branche {branch}: SHA non prouvé (aucune PR/manifeste)")
            manifest.head_shas[branch] = current
            branch_heads[branch] = current
            head_proof_changed = True
        if head_proof_changed:
            _write_manifest(cfg.manifest_path, manifest)

        # Phase 3 — fermetures. Si l'une échoue, garder toutes les refs comme
        # ancres ; aucune suppression de branche/base n'est tentée ensuite.
        close_errors: list[str] = []
        for number in pr_numbers:
            try:
                pr = validated_prs[number]
                was_open = pr.state == "open"
                issue_number = int(pr.head_branch.rsplit("-", 1)[1])
                self.clients.prs.close_pr(
                    cfg.owner,
                    cfg.repo,
                    number,
                    expected_head_sha=pr.head_sha,
                    expected_head_branch=pr.head_branch,
                    expected_base_branch=cfg.base_branch,
                    body_marker=f"<!-- collegue-exec:{issue_number} -->",
                )
                if was_open:
                    closed_prs.append(number)
            except Exception as exc:  # noqa: BLE001 - garder toutes les refs
                close_errors.append(f"PR #{number}: {exc}")
        for number in sorted(issue_numbers):
            try:
                task_id = task_by_issue[number]
                issue = validated_issues[number]
                was_open = issue.state == "open"
                if was_open:
                    self.clients.issues.close_issue(
                        cfg.owner,
                        cfg.repo,
                        number,
                        expected_labels=[cfg.issue_label],
                        body_marker=f"<!-- collegue-task:{task_id} -->",
                    )
                    closed_issues.append(number)
            except Exception as exc:  # noqa: BLE001 - garder toutes les refs
                close_errors.append(f"issue #{number}: {exc}")
        if close_errors:
            raise NightlyE2EError("fermetures incomplètes, refs conservées: " + " | ".join(close_errors))

        remaining_prs = self.clients.prs.list_prs(cfg.owner, cfg.repo, state="open", limit=100, base=cfg.base_branch)
        if remaining_prs:
            raise NightlyE2EError("PR encore ouverte après fermeture ; refs conservées")

        # Phase 4 — refs prouvées seulement. Une issue corrélée ne prouve PAS le
        # SHA de sa branche déterministe : si le crash a précédé create_pr, on
        # conserve cette ref au lieu d'adopter son SHA courant comme attendu.
        branch_errors: list[str] = []
        candidate_branches = set(branch_heads) | set(manifest.head_shas)
        unproven_branches = {f"collegue/issue-{number}" for number in validated_issues} - candidate_branches
        for branch in sorted(unproven_branches):
            current = self._branch_sha_or_none(branch)
            if current is None:
                continue
            branch_errors.append(f"branche {branch}: SHA non prouvé (aucune PR/manifeste)")
        for branch in sorted(candidate_branches):
            try:
                current = self._branch_sha_or_none(branch)
                if current is None:
                    continue
                recorded = branch_heads.get(branch) or manifest.head_shas.get(branch)
                if recorded is not None and current != recorded:
                    raise NightlyE2EError(f"branche {branch} déplacée depuis la preuve")
                self.clients.branches.delete_branch(
                    cfg.owner,
                    cfg.repo,
                    branch,
                    default_branch=cfg.root_branch,
                    expected_sha=current,
                )
            except Exception as exc:  # noqa: BLE001 - conserver la base comme ancre
                branch_errors.append(f"branche {branch}: {exc}")
        if branch_errors:
            raise NightlyE2EError("suppression des heads incomplète, base conservée: " + " | ".join(branch_errors))

        if base_current is not None:
            if not manifest.base_created or manifest.base_sha != base_current:
                raise NightlyE2EError("branche base présente sans preuve de création/SHA ; conservation")
            self.clients.branches.delete_branch(
                cfg.owner,
                cfg.repo,
                cfg.base_branch,
                default_branch=cfg.root_branch,
                expected_sha=base_current,
            )

        final_root = self.clients.branches.get_branch_sha(cfg.owner, cfg.repo, cfg.root_branch)
        if final_root != manifest.root_sha or current_root != manifest.root_sha:
            raise NightlyE2EError("la branche par défaut de la fixture a bougé pendant le smoke")
        remaining_prs = self.clients.prs.list_prs(cfg.owner, cfg.repo, state="open", limit=100, base=cfg.base_branch)
        remaining_issues = (
            self.clients.issues.list_issues(
                cfg.owner,
                cfg.repo,
                state="open",
                limit=100,
                labels=cfg.issue_label,
            )
            if label_remote_present
            else []
        )
        if remaining_prs or remaining_issues:
            raise NightlyE2EError("des PR/issues du run sont encore ouvertes après cleanup")
        if owns_label and not manifest.label_deleted:
            # Relecture juste avant DELETE : un nom identique avec un autre
            # marqueur n'est jamais adopté. Une absence signifie soit que le POST
            # n'a jamais abouti, soit qu'un premier finalizer a déjà supprimé le
            # label avant de pouvoir checkpoint son succès.
            if self._owned_label_or_none() is not None:
                self.clients.labels.delete_label(
                    cfg.owner,
                    cfg.repo,
                    cfg.issue_label,
                    expected_name=cfg.issue_label,
                    expected_color=_NIGHTLY_LABEL_COLOR,
                    expected_description=self._label_owner_description(),
                )
            manifest.label_deleted = True
            _write_manifest(cfg.manifest_path, manifest)
        try:
            Path(cfg.manifest_path).unlink()
        except FileNotFoundError:
            pass
        return {"status": "clean", "closed_prs": closed_prs, "closed_issues": closed_issues}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m collegue.pilot.nightly_e2e")
    parser.add_argument("action", choices=("run", "cleanup"))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        runner = NightlyE2ERunner(NightlyConfig.from_env(action=args.action))
        payload = runner.run() if args.action == "run" else runner.cleanup()
    except Exception as exc:  # noqa: BLE001 - frontière CLI : erreur lisible + code 1
        print(f"nightly e2e refusé/échoué: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercé par le workflow réel
    raise SystemExit(main())
