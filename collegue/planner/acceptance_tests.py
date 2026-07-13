"""Génération plan-time de tests d'acceptation pytest indépendants du codeur.

Ce module ne connaît ni workspace, ni diff, ni exécuteur. Il transforme le SPEC
et le DAG déjà planifié en **une source pytest par tâche**, valide toutes les
sources en mémoire, puis confie leur persistance atomique au state manager.

Le manager est volontairement utilisé par duck typing : la migration d'état qui
porte les artefacts fournit ``set_acceptance_test_artifacts(project_id,
artifacts)``. Le format transmis est ``{task_id: {"source": str,
"provenance": dict}}``.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

from collegue.core.llm import LLMRole, model_preferences_for_role, resolve_role
from collegue.core.llm.client import UsageAccountingError, accounted_sample

PROVENANCE_SCHEMA_VERSION = 1
GENERATOR_NAME = "collegue.planner.acceptance_tests"
RUNNER_NAME = "pytest"
MAX_SOURCE_BYTES = 64 * 1024
DEFAULT_MAX_TOKENS = 8192

ACCEPTANCE_TEST_SYSTEM_PROMPT = """Tu es un ingénieur QA indépendant du codeur.
À partir du SPEC et du contrat de tâche fournis, écris un module pytest exécutable
qui vérifie objectivement le critère d'acceptation de CETTE tâche.

Contraintes impératives :
- réponds uniquement avec la source Python du module (un fence ```python est toléré) ;
- définis au moins une fonction ou méthode collectable nommée test_* ;
- chaque test contient au moins une instruction assert qui vérifie un résultat observable ;
- vérifie uniquement le critère d'acceptation de la tâche : n'ajoute aucun test
  d'intégrité, de dépendances ou de non-régression sans lien direct avec ce critère ;
- le runner crée le module sous un nom aléatoire dans /tmp puis l'exécute avec
  /workspace comme répertoire courant : utilise Path.cwd() pour trouver le projet
  et n'utilise jamais __file__ pour en déduire la racine ;
- n'utilise jamais skip, skipif, xfail ou importorskip ;
- n'utilise aucun conftest, plugin pytest ou configuration pytest du projet ;
- ne remplace pas la vérification par une opinion, un commentaire ou un placeholder.
"""

_FENCE_RE = re.compile(r"\A```(?:python|py)?[ \t]*\n(?P<code>.*)\n```[ \t]*\Z", re.DOTALL | re.IGNORECASE)
_LEADING_THOUGHT_RE = re.compile(r"\A<thought>.*?</thought>", re.DOTALL | re.IGNORECASE)
_FORBIDDEN_PYTEST_CONTROLS = frozenset({"skip", "skipif", "xfail", "importorskip"})


def normalize_plan_text(value: Any) -> str:
    """Normalise un texte pour hashing : LF, bords retirés, un LF final si non vide."""

    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return f"{text}\n" if text else ""


# Alias explicite conservé pour les consommateurs qui préfèrent un adjectif.
normalized_text = normalize_plan_text


def sha256_text(value: str) -> str:
    """SHA-256 hexadécimal du texte **tel quel**, encodé en UTF-8."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _task_id(task: Any) -> int:
    value = _field(task, "id")
    if isinstance(value, bool):
        raise ValueError("id de tâche invalide (booléen)")
    try:
        task_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"id de tâche invalide : {value!r}") from exc
    if task_id < 1:
        raise ValueError(f"id de tâche invalide : {task_id}")
    return task_id


def _dependency_ids(task: Any) -> list[int]:
    dependencies = _field(task, "depends_on", None) or []
    if isinstance(dependencies, (str, bytes)):
        raise ValueError("depends_on doit être une liste d'ids")
    try:
        values = [int(value) for value in dependencies]
    except (TypeError, ValueError) as exc:
        raise ValueError("depends_on contient un id invalide") from exc
    if any(value < 1 for value in values):
        raise ValueError("depends_on contient un id invalide")
    return sorted(set(values))


def _task_payload(task: Any) -> Dict[str, Any]:
    return {
        "task_id": _task_id(task),
        "title": normalize_plan_text(_field(task, "title", "")),
        "acceptance": normalize_plan_text(_field(task, "acceptance", "")),
        "depends_on": _dependency_ids(task),
    }


def task_contract_payload(project_id: int, task: Any) -> Dict[str, Any]:
    """Contrat canonique exact couvert par ``contract_sha256``.

    La forme est partagée avec le checker runtime : ne pas y ajouter de champ
    sans incrémenter le schéma de provenance.
    """

    if isinstance(project_id, bool):
        raise ValueError("project_id invalide (booléen)")
    try:
        canonical_project_id = int(project_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"project_id invalide : {project_id!r}") from exc
    if canonical_project_id < 1:
        raise ValueError(f"project_id invalide : {canonical_project_id}")
    payload = _task_payload(task)
    return {
        "project_id": canonical_project_id,
        **payload,
    }


# Nom court pratique pour le checker, même contrat que task_contract_payload.
contract_payload = task_contract_payload


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def task_contract_sha256(project_id: int, task: Any) -> str:
    """Empreinte du payload renvoyé par :func:`task_contract_payload`."""

    return sha256_text(_canonical_json(task_contract_payload(project_id, task)))


def spec_text(spec: Any) -> str:
    """Représentation canonique du SPEC utilisée dans le prompt et sa provenance."""

    rendered = spec.to_markdown() if hasattr(spec, "to_markdown") else str(spec)
    return normalize_plan_text(rendered)


def criteria_text(task: Any) -> str:
    """Critère d'acceptation canonique d'une tâche."""

    return normalize_plan_text(_field(task, "acceptance", ""))


def normalize_pytest_source(raw: Any) -> str:
    """Isole la réponse finale, retire son fence puis normalise le source.

    Gemini peut exposer une enveloppe ``<thought>…</thought>`` avant sa réponse
    finale. On ne la retire que lorsqu'elle est complète et strictement en tête ;
    le reste doit encore être soit un module Python pur, soit un unique fence
    externe couvrant toute la réponse. Une enveloppe incomplète, du texte après
    le fence ou plusieurs réponses restent donc rejetés par la validation AST.
    """

    text = "" if raw is None else str(raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    thought = _LEADING_THOUGHT_RE.match(text)
    if thought is not None:
        text = text[thought.end() :].strip()
    match = _FENCE_RE.fullmatch(text)
    if match is not None:
        text = match.group("code")
    return normalize_plan_text(text)


def _dotted_name(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    current: Optional[ast.AST] = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return tuple(reversed(parts))


def _collected_tests(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    tests: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            tests.append(node)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            tests.extend(
                child
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test_")
            )
    return tests


def _observable_assert(node: ast.Assert) -> bool:
    """Écarte les pseudo-oracles constants (`assert True`, `assert 1 == 1`).

    Une assertion crédible doit au minimum dépendre d'une valeur nommée, d'un
    appel, d'un attribut ou d'un accès indexé. Ce n'est pas une preuve sémantique
    complète, mais cela bloque les réponses LLM manifestement tautologiques avant
    qu'elles soient scellées comme contrat.
    """
    observable_nodes = (ast.Name, ast.Call, ast.Attribute, ast.Subscript)
    return any(isinstance(child, observable_nodes) for child in ast.walk(node.test))


def validate_pytest_source(source: str, *, max_source_bytes: int = MAX_SOURCE_BYTES) -> None:
    """Valide statiquement un artefact QA ; toute ambiguïté échoue fermée."""

    if not source:
        raise ValueError("source pytest vide")
    if "\x00" in source:
        raise ValueError("source pytest invalide : octet NUL")
    size = len(source.encode("utf-8"))
    if size > max_source_bytes:
        raise ValueError(f"source pytest trop grande ({size} octets > {max_source_bytes})")
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise ValueError(f"source pytest syntaxiquement invalide : {exc.msg}") from exc

    tests = _collected_tests(tree)
    if not tests:
        raise ValueError("source pytest invalide : aucune fonction test_* collectable")

    for node in ast.walk(tree):
        # L'oracle est matérialisé sous un chemin aléatoire dans /tmp au runtime.
        # En déduire la racine du projet via ``__file__`` produit donc un faux
        # négatif même lorsque le livrable est conforme (nightly réel #598).
        if isinstance(node, ast.Name) and node.id == "__file__":
            raise ValueError(
                "source pytest invalide : __file__ est interdit ; utiliser Path.cwd() pour localiser le workspace"
            )
        if isinstance(node, (ast.Attribute, ast.Name)):
            parts = _dotted_name(node)
            if any(part.lower() in _FORBIDDEN_PYTEST_CONTROLS for part in parts):
                forbidden = next(part for part in parts if part.lower() in _FORBIDDEN_PYTEST_CONTROLS)
                raise ValueError(f"source pytest invalide : {forbidden} est interdit")

    for test in tests:
        assertions = [node for node in ast.walk(test) if isinstance(node, ast.Assert)]
        if not assertions:
            raise ValueError(f"source pytest invalide : test {test.name} sans instruction assert")
        if not any(_observable_assert(node) for node in assertions):
            raise ValueError(f"source pytest invalide : test {test.name} ne contient qu'une assertion constante")


def acceptance_prompt(spec: Any, task: Any, tasks: Iterable[Any], project_id: int) -> str:
    """Prompt utilisateur déterministe : SPEC + contrat courant + métadonnées du DAG."""

    task_list = list(tasks)
    contract = task_contract_payload(project_id, task)
    dag = sorted((task_contract_payload(project_id, item) for item in task_list), key=lambda item: item["task_id"])
    return normalize_plan_text(
        "## SPEC proposé\n"
        f"{spec_text(spec)}\n"
        "## Contrat de la tâche (JSON canonique)\n"
        f"{_canonical_json(contract)}\n\n"
        "## DAG complet (JSON canonique)\n"
        f"{_canonical_json(dag)}\n\n"
        "Écris maintenant le module pytest de cette tâche."
    )


# Alias descriptif pour les appelants existants et la lisibilité du générateur.
build_acceptance_prompt = acceptance_prompt


def prompt_sha256(prompt: str) -> str:
    """Empreinte du prompt complet (système + utilisateur) réellement envoyé."""

    payload = {"system": normalize_plan_text(ACCEPTANCE_TEST_SYSTEM_PROMPT), "user": prompt}
    return sha256_text(_canonical_json(payload))


def acceptance_prompt_sha256(spec: Any, task: Any, tasks: Iterable[Any], project_id: int) -> str:
    """Recalcule l'empreinte de prompt depuis l'état durable du plan."""

    return prompt_sha256(acceptance_prompt(spec, task, tasks, project_id))


def _generated_at(clock: Callable[[], datetime]) -> str:
    value = clock()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


async def _sample_source(
    prompt: str,
    ctx: Any,
    *,
    sample_fn: Optional[Callable[[str, str], Any]],
    settings_obj: Optional[object],
    max_tokens: int,
) -> str:
    if sample_fn is not None:
        if (
            int(getattr(settings_obj, "MAX_TOKENS_BUDGET", 0) or 0) > 0
            or float(getattr(settings_obj, "MAX_COST_USD", 0) or 0) > 0
        ):
            raise UsageAccountingError(
                "sample_fn QA injecté sans preuve d'usage : interdit lorsqu'un budget dur est actif."
            )
        result = sample_fn(prompt, ACCEPTANCE_TEST_SYSTEM_PROMPT)
        if inspect.isawaitable(result):
            result = await result
        return str(result or "")
    if ctx is None:
        raise RuntimeError("ctx de sampling absent pour la génération des tests d'acceptation")
    kwargs: Dict[str, Any] = {
        "messages": prompt,
        "system_prompt": ACCEPTANCE_TEST_SYSTEM_PROMPT,
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    preferences = model_preferences_for_role(LLMRole.QA, settings_obj)
    if preferences:
        kwargs["model_preferences"] = preferences
    result = await accounted_sample(
        ctx,
        role=LLMRole.QA,
        operation="planner.acceptance",
        settings_obj=settings_obj,
        **kwargs,
    )
    text = getattr(result, "text", "") or ""
    if not text and isinstance(getattr(result, "result", None), str):
        text = result.result
    return str(text or "")


async def generate_acceptance_tests(
    spec: Any,
    tasks: Iterable[Any],
    ctx: Any,
    *,
    manager: Any,
    project_id: int,
    settings_obj: Optional[object] = None,
    sample_fn: Optional[Callable[[str, str], Any]] = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_source_bytes: int = MAX_SOURCE_BYTES,
    clock: Optional[Callable[[], datetime]] = None,
) -> Dict[int, Dict[str, Any]]:
    """Génère, valide puis persiste atomiquement une source pytest par tâche.

    Aucun artefact n'est envoyé au manager avant que **toutes** les sources aient
    passé la validation. Toute exception de sampling, parsing ou validation
    remonte à l'appelant (fail-closed).
    """

    task_list = list(tasks)
    if not task_list:
        raise ValueError("impossible de générer les tests d'acceptation : DAG vide")
    # Valide l'ensemble et les ids avant de dépenser le moindre token.
    ids = [_task_id(task) for task in task_list]
    if len(ids) != len(set(ids)):
        raise ValueError("DAG invalide : ids de tâches dupliqués")
    for task in task_list:
        task_id = _task_id(task)
        if not criteria_text(task):
            raise ValueError(f"tâche {task_id} sans critère d'acceptation")
        dependencies = _dependency_ids(task)
        if task_id in dependencies:
            raise ValueError(f"tâche {task_id} dépend d'elle-même")
        missing = sorted(set(dependencies) - set(ids))
        if missing:
            raise ValueError(f"tâche {task_id}: dépendances absentes du DAG : {missing}")
        task_contract_payload(project_id, task)

    provider, model = resolve_role(LLMRole.QA, settings_obj)
    provider = str(provider or "").strip()
    model = str(model or "").strip()
    if not provider or not model:
        raise ValueError("modèle/provider QA non configuré : provenance plan-time impossible")
    timestamp = _generated_at(clock or (lambda: datetime.now(timezone.utc)))
    canonical_spec = spec_text(spec)
    artifacts: Dict[int, Dict[str, Any]] = {}

    for task in sorted(task_list, key=_task_id):
        task_id = _task_id(task)
        prompt = acceptance_prompt(spec, task, task_list, project_id)
        source = normalize_pytest_source(
            await _sample_source(
                prompt,
                ctx,
                sample_fn=sample_fn,
                settings_obj=settings_obj,
                max_tokens=max_tokens,
            )
        )
        validate_pytest_source(source, max_source_bytes=max_source_bytes)
        artifacts[task_id] = {
            "source": source,
            "provenance": {
                "schema_version": PROVENANCE_SCHEMA_VERSION,
                "generator": GENERATOR_NAME,
                "role": LLMRole.QA.value,
                "requested_provider": provider,
                "requested_model": model,
                "prompt_sha256": prompt_sha256(prompt),
                "spec_sha256": sha256_text(canonical_spec),
                "criteria_sha256": sha256_text(criteria_text(task)),
                "contract_sha256": task_contract_sha256(project_id, task),
                "runner": RUNNER_NAME,
                "generated_at": timestamp,
            },
        }

    setter = getattr(manager, "set_acceptance_test_artifacts", None)
    if not callable(setter):
        raise AttributeError("le state manager n'expose pas set_acceptance_test_artifacts(project_id, artifacts)")
    setter(project_id, artifacts)
    return artifacts


__all__ = [
    "ACCEPTANCE_TEST_SYSTEM_PROMPT",
    "DEFAULT_MAX_TOKENS",
    "GENERATOR_NAME",
    "MAX_SOURCE_BYTES",
    "PROVENANCE_SCHEMA_VERSION",
    "RUNNER_NAME",
    "acceptance_prompt",
    "acceptance_prompt_sha256",
    "build_acceptance_prompt",
    "contract_payload",
    "criteria_text",
    "generate_acceptance_tests",
    "normalize_plan_text",
    "normalize_pytest_source",
    "normalized_text",
    "prompt_sha256",
    "sha256_text",
    "spec_text",
    "task_contract_payload",
    "task_contract_sha256",
    "validate_pytest_source",
]
