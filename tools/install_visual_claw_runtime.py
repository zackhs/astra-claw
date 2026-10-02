"""Install the pinned visual OpenClaw runtime using an explicit local Node 24.

This standalone tool has no Astra, Factorio or account imports. It installs only
open-source npm packages in a new user-owned directory. Node itself is supplied
by the user; no architecture-specific executable or machine path is shipped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path


OPENCLAW_VERSION = "2026.9.7"
MINIMUM_NODE_MINOR = 16
LOCK_PATH = Path(__file__).with_name("visual-claw-package-lock.json")


def npm_script(node, supplied=None):
    if supplied:
        candidates = [Path(supplied).expanduser().resolve()]
    else:
        candidates = [node.parent.parent / "lib/node_modules/npm/bin/npm-cli.js"]
        npm = shutil.which("npm")
        if npm:
            candidates.append(Path(npm).resolve())
    for candidate in candidates:
        if candidate.is_file() and candidate.name == "npm-cli.js":
            return candidate
    raise ValueError("Provide the npm-cli.js included with Node using --npm-cli")


def install(directory, *, node=None, npm_cli=None):
    if platform.system() != "Linux":
        raise ValueError("The visual companion currently supports Linux; install inside your Linux VM")
    node_path = Path(node).expanduser().resolve() if node else Path(shutil.which("node") or "").resolve()
    if not node_path.is_file():
        raise ValueError("Install Node 24 and supply its executable with --node")
    checked = subprocess.run([str(node_path), "--version"], capture_output=True, text=True, check=True, timeout=15)
    matched = re.fullmatch(r"v24\.(\d+)\.\d+", checked.stdout.strip())
    if not matched or int(matched.group(1)) < MINIMUM_NODE_MINOR:
        raise ValueError("Use Node 24.16.0 or newer within Node 24; Node 24.21.0 is the tested version")
    npm = npm_script(node_path, npm_cli)
    lock = json.loads(LOCK_PATH.read_text())
    if lock.get("packages", {}).get("", {}).get("dependencies", {}).get("openclaw") != OPENCLAW_VERSION:
        raise ValueError("The bundled runtime lock does not match the tested OpenClaw version")
    for package in lock["packages"].values():
        resolved = package.get("resolved")
        if resolved and (not resolved.startswith("https://registry.npmjs.org/") or not package.get("integrity")):
            raise ValueError("Runtime dependencies must use the public registry with integrity hashes")
    lock_text = json.dumps(lock, indent=2) + "\n"
    lock_hash = hashlib.sha256(lock_text.encode()).hexdigest()
    root = Path(directory).expanduser().resolve()
    marker_path = root / "visual-runtime.json"
    if root.exists() and any(root.iterdir()):
        if not marker_path.is_file():
            raise ValueError("Use a new empty runtime directory; existing unrelated installations are left untouched")
        marker = json.loads(marker_path.read_text())
        if (marker.get("lock_sha256") != lock_hash or marker.get("node_binary") != str(node_path)
                or marker.get("node_version") != checked.stdout.strip()):
            raise ValueError("Existing visual runtime differs; use a new runtime directory")
        if marker.get("status") == "complete":
            if not (root / "openclaw/node_modules/openclaw/openclaw.mjs").is_file() or not (root / "node-runtime.json").is_file():
                raise ValueError("Existing visual runtime is incomplete; use a new directory")
            return {"runtime": str(root), "node_version": marker["node_version"], "openclaw_version": OPENCLAW_VERSION,
                    "installed": False, "reused": True}
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    marker = {"status": "installing", "lock_sha256": lock_hash, "node_binary": str(node_path),
              "node_version": checked.stdout.strip(), "openclaw_version": OPENCLAW_VERSION}
    marker_path.write_text(json.dumps(marker, indent=2) + "\n")
    marker_path.chmod(0o600)
    node_directory = root / "node"
    private_node = node_directory / "bin/node"
    private_node.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not private_node.exists():
        private_node.symlink_to(node_path)
    elif private_node.resolve() != node_path:
        raise ValueError("Existing private Node executable differs; use a new runtime directory")
    package_directory = root / "openclaw"
    package_directory.mkdir(exist_ok=True, mode=0o700)
    manifest = {"name": "astra-visual-runtime", "version": "1.0.0", "private": True,
                "dependencies": {"openclaw": OPENCLAW_VERSION}}
    (package_directory / "package.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (package_directory / "package-lock.json").write_text(lock_text)
    npm_home = root / "npm-home"
    npm_configs = root / "npm-config"
    for private_directory in (npm_home, npm_configs):
        private_directory.mkdir(exist_ok=True, mode=0o700)
        private_directory.chmod(0o700)
    # npm rejects loading one file as both user and global configuration. Keep
    # two distinct empty files, rather than pointing both layers at /dev/null.
    user_config = npm_configs / "user.npmrc"
    global_config = npm_configs / "global.npmrc"
    for config_file in (user_config, global_config):
        config_file.write_text("")
        config_file.chmod(0o600)
    env = {key: os.environ[key] for key in ("LANG", "LC_ALL", "TZ") if key in os.environ}
    env.update(PATH=str(private_node.parent) + os.pathsep + os.defpath,
               HOME=str(npm_home), NPM_CONFIG_USERCONFIG=str(user_config), NPM_CONFIG_GLOBALCONFIG=str(global_config),
               NPM_CONFIG_CACHE=str(root / "npm-cache"))
    subprocess.run([str(node_path), str(npm), "ci", "--prefix", str(package_directory),
                    "--ignore-scripts", "--no-audit", "--no-fund", "--registry=https://registry.npmjs.org"],
                   env=env, cwd=root, check=True, timeout=1800)
    entry = package_directory / "node_modules/openclaw/openclaw.mjs"
    if not entry.is_file():
        raise ValueError("The installed OpenClaw runtime is missing its entry point")
    package = json.loads((entry.parent / "package.json").read_text())
    if package.get("version") != OPENCLAW_VERSION:
        raise ValueError("The installed OpenClaw version does not match the locked version")
    metadata_path = root / "node-runtime.json"
    metadata_path.write_text(json.dumps({"version": checked.stdout.strip(), "directory": str(node_directory),
                                         "source": "user-installed-node", "openclaw_version": OPENCLAW_VERSION,
                                         "package_lock_sha256": lock_hash}, indent=2) + "\n")
    metadata_path.chmod(0o600)
    marker["status"] = "complete"
    marker_path.write_text(json.dumps(marker, indent=2) + "\n")
    return {"runtime": str(root), "node_version": checked.stdout.strip(), "openclaw_version": OPENCLAW_VERSION,
            "installed": True, "reused": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="New private runtime directory")
    parser.add_argument("--node", type=Path, help="User-installed Node 24 executable; defaults to PATH")
    parser.add_argument("--npm-cli", type=Path, help="npm-cli.js if not discoverable beside Node")
    args = parser.parse_args(argv)
    print(json.dumps(install(args.directory, node=args.node, npm_cli=args.npm_cli), indent=2))


if __name__ == "__main__":
    main()
