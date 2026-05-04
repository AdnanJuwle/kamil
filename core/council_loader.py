import subprocess
import time
import httpx
import os

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

_processes = {}

def _is_running(port: int) -> bool:
    try:
        httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
        return True
    except:
        return False

def load_council():
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
def unload_council():
    for role, proc in _processes.items():
        print(f"Unloading council {role}...")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except:
            proc.kill()
    _processes.clear()

def council_available() -> bool:
    return all(_is_running(c["port"]) for c in MODELS.values())