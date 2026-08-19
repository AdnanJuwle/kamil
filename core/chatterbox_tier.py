"""
core/chatterbox_tier.py — manage the Chatterbox TTS microservice (isolated venv).

Chatterbox's deps (torch 2.6 CUDA, transformers 5.2, diffusers, gradio) can't live
in Kamil's main venv, so it runs as a separate process from tts_env, and Kamil talks
to it over HTTP. This spawns/idle-unloads that process, same as the model tiers.
"""
import atexit
import os
import subprocess
import threading
import time

import httpx

KAMIL_ROOT = os.getenv("KAMIL_ROOT", r"C:\kamil")
TTS_ENV_PY = os.getenv("KAMIL_CHATTERBOX_PY", os.path.join(KAMIL_ROOT, "tts_env", "Scripts", "python.exe"))
SERVER_PY = os.path.join(KAMIL_ROOT, "chatterbox_server.py")
CB_PORT = int(os.getenv("KAMIL_CHATTERBOX_PORT", "8095"))
CB_URL = f"http://127.0.0.1:{CB_PORT}"
IDLE_UNLOAD_SECONDS = int(os.getenv("KAMIL_CHATTERBOX_IDLE", "900"))

_proc: subprocess.Popen | None = None
_lock = threading.RLock()
_idle_timer: threading.Timer | None = None


def available() -> bool:
    return os.path.isfile(TTS_ENV_PY) and os.path.isfile(SERVER_PY)


def _is_running() -> bool:
    try:
        return httpx.get(f"{CB_URL}/health", timeout=2).status_code == 200
    except Exception:
        return False


def state() -> str:
    try:
        return "warm" if httpx.get(f"{CB_URL}/health", timeout=2).status_code == 200 else "loading"
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


def ensure_loaded(timeout: int = 180) -> bool:
    """Spawn the Chatterbox server if not running. True once it answers /health."""
    global _proc
    with _lock:
        if _is_running():
            _arm_idle()
            return True
        if not available():
            print(f"[chatterbox] unavailable (need tts_env + {SERVER_PY})")
            return False
        env = dict(os.environ)
        print(f"[chatterbox] starting server on :{CB_PORT} (isolated tts_env)")
        _proc = subprocess.Popen([TTS_ENV_PY, SERVER_PY], cwd=KAMIL_ROOT, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    from core import activity
    tok = activity.begin("loading voice model", "chatterbox")
    try:
        for _ in range(timeout):
            if _is_running():
                print("[chatterbox] ready")
                _arm_idle()
                activity.end(tok, ok=True, detail="chatterbox ready")
                return True
            time.sleep(1)
        activity.end(tok, ok=False, detail="timed out")
        return False
    except BaseException:
        activity.end(tok, ok=False)
        raise


def synth_wav(text: str, exaggeration: float | None = None, cfg_weight: float | None = None) -> bytes | None:
    """POST one line to the server, return WAV bytes (or None on failure)."""
    body = {"text": text}
    if exaggeration is not None:
        body["exaggeration"] = exaggeration
    if cfg_weight is not None:
        body["cfg_weight"] = cfg_weight
    try:
        r = httpx.post(f"{CB_URL}/tts", json=body, timeout=60)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("audio"):
            return r.content
    except Exception as e:
        print(f"[chatterbox] synth error: {e}")
    return None


def unload() -> None:
    global _proc, _idle_timer
    with _lock:
        if _idle_timer is not None:
            _idle_timer.cancel(); _idle_timer = None
        if _proc is not None:
            print("[chatterbox] unloading — reclaiming VRAM")
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
