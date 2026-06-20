import subprocess
import time
import threading
import atexit
import os
import httpx

LLAMA_SERVER = r"C:\llama.cpp\build\bin\llama-server.exe"
MODELS = {
    "advocate": {
        "path": r"C:\kamil\models\dolphin-mistral-7b-q4.gguf",
        "port": 8081
    },
    "synthesizer": {
        "path": r"C:\kamil\models\llama-3.1-8b-instruct-q4.gguf",
        "port": 8082
    }
}

# Free the council's VRAM after this many seconds with no council activity.
# Consecutive councils stay warm; an idle council unloads itself.
IDLE_UNLOAD_SECONDS = int(os.getenv("KAMIL_COUNCIL_IDLE", "600"))

_processes = {}
_lock = threading.RLock()
_idle_timer: threading.Timer | None = None


def _is_running(port: int) -> bool:
    try:
        httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
        return True
    except Exception:
        return False


def _cancel_idle_timer():
    global _idle_timer
    if _idle_timer is not None:
        _idle_timer.cancel()
        _idle_timer = None


def _schedule_idle_unload():
    """(Re)arm the idle-unload timer. Called on every council activity."""
    global _idle_timer
    with _lock:
        _cancel_idle_timer()
        if IDLE_UNLOAD_SECONDS <= 0:
            return  # idle-unload disabled
        _idle_timer = threading.Timer(IDLE_UNLOAD_SECONDS, _idle_unload)
        _idle_timer.daemon = True
        _idle_timer.start()


def _idle_unload():
    print(f"[council] Idle for {IDLE_UNLOAD_SECONDS}s — freeing council VRAM")
    unload_council()


def mark_activity():
    """Call at the end of a council deliberation to keep it warm a bit longer."""
    _schedule_idle_unload()


def load_council():
    with _lock:
        for role, config in MODELS.items():
            if _is_running(config["port"]):
                continue
            print(f"Loading council {role}...")
            proc = subprocess.Popen([
                LLAMA_SERVER,
                "-m", config["path"],
                "-c", "4096",
                "-ngl", "99",
                "--host", "0.0.0.0",
                "--port", str(config["port"]),
                "--log-disable"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _processes[role] = proc

        # Wait up to 120 seconds for both to be ready
        for role, config in MODELS.items():
            print(f"Waiting for council {role}...")
            for _ in range(120):
                if _is_running(config["port"]):
                    print(f"Council {role} ready")
                    break
                time.sleep(1)
            else:
                print(f"Council {role} failed to start in time")

    # Arm the idle-unload timer so VRAM is reclaimed once the council goes quiet.
    _schedule_idle_unload()


def unload_council():
    with _lock:
        _cancel_idle_timer()
        for role, proc in list(_processes.items()):
            print(f"Unloading council {role}...")
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
            except Exception:
                pass
        _processes.clear()


def council_available() -> bool:
    return all(_is_running(c["port"]) for c in MODELS.values())


# Make sure council subprocesses don't outlive the backend.
atexit.register(unload_council)
