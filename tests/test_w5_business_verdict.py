"""Verdict de campagne : preuve de dépense du registre (inconnue tardive, lecture obligatoire manquante, réparation).

Aucun réseau, aucune clé : seuls les rappels de phase et de lecture sont des doublures déterministes ; le séquencement et le
verdict sont ceux de la production (``run_campaign`` / ``RegistryProof``)."""

import pytest

from collegue.pilot import w4_business as business


def _preflight():
    report = business.CampaignReport("preflight", "verdict-probe")
    report.declare("P01", "preflight witness")
    report.run("P01", lambda step: None)
    return report


def counters(**changes):
    base = dict(
        scope="project:1", strict=True, cap_usd=2, cap_tokens=250000, consumed_micro_usd=0, consumed_tokens=15,
        reserved_micro_usd=0, reserved_tokens=0, unknown_micro_usd=0, unknown_tokens=0, blocked_reason=None, revision=1,
    )  # fmt: skip
    base.update(changes)
    return base


class Journey:
    """Une campagne dont les lectures du registre sont scriptées par numéro d'appel."""

    def __init__(self, script):
        self.script = script
        self.reads = 0
        self.ran = []

    def read(self, context):
        self.reads += 1
        outcome = self.script(self.reads)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def phase(self, name):
        def run(report, context):
            self.ran.append(name)

        return run

    def run(self):
        return business.run_campaign(
            {}, preflight=_preflight(), launch=lambda report: {"project_id": 1} | self._ran("R01") or {"project_id": 1},
            verify=lambda report, ctx: None, improve=self.phase("R04"), incident=self.phase("R05"),
            cleanup=lambda report: None, read_registry=self.read,
        )  # fmt: skip

    def _ran(self, name):
        self.ran.append(name)
        return {}


def states(report):
    return {step.id: step.state for step in report.steps}


def test_a_clean_journey_validates():
    journey = Journey(lambda n: counters(revision=n, consumed_tokens=10 * n))
    report = journey.run()
    assert report.verdict() == business.VERDICT_VALIDATED, report.to_json()
    assert journey.ran == ["R01", "R04", "R05"]


@pytest.mark.parametrize(
    "dirty",
    [
        dict(unknown_tokens=100, blocked_reason="lost response"),
        dict(unknown_micro_usd=5),
        dict(blocked_reason="manual"),
    ],
)
def test_unknown_or_blocked_usage_at_the_last_phase_cannot_validate(dirty):
    # La consommation inconnue n'apparaît qu'après R05 : lecture après R05, lecture finale et lecture de sortie.
    journey = Journey(lambda n: counters(revision=n, **(dirty if n >= 4 else {})))
    report = journey.run()
    assert report.verdict() == business.VERDICT_BUDGET_STOP, report.to_json()
    assert states(report)["R03-registry"] == business.STEP_BUDGET_STOP
    assert report.exit_code() == 4


def test_unknown_usage_revealed_only_by_the_exit_read_withdraws_the_success_of_r03():
    # Lectures : after-R01, after-R02, after-R04, after-R05, final (R03), exit — l'inconnue arrive à la toute dernière.
    journey = Journey(lambda n: counters(revision=n, unknown_tokens=7 if n == 6 else 0))
    report = journey.run()
    assert report.verdict() == business.VERDICT_BUDGET_STOP, report.to_json()
    assert "lecture de sortie" in report.step("R03-registry").detail


def test_an_unknown_usage_seen_earlier_is_never_forgotten_by_a_clean_later_read():
    journey = Journey(lambda n: counters(revision=n, unknown_tokens=9 if n == 3 else 0))
    report = journey.run()
    assert report.verdict() != business.VERDICT_VALIDATED, report.to_json()
    assert journey.ran == ["R01", "R04"] or journey.ran == ["R01"], journey.ran  # R05 jamais lancée après l'inconnue


def test_the_registry_bounds_helper_itself_refuses_unknown_usage_and_blocking():
    with pytest.raises(business.BudgetStop):
        business.assert_registry_within_bounds(counters(unknown_tokens=1))
    with pytest.raises(business.BudgetStop):
        business.assert_registry_within_bounds(counters(blocked_reason="x"))
    business.assert_registry_within_bounds(counters())


def test_a_missing_mandatory_read_closes_emissions_and_keeps_the_original_cause():
    journey = Journey(lambda n: OSError("registry unavailable after BUILD") if n == 1 else counters(revision=n))
    report = journey.run()
    assert report.verdict() == business.VERDICT_INCOMPLETE, report.to_json()
    assert journey.ran == ["R01"], "aucune émission après une lecture obligatoire manquante"
    assert states(report)["R04-improvement"] == business.STEP_INCOMPLETE
    assert states(report)["R05-incident-rollback"] == business.STEP_NOT_EXECUTED
    cause = report.step("R04-improvement").detail
    assert "after-R01-run" in cause and "registry unavailable after BUILD" in cause and "OSError" in cause
    gap = report.facts["registry_proof"]["gaps"][0]
    assert gap["label"] == "after-R01-run" and "registry unavailable after BUILD" in gap["cause"]


def test_a_later_clean_read_repairs_the_spend_proof_but_never_reopens_emissions():
    journey = Journey(lambda n: OSError("blip") if n == 1 else counters(revision=n, consumed_tokens=10 * n))
    report = journey.run()
    gap = report.facts["registry_proof"]["gaps"][0]
    assert (
        gap["repaired_by"] == "after-R02-business"
    )  # la lecture après R02 (non émettrice) répare la preuve de dépense
    assert journey.ran == ["R01"], "la réparation de la preuve ne rouvre pas les émissions"
    assert report.verdict() == business.VERDICT_INCOMPLETE


def test_a_gap_after_the_last_emitting_phase_can_be_repaired_by_the_final_read():
    # Lectures : 1 after-R01, 2 after-R02, 3 after-R04, 4 after-R05 (manquante), 5 final, 6 exit.
    journey = Journey(lambda n: OSError("late blip") if n == 4 else counters(revision=n, consumed_tokens=10 * n))
    report = journey.run()
    gap = report.facts["registry_proof"]["gaps"][0]
    assert gap["label"] == "after-R05-incident-rollback" and gap["repaired_by"] == "final"
    assert "late blip" in gap["cause"], "la cause d'origine reste consignée même réparée"
    assert journey.ran == ["R01", "R04", "R05"]
    assert report.verdict() == business.VERDICT_VALIDATED, report.to_json()


@pytest.mark.parametrize(
    "regress",
    [dict(revision=1), dict(consumed_tokens=1), dict(scope="project:2")],
    ids=["revision-regresses", "consumption-regresses", "other-scope"],
)
def test_a_read_that_does_not_extend_the_last_valid_one_cannot_repair_a_gap(regress):
    # 1..3 valides, 4 (après R05) manquante, 5 (finale) incohérente avec la dernière lecture valide, 6 (sortie) cohérente avec 5.
    def script(n):
        if n == 4:
            return OSError("late blip")
        if n == 5:
            return counters(**{"revision": 10, "consumed_tokens": 100, **regress})
        if n == 6:
            return counters(
                **{"revision": 11, "consumed_tokens": 101, **{k: v for k, v in regress.items() if k == "scope"}}
            )
        return counters(revision=n, consumed_tokens=10 * n)

    report = Journey(script).run()
    assert report.verdict() != business.VERDICT_VALIDATED, report.to_json()
    gap = report.facts["registry_proof"]["gaps"][0]
    assert gap["repaired_by"] is None, "ni la lecture incohérente ni la suivante ne répare la lacune"


def test_an_unreadable_exit_read_leaves_the_proof_incomplete():
    journey = Journey(lambda n: OSError("exit blip") if n == 6 else counters(revision=n, consumed_tokens=10 * n))
    report = journey.run()
    assert report.verdict() == business.VERDICT_INCOMPLETE, report.to_json()
    assert "exit blip" in report.step("R03-registry").detail


def test_the_final_read_is_mandatory_for_r03():
    journey = Journey(lambda n: OSError("no registry") if n >= 5 else counters(revision=n, consumed_tokens=10 * n))
    report = journey.run()
    assert report.verdict() == business.VERDICT_INCOMPLETE, report.to_json()
    assert "no registry" in report.step("R03-registry").detail
