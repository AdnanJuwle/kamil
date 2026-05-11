import os

LLAMA_TIMEOUT = int(os.getenv("KAMIL_TIMEOUT", "200"))
LLAMA_SERVER_URL    = os.getenv("KAMIL_SERVER_URL",    "http://127.0.0.1:8080")
EXECUTOR_SERVER_URL = os.getenv("KAMIL_EXECUTOR_URL",  "http://127.0.0.1:8083")
VOICE_SERVER_URL    = os.getenv("KAMIL_VOICE_URL",     "http://127.0.0.1:8084")
HEAVY_SERVER_URL    = os.getenv("KAMIL_HEAVY_URL",     "http://127.0.0.1:8086")

DEFAULT_TEMPERATURE = float(os.getenv("KAMIL_TEMPERATURE", "0.7"))
DEFAULT_MAX_TOKENS = int(os.getenv("KAMIL_MAX_TOKENS", "8192"))

KAMIL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(KAMIL_ROOT, "models")
DATA_DIR = os.path.join(KAMIL_ROOT, "data")
PROMPTS_DIR = os.path.join(KAMIL_ROOT, "prompts")

def load_system_prompt() -> str:
    prompt_path = os.path.join(PROMPTS_DIR, 'system.md')
    try:
        with open(prompt_path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except FileNotFoundError:
        # Fallback if file missing
        return "You are Kamil, a local AI assistant."

KAMIL_SYSTEM_PROMPT = load_system_prompt()
