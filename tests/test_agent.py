"""Tests for actions/agent.py — the agentic tool-use loop.

Fully offline: a fake model emits scripted tool calls, a fake executor records
them. Proves the loop feeds results back, pauses for confirmation, and bounds
itself.
"""
from actions import agent


class FakeModel:
    """Returns scripted responses in order; records the messages it was given."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, messages):
        self.calls.append([dict(m) for m in messages])
        if self.responses:
            return self.responses.pop(0)
        return "Nothing more to do."


def fake_executor(tool, params):
    return {"success": True, "output": {"echo": params}, "tool": tool}


def needs_confirm(tool):
    return tool in {"shell", "write_file", "edit_file"}


def run(responses, **kw):
    model = FakeModel(responses)
    result = agent.run_agent(
        model, [{"role": "user", "content": "go"}],
        executor=fake_executor, needs_confirm=needs_confirm, **kw,
    )
    return model, result


# ── basic flows ───────────────────────────────────────────────────

def test_plain_answer_no_tools():
    _, result = run(["Just a normal answer, no tools."])
    assert result["status"] == "done"
    assert "normal answer" in result["text"]


def test_single_tool_then_done():
    model, result = run([
        '```tool\n{"tool": "read_file", "params": {"path": "a.py"}}\n```',
        "The file looks fine. Done.",
    ])
    assert result["status"] == "done"
    assert "Done" in result["text"]
    types = [e["type"] for e in result["events"]]
    assert "tool_call" in types and "tool_result" in types


def test_results_are_fed_back_to_model():
    model, result = run([
        '```tool\n{"tool": "read_file", "params": {"path": "a.py"}}\n```',
        "Final answer.",
    ])
    # The model's SECOND call must include the tool result in its messages.
    second_call_msgs = model.calls[1]
    assert any("[tool results]" in m["content"] for m in second_call_msgs)


def test_multi_step_chain():
    model, result = run([
        '```tool\n{"tool": "list_directory", "params": {"path": "C:\\\\x"}}\n```',
        '```tool\n{"tool": "read_file", "params": {"path": "C:\\\\x\\\\a.py"}}\n```',
        "All done after two steps.",
    ])
    assert result["status"] == "done"
    tool_calls = [e for e in result["events"] if e["type"] == "tool_call"]
    assert [t["tool"] for t in tool_calls] == ["list_directory", "read_file"]


# ── confirmation gating ───────────────────────────────────────────

def test_mutating_tool_pauses_for_confirmation():
    model, result = run([
        '```tool\n{"tool": "shell", "params": {"command": "echo hi"}}\n```',
    ])
    assert result["status"] == "awaiting_confirmation"
    assert result["pending"][0]["tool"] == "shell"
    assert result["transcript"]  # resumable
    # The shell tool must NOT have executed (no tool_result event for it).
    assert not any(e["type"] == "tool_result" for e in result["events"])


def test_resume_after_confirmation_continues():
    # Simulate: paused on shell, user approved, caller ran it, now resume.
    transcript = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": '```tool\n{"tool":"shell","params":{"command":"echo hi"}}\n```'},
    ]
    executed = [({"tool": "shell", "params": {"command": "echo hi"}},
                 {"success": True, "output": {"stdout": "hi"}})]
    model = FakeModel(["Great, it printed hi. Done."])
    events = list(agent.resume_agent(
        model, transcript, executed,
        executor=fake_executor, needs_confirm=needs_confirm,
    ))
    assert events[-1]["type"] == "done"
    assert "hi" in model.calls[0][-1]["content"]  # executed result was fed in


# ── bounds and errors ─────────────────────────────────────────────

def test_max_iterations_cap():
    # Model keeps calling an auto tool forever; loop must stop.
    forever = ['```tool\n{"tool": "read_file", "params": {"path": "a"}}\n```'] * 10
    _, result = run(forever, max_iters=3)
    assert result["status"] == "max_iterations"
    tool_calls = [e for e in result["events"] if e["type"] == "tool_call"]
    assert len(tool_calls) == 3


def test_model_error_is_reported():
    class Boom:
        def __call__(self, messages):
            raise RuntimeError("server down")
    events = list(agent.iter_agent(
        Boom(), [{"role": "user", "content": "go"}],
        executor=fake_executor, needs_confirm=needs_confirm,
    ))
    assert events[-1]["type"] == "error"
    assert "server down" in events[-1]["error"]


# ── strip_tool_blocks ─────────────────────────────────────────────

def test_strip_tool_blocks_removes_fences_and_bare_json():
    text = 'Here goes:\n```tool\n{"tool":"read_file","params":{"path":"a"}}\n```\nand done'
    cleaned = agent.strip_tool_blocks(text)
    assert "read_file" not in cleaned
    assert "Here goes" in cleaned and "and done" in cleaned
