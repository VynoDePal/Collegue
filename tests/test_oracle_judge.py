"""Lanceur et juge des oracles (vague 3) : un verdict n'est jamais « exit 0 ».

Le VRAI lanceur de production (``python -I`` + plugin pytest de rapport) s'exécute localement via
``oracle_sandbox.LocalOracleSandbox`` ; l'hôte juge le rapport complet. Aucun rapport n'est fabriqué par le test
(sauf les cas unitaires du juge, explicitement synthétiques).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from oracle_sandbox import LocalOracleSandbox

from collegue.executor.oracle import (
    ORACLE_END,
    ORACLE_MARKER,
    STATUS_GREEN,
    STATUS_INVALID,
    STATUS_RED_ASSERTION,
    collect_oracle_report,
    judge_oracle_run,
    new_nonce,
    oracle_pytest_command,
    parse_oracle_report,
)

LABEL = "task-1"


def run_oracles(tmp_path, entries, *, workspace_files=None, timeout=60.0):
    """Exécute ``[(label, source)]`` avec le lanceur réel ; renvoie ``(rapport, SandboxResult, workspace)``."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    for rel, content in (workspace_files or {}).items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    tmp_dir = tmp_path / "oracle-tmp"
    tmp_dir.mkdir(exist_ok=True)
    nonce = new_nonce()
    command = oracle_pytest_command(entries, nonce, workspace_dir="/workspace", tmp_dir=str(tmp_dir))
    result = LocalOracleSandbox(timeout=timeout).run_tests(str(workspace), command)
    text = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return parse_oracle_report(text, nonce), result, workspace


def judge(tmp_path, source, *, phase="candidate", **kwargs):
    report, result, workspace = run_oracles(tmp_path, [(LABEL, source)], **kwargs)
    return judge_oracle_run(report, LABEL, phase=phase), report, result, workspace


# --- les sept cas de la sonde du manager + les arrêts prématurés ---------------------------------------------


def test_benign_oracle_is_green(tmp_path):
    run, report, _result, _ws = judge(tmp_path, "def test_contract():\n    assert 2 + 2 == 4\n")
    assert run.status == STATUS_GREEN and run.executed == 1 and run.passed == 1
    assert report["finished"] is True and report["nonce"]


def test_assertion_failure_in_the_call_phase_is_a_red_assertion(tmp_path):
    run, *_ = judge(tmp_path, "def test_contract():\n    assert 2 + 2 == 5, 'contrat violé'\n")
    assert run.status == STATUS_RED_ASSERTION
    assert run.assertion_failures == 1 and run.failed == 1 and run.executed == 1


@pytest.mark.parametrize(
    ("name", "source", "counter"),
    [
        ("tous ignorés", "import pytest\ndef test_c():\n    pytest.skip('contrat ignoré')\n", "skipped"),
        (
            "ignoré partiel",
            "import pytest\ndef test_a():\n    assert True\ndef test_b():\n    pytest.skip('ignoré')\n",
            "skipped",
        ),
        (
            "xfail échoué",
            "import pytest\n@pytest.mark.xfail(reason='absent')\ndef test_c():\n    assert False\n",
            "xfailed",
        ),
        (
            "xpass",
            "import pytest\n@pytest.mark.xfail(reason='absent')\ndef test_c():\n    assert True\n",
            "xpassed",
        ),
    ],
    ids=["skip-total", "skip-partiel", "xfail", "xpass"],
)
def test_skip_and_xfail_never_prove_anything(tmp_path, name, source, counter):
    run, *_ = judge(tmp_path, source)
    assert run.status == STATUS_INVALID, name
    assert getattr(run, counter) >= 1
    assert "sauté" in run.reason or "xfail" in run.reason


def test_collection_error_is_invalid_not_a_red_assertion(tmp_path):
    run, *_ = judge(tmp_path, "import module_inexistant_884992\ndef test_c():\n    assert True\n")
    assert run.status == STATUS_INVALID and run.collection_errors == 1 and run.executed == 0
    assert "collecte" in run.reason


def test_assertion_at_collection_time_is_not_a_red_test(tmp_path):
    """Un `assert False` au niveau module échoue pendant la COLLECTE : aucune assertion de phase call n'a eu lieu."""
    run, *_ = judge(tmp_path, "assert False, 'au chargement'\ndef test_c():\n    assert True\n")
    assert run.status == STATUS_INVALID and run.collection_errors == 1 and run.assertion_failures == 0


def test_zero_tests_is_invalid(tmp_path):
    run, *_ = judge(tmp_path, "valeur = 4\n")
    assert run.status == STATUS_INVALID and run.executed == 0
    assert "aucun test" in run.reason


def test_import_error_inside_the_test_body_is_not_a_contract_assertion(tmp_path):
    run, *_ = judge(tmp_path, "def test_c():\n    import module_inexistant_884992\n    assert True\n")
    assert run.status == STATUS_INVALID and run.errors == 1 and run.assertion_failures == 0
    assert "hors assertion" in run.reason


def test_a_failure_other_than_an_assertion_is_not_red(tmp_path):
    run, *_ = judge(tmp_path, "def test_c():\n    raise KeyError('x')\n")
    assert run.status == STATUS_INVALID and run.errors == 1


def test_setup_assertion_is_not_a_call_phase_assertion(tmp_path):
    source = (
        "import pytest\n"
        "@pytest.fixture\n"
        "def broken():\n"
        "    assert False, 'setup'\n"
        "def test_c(broken):\n"
        "    assert True\n"
    )
    run, *_ = judge(tmp_path, source)
    assert run.status == STATUS_INVALID and run.assertion_failures == 0 and run.executed == 0


def test_teardown_failure_spoils_a_passing_test(tmp_path):
    source = (
        "import pytest\n"
        "@pytest.fixture\n"
        "def late():\n"
        "    yield\n"
        "    assert False, 'teardown'\n"
        "def test_c(late):\n"
        "    assert True\n"
    )
    run, *_ = judge(tmp_path, source)
    assert run.status == STATUS_INVALID and run.passed == 1 and run.errors == 1


@pytest.mark.parametrize(
    "exit_call",
    ["os._exit(0)", "os.kill(os.getpid(), 9)"],
    ids=["os-exit", "sigkill"],
)
def test_premature_exit_without_a_complete_report_proves_nothing(tmp_path, exit_call):
    source = f"import os\ndef test_a():\n    assert True\ndef test_b():\n    {exit_call}\n"
    run, report, result, _ws = judge(tmp_path, source)
    assert report is None  # le process est mort avant d'émettre le rapport
    assert run.status == STATUS_INVALID and "rapport" in run.reason


def test_timeout_proves_nothing(tmp_path):
    source = "import time\ndef test_a():\n    time.sleep(60)\n    assert True\n"
    started = time.monotonic()
    report, result, _ws = run_oracles(tmp_path, [(LABEL, source)], timeout=3.0)
    assert time.monotonic() - started < 30
    assert result.timed_out is True and report is None
    assert judge_oracle_run(report, LABEL, phase="candidate").status == STATUS_INVALID


def test_each_oracle_is_judged_on_its_own_tests(tmp_path):
    entries = [
        ("task-1", "def test_one():\n    assert True\n"),
        ("task-2", "def test_two():\n    assert 1 == 2, 'rouge'\n"),
        ("task-3", "import pytest\ndef test_three():\n    pytest.skip('ignoré')\n"),
    ]
    report, _result, _ws = run_oracles(tmp_path, entries)
    verdicts = {label: judge_oracle_run(report, label, phase="candidate").status for label, _ in entries}
    assert verdicts == {"task-1": STATUS_GREEN, "task-2": STATUS_RED_ASSERTION, "task-3": STATUS_INVALID}


def test_an_unknown_label_has_no_tests_and_is_invalid(tmp_path):
    report, _result, _ws = run_oracles(tmp_path, [(LABEL, "def test_a():\n    assert True\n")])
    assert judge_oracle_run(report, "task-99", phase="candidate").status == STATUS_INVALID


# --- protections du lanceur conservées ---------------------------------------------------------------------------


def test_a_workspace_pytest_py_cannot_replace_the_real_pytest(tmp_path):
    witness = tmp_path / "pytest-py-witness"
    fake = f"import sys\nopen({str(witness)!r}, 'w').write('exécuté')\nsys.exit(0)\n"
    run, report, _result, _ws = judge(
        tmp_path,
        "def test_contract():\n    assert 2 + 2 == 5\n",
        workspace_files={"pytest.py": fake, "pytest/__init__.py": fake},
    )
    assert not witness.exists()
    assert run.status == STATUS_RED_ASSERTION  # le vrai pytest a tourné et jugé l'assertion


def test_a_workspace_conftest_cannot_alter_the_verdict(tmp_path):
    witness = tmp_path / "conftest-witness"
    conftest = (
        "import pytest\n"
        f"open({str(witness)!r}, 'w').write('exécuté')\n"
        "def pytest_collection_modifyitems(items):\n"
        "    for item in items:\n"
        "        item.add_marker(pytest.mark.skip(reason='forcé'))\n"
    )
    run, *_ = judge(
        tmp_path,
        "def test_contract():\n    assert 2 + 2 == 5\n",
        workspace_files={"conftest.py": conftest, "tests/conftest.py": conftest},
    )
    assert not witness.exists()
    assert run.status == STATUS_RED_ASSERTION and run.skipped == 0


def test_workspace_pytest_configuration_and_plugins_are_ignored(tmp_path):
    config = "[pytest]\naddopts = -k nothing --collect-only\n"
    pyproject = "[tool.pytest.ini_options]\naddopts = '-k nothing'\n"
    run, *_ = judge(
        tmp_path,
        "def test_contract():\n    assert True\n",
        workspace_files={"pytest.ini": config, "tox.ini": config, "setup.cfg": config, "pyproject.toml": pyproject},
    )
    assert run.status == STATUS_GREEN and run.executed == 1


def test_environment_cannot_inject_options_plugins_or_paths(tmp_path, monkeypatch):
    witness = tmp_path / "plugin-witness"
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "evil_plugin.py").write_text(f"open({str(witness)!r}, 'w').write('x')\n")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k nothing --collect-only")
    monkeypatch.setenv("PYTEST_PLUGINS", "evil_plugin")
    monkeypatch.setenv("PYTHONPATH", str(evil))
    run, *_ = judge(tmp_path, "def test_contract():\n    assert True\n")
    assert not witness.exists()
    assert run.status == STATUS_GREEN and run.executed == 1


def test_workspace_sitecustomize_and_cwd_modules_are_not_imported_before_pytest(tmp_path):
    witness = tmp_path / "sitecustomize-witness"
    body = f"open({str(witness)!r}, 'w').write('x')\n"
    run, *_ = judge(
        tmp_path,
        "def test_contract():\n    assert True\n",
        workspace_files={"sitecustomize.py": body, "usercustomize.py": body, "json.py": body, "os.py": body},
    )
    assert not witness.exists()
    assert run.status == STATUS_GREEN


def test_the_oracle_file_is_created_only_in_the_isolated_tmp_and_removed(tmp_path):
    source = (
        "import os\n"
        "def test_contract():\n"
        "    assert not os.path.abspath(__file__).startswith(os.getcwd())\n"
        "    assert os.path.basename(__file__).startswith('collegue_acceptance_')\n"
    )
    run, _report, _result, workspace = judge(tmp_path, source)
    assert run.status == STATUS_GREEN
    assert not list(workspace.glob("collegue_acceptance_*"))
    assert not list((tmp_path / "oracle-tmp").glob("collegue_acceptance_*"))  # nettoyé après le run


def test_the_oracle_sees_the_project_through_cwd_and_the_workspace_on_sys_path(tmp_path):
    source = (
        "from pathlib import Path\n"
        "def test_contract():\n"
        "    assert (Path.cwd() / 'feature.py').is_file(), 'feature.py absent'\n"
        "    import feature\n"
        "    assert feature.VALUE == 42\n"
    )
    (tmp_path / "ok").mkdir()
    (tmp_path / "ko").mkdir()
    green, *_ = judge(tmp_path / "ok", source, workspace_files={"feature.py": "VALUE = 42\n"})
    red, *_ = judge(tmp_path / "ko", source)
    assert green.status == STATUS_GREEN
    assert red.status == STATUS_RED_ASSERTION and red.assertion_failures == 1


# --- complétude : une limite de capacité ne produit jamais un vert ni un rouge partiel ------------------------------


PARAMETRIZED_LAST_FAILS = (
    "import pytest\n"
    "@pytest.mark.parametrize('i', range(1800))\n"
    "def test_contract(i):\n"
    "    assert i != 1799, 'dernier cas'\n"
)


def test_1800_parametrized_tests_where_only_the_last_fails_are_never_reported_green(tmp_path):
    """pytest dit « 1 failed, 1799 passed » : un rapport plafonné à 5000 événements voyait 1667 appels, tous verts."""
    run, report, result, _ws = judge(tmp_path, PARAMETRIZED_LAST_FAILS, timeout=120.0)
    assert result.exit_code == 1  # le process lui-même est rouge
    assert run.status != STATUS_GREEN
    assert run.status == STATUS_RED_ASSERTION and run.executed == 1800 and run.assertion_failures == 1


def test_1800_parametrized_tests_all_passing_is_green_with_complete_events(tmp_path):
    source = PARAMETRIZED_LAST_FAILS.replace("i != 1799", "i >= 0")
    run, report, result, _ws = judge(tmp_path, source, timeout=120.0)
    assert result.exit_code == 0 and run.status == STATUS_GREEN and run.executed == run.passed == 1800
    assert report["collected"]["task-1"] == 1800


def test_a_report_capacity_limit_makes_the_proof_unavailable_not_green(tmp_path, monkeypatch):
    """Quand la limite est atteinte (ici abaissée), les événements manquent : invalide, quel que soit le verdict de pytest."""
    import collegue.executor.oracle as oracle

    monkeypatch.setattr(oracle, "MAX_REPORT_ITEMS", 90)  # 30 tests ≈ 90 événements (setup/call/teardown)
    source_green = "import pytest\n@pytest.mark.parametrize('i', range(40))\ndef test_c(i):\n    assert i >= 0\n"
    source_last_fails = source_green.replace("i >= 0", "i != 39")
    for name, source in (("green", source_green), ("last-fails", source_last_fails)):
        (tmp_path / name).mkdir()
        run, _report, result, _ws = judge(tmp_path / name, source)
        assert run.status == STATUS_INVALID and "événements incomplets" in run.reason, (name, run)
    assert result.exit_code == 1  # pytest, lui, voyait bien l'échec : le rapport tronqué ne peut pas le masquer


def test_oracles_sharing_the_report_capacity_are_each_judged_on_complete_events(tmp_path, monkeypatch):
    import collegue.executor.oracle as oracle

    monkeypatch.setattr(oracle, "MAX_REPORT_ITEMS", 100)
    small = "def test_a():\n    assert True\n"  # 3 événements
    big = (
        "import pytest\n@pytest.mark.parametrize('i', range(40))\ndef test_b(i):\n    assert i >= 0\n"  # 120 événements
    )
    report, _result, _ws = run_oracles(tmp_path, [("task-1", small), ("task-2", big), ("task-3", small)])
    statuses = {
        label: judge_oracle_run(report, label, phase="candidate").status for label in ("task-1", "task-2", "task-3")
    }
    assert statuses["task-1"] == STATUS_GREEN  # tenu dans la capacité avant le dépassement
    assert statuses["task-2"] == STATUS_INVALID  # événements tronqués
    assert statuses["task-3"] == STATUS_INVALID  # la capacité est PARTAGÉE : plus aucun événement pour lui


# --- protocole du rapport -------------------------------------------------------------------------------------


def _report_text(nonce, body, *, end=ORACLE_END):
    return f"bruit\n{ORACLE_MARKER}:{nonce}@@{json.dumps(body)}{end}\nsuite\n"


def _body(nonce, **extra):
    body = {"nonce": nonce, "finished": True, "exitstatus": 0, "collected": {}, "items": [], "collect_errors": []}
    body.update(extra)
    return body


def test_report_parsing_requires_the_exact_nonce_and_a_complete_payload():
    nonce = new_nonce()
    assert parse_oracle_report(_report_text(nonce, _body(nonce)), nonce)["nonce"] == nonce
    assert parse_oracle_report(_report_text(nonce, _body(nonce)), new_nonce()) is None  # autre nonce
    assert parse_oracle_report(_report_text(nonce, _body(nonce), end=""), nonce) is None  # tronqué
    assert parse_oracle_report(f"{ORACLE_MARKER}:{nonce}@@{{pas du json{ORACLE_END}", nonce) is None
    assert parse_oracle_report(_report_text(nonce, {"nonce": "x" * 32, "items": []}), nonce) is None
    assert parse_oracle_report(_report_text(nonce, {"nonce": nonce, "items": "oui"}), nonce) is None
    assert parse_oracle_report("", nonce) is None and parse_oracle_report(None, nonce) is None


def test_a_forged_report_line_from_the_tested_code_does_not_replace_the_real_one(tmp_path):
    forged_nonce = "f" * 32
    forged = json.dumps(_body(forged_nonce, items=[]))
    source = (
        "def test_contract():\n"
        f"    print({ORACLE_MARKER + ':' + forged_nonce + '@@' + forged + ORACLE_END!r})\n"
        "    assert 2 + 2 == 5\n"
    )
    run, report, *_ = judge(tmp_path, source)
    assert report["nonce"] != forged_nonce
    assert run.status == STATUS_RED_ASSERTION  # le rapport authentique fait foi


# --- juge : cas synthétiques du protocole ---------------------------------------------------------------------


def _item(when="call", outcome="passed", *, skipped=False, xfail=False, assertion=False, label=LABEL, name="test_a"):
    return {"f": label, "n": name, "w": when, "o": outcome, "s": skipped, "x": xfail, "a": assertion, "t": None}


def _test(name="test_a", *, call="passed", assertion=False, label=LABEL, **flags):
    """Cycle de vie COMPLET d'un test : setup, call, teardown (ce que pytest émet réellement)."""
    return [
        _item("setup", "passed", label=label, name=name),
        _item("call", call, assertion=assertion, label=label, name=name, **flags),
        _item("teardown", "passed", label=label, name=name),
    ]


def _synthetic(items, *, finished=True, exitstatus=0, collect_errors=(), collected=None):
    items = list(items)
    if collected is None:
        collected = {}
        for item in items:
            collected.setdefault(item["f"], set()).add(item["n"])
        collected = {label: len(names) for label, names in collected.items()}
    return {
        "finished": finished,
        "exitstatus": exitstatus,
        "collected": collected,
        "items": items,
        "collect_errors": list(collect_errors),
    }


def test_judge_refuses_missing_unfinished_or_interrupted_reports():
    assert judge_oracle_run(None, LABEL, phase="candidate").status == STATUS_INVALID
    unfinished = _synthetic(_test(), finished=False)
    assert judge_oracle_run(unfinished, LABEL, phase="candidate").status == STATUS_INVALID
    for status in (2, 3, 4):  # interrompu / erreur interne / usage
        run = judge_oracle_run(_synthetic(_test(), exitstatus=status), LABEL, phase="candidate")
        assert run.status == STATUS_INVALID, status


def test_judge_green_requires_a_call_phase_pass_and_nothing_else():
    assert judge_oracle_run(_synthetic(_test()), LABEL, phase="candidate").status == STATUS_GREEN
    only_setup = _synthetic([_item(when="setup", outcome="passed")])
    assert judge_oracle_run(only_setup, LABEL, phase="candidate").status == STATUS_INVALID
    red = _synthetic(_test(call="failed", assertion=True), exitstatus=1)
    assert judge_oracle_run(red, LABEL, phase="candidate").status == STATUS_RED_ASSERTION
    mixed = _synthetic(
        _test(call="failed", assertion=True) + _test("test_b", call="failed"),  # un échec NON assertion
        exitstatus=1,
    )
    assert judge_oracle_run(mixed, LABEL, phase="candidate").status == STATUS_INVALID


def test_judge_requires_every_collected_test_to_have_run_its_whole_lifecycle():
    complete = _synthetic(_test() + _test("test_b"))
    assert judge_oracle_run(complete, LABEL, phase="candidate").status == STATUS_GREEN
    # 3 tests collectés, 2 seulement avec des événements : session interrompue / rapport borné
    missing = _synthetic(_test() + _test("test_b"), collected={LABEL: 3})
    run = judge_oracle_run(missing, LABEL, phase="candidate")
    assert run.status == STATUS_INVALID and "événements incomplets" in run.reason
    # collecte non déclarée pour cet oracle
    unknown = _synthetic(_test(), collected={})
    assert judge_oracle_run(unknown, LABEL, phase="candidate").status == STATUS_INVALID
    # teardown absent : arrêt prématuré APRÈS le call
    no_teardown = _synthetic([_item("setup"), _item("call")])
    run = judge_oracle_run(no_teardown, LABEL, phase="candidate")
    assert run.status == STATUS_INVALID and "teardown" in run.reason


def test_report_collection_names_the_exact_problem():
    nonce = new_nonce()
    good = _report_text(nonce, _body(nonce, exitstatus=0))
    assert collect_oracle_report(good, nonce, exit_code=0)[1] == ""
    # contradiction entre le code de sortie RÉEL du process et le statut du rapport
    report, problem = collect_oracle_report(good, nonce, exit_code=1)
    assert report is None and "contradictoire" in problem
    assert collect_oracle_report(_report_text(nonce, _body(nonce, exitstatus=1)), nonce, exit_code=0)[0] is None
    # plusieurs rapports pour un même nonce : jamais « le dernier gagne »
    twice = good + _report_text(nonce, _body(nonce, exitstatus=0))
    report, problem = collect_oracle_report(twice, nonce, exit_code=0)
    assert report is None and "2 rapports" in problem
    assert parse_oracle_report(twice, nonce) is None
    # sortie tronquée par le sandbox avant le rapport, absence pure, délai
    truncated = "bruit...\n[sandbox] sortie tronquée à 10485760 octets"
    assert "tronquée par le sandbox" in collect_oracle_report(truncated, nonce, exit_code=0)[1]
    assert "absent" in collect_oracle_report("", nonce)[1]
    assert "délai" in collect_oracle_report(good, nonce, timed_out=True)[1]
    assert "illisible" in collect_oracle_report(f"{ORACLE_MARKER}:{nonce}@@pas du json{ORACLE_END}", nonce)[1]


def test_the_judge_keeps_the_precise_problem_as_the_reason():
    run = judge_oracle_run(None, LABEL, phase="candidate", problem="2 rapports pour le même nonce : sortie non fiable")
    assert run.status == STATUS_INVALID and "2 rapports" in run.reason


def _exit_code_lie(tmp_path, claimed):
    """Un sandbox dont le code de sortie contredit le rapport émis (paramètre `claimed` = code renvoyé)."""
    from types import SimpleNamespace

    from collegue.executor.contracts import SealedContract, execute_oracles

    real = LocalOracleSandbox()

    class _Lying:
        def run_tests(self, workspace, command):
            result = real.run_tests(workspace, command)
            return SimpleNamespace(
                exit_code=claimed, stdout=result.stdout, stderr=result.stderr, timed_out=result.timed_out
            )

    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    contract = SealedContract(
        task_id=1,
        title="t",
        source="def test_a():\n    assert True\n",
        source_sha256="a" * 64,
        contract_sha256="b" * 64,
        provenance_sha256="c" * 64,
        role="delivered",
    )
    return execute_oracles(str(workspace), [contract], sandbox=_Lying(), phase="candidate").runs[1]


def test_a_process_exit_code_that_contradicts_the_report_is_invalid(tmp_path):
    honest = _exit_code_lie(tmp_path / "honest", 0)
    assert honest.status == STATUS_GREEN
    lying = _exit_code_lie(tmp_path / "lying", 1)  # le rapport dit « 0 », le process « 1 »
    assert lying.status == STATUS_INVALID and "contradictoire" in lying.reason


def test_judge_records_the_phase_and_counts():
    run = judge_oracle_run(_synthetic(_test() + _test("test_b")), LABEL, phase="preimage")
    assert (run.phase, run.executed, run.passed) == ("preimage", 2, 2) and run.tests == ("test_a", "test_b")
    assert os.getpid()  # (garde de lecture : le juge est pur, aucun effet de bord)


def test_judge_ignores_collection_errors_of_other_oracles():
    report = _synthetic(_test(), collect_errors=[{"f": "task-2", "e": "ImportError"}])
    assert judge_oracle_run(report, LABEL, phase="candidate").status == STATUS_GREEN
    assert judge_oracle_run(report, "task-2", phase="candidate").status == STATUS_INVALID


def test_command_keeps_every_launcher_protection():
    command = oracle_pytest_command([(LABEL, "def test_a():\n    assert True\n")], "n" * 32)
    for expected in (
        "python -I -c",
        "PYTHONPATH=",
        "PYTEST_ADDOPTS=",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        "PYTEST_PLUGINS=",
        "--noconftest",
        "/dev/null",
        "no:cacheprovider",
    ):
        assert expected in command, expected
    # pytest est importé AVANT l'ajout des chemins du projet (aucun pytest.py local ne peut le remplacer)
    assert command.index("import pytest") < command.index("sys.path[:0]")
    assert str(Path("/workspace")) in command
