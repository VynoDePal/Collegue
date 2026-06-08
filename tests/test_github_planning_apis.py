"""Tests P3 (#354) : APIs GitHub labels + milestones + board Projects.

Couche HTTP mockée (pas de réseau) : on vérifie la construction des requêtes,
l'idempotence (ensure_* ne duplique pas) et le parsing. Les écritures réelles sont
couvertes par des tests `integration` (non inclus ici : nécessitent un token).
"""

import pytest

from collegue.tools.base import ToolExecutionError
from collegue.tools.github_commands import LabelCommands, MilestoneCommands, ProjectCommands

# --- labels ---------------------------------------------------------------------


def test_ensure_label_existing_is_idempotent():
    cmd = LabelCommands(token="x")
    cmd._api_get = lambda ep, params=None: [{"name": "feature", "color": "abc", "description": "d"}]
    posted = []
    cmd._api_post = lambda ep, data: posted.append((ep, data))
    label = cmd.ensure_label("o", "r", "Feature")  # casse différente → match
    assert label.name == "feature"
    assert posted == []  # pas de création


def test_ensure_label_creates_when_absent():
    cmd = LabelCommands(token="x")
    cmd._api_get = lambda ep, params=None: []
    calls = {}

    def _post(ep, data):
        calls["ep"] = ep
        calls["data"] = data
        return {"name": data["name"], "color": data["color"], "description": data["description"]}

    cmd._api_post = _post
    label = cmd.ensure_label("o", "r", "epic", color="ff0000", description="grosse")
    assert label.name == "epic" and label.color == "ff0000"
    assert calls["ep"] == "/repos/o/r/labels"
    assert calls["data"]["name"] == "epic"


def test_add_labels_to_issue():
    cmd = LabelCommands(token="x")
    calls = {}

    def _post(ep, data):
        calls["ep"] = ep
        calls["data"] = data
        return [{"name": "feature"}, {"name": "epic"}]

    cmd._api_post = _post
    names = cmd.add_labels_to_issue("o", "r", 7, ["feature", "epic"])
    assert names == ["feature", "epic"]
    assert calls["ep"] == "/repos/o/r/issues/7/labels"
    assert calls["data"] == {"labels": ["feature", "epic"]}


# --- milestones -----------------------------------------------------------------


def test_ensure_milestone_existing_is_idempotent():
    cmd = MilestoneCommands(token="x")
    cmd._api_get = lambda ep, params=None: [{"number": 3, "title": "Phase 1", "state": "open"}]
    posted = []
    cmd._api_post = lambda ep, data: posted.append(data)
    ms = cmd.ensure_milestone("o", "r", "Phase 1")
    assert ms.number == 3
    assert posted == []


def test_ensure_milestone_creates_when_absent():
    cmd = MilestoneCommands(token="x")
    cmd._api_get = lambda ep, params=None: []
    calls = {}

    def _post(ep, data):
        calls["ep"] = ep
        calls["data"] = data
        return {"number": 9, "title": data["title"], "state": "open"}

    cmd._api_post = _post
    ms = cmd.ensure_milestone("o", "r", "Phase 2", due_on="2026-12-31T23:59:59Z")
    assert ms.number == 9 and ms.title == "Phase 2"
    assert calls["ep"] == "/repos/o/r/milestones"
    assert calls["data"]["due_on"] == "2026-12-31T23:59:59Z"


def test_assign_milestone_patches_issue():
    cmd = MilestoneCommands(token="x")
    calls = {}

    def _req(method, endpoint, *, params=None, json_data=None):
        calls["method"] = method
        calls["endpoint"] = endpoint
        calls["json"] = json_data
        return {}

    cmd._request_json = _req
    cmd.assign_milestone("o", "r", 12, 9)
    assert calls["method"] == "PATCH"
    assert calls["endpoint"] == "/repos/o/r/issues/12"
    assert calls["json"] == {"milestone": 9}


# --- projects (board, GraphQL) --------------------------------------------------


def _graphql_router(responses):
    """Construit un faux _api_post qui route selon le contenu de la requête GraphQL."""

    def _post(endpoint, payload):
        assert endpoint == "/graphql"
        query = payload.get("query", "")
        for marker, resp in responses.items():
            if marker in query:
                return resp
        raise AssertionError(f"requête GraphQL inattendue: {query[:60]}")

    return _post


def test_find_project_found():
    cmd = ProjectCommands(token="x")
    cmd._api_post = _graphql_router(
        {
            "projectsV2(first": {
                "data": {
                    "repositoryOwner": {
                        "projectsV2": {"nodes": [{"id": "P1", "number": 4, "title": "Board", "url": "u"}]}
                    }
                }
            }
        }
    )
    proj = cmd.find_project("o", "Board")
    assert proj is not None and proj.id == "P1" and proj.number == 4


def test_find_project_absent_returns_none():
    cmd = ProjectCommands(token="x")
    cmd._api_post = _graphql_router({"projectsV2(first": {"data": {"repositoryOwner": {"projectsV2": {"nodes": []}}}}})
    assert cmd.find_project("o", "Board") is None


def test_ensure_project_idempotent_when_found():
    cmd = ProjectCommands(token="x")
    cmd._api_post = _graphql_router(
        {
            "projectsV2(first": {
                "data": {"repositoryOwner": {"projectsV2": {"nodes": [{"id": "P1", "number": 4, "title": "Board"}]}}}
            }
        }
    )
    proj = cmd.ensure_project("o", "Board")
    assert proj.id == "P1"  # pas de création (aucun marker createProjectV2 routé)


def test_ensure_project_creates_when_absent():
    cmd = ProjectCommands(token="x")
    cmd._api_post = _graphql_router(
        {
            "projectsV2(first": {"data": {"repositoryOwner": {"projectsV2": {"nodes": []}}}},
            "repositoryOwner(login: $login) { id }": {"data": {"repositoryOwner": {"id": "OWNER"}}},
            "createProjectV2": {
                "data": {"createProjectV2": {"projectV2": {"id": "PNEW", "number": 5, "title": "Board", "url": "u"}}}
            },
        }
    )
    proj = cmd.ensure_project("o", "Board")
    assert proj.id == "PNEW" and proj.number == 5


def test_add_issue_to_project():
    cmd = ProjectCommands(token="x")
    cmd._api_post = _graphql_router(
        {"addProjectV2ItemById": {"data": {"addProjectV2ItemById": {"item": {"id": "ITEM1"}}}}}
    )
    assert cmd.add_issue_to_project("PNEW", "ISSUE_NODE") == "ITEM1"


def test_graphql_errors_raise():
    cmd = ProjectCommands(token="x")
    cmd._api_post = lambda ep, data: {"errors": [{"message": "boom"}]}
    with pytest.raises(ToolExecutionError):
        cmd.find_project("o", "Board")


def test_issue_node_id_from_rest():
    cmd = ProjectCommands(token="x")
    cmd._api_get = lambda ep, params=None: {"node_id": "NODE42"}
    assert cmd.issue_node_id("o", "r", 42) == "NODE42"


def test_issue_node_id_missing_raises():
    cmd = ProjectCommands(token="x")
    cmd._api_get = lambda ep, params=None: {}
    with pytest.raises(ToolExecutionError):
        cmd.issue_node_id("o", "r", 42)


# --- robustesse : pagination, idempotence sous course, validation, nulls --------


def test_list_labels_paginates():
    cmd = LabelCommands(token="x")
    page1 = [{"name": f"l{i}", "color": "x"} for i in range(100)]
    page2 = [{"name": "target", "color": "x"}]
    cmd._api_get = lambda ep, params=None: page1 if params.get("page") == 1 else page2
    labels = cmd.list_labels("o", "r")
    assert len(labels) == 101  # 2 pages suivies (sinon "target" manqué)
    assert any(label.name == "target" for label in labels)


def test_ensure_label_recovers_when_create_conflicts():
    # Course / élément hors page : le create échoue (422), mais re-find le trouve.
    cmd = LabelCommands(token="x")
    state = {"calls": 0}

    def _get(ep, params=None):
        state["calls"] += 1
        return [] if state["calls"] <= 1 else [{"name": "epic", "color": "x"}]

    cmd._api_get = _get

    def _post(ep, data):
        raise ToolExecutionError("422 already_exists")

    cmd._api_post = _post
    assert cmd.ensure_label("o", "r", "epic").name == "epic"


def test_ensure_label_none_response_raises():
    cmd = LabelCommands(token="x")
    cmd._api_get = lambda ep, params=None: []
    cmd._api_post = lambda ep, data: None
    with pytest.raises(ToolExecutionError):
        cmd.ensure_label("o", "r", "epic")


def test_label_commands_validate_refs():
    cmd = LabelCommands(token="x")
    with pytest.raises(ToolExecutionError):
        cmd.list_labels("bad/owner", "r")
    with pytest.raises(ToolExecutionError):
        cmd.add_labels_to_issue("o", "..", 1, ["x"])


def test_ensure_milestone_recovers_when_create_conflicts():
    cmd = MilestoneCommands(token="x")
    state = {"calls": 0}

    def _get(ep, params=None):
        state["calls"] += 1
        return [] if state["calls"] <= 1 else [{"number": 7, "title": "Phase 1", "state": "open"}]

    cmd._api_get = _get

    def _post(ep, data):
        raise ToolExecutionError("422 already_exists")

    cmd._api_post = _post
    assert cmd.ensure_milestone("o", "r", "Phase 1").number == 7


def test_ensure_milestone_with_description():
    cmd = MilestoneCommands(token="x")
    cmd._api_get = lambda ep, params=None: []
    captured = {}

    def _post(ep, data):
        captured["data"] = data
        return {"number": 1, "title": data["title"], "state": "open"}

    cmd._api_post = _post
    cmd.ensure_milestone("o", "r", "Phase 1", description="grosse phase")
    assert captured["data"]["description"] == "grosse phase"


def test_find_project_null_owner_raises():
    cmd = ProjectCommands(token="x")
    cmd._api_post = _graphql_router({"projectsV2(first": {"data": {"repositoryOwner": None}}})
    with pytest.raises(ToolExecutionError):
        cmd.find_project("ghost", "Board")


def test_ensure_project_threads_owner_id():
    cmd = ProjectCommands(token="x")
    seen = {}

    def _post(ep, payload):
        query = payload["query"]
        if "projectsV2(first" in query:
            return {"data": {"repositoryOwner": {"projectsV2": {"nodes": []}}}}
        if "repositoryOwner(login: $login) { id }" in query:
            return {"data": {"repositoryOwner": {"id": "OWNER"}}}
        if "createProjectV2" in query:
            seen["vars"] = payload["variables"]
            return {"data": {"createProjectV2": {"projectV2": {"id": "PNEW", "number": 5, "title": "Board"}}}}
        raise AssertionError(query)

    cmd._api_post = _post
    cmd.ensure_project("o", "Board")
    assert seen["vars"] == {"ownerId": "OWNER", "title": "Board"}


def test_ensure_project_null_create_raises():
    cmd = ProjectCommands(token="x")
    cmd._api_post = _graphql_router(
        {
            "projectsV2(first": {"data": {"repositoryOwner": {"projectsV2": {"nodes": []}}}},
            "repositoryOwner(login: $login) { id }": {"data": {"repositoryOwner": {"id": "OWNER"}}},
            "createProjectV2": {"data": {"createProjectV2": None}},
        }
    )
    with pytest.raises(ToolExecutionError):
        cmd.ensure_project("o", "Board")


def test_add_issue_null_item_raises():
    cmd = ProjectCommands(token="x")
    cmd._api_post = _graphql_router({"addProjectV2ItemById": {"data": {"addProjectV2ItemById": {"item": None}}}})
    with pytest.raises(ToolExecutionError):
        cmd.add_issue_to_project("P", "NODE")
