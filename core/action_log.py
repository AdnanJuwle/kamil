"""
core/action_log.py — a persistent log of everything Kamil DOES.

Every tool the agent runs (write_file, read_file, shell, web_search, …) and every
background job (indexing, model loads, project scans, speaker checks) is recorded
here, newest last, capped to the last N. The Work tab reads this so you can see
Kamil's actions over time — not just what's running right this second.
"""
from __future__ import annotations

import json
import os
import threading
import time

from core.config import DATA_DIR

LOG_PATH = os.path.join(DATA_DIR, "action_log.json")
_MAX = int(os.getenv("KAMIL_ACTION_LOG_MAX", "400"))
_lock = threading.Lock()

# keys whose value makes a good one-line summary of a tool call
_ARG_KEYS = ("path", "command", "pattern", "query", "url", "name", "title",
             "project_name", "text", "old")


def _short_arg(tool: str, params) -> str:
    p = params if isinstance(params, dict) else {}
    for k in _ARG_KEYS:
        v = p.get(k)
        if v:
            v = str(v)
            if k == "path":
                v = v.replace("\\", "/").rstrip("/").split("/")[-1]
            return v[:80]
    return ""


def _load() -> list:
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(log: list) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    tmp = LOG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(log, f)
    os.replace(tmp, LOG_PATH)


def record(kind: str, arg: str = "", ok: bool = True, detail: str = "") -> None:
    """Append an action. `kind` is the tool/job name, `arg` a short summary."""
    entry = {"kind": str(kind), "arg": str(arg)[:80], "ok": bool(ok),
             "detail": str(detail)[:140], "ts": time.time()}
    try:
        with _lock:
            log = _load()
            log.append(entry)
            _save(log[-_MAX:])
    except Exception:
        pass


def record_tool(tool: str, params=None, ok: bool = True, detail: str = "") -> None:
    record(tool, _short_arg(tool, params), ok, detail)


def recent(n: int = 80) -> list:
    with _lock:
        return list(reversed(_load()))[:max(1, n)]


def clear() -> None:
    with _lock:
        _save([])
