"""Scanner de fuite de la clé (``scripts/w5_leak_scan.py``) : binaires, blocs, encodages, quarantaine, rapport sans valeur.

Chaque garantie est éprouvée par un cas qui DOIT échouer (fuite trouvée) et un cas sain (rien trouvé), avec une fausse clé générée par le test.
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


@pytest.fixture(scope="module")
def scan():
    spec = importlib.util.spec_from_file_location("w5_leak_scan_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(scan, tmp_path, monkeypatch, capsys, *roots, key=KEY, chunk=None):
    if key is None:
        monkeypatch.delenv("W5_TEST_KEY", raising=False)
    else:
        monkeypatch.setenv("W5_TEST_KEY", key)
    quarantine = tmp_path / "quarantine"
    report = tmp_path / "leak-report.json"
    argv = ["--env", "W5_TEST_KEY", "--quarantine", str(quarantine), "--report", str(report), *map(str, roots)]
    if chunk:
        argv += ["--chunk-size", str(chunk)]
    code = scan.main(argv)
    captured = capsys.readouterr()
    return code, (json.loads(report.read_text()) if report.exists() else None), captured.out + captured.err, quarantine


def make(tmp_path, name="out"):
    root = tmp_path / name
    root.mkdir()
    return root


def test_a_clean_tree_is_reported_clean_and_nothing_is_moved(scan, tmp_path, monkeypatch, capsys):
    root = make(tmp_path)
    (root / "campaign.json").write_text('{"ok": true}')
    (root / "sub").mkdir()
    (root / "sub" / "blob.bin").write_bytes(os.urandom(4096))
    code, report, output, quarantine = run(scan, tmp_path, monkeypatch, capsys, root)
    assert code == 0 and report["verdict"] == "clean" and report["scanned_files"] == 2 and report["leaks"] == []
    assert (root / "campaign.json").exists() and not quarantine.exists()


def test_a_key_in_a_text_file_is_found_and_the_file_is_quarantined_but_the_rest_is_kept(
    scan, tmp_path, monkeypatch, capsys
):
    root = make(tmp_path)
    (root / "campaign.txt").write_text(f"trace: Authorization={KEY}\n")
    (root / "preflight.json").write_text("{}")
    code, report, output, quarantine = run(scan, tmp_path, monkeypatch, capsys, root)
    assert code == 1 and report["verdict"] == "leak" and [item["path"] for item in report["leaks"]] == ["campaign.txt"]
    assert not (root / "campaign.txt").exists() and (root / "preflight.json").exists(), "preuves saines conservées"
    assert len(list(quarantine.iterdir())) == 1


def test_a_key_inside_a_binary_sqlite_registry_is_found(scan, tmp_path, monkeypatch, capsys):
    root = make(tmp_path)
    db = root / "registry.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("create table log(msg blob)")
    conn.execute("insert into log values (?)", (b"\x00\x01prefix" + KEY.encode() + b"\xff\xfe",))
    conn.commit()
    conn.close()
    code, report, _output, _q = run(scan, tmp_path, monkeypatch, capsys, root)
    assert code == 1 and report["leaks"][0]["path"] == "registry.sqlite3", (
        "un scanner qui ignore les binaires (grep -I) la manquerait"
    )
    assert not db.exists()


def test_a_key_split_across_two_blocks_is_found(scan, tmp_path, monkeypatch, capsys):
    root = make(tmp_path)
    for offset in range(0, 12):
        (root / f"f{offset}.bin").write_bytes(b"x" * (64 - offset) + KEY.encode() + b"y" * 70)
    code, report, _o, _q = run(scan, tmp_path, monkeypatch, capsys, root, chunk=64)
    assert code == 1 and len(report["leaks"]) == 12


@pytest.mark.parametrize(
    "encoder, form",
    [
        (lambda k: urllib.parse.quote(k, safe="").encode(), "url"),
        (lambda k: json.dumps(k)[1:-1].encode(), "raw"),  # aucune échappée pour cette clé : forme brute
        (lambda k: base64.b64encode(k.encode()), "base64+0"),
        (lambda k: base64.b64encode(b"a" + k.encode() + b"b"), "base64+1"),
        (lambda k: base64.b64encode(b"ab" + k.encode() + b"c"), "base64+2"),
    ],
)
def test_encoded_forms_of_the_key_are_found(scan, tmp_path, monkeypatch, capsys, encoder, form):
    root = make(tmp_path)
    (root / "encoded.log").write_bytes(b"data=" + encoder(KEY) + b"&more")
    code, report, _o, _q = run(scan, tmp_path, monkeypatch, capsys, root)
    assert code == 1, form
    assert report["leaks"][0]["forms"], form


def test_the_key_value_never_appears_in_the_output_the_report_or_the_error(scan, tmp_path, monkeypatch, capsys):
    root = make(tmp_path)
    (root / "leak.txt").write_text(KEY)
    code, report, output, _q = run(scan, tmp_path, monkeypatch, capsys, root)
    assert code == 1
    blob = output + json.dumps(report) + (tmp_path / "leak-report.json").read_text()
    for needle in (KEY, urllib.parse.quote(KEY, safe=""), base64.b64encode(KEY.encode()).decode()):
        assert needle not in blob
    code2, _r, output2, _q2 = run(scan, tmp_path, monkeypatch, capsys, root, key="abc123Z")
    assert code2 == 2 and "trop courte" in output2 and "abc123Z" not in output2


def test_a_missing_key_a_missing_directory_or_a_misplaced_quarantine_is_an_incomplete_analysis(
    scan, tmp_path, monkeypatch, capsys
):
    root = make(tmp_path)
    (root / "a.txt").write_text("x")
    assert run(scan, tmp_path, monkeypatch, capsys, root, key=None)[0] == 2
    code, report, _o, _q = run(scan, tmp_path, monkeypatch, capsys, tmp_path / "absent")
    assert code == 2 and report["verdict"] == "incomplete" and report["unreadable"]
    monkeypatch.setenv("W5_TEST_KEY", KEY)
    inside = root / "q"
    assert (
        scan.main(
            ["--env", "W5_TEST_KEY", "--quarantine", str(inside), "--report", str(tmp_path / "r.json"), str(root)]
        )
        == 2
    )


def test_symlinks_and_unreadable_files_are_removed_from_the_uploaded_directory(scan, tmp_path, monkeypatch, capsys):
    root = make(tmp_path)
    secret = tmp_path / "outside.txt"
    secret.write_text(KEY)
    (root / "link").symlink_to(secret)
    unreadable = root / "locked.bin"
    unreadable.write_bytes(b"data")
    unreadable.chmod(0)
    try:
        code, report, _o, _q = run(scan, tmp_path, monkeypatch, capsys, root)
        if os.geteuid() == 0:
            pytest.skip("root lit tout : le cas illisible n'est pas éprouvable")
        assert code == 2 and report["verdict"] == "incomplete"
        assert report["symlinks_removed"] == ["link"] and [u["path"] for u in report["unreadable"]] == ["locked.bin"]
        assert not (root / "link").is_symlink() and not (root / "locked.bin").exists()
        assert secret.read_text() == KEY, "la cible du lien (hors dépôt) n'est ni lue ni modifiée"
    finally:
        if unreadable.exists():
            unreadable.chmod(0o600)


def test_the_script_takes_the_value_from_the_environment_only(scan):
    text = SCRIPT.read_text(encoding="utf-8")
    assert "os.environ.get(args.env" in text and "--value" not in text and "--key" not in text
    assert "print(key" not in text and 'print(f"{key' not in text
