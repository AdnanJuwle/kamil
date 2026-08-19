"""
core/bg_jobs.py — long-running shell commands that run in the background.

Some commands (pip install, npm install, builds, downloads) take minutes. Running
them inline blocks the agent and hits the shell timeout, so they'd fail even though
they were working. This module runs them as detached subprocesses: the agent starts
one, gets a job id back immediately, and moves on. The job announces itself to the
activity registry (so it shows as "running now" in the Work tab) and records itself
to the action log when it finishes.

Everything is local — a subprocess on this machine, nothing leaves it.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

from core.config import DATA_DIR

_lock = threading.Lock()
_jobs: dict[str, dict] = {}          # job_id -> record
_counter = 0
_MAX_OUTPUT = 20000                  # keep the tail of the output buffer, per job
_LOG_DIR = os.path.join(DATA_DIR, "job_logs")


def _label_for(command: str) -> str:
    """A short human label for the activity/Work tab (first two words)."""
    parts = command.strip().split()
    return " ".join(parts[:2])[:40] if parts else "shell"


def start(command: str, cwd: str = "C:\\kamil") -> dict:
    """Spawn `command` in the background. Returns {job_id, label} immediately."""
    global _counter
    from core import activity

    with _lock:
        _counter += 1
        job_id = f"job{_counter}"

    os.makedirs(_LOG_DIR, exist_ok=True)
    log_path = os.path.join(_LOG_DIR, f"{job_id}.log")
    label = _label_for(command)
    tok = activity.begin(label, "running…")

    rec = {
        "job_id": job_id, "command": command, "cwd": cwd, "label": label,
        "status": "running", "returncode": None, "started": time.time(),
        "ended": None, "log_path": log_path, "_tok": tok,
    }
    with _lock:
        _jobs[job_id] = rec

    def _run():
        rc = None
        try:
            with open(log_path, "w", encoding="utf-8", errors="replace") as logf:
                proc = subprocess.Popen(
                    command, shell=True, cwd=cwd,
                    stdout=logf, stderr=subprocess.STDOUT, text=True,
                )
                with _lock:
                    rec["pid"] = proc.pid
                rc = proc.wait()
        except Exception as e:
            try:
                with open(log_path, "a", encoding="utf-8", errors="replace") as logf:
                    logf.write(f"\n[bg_jobs] failed to run: {e}\n")
            except Exception:
                pass
            rc = -1
        ok = (rc == 0)
        with _lock:
            rec["status"] = "done" if ok else "failed"
            rec["returncode"] = rc
            rec["ended"] = time.time()
        activity.end(tok, ok=ok, detail=("exit 0" if ok else f"exit {rc}"))

    threading.Thread(target=_run, name=f"bgjob-{job_id}", daemon=True).start()
    return {"job_id": job_id, "label": label}


def _tail(log_path: str, limit: int = _MAX_OUTPUT) -> str:
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
        return data[-limit:] if len(data) > limit else data
    except Exception:
        return ""


def status(job_id: str) -> dict | None:
    """Current state of a job + the tail of its output. None if unknown id."""
    with _lock:
        rec = _jobs.get(job_id)
        if rec is None:
            return None
        out = {k: rec[k] for k in ("job_id", "command", "label", "status",
                                   "returncode", "started", "ended")}
        log_path = rec["log_path"]
    out["output"] = _tail(log_path)
    return out


def list_jobs() -> list[dict]:
    with _lock:
        recs = list(_jobs.values())
    return [{k: r[k] for k in ("job_id", "label", "command", "status",
                               "returncode", "started", "ended")} for r in recs]
