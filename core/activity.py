"""A tiny thread-safe registry of Kamil's background work.

Background tasks (indexing, project scans, loading the heavy model, verifying a
speaker, re-indexing edited files) announce themselves here so the UI's presence
panel can show what Kamil is doing when he isn't answering you directly.

Nothing here is faked — a task only appears while some real code is running it.
Use begin/end, or the `task(...)` context manager.
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_active: dict[int, dict] = {}      # token -> {label, detail, started}
_recent: list[dict] = []           # finished tasks, oldest -> newest, capped
_counter = 0
_MAX_RECENT = 8


def begin(label: str, detail: str = "") -> int:
    """Announce that a background task has started. Returns a token for end()."""
    global _counter
    with _lock:
        _counter += 1
        tok = _counter
        _active[tok] = {"label": label, "detail": detail, "started": time.time()}
    return tok


def update(tok: int, detail: str) -> None:
    """Update the detail line of a running task (e.g. progress)."""
    with _lock:
        if tok in _active:
            _active[tok]["detail"] = detail


def end(tok: int, ok: bool = True, detail: str | None = None) -> None:
    """Mark a background task finished; it moves to the 'recent' list briefly."""
    with _lock:
        item = _active.pop(tok, None)
        if item is not None:
            _recent.append({
                "label": item["label"],
                "detail": detail if detail is not None else item["detail"],
                "ok": ok,
                "ended": time.time(),
            })
            del _recent[:-_MAX_RECENT]


def snapshot(recent_window: float = 45.0) -> dict:
    """Current active tasks + recently-finished ones (within recent_window secs)."""
    now = time.time()
    with _lock:
        active = [
            {"label": v["label"], "detail": v["detail"], "elapsed": round(now - v["started"], 1)}
            for v in _active.values()
        ]
        recent = [
            {"label": r["label"], "detail": r["detail"], "ok": r["ok"], "ago": round(now - r["ended"], 1)}
            for r in reversed(_recent)
            if now - r["ended"] <= recent_window
        ]
    return {"active": active, "recent": recent}


class task:
    """Context manager: `with activity.task("indexing skill", name): ...`."""

    def __init__(self, label: str, detail: str = ""):
        self.label = label
        self.detail = detail
        self.tok = 0

    def __enter__(self) -> "task":
        self.tok = begin(self.label, self.detail)
        return self

    def note(self, detail: str) -> None:
        update(self.tok, detail)

    def __exit__(self, exc_type, exc, tb) -> None:
        end(self.tok, ok=exc_type is None)
