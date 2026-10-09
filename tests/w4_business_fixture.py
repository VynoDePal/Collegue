"""Application de référence W4 (FastAPI + SQLite + Alembic) : graine réelle, trois étapes livrables et oracles scellés.

Ce module est une DONNÉE de test (aucun import de l'application, aucune collecte pytest des fichiers qu'il décrit) :

* ``SEED`` : les huit fichiers de la graine immuable de ``VynoDePal/collegue-e2e-fixture`` (commit
  ``8e3691d8e4f311e00d620c9c2ca2d9edbd8b136a``), octet pour octet ;
* ``STAGE_1`` / ``STAGE_2`` / ``STAGE_3`` : ce que livre le « codeur » déterministe à chaque tâche (persistance et migration ;
  création puis lecture d'un audit ; export PDF dont le contenu porte les données de cet audit). Les étapes sont cumulatives :
  ``stage_files(n)`` rend l'arbre attendu APRÈS la tâche ``n`` ;
* ``ORACLE_1..3`` : sources pytest scellées HORS du workspace (produites par le transport QA simulé au plan-time). Chacune est
  ROUGE PAR ASSERTION sur l'arbre qui précède sa tâche et VERTE après, avec la même empreinte ;
* ``WRONG_DATA_STAGE_3`` : un export PDF VALIDE mais qui rend les données d'un AUTRE audit (témoin négatif) ;
* ``BROKEN_NOTICE_HEADER`` : une modification de documentation « autorisée » (faible risque) qui supprime la mention légale de
  l'export — invisible des oracles de données, visible de la sonde de santé d'exploitation (incident contrôlé).

Le PDF est écrit sans bibliothèque (flux compressé, police standard) ; il n'est donc PAS recherchable par octets : seul un vrai
lecteur (``pypdf``) en extrait le texte, ce que font les oracles et la sonde.
"""

from __future__ import annotations

from typing import Dict

# ── graine réelle (octet pour octet) ────────────────────────────────────────────────────────────────────────────────────

SEED: Dict[str, str] = {
    ".collegue-nightly-fixture": "COLLEGUE_NIGHTLY_FIXTURE_V1\n",
    ".gitignore": "__pycache__/\n.pytest_cache/\n.venv/\n*.py[cod]\n",
    "README.md": (
        "# Collègue E2E fixture\n\n"
        "Public, immutable seed repository used only by Collègue's bounded nightly\n"
        "product smoke test. The nightly creates temporary branches, issues and pull\n"
        "requests, then removes them. The `main` seed must not be edited in place.\n"
    ),
    "app/__init__.py": '"""Minimal application package for the Collègue nightly fixture."""\n',
    "app/main.py": (
        '"""Minimal FastAPI application used as the immutable nightly seed."""\n\n'
        "from fastapi import FastAPI\n\n"
        'app = FastAPI(title="Collègue nightly fixture")\n\n\n'
        '@app.get("/health")\n'
        "def health() -> dict[str, str]:\n"
        '    return {"status": "ok"}\n'
    ),
    "requirements.txt": "fastapi==0.116.1\nhttpx==0.28.1\npytest==8.4.1\nuvicorn==0.35.0\n",
    "tests/__init__.py": '"""Tests for the immutable Collègue nightly fixture seed."""\n',
    "tests/test_app.py": (
        "from fastapi.testclient import TestClient\n\n"
        "from app.main import app\n\n\n"
        "client = TestClient(app)\n\n\n"
        "def test_health() -> None:\n"
        '    response = client.get("/health")\n\n'
        "    assert response.status_code == 200\n"
        '    assert response.json() == {"status": "ok"}\n'
    ),
}

# ── tâche 1 : persistance et migration ──────────────────────────────────────────────────────────────────────────────────

STAGE_1: Dict[str, str] = {
    "requirements.txt": "alembic>=1.13\nfastapi==0.116.1\nhttpx==0.28.1\npytest==8.4.1\nsqlalchemy>=2.0\nuvicorn==0.35.0\n",
    # Artefacts de MESURE ignorés dès la tâche 1 (donc avant toute mesure) : `.coverage` (binaire) ne doit jamais être publié.
    ".gitignore": "__pycache__/\n.pytest_cache/\n.venv/\n*.py[cod]\n*.db\n.coverage\nhtmlcov/\n",
    "alembic.ini": "[alembic]\nscript_location = migrations\nprepend_sys_path = .\n",
    "app/db.py": '''"""Accès à la base SQLite : l'URL vient de l'environnement (``DATABASE_URL``), jamais d'un fichier versionné."""

from __future__ import annotations

import os
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event

DEFAULT_URL = "sqlite:///./audits.db"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


@lru_cache(maxsize=8)
def _engine(url: str) -> Engine:
    engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _foreign_keys(dbapi_connection, _record) -> None:
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

    return engine


def get_engine() -> Engine:
    """Moteur de l'URL COURANTE (relue à chaque appel : un redémarrage ou un autre processus voit la même base)."""
    return _engine(database_url())
''',
    "migrations/env.py": '''"""Environnement Alembic : applique les révisions sur ``DATABASE_URL`` (en ligne uniquement)."""

from alembic import context
from sqlalchemy import create_engine, pool

from app.db import database_url


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("les migrations de cette application s'exécutent en ligne")
run_migrations_online()
''',
    "migrations/versions/0001_create_audits.py": '''"""tables audits et findings

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audits",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("auditor", sa.String(120), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
    )
    op.create_table(
        "findings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("audit_id", sa.Integer(), sa.ForeignKey("audits.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("findings")
    op.drop_table("audits")
''',
    "tests/test_migration.py": """import os
import sqlite3
import subprocess
import sys


def test_migration_creates_the_audit_tables_on_an_empty_database(tmp_path) -> None:
    database = tmp_path / "empty.db"
    assert not database.exists()
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{database}")

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], env=env, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert {"audits", "findings", "alembic_version"} <= tables
    assert version == ("0001",)
""",
}

# ── tâche 2 : création puis lecture d'un audit ──────────────────────────────────────────────────────────────────────────

STAGE_2: Dict[str, str] = {
    "app/repository.py": '''"""Persistance des audits (SQL explicite : le schéma appartient aux migrations)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import Engine, text


def create_audit(engine: Engine, title: str, auditor: str, summary: str, findings: List[Dict[str, str]]) -> int:
    with engine.begin() as connection:
        result = connection.execute(
            text("INSERT INTO audits (title, auditor, summary) VALUES (:title, :auditor, :summary)"),
            {"title": title, "auditor": auditor, "summary": summary},
        )
        audit_id = int(result.lastrowid)
        for position, finding in enumerate(findings, start=1):
            connection.execute(
                text(
                    "INSERT INTO findings (audit_id, position, severity, description) "
                    "VALUES (:audit_id, :position, :severity, :description)"
                ),
                {
                    "audit_id": audit_id,
                    "position": position,
                    "severity": finding["severity"],
                    "description": finding["description"],
                },
            )
    return audit_id


def get_audit(engine: Engine, audit_id: int) -> Optional[Dict[str, Any]]:
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT id, title, auditor, summary FROM audits WHERE id = :id"), {"id": audit_id}
        ).first()
        if row is None:
            return None
        findings = connection.execute(
            text("SELECT severity, description FROM findings WHERE audit_id = :id ORDER BY position"), {"id": audit_id}
        ).all()
    return {
        "id": row.id,
        "title": row.title,
        "auditor": row.auditor,
        "summary": row.summary,
        "findings": [{"severity": f.severity, "description": f.description} for f in findings],
    }
''',
    "app/main.py": '''"""Application FastAPI : santé, création et consultation d'audits."""

from typing import List, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app import repository
from app.db import get_engine

app = FastAPI(title="Collègue nightly fixture")


class FindingIn(BaseModel):
    severity: Literal["low", "medium", "high"]
    description: str = Field(min_length=1)


class AuditIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    auditor: str = Field(min_length=1, max_length=120)
    summary: str = ""
    findings: List[FindingIn] = []


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/audits", status_code=201)
def create_audit(payload: AuditIn) -> dict:
    audit_id = repository.create_audit(
        get_engine(), payload.title, payload.auditor, payload.summary, [f.model_dump() for f in payload.findings]
    )
    return repository.get_audit(get_engine(), audit_id)


@app.get("/audits/{audit_id}")
def read_audit(audit_id: int) -> dict:
    audit = repository.get_audit(get_engine(), audit_id)
    if audit is None:
        raise HTTPException(status_code=404, detail="audit not found")
    return audit
''',
    "tests/test_audits.py": """import os
import subprocess
import sys

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch) -> TestClient:
    url = f"sqlite:///{tmp_path / 'audits.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=dict(os.environ), check=True)
    from app.main import app

    return TestClient(app)


def test_create_then_read_an_audit(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    payload = {"title": "Audit A", "auditor": "Alice", "summary": "RAS", "findings": [{"severity": "low", "description": "x"}]}

    created = client.post("/audits", json=payload)
    read = client.get(f"/audits/{created.json()['id']}")

    assert created.status_code == 201 and read.status_code == 200
    assert read.json() == created.json()
    assert read.json()["findings"] == payload["findings"]


def test_unknown_audit_is_a_404(tmp_path, monkeypatch) -> None:
    assert _client(tmp_path, monkeypatch).get("/audits/999").status_code == 404
""",
}

# ── tâche 3 : export PDF dont le contenu porte les données de l'audit ─────────────────────────────────────────────────

_EXPORT_MODULE = '''"""Export PDF d'un audit : en-tête documentaire (``docs/export_header.md``) + données persistées.

Le PDF est écrit sans bibliothèque (une page A4, police standard, flux compressé) : son texte n'est lisible que par un vrai
lecteur PDF, pas par une recherche d'octets.
"""

from __future__ import annotations

import zlib
from pathlib import Path
from typing import Any, Dict, List

HEADER_TEMPLATE = Path(__file__).resolve().parent.parent / "docs" / "export_header.md"


def _escape(line: str) -> bytes:
    data = line.encode("cp1252", errors="replace")
    return data.replace(b"\\\\", b"\\\\\\\\").replace(b"(", b"\\\\(").replace(b")", b"\\\\)")


def audit_lines(audit: Dict[str, Any], header_template: str) -> List[str]:
    lines = [part for part in header_template.format(**audit).splitlines() if part.strip()]
    lines.append("")
    lines.append(f"Resume : {audit['summary']}" if audit["summary"] else "Resume : (aucun)")
    lines.append("Constats :")
    for index, finding in enumerate(audit["findings"], start=1):
        lines.append(f"{index}. [{finding['severity']}] {finding['description']}")
    if not audit["findings"]:
        lines.append("(aucun constat)")
    return lines


def render_pdf(lines: List[str]) -> bytes:
    text = b"BT /F1 11 Tf 50 790 Td 15 TL\\n" + b"\\n".join(b"(" + _escape(line) + b") Tj T*" for line in lines) + b"\\nET"
    stream = zlib.compress(text)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d /Filter /FlateDecode >>\\nstream\\n" % len(stream) + stream + b"\\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out = bytearray(b"%PDF-1.4\\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\\n" % number + body + b"\\nendobj\\n"
    xref = len(out)
    out += b"xref\\n0 %d\\n0000000000 65535 f \\n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \\n" % offset
    out += b"trailer\\n<< /Size %d /Root 1 0 R >>\\nstartxref\\n%d\\n%%%%EOF\\n" % (len(objects) + 1, xref)
    return bytes(out)


def export_pdf(audit: Dict[str, Any]) -> bytes:
    return render_pdf(audit_lines(audit, HEADER_TEMPLATE.read_text(encoding="utf-8")))
'''

HEADER_DOC = (
    "Rapport d'audit n° {id} : {title}\n"
    "Auditeur : {auditor}\n"
    "CONFIDENTIEL - diffusion restreinte aux destinataires de l'audit\n"
)

# Runbook de déploiement livré avec l'export : il cite des identifiants d'EXEMPLE (valeurs factices publiées dans la documentation
# d'AWS) que le scan de secrets du moteur compte, ce qui donne à une vraie amélioration de DOCUMENTATION un gain MESURÉ.
DEPLOY_DOC = (
    "# Déploiement\n\n"
    "L'export lit le fichier `docs/export_header.md`. Pour publier le service, exporter les identifiants :\n\n"
    "    AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"
    "    AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n\n"
    "Puis lancer `alembic upgrade head` avant le premier démarrage.\n"
)
# Version nettoyée par l'amélioration d'incident : les identifiants disparaissent (gain mesuré par le scan de secrets).
CLEAN_DEPLOY_DOC = (
    "# Déploiement\n\n"
    "L'export lit le fichier `docs/export_header.md`. Les identifiants sont fournis par le coffre de secrets de la "
    "plateforme.\n\n"
    "Lancer `alembic upgrade head` avant le premier démarrage.\n"
)

STAGE_3: Dict[str, str] = {
    "requirements.txt": (
        "alembic>=1.13\nfastapi==0.116.1\nhttpx==0.28.1\npypdf>=5\npytest==8.4.1\nsqlalchemy>=2.0\nuvicorn==0.35.0\n"
    ),
    "docs/export_header.md": HEADER_DOC,
    "docs/deploiement.md": DEPLOY_DOC,
    "app/export.py": _EXPORT_MODULE,
    "app/main.py": STAGE_2["app/main.py"]
    .replace(
        "from fastapi import FastAPI, HTTPException\n",
        "from fastapi import FastAPI, HTTPException, Response\n",
    )
    .replace("from app import repository\n", "from app import export, repository\n")
    + """

@app.get("/audits/{audit_id}/export.pdf")
def export_audit(audit_id: int) -> Response:
    audit = repository.get_audit(get_engine(), audit_id)
    if audit is None:
        raise HTTPException(status_code=404, detail="audit not found")
    return Response(
        content=export.export_pdf(audit),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="audit-{audit_id}.pdf"'},
    )
""",
    "tests/test_export.py": """import io
import os
import subprocess
import sys

from fastapi.testclient import TestClient
from pypdf import PdfReader


def test_export_contains_the_persisted_audit(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'audits.db'}")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=dict(os.environ), check=True)
    from app.main import app

    client = TestClient(app)
    audit = client.post(
        "/audits",
        json={"title": "Audit Q3", "auditor": "Bob", "findings": [{"severity": "high", "description": "Mot de passe en clair"}]},
    ).json()

    response = client.get(f"/audits/{audit['id']}/export.pdf")

    assert response.status_code == 200 and response.headers["content-type"] == "application/pdf"
    text = "\\n".join(page.extract_text() for page in PdfReader(io.BytesIO(response.content)).pages)
    assert "Audit Q3" in text and "Bob" in text and "Mot de passe en clair" in text
""",
}

# ── témoins négatifs et incident ─────────────────────────────────────────────────────────────────────────────────────

# Tests « HTTP 200 + type MIME + signature PDF » seulement : le genre de test qui laisse passer un export aux mauvaises données.
WEAK_EXPORT_TEST = """import os
import subprocess
import sys

from fastapi.testclient import TestClient


def test_export_is_served_as_a_pdf(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'audits.db'}")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=dict(os.environ), check=True)
    from app.main import app

    client = TestClient(app)
    audit = client.post("/audits", json={"title": "Audit Q3", "auditor": "Bob"}).json()

    response = client.get(f"/audits/{audit['id']}/export.pdf")

    assert response.status_code == 200 and response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")
"""

# PDF valide mais données d'un AUTRE audit : l'export ignore l'audit demandé et rend toujours le même contenu constant ;
# ses propres tests (HTTP 200 + MIME + signature) passent, seul l'oracle scellé lit le texte du PDF.
WRONG_DATA_STAGE_3: Dict[str, str] = {
    **STAGE_3,
    "tests/test_export.py": WEAK_EXPORT_TEST,
    "app/export.py": _EXPORT_MODULE.replace(
        'def export_pdf(audit: Dict[str, Any]) -> bytes:\n    return render_pdf(audit_lines(audit, HEADER_TEMPLATE.read_text(encoding="utf-8")))\n',
        "def export_pdf(audit: Dict[str, Any]) -> bytes:\n"
        "    other = {'id': 0, 'title': 'Audit generique', 'auditor': 'Inconnu', 'summary': '', 'findings': []}\n"
        '    return render_pdf(audit_lines(other, HEADER_TEMPLATE.read_text(encoding="utf-8")))\n',
    ),
}

# Modification de DOCUMENTATION (chemin autorisé par la politique de faible risque) : retire la mention légale de l'export.
BROKEN_NOTICE_HEADER = "Rapport d'audit n° {id} : {title}\nAuditeur : {auditor}\n"
# Chemins touchés par l'amélioration d'incident (tous en documentation, donc éligibles à l'auto-merge de faible risque).
INCIDENT_DOCS = {"docs/deploiement.md": CLEAN_DEPLOY_DOC, "docs/export_header.md": BROKEN_NOTICE_HEADER}
# Modification de documentation inoffensive : reformule l'en-tête en gardant la mention légale.
HARMLESS_HEADER = (
    "Rapport d'audit n° {id} : {title}\n"
    "Auditeur : {auditor}\n"
    "CONFIDENTIEL - diffusion restreinte aux destinataires de l'audit (document de travail)\n"
)

# Mention légale que la sonde de santé exige dans le PDF exporté.
LEGAL_NOTICE = "CONFIDENTIEL"

# ── oracles scellés (sources pytest du transport QA simulé) ──────────────────────────────────────────────────────────────
# Chaque oracle s'appuie sur la tâche précédente (dépendance INTÉGRÉE dans la base) et échoue par ASSERTION sur l'arbre qui
# précède sa tâche : sous-processus d'Alembic dont on affirme le code retour, TestClient SANS levée d'exception serveur.

_ORACLE_PRELUDE = """\
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path.cwd()


def migrate(database: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{database}", PYTHONPATH=str(ROOT))
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, capture_output=True, text=True
    )
"""

ORACLE_1 = (
    _ORACLE_PRELUDE
    + """

def test_migration_builds_the_schema_on_a_really_empty_database():
    with tempfile.TemporaryDirectory() as folder:
        database = Path(folder) / "fresh.db"
        assert not database.exists(), "la base de l'oracle doit être réellement vierge"
        result = migrate(database)
        assert result.returncode == 0, f"alembic upgrade head a échoué: {result.stderr[-300:]}"
        with sqlite3.connect(database) as connection:
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            columns = [r[1] for r in connection.execute("PRAGMA table_info(audits)")]
        assert {"audits", "findings"} <= tables
        assert version == ("0001",)
        assert columns == ["id", "title", "auditor", "summary"]
"""
)

ORACLE_2 = (
    _ORACLE_PRELUDE
    + """

def _client(database: Path):
    assert migrate(database).returncode == 0, "la migration de la tâche 1 doit être intégrée dans la base"
    os.environ["DATABASE_URL"] = f"sqlite:///{database}"
    sys.path.insert(0, str(ROOT))
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


PAYLOAD = {
    "title": "Audit de sécurité T3",
    "auditor": "Camille Durand",
    "summary": "Revue des accès",
    "findings": [
        {"severity": "high", "description": "Mot de passe administrateur en clair"},
        {"severity": "low", "description": "En-têtes HTTP manquants"},
    ],
}


def test_audit_is_created_then_read_back_and_survives_a_restart():
    with tempfile.TemporaryDirectory() as folder:
        database = Path(folder) / "audits.db"
        client = _client(database)
        created = client.post("/audits", json=PAYLOAD)
        assert created.status_code == 201, f"POST /audits -> {created.status_code}"
        body = created.json()
        assert body["id"] >= 1 and body["title"] == PAYLOAD["title"] and body["auditor"] == PAYLOAD["auditor"]
        assert body["findings"] == PAYLOAD["findings"]
        read = client.get(f"/audits/{body['id']}")
        assert read.status_code == 200 and read.json() == body
        assert client.get("/audits/987654").status_code == 404
        with sqlite3.connect(database) as connection:  # persisté en base, pas seulement en mémoire
            assert connection.execute("SELECT count(*) FROM audits").fetchone() == (1,)
            assert connection.execute("SELECT count(*) FROM findings").fetchone() == (2,)
        again = _client_again(database)
        assert again.get(f"/audits/{body['id']}").json() == body


def _client_again(database: Path):
    for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        del sys.modules[name]  # « redémarrage » : l'application est ré-importée sur la même base
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app, raise_server_exceptions=False)
"""
)

ORACLE_3 = (
    ORACLE_2.split("def test_audit_is_created", 1)[0]
    + """
def test_exported_pdf_is_readable_and_carries_the_persisted_audit_data():
    import io

    from pypdf import PdfReader

    with tempfile.TemporaryDirectory() as folder:
        client = _client(Path(folder) / "audits.db")
        created = client.post("/audits", json=PAYLOAD).json()
        response = client.get(f"/audits/{created['id']}/export.pdf")
        assert response.status_code == 200, f"GET export.pdf -> {response.status_code}"
        assert response.headers["content-type"].startswith("application/pdf")
        assert response.content.startswith(b"%PDF-")
        text = "\\n".join(page.extract_text() for page in PdfReader(io.BytesIO(response.content)).pages)
        for expected in (
            PAYLOAD["title"],
            PAYLOAD["auditor"],
            "Mot de passe administrateur en clair",
            "En-têtes HTTP manquants",
            f"n° {created['id']}",
        ):
            assert expected in text, f"{expected!r} absent du texte extrait du PDF"
        assert client.get("/audits/987654/export.pdf").status_code == 404
"""
)

# ── socle W5 : documents d'EXEMPLE (identifiants factices) fournis par la fixture, sans aucune implémentation métier ─────────

from pathlib import Path as _Path  # noqa: E402

_SOCLE_DIR = _Path(__file__).resolve().parent / "fixtures" / "w5-business"
RUNBOOK_DOC = (_SOCLE_DIR / "docs" / "runbook-ops.md").read_text(encoding="utf-8")
SOCLE_DEPLOY_DOC = (_SOCLE_DIR / "docs" / "deploiement.md").read_text(encoding="utf-8")
SOCLE: Dict[str, str] = {"docs/runbook-ops.md": RUNBOOK_DOC, "docs/deploiement.md": SOCLE_DEPLOY_DOC}
# Version nettoyée par R04 (le « modèle » retire les identifiants d'exemple du runbook) : même texte sans les deux lignes.
CLEAN_RUNBOOK_DOC = "".join(line for line in RUNBOOK_DOC.splitlines(keepends=True) if "AWS_" not in line).replace(
    "(valeurs factices publiées dans la documentation d'AWS, sans aucun accès réel) :",
    "(fournis par le coffre de secrets) :",
)

STAGE_FILES = {1: STAGE_1, 2: STAGE_2, 3: STAGE_3}
ORACLES = {1: ORACLE_1, 2: ORACLE_2, 3: ORACLE_3}
TITLES = {
    1: "Persistance SQLite et migration Alembic",
    2: "Création puis lecture d'un audit",
    3: "Export PDF d'un audit",
}


def stage_files(stage: int, *, wrong_data: bool = False, socle: bool = False) -> Dict[str, str]:
    """Arbre complet attendu APRÈS la tâche ``stage`` (0 = graine). ``socle=True`` : graine + documents d'exemple du socle W5, que
    les tâches ne réécrivent pas (le socle ne fournit AUCUNE implémentation métier)."""
    files = dict(SEED)
    if socle:
        files.update(SOCLE)
    for number in range(1, stage + 1):
        source = WRONG_DATA_STAGE_3 if (wrong_data and number == 3) else STAGE_FILES[number]
        files.update({k: v for k, v in source.items() if not (socle and k in SOCLE)})
    return files
