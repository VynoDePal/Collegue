"""Application de référence W4 : chaque étape est vérifiable seule, les oracles sont rouges PAR ASSERTION puis verts.

Ces tests n'utilisent aucun produit Collègue : ils matérialisent les arbres de ``w4_business_fixture`` et exécutent les oracles
scellés avec le pytest du venv (même commande de production que dans la campagne, via ``LocalOracleSandbox``). Un rouge n'est
valide que s'il est une ASSERTION : une erreur d'import, de collecte, de migration ou un skip fait échouer le test.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
import w4_business_fixture as F
from oracle_sandbox import LocalOracleSandbox

from collegue.executor.oracle import judge_oracle_run, new_nonce, oracle_pytest_command, parse_oracle_report
from collegue.pilot import w4_business as business


pytestmark = pytest.mark.slow


def materialize(root: Path, stage: int, **kwargs) -> Path:
    for rel, content in F.stage_files(stage, **kwargs).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def run_oracle(root: Path, number: int, *, phase: str):
    """Lance l'oracle ``number`` sur ``root`` avec la commande ET le juge de production."""
    nonce = new_nonce()
    command = oracle_pytest_command([(f"oracle-{number}", F.ORACLES[number])], nonce)
    result = LocalOracleSandbox().run_tests(root, command)
    report = parse_oracle_report(result.stdout, nonce)
    assert report is not None, f"rapport d'oracle illisible: {result.stdout[-400:]} {result.stderr[-400:]}"
    return judge_oracle_run(report, f"oracle-{number}", phase=phase)


@pytest.mark.parametrize("number", [1, 2, 3])
def test_each_oracle_is_red_by_assertion_before_its_task_and_green_after_with_the_same_source(tmp_path, number):
    before = materialize(tmp_path / "before", number - 1)
    after = materialize(tmp_path / "after", number)

    red = run_oracle(before, number, phase="preimage")
    green = run_oracle(after, number, phase="candidate")

    assert red.status == "red-assertion", (red.status, red.reason)
    assert red.assertion_failures >= 1 and red.errors == 0 and red.collection_errors == 0 and red.skipped == 0
    assert green.status == "green" and green.failed == 0 and green.errors == 0 and green.skipped == 0


def test_the_seed_is_byte_for_byte_the_pinned_fixture_seed():
    assert sorted(F.SEED) == sorted(business.FIXTURE_SEED_FILES)
    assert F.SEED[".collegue-nightly-fixture"] == business.FIXTURE_SENTINEL
    assert "def health" in F.SEED["app/main.py"] and "/health" in F.SEED["app/main.py"]


def test_the_project_own_tests_pass_at_every_stage(tmp_path):
    import subprocess

    for stage in (1, 2, 3):
        root = materialize(tmp_path / f"s{stage}", stage)
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
            cwd=root,
            capture_output=True,
            text=True,
            env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        assert proc.returncode == 0, (stage, proc.stdout[-500:], proc.stderr[-300:])


def test_business_verifier_accepts_the_complete_reference_and_reads_the_pdf_with_a_real_reader(tmp_path):
    root = materialize(tmp_path / "ok", 3)

    observation = business.verify_business_checkout(str(root), python=sys.executable)

    assert observation.status == "passed", observation.failed
    seen = observation.observations
    assert seen["write.db_existed_before"] is False, "la base de la vérification est réellement vierge"
    assert seen["write.alembic_version"] == ["0001"] and seen["write.rows"] == {"audits": 1, "findings": 2}
    assert seen["write.pdf_reader"].startswith("pypdf ") and seen["write.pdf_pages"] == 1
    assert seen["write.pdf_missing_data"] == [] and seen["reread.reread_status"] == 200
    assert seen["write.pdf_raw_bytes_contain_title"] is False, (
        "le texte n'est PAS recherchable par octets (flux compressé)"
    )
    assert (
        "Audit de sécurité T3" in seen["write.pdf_text_excerpt"] and "Camille Durand" in seen["write.pdf_text_excerpt"]
    )


@pytest.mark.parametrize(
    "stage, failing",
    [
        (0, "write:migration_succeeds"),  # graine : pas de migration (rouge par assertion, pas d'exception)
        (1, "write:audit_created"),  # tâche 1 seule : POST /audits répond 404
        (2, "write:pdf_served"),  # tâches 1-2 : l'export n'existe pas
    ],
)
def test_business_verifier_is_red_before_each_task_by_assertion(tmp_path, stage, failing):
    root = materialize(tmp_path / "pre", stage)

    observation = business.verify_business_checkout(str(root), python=sys.executable)

    assert observation.status == "failed" and failing in observation.failed, observation


def test_a_valid_pdf_with_the_wrong_data_is_rejected_by_the_reader_based_check(tmp_path):
    """Témoin négatif : PDF valide (200, application/pdf, %PDF-) mais données d'un AUTRE audit."""
    root = materialize(tmp_path / "wrong", 3, wrong_data=True)

    observation = business.verify_business_checkout(str(root), python=sys.executable)

    assert observation.status == "failed"
    assert "write:pdf_served" not in observation.failed, "le PDF est servi et valide : seules les DONNÉES sont fausses"
    assert "write:pdf_text_has_the_persisted_audit_data" in observation.failed
    assert observation.observations["write.pdf_status"] == 200
    assert observation.observations["write.pdf_content_type"].startswith("application/pdf")
    # et l'oracle scellé de la tâche 3 est rouge PAR ASSERTION sur ce livrable (ses tests faibles, eux, passent)
    judged = run_oracle(root, 3, phase="candidate")
    assert judged.assertion_failures >= 1 and judged.collection_errors == 0 and judged.errors == 0, judged


def test_a_documentation_only_change_that_drops_the_legal_notice_is_invisible_to_the_data_oracle_but_not_to_the_probe(
    tmp_path,
):
    root = materialize(tmp_path / "incident", 3)
    (root / "docs" / "export_header.md").write_text(F.BROKEN_NOTICE_HEADER, encoding="utf-8")

    probe = business.verify_business_checkout(str(root), python=sys.executable)
    oracle = run_oracle(root, 3, phase="candidate")

    assert oracle.status == "green", "les données persistées sont toujours dans le PDF : l'oracle de données reste vert"
    assert probe.status == "failed" and sorted(probe.failed) == [
        "reread:legal_notice_present",
        "write:legal_notice_present",
    ]
    harmless = materialize(tmp_path / "harmless", 3)
    (harmless / "docs" / "export_header.md").write_text(F.HARMLESS_HEADER, encoding="utf-8")
    assert business.verify_business_checkout(str(harmless), python=sys.executable).status == "passed"


def test_the_pdf_writer_output_is_a_well_formed_pdf_for_an_independent_reader(tmp_path):
    from pypdf import PdfReader

    root = materialize(tmp_path / "pdf", 3)
    sys.path.insert(0, str(root))
    try:
        import importlib

        for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
            del sys.modules[name]
        export = importlib.import_module("app.export")
        audit = {**business.REFERENCE_AUDIT, "id": 7}
        data = export.export_pdf(audit)
    finally:
        sys.path.remove(str(root))
        for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
            del sys.modules[name]

    reader = PdfReader(io.BytesIO(data))
    text = reader.pages[0].extract_text()
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF") and len(reader.pages) == 1
    assert "n° 7" in text and "Camille Durand" in text and "Mot de passe administrateur en clair" in text
    assert b"Camille" not in data, "flux compressé : aucune recherche d'octets ne retrouve les données"
