"""Standalone setup tests; no npm install or network calls executed."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astra_engineer.visual_claw import configure_visual_claw


SCRIPT = Path(__file__).resolve().parents[1] / "tools/install_visual_claw_runtime.py"
spec = importlib.util.spec_from_file_location("install_visual_claw_runtime", SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def node_install(tmp_path):
    root = tmp_path / "user-node"
    node = root / "bin/node"
    npm = root / "lib/node_modules/npm/bin/npm-cli.js"
    node.parent.mkdir(parents=True)
    npm.parent.mkdir(parents=True)
    node.write_text("// Fake user Node, never executed")
    npm.write_text("// Fake npm, never executed")
    return node, npm


def fake_subprocess(monkeypatch, version="v24.21.0", failure=False):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[-1] == "--version":
            return SimpleNamespace(stdout=version + "\n", returncode=0)
        if failure:
            raise RuntimeError("Simulated interrupted npm install")
        prefix = Path(command[command.index("--prefix") + 1])
        entry = prefix / "node_modules/openclaw/openclaw.mjs"
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("// Fake locked OpenClaw entry")
        (entry.parent / "package.json").write_text(json.dumps({"version": "2026.9.7"}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(installer.subprocess, "run", run)
    monkeypatch.setattr(installer.platform, "system", lambda: "Linux")
    return calls


def test_installer_creates_generic_runtime_consumable_by_visual_setup(tmp_path, monkeypatch):
    node, npm = node_install(tmp_path)
    calls = fake_subprocess(monkeypatch)
    root = tmp_path / "private-runtime"
    result = installer.install(root, node=node, npm_cli=npm)
    assert result["installed"] is True and result["openclaw_version"] == "2026.9.7"
    assert len(calls) == 2
    command, options = calls[1]
    assert command[:3] == [str(node), str(npm), "ci"]
    assert "--ignore-scripts" in command and "--no-audit" in command
    assert "--registry=https://registry.npmjs.org" in command
    user_config = Path(options["env"]["NPM_CONFIG_USERCONFIG"])
    global_config = Path(options["env"]["NPM_CONFIG_GLOBALCONFIG"])
    assert user_config != global_config
    assert user_config == root / "npm-config/user.npmrc"
    assert global_config == root / "npm-config/global.npmrc"
    assert user_config.read_text() == global_config.read_text() == ""
    assert user_config.stat().st_mode & 0o777 == global_config.stat().st_mode & 0o777 == 0o600
    assert options["env"]["HOME"] == str(root / "npm-home")
    assert options["env"]["NPM_CONFIG_CACHE"] == str(root / "npm-cache")
    assert json.loads((root / "openclaw/package.json").read_text())["dependencies"] == {"openclaw": "2026.9.7"}
    assert (root / "node/bin/node").resolve() == node
    metadata = json.loads((root / "node-runtime.json").read_text())
    assert metadata["directory"] == str(root / "node")
    assert metadata["source"] == "user-installed-node"
    updates = configure_visual_claw(tmp_path / "companion", root)
    assert Path(updates["visual_claw_node"]).resolve() == node
    assert "runtime/claw/node-v24.21.0-linux-arm64" not in json.dumps(metadata)


def test_installer_reuses_completed_runtime_without_mutating_dependencies(tmp_path, monkeypatch):
    node, npm = node_install(tmp_path)
    calls = fake_subprocess(monkeypatch)
    root = tmp_path / "private-runtime"
    installer.install(root, node=node, npm_cli=npm)
    entry = root / "openclaw/node_modules/openclaw/openclaw.mjs"
    entry.write_text("Preserved existing installation")
    again = installer.install(root, node=node, npm_cli=npm)
    assert again["reused"] is True and again["installed"] is False
    assert len([command for command, _ in calls if "ci" in command]) == 1
    assert entry.read_text() == "Preserved existing installation"


def test_installer_refuses_existing_unrelated_runtime(tmp_path, monkeypatch):
    node, npm = node_install(tmp_path)
    calls = fake_subprocess(monkeypatch)
    root = tmp_path / "existing-live-runtime"
    root.mkdir()
    (root / "owner-config.json").write_text("Do not modify")
    with pytest.raises(ValueError, match="unrelated"):
        installer.install(root, node=node, npm_cli=npm)
    assert (root / "owner-config.json").read_text() == "Do not modify"
    assert not [command for command, _ in calls if "ci" in command]


@pytest.mark.parametrize("version", ["v22.22.2", "v24.15.9", "v26.1.0", "not-a-node-version"])
def test_installer_requires_tested_node_major_and_openclaw_minimum(tmp_path, monkeypatch, version):
    node, npm = node_install(tmp_path)
    calls = fake_subprocess(monkeypatch, version=version)
    with pytest.raises(ValueError, match="Node 24.16"):
        installer.install(tmp_path / "runtime", node=node, npm_cli=npm)
    assert len(calls) == 1


def test_installer_does_not_inherit_account_or_host_configuration(tmp_path, monkeypatch):
    node, npm = node_install(tmp_path)
    calls = fake_subprocess(monkeypatch)
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CODEX_HOME", "OPENCLAW_STATE_DIR", "DISPLAY", "NODE_OPTIONS", "NPM_CONFIG_REGISTRY", "HOME"):
        monkeypatch.setenv(key, "fake-host-secret")
    installer.install(tmp_path / "runtime", node=node, npm_cli=npm)
    assert "fake-host-secret" not in json.dumps(calls[1][1]["env"])


def test_failed_install_preserves_a_recoverable_marker_without_ready_metadata(tmp_path, monkeypatch):
    node, npm = node_install(tmp_path)
    fake_subprocess(monkeypatch, failure=True)
    root = tmp_path / "runtime"
    with pytest.raises(RuntimeError, match="interrupted"):
        installer.install(root, node=node, npm_cli=npm)
    assert json.loads((root / "visual-runtime.json").read_text())["status"] == "installing"
    assert not (root / "node-runtime.json").exists()


def test_bundled_lock_has_exact_version_and_integrity_checked_public_urls():
    lock = json.loads(installer.LOCK_PATH.read_text())
    assert lock["packages"][""]["dependencies"]["openclaw"] == "2026.9.7"
    assert lock["packages"]["node_modules/openclaw"]["version"] == "2026.9.7"
    for value in lock["packages"].values():
        if "resolved" in value:
            assert value["resolved"].startswith("https://registry.npmjs.org/")
            assert value["integrity"].startswith("sha512-") or value["integrity"].startswith("sha256-")
