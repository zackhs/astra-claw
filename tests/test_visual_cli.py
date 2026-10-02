from pathlib import Path
from types import SimpleNamespace

import pytest

from astra_engineer.visual_cli import configure


def install(tmp_path):
    root = tmp_path / "licensed-game"
    (root / "data/base").mkdir(parents=True)
    (root / "data/base/info.json").write_text('{}')
    (root / "bin/x64").mkdir(parents=True)
    (root / "bin/x64/factorio").touch()
    return root


def prerequisites(monkeypatch, tmp_path, output="Version: 2.0.77 (build 84539, linux64, full)"):
    tool = tmp_path / "tool"
    tool.touch()
    monkeypatch.setattr("astra_engineer.visual_cli.shutil.which", lambda _: str(tool))
    monkeypatch.setattr("astra_engineer.visual_cli.subprocess.run", lambda *a, **k:
                        SimpleNamespace(returncode=0, stdout=output))


def test_headless_is_not_a_playable_client(tmp_path, monkeypatch):
    root = install(tmp_path)
    prerequisites(monkeypatch, tmp_path, "Version: 2.0.77 (build 84539, linux64, headless)")
    with pytest.raises(ValueError, match="full graphical"):
        configure(tmp_path / "companion", root)
    assert not (tmp_path / "companion").exists()


def test_new_profile_does_not_copy_human_configuration(tmp_path, monkeypatch):
    root = install(tmp_path)
    prerequisites(monkeypatch, tmp_path)
    (root / "player-data.json").write_text('{"service-token":"human-token"}')
    data = tmp_path / "companion"
    configure(data, root, player_name="ClawTest")
    text = (data / "visual-config.json").read_text()
    assert "human-token" not in text
    assert "ClawTest" in text
    assert not (data / "mods").exists()
    assert not (data / "rcon-password").exists()
    assert (data / "visual-config.json").stat().st_mode & 0o777 == 0o600


def test_profile_must_be_outside_readonly_install(tmp_path, monkeypatch):
    root = install(tmp_path)
    prerequisites(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="separate"):
        configure(root / "claw-data", root)


def test_existing_profile_is_preserved(tmp_path, monkeypatch):
    root = install(tmp_path)
    prerequisites(monkeypatch, tmp_path)
    data = tmp_path / "companion"
    configure(data, root)
    original = (data / "visual-config.json").read_bytes()
    with pytest.raises(ValueError, match="already configured"):
        configure(data, root, width=800, height=600)
    assert (data / "visual-config.json").read_bytes() == original
