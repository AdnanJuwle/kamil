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

import glob
import re

LLAMA_SERVER = os.getenv("KAMIL_LLAMA_SERVER", r"C:\llama.cpp\build\bin\llama-server.exe")
HEAVY_PORT = int(os.getenv("KAMIL_HEAVY_PORT", "8086"))
HEAVY_URL = f"http://127.0.0.1:{HEAVY_PORT}"
MODELS_DIR = os.getenv("KAMIL_MODELS_DIR", r"C:\kamil\models")
_HEAVY_FALLBACK = r"C:\kamil\models\qwen2.5-coder-14b-q4.gguf"
CHAT_VRAM_GB = float(os.getenv("KAMIL_CHAT_VRAM_GB", "6"))     # reserve for the chat tier
IDLE_UNLOAD_SECONDS = int(os.getenv("KAMIL_HEAVY_IDLE", "600"))

_proc: subprocess.Popen | None = None
_lock = threading.RLock()
_idle_timer: threading.Timer | None = None


def _coder_score(path: str) -> int:
    n = os.path.basename(path).lower()
    s = 0
    if "coder" in n: s += 20
    if "qwen3" in n: s += 8
    if "a3b" in n: s += 6
    if "30b" in n or "32b" in n: s += 5
    if "14b" in n: s += 1
    return s


def heavy_model() -> str:
    """Resolve the coding model path, re-checked live so a newly-downloaded model is
    picked up without a config edit. Explicit KAMIL_HEAVY_MODEL wins if the file
    exists; otherwise pick the best coder GGUF in the models dir (prefers
    Qwen3-Coder / A3B / larger). Split GGUFs resolve to their first shard."""
    env = os.getenv("KAMIL_HEAVY_MODEL")
    if env and os.path.isfile(env):
        return env
    try:
        ggufs = glob.glob(os.path.join(MODELS_DIR, "*.gguf"))
        ggufs = [g for g in ggufs
                 if "-of-" not in os.path.basename(g).lower() or "00001-of-" in os.path.basename(g).lower()]
        coders = [g for g in ggufs if "coder" in os.path.basename(g).lower()]
        if coders:
            return max(coders, key=_coder_score)
    except Exception:
        pass
    return env or _HEAVY_FALLBACK


def _total_weights_gb(path: str) -> float:
    """Total on-disk size in GB, summing all shards of a split GGUF."""
    try:
        base = os.path.basename(path)
        m = re.match(r'(.*)-\d+-of-\d+\.gguf$', base, re.I)
        if m:
            shards = glob.glob(os.path.join(os.path.dirname(path), m.group(1) + "-*-of-*.gguf"))
            if shards:
                return sum(os.path.getsize(s) for s in shards) / 1073741824
        return os.path.getsize(path) / 1073741824
    except OSError:
        return 0.0


# Snapshot for labels/logs; functions below re-resolve live via heavy_model().
HEAVY_MODEL = heavy_model()


def available() -> bool:
    return os.path.isfile(heavy_model())


def label() -> str:
    """Short label for the resolved coder, e.g. 'coder-30b-a3b' / 'coder-14b'."""
    env = os.getenv("KAMIL_HEAVY_LABEL")
    if env:
        return env
    n = os.path.splitext(os.path.basename(heavy_model()))[0].lower()
    n = re.sub(r'-\d+-of-\d+$', '', n)
    m = re.search(r'(\d+x?\d*b(?:-a\d+b)?)', n)
    return ("coder-" + m.group(1)) if m else "coder"


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


def _port_up(port: int) -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200
    except Exception:
        return False


def _other_tiers_vram_gb() -> float:
    """VRAM already held by OTHER on-demand tiers (Chatterbox voice ~3GB, vision ~8GB).
    Without subtracting these, the heavy coder over-commits to the GPU and spills into
    shared/CPU memory — which is catastrophically slow (looks like a freeze). Chatterbox
    is now the default voice and stays resident, so this is the common case."""
    used = 0.0
    if _port_up(int(os.getenv("KAMIL_CHATTERBOX_PORT", "8095"))):
        used += float(os.getenv("KAMIL_CHATTERBOX_VRAM_GB", "3"))
    if _port_up(int(os.getenv("KAMIL_VISION_PORT", "8098"))):
        used += float(os.getenv("KAMIL_VISION_VRAM_GB", "8"))
    return used


def plan() -> tuple[dict, dict]:
    """(meta, fit-profile) for the heavy model, fit into VRAM minus the chat tier AND
    any other resident tiers (Chatterbox/vision). Match the chat model's context (32k)
    so the agent's big prompt doesn't overflow and 400. q4_0 KV keeps that 32k cache
    small enough to fit on the GPU alongside the other tiers."""
    ctx = int(os.getenv("KAMIL_HEAVY_CTX", "32768"))
    path = heavy_model()
    meta = hwfit.model_meta(path)
    meta["weights_gb"] = _total_weights_gb(path)   # sum shards for split GGUFs
    avail = hwfit.detect_vram_gb() - CHAT_VRAM_GB - _other_tiers_vram_gb()
    return meta, hwfit.fit_profile(meta, vram_gb=max(avail, 4.0), ctx=ctx, kv_type="q4_0")


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
            print(f"[heavy] model not found: {heavy_model()}")
            return False
        meta, prof = plan()
        args = hwfit.serve_args(heavy_model(), prof, HEAVY_PORT)
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
