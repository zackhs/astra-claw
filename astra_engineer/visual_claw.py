"""An optional isolated OpenClaw runtime for the mod-free visual teammate.

The Gateway receives only a credential for the owner's loopback model broker.
ChatGPT OAuth credentials remain in the companion's own account store. All game
tools are supplied by VisualAgent and executed outside OpenClaw's host runtime.
"""
from __future__ import annotations

import copy
import json
import os
import secrets
import signal
import socket
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .auth import write_private
from .interruptions import Interrupted
from .provider import AstraProvider


AGENT = "astra-visual"
MODEL_PROVIDER = "factorio-visual"
ALLOWED_TOOLS = {"game_action", "remember", "finish", "ask_owner"}


def _port(value):
    if type(value) is not int or not 1024 <= value <= 65535:
        raise ValueError("Use a local TCP port between 1024 and 65535")
    return value


def _gateway(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("The visual OpenClaw Gateway must be an explicit loopback URL")
    _port(parsed.port)
    return value.rstrip("/")


def _token(path):
    if path.is_symlink():
        raise ValueError("A visual runtime token cannot be a symbolic link")
    if not path.exists():
        path.write_text(secrets.token_urlsafe(40))
    token = path.read_text().strip()
    if not 32 <= len(token) <= 200 or any(char.isspace() for char in token):
        raise ValueError("Invalid private visual runtime token")
    path.chmod(0o600)
    return token


def configure_visual_claw(directory, runtime, gateway_port=18941, model_port=8792):
    """Write a distinct profile and return updates for the caller's visual config.

    This function never edits config.json or visual-config.json and never copies
    an existing account, native Factorio plugin, job queue or Gateway profile.
    ``runtime`` is a user-supplied installation containing node-runtime.json.
    """
    directory, runtime = Path(directory).resolve(), Path(runtime).resolve()
    gateway_port, model_port = _port(gateway_port), _port(model_port)
    if gateway_port == model_port:
        raise ValueError("Gateway and model broker need distinct loopback ports")
    metadata = json.loads((runtime / "node-runtime.json").read_text())
    node = Path(metadata["directory"]).resolve() / "bin/node"
    entry = runtime / "openclaw/node_modules/openclaw/openclaw.mjs"
    if not node.is_file() or not entry.is_file():
        raise ValueError("Supply an installed Node and OpenClaw runtime")
    state = directory / "visual-claw-state"
    workspace = directory / "visual-claw-workspace"
    private_home = directory / "visual-claw-home"
    for path in (directory, state, workspace, private_home):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    gateway_token = _token(directory / "visual-claw-gateway-token")
    model_token = _token(directory / "visual-claw-model-token")
    config = {
        "gateway": {"mode": "local", "bind": "loopback", "port": gateway_port,
                    "auth": {"mode": "token", "token": gateway_token},
                    "controlUi": {"enabled": False},
                    "http": {"endpoints": {"responses": {"enabled": True}}}},
        "models": {"mode": "replace", "providers": {MODEL_PROVIDER: {
            "baseUrl": f"http://127.0.0.1:{model_port}/v1", "apiKey": model_token,
            "api": "openai-responses", "models": [{"id": "gpt-6-astra", "name": "Astra visual Factorio teammate",
                "reasoning": True, "input": ["text", "image"], "contextWindow": 272000, "maxTokens": 24000,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "compat": {"supportsInstructions": True, "supportsReasoningEffort": True,
                           "supportsStore": True, "supportsTools": True}}]}}},
        "agents": {"defaults": {"workspace": str(workspace), "skipBootstrap": True,
                    "model": {"primary": MODEL_PROVIDER + "/gpt-6-astra"}, "thinkingDefault": "high",
                    "timeoutSeconds": 7200, "heartbeat": {"every": "0m"}},
                   "entries": {AGENT: {"default": True, "workspace": str(workspace), "skills": [],
                       "identity": {"name": "Astra", "theme": "A learning visual Factorio teammate", "emoji": "⚙️"}}}},
        "plugins": {"enabled": False, "slots": {"memory": "none"}},
        "tools": {"toolSearch": False, "allow": sorted(ALLOWED_TOOLS),
                  "deny": ["group:runtime", "group:fs", "group:web", "group:ui", "group:sessions",
                           "exec", "process", "read", "write", "edit", "apply_patch", "browser", "canvas", "nodes",
                           "cron", "gateway", "sessions_spawn", "sessions_send", "memory_search", "memory_get"]},
        "browser": {"enabled": False}, "cron": {"enabled": False},
    }
    write_private(state / "openclaw.json", config)
    instructions = workspace / "AGENTS.md"
    instructions.write_text("""# Astra visual Factorio teammate

You play a normal unmodified Factorio client in a dedicated virtual display.
The owner supplies screenshots and four external tools: game_action, remember,
finish and ask_owner. You have no host shell, browser, file or administrator tools.
Inspect the newest image after each bounded action; never invent native world
state or replay a stale plan after steering. Cooperate with human factory work.
The companion preserves the task, direct steering, human preferences and learned
lessons. A Gateway turn is transient. Learning means sourced experience and
visual evidence, not model weight training or verified native routines.
Complete only after seeing the requested result; otherwise pause with ask_owner.
""")
    instructions.chmod(0o600)
    return {"agent_runtime": "openclaw", "model": "gpt-6-astra", "effort": "high",
            "visual_claw_gateway": f"http://127.0.0.1:{gateway_port}", "visual_claw_model_port": model_port,
            "visual_claw_node": str(node), "visual_claw_entry": str(entry),
            "visual_claw_state": str(state), "visual_claw_workspace": str(workspace),
            "visual_claw_home": str(private_home)}


def visual_claw_environment(directory, config):
    """Do not inherit API tokens, display access, or another OpenClaw profile."""
    directory = Path(directory).resolve()
    allowed = ("LANG", "LC_ALL", "TZ", "TMPDIR", "SYSTEMROOT", "WINDIR")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update(OPENCLAW_STATE_DIR=str(directory / "visual-claw-state"),
               OPENCLAW_CONFIG_PATH=str(directory / "visual-claw-state/openclaw.json"),
               OPENCLAW_HOME=str(directory / "visual-claw-home"),
               OPENCLAW_DISABLE_BONJOUR="1", OPENCLAW_SKIP_CHANNELS="1",
               PATH=str(Path(config["visual_claw_node"]).parent) + os.pathsep + os.defpath,
               NO_PROXY="127.0.0.1,localhost")
    return env


def start_visual_gateway(directory, config):
    directory = Path(directory).resolve()
    gateway = _gateway(config["visual_claw_gateway"])
    # Validate local credentials before starting a process that needs cleanup.
    token = _token(directory / "visual-claw-gateway-token")
    # A public health endpoint alone cannot identify our process. Refuse to
    # launch over an occupied port rather than mistake another profile for ours.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", urlsplit(gateway).port))
        except OSError:
            raise RuntimeError("The visual Gateway port is already in use; choose a separate private port") from None
    log = directory / "visual-claw-gateway.log"
    log.touch(mode=0o600)
    log.chmod(0o600)
    with log.open("a") as output:
        process = subprocess.Popen([config["visual_claw_node"], config["visual_claw_entry"], "gateway", "run"],
                                   cwd=directory / "visual-claw-workspace", env=visual_claw_environment(directory, config),
                                   stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                   start_new_session=True)
    # OpenClaw's launcher may respawn a serving Node child and workers. The
    # new session owns the whole group; terminating only this launcher could
    # strand those descendants when its shutdown budget exceeds ours.
    process._astra_gateway_group = process.pid
    deadline = time.monotonic() + 45
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("The private visual OpenClaw Gateway exited; inspect visual-claw-gateway.log")
            try:
                request = Request(gateway + "/health", headers={"Authorization": "Bearer " + token})
                with urlopen(request, timeout=1) as result:
                    if result.status == 200:
                        return process
            except OSError:
                time.sleep(.3)
        raise RuntimeError("The private visual OpenClaw Gateway did not become ready")
    except BaseException:
        stop_visual_gateway(process)
        raise


def stop_visual_gateway(process, *, timeout=10):
    """Stop only the process group created by ``start_visual_gateway``.

    Keep supervising the group if its launcher exits before its workers. This
    helper is shared by startup failure and the CLI's ordinary shutdown path.
    No PID read from a persisted profile or an unrelated process is accepted.
    """
    group = getattr(process, "_astra_gateway_group", None)
    if group is None:
        return
    if group != process.pid:
        raise ValueError("The visual Gateway process group is not owned by this handle")
    try:
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            process.wait(timeout=5)
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            process.poll()  # Reap the launcher without mistaking it for workers.
            try:
                os.killpg(group, 0)
            except ProcessLookupError:
                break
            time.sleep(.05)
        else:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)
    finally:
        process._astra_gateway_group = None


def gateway_history(history):
    """Adapt Responses history to the installed Gateway's media schema.

    The Gateway accepts ``input_image.source`` and only extracts attachments on
    its final active user message. Move the latest two actual images there,
    preserving their observation captions and all bounded conversation text.
    Fresh Gateway sessions avoid accumulated stale image/conversation copies.
    """
    incoming, images = [], []
    for original in history:
        item = copy.deepcopy(original)
        if item.get("role"):
            role = item["role"]
            if role not in {"user", "assistant", "developer"}:
                raise ValueError("Unsupported visual history message role")
            content = item.get("content", "")
            if isinstance(content, list):
                caption = "\n".join(str(p.get("text", "")) for p in content if p.get("type") in {"input_text", "output_text"})
                normalized = []
                for part in content:
                    kind = part.get("type")
                    if kind == "input_image":
                        image_url = part.get("image_url", "")
                        prefix = "data:image/png;base64,"
                        if not isinstance(image_url, str) or not image_url.startswith(prefix):
                            raise ValueError("Only private PNG image data is admitted to the visual Gateway")
                        images.append((caption, {"type": "input_image", "source": {"type": "base64", "media_type": "image/png", "data": image_url[len(prefix):]}}))
                    elif kind in {"input_text", "output_text"}:
                        normalized.append({"type": kind, "text": str(part.get("text", ""))})
                    else:
                        raise ValueError("Unsupported visual history attachment")
                content = normalized
            elif not isinstance(content, str):
                raise ValueError("Visual history text must be a string or content array")
            incoming.append({"type": "message", "role": role, "content": content})
        elif item.get("type") == "function_call":
            if item.get("namespace") not in {None, "factorio"}:
                raise ValueError("Unexpected tool namespace in visual history")
            incoming.append({"type": "function_call", "call_id": item["call_id"],
                             "name": item["name"].removeprefix("factorio."), "arguments": item["arguments"]})
        elif item.get("type") == "function_call_output":
            incoming.append({"type": "function_call_output", "call_id": item["call_id"], "output": item["output"]})
        elif item.get("type") != "reasoning":
            raise ValueError("Unsupported visual history item")
    if images:
        content = [{"type": "input_text", "text": "Current private Factorio visual observations. The newest image is last. Direct owner steering in the conversation and instructions takes precedence over older plans."}]
        for caption, image in images[-2:]:
            content.extend([{"type": "input_text", "text": caption}, image])
        incoming.append({"type": "message", "role": "user", "content": content})
    return incoming


class VisualClawProvider(AstraProvider):
    runtime = "OpenClaw"

    def __init__(self, auth, config, directory):
        super().__init__(auth, config.get("model", "gpt-6-astra"), config.get("effort", "high"))
        self.gateway = _gateway(config["visual_claw_gateway"])
        self.token = _token(Path(directory) / "visual-claw-gateway-token")

    def respond(self, instructions, history, tools, cancel, on_text=None):
        if cancel.is_set():
            raise Interrupted("Visual OpenClaw turn interrupted before inference")
        if not self._checked:
            self.check_model()
        if cancel.is_set():
            raise Interrupted("Visual OpenClaw turn interrupted before inference")
        if not isinstance(tools, list) or any(tool.get("type") != "function" or tool.get("name") not in ALLOWED_TOOLS for tool in tools):
            raise ValueError("The visual Gateway admits only the fixed external game and learning tools")
        payload = {"model": "openclaw/" + AGENT, "instructions": instructions,
                   "input": gateway_history(history), "tools": copy.deepcopy(tools), "stream": True}
        encoded = json.dumps(payload).encode()
        if len(encoded) > 7_500_000:
            raise ValueError("Visual Gateway input exceeded the bounded local broker request size")
        request = Request(self.gateway + "/v1/responses", data=encoded, headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json", "Accept": "text/event-stream",
            "x-openclaw-agent-id": AGENT, "x-openclaw-session-key": f"agent:{AGENT}:visual-turn:{uuid.uuid4()}",
            "x-openclaw-message-channel": "factorio"})
        return self.stream_request(request, cancel, on_text)
