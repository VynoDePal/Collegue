"""Tests #406 : ``COLLEGUE_HOME`` ancré en chemin absolu (durabilité du plafond budget).

Le cumul coût/tokens (``MetricsCollector``) est persisté sous
``$COLLEGUE_HOME/monitoring/`` et le chemin est capturé TÔT (attribut de classe à
l'import) : s'il restait relatif, un ``chdir`` du process déplacerait la
persistance et le plafond dur repartirait de zéro, silencieusement.
"""

from pathlib import Path

from collegue.core.paths import collegue_home, memory_dir, monitoring_dir


def test_collegue_home_default_is_absolute(monkeypatch, tmp_path):
    monkeypatch.delenv("COLLEGUE_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    home = collegue_home()
    assert home.is_absolute()
    assert home == tmp_path / ".collegue"


def test_collegue_home_relative_env_resolved_against_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLEGUE_HOME", "data/colhome")
    monkeypatch.chdir(tmp_path)
    assert collegue_home() == (tmp_path / "data" / "colhome").resolve()


def test_collegue_home_expanduser(monkeypatch):
    monkeypatch.setenv("COLLEGUE_HOME", "~/colhome-406")
    assert collegue_home() == Path.home() / "colhome-406"


def test_subdirs_under_home(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLEGUE_HOME", str(tmp_path))
    assert monitoring_dir() == tmp_path / "monitoring"
    assert memory_dir() == tmp_path / "memory"


def test_captured_path_survives_cwd_change(monkeypatch, tmp_path):
    """#406 (critère d'acceptation) : un consommateur qui capture le chemin une
    fois (façon ``MetricsCollector._PERSIST_DIR`` à l'import) obtient un chemin
    absolu — un ``chdir`` ultérieur du process ne déplace plus la persistance."""
    monkeypatch.delenv("COLLEGUE_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    captured = monitoring_dir()  # capture précoce, comme à l'import
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert captured == tmp_path / ".collegue" / "monitoring"
    assert monitoring_dir() == elsewhere / ".collegue" / "monitoring"  # nouvel appel = nouveau cwd
