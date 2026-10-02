"""The application's own Sign in with ChatGPT public-client registration.

Credentials never enter the game, browser dashboard, model prompt or logs.
Reference: https://developers.openai.com/siwc/token-sharing-open-source/sign-in
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

import jwt

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
TOKEN_ENDPOINT = ISSUER + "/api/accounts/oauth/token"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"


class AuthError(RuntimeError):
    pass


def write_private(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(value, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def request_json(url: str, *, form=None, bearer=None):
    headers = {"Accept": "application/json"}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    data = None
    if form is not None:
        data = urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        # Return an error code, never dump a response containing credentials.
        try:
            value = json.loads(error.read(65536))
            code = value.get("error", "request_failed")
            if isinstance(code, dict):
                code = code.get("code", "request_failed")
        except (ValueError, AttributeError):
            code = "request_failed"
        raise AuthError(f"OpenAI request failed ({error.code}: {str(code)[:100]})") from None
    except (OSError, URLError):
        raise AuthError("Could not reach OpenAI. Check the connection and try again.") from None


class ChatGPTAuth:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profile_path = self.directory / "chatgpt-account.json"
        self.registration_path = self.directory / "chatgpt-registration.json"
        host_path = self.directory / "host-id.json"
        if not host_path.exists():
            write_private(host_path, {"id": "urn:uuid:" + str(uuid.uuid4())})
        self.host_id = json.loads(host_path.read_text())["id"]
        self._lock = threading.RLock()
        self._pending = None
        self.login_status = "Connected" if self.profile_path.exists() else "Signed out"
        self.discovery = None
        self.listener = None

    @contextmanager
    def _credential_lock(self):
        # Cross-process serialization also protects rotating refresh tokens.
        with self._lock:
            fd = os.open(self.directory / "credentials.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                if os.name == "posix":
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def _profile(self):
        if not self.profile_path.exists():
            return None
        return json.loads(self.profile_path.read_text())

    def summary(self):
        profile = self._profile()
        return {"connected": bool(profile), "email": profile.get("email") if profile else None,
                "plan_enabled": bool(profile and "chatgpt.tokens.use.direct" in profile["scopes"]),
                "status": self.login_status}

    def _discovery(self):
        if self.discovery is None:
            discovery = request_json(ISSUER + "/.well-known/openid-configuration")
            if discovery.get("issuer") != ISSUER:
                raise AuthError("Unexpected OpenAI identity issuer")
            for name in ("jwks_uri", "revocation_endpoint"):
                if urlsplit(discovery.get(name, "")).hostname != "auth.openai.com":
                    raise AuthError("Unexpected OpenAI identity endpoint")
            self.discovery = discovery
        return self.discovery

    def _identity(self, token, client_id, nonce=None):
        try:
            keys = jwt.PyJWKClient(self._discovery()["jwks_uri"], timeout=20)
            key = keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["RS256", "ES256"],
                                audience=client_id, issuer=ISSUER, leeway=5,
                                options={"require": ["sub", "exp", "iat"]})
            if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
                raise AuthError("The sign-in nonce did not match. Start again.")
            return claims
        except AuthError:
            raise
        except Exception:
            raise AuthError("OpenAI identity validation failed. Start sign-in again.") from None

    def start_login(self):
        with self._lock:
            if self._pending and time.time()-self._pending['started']>1800:
                self._pending=None
                if self.listener:
                    old_listener=self.listener
                    threading.Thread(target=lambda:(old_listener.shutdown(),old_listener.server_close()),daemon=True).start()
            if self._pending:
                return self._pending["url"]
            selected = self._profile()
            registration = json.loads(self.registration_path.read_text()) if self.registration_path.exists() else {}
            client_id = selected["client_id"] if selected else registration.get("client_id", "dynamic_agent_client")
            owner = self

            class Callback(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    if urlsplit(self.path).path != "/auth/callback":
                        self.send_error(404)
                        return
                    params = parse_qs(urlsplit(self.path).query)
                    try:
                        owner._callback({key: value[0] for key, value in params.items()})
                        message = "ChatGPT connected. You can close this tab and return to Astra Engineer."
                        status = 200
                    except AuthError as error:
                        message, status = str(error), 400
                    self.send_response(status)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(message.encode())

            self.listener = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
            redirect = f"http://127.0.0.1:{self.listener.server_port}/auth/callback"
            state, nonce, verifier = (secrets.token_urlsafe(48) for _ in range(3))
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            params = {"client_id": client_id, "ext_agent_host_id": self.host_id,
                      "response_type": "code", "redirect_uri": redirect, "scope": SCOPES,
                      "resource": RESOURCE, "state": state, "nonce": nonce,
                      "code_challenge_method": "S256", "code_challenge": challenge}
            if client_id == "dynamic_agent_client":
                params["agent_name_hint"] = "Astra Engineer"
            elif selected:
                params["id_token_hint"] = selected["id_token"]
                if selected.get("email"):
                    params["login_hint"] = selected["email"]
            url = ISSUER + "/api/accounts/authorize?" + urlencode(params)
            self._pending = {"state": state, "nonce": nonce, "verifier": verifier,
                             "redirect": redirect, "client_id": client_id, "selected": selected,
                             "url": url, "started": time.time()}
            self.login_status = "Waiting for ChatGPT sign-in"
            threading.Thread(target=self.listener.serve_forever, daemon=True).start()
            return url

    def _callback(self, params):
        with self._credential_lock():
            pending = self._pending
            if not pending or not secrets.compare_digest(params.get("state", ""), pending["state"]):
                raise AuthError("This sign-in attempt is invalid or expired. Start again.")
            try:
                if time.time() - pending["started"] > 1800:
                    raise AuthError("Sign-in expired. Start again.")
                if params.get("error"):
                    raise AuthError("ChatGPT authorization was not granted. You can sign in again when ready.")
                issued = params.get("client_id", pending["client_id"])
                if not issued or issued == "dynamic_agent_client":
                    raise AuthError("Registration did not return an issued client ID.")
                if pending["client_id"] != "dynamic_agent_client" and issued != pending["client_id"]:
                    raise AuthError("The issued client did not match this account.")
                # Retain registration even if the one-time code exchange fails.
                write_private(self.registration_path, {"client_id": issued})
                if not params.get("code"):
                    raise AuthError("Sign-in did not return an authorization code.")
                tokens = request_json(TOKEN_ENDPOINT, form={"grant_type": "authorization_code", "client_id": issued,
                                      "code": params["code"], "code_verifier": pending["verifier"],
                                      "redirect_uri": pending["redirect"], "resource": RESOURCE})
                identity = self._identity(tokens["id_token"], issued, pending["nonce"])
                selected = pending["selected"]
                if selected and identity["sub"] != selected["subject"]:
                    raise AuthError("This sign-in belongs to another account. Sign out before changing accounts.")
                profile = {**tokens, "client_id": issued, "issuer": ISSUER, "subject": identity["sub"],
                           "email": identity.get("email"), "ext_agent_host_id": self.host_id,
                           "scopes": tokens.get("scope", "").split(), "saved_at": time.time()}
                if not profile.get("access_token") or not profile.get("refresh_token"):
                    raise AuthError("A renewable ChatGPT plan session was not granted.")
                write_private(self.profile_path, profile)
                self.login_status = "Connected" if "chatgpt.tokens.use.direct" in profile["scopes"] else "Authorize ChatGPT plan usage to run Astra"
            except (KeyError,TypeError,ValueError):
                self.login_status='OpenAI returned an incomplete authorization. Start sign-in again.'
                raise AuthError(self.login_status) from None
            except AuthError as error:
                self.login_status = str(error)
                raise
            finally:
                self._pending = None
                if self.listener:
                    listener=self.listener
                    threading.Thread(target=lambda:(listener.shutdown(),listener.server_close()), daemon=True).start()

    def access_token(self):
        with self._credential_lock():
            profile = self._profile()
            if not profile or "chatgpt.tokens.use.direct" not in profile["scopes"]:
                raise AuthError("Continue with ChatGPT and authorize plan usage before running Astra.")
            if time.time() >= profile["saved_at"] + profile["expires_in"] - 120:
                tokens = request_json(TOKEN_ENDPOINT, form={"grant_type": "refresh_token", "client_id": profile["client_id"],
                                      "refresh_token": profile["refresh_token"], "resource": RESOURCE})
                if tokens.get("id_token"):
                    identity = self._identity(tokens["id_token"], profile["client_id"])
                    if identity["sub"] != profile["subject"]:
                        raise AuthError("Refreshed identity does not match the selected account.")
                if not tokens.get("refresh_token"):
                    raise AuthError("Refresh did not return replacement credentials. Sign in again.")
                profile.update(tokens)
                profile["scopes"] = tokens.get("scope", " ".join(profile["scopes"])).split()
                profile["saved_at"] = time.time()
                write_private(self.profile_path, profile)
            if "chatgpt.tokens.use.direct" not in profile["scopes"]:
                raise AuthError("ChatGPT plan usage is no longer authorized.")
            return profile["access_token"]

    def models(self):
        catalog = request_json(RESOURCE + "/models", bearer=self.access_token())
        return [{"slug": item["slug"], "display_name": item["display_name"]}
                for item in catalog.get("models", []) if item.get("visibility") == "list"]

    def sign_out(self):
        with self._credential_lock():
            profile = self._profile()
            confirmed = True
            if profile:
                try:
                    endpoint = self._discovery()["revocation_endpoint"]
                    data = urlencode({"token": profile["refresh_token"], "token_type_hint": "refresh_token",
                                      "client_id": profile["client_id"]}).encode()
                    with urlopen(Request(endpoint, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=20):
                        pass
                except Exception:
                    confirmed = False
                self.profile_path.unlink(missing_ok=True)
            self.registration_path.unlink(missing_ok=True)
            self.login_status = "Signed out" if confirmed else "Signed out locally; disconnect Astra Engineer in ChatGPT Settings to confirm revocation"
            return confirmed
