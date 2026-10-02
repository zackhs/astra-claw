"""Run the mod-free companion without accessing the user's desktop."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import threading
from pathlib import Path

from .auth import ChatGPTAuth, write_private
from .provider import AstraProvider


def configure(directory: Path, install: Path, *, launcher=None, xvfb=None,
              width=1024, height=768, player_name=None):
    """Create a new profile; never copy a human's Factorio settings or tokens."""
    directory = directory.expanduser().resolve()
    install = install.expanduser().resolve()
    if install.is_file():
        install = install.parents[2]
    if not (install / "data/base/info.json").is_file():
        raise ValueError("Provide the installed Factorio directory containing bin/ and data/")
    binaries = [p for p in (install / "bin").glob("*/factorio") if p.is_file()]
    if len(binaries) != 1:
        raise ValueError("The Factorio installation must contain one Linux game executable")
    prefix = []
    if launcher:
        launcher = Path(launcher).expanduser().resolve()
        if not launcher.is_file():
            raise ValueError("The supplied executable launcher does not exist")
        prefix = [str(launcher)]
    result = subprocess.run(prefix + [str(binaries[0]), "--version"],
                            capture_output=True, text=True, timeout=60)
    version = re.search(r"Version:\s*(\d+\.\d+\.\d+).*\bfull\b", result.stdout)
    if result.returncode or not version:
        raise ValueError("Use the full graphical Linux client, not Factorio's headless server")
    tools = {}
    for name in ("bwrap", "xauth", "xdotool", "Xvfb"):
        found = str(Path(xvfb).expanduser().resolve()) if name == "Xvfb" and xvfb else shutil.which(name)
        if not found or not Path(found).is_file():
            raise ValueError(f"Install {name}; see README.md for the Linux prerequisites")
        tools[{"Xvfb": "xvfb"}.get(name, name) + "_binary"] = found
    if not 640 <= width <= 1920 or not 480 <= height <= 1080:
        raise ValueError("Use a preview between 640x480 and 1920x1080")
    if directory == install or directory.is_relative_to(install) or install.is_relative_to(directory):
        raise ValueError("Keep the companion data directory separate from the licensed installation")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    path = directory / "visual-config.json"
    if path.exists():
        raise ValueError("This companion is already configured; use its existing profile or a fresh directory")
    config = {"factorio_install": str(install), "factorio_binary": str(binaries[0].relative_to(install)),
              "factorio_version": version.group(1),
              "binary_prefix": prefix, "width": width, "height": height,
              "model": "gpt-6-astra", "effort": "high", **tools}
    if player_name:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", player_name):
            raise ValueError("Use a player name of 1–60 letters, numbers, underscores or dashes")
        config["player_name"] = player_name
    write_private(path, config)
    return {"configured": True, "version": version.group(1), "data": str(directory),
            "next": "Run astra-claw serve and complete login in the private game preview"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="A user-hosted, mod-free Astra Factorio companion")
    parser.add_argument("--data", type=Path, default=Path.home() / ".local/share/astra-claw")
    parser.add_argument("--account-data", type=Path,
                        help="Optional existing account profile owned by this application")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Configure your licensed full Linux Factorio installation")
    init.add_argument("--factorio-install", type=Path, required=True)
    init.add_argument("--launcher", type=Path, help="Optional explicit emulator executable, e.g. Box64")
    init.add_argument("--xvfb", type=Path, help="Path to Xvfb if it is outside PATH")
    init.add_argument("--width", type=int, default=1024)
    init.add_argument("--height", type=int, default=768)
    init.add_argument("--player-name", help="Distinct player name for a private server without identity verification")
    run = sub.add_parser("serve", help="Open the local control service; the model starts idle")
    run.add_argument("--port", type=int, default=8790)
    run.add_argument("--start-client", action="store_true", help="Start only the hidden client, not Astra")
    run.add_argument("--server", help="Join address when --start-client is selected")
    sub.add_parser("account", help="Show ChatGPT connection status without credentials")
    sub.add_parser("models", help="Check models offered to your authorized account")
    sub.add_parser("logout", help="Disconnect this application's ChatGPT connection")
    claw = sub.add_parser("claw-setup", help="Use a separate installed OpenClaw runtime for visual gameplay")
    claw.add_argument("--runtime", type=Path, required=True)
    claw.add_argument("--gateway-port", type=int, default=18941)
    claw.add_argument("--model-port", type=int, default=8792)
    args = parser.parse_args(argv)
    directory = args.data.expanduser().resolve()
    if args.command == "init":
        print(json.dumps(configure(directory, args.factorio_install, launcher=args.launcher,
                                   xvfb=args.xvfb, width=args.width, height=args.height,
                                   player_name=args.player_name), indent=2))
        return
    if args.command == "claw-setup":
        from .visual_claw import configure_visual_claw
        config_path = directory / "visual-config.json"
        config = json.loads(config_path.read_text())
        config.update(configure_visual_claw(directory, args.runtime, args.gateway_port, args.model_port))
        write_private(config_path, config)
        print("Separate visual OpenClaw profile configured. Start the companion with astra-claw serve.")
        return
    auth = ChatGPTAuth(args.account_data.expanduser().resolve() if args.account_data else directory / "account")
    if args.command == "account":
        print(json.dumps({k: v for k, v in auth.summary().items() if k != "email"}, indent=2))
        return
    if args.command == "models":
        print(json.dumps(auth.models(), indent=2))
        return
    if args.command == "logout":
        print("Signed out" if auth.sign_out() else "Signed out locally; remote revocation was not confirmed")
        return
    if args.server and not args.start_client:
        parser.error("--server requires --start-client")
    if not 1 <= args.port <= 65535:
        parser.error("Use a port between 1 and 65535")
    from .visual_session import VisualSession
    from .visual_agent import VisualAgent
    from .visual_web import serve_visual
    config = json.loads((directory / "visual-config.json").read_text())
    lease = os.open(directory / "service.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lease)
        raise RuntimeError("This companion profile already has a running service") from None
    ending = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: ending.set())
    signal.signal(signal.SIGINT, lambda *_: ending.set())
    with ExitStack() as cleanup:
        cleanup.callback(os.close, lease)
        session = VisualSession(config, directory / "session")
        cleanup.callback(session.close)
        if config.get("agent_runtime") == "openclaw":
            from .model_bridge import serve_model_bridge
            from .visual_claw import VisualClawProvider, start_visual_gateway, stop_visual_gateway
            broker = serve_model_bridge(auth, (directory / "visual-claw-model-token").read_text().strip(),
                                        config["visual_claw_model_port"], config.get("model", "gpt-6-astra"))
            cleanup.callback(broker.server_close)
            threading.Thread(target=broker.serve_forever, daemon=True).start()
            cleanup.callback(broker.shutdown)
            gateway = start_visual_gateway(directory, config)
            cleanup.callback(stop_visual_gateway, gateway)
            provider = VisualClawProvider(auth, config, directory)
        else:
            provider = AstraProvider(auth, config.get("model", "gpt-6-astra"), config.get("effort", "high"))
        agent = VisualAgent(session, provider, directory / "learning")
        cleanup.callback(agent.close)
        if args.start_client:
            session.start(args.server)
        server = serve_visual(session, agent, auth, directory, args.port)
        cleanup.callback(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        cleanup.callback(server.shutdown)
        print(f"Claw desk: http://127.0.0.1:{server.server_port}/", flush=True)
        print("Astra is idle until you start a task. Game input stays inside the private session.", flush=True)
        while not ending.wait(.5):
            pass
        if auth.listener:
            auth.listener.shutdown()
            auth.listener.server_close()


if __name__ == "__main__":
    main()
