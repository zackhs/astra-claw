"""Streaming ChatGPT-plan inference through the supported public endpoint."""
from __future__ import annotations

import json
import socket
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .auth import AuthError, ChatGPTAuth, RESOURCE
from .interruptions import Interrupted


class ModelError(RuntimeError):
    pass


class AstraProvider:
    def __init__(self, auth: ChatGPTAuth, model="gpt-6-astra", effort="high"):
        self.auth, self.model, self.effort = auth, model, effort
        self._checked = False

    def check_model(self):
        catalog = self.auth.models()
        if self.model not in {item["slug"] for item in catalog}:
            raise AuthError(f"{self.model} is not available to the connected ChatGPT account. Select an offered model explicitly.")
        self._checked = True
        return catalog

    def respond(self, instructions, history, tools, cancel: threading.Event, on_text=None):
        if not self._checked:
            self.check_model()
        if cancel.is_set():
            raise Interrupted("Job interrupted before inference")
        payload = {"model": self.model, "instructions": instructions, "input": history,
                   "store": False, "stream": True, "reasoning": {"effort": self.effort}}
        if tools:
            payload["tools"]=[{"type": "namespace", "name": "factorio", "description": "Private isolated Factorio teammate tools", "tools": tools}]
        request = Request(RESOURCE + "/responses", data=json.dumps(payload).encode(), headers={
            "Authorization": "Bearer " + self.auth.access_token(), "Content-Type": "application/json", "Accept": "text/event-stream"})
        return self.stream_request(request, cancel, on_text)

    def stream_request(self, request, cancel, on_text=None):
        try:
            response = urlopen(request, timeout=180)
        except HTTPError as error:
            try:
                value = json.loads(error.read(65536)); detail = value.get("error", {})
                code = detail.get("code", "request_failed") if isinstance(detail, dict) else str(detail)
            except ValueError:
                code = "request_failed"
            raise ModelError(f"OpenAI inference failed ({error.code}: {str(code)[:120]})") from None
        except (OSError, URLError):
            raise ModelError("Could not connect to OpenAI") from None
        finished = threading.Event()

        def interrupt_stream():
            while not finished.wait(.1):
                if cancel.is_set():
                    # Closing just HTTPResponse can wait for its read lock.
                    # Shutdown the underlying socket first to wake readline.
                    try:
                        response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
                    except (AttributeError, OSError):
                        pass
                    return

        threading.Thread(target=interrupt_stream, daemon=True).start()
        terminal = None
        output_items = {}
        event_data = []
        try:
            with response:
                while True:
                    if cancel.is_set():
                        raise Interrupted("Player interrupted Astra's current thinking")
                    line = response.readline(8_000_000)
                    if not line:
                        break
                    text = line.decode().rstrip("\r\n")
                    if text.startswith("data:"):
                        event_data.append(text[5:].lstrip())
                    elif not text and event_data:
                        raw = "\n".join(event_data); event_data = []
                        if raw == "[DONE]":
                            continue
                        event = json.loads(raw)
                        kind = event.get("type")
                        if kind == "response.output_text.delta" and on_text:
                            on_text(event.get("delta", ""))
                        elif kind == "response.output_item.done":
                            output_items[event["output_index"]]=event["item"]
                        elif kind == "response.completed":
                            terminal = event["response"]
                        elif kind in ("response.failed", "response.incomplete", "error"):
                            detail = event.get("response", event).get("error") or {}
                            raise ModelError("OpenAI stream failed: " + str(detail.get("code", kind))[:120])
            if cancel.is_set():
                raise Interrupted("Player interrupted Astra's current thinking")
            if not terminal or terminal.get("status") != "completed":
                raise ModelError("Inference stream ended without a completed response")
            # The plan-usage stream's terminal envelope may omit its output.
            # Item-done events contain the complete replayable messages,
            # reasoning items and function arguments. Never execute deltas.
            if output_items:
                terminal["output"]=[output_items[index] for index in sorted(output_items)]
            return terminal
        except (OSError, ValueError) as error:
            if cancel.is_set():
                raise Interrupted("Player interrupted Astra's current thinking") from None
            raise ModelError("Inference stream was interrupted; no unfinished tools were executed") from None
        finally:
            finished.set()
