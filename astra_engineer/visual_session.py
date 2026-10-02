"""A licensed, ordinary Factorio client on an isolated Linux virtual display.

This backend deliberately has no RCON, Lua, mod installation, shell-action or
host-desktop capability. Bubblewrap is mandatory; failures never trigger an
unsandboxed retry. Licensed game assets are supplied by the user, not shipped.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import zlib


class VisualSessionError(RuntimeError):
    """The isolated game session cannot safely perform the requested action."""


_KEYS = frozenset({
    "Return", "Escape", "Tab", "space", "BackSpace", "Delete", "Home", "End",
    "Up", "Down", "Left", "Right", "Page_Up", "Page_Down", "Insert",
    "minus", "equal", "bracketleft", "bracketright", "comma", "period",
    "slash", "apostrophe", "semicolon", "grave", "backslash",
    *[f"F{i}" for i in range(1, 13)],
    *list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"),
})
_MODIFIERS = {"Ctrl": "ctrl", "Control": "ctrl", "ctrl": "ctrl",
              "Shift": "shift", "shift": "shift", "Alt": "alt", "alt": "alt"}
_BUTTONS = {"left": "1", "middle": "2", "right": "3"}
_SYSTEM_PYTHON_PATHS = ("/usr/bin/python3", "/bin/python3")


def _integer(value, name, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{name} must be an integer from {lower} to {upper}")
    return value


def _seconds(value, name, lower=0.0, upper=2.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(f"{name} must be a finite number from {lower} to {upper}")
    return float(value)


def _key(value):
    if not isinstance(value, str):
        raise ValueError("key must name one key, optionally with Ctrl/Shift/Alt")
    parts = value.split("+")
    if not 1 <= len(parts) <= 4 or parts[-1] not in _KEYS:
        raise ValueError("key is not in the permitted game-key vocabulary")
    modifiers = [_MODIFIERS.get(part) for part in parts[:-1]]
    if any(part is None for part in modifiers) or len(set(modifiers)) != len(modifiers):
        raise ValueError("key modifiers must be distinct Ctrl/Shift/Alt names")
    return "+".join([*modifiers, parts[-1]])


def _program(value, default):
    candidate = value or shutil.which(default)
    if not candidate:
        raise VisualSessionError(f"Required program {default} is missing")
    path = Path(candidate).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise VisualSessionError(f"Required program {default} is not executable")
    return str(path)


def _system_python():
    # The trusted capture helper needs only the standard library. A Conda or
    # pyenv interpreter under HOME is intentionally absent from the sandbox;
    # choose an executable in the immutable /usr mount rather than mounting
    # the application's host interpreter, environment or user home.
    for candidate in _SYSTEM_PYTHON_PATHS:
        path = Path(candidate).resolve()
        if path.is_relative_to(Path("/usr")) and path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise VisualSessionError("Install system Python at /usr/bin/python3 for private display capture")


class VisualSession:
    """Own one private client, display, input stream and filesystem sandbox.

    ``factorio_install`` is the licensed extracted installation. Optional
    ``binary_prefix`` supplies an explicit emulator command such as Box64.
    ``runtime_readonly_paths`` exposes only its explicitly selected libraries.
    The session directory must be dedicated to this backend; its game-write
    subtree contains the Factorio login, saves and empty third-party mod folder.
    No other runner credentials or user home files are mounted into the game.
    """

    def __init__(self, config: dict, directory: Path):
        self.config = dict(config)
        self.directory = Path(directory).expanduser().resolve()
        self.width = _integer(config.get("visual_width", config.get("width", 1280)), "width", 640, 2560)
        self.height = _integer(config.get("visual_height", config.get("height", 720)), "height", 480, 1440)
        install = config.get("factorio_install")
        if not isinstance(install, str) or not install:
            raise ValueError("factorio_install must point to a user-supplied licensed client")
        self.install = Path(install).expanduser().resolve()
        if self.directory.is_relative_to(self.install) or self.install.is_relative_to(self.directory):
            raise ValueError("The licensed installation and private session directory must not overlap")
        binary = Path(config.get("factorio_binary", "bin/x64/factorio"))
        self.binary = (self.install / binary).resolve()
        if not self.binary.is_relative_to(self.install) or not self.binary.is_file():
            raise ValueError("factorio_binary must be a file inside factorio_install")
        if not (self.install / "data" / "core").is_dir():
            raise ValueError("factorio_install does not contain the normal client's data/core")
        prefix = config.get("binary_prefix", [])
        if not isinstance(prefix, list) or any(not isinstance(v, str) or not v or "\0" in v for v in prefix):
            raise ValueError("binary_prefix must be an explicit argument list")
        self.prefix = list(prefix)
        if self.prefix:
            self.prefix[0] = _program(self.prefix[0], "client emulator")
        paths = config.get("runtime_readonly_paths", [])
        if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
            raise ValueError("runtime_readonly_paths must be a list of explicit paths")
        self.runtime_paths = [Path(p).expanduser().resolve() for p in paths]
        forbidden = {Path("/"), Path("/home"), Path.home().resolve(), Path("/tmp"), Path("/run"), Path("/etc"), Path("/proc"), Path("/sys")}
        for path in self.runtime_paths:
            if (not path.exists() or path in forbidden or self.directory.is_relative_to(path)
                    or path.is_relative_to(self.directory)):
                raise ValueError("runtime_readonly_paths must expose selected runtime assets, not home/session/system roots")
        # Custom runtime variables are deliberately restricted; never inherit
        # the host DISPLAY, XAUTHORITY, DBus, Wayland or provider credentials.
        runtime_env = config.get("runtime_env", {})
        if not isinstance(runtime_env, dict) or any(k not in {"BOX64_LD_LIBRARY_PATH", "BOX64_PATH", "BOX64_LOG", "LD_LIBRARY_PATH"} or not isinstance(v, str) or "\0" in v for k, v in runtime_env.items()):
            raise ValueError("runtime_env permits only explicit emulator/library variables")
        self.runtime_env = dict(runtime_env)
        self.player_name = config.get("player_name")
        if self.player_name is not None and (not isinstance(self.player_name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", self.player_name)):
            raise ValueError("player_name must contain 1..60 letters, digits, underscores or hyphens")
        self.bwrap = _program(config.get("bwrap_binary"), "bwrap")
        self.xvfb = _program(config.get("xvfb_binary"), "Xvfb")
        self.xdotool = _program(config.get("xdotool_binary"), "xdotool")
        self.xauth = _program(config.get("xauth_binary"), "xauth")
        self.capture_python = _system_python()
        self.game_write = self.directory / "game-write"
        self.socket_directory = self.directory / "x-sockets"
        self.authority = self.directory / "Xauthority"
        self._lock_handle = None
        self._display_process = None
        self._client_process = None
        self._logs = []
        self._display = None
        self._server = None
        self._pressed = set()
        self._lock = threading.RLock()
        self._cancel = threading.Event()

    def _sandbox(self, command, *, game=False, display=True):
        # Mount only immutable OS/runtime libraries and this client's assets.
        # Private /tmp excludes every host filesystem X socket; Xauthority is
        # ours alone. Never mount /home, /run/user, DBus or a Wayland socket.
        args = [self.bwrap, "--unshare-all", "--share-net", "--die-with-parent", "--new-session", "--clearenv",
                "--ro-bind", "/usr", "/usr", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--tmpfs", "/run", "--dir", "/run/claw", "--chmod", "0700", "/run/claw",
                "--dir", "/home", "--dir", "/home/claw", "--dir", "/etc"]
        for source in ("/bin", "/sbin", "/lib", "/lib64"):
            path = Path(source)
            if path.is_symlink():
                args += ["--symlink", os.readlink(path), source]
            elif path.exists():
                args += ["--ro-bind", source, source]
        for source in ("/etc/ld.so.cache", "/etc/fonts", "/etc/ssl/certs", "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/machine-id"):
            if Path(source).exists():
                args += ["--ro-bind", source, source]
        args += ["--bind", str(self.socket_directory), "/tmp/.X11-unix", "--ro-bind", str(self.authority), "/auth/Xauthority"]
        assets = [*self.runtime_paths, Path(self.xvfb), Path(self.xdotool), Path(self.xauth)]
        if self.prefix:
            assets.append(Path(self.prefix[0]))
        if game:
            args += ["--ro-bind", str(self.install), "/factorio", "--bind", str(self.game_write), "/game-write", "--chdir", "/game-write"]
        for path in assets:
            if not path.is_relative_to(Path("/usr")):
                args += ["--ro-bind", str(path), str(path)]
        environment = {"HOME": "/home/claw", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                       "LIBGL_ALWAYS_SOFTWARE": "1", "SDL_AUDIODRIVER": "dummy", "XAUTHORITY": "/auth/Xauthority",
                       "XDG_RUNTIME_DIR": "/run/claw",
                       "__GLX_VENDOR_LIBRARY_NAME": "mesa", "GALLIUM_DRIVER": "llvmpipe",
                       **self.runtime_env}
        mesa_vendor = Path("/usr/share/glvnd/egl_vendor.d/50_mesa.json")
        if mesa_vendor.is_file():
            # Without a GPU device, NVIDIA's EGL discovery can crash before
            # GLX initialization even when LIBGL_ALWAYS_SOFTWARE is enabled.
            # Explicit Mesa selection keeps Xvfb and the client software-only.
            environment["__EGL_VENDOR_LIBRARY_FILENAMES"] = str(mesa_vendor)
        if display and self._display:
            # unix/ explicitly avoids any TCP X transport.
            environment["DISPLAY"] = "unix/" + self._display
        for key, value in environment.items():
            args += ["--setenv", key, value]
        return [*args, "--", *command]

    def _spawn(self, command, log_name):
        log = (self.directory / log_name).open("ab", buffering=0)
        self._logs.append(log)
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, start_new_session=True)

    def _auth_entry(self, display):
        result = subprocess.run([self.xauth, "-f", str(self.authority), "add", display,
                                 "MIT-MAGIC-COOKIE-1", self._cookie], stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=5, env={"PATH": "/usr/bin:/bin"})
        if result.returncode:
            raise VisualSessionError("Could not create the private display authorization")

    def _verify_client(self):
        command = [*self.prefix, "/factorio/" + str(self.binary.relative_to(self.install)), "--version"]
        result = subprocess.run(self._sandbox(command, game=True, display=False), stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=30, env={"PATH": "/usr/bin:/bin"})
        match = re.search(rb"Version:\s*(\d+\.\d+\.\d+)[^\n]*\bfull\b", result.stdout)
        if result.returncode or not match:
            raise VisualSessionError("The sandbox requires the full graphical Factorio client; headless is a server")
        self._version = match.group(1).decode("ascii")

    def start(self, server: str | None = None):
        with self._lock:
            if self._client_process and self._client_process.poll() is None:
                raise VisualSessionError("This session already owns a running client")
            if server is not None and (not isinstance(server, str) or not re.fullmatch(r"[A-Za-z0-9_.:\[\]-]{1,255}", server) or server.startswith("-")):
                raise ValueError("server must be a hostname or IP address, optionally with a port")
            self.close()
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.directory, 0o700)
            lock_path = self.directory / "visual.lock"
            # Reject filesystem redirection before writing account/session data.
            for path in (lock_path, self.game_write, self.socket_directory, self.authority):
                if path.is_symlink():
                    raise VisualSessionError("Private session paths must not be symbolic links")
            self._lock_handle = lock_path.open("a+")
            try:
                fcntl.flock(self._lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._lock_handle.close()
                self._lock_handle = None
                raise VisualSessionError("Another process already owns this isolated session") from None
            try:
                self.game_write.mkdir(mode=0o700, exist_ok=True)
                self.socket_directory.mkdir(mode=0o700, exist_ok=True)
                os.chmod(self.socket_directory, 0o700)
                for name in ("config", "mods", "saves"):
                    path = self.game_write / name
                    if path.is_symlink():
                        raise VisualSessionError("Game data directories must not be symbolic links")
                    path.mkdir(mode=0o700, exist_ok=True)
                # The only enabled content is base; no companion mod is copied.
                mod_list = self.game_write / "mods/mod-list.json"
                player_data = self.game_write / "player-data.json"
                status_file = self.directory / "visual-session.json"
                for path in (mod_list, player_data, status_file):
                    if path.is_symlink():
                        raise VisualSessionError("Private game files must not be symbolic links")
                mod_list.write_text(json.dumps({"mods": [{"name": "base", "enabled": True}]}))
                if self.player_name and not player_data.exists():
                    # A nickname permits joining an explicitly unverified
                    # private test server. This is not a Factorio account login.
                    player_data.write_text(json.dumps({"service-username": self.player_name, "service-token": ""}))
                    os.chmod(player_data, 0o600)
                config_file = self.game_write / "config/config.ini"
                if config_file.is_symlink():
                    raise VisualSessionError("Game config must not be a symbolic link")
                if not config_file.exists():
                    config_file.write_text("[path]\nread-data=/factorio/data\nwrite-data=/game-write\n[graphics]\nfull-screen=false\n[general]\ncheck-updates=false\n")
                self.authority.write_bytes(b"")
                os.chmod(self.authority, 0o600)
                self._cookie = secrets.token_hex(16)
                # Xvfb loads the cookie regardless of the initial entry's
                # display number. Add the allocated client entry afterwards.
                self._auth_entry(":65535")
                self._verify_client()
                self._cancel.clear()
                display_log = (self.directory / "xvfb.log").open("ab", buffering=0)
                self._logs.append(display_log)
                display_command = self._sandbox([self.xvfb, "-displayfd", "1", "-screen", "0",
                                                 f"{self.width}x{self.height}x24", "-auth", "/auth/Xauthority",
                                                 "-nolisten", "tcp", "-noreset", "-s", "0", "-dpms"], display=False)
                self._display_process = subprocess.Popen(display_command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                         stderr=display_log, env={"PATH": "/usr/bin:/bin"}, start_new_session=True)
                ready, _, _ = select.select([self._display_process.stdout], [], [], 15)
                if not ready:
                    raise VisualSessionError("Private Xvfb did not become ready; no unsandboxed fallback is permitted")
                number = self._display_process.stdout.readline(32).strip()
                if not number.isdigit() or self._display_process.poll() is not None:
                    raise VisualSessionError("Private Xvfb sandbox failed; inspect xvfb.log")
                self._display = ":" + number.decode("ascii")
                self._auth_entry(self._display)
                # The local abstract transport must remain enabled: Xlib tries
                # it before filesystem sockets in the shared network namespace.
                # displayfd therefore reserves a unique live abstract endpoint,
                # even if a host desktop already owns display :0. Authenticate
                # and capture that owned display before launching any game.
                probe = self._capture()
                if struct.unpack("!II", probe[16:24]) != (self.width, self.height):
                    raise VisualSessionError("The authenticated private display dimensions do not match this session")
                self._server = server
                command = [*self.prefix, "/factorio/" + str(self.binary.relative_to(self.install)),
                           "--config", "/game-write/config/config.ini", "--mod-directory", "/game-write/mods", "--disable-audio",
                           "--window-size", f"{self.width}x{self.height}", "--force-graphics-preset", "very-low"]
                if server:
                    command += ["--mp-connect", server]
                self._client_process = self._spawn(self._sandbox(command, game=True), "factorio-client.log")
                # Detect an immediate sandbox/emulator/argument failure. Loading
                # or server joining continues asynchronously, observable in UI.
                time.sleep(0.25)
                if self._client_process.poll() is not None:
                    raise VisualSessionError("Isolated Factorio client exited during startup; inspect factorio-client.log")
                result = self.status()
                (self.directory / "visual-session.json").write_text(json.dumps(result, indent=2))
                return result
            except BaseException:
                self.close()
                raise

    def status(self):
        with self._lock:
            display_alive = bool(self._display_process and self._display_process.poll() is None)
            client_alive = bool(self._client_process and self._client_process.poll() is None)
            return {"running": display_alive and client_alive, "display": self._display,
                    "width": self.width, "height": self.height, "server": self._server,
                    "pid": self._client_process.pid if client_alive else None,
                    "xvfb_pid": self._display_process.pid if display_alive else None,
                    "isolated": True, "sandbox": "bubblewrap", "mod_free": True,
                    "factorio_version": getattr(self, "_version", None),
                    "client_exit_code": self._client_process.poll() if self._client_process else None}

    def _require_alive(self):
        if not self.status()["running"]:
            raise VisualSessionError("Screenshot/input requires this session's living client and private display")

    def _tool(self, arguments, *, timeout=10):
        result = subprocess.run(self._sandbox([self.xdotool, *arguments]), stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=timeout, env={"PATH": "/usr/bin:/bin"})
        if result.returncode:
            raise VisualSessionError("Private-display input failed")
        return result

    def screenshot(self) -> bytes:
        with self._lock:
            self._require_alive()
            return self._capture()

    def _capture(self):
        if not self._display_process or self._display_process.poll() is not None:
            raise VisualSessionError("The owned virtual display is no longer alive")
        command = self._sandbox([self.capture_python, "/visual-session.py", "--capture"])
        # Only this trusted capture implementation is mounted, never a
        # configurable script or arbitrary code tool.
        command[1:1] = ["--ro-bind", str(Path(__file__).resolve()), "/visual-session.py"]
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
                                env={"PATH": "/usr/bin:/bin"})
        if result.returncode or len(result.stdout) < 24 or not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
            raise VisualSessionError("Private-display authenticated screenshot failed")
        return result.stdout

    def _release_all(self):
        if not self._display_process or self._display_process.poll() is not None:
            self._pressed.clear()
            return
        # Reset the entire permitted game-key vocabulary on this private X
        # server. A timed-out atomic key/type subprocess may have stopped after
        # keydown but before its own keyup; that partial state is not reliably
        # represented by the requested action alone. Zero key delay keeps this
        # bounded reset inexpensive. Never forget tracked holds on failure: a
        # later explicit release must still be able to recover them.
        self._tool(["keyup", "--delay", "0", *sorted(self._pressed | _KEYS | {"ctrl", "shift", "alt"}),
                    "mouseup", "1", "mouseup", "2", "mouseup", "3"], timeout=3)
        self._pressed.clear()

    def action(self, action: dict) -> dict:
        if not isinstance(action, dict) or not isinstance(action.get("action"), str):
            raise ValueError("action must contain a named action")
        name = action["action"]
        if name == "release":
            if set(action) != {"action"}:
                raise ValueError("release accepts no additional fields")
            self._cancel.set()
            with self._lock:
                self._release_all()
            return {"ok": True, "action": name}
        allowed = {"screenshot": set(), "click": {"x", "y", "button", "count"}, "move": {"x", "y"},
                   "key": {"key"}, "hold": {"key", "button", "x", "y", "seconds"}, "scroll": {"amount"}, "text": {"text"}, "wait": {"seconds"}}
        if name not in allowed or set(action) - {"action"} - allowed[name]:
            raise ValueError("Unsupported action or additional fields")
        # Validate fully before making any game input.
        if name in {"click", "move"}:
            x = _integer(action.get("x"), "x", 0, self.width - 1)
            y = _integer(action.get("y"), "y", 0, self.height - 1)
        if name == "click":
            button = _BUTTONS.get(action.get("button", "left"))
            if button is None:
                raise ValueError("button must be left, middle or right")
            count = _integer(action.get("count", 1), "count", 1, 2)
        if name == "hold":
            if ("key" in action) == ("button" in action):
                raise ValueError("hold requires exactly one key or mouse button")
            if "key" in action and ("x" in action or "y" in action):
                raise ValueError("key hold does not accept pointer coordinates")
            if ("x" in action) != ("y" in action):
                raise ValueError("mouse hold requires both pointer coordinates or neither")
            if "button" in action:
                button = _BUTTONS.get(action["button"])
                if button is None:
                    raise ValueError("button must be left, middle or right")
                if "x" in action:
                    x = _integer(action["x"], "x", 0, self.width - 1)
                    y = _integer(action["y"], "y", 0, self.height - 1)
        if name == "key" or (name == "hold" and "key" in action):
            key = _key(action.get("key"))
        if name in {"hold", "wait"}:
            seconds = _seconds(action.get("seconds"), "seconds", 0.02 if name == "hold" else 0.0)
        if name == "scroll":
            amount = _integer(action.get("amount"), "amount", -10, 10)
        if name == "text":
            value = action.get("text")
            if not isinstance(value, str) or not 1 <= len(value) <= 1000 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError("text must contain 1..1000 characters without control characters")
        with self._lock:
            self._require_alive()
            self._cancel.clear()
            if name == "screenshot":
                return {"ok": True, "action": name, "bytes": len(self.screenshot())}
            try:
                if name == "move":
                    self._tool(["mousemove", "--sync", str(x), str(y)])
                elif name == "click":
                    self._tool(["mousemove", "--sync", str(x), str(y), "click", "--repeat", str(count), "--delay", "80", button])
                elif name == "key":
                    self._pressed.add(key)
                    self._tool(["key", "--clearmodifiers", key])
                elif name == "hold":
                    if "key" in action:
                        self._pressed.add(key)
                        self._tool(["keydown", key])
                    else:
                        arguments = (["mousemove", "--sync", str(x), str(y)] if "x" in action else [])
                        self._tool([*arguments, "mousedown", button])
                    self._cancel.wait(seconds)
                elif name == "scroll" and amount:
                    self._tool(["click", "--repeat", str(abs(amount)), "--delay", "40", "4" if amount > 0 else "5"])
                elif name == "text":
                    self._tool(["type", "--clearmodifiers", "--delay", "0", "--", value])
                elif name == "wait":
                    self._cancel.wait(seconds)
            finally:
                # Key holds and interrupted click/type actions can never leave
                # a walking key, modifier or mouse button stuck down.
                self._release_all()
            return {"ok": True, "action": name, "cancelled": self._cancel.is_set()}

    def close(self):
        self._cancel.set()
        with self._lock:
            try:
                self._release_all()
            except (VisualSessionError, OSError, subprocess.SubprocessError):
                pass
            for process in (self._client_process, self._display_process):
                if process is None:
                    continue
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                    except ProcessLookupError:
                        pass
                if process.stdout and process.stdout not in self._logs:
                    process.stdout.close()
            self._client_process = self._display_process = None
            self._display = None
            for log in self._logs:
                log.close()
            self._logs.clear()
            if self._lock_handle:
                fcntl.flock(self._lock_handle, fcntl.LOCK_UN)
                self._lock_handle.close()
                self._lock_handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _XImage(ctypes.Structure):
    _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int), ("xoffset", ctypes.c_int),
                ("format", ctypes.c_int), ("data", ctypes.c_void_p), ("byte_order", ctypes.c_int),
                ("bitmap_unit", ctypes.c_int), ("bitmap_bit_order", ctypes.c_int), ("bitmap_pad", ctypes.c_int),
                ("depth", ctypes.c_int), ("bytes_per_line", ctypes.c_int), ("bits_per_pixel", ctypes.c_int),
                ("red_mask", ctypes.c_ulong), ("green_mask", ctypes.c_ulong), ("blue_mask", ctypes.c_ulong),
                ("obdata", ctypes.c_void_p)]


def _png(width, height, rows):
    def chunk(kind, content):
        return struct.pack("!I", len(content)) + kind + content + struct.pack("!I", zlib.crc32(kind + content) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(b"\0" + row for row in rows), 3)) + chunk(b"IEND", b""))


def _capture_private_display():
    """Run only as a child in the private filesystem sandbox; stdout is PNG."""
    lib = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
    lib.XOpenDisplay.argtypes, lib.XOpenDisplay.restype = [ctypes.c_char_p], ctypes.c_void_p
    lib.XDefaultRootWindow.argtypes, lib.XDefaultRootWindow.restype = [ctypes.c_void_p], ctypes.c_ulong
    lib.XDisplayWidth.argtypes, lib.XDisplayWidth.restype = [ctypes.c_void_p, ctypes.c_int], ctypes.c_int
    lib.XDisplayHeight.argtypes, lib.XDisplayHeight.restype = [ctypes.c_void_p, ctypes.c_int], ctypes.c_int
    lib.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int]
    lib.XGetImage.restype = ctypes.POINTER(_XImage)
    lib.XDestroyImage.argtypes, lib.XDestroyImage.restype = [ctypes.POINTER(_XImage)], ctypes.c_int
    lib.XCloseDisplay.argtypes, lib.XCloseDisplay.restype = [ctypes.c_void_p], ctypes.c_int
    display = lib.XOpenDisplay(None)
    if not display:
        raise VisualSessionError("Private X display is unavailable")
    image = None
    try:
        width, height = lib.XDisplayWidth(display, 0), lib.XDisplayHeight(display, 0)
        image = lib.XGetImage(display, lib.XDefaultRootWindow(display), 0, 0, width, height, ctypes.c_ulong(-1), 2)
        if not image:
            raise VisualSessionError("Private X display did not supply an image")
        frame = image.contents
        if frame.bits_per_pixel not in (24, 32) or (frame.red_mask, frame.green_mask, frame.blue_mask) != (0xff0000, 0xff00, 0xff):
            raise VisualSessionError("Unsupported private display pixel format")
        raw = ctypes.string_at(frame.data, frame.bytes_per_line * height)
        stride = frame.bits_per_pixel // 8
        rows = []
        for y in range(height):
            row = raw[y * frame.bytes_per_line:y * frame.bytes_per_line + width * stride]
            rgb = bytearray(width * 3)
            if frame.byte_order == 0:
                rgb[0::3], rgb[1::3], rgb[2::3] = row[2::stride], row[1::stride], row[0::stride]
            else:
                offset = stride - 3
                rgb[0::3], rgb[1::3], rgb[2::3] = row[offset::stride], row[offset + 1::stride], row[offset + 2::stride]
            rows.append(bytes(rgb))
        return _png(width, height, rows)
    finally:
        if image:
            lib.XDestroyImage(image)
        lib.XCloseDisplay(display)


if __name__ == "__main__":
    if sys.argv[1:] != ["--capture"]:
        raise SystemExit("This module's subprocess entry point only captures its private display")
    sys.stdout.buffer.write(_capture_private_display())
