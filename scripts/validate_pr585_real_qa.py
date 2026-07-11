"""Validation réelle, bornée et sans écriture GitHub du générateur QA de la PR #585."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from collegue.config import settings
from collegue.core.llm.sampling_ctx import LocalSamplingContext
from collegue.planner.acceptance_tests import (
    GENERATOR_NAME,
    RUNNER_NAME,
    generate_acceptance_tests,
    sha256_text,
    validate_pytest_source,
)
from collegue.planner.plan_review import build_plan_preview
from collegue.planner.spec_generator import Spec, persist_spec
from collegue.state import ProjectStateManager


async def main() -> None:
    manager = ProjectStateManager.from_url(settings.STATE_DATABASE_URL, create=False)
    spec = Spec(
        title="Temperature API",
        summary="Micro-API FastAPI déterministe de conversion Celsius vers Fahrenheit.",
        objectives=["Exposer une conversion de température objectivement vérifiable."],
        scope="Backend FastAPI uniquement; aucun frontend, aucune base de données, aucune authentification.",
        constraints=[
            "Application ASGI importable via app.main:app.",
            "Tests exécutables avec pytest.",
        ],
        assumptions=["Python 3.12."],
        acceptance_criteria=[
            "from app.main import app réussit; GET /convert?celsius=0 renvoie HTTP 200 et exactement le JSON "
            "celsius=0.0, fahrenheit=32.0; une valeur celsius non numérique renvoie HTTP 422."
        ],
    )
    project_id = persist_spec(manager, "pr585-real-qa", spec)
    task_id = manager.add_task(
        project_id,
        title="Livrer l’endpoint public de conversion",
        acceptance=spec.acceptance_criteria[0],
        depends_on=[],
    )
    task = manager.get_task(task_id)
    ctx = LocalSamplingContext.from_settings(settings)
    try:
        await generate_acceptance_tests(
            spec,
            [task],
            ctx,
            manager=manager,
            project_id=project_id,
            settings_obj=settings,
            max_tokens=2048,
        )
    finally:
        await ctx.aclose()

    project = manager.get_project(project_id)
    tasks = manager.get_tasks(project_id)
    assert project.acceptance_tests_required is True
    assert len(tasks) == 1
    stored = tasks[0]
    source = stored.acceptance_test_source
    provenance = stored.acceptance_test_provenance
    validate_pytest_source(source)
    assert sha256_text(source) == stored.acceptance_test_sha256
    assert provenance["role"] == "qa"
    assert provenance["requested_provider"] == settings.LLM_PROVIDER_QA
    assert provenance["requested_model"] == settings.LLM_MODEL_QA
    assert provenance["generator"] == GENERATOR_NAME
    assert provenance["runner"] == RUNNER_NAME
    for key in ("prompt_sha256", "spec_sha256", "criteria_sha256", "contract_sha256"):
        assert len(provenance[key]) == 64

    preview = build_plan_preview(manager, project_id)
    assert preview is not None
    report = "\n".join(
        [
            "# Validation réelle QA — PR #585",
            "",
            "- Résultat machine : `PASS`",
            f"- SHA de la PR sous test : `{os.environ['PR585_HEAD_SHA']}`",
            f"- Provider QA : `{provenance['requested_provider']}`",
            f"- Modèle QA : `{provenance['requested_model']}`",
            f"- SHA-256 de l’oracle : `{stored.acceptance_test_sha256}`",
            "- Validation statique : syntaxe pytest, test collectable et assertion observable",
            "- Persistance : source, empreinte et provenance relues depuis SQLite",
            "",
            preview.to_markdown(),
            "",
        ]
    )
    report_path = Path(os.environ.get("QA_REPORT_PATH", "qa-validation/pr585-oracle.md"))
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    report_path.chmod(0o600)

    print("REAL_QA_RUN=PASS")
    print(f"QA_PROVIDER={provenance['requested_provider']}")
    print(f"QA_MODEL={provenance['requested_model']}")
    print(f"ORACLE_SHA256={stored.acceptance_test_sha256}")


if __name__ == "__main__":
    asyncio.run(main())
