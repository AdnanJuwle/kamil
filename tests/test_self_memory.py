"""Tests for core/self_memory.py + the remember/update_memory tools + the
parse->execute->strip flow that lets Kamil save memory during normal chat."""
import pytest

from core import self_memory
from actions import tools
from actions.agent import strip_tool_blocks


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(self_memory, "MEMORY_PATH", str(tmp_path / "kamil_memory.md"))
    yield


# ── store ─────────────────────────────────────────────────────────

def test_append_and_load():
    self_memory.append_note("Adnan prefers blunt answers")
    text = self_memory.load_memory()
    assert "Adnan prefers blunt answers" in text
    assert text.startswith("# Kamil's Memory")


def test_append_rejects_empty():
    assert self_memory.append_note("   ")["success"] is False


def test_set_memory_replaces_body():
    self_memory.append_note("old note one")
    self_memory.append_note("old note two")
    self_memory.set_memory("- consolidated single note")
    text = self_memory.load_memory()
    assert "consolidated single note" in text
    assert "old note one" not in text


def test_is_over_cap():
    assert self_memory.is_over_cap() is False
    self_memory.set_memory("x" * (self_memory.MAX_CHARS + 100))
    assert self_memory.is_over_cap() is True


def test_memory_for_prompt_truncates():
    self_memory.set_memory("y" * (self_memory.INJECT_HARD_CAP + 5000))
    out = self_memory.memory_for_prompt()
    assert len(out) <= self_memory.INJECT_HARD_CAP + 60
    assert "truncated" in out


# ── tools ─────────────────────────────────────────────────────────

def test_remember_tool():
    res = tools.execute_tool("remember", {"note": "model is Lexi-Uncensored"})
    assert res.success is True
    assert "Lexi-Uncensored" in self_memory.load_memory()
    assert tools.requires_confirmation("remember") is False  # auto


def test_update_memory_tool():
    tools.execute_tool("remember", {"note": "scratch"})
    res = tools.execute_tool("update_memory", {"content": "- clean note"})
    assert res.success is True
    assert "scratch" not in self_memory.load_memory()
    assert "clean note" in self_memory.load_memory()


def test_forget_removes_only_matching_line():
    self_memory.append_note("Adnan likes chicken popcorn")
    self_memory.append_note("Adnan ships on Fridays")
    res = self_memory.forget("chicken popcorn")
    assert res["removed"] == 1
    text = self_memory.load_memory()
    assert "chicken popcorn" not in text
    assert "ships on Fridays" in text          # the other note survives
    assert text.startswith("# Kamil's Memory")  # header survives


def test_forget_cannot_wipe_header_or_all():
    self_memory.append_note("only note")
    # a query matching the header words must not delete the header
    self_memory.forget("Kamil's Memory")
    assert self_memory.load_memory().startswith("# Kamil's Memory")


def test_forget_via_tool():
    self_memory.append_note("temp fact about cats")
    res = tools.execute_tool("forget", {"query": "cats"})
    assert res.success is True and res.output["removed"] == 1
    assert "cats" not in self_memory.load_memory()


# ── the in-chat save flow (mirrors orchestrator._apply_memory_ops) ─

def _apply(text):
    calls = [c for c in tools.parse_tool_calls(text)
             if c.get("tool") in ("remember", "update_memory")]
    for c in calls:
        tools.execute_tool(c["tool"], c["params"])
    return strip_tool_blocks(text) if calls else text


def test_remember_during_chat_saves_and_strips():
    reply = ('Got it, I\'ll keep that in mind.\n'
             '```tool\n{"tool": "remember", "params": {"note": "Adnan ships on Fridays"}}\n```')
    cleaned = _apply(reply)
    assert "Adnan ships on Fridays" in self_memory.load_memory()  # saved
    assert "remember" not in cleaned and "```" not in cleaned     # hidden from view
    assert "Got it" in cleaned                                    # answer kept


def test_plain_reply_untouched():
    reply = "just a normal answer, nothing to remember"
    assert _apply(reply) == reply
    assert self_memory.load_memory() == ""  # nothing written
