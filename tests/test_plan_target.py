"""Tests du contrat de cible GitHub scellé avec le plan (#588)."""

import pytest

from collegue.planner import PlanTargetError, normalize_plan_sync_config


def test_normalize_plan_sync_config_applies_defaults_and_cleans_strings():
    target = normalize_plan_sync_config(
        {
            "owner": "  acme  ",
            "repo": " app ",
            "labels": [" autonome ", "bug", "autonome", "bug"],
            "milestone_title": " MVP ",
            "board_title": "  ",
            "spec_filename": " docs/SPEC.md ",
            "base_branch": " develop ",
        }
    )

    assert target == {
        "owner": "acme",
        "repo": "app",
        "labels": ["autonome", "bug"],
        "milestone_title": "MVP",
        "board_title": None,
        "spec_filename": "docs/SPEC.md",
        "base_branch": "develop",
    }


def test_normalize_plan_sync_config_default_values():
    assert normalize_plan_sync_config({"owner": "acme", "repo": "app"}) == {
        "owner": "acme",
        "repo": "app",
        "labels": ["autonome"],
        "milestone_title": None,
        "board_title": None,
        "spec_filename": "SPEC.md",
        "base_branch": "main",
    }


@pytest.mark.parametrize("missing", ["owner", "repo"])
def test_normalize_plan_sync_config_requires_repository_coordinates(missing):
    config = {"owner": "acme", "repo": "app"}
    del config[missing]

    with pytest.raises(PlanTargetError, match=missing):
        normalize_plan_sync_config(config)


def test_normalize_plan_sync_config_rejects_unknown_keys():
    with pytest.raises(PlanTargetError, match="token"):
        normalize_plan_sync_config({"owner": "acme", "repo": "app", "token": "secret"})


@pytest.mark.parametrize(
    "config",
    [
        None,
        {"owner": "acme", "repo": "app", "labels": "autonome"},
        {"owner": "acme", "repo": "app", "labels": [1]},
        {"owner": "acme", "repo": "app", "labels": ["  "]},
        {"owner": "acme", "repo": "app", "labels": ["x" * 51]},
        {"owner": "acme", "repo": "app", "board_title": 1},
        {"owner": "acme", "repo": "app", "spec_filename": "  "},
    ],
)
def test_normalize_plan_sync_config_rejects_invalid_types(config):
    with pytest.raises(PlanTargetError):
        normalize_plan_sync_config(config)


@pytest.mark.parametrize("key", ["owner", "repo"])
@pytest.mark.parametrize(
    "value",
    ["org/repo", "bad\\name", "bad\x00name", "bad\nname", "bad name", "repo?ref=x", "bad%2Fname", "`x`"],
)
def test_normalize_plan_sync_config_rejects_unsafe_repository_coordinates(key, value):
    config = {"owner": "acme", "repo": "app", key: value}

    with pytest.raises(PlanTargetError, match=key):
        normalize_plan_sync_config(config)


@pytest.mark.parametrize(
    "filename",
    [
        "/SPEC.md",
        "../SPEC.md",
        "docs/../SPEC.md",
        "docs\\SPEC.md",
        "docs//SPEC.md",
        "docs/./SPEC.md",
        "x\x00.py",
        "docs/my SPEC.md",
        "SPEC.md?ref=other",
        "SPEC%2Fother.md",
        "`SPEC`.md",
    ],
)
def test_normalize_plan_sync_config_rejects_unsafe_spec_filename(filename):
    with pytest.raises(PlanTargetError, match="spec_filename"):
        normalize_plan_sync_config({"owner": "acme", "repo": "app", "spec_filename": filename})


def test_normalize_plan_sync_config_allows_no_labels():
    target = normalize_plan_sync_config({"owner": "acme", "repo": "app", "labels": []})

    assert target["labels"] == []


@pytest.mark.parametrize("key,value", [("owner", "."), ("owner", ".."), ("owner", "a:b"), ("repo", "..")])
def test_normalize_plan_sync_config_rejects_non_github_coordinates(key, value):
    with pytest.raises(PlanTargetError, match=key):
        normalize_plan_sync_config({"owner": "acme", "repo": "app", key: value})


@pytest.mark.parametrize("branch", ["", "../main", "feature//x", "bad branch", "main.lock", "@{bad}"])
def test_normalize_plan_sync_config_rejects_unsafe_base_branch(branch):
    with pytest.raises(PlanTargetError, match="base_branch"):
        normalize_plan_sync_config({"owner": "acme", "repo": "app", "base_branch": branch})
