"""
core/tts_tier.py — the Orpheus TTS model as an on-demand llama-server tier.

Orpheus is a Llama-3B fine-tune that emits SNAC *audio tokens* instead of words; a
tiny SNAC codec (core/orpheus_tts.py) turns those tokens into a 24kHz waveform. So
the "voice" is literally another llama-server on the GPU, loaded on demand and idle-
unloaded, exactly like the coder and vision tiers. 100% local.
"""
import atexit
import glob
import os
import re
import subprocess
import threading
import time

import httpx

from core.paths import free_port_cmd, llama_server_bin, MODELS_DIR as _MD
LLAMA_SERVER = llama_server_bin()
TTS_PORT = int(os.getenv("KAMIL_TTS_PORT", "8090"))
TTS_URL = f"http://127.0.0.1:{TTS_PORT}"
MODELS_DIR = str(_MD)
TTS_CTX = int(os.getenv("KAMIL_TTS_CTX", "4096"))
IDLE_UNLOAD_SECONDS = int(os.getenv("KAMIL_TTS_IDLE", "900"))

_proc: subprocess.Popen | None = None
_lock = threading.RLock()
_idle_timer: threading.Timer | None = None


def tts_model() -> str:
    """Resolve the Orpheus GGUF (KAMIL_TTS_MODEL wins, else the best orpheus in models/)."""
    env = os.getenv("KAMIL_TTS_MODEL")
    if env and os.path.isfile(env):
        return env
    try:
        ggufs = glob.glob(os.path.join(MODELS_DIR, "*.gguf"))
        orph = [g for g in ggufs if "orpheus" in os.path.basename(g).lower()]
        if orph:
            # Prefer the lex-au FT (Q8): clean, correctly-timed output. (The isaiahbjork
            # Q4 rambled — generated many seconds of audio for a two-word line.)
            return max(orph, key=lambda g: ("lex" in g.lower() or "ft-q8" in g.lower(),
                                            "q8" in g.lower(), os.path.getsize(g)))
    except Exception:
        pass
    return ""


def available() -> bool:
    return bool(tts_model()) and os.path.isfile(tts_model())


def label() -> str:
    return "orpheus-3b"


def _is_running() -> bool:
    try:
        return httpx.get(f"{TTS_URL}/health", timeout=2).status_code == 200
    except Exception:
        return False


def state() -> str:
    try:
        code = httpx.get(f"{TTS_URL}/health", timeout=2).status_code
        return "warm" if code == 200 else "loading"
    except Exception:
        return "off"


def _arm_idle() -> None:
    global _idle_timer
    with _lock:
        if _idle_timer is not None:
            _idle_timer.cancel(); _idle_timer = None
        if IDLE_UNLOAD_SECONDS > 0:
            _idle_timer = threading.Timer(IDLE_UNLOAD_SECONDS, unload)
            _idle_timer.daemon = True
            _idle_timer.start()


def mark_activity() -> None:
    _arm_idle()


def _kill_stale() -> None:
    try:
        import psutil
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == TTS_PORT and c.status == psutil.CONN_LISTEN and c.pid:
                try:
                    psutil.Process(c.pid).kill()
                    print(f"[tts] killed stale process on :{TTS_PORT} (pid {c.pid})")
                except Exception:
                    pass
    except Exception:
        try:
            subprocess.run(free_port_cmd(TTS_PORT),
                           capture_output=True, timeout=8)
        except Exception:
            pass


def ensure_loaded(timeout: int = 180) -> bool:
    """Spawn the Orpheus server if not already running. Returns True when healthy."""
    global _proc
    with _lock:
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
            print("[tts] Orpheus model not found in models/ (Orpheus-3b-FT-*.gguf)")
            return False
        model = tts_model()
        args = ["-m", model, "-c", str(TTS_CTX), "-ngl", "99",
                "--flash-attn", "on",
                "--host", "127.0.0.1", "--port", str(TTS_PORT), "--log-disable"]
        print(f"[tts] loading {os.path.basename(model)} on :{TTS_PORT}")
        _proc = subprocess.Popen([LLAMA_SERVER] + args,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    from core import activity
    tok = activity.begin("loading voice model", os.path.basename(tts_model()))
    try:
        for _ in range(timeout):
            if _is_running():
                _warmup()   # prime CUDA graphs so the FIRST real reply isn't ~5x slow
                print("[tts] ready")
                _arm_idle()
                activity.end(tok, ok=True, detail="orpheus ready")
                return True
            time.sleep(1)
        activity.end(tok, ok=False, detail="timed out")
        return False
    except BaseException:
        activity.end(tok, ok=False)
        raise


def _warmup() -> None:
    """One tiny generation to trigger CUDA-graph/kernel capture (the first real
    inference is otherwise ~5x slow). Best-effort; ignore failures."""
    try:
        httpx.post(f"{TTS_URL}/completion",
                   json={"prompt": [128259, 128009, 128260, 128261, 128257],
                         "n_predict": 16, "temperature": 0.6, "stream": False},
                   timeout=30)
    except Exception:
        pass


def unload() -> None:
    global _proc, _idle_timer
    with _lock:
        if _idle_timer is not None:
            _idle_timer.cancel(); _idle_timer = None
        if _proc is not None:
            print("[tts] unloading — reclaiming VRAM")
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
