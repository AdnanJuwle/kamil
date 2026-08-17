"""
core/heavy_tier.py — the heavy model tier for coding / complex tasks.

Two-tier design: the fast chat model (8B, fully on GPU) stays loaded for
conversation; when a real task comes (agent/coding mode), a bigger model is spun
up on port 8086 with hwfit-computed offload flags, COEXISTING with the chat model
by offloading MoE experts / dense layers to RAM as needed. It idle-unloads to
reclaim VRAM.

100% local llama.cpp — nothing leaves the machine. The chat tier's VRAM is
reserved (KAMIL_CHAT_VRAM_GB) so the heavy model is fit into the remainder.
"""

import atexit
import os
import subprocess
import threading
import time

import httpx

from core import hwfit

LLAMA_SERVER = os.getenv("KAMIL_LLAMA_SERVER", r"C:\llama.cpp\build\bin\llama-server.exe")
HEAVY_PORT = int(os.getenv("KAMIL_HEAVY_PORT", "8086"))
HEAVY_URL = f"http://127.0.0.1:{HEAVY_PORT}"
HEAVY_MODEL = os.getenv("KAMIL_HEAVY_MODEL", r"C:\kamil\models\qwen2.5-coder-14b-q4.gguf")
CHAT_VRAM_GB = float(os.getenv("KAMIL_CHAT_VRAM_GB", "6"))     # reserve for the chat tier
IDLE_UNLOAD_SECONDS = int(os.getenv("KAMIL_HEAVY_IDLE", "600"))

_proc: subprocess.Popen | None = None
_lock = threading.RLock()
_idle_timer: threading.Timer | None = None


def available() -> bool:
    return os.path.isfile(HEAVY_MODEL)


def _is_running() -> bool:
    # llama-server returns 503 "loading model" on /health while weights load, and
    # 200 only when ready for inference — so require 200, not just any response.
    try:
        return httpx.get(f"{HEAVY_URL}/health", timeout=2).status_code == 200
    except Exception:
        return False


def state() -> str:
    """UI-facing tier state: 'warm' (ready), 'loading' (weights loading), 'off'."""
    try:
        code = httpx.get(f"{HEAVY_URL}/health", timeout=2).status_code
        return "warm" if code == 200 else "loading"
    except Exception:
        return "off"


def plan() -> tuple[dict, dict]:
    """(meta, fit-profile) for the heavy model, fit into VRAM minus the chat tier.
    Match the chat model's context (32k) so the agent's big prompt (system + project
    code + tool schema + history) doesn't overflow and 400. q4_0 KV keeps that 32k
    cache small enough to still fit on the GPU alongside the chat tier."""
    ctx = int(os.getenv("KAMIL_HEAVY_CTX", "32768"))
    meta = hwfit.model_meta(HEAVY_MODEL)
    avail = hwfit.detect_vram_gb() - CHAT_VRAM_GB
    return meta, hwfit.fit_profile(meta, vram_gb=avail, ctx=ctx, kv_type="q4_0")


def _arm_idle() -> None:
    global _idle_timer
    with _lock:
        if _idle_timer is not None:
            _idle_timer.cancel()
            _idle_timer = None
        if IDLE_UNLOAD_SECONDS > 0:
            _idle_timer = threading.Timer(IDLE_UNLOAD_SECONDS, unload)
            _idle_timer.daemon = True
            _idle_timer.start()


def mark_activity() -> None:
    """Keep the heavy tier warm a bit longer (call at the end of a heavy task)."""
    _arm_idle()


def _kill_stale() -> None:
    """Kill whatever is listening on HEAVY_PORT. A heavy-model subprocess from a
    previous run often survives an app restart on Windows; if we reuse it we'd be
    stuck with its OLD config (e.g. an 8k context that 400s on the agent prompt)."""
    try:
        import psutil
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == HEAVY_PORT and c.status == psutil.CONN_LISTEN and c.pid:
                try:
                    psutil.Process(c.pid).kill()
                    print(f"[heavy] killed stale process on :{HEAVY_PORT} (pid {c.pid})")
                except Exception:
                    pass
    except Exception:
        try:
            subprocess.run(["powershell", "-Command",
                            f"Get-NetTCPConnection -LocalPort {HEAVY_PORT} -State Listen "
                            f"-ErrorAction SilentlyContinue | ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force }}"],
                           capture_output=True, timeout=8)
        except Exception:
            pass


def ensure_loaded(timeout: int = 240) -> bool:
    """Spawn the heavy model if not already running. Returns True when healthy."""
    global _proc
    with _lock:
        # If we didn't spawn it this process but something's on the port, it's a
        # stale server from a previous run — kill it so we respawn with current config.
        if _proc is None and _is_running():
            _kill_stale()
            for _ in range(10):
                if not _is_running():
                    break
                time.sleep(0.5)
        if _proc is not None and _is_running():
            _arm_idle()
            return True
        if not available():
            print(f"[heavy] model not found: {HEAVY_MODEL}")
            return False
        meta, prof = plan()
        args = hwfit.serve_args(HEAVY_MODEL, prof, HEAVY_PORT)
        print(f"[heavy] loading {meta['name']} — {prof['note']} (est {prof['est_vram_gb']}GB)")
        _proc = subprocess.Popen([LLAMA_SERVER] + args,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    from core import activity
    tok = activity.begin("loading coding model", f"{meta['name']} · {prof['note']}")
    try:
        for _ in range(timeout):
            if _is_running():
                print("[heavy] ready")
                _arm_idle()
                activity.end(tok, ok=True, detail=f"{meta['name']} ready")
                return True
            time.sleep(1)
        print("[heavy] failed to become ready in time")
        activity.end(tok, ok=False, detail="timed out")
        return False
    except BaseException:
        activity.end(tok, ok=False)
        raise


def unload() -> None:
    global _proc, _idle_timer
    with _lock:
        if _idle_timer is not None:
            _idle_timer.cancel()
            _idle_timer = None
        if _proc is not None:
            print("[heavy] unloading — reclaiming VRAM")
            try:
                _proc.terminate()
                try:
                    _proc.wait(timeout=5)
                except Exception:
                    _proc.kill()
            except Exception:
                pass
            _proc = None


atexit.register(unload)
