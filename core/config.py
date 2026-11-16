import os

LLAMA_TIMEOUT = int(os.getenv("KAMIL_TIMEOUT", "200"))
LLAMA_SERVER_URL    = os.getenv("KAMIL_SERVER_URL",    "http://127.0.0.1:8080")
# ONE model now. Huihui-Qwen3.8-27B-abliterated is strong enough to answer spoken
# questions directly, so the small :8084 voice model and its "let me check that for
# you" handoff are gone -- the voice URL defaults to the primary's. Set
# KAMIL_VOICE_URL explicitly to bring a separate fast voice model back.
VOICE_SERVER_URL    = os.getenv("KAMIL_VOICE_URL",     LLAMA_SERVER_URL)
HEAVY_SERVER_URL    = os.getenv("KAMIL_HEAVY_URL",     "http://127.0.0.1:8086")

# True when voice and reasoning are the same server, i.e. the normal single-model
# setup. Code that used to orchestrate a two-stage handoff checks this and answers
# in one pass instead of calling the same weights twice.
SINGLE_MODEL = (VOICE_SERVER_URL == LLAMA_SERVER_URL)

DEFAULT_TEMPERATURE = float(os.getenv("KAMIL_TEMPERATURE", "0.7"))
# Chat replies should be snappy; 8192 let the old reasoning model ramble for ages.
# Raise via KAMIL_MAX_TOKENS if you need long code generations.
DEFAULT_MAX_TOKENS = int(os.getenv("KAMIL_MAX_TOKENS", "1024"))

# Paths and platform differences all resolve through core.paths -- the single
# place that knows where Kamil lives and what OS it is on.
from core.paths import (  # noqa: E402
    KAMIL_ROOT as _ROOT, DATA_DIR as _DATA, MODELS_DIR as _MODELS,
    PROMPTS_DIR as _PROMPTS, PROJECTS_ROOT as _PROJECTS,
)

KAMIL_ROOT = str(_ROOT)
MODELS_DIR = str(_MODELS)
DATA_DIR = str(_DATA)
PROMPTS_DIR = str(_PROMPTS)

# Where Kamil's editable projects live (the coding-agent write root, project
# auto-load, and the symbol indexer all resolve against this). core.paths picks
# the first conventional location that exists for this OS; KAMIL_PROJECTS_ROOT
# overrides it.
PROJECTS_ROOT = str(_PROJECTS)

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
