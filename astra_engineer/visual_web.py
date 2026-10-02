"""Loopback-only desk for the companion's private Factorio display.

This server has no filesystem, shell, host input or credential-reading API.
The caller owns the session/agent lifecycle and the server's shutdown.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


MAX_BODY = 16384
MANUAL_STATES = frozenset({"idle", "paused", "completed", "error"})
POST_ROUTES = frozenset({
    "/api/session/start", "/api/session/stop", "/api/manual/action",
    "/api/auth/start", "/api/task", "/api/steer", "/api/pause",
    "/api/resume", "/api/preferences",
})
SESSION_FIELDS = ("running", "display", "width", "height", "server", "pid",
                  "xvfb_pid", "isolated", "sandbox", "mod_free")
AGENT_FIELDS = ("state", "task_id", "task", "revision", "activity",
                "model", "summary", "steering", "preferences", "turns",
                "actions", "running", "counters", "model_turns", "task_turns", "history_turns")


def _text(body, key, limit=4000):
    value = body.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("Invalid text")
    return value.strip()


def _login_url(value):
    """A reconnect ID-token hint must never be returned to the browser desk."""
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.netloc != "auth.openai.com" or parsed.path != "/api/accounts/authorize":
        raise ValueError("Unexpected sign-in endpoint")
    params = [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
              if key not in {"id_token_hint", "login_hint"}]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(params), ""))


def serve_visual(session, agent, auth, directory, port=8790):
    """Return a bound, unstarted HTTP server; no game/model action on creation."""
    # Deliberately no option for a public, tailnet or all-interfaces bind.
    page_template = Path(__file__).with_name("visual_dashboard.html").read_text()
    csrf = secrets.token_urlsafe(32)
    script_nonce = secrets.token_urlsafe(24)
    page = page_template.replace("__CSRF_TOKEN__", csrf).replace("__SCRIPT_NONCE__", script_nonce).encode()
    mutation_lock = threading.RLock()

    def snapshot():
        native = session.status()
        worker = agent.status()
        account = auth.summary()
        safe_worker = {key: worker[key] for key in AGENT_FIELDS if key in worker}
        if worker.get("error"):
            safe_worker["error"] = "The worker stopped after an error. Check local setup before resuming."
        # Observation paths stay local and are never exposed as file endpoints.
        observation = worker.get("latest_observation")
        if isinstance(observation, dict):
            safe_worker["latest_observation"] = {key: observation[key] for key in ("id", "created") if key in observation}
        verification = worker.get("verification")
        if isinstance(verification, dict):
            safe_worker["verification"] = {key: verification[key] for key in ("level", "observation_id", "evidence") if key in verification}
        return {"session": {key: native[key] for key in SESSION_FIELDS if key in native},
                "agent": safe_worker,
                "auth": {"connected": bool(account.get("connected")),
                         "plan_enabled": bool(account.get("plan_enabled")),
                         "status": account.get("status", "Signed out")}}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            # URLs, requests and game login input are not access-logged.
            pass

        def _guard(self, mutation=False):
            hosts = self.headers.get_all("Host", [])
            if hosts != [self.server.allowed_host]:
                return False
            origins = self.headers.get_all("Origin", [])
            if origins and origins != [self.server.origin]:
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return False
            if mutation:
                tokens = self.headers.get_all("X-Astra-CSRF", [])
                return (origins == [self.server.origin] and len(tokens) == 1
                        and secrets.compare_digest(tokens[0], csrf))
            return True

        def _send(self, body=b"", status=200, content_type="application/json", **headers):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("X-Frame-Options", "DENY")
            for key, value in headers.items():
                self.send_header(key.replace("_", "-"), value)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _json(self, value, status=200):
            self._send(json.dumps(value, ensure_ascii=False).encode(), status)

        def _error(self, message, status=400):
            self._json({"error": message}, status)

        def do_GET(self):
            if not self._guard():
                self._error("Open this desk on its exact localhost address.", 403)
                return
            # No query-based files or aliases; only these fixed routes exist.
            if self.path == "/":
                self._send(page, content_type="text/html; charset=utf-8",
                           Content_Security_Policy=("default-src 'none'; "
                               f"script-src 'nonce-{script_nonce}'; style-src 'unsafe-inline'; "
                               "img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; "
                               "base-uri 'none'; form-action 'none'"))
            elif self.path == "/api/status":
                try:
                    self._json(snapshot())
                except Exception:
                    self._error("Companion status is unavailable.", 503)
            elif self.path == "/api/screen.png":
                try:
                    body = agent.manual_screenshot()
                    if not isinstance(body, bytes) or not body.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise ValueError("Invalid frame")
                    etag = '"' + hashlib.sha256(body).hexdigest() + '"'
                    if self.headers.get("If-None-Match") == etag:
                        self._send(status=304, content_type="image/png", ETag=etag)
                    else:
                        self._send(body, content_type="image/png", ETag=etag)
                except Exception:
                    self._error("Start the isolated client to view its display.", 409)
            else:
                self._error("Unknown route.", 404)

        def do_POST(self):
            if not self._guard(mutation=True):
                self.close_connection = True
                self._error("Use the same-origin desk and its request token.", 403)
                return
            if self.path not in POST_ROUTES:
                self.close_connection = True
                self._error("Unknown route.", 404)
                return
            lengths = self.headers.get_all("Content-Length", [])
            try:
                if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
                    raise ValueError("Invalid length")
                length = int(lengths[0])
                if not 0 < length <= MAX_BODY:
                    raise ValueError("Invalid length")
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise ValueError("Invalid content type")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("Incomplete body")
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("Expected object")
            except Exception:
                self.close_connection = True
                self._error("Send a bounded JSON object with Content-Length.")
                return
            try:
                with mutation_lock:
                    path = self.path
                    if path in {"/api/task", "/api/steer"}:
                        _text(body, "text")
                    if path in {"/api/task", "/api/steer", "/api/resume"} and not session.status().get("running"):
                        self._error("Start the isolated client and finish Factorio setup first.", 409)
                        return
                    if path == "/api/session/start":
                        address = body.get("server")
                        if address is not None and (not isinstance(address, str) or len(address) > 253
                                                    or any(ord(char) < 32 for char in address)):
                            raise ValueError("Invalid server")
                        session.start(server=address.strip() if address else None)
                        result = snapshot()
                    elif path == "/api/session/stop":
                        agent.pause()
                        session.close()
                        result = snapshot()
                    elif path == "/api/manual/action":
                        action = body.get("action")
                        if not isinstance(action, dict):
                            raise ValueError("Invalid action")
                        if agent.status().get("state") not in MANUAL_STATES:
                            self._error("Pause Astra before using the private game controls.", 409)
                            return
                        agent.manual_action(action)
                        # Action results may contain native details; return only status.
                        result = {"accepted": True}
                    elif path == "/api/auth/start":
                        result = {"url": _login_url(auth.start_login())}
                    elif path == "/api/task":
                        agent.task(_text(body, "text"))
                        result = snapshot()
                    elif path == "/api/steer":
                        agent.steer(_text(body, "text"))
                        result = snapshot()
                    elif path == "/api/pause":
                        agent.pause()
                        result = snapshot()
                    elif path == "/api/resume":
                        agent.resume()
                        result = snapshot()
                    elif path == "/api/preferences":
                        values = body.get("preferences")
                        if (not isinstance(values, dict) or len(json.dumps(values)) > 4096
                                or not all(isinstance(key, str) and isinstance(value, str)
                                           for key, value in values.items())):
                            raise ValueError("Invalid preferences")
                        agent.preferences(values)
                        result = snapshot()
                    self._json(result)
            except (ValueError, TypeError, KeyError):
                self._error("Check the request fields and try again.")
            except Exception:
                # Never echo native/provider exceptions that might include login input.
                self._error("The action could not complete. Check the companion state and local setup.", 409)

        def do_OPTIONS(self):
            self._error("Cross-origin requests are not supported.", 403)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.allowed_host = f"127.0.0.1:{server.server_port}"
    server.origin = "http://" + server.allowed_host
    return server
