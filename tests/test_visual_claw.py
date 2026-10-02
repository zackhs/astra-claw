"""Optional OpenClaw boundary tests; no real Gateway or inference contacted."""
import base64
import json
import signal
import threading
from pathlib import Path

import pytest

from astra_engineer.interruptions import Interrupted
from astra_engineer.visual_agent import TOOLS
from astra_engineer.visual_claw import (
    AGENT, ALLOWED_TOOLS, VisualClawProvider, configure_visual_claw,
    gateway_history, start_visual_gateway, visual_claw_environment,
    stop_visual_gateway,
)


def runtime(tmp_path):
    root = tmp_path / "runtime"
    node = root / "node/bin/node"
    entry = root / "openclaw/node_modules/openclaw/openclaw.mjs"
    node.parent.mkdir(parents=True)
    entry.parent.mkdir(parents=True)
    node.write_text("#!/bin/sh\nexit 0\n")
    node.chmod(0o755)
    entry.write_text("// Fake installed runtime; never launched in tests.\n")
    (root / "node-runtime.json").write_text(json.dumps({"directory": str(root / "node")}))
    return root


class Account:
    def __init__(self):
        self.model_reads = 0

    def models(self):
        self.model_reads += 1
        return [{"slug": "gpt-6-astra"}]

    def access_token(self):
        raise AssertionError("Gateway profile/provider must not obtain the account token")


def image(name):
    return {"role": "user", "content": [{"type": "input_text", "text": "Observation " + name},
            {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(name.encode()).decode()}]}


def test_isolated_profile_keeps_old_configs_and_has_no_host_tools_or_credentials(tmp_path):
    directory = tmp_path / "companion"
    directory.mkdir()
    (directory / "config.json").write_text('{"old_native_profile": true}')
    (directory / "visual-config.json").write_text('{"owner_config": true}')
    updates = configure_visual_claw(directory, runtime(tmp_path))
    assert json.loads((directory / "config.json").read_text()) == {"old_native_profile": True}
    assert json.loads((directory / "visual-config.json").read_text()) == {"owner_config": True}
    assert updates["visual_claw_gateway"] == "http://127.0.0.1:18941"
    assert updates["visual_claw_model_port"] == 8792
    config = json.loads((directory / "visual-claw-state/openclaw.json").read_text())
    assert config["gateway"]["bind"] == "loopback"
    assert config["gateway"]["controlUi"]["enabled"] is False
    assert set(config["tools"]["allow"]) == ALLOWED_TOOLS
    assert {"exec", "browser", "read", "write", "cron", "sessions_spawn"} <= set(config["tools"]["deny"])
    assert config["browser"]["enabled"] is False and config["cron"]["enabled"] is False
    assert config["plugins"]["enabled"] is False
    assert "astra-factorio" not in json.dumps(config)
    assert list(config["models"]["providers"]) == ["factorio-visual"]
    model = config["models"]["providers"]["factorio-visual"]
    assert model["models"][0]["input"] == ["text", "image"]
    assert model["apiKey"] == (directory / "visual-claw-model-token").read_text()
    assert config["gateway"]["auth"]["token"] == (directory / "visual-claw-gateway-token").read_text()
    assert not (directory / "claw-state").exists()
    assert "refresh_token" not in json.dumps(config) and "account.json" not in json.dumps(config)
    assert (directory / "visual-claw-state/openclaw.json").stat().st_mode & 0o777 == 0o600
    assert (directory / "visual-claw-workspace/AGENTS.md").stat().st_mode & 0o777 == 0o600


def test_configure_is_idempotent_and_tokens_are_owner_local(tmp_path):
    install, directory = runtime(tmp_path), tmp_path / "companion"
    configure_visual_claw(directory, install)
    token = (directory / "visual-claw-gateway-token").read_text()
    configure_visual_claw(directory, install)
    assert (directory / "visual-claw-gateway-token").read_text() == token
    for path in (directory / "visual-claw-gateway-token", directory / "visual-claw-model-token"):
        assert path.stat().st_mode & 0o777 == 0o600


def test_environment_does_not_inherit_host_display_secrets_or_other_profiles(tmp_path, monkeypatch):
    directory = tmp_path / "companion"
    config = configure_visual_claw(directory, runtime(tmp_path))
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY", "CODEX_HOME", "DISPLAY", "XAUTHORITY",
                "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH", "OPENCLAW_GATEWAY_TOKEN", "HTTP_PROXY"):
        monkeypatch.setenv(key, "host-secret-or-native-profile")
    env = visual_claw_environment(directory, config)
    assert env["OPENCLAW_STATE_DIR"] == str(directory / "visual-claw-state")
    assert env["OPENCLAW_CONFIG_PATH"] == str(directory / "visual-claw-state/openclaw.json")
    assert env["OPENCLAW_HOME"] == str(directory / "visual-claw-home")
    assert "host-secret-or-native-profile" not in json.dumps(env)
    assert not {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DISPLAY", "XAUTHORITY", "CODEX_HOME"} & env.keys()


def test_gateway_history_keeps_images_as_actual_attachments_on_active_user_message():
    history = [image("old"), {"type": "reasoning", "encrypted_content": "opaque-not-replayed"},
               {"type": "function_call", "namespace": "factorio", "name": "game_action", "call_id": "a", "arguments": '{"action":"key","key":"w"}'},
               {"type": "function_call_output", "call_id": "a", "output": '{"ok":true}'},
               image("recent"), {"role": "user", "content": "Owner steering: use the stone patch"}, image("current")]
    incoming = gateway_history(history)
    final = incoming[-1]
    assert final["type"] == "message" and final["role"] == "user"
    images = [p for p in final["content"] if p["type"] == "input_image"]
    assert len(images) == 2
    assert [base64.b64decode(p["source"]["data"]).decode() for p in images] == ["recent", "current"]
    assert all(p["source"]["type"] == "base64" and p["source"]["media_type"] == "image/png" for p in images)
    assert "image_url" not in json.dumps(incoming)
    assert "opaque-not-replayed" not in json.dumps(incoming)
    assert "Owner steering: use the stone patch" in json.dumps(incoming)
    assert all(not any(p.get("type") == "input_image" for p in i.get("content", []) if isinstance(p, dict)) for i in incoming[:-1] if isinstance(i.get("content"), list))
    call = next(i for i in incoming if i["type"] == "function_call")
    assert "namespace" not in call
    assert next(i for i in incoming if i["type"] == "function_call_output")["call_id"] == "a"


def test_provider_uses_fresh_gateway_sessions_and_full_explicit_visual_history(tmp_path, monkeypatch):
    directory = tmp_path / "companion"
    config = configure_visual_claw(directory, runtime(tmp_path))
    auth, requests = Account(), []

    def capture(self, request, cancel, on_text=None):
        requests.append(request)
        return {"status": "completed", "output": []}

    monkeypatch.setattr("astra_engineer.visual_claw.AstraProvider.stream_request", capture)
    provider = VisualClawProvider(auth, config, directory)
    history = [{"role": "user", "content": "Build belts"}, image("first")]
    provider.respond("Pinned preference: conveyors feed central hub", history, TOOLS, threading.Event())
    provider.respond("Pinned preference: conveyors feed central hub", history + [{"role": "user", "content": "Owner steering: use iron instead"}, image("second")], TOOLS, threading.Event())
    assert auth.model_reads == 1
    assert requests[0].full_url == "http://127.0.0.1:18941/v1/responses"
    assert requests[0].get_header("Authorization") == "Bearer " + (directory / "visual-claw-gateway-token").read_text()
    assert requests[0].get_header("X-openclaw-session-key") != requests[1].get_header("X-openclaw-session-key")
    assert requests[0].get_header("X-openclaw-agent-id") == AGENT
    payload = json.loads(requests[1].data)
    assert payload["model"] == "openclaw/" + AGENT
    assert "central hub" in payload["instructions"]
    assert "Build belts" in json.dumps(payload["input"]) and "use iron instead" in json.dumps(payload["input"])
    assert set(t["name"] for t in payload["tools"]) == ALLOWED_TOOLS


def test_cancelled_turn_does_not_request_models_or_gateway(tmp_path, monkeypatch):
    directory = tmp_path / "companion"
    config = configure_visual_claw(directory, runtime(tmp_path))
    auth = Account()
    provider = VisualClawProvider(auth, config, directory)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(Interrupted):
        provider.respond("instructions", [], TOOLS, cancelled)
    assert auth.model_reads == 0


def test_provider_rejects_host_tools_and_remote_gateway(tmp_path):
    directory = tmp_path / "companion"
    config = configure_visual_claw(directory, runtime(tmp_path))
    provider = VisualClawProvider(Account(), config, directory)
    with pytest.raises(ValueError, match="fixed external"):
        provider.respond("instructions", [], [{"type": "function", "name": "exec"}], threading.Event())
    for url in ("http://example.com:18941", "http://127.0.0.1:18941/other", "http://user:pass@127.0.0.1:18941", "https://127.0.0.1:18941"):
        with pytest.raises(ValueError, match="loopback"):
            VisualClawProvider(Account(), config | {"visual_claw_gateway": url}, directory)


@pytest.mark.parametrize("part", [
    {"type": "input_image", "image_url": "https://example.com/image.png"},
    {"type": "input_file", "file_url": "file:///home/user/account.json"},
])
def test_gateway_rejects_remote_or_file_attachments(part):
    with pytest.raises(ValueError):
        gateway_history([{"role": "user", "content": [part]}])


def test_distinct_ports_required(tmp_path):
    install = runtime(tmp_path)
    with pytest.raises(ValueError, match="distinct"):
        configure_visual_claw(tmp_path / "companion", install, gateway_port=18941, model_port=18941)
    with pytest.raises(ValueError, match="local TCP"):
        configure_visual_claw(tmp_path / "companion", install, gateway_port=80)


def test_start_gateway_uses_only_new_profile_environment_without_launching_real_node(tmp_path, monkeypatch):
    directory = tmp_path / "companion"
    config = configure_visual_claw(directory, runtime(tmp_path))
    captured = []

    class Process:
        pid = 7654
        def poll(self):
            return None

    class Health:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    class Probe:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def bind(self, address):
            assert address == ("127.0.0.1", 18941)

    def launch(command, **kwargs):
        captured.append((command, kwargs))
        return Process()

    monkeypatch.setattr("astra_engineer.visual_claw.socket.socket", lambda *a: Probe())
    monkeypatch.setattr("astra_engineer.visual_claw.subprocess.Popen", launch)
    monkeypatch.setattr("astra_engineer.visual_claw.urlopen", lambda *a, **k: Health())
    process = start_visual_gateway(directory, config)
    assert isinstance(process, Process)
    assert captured[0][0][-2:] == ["gateway", "run"]
    assert captured[0][1]["cwd"] == directory / "visual-claw-workspace"
    assert captured[0][1]["env"]["OPENCLAW_STATE_DIR"] == str(directory / "visual-claw-state")
    assert captured[0][1]["start_new_session"] is True
    assert process._astra_gateway_group == process.pid


def test_gateway_shutdown_escalates_whole_owned_group_even_after_launcher_exit(monkeypatch):
    class Process:
        pid = 7654
        _astra_gateway_group = 7654
        def poll(self):
            return 0  # Launcher exited; its serving child can still be alive.
        def wait(self, timeout):
            return 0

    calls = []
    monkeypatch.setattr("astra_engineer.visual_claw.os.killpg", lambda pid, sig: calls.append((pid, sig)))
    process = Process()
    stop_visual_gateway(process, timeout=0)
    assert calls == [(7654, signal.SIGTERM), (7654, signal.SIGKILL)]
    assert process._astra_gateway_group is None
    stop_visual_gateway(process, timeout=0)
    assert len(calls) == 2


def test_gateway_shutdown_waits_for_workers_and_does_not_stop_unowned_groups(monkeypatch):
    class Process:
        pid = 7654
        _astra_gateway_group = 7654
        def poll(self):
            return 0
        def wait(self, timeout):
            return 0

    calls = []
    probes = []
    def signal_group(pid, sig):
        calls.append((pid, sig))
        if sig == 0:
            probes.append(pid)
            if len(probes) == 2:
                raise ProcessLookupError

    monkeypatch.setattr("astra_engineer.visual_claw.os.killpg", signal_group)
    monkeypatch.setattr("astra_engineer.visual_claw.time.sleep", lambda delay: None)
    process = Process()
    stop_visual_gateway(process)
    assert calls == [(7654, signal.SIGTERM), (7654, 0), (7654, 0)]
    stop_visual_gateway(type("Unowned", (), {"pid": 9999})())
    assert len(calls) == 3
