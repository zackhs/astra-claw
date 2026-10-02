"""Real loopback HTTP checks; no game process, auth exchange or model calls."""
import base64
import http.client
import json
import re
import threading

import pytest

from astra_engineer.visual_web import MAX_BODY, serve_visual


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aarsAAAAASUVORK5CYII=")


class Session:
    def __init__(self):
        self.running = False
        self.server = None
        self.calls = []
        self.fail_start = False

    def start(self, server=None):
        if self.fail_start:
            raise RuntimeError("Do not echo password=private-factorio-login")
        self.calls.append(("start", server))
        self.running = True
        self.server = server

    def status(self):
        return {"running": self.running, "server": self.server, "width": 1280,
                "height": 720, "isolated": True, "sandbox": "bubblewrap",
                "mod_free": True, "access_token": "must-never-leave-process"}

    def screenshot(self):
        if not self.running:
            raise RuntimeError("not running")
        self.calls.append(("screenshot",))
        return PNG

    def action(self, action):
        if not self.running:
            raise RuntimeError("not running")
        self.calls.append(("action", action))
        return {"secret": "do-not-echo-native-action-result"}

    def close(self):
        self.calls.append(("close",))
        self.running = False


class Agent:
    def __init__(self, session):
        self.session = session
        self.state = "idle"
        self.calls = []
        self.task_id = None
        self.prefs = {"layout": "Prefer conveyors feeding a central hub."}

    def status(self):
        return {"state": self.state, "model": "gpt-6-astra", "task_id": self.task_id,
                "preferences": self.prefs, "model_turns": 17, "task_turns": 4, "access_token": "private-model-token",
                "latest_observation": {"id": "frame-1", "path": "/private/account.json", "created": 1},
                "verification": {"level": "model_visual", "observation_id": "frame-1",
                                 "screenshot": "/private/frame.png", "evidence": "Visible belt"}}

    def task(self, text):
        self.calls.append(("task", text))
        self.state = "running"
        self.task_id = "task-1"

    def steer(self, text):
        self.calls.append(("steer", text))

    def pause(self):
        self.calls.append(("pause",))
        self.state = "paused"

    def resume(self):
        self.calls.append(("resume",))
        self.state = "running"

    def preferences(self, value):
        self.calls.append(("preferences", value))
        self.prefs.update(value)

    def manual_action(self, action):
        self.calls.append(("manual_action", action))
        # Real VisualAgent performs this check under its shared control lock.
        if self.state not in {"idle", "paused", "completed", "error"}:
            raise RuntimeError("not paused")
        return self.session.action(action)

    def manual_screenshot(self):
        return self.session.screenshot()


class Auth:
    def __init__(self):
        self.calls = []

    def summary(self):
        return {"connected": True, "plan_enabled": True, "status": "Connected",
                "email": "not-needed@example.test", "access_token": "private-auth-token"}

    def start_login(self):
        self.calls.append("login")
        return ("https://auth.openai.com/api/accounts/authorize?client_id=local-client&state=ephemeral"
                "&id_token_hint=private-identity-token&login_hint=not-needed%40example.test")


@pytest.fixture
def desk(tmp_path):
    session = Session()
    agent = Agent(session)
    auth = Auth()
    server = serve_visual(session, agent, auth, tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path, *, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    _, _, page = request("/")
    csrf = re.search(rb'name="astra-csrf" content="([^"]+)"', page).group(1).decode()

    def post(path, body=None, headers=None):
        safe = {"Origin": server.origin, "X-Astra-CSRF": csrf, "Content-Type": "application/json"}
        safe.update(headers or {})
        return request(path, method="POST", body=json.dumps(body or {}).encode(), headers=safe)

    yield server, session, agent, auth, request, post
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_creation_and_status_do_not_start_client_agent_or_login(desk):
    server, session, agent, auth, request, _ = desk
    assert server.server_address[0] == "127.0.0.1"
    status, headers, page = request("/")
    assert status == 200
    assert b"__CSRF_TOKEN__" not in page
    assert b"__SCRIPT_NONCE__" not in page
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    status, headers, body = request("/api/status")
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    value = json.loads(body)
    assert value["agent"]["state"] == "idle"
    assert value["agent"]["model_turns"] == 17
    assert value["session"]["running"] is False
    assert not (session.calls or agent.calls or auth.calls)
    assert b"private" not in body
    assert b"not-needed@example.test" not in body
    assert value["agent"]["latest_observation"] == {"id": "frame-1", "created": 1}
    assert "screenshot" not in value["agent"]["verification"]


def test_host_origin_and_csrf_guards_block_rebinding_and_browser_mutations(desk):
    server, session, _, _, request, post = desk
    assert request("/", headers={"Host": "attacker.example"})[0] == 403
    assert request("/", headers={"Host": f"localhost:{server.server_port}"})[0] == 403
    assert request("/api/status", headers={"Origin": "https://attacker.example"})[0] == 403
    assert request("/", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert post("/api/session/start", headers={"Origin": "https://attacker.example"})[0] == 403
    assert post("/api/session/start", headers={"X-Astra-CSRF": "wrong"})[0] == 403
    assert request("/api/session/start", method="POST", body=b"{}",
                   headers={"Content-Type": "application/json", "Origin": server.origin})[0] == 403
    assert not session.calls
    assert request("/api/task", method="OPTIONS")[0] == 403


def test_fixed_routes_no_file_downloads_or_query_aliases(desk):
    _, _, _, _, request, post = desk
    for path in ("/api/status?file=account.json", "/../../chatgpt-account.json", "/downloads/game.zip", "/api/shell"):
        assert request(path)[0] == 404
    assert post("/api/shell", {"cmd": "cat /private/account.json"})[0] == 404


def test_png_preview_conditional_requests_and_stopped_client(desk):
    _, session, agent, _, request, post = desk
    assert request("/api/screen.png")[0] == 409
    assert post("/api/session/start", {"server": "factorio.example:34197"})[0] == 200
    assert agent.calls == []
    assert session.calls == [("start", "factorio.example:34197")]
    code, headers, body = request("/api/screen.png")
    assert code == 200 and headers["Content-Type"] == "image/png" and body == PNG
    code, cached_headers, body = request("/api/screen.png", headers={"If-None-Match": headers["ETag"]})
    assert code == 304 and body == b""
    assert cached_headers["ETag"] == headers["ETag"]
    assert agent.calls == []  # Native preview never starts an inference job.
    assert post("/api/session/stop")[0] == 200
    assert agent.calls == [("pause",)]
    assert session.calls[-1] == ("close",)
    assert request("/api/screen.png")[0] == 409


def test_manual_controls_require_safe_agent_state_and_use_atomic_agent_entrypoint(desk):
    _, session, agent, _, _, post = desk
    action = {"action": "click", "x": 20, "y": 30, "button": "left"}
    assert post("/api/manual/action", {"action": action})[0] == 409
    post("/api/session/start")
    assert post("/api/manual/action", {"action": action})[0] == 200
    assert agent.calls[-1] == ("manual_action", action)
    assert session.calls[-1] == ("action", action)
    agent.state = "running"
    calls = len(session.calls)
    assert post("/api/manual/action", {"action": action})[0] == 409
    assert len(session.calls) == calls
    post("/api/pause")
    code, _, body = post("/api/manual/action", {"action": {"action": "text", "text": "private-factorio-login"}})
    assert code == 200 and json.loads(body) == {"accepted": True}
    assert b"private" not in body
    assert b"do-not-echo" not in body


def test_explicit_task_steering_pause_resume_and_string_preferences(desk):
    _, _, agent, _, _, post = desk
    post("/api/session/start")
    assert post("/api/task", {"text": "Set up the closest stone patch"})[0] == 200
    assert agent.calls[-1] == ("task", "Set up the closest stone patch")
    assert post("/api/steer", {"text": "Feed the central hub"})[0] == 200
    assert agent.calls[-1] == ("steer", "Feed the central hub")
    assert post("/api/pause")[0] == 200 and agent.state == "paused"
    assert post("/api/resume")[0] == 200 and agent.state == "running"
    prefs = {"layout": "Prefer conveyors feeding a central hub.", "notes": "Leave room to expand."}
    assert post("/api/preferences", {"preferences": prefs})[0] == 200
    assert agent.prefs == prefs
    assert post("/api/preferences", {"preferences": {"central_hub": True}})[0] == 400


def test_signin_requires_explicit_post_and_returns_no_identity_hint(desk):
    _, _, _, auth, request, post = desk
    assert request("/api/auth/start")[0] == 404
    assert auth.calls == []
    code, _, body = post("/api/auth/start")
    assert code == 200 and auth.calls == ["login"]
    url = json.loads(body)["url"]
    assert url == "https://auth.openai.com/api/accounts/authorize?client_id=local-client&state=ephemeral"
    assert "id_token" not in url and "login_hint" not in url and "private" not in url


def test_bounds_bad_json_and_lifecycle_errors_never_echo_credentials(desk):
    server, session, _, _, request, post = desk
    assert post("/api/task", {"text": " "})[0] == 400
    assert post("/api/task", {"text": "x" * 4001})[0] == 400
    assert post("/api/session/start", {"server": "host\npassword=secret"})[0] == 400
    assert post("/api/task", {"text": "x" * MAX_BODY})[0] == 400
    headers = {"Origin": server.origin, "Content-Type": "application/json"}
    _, _, page = request("/")
    headers["X-Astra-CSRF"] = re.search(rb'name="astra-csrf" content="([^"]+)"', page).group(1).decode()
    assert request("/api/task", method="POST", body=b"[]", headers=headers)[0] == 400
    assert request("/api/task", method="POST", body=b"{bad-json", headers=headers)[0] == 400
    assert post("/api/task", headers={"Content-Type": "text/plain"})[0] == 400
    session.fail_start = True
    code, _, body = post("/api/session/start")
    assert code == 409
    assert b"password" not in body and b"private" not in body


def test_no_task_steering_or_resume_before_private_client_is_started(desk):
    _, _, agent, _, _, post = desk
    for path in ("/api/task", "/api/steer", "/api/resume"):
        code, _, body = post(path, {"text": "Set up the stone patch"})
        assert code == 409
        assert "Start the isolated client" in json.loads(body)["error"]
    assert agent.calls == []
