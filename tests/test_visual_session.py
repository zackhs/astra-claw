import io
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import threading
import time
import zlib

import pytest

from astra_engineer import visual_session as visual


def configuration(tmp_path):
    install = tmp_path / "licensed-factorio"
    (install / "bin/x64").mkdir(parents=True)
    (install / "data/core").mkdir(parents=True)
    binary = install / "bin/x64/factorio"
    binary.write_text("licensed binary fixture")
    binary.chmod(0o700)
    return {"factorio_install": str(install), "bwrap_binary": "/usr/bin/true", "xvfb_binary": "/usr/bin/true",
            "xdotool_binary": "/usr/bin/true", "xauth_binary": "/usr/bin/true"}


class Child:
    def __init__(self, pid, output=None):
        self.pid = pid
        self.stdout = io.BytesIO(output) if output is not None else None
        self.code = None

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        self.code = -signal.SIGTERM
        return self.code


def alive_session(tmp_path, monkeypatch):
    session = visual.VisualSession(configuration(tmp_path), tmp_path / "private")
    session._display = ":42"
    session._display_process = Child(100)
    session._client_process = Child(101)
    calls = []
    monkeypatch.setattr(session, "_tool", lambda arguments, **kwargs: calls.append(arguments))
    return session, calls


def test_sandbox_excludes_host_desktop_credentials_and_game_installs(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XAUTHORITY", "/home/user/.Xauthority")
    monkeypatch.setenv("OPENAI_API_KEY", "host-secret")
    session = visual.VisualSession(configuration(tmp_path), tmp_path / "private")
    session._display = ":42"
    command = session._sandbox(["/factorio/bin/x64/factorio"], game=True)
    assert "--clearenv" in command
    assert ["--tmpfs", "/tmp"] == command[command.index("--tmpfs"):command.index("--tmpfs") + 2]
    assert str(session.socket_directory) in command
    assert "/tmp/.X11-unix" in command
    assert str(session.game_write) in command
    assert str(session.directory) not in command
    assert "/home/user/.Xauthority" not in command
    assert "host-secret" not in command
    assert "unix/:42" in command
    assert "WAYLAND_DISPLAY" not in command
    assert "DBUS_SESSION_BUS_ADDRESS" not in command
    assert "XDG_RUNTIME_DIR" in command and "/run/claw" in command
    assert "LIBGL_ALWAYS_SOFTWARE" in command and "__GLX_VENDOR_LIBRARY_NAME" in command
    assert "--ro-bind" in command and str(session.install) in command
    helper = session._sandbox(["/usr/bin/xdotool", "key", "w"])
    assert str(session.game_write) not in helper
    assert str(session.install) not in helper
    assert "--unshare-all" in command and "--die-with-parent" in command


@pytest.mark.parametrize("overrides", [
    {"runtime_env": {"DISPLAY": ":0"}}, {"runtime_env": {"OPENAI_API_KEY": "secret"}},
    {"runtime_readonly_paths": ["/home"]}, {"runtime_readonly_paths": ["/tmp"]},
    {"binary_prefix": "bash -c"}, {"factorio_binary": "/usr/bin/true"},
    {"visual_width": True}, {"visual_height": 100000},
])
def test_unsafe_configuration_is_rejected(tmp_path, overrides):
    config = configuration(tmp_path)
    config.update(overrides)
    with pytest.raises((ValueError, visual.VisualSessionError)):
        visual.VisualSession(config, tmp_path / "private")


def test_missing_sandbox_is_a_hard_failure(tmp_path):
    config = configuration(tmp_path)
    config["bwrap_binary"] = str(tmp_path / "missing-bwrap")
    with pytest.raises(visual.VisualSessionError, match="bwrap"):
        visual.VisualSession(config, tmp_path / "private")


def test_missing_system_python_fails_without_mounting_host_interpreter(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    home_python = tmp_path / "user-home/conda/bin/python"
    home_python.parent.mkdir(parents=True)
    home_python.write_text("host interpreter must stay outside the game")
    home_python.chmod(0o700)
    monkeypatch.setattr(visual.sys, "executable", str(home_python))
    monkeypatch.setattr(visual, "_SYSTEM_PYTHON_PATHS", (str(home_python), str(tmp_path / "missing-python")))
    with pytest.raises(visual.VisualSessionError, match="system Python"):
        visual.VisualSession(config, tmp_path / "private")


def test_capture_uses_system_python_even_when_companion_runs_in_pyenv(tmp_path, monkeypatch):
    session, _ = alive_session(tmp_path, monkeypatch)
    host_python = str(tmp_path / "user-home/.pyenv/bin/python")
    monkeypatch.setattr(visual.sys, "executable", host_python)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=visual._png(1, 1, [b"\0\0\0"]), stderr=b"")

    monkeypatch.setattr(visual.subprocess, "run", run)
    assert session.screenshot().startswith(b"\x89PNG")
    command = commands[0]
    assert command[-3:] == [session.capture_python, "/visual-session.py", "--capture"]
    assert session.capture_python.startswith("/usr/")
    assert host_python not in command
    assert not any("user-home" in argument for argument in command)


@pytest.mark.parametrize("relationship", ["same", "session_inside_install", "install_inside_session"])
def test_direct_api_never_mounts_session_through_game_install(tmp_path, relationship):
    config = configuration(tmp_path)
    install = Path(config["factorio_install"])
    directory = {"same": install, "session_inside_install": install / "private",
                 "install_inside_session": tmp_path}[relationship]
    with pytest.raises(ValueError, match="must not overlap"):
        visual.VisualSession(config, directory)


@pytest.mark.parametrize("relative", ["account.json", "game-write", "x-sockets", ""])
def test_runtime_assets_cannot_expose_session_credentials_or_sockets(tmp_path, relative):
    config = configuration(tmp_path)
    directory = tmp_path / "private"
    directory.mkdir()
    asset = directory / relative
    if relative.endswith(".json"):
        asset.write_text("private account fixture")
    else:
        asset.mkdir(exist_ok=True)
    config["runtime_readonly_paths"] = [str(asset)]
    with pytest.raises(ValueError, match="selected runtime assets"):
        visual.VisualSession(config, directory)


def test_separate_selected_emulator_assets_remain_usable(tmp_path):
    config = configuration(tmp_path)
    library = tmp_path / "selected-runtime"
    library.mkdir()
    config["runtime_readonly_paths"] = [str(library)]
    session = visual.VisualSession(config, tmp_path / "private")
    assert session.runtime_paths == [library]


@pytest.mark.parametrize("action", [
    {"action": "shell", "command": "id"}, {"action": "key", "key": "w", "command": "id"},
    {"action": "key", "key": "keydown w"}, {"action": "key", "key": "Super_L"},
    {"action": "hold", "key": "w", "seconds": 3}, {"action": "hold", "key": "w", "seconds": float("nan")},
    {"action": "hold", "key": "w", "button": "right", "seconds": 1},
    {"action": "hold", "key": "w", "x": 1, "y": 2, "seconds": 1},
    {"action": "hold", "button": "right", "x": 1, "seconds": 1},
    {"action": "click", "x": -1, "y": 0}, {"action": "click", "x": 1, "y": True},
    {"action": "click", "x": 1, "y": 1, "button": "4"},
    {"action": "click", "x": 1, "y": 1, "count": 1000},
    {"action": "text", "text": "hello\n/c command"}, {"action": "text", "text": "x" * 1001},
    {"action": "scroll", "amount": 11}, {"action": "wait", "seconds": float("inf")},
    {"action": "release", "key": "w"},
])
def test_unbounded_or_non_game_actions_never_send_input(tmp_path, monkeypatch, action):
    session, calls = alive_session(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        session.action(action)
    assert calls == []


def test_hold_releases_keys_after_input_failure(tmp_path, monkeypatch):
    session, calls = alive_session(tmp_path, monkeypatch)

    def tool(arguments, **kwargs):
        calls.append(arguments)
        if arguments[0] == "keydown":
            raise visual.VisualSessionError("connection interrupted")

    monkeypatch.setattr(session, "_tool", tool)
    with pytest.raises(visual.VisualSessionError, match="interrupted"):
        session.action({"action": "hold", "key": "Ctrl+w", "seconds": 0.02})
    assert calls[0] == ["keydown", "ctrl+w"]
    assert calls[-1][0] == "keyup" and "ctrl+w" in calls[-1]
    assert "mouseup" in calls[-1]
    assert session._pressed == set()


def test_failed_release_preserves_held_key_for_recovery(tmp_path, monkeypatch):
    session, calls = alive_session(tmp_path, monkeypatch)
    session._pressed.add("ctrl+w")

    def failed_reset(arguments, **kwargs):
        calls.append(arguments)
        raise subprocess.TimeoutExpired("private key reset", 3)

    monkeypatch.setattr(session, "_tool", failed_reset)
    with pytest.raises(subprocess.TimeoutExpired):
        session.action({"action": "release"})
    assert session._pressed == {"ctrl+w"}
    monkeypatch.setattr(session, "_tool", lambda arguments, **kwargs: calls.append(arguments))
    session.action({"action": "release"})
    assert "ctrl+w" in calls[-1]
    assert session._pressed == set()


def test_atomic_key_failure_is_tracked_before_dispatch(tmp_path, monkeypatch):
    session, calls = alive_session(tmp_path, monkeypatch)

    def tool(arguments, **kwargs):
        calls.append(arguments)
        if arguments[0] == "key":
            assert session._pressed == {"w"}
            raise subprocess.TimeoutExpired("private atomic key", 10)
        assert arguments[:3] == ["keyup", "--delay", "0"]
        assert "w" in arguments and "Return" in arguments

    monkeypatch.setattr(session, "_tool", tool)
    with pytest.raises(subprocess.TimeoutExpired):
        session.action({"action": "key", "key": "w"})
    assert calls[-1][0] == "keyup"
    assert session._pressed == set()


def test_mining_holds_private_pointer_then_releases_button(tmp_path, monkeypatch):
    session, calls = alive_session(tmp_path, monkeypatch)
    result = session.action({"action": "hold", "button": "right", "x": 33, "y": 44, "seconds": 0.02})
    assert calls[0] == ["mousemove", "--sync", "33", "44", "mousedown", "3"]
    assert calls[-1][-2:] == ["mouseup", "3"]
    assert result["ok"]


def test_release_cancels_long_hold_and_always_releases(tmp_path, monkeypatch):
    session, calls = alive_session(tmp_path, monkeypatch)
    held = threading.Event()

    def tool(arguments, **kwargs):
        calls.append(arguments)
        if arguments[0] == "keydown":
            held.set()

    monkeypatch.setattr(session, "_tool", tool)
    results = []
    worker = threading.Thread(target=lambda: results.append(session.action({"action": "hold", "key": "w", "seconds": 2})))
    worker.start()
    assert held.wait(timeout=1)
    started = time.monotonic()
    session.action({"action": "release"})
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert time.monotonic() - started < 1
    assert results[0]["cancelled"]
    assert session._pressed == set()


def test_typing_never_uses_clipboard_and_option_text_stays_data(tmp_path, monkeypatch):
    session, calls = alive_session(tmp_path, monkeypatch)
    session.action({"action": "text", "text": "--window 0 hello $(id)"})
    assert calls[0] == ["type", "--clearmodifiers", "--delay", "0", "--", "--window 0 hello $(id)"]
    assert all("clipboard" not in arg for call in calls for arg in call)


def test_dead_own_client_blocks_observation_and_actions(tmp_path, monkeypatch):
    session, calls = alive_session(tmp_path, monkeypatch)
    session._client_process.code = 1
    assert session.status()["running"] is False
    with pytest.raises(visual.VisualSessionError, match="living client"):
        session.screenshot()
    with pytest.raises(visual.VisualSessionError, match="living client"):
        session.action({"action": "key", "key": "w"})
    assert calls == []


def test_start_detects_sandbox_failure_without_unsandboxed_retry(tmp_path, monkeypatch):
    session = visual.VisualSession(configuration(tmp_path), tmp_path / "private")
    child = Child(112, b"")
    child.code = 1
    commands = []

    def spawn(command, **kwargs):
        commands.append(command)
        return child

    monkeypatch.setattr(visual.subprocess, "Popen", spawn)
    monkeypatch.setattr(session, "_auth_entry", lambda display: None)
    monkeypatch.setattr(session, "_verify_client", lambda: None)
    monkeypatch.setattr(visual.select, "select", lambda *args: ([child.stdout], [], []))
    with pytest.raises(visual.VisualSessionError, match="sandbox failed"):
        session.start()
    assert len(commands) == 1 and commands[0][0] == session.bwrap
    assert "-nolisten" in commands[0] and "tcp" in commands[0]
    assert "local" not in commands[0]  # displayfd must reserve a unique abstract endpoint too.
    assert session._lock_handle is None
    assert session._display_process is None
    assert session.status()["running"] is False
    assert json.loads((session.game_write / "mods/mod-list.json").read_text()) == {"mods": [{"name": "base", "enabled": True}]}


@pytest.mark.parametrize("version", [b"Version: 2.0.77 (build 845, linux64, headless)\n", b"not Factorio"])
def test_direct_session_rejects_headless_before_display_or_client_spawn(tmp_path, monkeypatch, version):
    session = visual.VisualSession(configuration(tmp_path), tmp_path / "private")
    monkeypatch.setattr(session, "_auth_entry", lambda display: None)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=version, stderr=b"")

    monkeypatch.setattr(visual.subprocess, "run", run)
    monkeypatch.setattr(visual.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("non-graphical client spawned"))
    with pytest.raises(visual.VisualSessionError, match="full graphical"):
        session.start()
    assert commands[0][0] == session.bwrap
    assert commands[0][-1] == "--version"
    assert session._lock_handle is None


def test_unauthenticated_display_cannot_launch_client(tmp_path, monkeypatch):
    session = visual.VisualSession(configuration(tmp_path), tmp_path / "private")
    child = Child(100, b"55\n")
    monkeypatch.setattr(session, "_auth_entry", lambda display: None)
    monkeypatch.setattr(session, "_verify_client", lambda: None)
    monkeypatch.setattr(visual.subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(visual.select, "select", lambda *args: ([child.stdout], [], []))
    monkeypatch.setattr(session, "_tool", lambda *args, **kwargs: None)
    monkeypatch.setattr(visual.os, "killpg", lambda *args: None)
    monkeypatch.setattr(session, "_capture", lambda: (_ for _ in ()).throw(visual.VisualSessionError("authentication failed")))
    monkeypatch.setattr(session, "_spawn", lambda *args: pytest.fail("game launched without an authenticated private display"))
    with pytest.raises(visual.VisualSessionError, match="authentication failed"):
        session.start()
    assert session._display_process is None and session._lock_handle is None


def test_wrong_private_frame_dimensions_cannot_launch_client(tmp_path, monkeypatch):
    session = visual.VisualSession(configuration(tmp_path), tmp_path / "private")
    child = Child(100, b"55\n")
    monkeypatch.setattr(session, "_auth_entry", lambda display: None)
    monkeypatch.setattr(session, "_verify_client", lambda: None)
    monkeypatch.setattr(visual.subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(visual.select, "select", lambda *args: ([child.stdout], [], []))
    monkeypatch.setattr(session, "_tool", lambda *args, **kwargs: None)
    monkeypatch.setattr(visual.os, "killpg", lambda *args: None)
    monkeypatch.setattr(session, "_capture", lambda: visual._png(1, 1, [b"\0\0\0"]))
    monkeypatch.setattr(session, "_spawn", lambda *args: pytest.fail("game launched into a mismatched display"))
    with pytest.raises(visual.VisualSessionError, match="dimensions"):
        session.start()


def test_start_uses_real_kernel_lock_not_persisted_pid(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    first = visual.VisualSession(config, tmp_path / "private")
    second = visual.VisualSession(config, tmp_path / "private")
    first.directory.mkdir()
    first._lock_handle = (first.directory / "visual.lock").open("a+")
    visual.fcntl.flock(first._lock_handle, visual.fcntl.LOCK_EX | visual.fcntl.LOCK_NB)
    monkeypatch.setattr(visual.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("duplicate session spawned a child"))
    with pytest.raises(visual.VisualSessionError, match="Another process"):
        second.start("127.0.0.1:34197")
    first.close()


def test_close_only_signals_owned_process_handles(tmp_path, monkeypatch):
    session, calls = alive_session(tmp_path, monkeypatch)
    killed = []
    monkeypatch.setattr(visual.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    session.close()
    assert killed == [(101, signal.SIGTERM), (100, signal.SIGTERM)]
    session.close()
    assert len(killed) == 2


def test_png_encoder_has_real_pixels_and_valid_dimensions():
    image = visual._png(2, 1, [b"\xff\0\0\0\xff\0"])
    assert image[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack("!II", image[16:24])
    assert (width, height) == (2, 1)
    offset = 8
    pixels = None
    while offset < len(image):
        length = struct.unpack("!I", image[offset:offset + 4])[0]
        kind = image[offset + 4:offset + 8]
        data = image[offset + 8:offset + 8 + length]
        crc = struct.unpack("!I", image[offset + 8 + length:offset + 12 + length])[0]
        assert crc == zlib.crc32(kind + data) & 0xffffffff
        if kind == b"IDAT":
            pixels = zlib.decompress(data)
        offset += 12 + length
    assert pixels == b"\0\xff\0\0\0\xff\0"
