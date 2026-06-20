"""Tests for memory/thread_state.py — classifiers + live thread persistence."""
import pytest

from memory import thread_state as ts


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "THREAD_STATE_PATH", str(tmp_path / "active_thread.json"))
    yield


# ── pure classifiers ──────────────────────────────────────────────

def test_detect_thread_type_code():
    assert ts.detect_thread_type("there's a bug in the ekf.py function") == "code"


def test_detect_thread_type_emotional():
    assert ts.detect_thread_type("i've been so stressed and overwhelmed, can't sleep") == "emotional"


def test_detect_thread_type_religious():
    assert ts.detect_thread_type("what does the quran say about this hadith") == "religious"


def test_detect_thread_type_general_fallback():
    assert ts.detect_thread_type("hey what's up") == "general"


def test_is_decision_marker():
    assert ts.is_decision_marker("we decided to go with A-star") is True
    assert ts.is_decision_marker("the root cause is the GPS update") is True
    assert ts.is_decision_marker("what's the weather like") is False


def test_extract_decisions():
    decisions = ts.extract_decisions("After debate we decided to use the MPC controller for this")
    assert any("we decided" in d.lower() for d in decisions)


# ── stateful update / persistence ─────────────────────────────────

def test_update_thread_increments_and_persists():
    ts.update_thread("first message about the rover code bug", role="user")
    ts.update_thread("second message", role="user")
    state = ts.load_thread_state()
    assert state.message_count == 2


def test_emotional_thread_marked_not_compression_safe():
    ts.update_thread("i feel so depressed and stuck, haven't moved in weeks", role="user")
    state = ts.load_thread_state()
    assert state.thread_type == "emotional"
    assert state.compression_safe is False


def test_get_current_thread_type_reflects_update():
    ts.update_thread("debug the navigation sensor module import error", role="user")
    assert ts.get_current_thread_type() == "code"


def test_user_decision_marker_stored_as_critical_context():
    ts.update_thread("the problem is the encoder ticks per rev are wrong", role="user")
    state = ts.load_thread_state()
    assert any("encoder ticks" in c for c in state.critical_context)
