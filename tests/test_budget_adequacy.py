"""Juge d'adéquation et inventaire des appels LLM du cycle projet (vague 2, passe 10).

La FACTORY de production (``_gate_options`` → ``_build_adequacy_checker``) est exercée, pas un ``sample_fn`` injecté :
le juge doit passer par le transport gardé (réservation avant émission, règlement, rôle reviewer, scope du projet).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from collegue.core.llm.budget_guard import bind_budget
from collegue.core.llm.sampling_ctx import LocalSamplingContext
from collegue.executor.agent import IssueSpec
from collegue.monitoring.metrics import MetricsCollector
from collegue.pilot.runtime import _gate_options
from collegue.state import BudgetRefused, ProjectStateManager
from collegue.state.models import BudgetReservation

DIFF = "diff --git a/x.py b/x.py\n+++ b/x.py\n+print(1)\n diff --git a/tests/test_x.py b/tests/test_x.py\n+++ b/tests/test_x.py\n+assert 1\n"
ISSUE = IssueSpec(number=1, title="t", body="b", acceptance_criteria=["c1"])
VERDICT = json.dumps({"implemented": True, "tests_assert_criteria": True, "justification": "ok"})


@pytest.fixture(autouse=True)
def _isolated_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(MetricsCollector, "_PERSIST_DIR", tmp_path / "monitoring")


def _settings(**extra):
    base = dict(
        LLM_PROVIDER="openai",
        LLM_MODEL="gpt-4o-mini",
        LLM_MODEL_REVIEWER="gpt-4o-mini",
        LLM_PRICE_PROMPT_PER_1M=1.0,
        LLM_PRICE_COMPLETION_PER_1M=2.0,
        MAX_TOKENS=128,
        MAX_COST_USD=1.0,
        MAX_TOKENS_BUDGET=100_000,
        LLM_CALL_TIMEOUT=2,
        GATE_ADEQUACY=True,
    )
    base.update(extra)
    return SimpleNamespace(**base)


class Provider:
    def __init__(self, manager, key):
        self.manager, self.key, self.calls, self.during = manager, key, 0, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def with_options(self, **_):
        return self

    async def _create(self, *, model, messages, **_):
        self.calls += 1
        self.during.append(self.manager.budget_ledger.snapshot(self.key).reserved_tokens)
        return SimpleNamespace(
            model=model,
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10),
            choices=[SimpleNamespace(message=SimpleNamespace(content=VERDICT))],
        )


def _env(tmp_path, *, cap_usd=1.0):
    manager = ProjectStateManager.from_url(f"sqlite:///{tmp_path / 's.db'}", create=True)
    pid = manager.create_project(name="Audit")
    key = manager.budget_ledger.scope_for_project(pid, max_cost_usd=cap_usd, max_tokens=100_000).scope_key
    return manager, key


async def test_the_production_adequacy_judge_is_reserved_settled_and_attributed_to_the_reviewer(tmp_path, monkeypatch):
    from collegue.resources.llm import providers

    outside = []

    async def unguarded(*args, **kwargs):  # l'ancien chemin direct : ne doit JAMAIS être emprunté
        outside.append(1)
        return SimpleNamespace(text=VERDICT)

    monkeypatch.setattr(providers, "generate_text", unguarded)
    manager, key = _env(tmp_path)
    provider = Provider(manager, key)
    settings = _settings()
    checker = _gate_options(settings)["adequacy_checker"]  # la factory réelle
    ctx = LocalSamplingContext(default_model="gpt-4o-mini", client=provider, max_retries=0)

    with bind_budget(manager.budget_ledger, key, settings=settings):
        outcome = await checker.check(DIFF, ISSUE, ctx)

    assert outcome.implemented is True and outside == []
    assert provider.calls == 2 and all(
        reserved > 0 for reserved in provider.during
    )  # réservation AVANT chaque émission
    snap = manager.budget_ledger.snapshot(key)
    assert snap.consumed_tokens == 220 and snap.reserved_tokens == 0
    with manager.session() as session:
        assert {r.role for r in session.query(BudgetReservation).all()} == {"reviewer"}
    assert (
        ProjectStateManager.from_url(f"sqlite:///{tmp_path / 's.db'}").budget_ledger.snapshot(key).consumed_tokens
        == 220
    )


async def test_the_production_adequacy_judge_emits_nothing_when_the_budget_is_insufficient(tmp_path):
    manager, key = _env(tmp_path, cap_usd=0.000001)
    provider = Provider(manager, key)
    settings = _settings(MAX_COST_USD=0.000001)
    checker = _gate_options(settings)["adequacy_checker"]
    ctx = LocalSamplingContext(default_model="gpt-4o-mini", client=provider, max_retries=0)

    with bind_budget(manager.budget_ledger, key, settings=settings):
        with pytest.raises(BudgetRefused):
            await checker.check(DIFF, ISSUE, ctx)

    assert provider.calls == 0 and manager.budget_ledger.snapshot(key).reserved_tokens == 0


async def test_the_judge_without_a_sampling_context_fails_closed_instead_of_calling_a_provider_directly():
    checker = _gate_options(_settings())["adequacy_checker"]
    with pytest.raises(RuntimeError, match="ctx de sampling absent"):
        await checker.check(DIFF, ISSUE, None)


# --- inventaire : aucun appel fournisseur direct dans le cycle projet -------------------------------------------

_CYCLE_PACKAGES = ("pilot", "planner", "executor", "improve", "sandbox", "state", "core/llm")
_DIRECT_CALL = re.compile(
    r"generate_text|resources\.llm|AsyncOpenAI|chat\.completions\.create|genai\.Client|google\.genai"
)
# Transports gardés (réservation avant émission) ou hors cycle projet : la liste est EXPLICITE.
_GUARDED_OR_OUTSIDE = {
    "core/llm/sampling_ctx.py",  # transport HTTP gardé + sampler d'abonnement gardé
    "core/llm/sampling_handler.py",  # handler MCP : gardé quand un scope est lié, sinon hors garantie (documenté)
}


def test_no_direct_provider_call_exists_in_the_project_cycle_outside_the_guarded_transports():
    root = Path(__file__).resolve().parents[1] / "collegue"
    offenders = []
    for package in _CYCLE_PACKAGES:
        for path in sorted((root / package).rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            if relative in _GUARDED_OR_OUTSIDE:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if _DIRECT_CALL.search(code):
                    offenders.append(f"{relative}:{number}: {line.strip()}")
    assert offenders == [], "appel fournisseur direct HORS du transport gardé :\n" + "\n".join(offenders)
