import json
import os
from datetime import datetime
from core.config import DATA_DIR

THREADS_PATH = os.path.join(DATA_DIR, "threads.json")

def load_threads() -> list:
    try:
        with open(THREADS_PATH) as f:
            return json.load(f)
    except:
        return []

def save_threads(threads: list):
    os.makedirs(os.path.dirname(THREADS_PATH), exist_ok=True)
    with open(THREADS_PATH, 'w') as f:
        json.dump(threads, f, indent=2)

def add_thread(title: str, summary: str, tags: list = []):
    threads = load_threads()
    threads.append({
        "id": len(threads) + 1,
        "title": title,
        "summary": summary,
        "tags": tags,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat()
    })
    save_threads(threads)

def update_thread(thread_id: int, summary: str):
    threads = load_threads()
    for t in threads:
        if t["id"] == thread_id:
            t["summary"] = summary
            t["updated_at"] = datetime.now().isoformat()
    save_threads(threads)

def get_threads_summary() -> str:
    threads = load_threads()
    if not threads:
        return ""
    lines = [f"- [{t['title']}]: {t['summary']}" for t in threads[-10:]]
    return "\n".join(lines)