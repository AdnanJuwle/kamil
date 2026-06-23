"""
core/self_memory.py — Kamil's own memory.md: notes Kamil writes about its world.

Distinct from the conversational long-term memory (memory/long_term.py), which the
SYSTEM auto-extracts from chats. THIS file is KAMIL-authored: Kamil decides what to
keep, in its own words, via the `remember` (append) and `update_memory` (consolidate)
tools. It is injected into every prompt, so it must stay lean and curated.

Adnan can hand-edit data/kamil_memory.md too — it's just markdown.
"""

import os

from core.config import DATA_DIR

MEMORY_PATH = os.path.join(DATA_DIR, "kamil_memory.md")
MAX_CHARS = 6000          # ~1500 tokens; injected every turn -> keep it lean
INJECT_HARD_CAP = 9000    # absolute ceiling on what we inject, just in case

_HEADER = (
    "# Kamil's Memory\n"
    "<!-- My own notes on Adnan, my environment, and ongoing things. I curate this. -->\n\n"
)


def load_memory() -> str:
    try:
        with open(MEMORY_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _ensure() -> None:
    if not os.path.exists(MEMORY_PATH):
        os.makedirs(os.path.dirname(MEMORY_PATH), exist_ok=True)
        with open(MEMORY_PATH, "w", encoding="utf-8") as f:
            f.write(_HEADER)


def append_note(note: str) -> dict:
    note = " ".join(note.split()).strip()
    if not note:
        return {"success": False, "error": "empty note"}
    _ensure()
    with open(MEMORY_PATH, "a", encoding="utf-8") as f:
        f.write(f"- {note}\n")
    return {"success": True, "note": note, "chars": len(load_memory())}


def set_memory(body: str) -> dict:
    """Replace the whole notes body (for consolidation/pruning). Keeps the header."""
    os.makedirs(os.path.dirname(MEMORY_PATH), exist_ok=True)
    body = body.strip()
    # don't let a runaway rewrite blow up the file
    if len(body) > INJECT_HARD_CAP:
        body = body[:INJECT_HARD_CAP]
    text = _HEADER + body + "\n"
    with open(MEMORY_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    return {"success": True, "chars": len(text)}


def forget(query: str) -> dict:
    """Surgically drop only the note line(s) matching `query`. Cannot wipe the file."""
    query = " ".join(query.split()).strip()
    if not query:
        return {"success": False, "error": "empty query"}
    text = load_memory()
    if not text:
        return {"success": True, "removed": 0, "query": query}
    lines = text.splitlines()
    kept, removed = [], 0
    for line in lines:
        # only remove bullet/content lines, never the header/comment
        if line.strip().startswith(("#", "<!--")) or not line.strip():
            kept.append(line)
            continue
        if query.lower() in line.lower():
            removed += 1
        else:
            kept.append(line)
    if removed:
        with open(MEMORY_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + "\n")
    return {"success": True, "removed": removed, "query": query}


def is_over_cap() -> bool:
    return len(load_memory()) > MAX_CHARS


def memory_for_prompt() -> str:
    """The block injected into the system prompt (truncated to a hard ceiling)."""
    text = load_memory().strip()
    if not text:
        return ""
    if len(text) > INJECT_HARD_CAP:
        text = text[:INJECT_HARD_CAP] + "\n…(truncated — consolidate with update_memory)"
    return text
