"""Tests du générateur QA plan-time (SPEC + DAG → pytest persisté atomiquement)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import collegue.planner.acceptance_tests as at
from collegue.core.llm import LLMRole, UsageAccountingError
from collegue.planner import Spec, generate_acceptance_tests

VALID_SOURCE = """\
def test_contract_is_observable():
    actual = 2 + 2
    assert actual == 4
"""


def _task(task_id: int, *, title: str = "Tâche", acceptance: str = "Le résultat vaut 4", depends_on=None):
    return SimpleNamespace(
        id=task_id,
        title=title,
        acceptance=acceptance,
        depends_on=list(depends_on or []),
    )


def _spec():
    return Spec(title="Produit", summary="Résumé", acceptance_criteria=["Le produit fonctionne"])


class _Manager:
    def __init__(self):
        self.calls = []

    def set_acceptance_test_artifacts(self, project_id, artifacts):
        self.calls.append((project_id, artifacts))


class _Result:
    def __init__(self, text="", result=None):
        self.text = text
        self.result = result


class _Ctx:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def sample(self, **kwargs):
        self.calls.append(kwargs)
        return self.results.pop(0)


FIXED_NOW = datetime(2026, 7, 10, 12, 30, tzinfo=timezone.utc)


def test_text_and_source_normalization_are_deterministic():
    assert at.normalized_text("  a\r\nb  \r\n") == "a\nb\n"
    assert at.normalized_text("  ") == ""
    assert at.normalize_pytest_source(f"```python\r\n{VALID_SOURCE}```\r\n") == VALID_SOURCE


def test_source_normalization_accepts_one_leading_thought_envelope():
    raw = (
        "<thought>Analyse interne avec un exemple ambigu :\n"
        "```python\nassert True\n```\n"
        "</thought>"
        f"```python\r\n{VALID_SOURCE}```\r\n"
    )

    source = at.normalize_pytest_source(raw)

    assert source == VALID_SOURCE
    at.validate_pytest_source(source)


@pytest.mark.parametrize(
    "raw",
    [
        f"<thought>raisonnement non fermé```python\n{VALID_SOURCE}```",
        f"préface<thought>raisonnement</thought>```python\n{VALID_SOURCE}```",
        f"<thought>un</thought><thought>deux</thought>```python\n{VALID_SOURCE}```",
        f"<thought>raisonnement</thought>```python\n{VALID_SOURCE}```\n```python\n{VALID_SOURCE}```",
        f"<thought>raisonnement</thought>```python\n{VALID_SOURCE}```\nexplication finale",
    ],
)
def test_source_normalization_keeps_ambiguous_thought_responses_invalid(raw):
    with pytest.raises(ValueError, match="syntaxiquement"):
        at.validate_pytest_source(at.normalize_pytest_source(raw))


def test_sha256_text_hashes_exact_utf8_bytes():
    assert at.sha256_text("é\n") == hashlib.sha256("é\n".encode()).hexdigest()


def test_task_contract_is_canonical_across_task_and_dependency_order():
    a = _task(1)
    b = _task(2, depends_on=[1, 1])
    assert at.task_contract_sha256(9, b) == at.task_contract_sha256(9, b)
    assert at.task_contract_payload(9, b) == {
        "project_id": 9,
        "task_id": 2,
        "title": "Tâche\n",
        "acceptance": "Le résultat vaut 4\n",
        "depends_on": [1],
    }


@pytest.mark.parametrize(
    "source, message",
    [
        ("", "vide"),
        ("def helper():\n    assert True\n", "test_"),
        ("def test_x():\n    return True\n", "assert"),
        ("def test_x():\n    assert True\n", "constante"),
        ("def test_x():\n    assert 1 == 1\n", "constante"),
        (
            "def test_ok():\n    value = 1\n    assert value == 1\n\ndef test_missing():\n    return True\n",
            "test_missing.*assert",
        ),
        ("def test_x(:\n    assert True\n", "syntaxiquement"),
        ("import pytest\ndef test_x():\n    pytest.skip('x')\n    assert True\n", "skip"),
        ("import pytest\n@pytest.mark.skipif(True, reason='x')\ndef test_x():\n    assert True\n", "skipif"),
        ("import pytest\ndef test_x():\n    pytest.xfail('x')\n    assert True\n", "xfail"),
        ("import pytest\npytest.importorskip('x')\ndef test_x():\n    assert True\n", "importorskip"),
        (
            "from pathlib import Path\ndef test_x():\n    root = Path(__file__).parent\n    assert root.exists()\n",
            "__file__",
        ),
    ],
)
def test_static_validation_is_fail_closed(source, message):
    with pytest.raises(ValueError, match=message):
        at.validate_pytest_source(source)


def test_static_validation_accepts_module_function_and_test_class():
    at.validate_pytest_source(VALID_SOURCE)
    at.validate_pytest_source(
        "class TestAPI:\n    def test_ok(self):\n        status = 200\n        assert status < 500\n"
    )


def test_static_validation_rejects_oversize_source():
    source = "def test_x():\n    assert True\n" + ("#x\n" * 10)
    with pytest.raises(ValueError, match="trop grande"):
        at.validate_pytest_source(source, max_source_bytes=20)


@pytest.mark.asyncio
async def test_generate_uses_qa_role_normalizes_and_persists_expected_provenance():
    manager = _Manager()
    tasks = [_task(2, title="API", depends_on=[1]), _task(1, title="Socle")]
    ctx = _Ctx([_Result(text=f"```python\r\n{VALID_SOURCE}```"), _Result(text=VALID_SOURCE)])
    settings = SimpleNamespace(
        LLM_PROVIDER="gemini",
        LLM_MODEL="default",
        LLM_PROVIDER_QA="openai",
        LLM_MODEL_QA="qa-model",
        LLM_CALL_TIMEOUT=0,
    )

    artifacts = await generate_acceptance_tests(
        _spec(),
        tasks,
        ctx,
        manager=manager,
        project_id=7,
        settings_obj=settings,
        clock=lambda: FIXED_NOW,
    )

    assert list(artifacts) == [1, 2]  # sampling et résultat en ordre stable par id
    assert len(ctx.calls) == 2
    assert all(call["model_preferences"] == ["qa-model"] for call in ctx.calls)
    assert all(call["system_prompt"] == at.ACCEPTANCE_TEST_SYSTEM_PROMPT for call in ctx.calls)
    assert all(call["temperature"] == 0.1 for call in ctx.calls)
    assert manager.calls == [(7, artifacts)]

    first = artifacts[1]
    assert first["source"] == VALID_SOURCE
    provenance = first["provenance"]
    assert provenance == {
        "schema_version": 1,
        "generator": "collegue.planner.acceptance_tests",
        "role": "qa",
        "requested_provider": "openai",
        "requested_model": "qa-model",
        "prompt_sha256": provenance["prompt_sha256"],
        "spec_sha256": at.sha256_text(at.spec_text(_spec())),
        "criteria_sha256": at.sha256_text(at.criteria_text(tasks[1])),
        "contract_sha256": at.task_contract_sha256(7, tasks[1]),
        "runner": "pytest",
        "generated_at": "2026-07-10T12:30:00Z",
    }
    assert len(provenance["prompt_sha256"]) == 64


@pytest.mark.asyncio
async def test_prompt_contains_only_spec_and_plan_contract_metadata():
    manager = _Manager()
    task = _task(1, title="Endpoint factures", acceptance="POST /invoices renvoie 201")
    prompts = []

    async def sample(prompt, system_prompt):
        prompts.append((prompt, system_prompt))
        return VALID_SOURCE

    await generate_acceptance_tests(
        _spec(),
        [task],
        None,
        manager=manager,
        project_id=1,
        sample_fn=sample,
        clock=lambda: FIXED_NOW,
    )

    prompt, system = prompts[0]
    assert "# Produit" in prompt
    assert "Endpoint factures" in prompt
    assert "POST /invoices renvoie 201" in prompt
    assert '"depends_on":[]' in prompt
    assert "diff livré" not in prompt.lower()
    assert "workspace" not in prompt.lower()
    assert "indépendant du codeur" in system
    assert "Path.cwd()" in system
    assert "n'utilise jamais __file__" in system
    assert "sans lien direct" in system


@pytest.mark.asyncio
async def test_sample_fn_may_be_synchronous():
    manager = _Manager()
    result = await generate_acceptance_tests(
        _spec(),
        [_task(1)],
        None,
        manager=manager,
        project_id=1,
        sample_fn=lambda _prompt, _system: VALID_SOURCE,
        clock=lambda: FIXED_NOW,
    )
    assert result[1]["source"] == VALID_SOURCE


@pytest.mark.asyncio
async def test_sample_fn_cannot_bypass_active_hard_budget_or_persist_partial_artifacts():
    manager = _Manager()
    sampled = False

    def sample(_prompt, _system):
        nonlocal sampled
        sampled = True
        return VALID_SOURCE

    with pytest.raises(UsageAccountingError, match="sans preuve d'usage"):
        await generate_acceptance_tests(
            _spec(),
            [_task(1)],
            None,
            manager=manager,
            project_id=1,
            settings_obj=SimpleNamespace(
                LLM_PROVIDER="openai",
                LLM_MODEL="gpt-5.4",
                MAX_TOKENS_BUDGET=100,
                MAX_COST_USD=0,
            ),
            sample_fn=sample,
        )

    assert sampled is False
    assert manager.calls == []


@pytest.mark.asyncio
async def test_result_field_is_fallback_when_ctx_text_is_empty():
    manager = _Manager()
    ctx = _Ctx([_Result(result=VALID_SOURCE)])
    artifacts = await generate_acceptance_tests(
        _spec(), [_task(1)], ctx, manager=manager, project_id=1, clock=lambda: FIXED_NOW
    )
    assert artifacts[1]["source"] == VALID_SOURCE


@pytest.mark.asyncio
async def test_all_sources_are_validated_before_the_single_persistence_call():
    manager = _Manager()
    calls = 0

    async def sample(_prompt, _system):
        nonlocal calls
        calls += 1
        return VALID_SOURCE if calls == 1 else "def test_without_assert():\n    return True\n"

    with pytest.raises(ValueError, match="assert"):
        await generate_acceptance_tests(
            _spec(),
            [_task(1), _task(2)],
            None,
            manager=manager,
            project_id=1,
            sample_fn=sample,
            clock=lambda: FIXED_NOW,
        )
    assert calls == 2
    assert manager.calls == []


@pytest.mark.asyncio
async def test_sampling_error_propagates_without_persistence():
    manager = _Manager()

    async def sample(_prompt, _system):
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError, match="provider down"):
        await generate_acceptance_tests(
            _spec(),
            [_task(1)],
            None,
            manager=manager,
            project_id=1,
            sample_fn=sample,
            clock=lambda: FIXED_NOW,
        )
    assert manager.calls == []


@pytest.mark.asyncio
async def test_invalid_dag_is_rejected_before_sampling():
    manager = _Manager()
    sampled = False

    async def sample(_prompt, _system):
        nonlocal sampled
        sampled = True
        return VALID_SOURCE

    with pytest.raises(ValueError, match="dupliqués"):
        await generate_acceptance_tests(
            _spec(),
            [_task(1), _task(1)],
            None,
            manager=manager,
            project_id=1,
            sample_fn=sample,
        )
    assert sampled is False
    assert manager.calls == []


@pytest.mark.asyncio
async def test_missing_qa_model_fails_before_sampling():
    manager = _Manager()
    sampled = False

    async def sample(_prompt, _system):
        nonlocal sampled
        sampled = True
        return VALID_SOURCE

    with pytest.raises(ValueError, match="modèle/provider QA"):
        await generate_acceptance_tests(
            _spec(),
            [_task(1)],
            None,
            manager=manager,
            project_id=1,
            settings_obj=SimpleNamespace(LLM_PROVIDER="", LLM_MODEL=""),
            sample_fn=sample,
        )
    assert sampled is False
    assert manager.calls == []


@pytest.mark.asyncio
async def test_empty_dag_and_missing_manager_contract_fail_closed():
    with pytest.raises(ValueError, match="DAG vide"):
        await generate_acceptance_tests(_spec(), [], None, manager=object(), project_id=1)

    with pytest.raises(AttributeError, match="set_acceptance_test_artifacts"):
        await generate_acceptance_tests(
            _spec(),
            [_task(1)],
            None,
            manager=object(),
            project_id=1,
            sample_fn=lambda _prompt, _system: VALID_SOURCE,
        )


def test_public_export_is_available():
    assert generate_acceptance_tests is at.generate_acceptance_tests
    assert LLMRole.QA.value == "qa"
