"""
core/vision_tier.py — Kamil's eyes: an on-demand vision-language model tier.

Same two-tier idea as heavy_tier, but for images: a multimodal GGUF (Qwen3-VL) is
spun up on port 8098 with its mmproj (image encoder) projector, COEXISTING with the
chat model. It answers questions about an image, then idle-unloads to reclaim VRAM.

Needs TWO files in the models dir: the VLM weights (e.g. Qwen3-VL-8B-Instruct-Q8_0
.gguf) AND its projector (mmproj-*.gguf). Without the mmproj, available() is False —
the model can talk but can't see.

100% local llama.cpp (build must support qwen3vl / mtmd). Nothing leaves the machine.
"""

import atexit
import glob
import os
import re
import subprocess
import threading
import time

import httpx

LLAMA_SERVER = os.getenv("KAMIL_LLAMA_SERVER", r"C:\llama.cpp\build\bin\llama-server.exe")
VISION_PORT = int(os.getenv("KAMIL_VISION_PORT", "8098"))
VISION_URL = f"http://127.0.0.1:{VISION_PORT}"
MODELS_DIR = os.getenv("KAMIL_MODELS_DIR", r"C:\kamil\models")
VISION_CTX = int(os.getenv("KAMIL_VISION_CTX", "8192"))
IDLE_UNLOAD_SECONDS = int(os.getenv("KAMIL_VISION_IDLE", "600"))

_proc: subprocess.Popen | None = None
_lock = threading.RLock()
_idle_timer: threading.Timer | None = None


def _is_mmproj(name: str) -> bool:
    n = name.lower()
    return "mmproj" in n or "m.proj" in n or n.startswith("proj") or "-proj" in n


def _vlm_score(path: str) -> int:
    n = os.path.basename(path).lower()
    s = 0
    if "vl" in n or "vision" in n: s += 20
    if "qwen3" in n: s += 8
    if "instruct" in n: s += 2
    if "8b" in n: s += 2
    return s


def vision_model() -> str:
    """Resolve the VLM weights path (re-checked live so a fresh download is picked up).
    KAMIL_VISION_MODEL wins if it exists; else the best vision GGUF in the models dir."""
    env = os.getenv("KAMIL_VISION_MODEL")
    if env and os.path.isfile(env):
        return env
    try:
        ggufs = [g for g in glob.glob(os.path.join(MODELS_DIR, "*.gguf"))
                 if not _is_mmproj(os.path.basename(g))]
        vlms = [g for g in ggufs if "vl" in os.path.basename(g).lower()
                or "vision" in os.path.basename(g).lower()]
        if vlms:
            return max(vlms, key=_vlm_score)
    except Exception:
        pass
    return env or ""


def mmproj_path() -> str | None:
    """Resolve the multimodal projector (image encoder) GGUF. None if not present."""
    env = os.getenv("KAMIL_VISION_MMPROJ")
    if env and os.path.isfile(env):
        return env
    try:
        cands = [g for g in glob.glob(os.path.join(MODELS_DIR, "*.gguf"))
                 if _is_mmproj(os.path.basename(g))]
        # prefer one whose name matches the chosen model family (e.g. both 'qwen3')
        vm = os.path.basename(vision_model()).lower()
        fam = [c for c in cands if any(t in os.path.basename(c).lower()
                                       for t in ("qwen3", "vl") ) or vm[:6] in os.path.basename(c).lower()]
        pool = fam or cands
        if pool:
            return max(pool, key=os.path.getsize)  # f16 projector > quantized
    except Exception:
        pass
    return None


def available() -> bool:
    """Vision works only when BOTH the VLM weights and its mmproj are present."""
    return bool(vision_model()) and os.path.isfile(vision_model()) and mmproj_path() is not None


def missing_reason() -> str:
    """Human explanation of why vision isn't available (for the UI / logs)."""
    if not (vision_model() and os.path.isfile(vision_model())):
        return "no vision model GGUF in models/ (need e.g. Qwen3-VL-8B-Instruct-*.gguf)"
    if mmproj_path() is None:
        return "vision model present but its mmproj (image encoder) GGUF is missing — download mmproj-*.gguf into models/"
    return ""


def label() -> str:
    env = os.getenv("KAMIL_VISION_LABEL")
    if env:
        return env
    vm = vision_model()
    if not vm:
        return "vision"
    n = os.path.splitext(os.path.basename(vm))[0].lower()
    m = re.search(r'(qwen3-?vl)', n)
    b = re.search(r'(\d+b)', n)
    return (m.group(1).replace("qwen3vl", "qwen3-vl") if m else "vision") + (f"-{b.group(1)}" if b else "")


def _is_running() -> bool:
    try:
        return httpx.get(f"{VISION_URL}/health", timeout=2).status_code == 200
    except Exception:
        return False


def state() -> str:
    """UI-facing tier state: 'warm' (ready), 'loading', 'off'."""
    try:
        code = httpx.get(f"{VISION_URL}/health", timeout=2).status_code
        return "warm" if code == 200 else "loading"
    except Exception:
        return "off"


def _serve_args() -> list[str]:
    """llama-server args for the VLM + its projector. Dense 8B fits fully on GPU."""
    args = ["-m", vision_model(),
            "--mmproj", mmproj_path(),
            "-c", str(VISION_CTX),
            "-ngl", "99",
            "--mmproj-offload",              # image encoder on GPU too
            "--flash-attn", "on",
            "--host", "127.0.0.1", "--port", str(VISION_PORT), "--log-disable"]
    return args


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
    """Keep the vision tier warm a bit longer (call after a vision task)."""
    _arm_idle()


def _kill_stale() -> None:
    """Kill whatever is listening on VISION_PORT (a survivor from a previous run)."""
    try:
        import psutil
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == VISION_PORT and c.status == psutil.CONN_LISTEN and c.pid:
                try:
                    psutil.Process(c.pid).kill()
                    print(f"[vision] killed stale process on :{VISION_PORT} (pid {c.pid})")
                except Exception:
                    pass
    except Exception:
        try:
            subprocess.run(["powershell", "-Command",
                            f"Get-NetTCPConnection -LocalPort {VISION_PORT} -State Listen "
                            f"-ErrorAction SilentlyContinue | ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force }}"],
                           capture_output=True, timeout=8)
        except Exception:
            pass


def ensure_loaded(timeout: int = 240) -> bool:
    """Spawn the vision model if not already running. Returns True when healthy."""
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
            print(f"[vision] unavailable: {missing_reason()}")
            return False
        args = _serve_args()
        name = os.path.basename(vision_model())
        print(f"[vision] loading {name} + {os.path.basename(mmproj_path())} on :{VISION_PORT}")
        _proc = subprocess.Popen([LLAMA_SERVER] + args,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    from core import activity
    tok = activity.begin("loading vision model", name)
    try:
        for _ in range(timeout):
            if _is_running():
                print("[vision] ready")
                _arm_idle()
                activity.end(tok, ok=True, detail=f"{name} ready")
                return True
            time.sleep(1)
        print("[vision] failed to become ready in time")
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
            print("[vision] unloading — reclaiming VRAM")
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
