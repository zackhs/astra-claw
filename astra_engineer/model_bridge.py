"""Loopback-only inference broker for the app's authorized ChatGPT connection.

OpenClaw holds a local broker credential. Renewable ChatGPT credentials stay
in Astra Engineer's account store and never enter the OpenClaw profile.
"""
from __future__ import annotations

import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .auth import RESOURCE


def serve_model_bridge(auth, token, port, model="gpt-6-astra"):
    host = f"127.0.0.1:{port}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def authorized(self):
            return (self.headers.get("Host") == host and not self.headers.get("Origin") and
                    hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token))

        def json(self, value, code=200):
            data = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.authorized():
                self.json({"error": {"code": "unauthorized"}}, 403)
                return
            if self.path == "/v1/models":
                available = model in {item["slug"] for item in auth.models()}
                self.json({"object": "list", "data": [{"id": model, "object": "model", "owned_by": "astra-engineer"}] if available else []})
            else:
                self.json({"error": {"code": "not_found"}}, 404)

        def do_POST(self):
            if not self.authorized():
                self.json({"error": {"code": "unauthorized"}}, 403)
                return
            if self.path != "/v1/responses":
                self.json({"error": {"code": "not_found"}}, 404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8_000_000:
                    raise ValueError("Invalid request size")
                payload = json.loads(self.rfile.read(length))
                if payload.get("model") != model or payload.get("stream") is not True:
                    raise ValueError("Use the configured Astra model with streaming")
                # Explicit histories are retained by the private Gateway; this
                # broker never changes the account's inference/billing route.
                payload["store"] = False
                # ChatGPT plan inference controls its own output budget and
                # rejects the API adapter's optional max_output_tokens field.
                payload.pop("max_output_tokens", None)
                request = Request(RESOURCE + "/responses", data=json.dumps(payload).encode(), headers={
                    "Authorization": "Bearer " + auth.access_token(), "Content-Type": "application/json", "Accept": "text/event-stream"})
                upstream = urlopen(request, timeout=180)
            except HTTPError as error:
                try:
                    data = json.loads(error.read(65536))
                    code = data.get("error", {}).get("code", "inference_failed")
                except (ValueError, AttributeError):
                    code = "inference_failed"
                self.json({"error": {"code": str(code)[:160], "message": "Authorized Astra inference failed"}}, error.code)
                return
            except Exception as error:
                self.json({"error": {"code": "broker_failed", "message": str(error)[:200]}}, 400)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            items = {}
            event_lines = []
            try:
                with upstream:
                    for raw in upstream:
                        line = raw.decode().rstrip("\r\n")
                        if line:
                            event_lines.append(line)
                            continue
                        data_lines = [s[5:].lstrip() for s in event_lines if s.startswith("data:")]
                        if data_lines and data_lines != ["[DONE]"]:
                            value = json.loads("\n".join(data_lines))
                            if value.get("type") == "response.output_item.done":
                                items[value["output_index"]] = value["item"]
                            if value.get("type") == "response.completed" and items:
                                value["response"]["output"] = [items[index] for index in sorted(items)]
                            event_name = [s for s in event_lines if s.startswith("event:")]
                            message = "\n".join([*event_name, "data: " + json.dumps(value)]) + "\n\n"
                        else:
                            message = "\n".join(event_lines) + "\n\n"
                        self.wfile.write(message.encode())
                        self.wfile.flush()
                        event_lines = []
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                upstream.close()
                self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server
