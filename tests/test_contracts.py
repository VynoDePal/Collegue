"""Contrats d'acceptation scellés (vague 3) : relus depuis l'état, rejoués sur le candidat, preuve négative.

SQLite, plan approuvé, provenance et oracles RÉELS (``generate_acceptance_tests`` écrit les artefacts) ; les oracles
s'exécutent avec le vrai lanceur (``oracle_sandbox.LocalOracleSandbox``). Aucun LLM, aucun réseau.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from oracle_sandbox import LocalOracleSandbox

import collegue.planner.acceptance_tests as at
from collegue.executor.agent import IssueSpec
from collegue.executor.contracts import (
    ContractError,
    OracleBatch,
    SealedContract,
    execute_oracles,
    has_sealed_contracts,
    load_current_contract,
    load_delivered_contracts,
    replay_contracts,
    verify_task_contract,
)
from collegue.planner import generate_acceptance_tests
from collegue.planner.plan_review import approve_plan
from collegue.state import ProjectStateManager

# Oracle « bien écrit » : échoue PAR ASSERTION tant que feature.py n'existe pas, puis réussit (prompt QA v2).
FEATURE_ORACLE = (
    "from pathlib import Path\n"
    "def test_feature_value():\n"
    "    feature = Path.cwd() / 'feature.py'\n"
    "    assert feature.is_file(), 'feature.py est requis par le critère'\n"
    "    namespace = {}\n"
    "    exec(feature.read_text(), namespace)\n"
    "    assert namespace['VALUE'] == 42\n"
)
OTHER_ORACLE = (
    "from pathlib import Path\n"
    "def test_other_value():\n"
    "    other = Path.cwd() / 'other.py'\n"
    "    assert other.is_file(), 'other.py est requis par le critère'\n"
    "    namespace = {}\n"
    "    exec(other.read_text(), namespace)\n"
    "    assert namespace['OTHER'] == 7\n"
)
SETTINGS = SimpleNamespace(
    LLM_PROVIDER="gemini", LLM_MODEL="default", LLM_PROVIDER_QA="openai", LLM_MODEL_QA="qa-model", LLM_CALL_TIMEOUT=0
)


@pytest.fixture
def manager(tmp_path):
    return ProjectStateManager.from_url(f"sqlite:///{tmp_path / 'state.db'}", create=True)


async def sealed_project(manager, oracles, *, statuses=None, approve=True):
    """Projet avec une tâche par oracle, oracles scellés par le générateur réel, plan approuvé."""
    spec = "# SPEC\nproduit de test\n"
    pid = manager.create_project(name="contrats", spec=spec)
    ids = []
    for index, _ in enumerate(oracles, 1):
        ids.append(manager.add_task(pid, title=f"Tâche {index}", acceptance=f"Critère {index}", depends_on=ids[-1:]))
    tasks = manager.get_tasks(pid)
    sources = iter(oracles)
    await generate_acceptance_tests(
        spec,
        tasks,
        None,
        manager=manager,
        project_id=pid,
        settings_obj=SETTINGS,
        sample_fn=lambda _prompt, _system: next(sources),
    )
    if approve:
        approve_plan(manager, pid, require_acceptance_artifacts=True)
    for task_id, status in (statuses or {}).items():
        manager.update_task_status(ids[task_id - 1], status)
    return pid, ids


def workspace(tmp_path, name, files=None):
    root = tmp_path / name
    root.mkdir()
    for rel, content in (files or {}).items():
        (root / rel).write_text(content)
    return str(root)


@contextlib.contextmanager
def preimage_at(path):
    yield path


# --- relecture et vérification depuis l'état -----------------------------------------------------------------------


async def test_current_contract_is_read_from_state_and_verified(manager):
    pid, ids = await sealed_project(manager, [FEATURE_ORACLE])
    issue = IssueSpec(number=1, title="Tâche 1", acceptance_criteria=("Critère 1",), source_task_id=ids[0])
    contract = load_current_contract(manager, pid, issue)
    assert isinstance(contract, SealedContract) and contract.label == f"task-{ids[0]}"
    assert contract.source == FEATURE_ORACLE and contract.role == "current"
    import hashlib

    assert contract.source_sha256 == hashlib.sha256(FEATURE_ORACLE.encode()).hexdigest()
    assert len(contract.contract_sha256) == len(contract.provenance_sha256) == 64


async def test_wrong_issue_binding_is_refused(manager):
    pid, ids = await sealed_project(manager, [FEATURE_ORACLE])
    for issue, message in (
        (IssueSpec(number=1, title="Tâche 1", acceptance_criteria=("Critère 1",)), "source_task_id"),
        (
            IssueSpec(number=1, title="Tâche 1", acceptance_criteria=("Autre critère",), source_task_id=ids[0]),
            "critères",
        ),
        (IssueSpec(number=1, title="Autre titre", acceptance_criteria=("Critère 1",), source_task_id=ids[0]), "titre"),
        (IssueSpec(number=1, title="Tâche 1", acceptance_criteria=("Critère 1",), source_task_id=999), "introuvable"),
    ):
        with pytest.raises(ContractError, match=message):
            load_current_contract(manager, pid, issue)


async def test_unapproved_or_modified_plan_refuses_every_contract(manager):
    pid, ids = await sealed_project(manager, [FEATURE_ORACLE], approve=False)
    issue = IssueSpec(number=1, title="Tâche 1", acceptance_criteria=("Critère 1",), source_task_id=ids[0])
    with pytest.raises(ContractError, match="non approuvé"):
        load_current_contract(manager, pid, issue)
    approve_plan(manager, pid, require_acceptance_artifacts=True)
    load_current_contract(manager, pid, issue)  # témoin : approuvé, accepté
    manager.update_project(pid, spec="# SPEC modifiée après approbation\n")
    with pytest.raises(ContractError, match="non approuvé|modifié|incohérent"):
        load_current_contract(manager, pid, issue)
    with pytest.raises(ContractError):
        load_delivered_contracts(manager, pid)


def _rewrite_provenance(manager, pid, task_id, **changes):
    """Réécrit la provenance d'une tâche dans l'état (simule une altération de la base)."""
    tasks = manager.get_tasks(pid)
    artifacts = {
        t.id: {"source": t.acceptance_test_source, "provenance": dict(t.acceptance_test_provenance)} for t in tasks
    }
    artifacts[task_id]["provenance"].update(changes)
    manager.set_acceptance_test_artifacts(pid, artifacts)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prompt_sha256", "0" * 64),
        ("spec_sha256", "1" * 64),
        ("criteria_sha256", "2" * 64),
        ("contract_sha256", "3" * 64),
        ("role", "coder"),
        ("generator", "someone.else"),
        ("runner", "unittest"),
        ("schema_version", 2),
        ("generated_at", "hier"),
    ],
)
async def test_altered_provenance_is_refused(manager, field, value):
    pid, ids = await sealed_project(manager, [FEATURE_ORACLE])
    task = manager.get_task(ids[0])
    verify_task_contract(manager, pid, task, role="delivered")  # témoin : intacte, acceptée
    try:
        _rewrite_provenance(manager, pid, ids[0], **{field: value})
    except ValueError:
        return  # le manager refuse déjà l'écriture : défense en profondeur
    approve_plan(manager, pid, require_acceptance_artifacts=True)
    with pytest.raises(ContractError):
        verify_task_contract(manager, pid, manager.get_task(ids[0]), role="delivered")


async def test_a_sealed_prompt_from_a_known_legacy_version_still_verifies(manager):
    pid, ids = await sealed_project(manager, [FEATURE_ORACLE])
    task = manager.get_task(ids[0])
    project = manager.get_project(pid)
    prompt = at.acceptance_prompt(project.spec, task, manager.get_tasks(pid), pid)
    legacy = at.prompt_sha256(prompt, system_prompt=at.LEGACY_SYSTEM_PROMPTS[0])
    assert legacy != task.acceptance_test_provenance["prompt_sha256"]
    _rewrite_provenance(manager, pid, ids[0], prompt_sha256=legacy)
    approve_plan(manager, pid, require_acceptance_artifacts=True)
    assert verify_task_contract(manager, pid, manager.get_task(ids[0]), role="delivered").task_id == ids[0]


async def test_delivered_contracts_are_all_tasks_done_or_merged_except_the_excluded(manager):
    pid, ids = await sealed_project(manager, [FEATURE_ORACLE, OTHER_ORACLE], statuses={1: "merged", 2: "in_review"})
    assert [c.task_id for c in load_delivered_contracts(manager, pid)] == [ids[0]]
    manager.update_task_status(ids[1], "done")
    assert [c.task_id for c in load_delivered_contracts(manager, pid)] == ids
    assert [c.task_id for c in load_delivered_contracts(manager, pid, exclude_task_id=ids[1])] == [ids[0]]
    assert all(c.role == "delivered" for c in load_delivered_contracts(manager, pid))


async def test_has_sealed_contracts_follows_the_delivered_tasks(manager):
    pid, ids = await sealed_project(manager, [FEATURE_ORACLE], statuses={})
    assert has_sealed_contracts(manager, pid) is False  # rien de livré : rien à rejouer
    manager.update_task_status(ids[0], "merged")
    assert has_sealed_contracts(manager, pid) is True
    bare = manager.create_project(name="sans oracle")
    done = manager.add_task(bare, title="T", acceptance="a")
    manager.update_task_status(done, "merged")
    assert has_sealed_contracts(manager, bare) is False  # BUILD sans gate d'acceptation


# --- exécution : même oracle, préimage rouge par assertion, candidat vert -----------------------------------------------


async def test_current_contract_is_red_by_assertion_on_the_preimage_then_green_on_the_candidate(tmp_path, manager):
    pid, ids = await sealed_project(manager, [FEATURE_ORACLE])
    issue = IssueSpec(number=1, title="Tâche 1", acceptance_criteria=("Critère 1",), source_task_id=ids[0])
    current = load_current_contract(manager, pid, issue)
    preimage = workspace(tmp_path, "preimage")  # avant la tâche : pas de feature.py
    candidate = workspace(tmp_path, "candidate", {"feature.py": "VALUE = 42\n"})

    result = replay_contracts(
        candidate,
        current=current,
        delivered=(),
        sandbox=LocalOracleSandbox(),
        preimage=lambda: preimage_at(preimage),
    )

    assert result.ok, result.reason
    (evidence,) = result.evidence
    assert evidence.passed and evidence.expected_preimage == "red-assertion"
    assert evidence.preimage.status == "red-assertion" and evidence.preimage.assertion_failures == 1
    assert evidence.candidate.status == "green"
    assert evidence.source_sha256 == current.source_sha256  # MÊME oracle (SHA-256) des deux côtés


async def test_an_oracle_that_already_passes_on_the_preimage_proves_nothing(tmp_path, manager):
    pid, ids = await sealed_project(manager, [FEATURE_ORACLE])
    issue = IssueSpec(number=1, title="Tâche 1", acceptance_criteria=("Critère 1",), source_task_id=ids[0])
    current = load_current_contract(manager, pid, issue)
    already = workspace(tmp_path, "already", {"feature.py": "VALUE = 42\n"})
    result = replay_contracts(
        already, current=current, delivered=(), sandbox=LocalOracleSandbox(), preimage=lambda: preimage_at(already)
    )
    assert not result.ok and "préimage" in result.reason


IMPORT_STYLE_ORACLE = "def test_feature_value():\n    from feature import VALUE\n    assert VALUE == 42\n"
COLLECTION_STYLE_ORACLE = "from feature import VALUE\ndef test_feature_value():\n    assert VALUE == 42\n"


@pytest.mark.parametrize(
    "oracle", [IMPORT_STYLE_ORACLE, COLLECTION_STYLE_ORACLE], ids=["import-dans-le-test", "import-collecte"]
)
async def test_an_import_error_on_the_preimage_is_not_a_negative_proof(tmp_path, manager, oracle):
    pid, ids = await sealed_project(manager, [oracle])
    issue = IssueSpec(number=1, title="Tâche 1", acceptance_criteria=("Critère 1",), source_task_id=ids[0])
    current = load_current_contract(manager, pid, issue)
    preimage = workspace(tmp_path, "preimage")
    candidate = workspace(tmp_path, "candidate", {"feature.py": "VALUE = 42\n"})
    result = replay_contracts(
        candidate, current=current, delivered=(), sandbox=LocalOracleSandbox(), preimage=lambda: preimage_at(preimage)
    )
    assert not result.ok
    (evidence,) = result.evidence
    assert evidence.candidate.status == "green"  # le candidat est bon...
    assert evidence.preimage.status == "invalid"  # ... mais l'échec de la préimage n'est PAS une assertion
    assert "préimage" in result.reason


async def test_missing_preimage_or_a_preimage_that_fails_to_provide_a_base_is_refused(tmp_path, manager):
    pid, ids = await sealed_project(manager, [FEATURE_ORACLE])
    issue = IssueSpec(number=1, title="Tâche 1", acceptance_criteria=("Critère 1",), source_task_id=ids[0])
    current = load_current_contract(manager, pid, issue)
    candidate = workspace(tmp_path, "candidate", {"feature.py": "VALUE = 42\n"})
    none = replay_contracts(candidate, current=current, delivered=(), sandbox=LocalOracleSandbox(), preimage=None)
    assert not none.ok and "préimage" in none.reason

    @contextlib.contextmanager
    def broken():
        raise RuntimeError("clone impossible")
        yield  # pragma: no cover

    boom = replay_contracts(candidate, current=current, delivered=(), sandbox=LocalOracleSandbox(), preimage=broken)
    assert not boom.ok and "préimage" in boom.reason


async def test_a_red_candidate_is_refused_without_running_the_preimage(tmp_path, manager):
    pid, ids = await sealed_project(manager, [FEATURE_ORACLE])
    issue = IssueSpec(number=1, title="Tâche 1", acceptance_criteria=("Critère 1",), source_task_id=ids[0])
    current = load_current_contract(manager, pid, issue)
    candidate = workspace(tmp_path, "candidate", {"feature.py": "VALUE = 41\n"})  # mauvaise valeur
    called = []

    @contextlib.contextmanager
    def preimage():
        called.append(True)
        yield workspace(tmp_path, "unused")

    result = replay_contracts(candidate, current=current, delivered=(), sandbox=LocalOracleSandbox(), preimage=preimage)
    assert not result.ok and called == []
    assert result.evidence[0].candidate.status == "red-assertion"


# --- contrats déjà livrés : non-régression -------------------------------------------------------------------------------


async def test_delivered_contract_must_stay_green_when_the_next_task_is_added(tmp_path, manager):
    pid, ids = await sealed_project(manager, [FEATURE_ORACLE, OTHER_ORACLE], statuses={1: "merged"})
    issue = IssueSpec(number=2, title="Tâche 2", acceptance_criteria=("Critère 2",), source_task_id=ids[1])
    current = load_current_contract(manager, pid, issue)
    delivered = load_delivered_contracts(manager, pid, exclude_task_id=ids[1])
    preimage = workspace(tmp_path, "preimage", {"feature.py": "VALUE = 42\n"})  # base : tâche 1 livrée

    complete = workspace(tmp_path, "complete", {"feature.py": "VALUE = 42\n", "other.py": "OTHER = 7\n"})
    ok = replay_contracts(
        complete,
        current=current,
        delivered=delivered,
        sandbox=LocalOracleSandbox(),
        preimage=lambda: preimage_at(preimage),
    )
    assert ok.ok, ok.reason
    assert {e.task_id: e.role for e in ok.evidence} == {ids[1]: "current", ids[0]: "delivered"}
    delivered_evidence = next(e for e in ok.evidence if e.role == "delivered")
    assert delivered_evidence.expected_preimage == "not-required" and delivered_evidence.preimage is None

    broken = workspace(tmp_path, "broken", {"feature.py": "VALUE = 0\n", "other.py": "OTHER = 7\n"})  # casse la tâche 1
    ko = replay_contracts(
        broken,
        current=current,
        delivered=delivered,
        sandbox=LocalOracleSandbox(),
        preimage=lambda: preimage_at(preimage),
    )
    assert not ko.ok
    assert f"tâche {ids[0]}" in ko.reason and "delivered" in ko.reason


async def test_improvement_style_replay_without_a_current_contract(tmp_path, manager):
    pid, ids = await sealed_project(manager, [FEATURE_ORACLE, OTHER_ORACLE], statuses={1: "merged", 2: "merged"})
    delivered = load_delivered_contracts(manager, pid)
    good = workspace(tmp_path, "good", {"feature.py": "VALUE = 42\n", "other.py": "OTHER = 7\n"})
    assert replay_contracts(good, current=None, delivered=delivered, sandbox=LocalOracleSandbox()).ok
    bad = workspace(tmp_path, "bad", {"feature.py": "VALUE = 42\n"})  # other.py supprimé par l'amélioration
    result = replay_contracts(bad, current=None, delivered=delivered, sandbox=LocalOracleSandbox())
    assert not result.ok and f"tâche {ids[1]}" in result.reason


async def test_workspace_tests_never_replace_the_sealed_source(tmp_path, manager):
    """Un module de test placé dans le workspace (même nom, même contenu « vert ») ne remplace pas l'oracle scellé."""
    pid, ids = await sealed_project(manager, [FEATURE_ORACLE], statuses={1: "merged"})
    delivered = load_delivered_contracts(manager, pid)
    forged = {
        f"task-{ids[0]}.py": "def test_feature_value():\n    assert True\n",
        "test_feature.py": "def test_feature_value():\n    assert True\n",
        "conftest.py": "import pytest\ndef pytest_collection_modifyitems(items):\n    items.clear()\n",
        "feature.py": "VALUE = 0\n",
    }
    candidate = workspace(tmp_path, "candidate", forged)
    result = replay_contracts(candidate, current=None, delivered=delivered, sandbox=LocalOracleSandbox())
    assert not result.ok  # l'oracle scellé (rouge ici : VALUE = 0) fait foi, pas les tests du workspace


async def test_oracle_artefact_missing_for_a_delivered_task_refuses_the_replay(manager):
    pid, ids = await sealed_project(manager, [FEATURE_ORACLE, OTHER_ORACLE], statuses={1: "merged", 2: "merged"})
    with manager.session() as session:
        from collegue.state.models import Task

        task = session.get(Task, ids[1])
        task.acceptance_test_source = None  # artefact supprimé de l'état (la contrainte SQL exige le triplet entier)
        task.acceptance_test_sha256 = None
        task.acceptance_test_provenance = None
    with pytest.raises(ContractError):
        load_delivered_contracts(manager, pid)


async def test_sandbox_failures_make_every_oracle_invalid_never_green(tmp_path, manager):
    pid, ids = await sealed_project(manager, [FEATURE_ORACLE], statuses={1: "merged"})
    delivered = load_delivered_contracts(manager, pid)
    candidate = workspace(tmp_path, "candidate", {"feature.py": "VALUE = 42\n"})

    class _Exploding:
        def run_tests(self, workspace, command):
            raise RuntimeError("docker indisponible")

    class _GreenButNoReport:
        def run_tests(self, workspace, command):
            return SimpleNamespace(exit_code=0, stdout="1 passed", stderr="", timed_out=False)

    class _TimedOut:
        def run_tests(self, workspace, command):
            return SimpleNamespace(exit_code=124, stdout="", stderr="délai", timed_out=True)

    for sandbox in (_Exploding(), _GreenButNoReport(), _TimedOut()):
        result = replay_contracts(candidate, current=None, delivered=delivered, sandbox=sandbox)
        assert not result.ok, type(sandbox).__name__
        assert result.evidence[0].candidate.status == "invalid"
    # témoin : le même appel avec le vrai lanceur réussit
    assert replay_contracts(candidate, current=None, delivered=delivered, sandbox=LocalOracleSandbox()).ok


async def test_duplicate_contracts_and_empty_sets(tmp_path, manager):
    pid, ids = await sealed_project(manager, [FEATURE_ORACLE], statuses={1: "merged"})
    (contract,) = load_delivered_contracts(manager, pid)
    candidate = workspace(tmp_path, "candidate", {"feature.py": "VALUE = 42\n"})
    with pytest.raises(ContractError, match="dupliqué"):
        replay_contracts(candidate, current=contract, delivered=(contract,), sandbox=LocalOracleSandbox())
    nothing = replay_contracts(candidate, current=None, delivered=(), sandbox=LocalOracleSandbox())
    assert (
        nothing.ok
        and nothing.evidence == ()
        and isinstance(execute_oracles(candidate, (), sandbox=None, phase="x"), OracleBatch)
    )
    assert Path(candidate).is_dir()
