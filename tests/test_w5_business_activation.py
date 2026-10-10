"""Activation de la campagne : scope durable ``planning:cycle:<id>`` (2 USD / 250 000 tokens) puis qualification des deux modèles.

La qualification se fait AVANT toute création distante et toute planification, sur le MÊME scope que le brouillon public
(``--cycle-id``) ; sa dépense est lisible par scope même si aucun projet n'aboutit. Registre SQLite réel ; seule la qualification
est un double, qui reproduit le contrat FINAL d'A (``await qualify_models(settings, ledger, scope_key) -> QualificationReport``,
mêmes noms de champs) — jamais un mapping « accepté » ni une estimation de remplacement. Le raccord au vrai runtime d'A est établi
séparément (rapport de B : composition réelle avec faux fournisseur amont).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import pytest
from test_w4_business_launch import ENV, FakeAdapter

from collegue.pilot import w4_business as business
from collegue.pilot import w5_business as w5
from collegue.pilot.w4_business import CampaignReport, IncompleteValidation
from collegue.state import ProjectStateManager
from collegue.state.budget_ledger import BudgetRefused

CAMPAIGN = "w5-camp-001"
SCOPE = f"planning:cycle:{CAMPAIGN}"
BOTH = [business.MODEL_PRIMARY, business.MODEL_CODER_FALLBACK]
NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


# Doubles de test des dataclasses d'A (``collegue.broker.service``) : mêmes noms de champs, même ``to_dict``.
@dataclass(frozen=True)
class CapabilityResult:
    capability: str
    ok: bool
    detail: str
    request_id: str
    tokens: int = 0


@dataclass(frozen=True)
class ModelQualification:
    model: str
    role: str
    capabilities: Tuple[CapabilityResult, ...]
    ok: bool


@dataclass(frozen=True)
class QualificationReport:
    scope_key: str
    ok: bool
    reason: str
    models: Tuple[ModelQualification, ...]
    deadline_at: Optional[datetime]
    consumed_tokens: int
    blocked: bool
    destination: str

    def to_dict(self) -> dict:
        return {
            "scope_key": self.scope_key,
            "ok": self.ok,
            "reason": self.reason,
            "destination": self.destination,
            "deadline_at": None if self.deadline_at is None else self.deadline_at.isoformat(),
            "consumed_tokens": self.consumed_tokens,
            "blocked": self.blocked,
            "models": [
                {
                    "model": m.model,
                    "role": m.role,
                    "ok": m.ok,
                    "capabilities": [
                        {
                            "capability": c.capability,
                            "ok": c.ok,
                            "detail": c.detail,
                            "request_id": c.request_id,
                            "tokens": c.tokens,
                        }
                        for c in m.capabilities
                    ],
                }
                for m in self.models
            ],
        }


def model_result(model, role, *, scope=SCOPE, ok=True, **changes):
    capabilities = tuple(
        CapabilityResult(name, ok, "reçu" if ok else "refusé", f"qualify:{scope}:{model}:{name}", 120)
        for name in ("text", "json", "tools")
    )
    return ModelQualification(model=model, role=role, capabilities=capabilities, ok=ok, **changes)


def good_report(**changes):
    values = dict(
        scope_key=SCOPE,
        ok=True,
        reason="",
        models=(model_result(business.MODEL_PRIMARY, "default"), model_result(business.MODEL_CODER_FALLBACK, "coder")),
        deadline_at=NOW + timedelta(seconds=850),
        consumed_tokens=720,
        blocked=False,
        destination="generativelanguage.googleapis.com/v1beta (natif)",
    )
    values.update(changes)
    return QualificationReport(**values)


@pytest.fixture
def env(tmp_path):
    values = {**business.campaign_environment("act", str(tmp_path)), "LLM_TRANSPORT": "budget_broker"}
    ProjectStateManager.from_url(
        values["STATE_DATABASE_URL"], create=True
    )  # le workflow migre le registre vierge avant tout
    return values


def accepted(**changes):
    return lambda settings, ledger, scope_key: good_report(**changes)


def activate(env, qualify, report=None, **kwargs):
    report = report or CampaignReport("campaign", "unit")
    w5.activate_budget(env, CAMPAIGN, report, qualify=qualify, now=lambda: NOW, **kwargs)
    return report


def test_the_durable_scope_is_opened_strict_with_the_global_caps_before_the_qualification_runs(env):
    seen = {}

    def qualify(settings, ledger, scope_key):
        snapshot = ledger.snapshot(scope_key)  # le scope EXISTE déjà au moment de la qualification
        seen.update(scope=scope_key, strict=snapshot.strict, usd=snapshot.cap_micro_usd, tokens=snapshot.cap_tokens)
        return good_report()

    report = activate(env, qualify)

    assert seen == {"scope": SCOPE, "strict": True, "usd": 2_000_000, "tokens": 250_000}
    assert report.facts["launch"]["scope_key"] == SCOPE
    assert [m["model"] for m in report.facts["qualification"]["models"]] == BOTH


def test_the_qualification_spend_is_readable_by_scope_even_when_no_project_exists(env):
    def qualify(settings, ledger, scope_key):
        reservation = ledger.reserve(scope_key, usd=0.01, tokens=900)
        ledger.commit(reservation.reservation_id, usd=0.004, tokens=640)
        return good_report(consumed_tokens=640)

    report = activate(env, qualify)

    counters = business.registry_reader(env)(report.facts["launch"])  # contexte SANS project_id
    assert counters["scope"] == SCOPE and (counters["consumed_tokens"], counters["consumed_micro_usd"]) == (640, 4_000)
    assert counters["reserved_tokens"] == 0 and counters["strict"] is True
    assert counters["cap_usd"] == 2 and counters["cap_tokens"] == 250_000
    assert report.facts["qualification_registry"]["consumed_tokens"] == 640, "capturé dès les canaris"


def test_the_project_scope_takes_over_from_the_cycle_scope_once_the_project_exists(env):
    report = activate(env, accepted())
    manager = ProjectStateManager.from_url(env["STATE_DATABASE_URL"])
    project_id = manager.create_project(name="liaison", spec="x")
    manager.budget_ledger.scope_for_project(project_id, max_cost_usd=2, max_tokens=250_000, strict=True)
    context = {**report.facts["launch"], "project_id": project_id}

    counters = business.registry_reader(env)(context)

    assert counters["scope"] is not None and counters["strict"] is True


def test_the_final_contract_of_lot_a_is_consumed_and_every_capability_detail_is_kept(env):
    report = activate(env, accepted())

    facts = report.facts["qualification"]
    assert facts["ok"] is True and facts["scope_key"] == SCOPE and facts["consumed_tokens"] == 720
    assert facts["destination"].startswith("generativelanguage.googleapis.com")
    capabilities = {(m["model"], c["capability"]): c for m in facts["models"] for c in m["capabilities"]}
    assert sorted(capabilities) == sorted((m, c) for m in BOTH for c in ("text", "json", "tools"))
    assert (
        capabilities[(business.MODEL_PRIMARY, "tools")]["request_id"]
        == f"qualify:{SCOPE}:{business.MODEL_PRIMARY}:tools"
    )
    assert facts["remaining_seconds"] == 850.0, "échéance ABSOLUE durable − maintenant"


def broken(model, role, **changes):
    return replace(model_result(model, role), **changes)


REFUSALS = {
    "not-ok": (dict(ok=False, reason="gemma-4-26b-a4b-it/tools: appel d'outil absent"), "qualification non réussie"),
    "wrong-scope": (dict(scope_key="planning:cycle:autre"), "scope qualifié"),
    "no-consumption": (dict(consumed_tokens=0), "aucune consommation établie"),
    "foreign-destination": (dict(destination="api.openai.com"), "destination non native Google"),
    "single-model": (dict(models=(model_result(business.MODEL_PRIMARY, "default"),)), "identités qualifiées"),
    "third-model": (
        dict(models=good_report().models + (model_result("gemini-2.5-flash", "default"),)),
        "identités qualifiées",
    ),
    "wrong-role": (
        dict(
            models=(model_result(business.MODEL_PRIMARY, "coder"), model_result(business.MODEL_CODER_FALLBACK, "coder"))
        ),
        r"rôle 'coder' \(attendu 'default'\)",
    ),
    "model-not-ok": (
        dict(
            models=(
                model_result(business.MODEL_PRIMARY, "default"),
                model_result(business.MODEL_CODER_FALLBACK, "coder", ok=False),
            )
        ),
        "gemma-4-26b-a4b-it non qualifié",
    ),
    "capability-missing": (
        dict(
            models=(
                replace(
                    model_result(business.MODEL_PRIMARY, "default"),
                    capabilities=model_result(business.MODEL_PRIMARY, "default").capabilities[:2],
                ),
                model_result(business.MODEL_CODER_FALLBACK, "coder"),
            )
        ),
        "capacités",
    ),
    "foreign-request-identity": (
        dict(
            models=(
                replace(model_result(business.MODEL_PRIMARY, "default", scope="autre"), ok=True),
                model_result(business.MODEL_CODER_FALLBACK, "coder"),
            )
        ),
        "identité durable inattendue",
    ),
    "deadline-absent": (dict(deadline_at=None), "échéance absolue durable absente"),
    "deadline-naive": (dict(deadline_at=datetime(2026, 10, 9, 12, 14)), "sans fuseau"),
    "deadline-passed": (dict(deadline_at=NOW - timedelta(seconds=1)), "déjà dépassée"),
    "deadline-beyond-the-window": (dict(deadline_at=NOW + timedelta(seconds=3600)), "au-delà de la fenêtre"),
}


@pytest.mark.parametrize("name", sorted(REFUSALS))
def test_a_qualification_that_does_not_establish_the_whole_contract_stops_the_campaign_and_keeps_the_details(env, name):
    changes, needle = REFUSALS[name]
    report = CampaignReport("campaign", "unit")

    with pytest.raises(IncompleteValidation, match=needle):
        activate(env, accepted(**changes), report)

    assert report.facts["qualification"]["scope_key"], "les détails de chaque capacité sont conservés AUSSI en refus"
    assert report.facts["qualification"]["models"], report.facts["qualification"]


def test_a_blocked_scope_is_a_budget_stop_and_the_details_are_kept(env):
    report = CampaignReport("campaign", "unit")
    with pytest.raises(business.BudgetStop, match="scope bloqué"):
        activate(env, accepted(blocked=True), report)
    assert report.facts["qualification"]["blocked"] is True


def test_a_refused_capability_reports_which_model_and_capability_failed(env):
    refused = model_result(business.MODEL_CODER_FALLBACK, "coder")
    refused = replace(
        refused,
        ok=False,
        capabilities=(
            refused.capabilities[0],
            replace(refused.capabilities[1], ok=False, detail="la réponse n'est pas du JSON"),
        )
        + (replace(refused.capabilities[2], ok=False, detail="non exécuté (qualification interrompue)"),),
    )
    with pytest.raises(IncompleteValidation, match="gemma-4-26b-a4b-it/json refusé : la réponse n'est pas du JSON"):
        activate(env, accepted(ok=False, reason="x", models=(model_result(business.MODEL_PRIMARY, "default"), refused)))


def test_a_mapping_is_never_accepted_as_the_qualification(env):
    """L'ancien contrat (mapping ``accepted``) n'établit rien : seul le QualificationReport d'A est une preuve."""
    with pytest.raises(IncompleteValidation, match="QualificationReport d'A requis"):
        activate(env, lambda settings, ledger, scope_key: {"accepted": True, "models": BOTH, "ok": True})


def test_a_budget_refusal_during_the_qualification_is_a_budget_stop_not_a_failure(env):
    def qualify(settings, ledger, scope_key):
        raise BudgetRefused("blocked_unknown_usage", "usage inconnu en mode strict")

    report = CampaignReport("campaign", "unit")
    with pytest.raises(business.BudgetStop, match="qualification des modèles refusée par le registre"):
        activate(env, qualify, report)
    assert report.facts["qualification_registry"]["scope"] == SCOPE


def test_an_unexpected_transport_error_stops_without_any_estimated_substitute(env):
    def qualify(settings, ledger, scope_key):
        raise ConnectionError("countTokens a répondu 500")

    with pytest.raises(IncompleteValidation, match="qualification des modèles impossible .ConnectionError"):
        activate(env, qualify)


def test_an_awaitable_qualification_is_run_to_completion(env):
    async def qualify(settings, ledger, scope_key):
        return good_report()

    assert activate(env, qualify).facts["qualification"]["ok"] is True


def test_the_qualification_is_called_with_the_positional_signature_of_lot_a(env):
    calls = []

    def qualify(*args, **kwargs):
        calls.append((len(args), sorted(kwargs)))
        return good_report()

    activate(env, qualify)

    assert calls == [(3, [])], "qualify_models(settings, ledger, scope_key) : trois arguments positionnels"


def test_the_absent_public_api_of_lot_a_is_an_explicit_incomplete_validation(env, monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "collegue.broker", types.ModuleType("collegue.broker"))
    with pytest.raises(IncompleteValidation, match="collegue.broker.qualify_models"):
        w5.activate_budget(env, CAMPAIGN, CampaignReport("campaign", "unit"))


def test_the_remaining_durable_window_is_the_absolute_deadline_handed_to_the_campaign_not_a_second_window(env):
    remaining = []

    activate(env, accepted(deadline_at=NOW + timedelta(seconds=640, milliseconds=500)), on_remaining=remaining.append)

    assert remaining == [640.5]


def test_the_report_carries_only_the_fields_of_the_final_contract(env):
    report = activate(env, accepted())

    assert set(report.facts["qualification"]) == {
        "scope_key", "ok", "reason", "destination", "deadline_at", "consumed_tokens", "blocked", "models", "remaining_seconds",
    }  # fmt: skip


# ── lancement : ordre exact et brouillon lié au même cycle ──────────────────────────────────────────────────────────────────


def test_the_activation_runs_after_the_claim_and_before_any_remote_creation_and_the_draft_reuses_the_cycle(tmp_path):
    adapter = FakeAdapter(tmp_path)
    order, drafts = [], []
    adapter.guard_fixture = lambda: (order.append("guard"), business.FIXTURE_SEED_SHA)[1]
    original_base, original_product = adapter.create_base, adapter.product
    adapter.create_base = lambda manifest: (order.append("base"), original_base(manifest))[1]

    def product(*args, **kwargs):
        if args[:2] == ("plan", "draft"):
            drafts.append(args)
        return original_product(*args, **kwargs)

    adapter.product = product
    report = CampaignReport("campaign", "unit")

    business.launch_campaign(
        report, adapter=adapter, env=ENV, claim=lambda r: order.append("claim"), activate=lambda r: order.append("activate"),
        cycle_id=CAMPAIGN,
    )  # fmt: skip

    assert order == ["guard", "claim", "activate", "base"]
    (draft,) = drafts
    assert draft[draft.index("--cycle-id") + 1] == CAMPAIGN, "le brouillon public reprend le cycle de l'activation"


def test_without_activation_the_draft_carries_no_cycle_option(tmp_path):
    adapter = FakeAdapter(tmp_path)
    seen = []
    original = adapter.product
    adapter.product = lambda *a, **k: (seen.append(a), original(*a, **k))[1]

    business.launch_campaign(CampaignReport("campaign", "unit"), adapter=adapter, env=ENV)

    assert "--cycle-id" not in next(a for a in seen if a[:2] == ("plan", "draft"))


def test_a_refused_activation_stops_the_campaign_before_any_creation_and_leaves_the_spend_readable(env, tmp_path):
    adapter = FakeAdapter(tmp_path)
    calls = []
    original_cleanup = adapter.cleanup
    adapter.cleanup = lambda: (calls.append("cleanup"), original_cleanup())[1]

    def qualify(settings, ledger, scope_key):
        reservation = ledger.reserve(scope_key, usd=0.02, tokens=1500)
        ledger.commit(
            reservation.reservation_id, usd=0.012, tokens=1100
        )  # les canaris ont dépensé, puis le 26B est refusé
        return good_report(ok=False, reason="gemma-4-26b-a4b-it/text: countTokens indisponible", consumed_tokens=1100)

    preflight = CampaignReport("preflight", "unit")
    preflight.declare("P01", "ok")
    preflight.run("P01", lambda s: None)
    report = business.run_campaign(
        env,
        preflight=preflight,
        launch=lambda r: business.launch_campaign(
            r, adapter=adapter, env=ENV, activate=lambda rep: w5.activate_budget(env, CAMPAIGN, rep, qualify=qualify),
            cycle_id=CAMPAIGN,
        ),
        read_registry=business.registry_reader(env),
        cleanup=lambda r: adapter.cleanup(),
    )  # fmt: skip

    assert report.step("R01-run").state == "incomplete_validation" and adapter.calls == ["guard", "cleanup"], (
        adapter.calls
    )
    exit_counters = report.facts["registry"]["exit"]
    assert exit_counters["scope"] == SCOPE and exit_counters["consumed_tokens"] == 1100, (
        "dépense des canaris relue par scope"
    )
    assert report.step("R03-registry").state == "succeeded" and calls == ["cleanup"]
    assert report.verdict() == "incomplete_validation"


# ── pile approuvée annoncée au codeur (travail hors ligne) ──────────────────────────────────────────────────────────────────


def test_the_approved_offline_stack_is_announced_in_the_problem_without_adding_a_task(tmp_path):
    adapter = FakeAdapter(tmp_path)
    drafts = []
    original = adapter.product
    adapter.product = lambda *a, **k: (drafts.append(a) if a[:2] == ("plan", "draft") else None, original(*a, **k))[1]
    report = CampaignReport("campaign", "unit")
    report.facts["bootstrap_revalidated"] = {"approved_stack": ["fastapi==0.141.1", "pypdf==6.19.0", "pytest==9.1.1"]}

    business.launch_campaign(report, adapter=adapter, env=ENV)

    (draft,) = drafts
    problem = draft[draft.index("--problem") + 1]
    assert problem.startswith(business.BUSINESS_PROBLEM)
    assert "fastapi==0.141.1, pypdf==6.19.0, pytest==9.1.1" in problem and "HORS LIGNE" in problem
    assert "ce n'est pas une tâche supplémentaire" in problem
    assert draft[draft.index("--nightly-exact-task-count") + 1] == "3"


def test_without_a_revalidated_stack_the_problem_is_the_plain_business_problem():
    assert business.business_problem() == business.BUSINESS_PROBLEM == business.business_problem(())
