"""Scanner de fuite de la clé (``scripts/w5_leak_scan.py``) : contenu ET noms, binaires, blocs, encodages, parcours en erreur, ensemble publiable atomique.

Chaque garantie est éprouvée par un cas qui DOIT échouer (fuite, erreur) et un cas sain, avec une fausse clé générée par le test. Les deux
sondes indépendantes du manager (répertoire inaccessible déclaré « clean », clé recopiée depuis un nom de fichier) sont reproduites ici.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import sqlite3
import urllib.parse
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "w5_leak_scan.py"
KEY = "FAKE-" + "k3y-AbCdEf0123456789/+=xyz"  # factice, jamais une vraie clé
FLAT = "FAKE-" + "flat-key-0123456789abcdef"  # sans « / » : peut être un nom de fichier


@pytest.fixture(scope="module")
def scan():
    spec = importlib.util.spec_from_file_location("w5_leak_scan_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Run:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.stage = tmp_path / "publish"
        self.quarantine = tmp_path / "quarantine"
        self.report_path = tmp_path / "diag" / "leak-scan.json"
        self.report_path.parent.mkdir()

    def __call__(self, scan, monkeypatch, capsys, sources, *, key=KEY, no_key=False, chunk=None):
        if key is None:
            monkeypatch.delenv("W5_TEST_KEY", raising=False)
        else:
            monkeypatch.setenv("W5_TEST_KEY", key)
        argv = ["--quarantine", str(self.quarantine), "--stage", str(self.stage), "--report", str(self.report_path)]
        argv += ["--assume-no-key"] if no_key else ["--env", "W5_TEST_KEY"]
        for spec in sources:
            argv += ["--source", spec]
        if chunk:
            argv += ["--chunk-size", str(chunk)]
        code = scan.main(argv)
        captured = capsys.readouterr()
        report = json.loads(self.report_path.read_text()) if self.report_path.exists() else None
        return code, report, captured.out + captured.err

    def published(self):
        return (
            sorted(str(p.relative_to(self.stage)) for p in self.stage.rglob("*") if p.is_file())
            if self.stage.exists()
            else []
        )


@pytest.fixture
def run(tmp_path):
    return Run(tmp_path)


def make(tmp_path, name="out"):
    root = tmp_path / name
    root.mkdir()
    return root


def test_a_clean_tree_is_staged_byte_for_byte_and_nothing_is_quarantined(scan, run, tmp_path, monkeypatch, capsys):
    root = make(tmp_path)
    (root / "campaign.json").write_text('{"ok": true}')
    (root / "sub").mkdir()
    blob = os.urandom(4096)
    (root / "sub" / "blob.bin").write_bytes(blob)
    code, report, _out = run(scan, monkeypatch, capsys, [f"report={root}"])
    assert code == 0 and report["verdict"] == "clean" and report["scanned_files"] == 2 and report["staged_files"] == 2
    assert run.published() == ["report/campaign.json", "report/sub/blob.bin"]
    assert (run.stage / "report" / "sub" / "blob.bin").read_bytes() == blob
    assert not run.quarantine.exists() and (root / "campaign.json").exists()
    assert not (tmp_path / "publish.tmp").exists() or not list((tmp_path / "publish.tmp").iterdir()), (
        "aucun résidu temporaire"
    )


def test_a_key_in_a_text_file_is_excluded_and_quarantined_but_the_rest_is_published(
    scan, run, tmp_path, monkeypatch, capsys
):
    root = make(tmp_path)
    (root / "campaign.txt").write_text(f"trace: Authorization={KEY}\n")
    (root / "preflight.json").write_text("{}")
    code, report, _out = run(scan, monkeypatch, capsys, [f"report={root}"])
    assert code == 1 and report["verdict"] == "leak" and report["leaks"][0]["where"] == "content"
    assert run.published() == ["report/preflight.json"], "le fichier contaminé n'est JAMAIS dans l'ensemble publiable"
    assert not (root / "campaign.txt").exists() and len(list(run.quarantine.iterdir())) == 1


def test_a_key_inside_a_binary_sqlite_registry_is_found_and_not_published(scan, run, tmp_path, monkeypatch, capsys):
    root = make(tmp_path)
    db = root / "registry.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("create table log(msg blob)")
    conn.execute("insert into log values (?)", (b"\x00\x01prefix" + KEY.encode() + b"\xff\xfe",))
    conn.commit()
    conn.close()
    code, report, _o = run(scan, monkeypatch, capsys, [f"registry={root}::*.sqlite3*"])
    assert code == 1 and run.published() == [], "un scanner qui ignore les binaires (grep -I) la manquerait"


def test_the_glob_publishes_only_the_registry_files_not_the_workspaces(scan, run, tmp_path, monkeypatch, capsys):
    root = make(tmp_path)
    (root / "run.sqlite3").write_bytes(b"db")
    (root / "run.sqlite3-wal").write_bytes(b"wal")
    (root / "workspace.txt").write_text("espace de travail")
    code, _report, _o = run(scan, monkeypatch, capsys, [f"registry={root}::*.sqlite3*"])
    assert code == 0 and run.published() == ["registry/run.sqlite3", "registry/run.sqlite3-wal"]


def test_a_key_split_across_two_blocks_is_found(scan, run, tmp_path, monkeypatch, capsys):
    root = make(tmp_path)
    for offset in range(0, 12):
        (root / f"f{offset}.bin").write_bytes(b"x" * (64 - offset) + KEY.encode() + b"y" * 70)
    code, report, _o = run(scan, monkeypatch, capsys, [f"report={root}"], chunk=64)
    assert code == 1 and len(report["leaks"]) == 12 and run.published() == []


@pytest.mark.parametrize(
    "encoder",
    [
        lambda k: urllib.parse.quote(k, safe="").encode(),
        lambda k: base64.b64encode(k.encode()),
        lambda k: base64.b64encode(b"a" + k.encode() + b"b"),
        lambda k: base64.b64encode(b"ab" + k.encode() + b"c"),
    ],
)
def test_encoded_forms_of_the_key_are_found(scan, run, tmp_path, monkeypatch, capsys, encoder):
    root = make(tmp_path)
    (root / "encoded.log").write_bytes(b"data=" + encoder(KEY) + b"&more")
    code, report, _o = run(scan, monkeypatch, capsys, [f"report={root}"])
    assert code == 1 and report["leaks"][0]["forms"] and run.published() == []


# ── noms de fichiers : la clé n'est jamais recopiée (sonde manager) ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("where", ["file", "directory", "nested"])
def test_a_key_in_a_name_is_a_leak_never_copied_never_reproduced_in_any_output(
    scan, run, tmp_path, monkeypatch, capsys, where
):
    root = make(tmp_path)
    if where == "file":
        (root / (FLAT + ".log")).write_text("contenu sain")
    elif where == "directory":
        (root / FLAT).mkdir()
        (root / FLAT / "inner.txt").write_text("sain")
    else:
        (root / "a").mkdir()
        (root / "a" / urllib.parse.quote(FLAT, safe="")).write_text("sain")
    (root / "ok.json").write_text("{}")
    code, report, output = run(scan, monkeypatch, capsys, [f"report={root}"], key=FLAT)
    assert code == 1 and report["leaks"][0]["where"] == "name"
    blob = output + run.report_path.read_text() + json.dumps(run.published())
    for needle in (FLAT, urllib.parse.quote(FLAT, safe=""), base64.b64encode(FLAT.encode()).decode()):
        assert needle not in blob, (
            "la clé ne doit apparaître ni dans la sortie, ni dans le rapport, ni dans l'ensemble publié"
        )
    assert "[REDACTED]" in report["leaks"][0]["path"] and len(report["leaks"][0]["id"]) == 16
    assert run.published() == ["report/ok.json"]
    assert not any(FLAT in str(p) for p in run.quarantine.rglob("*"))


def test_scan_tree_keeps_the_manager_probe_signature_and_reports_no_key_in_the_report(scan, tmp_path):
    root = make(tmp_path)
    (root / (FLAT + ".log")).write_text(FLAT)
    report, code = scan.scan_tree([root], FLAT, tmp_path / "q")
    assert code != 0 and FLAT not in json.dumps(report)


# ── parcours en erreur : jamais « clean » (sonde manager) ─────────────────────────────────────────────────────────────────────────


def test_an_inaccessible_subdirectory_makes_the_analysis_incomplete_and_is_never_published(
    scan, run, tmp_path, monkeypatch, capsys
):
    root = make(tmp_path)
    hidden = root / "inaccessible"
    hidden.mkdir()
    (hidden / "trace.bin").write_bytes(KEY.encode())
    (root / "ok.json").write_text("{}")
    original = os.scandir

    def denied(path):
        if Path(path) == hidden:
            raise PermissionError("simulated inaccessible report directory")
        return original(path)

    monkeypatch.setattr(os, "scandir", denied)
    code, report, output = run(scan, monkeypatch, capsys, [f"report={root}"])
    assert code == 2 and report["verdict"] == "incomplete" and report["unreadable"], report
    assert report["unreadable"][0]["reason"] == "PermissionError"
    assert run.published() == ["report/ok.json"], "ce qui n'a pas été lu n'est jamais publié"
    assert "trace.bin" not in "".join(run.published())


def test_scan_tree_reports_an_inaccessible_directory_as_incomplete_with_the_manager_signature(
    scan, tmp_path, monkeypatch
):
    root = make(tmp_path)
    hidden = root / "inaccessible"
    hidden.mkdir()
    (hidden / "trace.bin").write_bytes(KEY.encode())
    original = os.scandir

    def denied(path):
        if Path(path) == hidden:
            raise PermissionError("x")
        return original(path)

    monkeypatch.setattr(os, "scandir", denied)
    report, code = scan.scan_tree([root], KEY, tmp_path / "quarantine")
    assert code == 2 and report["unreadable"]


def test_an_unreadable_file_is_excluded_and_makes_the_analysis_incomplete(scan, run, tmp_path, monkeypatch, capsys):
    if os.geteuid() == 0:
        pytest.skip("root lit tout : le cas illisible n'est pas éprouvable")
    root = make(tmp_path)
    locked = root / "locked.bin"
    locked.write_bytes(b"data")
    locked.chmod(0)
    (root / "ok.json").write_text("{}")
    try:
        code, report, _o = run(scan, monkeypatch, capsys, [f"report={root}"])
        assert code == 2 and [u["reason"] for u in report["unreadable"]] == ["PermissionError"]
        assert run.published() == ["report/ok.json"]
    finally:
        for candidate in (locked, run.quarantine / "x"):
            if candidate.exists():
                candidate.chmod(0o600)
        for moved in run.quarantine.glob("*") if run.quarantine.exists() else []:
            moved.chmod(0o600)


def test_symlinks_and_irregular_objects_are_removed_and_never_followed(scan, run, tmp_path, monkeypatch, capsys):
    root = make(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text(KEY)
    (root / "link").symlink_to(outside)
    os.mkfifo(root / "pipe")
    (root / "ok.json").write_text("{}")
    code, report, _o = run(scan, monkeypatch, capsys, [f"report={root}"])
    assert code == 0, "ni lien ni fifo n'est une fuite ; ils sont retirés, jamais publiés ni suivis"
    assert sorted(item["path"] for item in report["irregular_removed"]) == ["link", "pipe"]
    assert run.published() == ["report/ok.json"] and not (root / "link").is_symlink()
    assert outside.read_text() == KEY, "la cible du lien n'est ni lue ni modifiée"


# ── ensemble publiable atomique, absence de clé, arguments ───────────────────────────────────────────────────────────────────────


def test_a_crash_during_the_copy_never_leaves_a_partial_unverified_file_in_the_published_set(
    scan, run, tmp_path, monkeypatch, capsys
):
    root = make(tmp_path)
    (root / "a.json").write_text("{}")
    (root / "b.json").write_text('{"x": 1}')
    real_replace = os.replace
    seen = []

    def explode(src, dst):
        seen.append(Path(dst).name)
        if len(seen) == 2:
            raise KeyboardInterrupt("commande interrompue")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(KeyboardInterrupt):
        run(scan, monkeypatch, capsys, [f"report={root}"])
    assert run.published() == ["report/a.json"], (
        "seul un fichier ENTIÈREMENT vérifié est publié ; jamais b.json partiel"
    )
    leftovers = list((tmp_path / "publish.tmp").rglob("*")) if (tmp_path / "publish.tmp").exists() else []
    assert [p for p in leftovers if p.is_file()] == [], "le temporaire partiel est supprimé même sur interruption"


def test_assume_no_key_applies_the_same_nature_rules_and_atomic_staging(scan, run, tmp_path, monkeypatch, capsys):
    root = make(tmp_path)
    (root / "preflight.json").write_text("{}")
    (root / "link").symlink_to(root / "preflight.json")
    code, report, _o = run(scan, monkeypatch, capsys, [f"report={root}"], key=None, no_key=True)
    assert code == 0 and report["key_proof"] == "no-key-assumed" and run.published() == ["report/preflight.json"]
    assert report["forms_searched"] == [] and report["irregular_removed"]


def test_the_key_value_never_appears_in_the_output_the_report_or_the_error(scan, run, tmp_path, monkeypatch, capsys):
    root = make(tmp_path)
    (root / "leak.txt").write_text(KEY)
    code, report, output = run(scan, monkeypatch, capsys, [f"report={root}"])
    assert code == 1
    blob = output + json.dumps(report) + run.report_path.read_text()
    for needle in (KEY, urllib.parse.quote(KEY, safe=""), base64.b64encode(KEY.encode()).decode()):
        assert needle not in blob
    code2, _r, output2 = run(scan, monkeypatch, capsys, [f"report={root}"], key="abc123Z")
    assert code2 == 2 and "trop courte" in output2 and "abc123Z" not in output2


def test_a_missing_key_source_or_misplaced_directory_is_an_incomplete_analysis_and_publishes_nothing(
    scan, run, tmp_path, monkeypatch, capsys
):
    root = make(tmp_path)
    (root / "a.txt").write_text("x")
    assert run(scan, monkeypatch, capsys, [f"report={root}"], key=None)[0] == 2
    assert run.published() == []
    code, report, _o = run(scan, monkeypatch, capsys, [f"report={tmp_path / 'absent'}"])
    assert code == 2 and report["unreadable"][0]["reason"] == "répertoire absent"
    monkeypatch.setenv("W5_TEST_KEY", KEY)
    for stage, quarantine in (
        (root / "p", tmp_path / "q"),
        (tmp_path / "p", root / "q"),
        (tmp_path / "same", tmp_path / "same"),
    ):
        argv = [
            "--env",
            "W5_TEST_KEY",
            "--stage",
            str(stage),
            "--quarantine",
            str(quarantine),
            "--report",
            str(tmp_path / "r.json"),
        ]
        assert scan.main([*argv, "--source", f"report={root}"]) == 2
    assert (
        scan.main(
            [
                "--env",
                "W5_TEST_KEY",
                "--assume-no-key",
                "--stage",
                str(tmp_path / "s"),
                "--quarantine",
                str(tmp_path / "q"),
                "--report",
                str(tmp_path / "r.json"),
                "--source",
                f"report={root}",
            ]
        )
        == 2
    )
    assert (
        scan.main(
            [
                "--stage",
                str(tmp_path / "s"),
                "--quarantine",
                str(tmp_path / "q"),
                "--report",
                str(tmp_path / "r.json"),
                "--source",
                f"report={root}",
            ]
        )
        == 2
    )
    inside = [
        "--env",
        "W5_TEST_KEY",
        "--stage",
        str(tmp_path / "st"),
        "--quarantine",
        str(tmp_path / "q"),
        "--report",
        str(tmp_path / "st" / "r.json"),
    ]
    assert scan.main([*inside, "--source", f"report={root}"]) == 2, (
        "le rapport ne peut pas être dans l'ensemble publiable"
    )


def test_the_script_takes_the_value_from_the_environment_only(scan):
    text = SCRIPT.read_text(encoding="utf-8")
    assert "os.environ.get(args.env" in text and "--value" not in text and "--key" not in text
    assert "print(key" not in text and 'print(f"{key' not in text
