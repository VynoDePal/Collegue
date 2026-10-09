"""Garde de PUBLICATION de la fixture de campagne (B23) : aucun contrôle (``.github/``, ``ci/``) ne se publie par le chemin de Collègue.

Constat réel (C47) : GitHub a ACCEPTÉ la fusion d'une PR qui modifiait le workflow malgré ``require_code_owner_review`` (0 approbation).
Et une tête hostile déjà PUBLIÉE peut exécuter son workflow avec un jeton Actions : une barrière avant fusion est trop tardive. Le codeur
n'a ni réseau ni identifiant : la seule porte vers GitHub est ``executor.pr.open_pr``, commun à BUILD, IMPROVE et aux reprises.

Ces tests traversent la VRAIE entrée publique (``run_project_from_settings`` → ``execute_issue`` / ``run_improvement`` → ``open_pr``, vrais
clients GitHub derrière un VRAI dépôt Git distant) et exigent ZÉRO écriture distante sur refus : ni branche, ni fichier, ni PR — le
journal du serveur ne contient AUCUNE requête mutatrice. Chaque famille adverse a son témoin bénin sur le même chemin.
"""

from __future__ import annotations

import os

import pytest
from github_fakes import _AUTHOR_ENV, git
from test_w3_integration_build import FilesAgent, linear_project, open_manager, run_pass, statuses
from w3_remote_bridge import make_bridge
from w5_campaign_support import (
    CODEOWNERS_PATH,
    LOCK_PATH,
    WORKFLOW_PATH,
    attach_identity,
    campaign_source,
)

from collegue.executor.pr import DeliveryFile, DeliverySnapshot
from collegue.pilot import w5_business_policy as fixture_policy


@pytest.fixture
def campaign(tmp_path):
    return campaign_source(tmp_path / "source")


@pytest.fixture
def source(campaign):
    return campaign.path


@pytest.fixture
def bridge(tmp_path, source):
    return make_bridge(tmp_path, source)


@pytest.fixture
def state_url(tmp_path):
    return f"sqlite:///{tmp_path / 'state.db'}"


@pytest.fixture
def anchored(monkeypatch, campaign, tmp_path):
    """Identité de campagne + socle de confiance (manifeste du lancement) : l'état NOMINAL de la campagne."""
    attach_identity(monkeypatch, campaign, directory=tmp_path)
    return campaign


def mutations(bridge):
    return [call for call in bridge.calls if call[0] != "GET"]


def assert_nothing_was_written(bridge):
    assert mutations(bridge) == [], f"AUCUNE requête mutatrice attendue : {mutations(bridge)[:3]}"
    assert bridge.prs == {} and bridge.remote.writes == [], "ZÉRO écriture distante : ni fichier, ni branche, ni PR"
    assert not [b for b in bridge.branches if b.startswith("collegue/")]


async def publish(state_url, source, bridge, files=None, extra=None):
    pid = linear_project(state_url, 1)
    await run_pass(
        state_url,
        source,
        bridge,
        pid,
        agent=FilesAgent(files=files, extra=extra),
        settings={"BUILD_AUTO_MERGE": False, "TASK_MAX_ATTEMPTS": 1},
    )
    return pid


def refused_reason(state_url, pid):
    task = open_manager(state_url).get_tasks(pid)[0]
    assert task.status != "in_review", task.last_error
    return task.last_error or ""


# ── altérations : chacune est refusée AVANT la première écriture ──────────────────────────────────────────────────────────────


def _write(path, text="# altéré par la tâche\n"):
    def apply(workspace):
        target = os.path.join(workspace, path)
        os.makedirs(os.path.dirname(target) or workspace, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text)

    return apply


def _delete(path):
    return lambda workspace: os.remove(os.path.join(workspace, path))


def _rename(old, new):
    def apply(workspace):
        os.makedirs(os.path.dirname(os.path.join(workspace, new)), exist_ok=True)
        os.rename(os.path.join(workspace, old), os.path.join(workspace, new))

    return apply


FORGED_SUCCESS = "name: Fixture tests\non: pull_request\njobs:\n  x:\n    name: Fixture tests\n    runs-on: ubuntu-latest\n    if: false\n"

TAMPERINGS = {
    "workflow-forged-to-succeed": _write(WORKFLOW_PATH, FORGED_SUCCESS),
    "workflow-modified": _write(WORKFLOW_PATH),
    "workflow-added": _write(".github/workflows/second.yml"),
    "workflow-deleted": _delete(WORKFLOW_PATH),
    "workflow-renamed": _rename(WORKFLOW_PATH, ".github/workflows/renamed.yml"),
    "workflow-moved-out": _rename(WORKFLOW_PATH, "docs/workflow.yml"),
    "codeowners-modified": _write(CODEOWNERS_PATH),
    "codeowners-deleted": _delete(CODEOWNERS_PATH),
    "approved-lock-modified": _write(LOCK_PATH, "evil==1.0 \\\n    --hash=sha256:" + "b" * 64 + "\n"),
    "approved-lock-deleted": _delete(LOCK_PATH),
    "ci-file-added": _write("ci/extra.sh"),
    "ci-nested-file-added": _write("ci/tools/deep/hook.py"),
    "case-variant-github": _write(".GitHub/workflows/hidden.yml"),
    "case-variant-ci": _write("CI/requirements-approved.lock"),
    "fullwidth-ci": _write("ｃｉ/hook.sh"),
    "trailing-dot-ci": _write("ci./hook.sh"),
}


@pytest.mark.parametrize("name", sorted(TAMPERINGS))
async def test_a_task_that_alters_a_control_publishes_nothing_at_all(name, anchored, bridge, source, state_url):
    pid = await publish(state_url, source, bridge, extra=TAMPERINGS[name])

    assert_nothing_was_written(bridge)
    reason = refused_reason(state_url, pid)
    assert "LIVRAISON REFUSÉE" in reason and any(token in reason for token in (".github", "ci/")), reason


@pytest.mark.parametrize(
    "files",
    [
        {"app/main.py": "def health():\n    return {'status': 'ok', 'v': 2}\n"},
        {"docs/note.md": "# note\n"},
        {"requirements.txt": "fastapi==0.141.1\n"},
        {"github-notes.md": "# pas un contrôle : le nom ressemble à .github\n"},
        {"ci-notes/readme.md": "# pas le répertoire ci/\n"},
        {"app/ci/helper.py": "VALUE = 1\n"},
    ],
    ids=["code", "docs", "requirements", "lookalike-file", "lookalike-directory", "nested-ci-name"],
)
async def test_the_same_path_publishes_a_task_that_touches_no_control(files, anchored, bridge, source, state_url):
    """Témoin bénin : ce qui n'est pas un contrôle (noms voisins compris) reste publiable, sous la politique de campagne."""
    pid = await publish(state_url, source, bridge, files=files)

    assert sorted(bridge.prs) == [101] and statuses(state_url, pid) == {"T0": "in_review"}, (
        open_manager(state_url).get_tasks(pid)[0].last_error
    )


# ── preuve nécessaire indisponible ou incohérente : refus, jamais « pas de socle = pas de garde » ─────────────────────────────


async def test_without_a_trust_anchor_nothing_is_published_on_the_fixture(
    monkeypatch, campaign, bridge, source, state_url
):
    attach_identity(monkeypatch, campaign)

    pid = await publish(state_url, source, bridge, files={"docs/note.md": "# bénin\n"})

    assert_nothing_was_written(bridge)
    assert "socle de confiance non fourni" in refused_reason(state_url, pid)


@pytest.mark.parametrize(
    "override, needle",
    [
        ({"schema": "autre/1"}, "schéma"),
        ({"repository": "someone/else"}, "dépôt"),
        ({"seed_sha": "c" * 40}, "graine"),
        ({"protected_prefixes": [".github/"]}, "préfixes protégés"),
        ({"bootstrap_sha": "court"}, "bootstrap_sha"),
        ({"bootstrap_sha": "d" * 40}, "illisible"),
    ],
)
async def test_an_inconsistent_trust_anchor_refuses_every_publication(
    override, needle, monkeypatch, campaign, bridge, source, state_url, tmp_path
):
    attach_identity(monkeypatch, campaign, directory=tmp_path, **override)

    pid = await publish(state_url, source, bridge, files={"docs/note.md": "# bénin\n"})

    assert_nothing_was_written(bridge)
    assert needle in refused_reason(state_url, pid)


async def test_an_unreadable_manifest_refuses_every_publication(
    monkeypatch, campaign, bridge, source, state_url, tmp_path
):
    broken = tmp_path / "manifest.json"
    broken.write_text("pas du json", encoding="utf-8")
    attach_identity(monkeypatch, campaign)
    monkeypatch.setenv(fixture_policy.TRUST_ANCHOR_ENV, str(broken))

    pid = await publish(state_url, source, bridge, files={"docs/note.md": "# bénin\n"})

    assert_nothing_was_written(bridge)
    assert "manifeste du socle illisible" in refused_reason(state_url, pid)


async def test_a_bootstrap_that_is_not_a_direct_child_of_the_immutable_seed_is_refused(
    monkeypatch, campaign, bridge, source, state_url, tmp_path
):
    # le « socle » déclaré est la graine elle-même : son parent n'est pas la graine
    attach_identity(monkeypatch, campaign, directory=tmp_path, bootstrap_sha=campaign.seed_sha)

    pid = await publish(state_url, source, bridge, files={"docs/note.md": "# bénin\n"})

    assert_nothing_was_written(bridge)
    assert "descendant direct de la graine" in refused_reason(state_url, pid)


async def test_a_truncated_remote_tree_proves_nothing_and_blocks_the_publication(anchored, bridge, source, state_url):
    bridge.truncate_trees = True

    pid = await publish(state_url, source, bridge, files={"docs/note.md": "# bénin\n"})

    assert_nothing_was_written(bridge)
    assert "illisible" in refused_reason(state_url, pid)


async def test_an_api_outage_while_reading_the_controls_refuses_without_writing_and_is_retried_afterwards(
    anchored, bridge, source, state_url
):
    """Panne PASSAGÈRE de l'API : la livraison est refusée (rien n'est écrit) mais RETENTABLE ; à la tentative suivante elle est publiée."""
    bridge.fail("GET", r"/git/trees/", 503, times=1)
    pid = linear_project(state_url, 1)

    await run_pass(
        state_url,
        source,
        bridge,
        pid,
        agent=FilesAgent(files={"docs/note.md": "# bénin\n"}),
        settings={"BUILD_AUTO_MERGE": False, "TASK_MAX_ATTEMPTS": 2},
    )

    assert sorted(bridge.prs) == [101] and statuses(state_url, pid) == {"T0": "in_review"}
    first_attempt_writes = [c for c in bridge.calls if c[0] != "GET"][:1]
    assert first_attempt_writes and "git/refs" in first_attempt_writes[0][1], (
        "la première écriture n'a lieu qu'à la 2ᵉ tentative, une fois les contrôles relus"
    )


async def test_a_base_whose_controls_already_diverge_from_the_trust_anchor_is_never_published_on(
    monkeypatch, campaign, tmp_path
):
    # la base de la campagne porte déjà un workflow différent de celui du socle de confiance (3ᵉ commit)
    workflow = os.path.join(campaign.path, WORKFLOW_PATH)
    with open(workflow, "a", encoding="utf-8") as handle:
        handle.write("# ajout hors socle\n")
    git(campaign.path, "add", "-A")
    git(
        campaign.path,
        "-c",
        "user.name=x",
        "-c",
        "user.email=x@e.invalid",
        "commit",
        "-q",
        "-m",
        "dérive",
        env=_AUTHOR_ENV,
    )
    bridge = make_bridge(tmp_path, campaign.path)
    state_url = f"sqlite:///{tmp_path / 'state.db'}"
    attach_identity(monkeypatch, campaign, directory=tmp_path)

    pid = await publish(state_url, campaign.path, bridge, files={"docs/note.md": "# bénin\n"})

    assert_nothing_was_written(bridge)
    reason = refused_reason(state_url, pid)
    assert "diverge du socle de confiance" in reason and WORKFLOW_PATH in reason


# ── décalage preuve / payload : ni la liste déclarative ni le payload ne tiennent lieu de preuve ──────────────────────────────


def replace_snapshot(monkeypatch, rewrite):
    """Un attaquant qui contrôlerait le payload entre sa capture et la publication : ``open_pr`` reçoit un manifeste réécrit (et la liste
    de fichiers assortie, pour que la seule cohérence interne ne le refuse pas) — le garde doit juger le contenu et le payload RÉELS."""
    import collegue.executor.pipeline as pipeline

    real = pipeline.open_pr

    def wrapped(workspace, report, issue, owner, repo, **kwargs):
        snapshot = rewrite(kwargs["snapshot"])
        kwargs.update(snapshot=snapshot, files_changed=snapshot.paths)
        return real(workspace, report, issue, owner, repo, **kwargs)

    monkeypatch.setattr(pipeline, "open_pr", wrapped)


async def test_a_payload_that_omits_the_altered_control_is_still_refused_from_the_tested_tree(
    monkeypatch, anchored, bridge, source, state_url
):
    """Liste de changements TRONQUÉE : le payload ne mentionne pas le workflow modifié, le contenu testé (arbre Git) le contient."""
    replace_snapshot(
        monkeypatch,
        lambda snapshot: DeliverySnapshot(
            files=tuple(item for item in snapshot.files if not item.path.startswith(".github")),
            diff_sha256=snapshot.diff_sha256,
        ),
    )

    pid = await publish(
        state_url, source, bridge, files={"docs/note.md": "# bénin\n"}, extra=_write(WORKFLOW_PATH, FORGED_SUCCESS)
    )

    assert_nothing_was_written(bridge)
    reason = refused_reason(state_url, pid)
    assert "modifie les contrôles" in reason and WORKFLOW_PATH in reason, reason


async def test_a_payload_that_targets_a_control_the_proof_does_not_mention_is_refused(
    monkeypatch, anchored, bridge, source, state_url
):
    """Décalage inverse : le contenu testé est propre, mais le payload RÉELLEMENT envoyé écrirait sous ``.github/``."""
    injected = DeliveryFile(path=".github/workflows/injected.yml", operation="update", content="name: x\n")
    replace_snapshot(
        monkeypatch,
        lambda snapshot: DeliverySnapshot(files=snapshot.files + (injected,), diff_sha256=snapshot.diff_sha256),
    )

    pid = await publish(state_url, source, bridge, files={"docs/note.md": "# bénin\n"})

    assert_nothing_was_written(bridge)
    assert "payload" in refused_reason(state_url, pid)


# ── reprise : une PR déjà publiée ne devient jamais sûre parce qu'elle existe ─────────────────────────────────────────────────


async def test_a_resume_on_an_already_published_hostile_pr_is_refused_not_adopted(
    monkeypatch, campaign, bridge, source, state_url, tmp_path
):
    # 1) hors campagne (comportement historique) : la tâche publie sa PR, qui modifie le workflow
    pid = await publish(state_url, source, bridge, extra=_write(WORKFLOW_PATH, FORGED_SUCCESS))
    assert sorted(bridge.prs) == [101], "témoin : hors campagne, l'historique est inchangé"
    published = len(mutations(bridge))
    head = bridge.prs[101]["head"]["sha"]
    # 2) la campagne s'applique ; la tâche est rejouée (crash avant l'écriture du statut) avec le MÊME contenu hostile
    attach_identity(monkeypatch, campaign, directory=tmp_path)
    manager = open_manager(state_url)
    manager.update_task_status(manager.get_tasks(pid)[0].id, "todo")

    await run_pass(
        state_url,
        source,
        bridge,
        pid,
        agent=FilesAgent(extra=_write(WORKFLOW_PATH, FORGED_SUCCESS)),
        settings={"BUILD_AUTO_MERGE": False, "TASK_MAX_ATTEMPTS": 1},
    )

    task = open_manager(state_url).get_tasks(pid)[0]
    assert task.status != "in_review" and "LIVRAISON REFUSÉE" in (task.last_error or ""), task.last_error
    assert len(mutations(bridge)) == published, "aucune écriture de plus : la PR existante n'est pas adoptée"
    assert bridge.prs[101]["head"]["sha"] == head and bridge.merge_calls() == []


def test_the_remote_head_reading_compares_the_published_controls_with_the_trust_anchor(anchored, bridge):
    """Défense en profondeur : la tête DISTANTE (PR préexistante ou tête publiée) est relue, indépendamment du contenu testé local."""
    from collegue.executor.delivery_proof import DeliveryRemoteError
    from collegue.executor.pr import DeliveryRefusedError, _assert_remote_head_controls

    clients = bridge.clients()
    anchor = fixture_policy.load_trust_anchor(
        clients.branches, "fixture", "fixture", os.environ[fixture_policy.TRUST_ANCHOR_ENV]
    )
    bridge.branches["collegue/clean"] = bridge.base_tip
    bridge.branches["collegue/hostile"] = bridge.base_tip
    clean = bridge.write_remote_file("collegue/clean", "docs/note.md", "# bénin\n")
    hostile = bridge.write_remote_file("collegue/hostile", CODEOWNERS_PATH, "* @intrus\n")

    _assert_remote_head_controls(clients, "fixture", "fixture", clean, anchor)
    with pytest.raises(DeliveryRefusedError, match=r"modifie les contrôles .*CODEOWNERS"):
        _assert_remote_head_controls(clients, "fixture", "fixture", hostile, anchor)
    with pytest.raises(DeliveryRemoteError, match="illisibles"):
        _assert_remote_head_controls(clients, "fixture", "fixture", "e" * 40, anchor)
    bridge.truncate_trees = True
    with pytest.raises(DeliveryRefusedError, match="illisible"):
        _assert_remote_head_controls(clients, "fixture", "fixture", clean, anchor)


# ── identification : hors campagne, rien ne change ; aucun paramètre de la contribution ne désactive la garde ─────────────────


async def test_outside_the_campaign_the_historical_publication_is_unchanged(bridge, source, state_url):
    pid = await publish(state_url, source, bridge, extra=_write(WORKFLOW_PATH))

    assert sorted(bridge.prs) == [101] and statuses(state_url, pid) == {"T0": "in_review"}, (
        "autre dépôt : la politique de campagne ne s'applique pas (aucune ancre exigée)"
    )


def test_no_contribution_controlled_parameter_can_disable_the_guard():
    import inspect

    import collegue.executor.pr as pr_module

    parameters = set(inspect.signature(pr_module.open_pr).parameters)
    assert not {
        name for name in parameters if "control" in name or "guard" in name or "policy" in name or "skip" in name
    }
    assert fixture_policy.TRUST_ANCHOR_ENV == "W5_BOOTSTRAP_MANIFEST"


# ── IMPROVE : même garde, même porte (``run_improvement`` → ``open_pr``), puis Phase 5 sous la garde de fusion ────────────────────


@pytest.fixture
def improve_world(tmp_path, campaign, monkeypatch):
    from types import SimpleNamespace

    from github_fakes import make_source_repo  # noqa: F401 - (le dépôt source est celui du socle à deux commits)
    from w5_campaign_support import campaign_mode

    from collegue.state import ProjectStateManager

    url = f"sqlite:///{tmp_path / 'state.db'}"
    manager = ProjectStateManager.from_url(url, create=True)
    bridge = make_bridge(tmp_path, campaign.path)
    campaign_mode(monkeypatch, bridge, campaign=campaign, directory=tmp_path)
    return SimpleNamespace(
        source=campaign.path, manager=manager, url=url, project_id=manager.create_project(name="improve"), bridge=bridge
    )


async def test_an_improvement_that_alters_a_control_publishes_nothing_at_all(improve_world):
    from test_improve_promotion import metrics
    from test_w3_integration_improve import FilesFeature, improve, phase5_hook

    result = await improve(
        improve_world,
        [metrics(80), metrics(90)],
        agent=FilesFeature({"docs/gain.md": "# gain\n", WORKFLOW_PATH: FORGED_SUCCESS, LOCK_PATH: "evil==1\n"}),
        promotion_hook=phase5_hook(improve_world),
    )

    assert result.promoted == [] and mutations(improve_world.bridge) == [], result.rejected
    assert improve_world.bridge.prs == {} and improve_world.bridge.remote.writes == []
    assert any("LIVRAISON REFUSÉE" in reason and ".github" in reason for _dim, reason in result.rejected), (
        result.rejected
    )


async def test_the_same_improvement_without_touching_a_control_is_published_and_merged_by_phase_5(improve_world):
    from test_improve_promotion import metrics
    from test_w3_integration_improve import FilesFeature, improve, phase5_hook

    result = await improve(
        improve_world,
        [metrics(80), metrics(90)],
        agent=FilesFeature({"docs/gain.md": "# gain\n"}),
        promotion_hook=phase5_hook(improve_world),
    )

    assert len(result.promoted) == 1, result.rejected
    assert improve_world.bridge.merged_pr_numbers() == [result.promoted[0].pr_number]


def test_an_api_failure_is_a_retryable_remote_error_while_a_truncated_tree_is_a_definitive_refusal(anchored, bridge):
    from collegue.executor.delivery_proof import DeliveryRemoteError
    from collegue.executor.pr import DeliveryRefusedError, _assert_publication_controls

    clients = bridge.clients()
    bridge.fail("GET", r"/git/commits/", 503, times=1)
    with pytest.raises(
        DeliveryRemoteError, match="illisibles"
    ):  # panne passagère : rien n'a été écrit, la tentative suivante relit
        _assert_publication_controls(None, None, None, clients, "fixture", "fixture", None)

    bridge.truncate_trees = True  # tronqué : une relecture ne prouverait pas davantage
    with pytest.raises(DeliveryRefusedError, match="illisible"):
        _assert_publication_controls(None, None, None, clients, "fixture", "fixture", None)


# ── branche de tête PRÉEXISTANTE (sans PR ouverte) : examinée AVANT toute écriture, jamais réutilisée à l'aveugle (B24) ───────────

HEAD_BRANCH = "collegue/issue-1"
NOTE = {"docs/note.md": "# bénin\n"}


def seed_branch(bridge, files=None, *, from_sha=None):
    """Branche de tête laissée par un tiers ou une publication interrompue ; la préparation n'est pas une écriture de Collègue."""
    bridge.branches[HEAD_BRANCH] = from_sha or bridge.base_tip
    tip = bridge.branches[HEAD_BRANCH]
    for path, text in (files or {}).items():
        tip = bridge.write_remote_file(HEAD_BRANCH, path, text)
    bridge.calls.clear()
    bridge.remote.writes.clear()
    return tip


async def test_a_branch_left_at_the_validated_base_is_continued(anchored, bridge, source, state_url):
    seed_branch(bridge)

    pid = await publish(state_url, source, bridge, files=NOTE)

    assert sorted(bridge.prs) == [101] and statuses(state_url, pid) == {"T0": "in_review"}


async def test_a_clean_branch_ahead_of_the_base_with_another_content_is_refused_without_any_write(
    anchored, bridge, source, state_url
):
    tip = seed_branch(bridge, {"docs/autre.md": "# un autre contenu propre\n"})

    pid = await publish(state_url, source, bridge, files=NOTE)

    assert mutations(bridge) == [] and bridge.prs == {}
    reason = refused_reason(state_url, pid)
    assert "existe déjà sans PR ouverte" in reason and HEAD_BRANCH in reason and tip[:12] in reason, reason
    assert bridge.branches[HEAD_BRANCH] == tip, "le contenu de la branche n'est jamais remplacé en aveugle"


async def test_a_branch_that_already_carries_exactly_the_tested_tree_is_resumed_without_rewriting(
    anchored, bridge, source, state_url
):
    tip = seed_branch(bridge, NOTE)  # publication interrompue AVANT la PR : l'arbre de la branche EST l'arbre testé

    pid = await publish(state_url, source, bridge, files=NOTE)

    assert sorted(bridge.prs) == [101] and statuses(state_url, pid) == {"T0": "in_review"}
    assert bridge.prs[101]["head"]["sha"] == tip, "la branche est reprise telle quelle"
    assert [call for call in mutations(bridge) if "/contents/" in call[1] or "git/refs" in call[1]] == [], (
        "aucune écriture de contenu ni de référence : seule la PR est créée"
    )


async def test_a_residual_branch_after_a_closed_pr_is_examined_not_blindly_reused(
    anchored, bridge, source, state_url, monkeypatch
):
    pid = await publish(state_url, source, bridge, files=NOTE)
    assert sorted(bridge.prs) == [101]
    bridge.prs[101]["state"] = "closed"  # PR fermée, branche conservée
    manager = open_manager(state_url)
    manager.update_task_status(manager.get_tasks(pid)[0].id, "todo")
    tip = bridge.branches[HEAD_BRANCH]
    bridge.calls.clear()

    # même contenu : la branche résiduelle EST l'arbre testé → reprise sans réécriture
    await run_pass(
        state_url,
        source,
        bridge,
        pid,
        agent=FilesAgent(files=NOTE),
        settings={"BUILD_AUTO_MERGE": False, "TASK_MAX_ATTEMPTS": 1},
    )
    assert [c for c in mutations(bridge) if "/contents/" in c[1] or "git/refs" in c[1]] == []
    assert bridge.branches[HEAD_BRANCH] == tip

    # contenu DIFFÉRENT : la branche résiduelle n'est ni réutilisée ni réécrite
    bridge.prs[101]["state"] = "closed"
    for number in [n for n in bridge.prs if n != 101]:
        bridge.prs[number]["state"] = "closed"
    manager = open_manager(state_url)
    manager.update_task_status(manager.get_tasks(pid)[0].id, "todo")
    bridge.calls.clear()
    await run_pass(
        state_url,
        source,
        bridge,
        pid,
        agent=FilesAgent(files={"docs/autre.md": "# autre\n"}),
        settings={"BUILD_AUTO_MERGE": False, "TASK_MAX_ATTEMPTS": 1},
    )
    assert [c for c in mutations(bridge) if "/contents/" in c[1] or "git/refs" in c[1]] == []
    assert bridge.branches[HEAD_BRANCH] == tip
    assert "existe déjà sans PR ouverte" in (open_manager(state_url).get_tasks(pid)[0].last_error or "")


async def test_an_unreadable_branch_tip_is_a_retryable_error_never_an_absence(anchored, bridge, source, state_url):
    bridge.fail("GET", r"/git/ref/heads/collegue/issue-1", 503, times=1)
    pid = linear_project(state_url, 1)

    await run_pass(
        state_url,
        source,
        bridge,
        pid,
        agent=FilesAgent(files=NOTE),
        settings={"BUILD_AUTO_MERGE": False, "TASK_MAX_ATTEMPTS": 1},
    )

    assert mutations(bridge) == [] and bridge.prs == {}, "panne de lecture ≠ branche absente : aucune écriture"
    assert "illisible" in refused_reason(state_url, pid) or "503" in refused_reason(state_url, pid)


async def test_a_branch_that_appears_during_the_creation_is_never_reused_blindly(anchored, bridge, source, state_url):
    """Course : le sommet est ABSENT à l'examen, un tiers crée la branche (avec un workflow altéré) avant ``ensure_branch``."""
    hostile = {"value": None}

    def third_party(server):
        server.branches[HEAD_BRANCH] = server.base_tip
        hostile["value"] = server.write_remote_file(HEAD_BRANCH, WORKFLOW_PATH, "name: x\non: push\njobs: {}\n")

    # 1er GET de la branche = l'examen (404) ; 2ᵉ GET = celui d'``ensure_branch`` : le tiers passe juste avant
    bridge.on_get(r"/git/ref/heads/collegue/issue-1$", third_party, nth=2)
    pid = await publish(state_url, source, bridge, files=NOTE)

    assert hostile["value"], "le scénario de course a bien eu lieu"
    assert [c for c in mutations(bridge) if "/contents/" in c[1]] == [], "aucun contenu écrit sur la branche apparue"
    assert bridge.prs == {}
    assert "n'est pas sur la base validée" in refused_reason(state_url, pid)
    assert bridge.branches[HEAD_BRANCH] == hostile["value"]


async def test_a_branch_that_appears_during_the_creation_at_the_validated_base_is_continued(
    anchored, bridge, source, state_url
):
    bridge.on_get(
        r"/git/ref/heads/collegue/issue-1$",
        lambda server: server.branches.update({HEAD_BRANCH: server.base_tip}),
        nth=2,
    )

    pid = await publish(state_url, source, bridge, files=NOTE)

    assert sorted(bridge.prs) == [101] and statuses(state_url, pid) == {"T0": "in_review"}


async def test_outside_the_campaign_an_existing_branch_is_still_reused_as_before(bridge, source, state_url):
    """Politique hors campagne inchangée : aucune inspection préalable. Branche au niveau de la base ⇒ publiée ; branche en avance ⇒
    réutilisée et écrite comme avant (le refus vient de la seule vérification post-publication, arbre publié ≠ arbre testé)."""
    seed_branch(bridge)
    pid = await publish(state_url, source, bridge, files=NOTE)
    assert sorted(bridge.prs) == [101] and statuses(state_url, pid) == {"T0": "in_review"}


async def test_outside_the_campaign_an_ahead_branch_is_written_into_as_before_and_refused_only_after_publication(
    bridge, source, state_url
):
    seed_branch(bridge, {"docs/autre.md": "# résiduel\n"})

    pid = await publish(state_url, source, bridge, files=NOTE)

    assert [c for c in mutations(bridge) if "/contents/docs/note.md" in c[1]], (
        "historique : la branche est réécrite avant le refus"
    )
    assert bridge.prs == {} and "arbre publié" in refused_reason(state_url, pid)


async def test_an_improvement_branch_left_with_altered_controls_is_not_written_into(improve_world):
    from test_improve_promotion import metrics
    from test_w3_integration_improve import FilesFeature, improve, phase5_hook

    bridge = improve_world.bridge
    probe = await improve(
        improve_world, [metrics(80), metrics(90)], agent=FilesFeature({"docs/gain.md": "# gain\n"}), promotion_hook=None
    )
    assert len(probe.promoted) == 1
    branch = bridge.prs[probe.promoted[0].pr_number]["head"]["ref"]
    # on repart d'un monde vierge : même branche d'amélioration, laissée avec un workflow altéré et SANS PR ouverte
    bridge.prs.clear()
    bridge.branches[branch] = bridge.base_tip
    bridge.write_remote_file(branch, WORKFLOW_PATH, "name: Existing hostile\non: push\njobs: {}\n")
    bridge.calls.clear()
    bridge.remote.writes.clear()

    result = await improve(
        improve_world,
        [metrics(80), metrics(90)],
        agent=FilesFeature({"docs/gain.md": "# gain\n"}),
        promotion_hook=phase5_hook(improve_world),
    )

    assert result.promoted == [] and mutations(bridge) == [], result.rejected
