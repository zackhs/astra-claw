"""Gateway startup regression tests; fake processes and readiness only."""
import json
from pathlib import Path

import pytest

import astra_engineer.visual_claw as claw


def profile(tmp_path):
    directory = tmp_path / "companion"
    directory.mkdir()
    (directory / "visual-claw-workspace").mkdir()
    (directory / "visual-claw-gateway-token").write_text("g" * 40)
    config = {"visual_claw_gateway": "http://127.0.0.1:18941",
              "visual_claw_node": "/fake/node/bin/node", "visual_claw_entry": "/fake/openclaw.mjs"}
    return directory, config


class Process:
    pid = 123456789
    def __init__(self):
        self.exit_code = None
    def poll(self):
        return self.exit_code


def fake_startup(monkeypatch):
    process, launches, stopped = Process(), [], []
    class Probe:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def bind(self, address):
            assert address == ("127.0.0.1", 18941)
    def launch(command, **kwargs):
        launches.append((command, kwargs))
        return process
    def stop(child):
        stopped.append(child)
        child.exit_code = -15
    monkeypatch.setattr(claw.socket, "socket", lambda *args: Probe())
    monkeypatch.setattr(claw.subprocess, "Popen", launch)
    monkeypatch.setattr(claw, "stop_visual_gateway", stop)
    return process, launches, stopped


def test_bad_token_is_rejected_before_any_gateway_process_launch(tmp_path, monkeypatch):
    directory, config = profile(tmp_path)
    _, launches, _ = fake_startup(monkeypatch)
    (directory / "visual-claw-gateway-token").write_text("bad")
    with pytest.raises(ValueError, match="runtime token"):
        claw.start_visual_gateway(directory, config)
    assert launches == []


def test_token_symlink_is_rejected_before_any_gateway_process_launch(tmp_path, monkeypatch):
    directory, config = profile(tmp_path)
    _, launches, _ = fake_startup(monkeypatch)
    token = directory / "visual-claw-gateway-token"
    token.unlink()
    other = tmp_path / "unrelated-token"
    other.write_text("other-owner-token" * 3)
    token.symlink_to(other)
    with pytest.raises(ValueError, match="symbolic link"):
        claw.start_visual_gateway(directory, config)
    assert launches == []


def test_unexpected_readiness_exception_stops_owned_gateway(tmp_path, monkeypatch):
    directory, config = profile(tmp_path)
    process, launches, stopped = fake_startup(monkeypatch)
    def unexpected(*args, **kwargs):
        raise ValueError("Simulated readiness decoding error")
    monkeypatch.setattr(claw, "urlopen", unexpected)
    with pytest.raises(ValueError, match="readiness"):
        claw.start_visual_gateway(directory, config)
    assert len(launches) == 1
    assert stopped == [process]


def test_readiness_timeout_stops_owned_gateway_without_real_wait(tmp_path, monkeypatch):
    directory, config = profile(tmp_path)
    process, launches, stopped = fake_startup(monkeypatch)
    values = iter([0, 1, 46])
    monkeypatch.setattr(claw.time, "monotonic", lambda: next(values))
    monkeypatch.setattr(claw.time, "sleep", lambda *args: None)
    def unavailable(*args, **kwargs):
        raise OSError("No listener yet")
    monkeypatch.setattr(claw, "urlopen", unavailable)
    with pytest.raises(RuntimeError, match="did not become ready"):
        claw.start_visual_gateway(directory, config)
    assert len(launches) == 1 and stopped == [process]


def test_interrupt_during_readiness_also_stops_owned_gateway(tmp_path, monkeypatch):
    directory, config = profile(tmp_path)
    process, _, stopped = fake_startup(monkeypatch)
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(claw, "urlopen", interrupted)
    with pytest.raises(KeyboardInterrupt):
        claw.start_visual_gateway(directory, config)
    assert stopped == [process]
