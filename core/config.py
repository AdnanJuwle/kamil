import os

LLAMA_TIMEOUT = int(os.getenv("KAMIL_TIMEOUT", "200"))
LLAMA_SERVER_URL    = os.getenv("KAMIL_SERVER_URL",    "http://127.0.0.1:8080")
VOICE_SERVER_URL    = os.getenv("KAMIL_VOICE_URL",     "http://127.0.0.1:8084")
HEAVY_SERVER_URL    = os.getenv("KAMIL_HEAVY_URL",     "http://127.0.0.1:8086")

DEFAULT_TEMPERATURE = float(os.getenv("KAMIL_TEMPERATURE", "0.7"))
# Chat replies should be snappy; 8192 let the old reasoning model ramble for ages.
# Raise via KAMIL_MAX_TOKENS if you need long code generations.
DEFAULT_MAX_TOKENS = int(os.getenv("KAMIL_MAX_TOKENS", "1024"))

KAMIL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(KAMIL_ROOT, "models")
DATA_DIR = os.path.join(KAMIL_ROOT, "data")
PROMPTS_DIR = os.path.join(KAMIL_ROOT, "prompts")

# Where Kamil's editable projects live (the coding-agent write root, project
# auto-load, and the symbol indexer all resolve against this). Override with
# KAMIL_PROJECTS_ROOT so nothing is nailed to one machine's home directory.
PROJECTS_ROOT = os.getenv("KAMIL_PROJECTS_ROOT", r"C:\Users\adnan\Documents\repos")

def load_system_prompt() -> str:
    prompt_path = os.path.join(PROMPTS_DIR, 'system.md')
    try:
        with open(prompt_path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except FileNotFoundError:
        # Fallback if file missing
        return "You are Kamil, a local AI assistant."

KAMIL_SYSTEM_PROMPT = load_system_prompt()

# Hot-reloading accessor: re-reads system.md only when the file changes (cheap
# mtime check), so editing the prompt takes effect on the next message — no
# backend restart needed for prompt tweaks.
_PROMPT_CACHE = {"mtime": 0.0, "text": KAMIL_SYSTEM_PROMPT}

def get_system_prompt() -> str:
    prompt_path = os.path.join(PROMPTS_DIR, 'system.md')
    try:
        mtime = os.path.getmtime(prompt_path)
        if mtime != _PROMPT_CACHE["mtime"]:
            with open(prompt_path, 'r', encoding='utf-8') as f:
                _PROMPT_CACHE["text"] = f.read().strip()
            _PROMPT_CACHE["mtime"] = mtime
        return _PROMPT_CACHE["text"]
    except FileNotFoundError:
        return _PROMPT_CACHE["text"] or "You are Kamil, a local AI assistant."
