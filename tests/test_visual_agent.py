"""Isolated visual-agent behavior, with no game/model/network contacted."""
import base64
import json
import re
import threading
import time

import pytest

from astra_engineer.visual_agent import TOOLS, VisualAgent, validate_action


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


@pytest.mark.parametrize("key,expected", [("`", "grave"), ("grave", "grave"),
                                         ("Ctrl+a", "Ctrl+a"), ("minus", "minus"),
                                         ("Page_Up", "Page_Up")])
def test_factorio_chat_and_interface_keys_reach_private_session(key, expected):
    from astra_engineer.visual_session import _key
    value = validate_action({"action": "key", "key": key})
    assert value["key"] == expected
    assert _key(value["key"])


class Session:
    def __init__(self):
        self.actions = []
        self.screenshots = 0

    def action(self, action):
        self.actions.append(dict(action))
        return {"ok": True, "action": action["action"]}

    def screenshot(self):
        self.screenshots += 1
        return PNG


def call(name, arguments, call_id="call-test"):
    return {"type": "function_call", "namespace": "factorio", "name": name,
            "call_id": call_id, "arguments": json.dumps(arguments)}


def response(*items):
    return {"status": "completed", "output": list(items)}


def observation(history):
    for item in reversed(history):
        content = item.get("content")
        if isinstance(content, list):
            for part in content:
                if part.get("type") == "input_text":
                    match = re.search(r"observation_id=([a-f0-9-]+)", part["text"])
                    if match:
                        return match[1]
    raise AssertionError("No screenshot observation supplied")


class Provider:
    model = "gpt-6-astra"

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def respond(self, instructions, history, tools, cancel):
        index = len(self.calls)
        self.calls.append({"instructions": instructions, "history": history, "tools": tools, "cancel": cancel})
        if index >= len(self.steps):
            return response(call("ask_owner", {"message": "Test provider exhausted; pause"}))
        step = self.steps[index]
        return step(history, cancel) if callable(step) else step


def wait_state(agent, state, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = agent.status()
        if status["state"] == state:
            return status
        time.sleep(.01)
    raise AssertionError(f"Expected {state}, got {agent.status()}")


def finished(history, cancel):
    return response(call("finish", {"summary": "Requested result is visible", "evidence": "The latest observed game frame shows the result", "observation_id": observation(history)}))


def test_no_model_or_screenshot_before_owner_task(tmp_path):
    session, provider = Session(), Provider([])
    agent = VisualAgent(session, provider, tmp_path)
    try:
        agent.preferences({"layout": "Conveyors feed the central hub"})
        time.sleep(.08)
        assert provider.calls == []
        assert session.screenshots == 0
        assert agent.status()["state"] == "idle"
        assert not list((tmp_path / "evidence").rglob("*.png"))
        assert {tool["name"] for tool in TOOLS} == {"game_action", "remember", "finish", "ask_owner"}
    finally:
        agent.close()


def test_real_png_inputs_after_actions_and_visual_completion(tmp_path):
    session = Session()
    provider = Provider([response(call("game_action", {"action": "click", "x": 10, "y": 20, "button": "left", "count": 1})), finished])
    agent = VisualAgent(session, provider, tmp_path)
    try:
        agent.preferences({"layout": "Conveyors feed the central hub"})
        task = agent.task("Build a belt to the central hub")
        status = wait_state(agent, "completed")
        assert status["task_id"] == task["task_id"]
        assert session.screenshots == 3  # Initial, after input, final checkpoint.
        assert [a for a in session.actions if a["action"] != "release"] == [{"action": "click", "x": 10, "y": 20, "button": "left", "count": 1}]
        assert status["verification"]["level"] == "model_visual"
        assert (tmp_path / status["verification"]["screenshot"]).read_bytes() == PNG
        assert (tmp_path / status["verification"]["checkpoint"]).read_bytes() == PNG
        images = [part for item in provider.calls[0]["history"] if isinstance(item.get("content"), list)
                  for part in item["content"] if part["type"] == "input_image"]
        assert images[0]["image_url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()
        assert "Conveyors feed the central hub" in provider.calls[0]["instructions"]
        assert "Build a belt" in provider.calls[0]["instructions"]
        assert (tmp_path / "visual-agent.json").stat().st_mode & 0o777 == 0o600
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in (tmp_path / "evidence").rglob("*.png"))
    finally:
        agent.close()


def test_steering_invalidates_already_streaming_action_and_keeps_task(tmp_path):
    started, release = threading.Event(), threading.Event()

    def old_plan(history, cancel):
        started.set()
        assert release.wait(2)
        # An uncooperative provider returns a complete stale plan anyway.
        return response(call("game_action", {"action": "key", "key": "w"}))

    session, provider = Session(), Provider([old_plan, response(call("ask_owner", {"message": "Read the owner's change"}))])
    agent = VisualAgent(session, provider, tmp_path)
    try:
        task = agent.task("Build a mine")
        assert started.wait(2)
        original_observation = observation(provider.calls[0]["history"])
        steering = agent.steer("Leave the copper patch alone and use stone instead")
        assert steering["task_id"] == task["task_id"]
        assert provider.calls[0]["cancel"].is_set()
        release.set()
        status = wait_state(agent, "paused")
        assert not [a for a in session.actions if a["action"] != "release"]
        assert observation(provider.calls[1]["history"]) != original_observation
        assert "use stone instead" in provider.calls[1]["instructions"]
        assert any("use stone instead" in str(item.get("content")) for item in provider.calls[1]["history"])
        assert status["steering"][0]["text"].endswith("use stone instead")
        assert status["verification"] is None
    finally:
        release.set()
        agent.close()


def test_pause_discards_stale_response_resume_observes_fresh_frame(tmp_path):
    started, release = threading.Event(), threading.Event()

    def old_plan(history, cancel):
        started.set()
        assert release.wait(2)
        return response(call("game_action", {"action": "hold", "key": "w", "seconds": 2}))

    session, provider = Session(), Provider([old_plan, finished])
    agent = VisualAgent(session, provider, tmp_path)
    try:
        agent.task("Inspect the iron patch")
        assert started.wait(2)
        old_observation = observation(provider.calls[0]["history"])
        paused = agent.pause()
        assert paused["state"] == "paused"
        assert session.actions[-1] == {"action": "release"}
        agent.steer("Be careful near the player's machinery")
        assert agent.status()["state"] == "paused"
        release.set()
        time.sleep(.1)
        assert len(provider.calls) == 1
        agent.resume()
        wait_state(agent, "completed")
        assert observation(provider.calls[1]["history"]) != old_observation
        assert not [a for a in session.actions if a["action"] != "release"]
    finally:
        release.set()
        agent.close()


def test_restart_keeps_preferences_history_and_lessons_without_auto_play(tmp_path):
    def learned(history, cancel):
        return response(call("remember", {"lesson": "Inspect both belt lanes before changing routing", "observation_id": observation(history)}))

    session, provider = Session(), Provider([learned, response(call("ask_owner", {"message": "Need belts"}))])
    agent = VisualAgent(session, provider, tmp_path)
    agent.preferences({"layout": "Conveyors feed the central hub"})
    agent.task("Route stone")
    old = wait_state(agent, "paused")
    agent.close()
    restored_session, restored_provider = Session(), Provider([finished])
    restored = VisualAgent(restored_session, restored_provider, tmp_path)
    try:
        time.sleep(.08)
        state = restored.status()
        assert state["state"] == "paused"
        assert state["task_id"] == old["task_id"]
        assert state["preferences"] == {"layout": "Conveyors feed the central hub"}
        assert state["lessons"][0]["source"] == "model_visual_observation"
        assert restored_session.screenshots == 0 and restored_provider.calls == []
        restored.resume()
        wait_state(restored, "completed")
        assert restored_session.screenshots == 2
    finally:
        restored.close()


def test_completion_after_same_response_action_is_rejected(tmp_path):
    def premature(history, cancel):
        return response(call("game_action", {"action": "key", "key": "e"}, "input"),
                        call("finish", {"summary": "Done", "evidence": "Claimed without looking", "observation_id": observation(history)}, "finish"))

    session, provider = Session(), Provider([premature, response(call("ask_owner", {"message": "Need another observation"}))])
    agent = VisualAgent(session, provider, tmp_path)
    try:
        agent.task("Check inventory")
        status = wait_state(agent, "paused")
        assert status["verification"] is None
        finish_outputs = [item for item in provider.calls[1]["history"] if item.get("type") == "function_call_output" and item.get("call_id") == "finish"]
        assert "Inspect the latest action screenshot" in finish_outputs[0]["output"]
    finally:
        agent.close()


def test_unknown_code_tool_never_executes_and_calls_are_paired(tmp_path):
    session, provider = Session(), Provider([response(call("execute", {"code": "import os; os.system('touch /tmp/should-not-exist')"})), finished])
    agent = VisualAgent(session, provider, tmp_path)
    try:
        agent.task("Inspect factory")
        wait_state(agent, "completed")
        assert not [a for a in session.actions if a["action"] != "release"]
        outputs = [item for item in provider.calls[1]["history"] if item.get("type") == "function_call_output"]
        assert "Unknown tool" in outputs[0]["output"]
    finally:
        agent.close()


def test_manual_input_is_serialized_and_requires_pause(tmp_path):
    started, release = threading.Event(), threading.Event()

    def thinking(history, cancel):
        started.set()
        assert release.wait(2)
        return finished(history, cancel)

    session, provider = Session(), Provider([thinking])
    agent = VisualAgent(session, provider, tmp_path)
    try:
        assert agent.manual_action({"action": "key", "key": "Tab"})["ok"]
        assert agent.manual_screenshot() == PNG
        assert not list((tmp_path / "evidence").rglob("*.png"))
        agent.task("Inspect factory")
        assert started.wait(2)
        with pytest.raises(ValueError, match="Pause"):
            agent.manual_action({"action": "key", "key": "Tab"})
        agent.pause()
        assert agent.manual_action({"action": "key", "key": "Tab"})["ok"]
    finally:
        release.set()
        agent.close()


def test_errors_visible_and_unfinished_stream_never_applies_inputs(tmp_path):
    session, provider = Session(), Provider([{"status": "incomplete", "output": [call("game_action", {"action": "key", "key": "w"})]}])
    agent = VisualAgent(session, provider, tmp_path)
    try:
        agent.task("Walk north")
        state = wait_state(agent, "error")
        assert "completed, replayable" in state["error"]
        assert state["verification"] is None
        assert not [a for a in session.actions if a["action"] != "release"]
    finally:
        agent.close()


def test_history_compacts_whole_tool_turns_and_sends_only_two_images(tmp_path):
    steps = [response(call("game_action", {"action": "screenshot"}, f"screenshot-{index}")) for index in range(28)] + [finished]
    session, provider = Session(), Provider(steps)
    agent = VisualAgent(session, provider, tmp_path)
    try:
        agent.task("Observe factory for a while")
        status = wait_state(agent, "completed", timeout=5)
        assert status["history_turns"] <= 24
        history = provider.calls[-1]["history"]
        calls = {i["call_id"] for i in history if i.get("type") == "function_call"}
        outputs = {i["call_id"] for i in history if i.get("type") == "function_call_output"}
        assert calls == outputs
        images = [part for item in history if isinstance(item.get("content"), list) for part in item["content"] if part.get("type") == "input_image"]
        assert len(images) == 2
        assert "Observe factory for a while" in provider.calls[-1]["instructions"]
    finally:
        agent.close()


@pytest.mark.parametrize("action", [
    {"action": "shell", "text": "echo hi"}, {"action": "release"},
    {"action": "key", "key": "XF86PowerOff"}, {"action": "key", "key": "w; touch /tmp/x"},
    {"action": "hold", "key": "w", "seconds": 100}, {"action": "wait", "seconds": float("nan")},
    {"action": "hold", "key": "w", "button": "right", "seconds": 1},
    {"action": "hold", "button": "right", "x": 1, "seconds": 1},
    {"action": "click", "x": True, "y": 1, "button": "left", "count": 1},
    {"action": "text", "text": "hello\n/c do_bad_things()"},
    {"action": "text", "text": "/c game.player.cheat_mode=true"},
    {"action": "text", "text": " /silent-command game.speed=100"},
    {"action": "screenshot", "code": "print(1)"},
])
def test_bounded_actions_reject_shell_console_arbitrary_keys_and_parameters(action):
    with pytest.raises(ValueError):
        validate_action(action)


def test_strict_nullable_tool_fields_are_removed_before_session_action():
    value = {name: None for name in TOOLS[0]["parameters"]["properties"]}
    value.update(action="hold", key="Shift+w", seconds=1)
    assert validate_action(value) == {"action": "hold", "key": "Shift+w", "seconds": 1}


def test_bounded_mouse_hold_supports_native_mining():
    action = {"action": "hold", "button": "right", "x": 400, "y": 200, "seconds": 2}
    assert validate_action(action) == action
    assert validate_action({"action": "hold", "button": "right", "seconds": .5}) == {"action": "hold", "button": "right", "seconds": .5}


def test_preferences_validation_does_not_mutate_existing_state(tmp_path):
    agent = VisualAgent(Session(), Provider([]), tmp_path)
    try:
        original = {str(i): "Preserved owner preference" for i in range(30)}
        agent.preferences(original)
        with pytest.raises(ValueError, match="At most 30"):
            agent.preferences({"new": "Too many"})
        assert agent.status()["preferences"] == original
    finally:
        agent.close()


def test_dead_client_release_does_not_prevent_close_or_start_inference(tmp_path):
    class DeadSession(Session):
        def action(self, action):
            raise RuntimeError("Private subprocess included a fake login secret")

    session, provider = DeadSession(), Provider([])
    agent = VisualAgent(session, provider, tmp_path)
    state = agent.task("Start after login")
    assert state["state"] == "error"
    assert "fake login secret" not in state["error"]
    time.sleep(.08)
    assert provider.calls == [] and session.screenshots == 0
    assert agent.resume()["state"] == "error"
    assert agent.pause()["state"] == "paused"
    assert agent.steer("Use stone instead")["state"] == "error"
    agent.close()
    assert agent.status()["state"] == "closed"
    assert not agent._worker.is_alive()
    saved = json.loads((tmp_path / "visual-agent.json").read_text())
    assert saved["state"] == "paused"
    assert "fake login secret" not in json.dumps(saved)


def test_release_failure_on_pause_stops_already_streaming_mutation(tmp_path):
    started, release = threading.Event(), threading.Event()

    class SessionDies(Session):
        dead = False
        def action(self, action):
            if self.dead:
                raise RuntimeError("Dead game process")
            return super().action(action)

    def delayed(history, cancel):
        started.set()
        assert release.wait(2)
        return response(call("game_action", {"action": "key", "key": "w"}))

    session, provider = SessionDies(), Provider([delayed])
    agent = VisualAgent(session, provider, tmp_path)
    try:
        agent.task("Walk to stone")
        assert started.wait(2)
        session.dead = True
        assert agent.pause()["state"] == "paused"
        assert provider.calls[0]["cancel"].is_set()
        release.set()
        time.sleep(.08)
        assert not [a for a in session.actions if a["action"] != "release"]
        assert agent.status()["state"] == "paused"
    finally:
        release.set()
        agent.close()


def test_manual_release_allowed_even_while_running(tmp_path):
    started, release = threading.Event(), threading.Event()

    def delayed(history, cancel):
        started.set()
        assert release.wait(2)
        return finished(history, cancel)

    session, agent = Session(), None
    agent = VisualAgent(session, Provider([delayed]), tmp_path)
    try:
        agent.task("Inspect factory")
        assert started.wait(2)
        assert agent.manual_action({"action": "release"})["ok"]
        assert agent.status()["state"] == "running"
    finally:
        release.set()
        agent.close()
