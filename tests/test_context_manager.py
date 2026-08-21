"""Tests for memory/context_manager.py — the 3-tier predictive context.

Pure scoring math + RAM->cache eviction. The embedder (sentence-transformers) is
never loaded: scoring tests inject embeddings, eviction tests don't score.
"""
import time

import pytest

from memory import context_manager as cm
from memory import thread_state as ts


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "CACHE_PATH", str(tmp_path / "context_cache.json"))
    # add_message -> thread_state.update_thread writes a file; send it to tmp.
    monkeypatch.setattr(ts, "THREAD_STATE_PATH", str(tmp_path / "active_thread.json"))
    yield


# ── ContextPredictor pure math ────────────────────────────────────

def test_recency_score_recent_is_high():
    p = cm.ContextPredictor()
    assert p._recency_score(time.time()) > 0.99


def test_recency_score_old_is_low():
    p = cm.ContextPredictor()
    two_hours_ago = time.time() - 2 * 3600
    assert p._recency_score(two_hours_ago) < 0.05


def test_cosine_identical_and_orthogonal():
    p = cm.ContextPredictor()
    assert p._cosine([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)
    assert p._cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert p._cosine([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero vector guarded


def test_thread_coherence_levels():
    p = cm.ContextPredictor()
    assert p._thread_coherence("fix the bug in ekf.py code", "code") == 1.0
    assert p._thread_coherence("just chatting casually", "code") == pytest.approx(0.6)


def test_decision_marker_score_floor():
    p = cm.ContextPredictor()
    msg = cm.CachedMessage(
        role="user", content="hello",
        timestamp=time.time() - 10 * 3600,   # old -> low recency
        score=0.0, is_decision_marker=True,
        embedding=[1.0, 0.0, 0.0],
    )
    score = p.score(msg, "unrelated query", [0.0, 1.0, 0.0], "general")
    assert score >= cm.DECISION_FLOOR


def test_non_decision_low_signals_below_floor():
    p = cm.ContextPredictor()
    msg = cm.CachedMessage(
        role="user", content="hello",
        timestamp=time.time() - 10 * 3600,
        score=0.0, is_decision_marker=False,
        embedding=[1.0, 0.0, 0.0],
    )
    score = p.score(msg, "unrelated query", [0.0, 1.0, 0.0], "general")
    assert score < cm.DECISION_FLOOR


# ── RAM -> cache eviction ─────────────────────────────────────────

def test_ram_evicts_to_cache_when_over_budget():
    mgr = cm.ContextManager()
    big = "a" * 5000  # plain content: not a decision marker
    # Push comfortably past the RAM budget (tracks MAX_RAM_CHARS, which the longer-memory
    # feature raised) so eviction is guaranteed regardless of the configured budget.
    n = (cm.MAX_RAM_CHARS // 5000) + 3
    for _ in range(n):
        mgr.add_message({"role": "user", "content": big})
    # Over budget -> oldest message(s) pushed to cache.
    assert mgr._ram_chars() <= cm.MAX_RAM_CHARS or len(mgr.ram) <= 2
    assert len(mgr.cache) >= 1


def test_summary_stats_shape():
    mgr = cm.ContextManager()
    mgr.add_message({"role": "user", "content": "short msg"})
    stats = mgr.get_summary_stats()
    assert stats["ram_messages"] == 1
    assert "cache_messages" in stats and "cache_decision_markers" in stats


def test_build_context_returns_ram_when_cache_empty():
    mgr = cm.ContextManager()
    mgr.add_message({"role": "user", "content": "hello there"})
    ctx = mgr.build_context("hello there")
    assert ctx == mgr.ram
