"""Static regression guards for committed GitHub Actions workflows."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = tuple(sorted((ROOT / ".github" / "workflows").glob("*.y*ml")))


def _load_workflow(path: Path) -> dict[str, object]:
    # BaseLoader keeps keys such as ``on`` as strings while still validating the
    # YAML structure used by GitHub Actions.
    parsed = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(parsed, dict), f"{path} must contain a YAML mapping"
    return parsed


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_workflow_yaml_is_well_formed(path: Path) -> None:
    _load_workflow(path)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_job_env_does_not_use_runner_context(path: Path) -> None:
    """The runner context is unavailable while ``jobs.<id>.env`` is evaluated."""

    jobs = _load_workflow(path).get("jobs", {})
    assert isinstance(jobs, dict), f"{path}: jobs must be a mapping"
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        env = job.get("env", {})
        if not isinstance(env, dict):
            continue
        for name, value in env.items():
            assert "${{ runner." not in str(value), (
                f"{path}: jobs.{job_id}.env.{name} uses the runner context "
                "before a runner has been allocated"
            )


def test_nightly_paths_are_exported_from_runner_temp_at_runtime() -> None:
    workflow = (ROOT / ".github" / "workflows" / "integration-nightly.yml").read_text(
        encoding="utf-8"
    )

    assert 'root="$RUNNER_TEMP/collegue-nightly"' in workflow
    assert '} >> "$GITHUB_ENV"' in workflow
    for name in (
        "COLLEGUE_NIGHTLY_MANIFEST",
        "STATE_DATABASE_URL",
        "COLLEGUE_HOME",
        "SANDBOX_PIP_CACHE_DIR",
    ):
        assert f"printf '{name}=" in workflow
