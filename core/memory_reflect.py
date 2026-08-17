"""
core/memory_reflect.py — Kamil curates its own memory.md, on its own.

After conversation turns, a cheap background pass (on the fast chat model) reads
the recent exchange and extracts NEW, durable facts about Adnan/his world that
aren't already known, and appends them to memory.md. Non-blocking, throttled,
deduped, and length-capped so it can't spam or pollute the notes.

Reference: how good agent stacks (e.g. Odysseus) keep a curated long-term memory
distinct from raw transcript — here we keep memory.md lean and self-authored.
"""
from __future__ import annotations

import os
import re
import threading

from core import self_memory

REFLECT_EVERY = int(os.getenv("KAMIL_REFLECT_EVERY", "6"))   # reflect every N assistant turns
MAX_ADDS_PER_REFLECTION = int(os.getenv("KAMIL_REFLECT_MAX_ADDS", "1"))

_lock = threading.Lock()
_turns_since = 0

_REFLECT_SYS = (
    "You are the gatekeeper for Kamil's tiny, precious long-term memory about Adnan. It must stay "
    "SHORT — only a handful of the most important, lasting facts ever belong here.\n"
    "Read the recent conversation. In the VAST majority of cases the correct answer is NONE — most "
    "exchanges contain nothing worth permanently remembering.\n"
    "Save a fact ONLY if it is ALL of these: (1) about Adnan himself (not about Kamil, not the task, "
    "not the topic being discussed), (2) genuinely NEW — not already in the current memory or implied "
    "by it, (3) DURABLE and significant — a stable preference, a real relationship, a major decision, "
    "a long-term project he's committed to, or something he EXPLICITLY told you to remember.\n"
    "NEVER save: things you (Kamil) said or think, opinions, task steps, code, one-off requests, "
    "questions, current-events answers, or restatements of the conversation. When in any doubt: NONE.\n"
    "Output at most ONE new fact as a single line starting with '- ', third person ('Adnan ...'). "
    "If nothing clears the bar (usually), output exactly: NONE"
)

# Reject junk even if the model bullets it: must be a substantive third-person fact.
_BAD_STARTS = ("i ", "i'm", "you ", "you're", "we ", "let ", "here", "sure", "the file", "to ", "it ", "this ", "that ")

_STOP = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "is", "are", "was",
         "his", "her", "he", "she", "it", "with", "that", "this", "adnan", "kamil", "wants",
         "likes", "has", "have", "about", "from", "you", "your"}


def _significant_words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _STOP}


def _too_similar(fact: str, existing: str) -> bool:
    """True if `fact` mostly overlaps content already in memory (avoid duplicates)."""
    fw = _significant_words(fact)
    if not fw:
        return True
    ew = _significant_words(existing)
    overlap = len(fw & ew) / len(fw)
    return overlap >= 0.6


def _parse_facts(resp: str) -> list[str]:
    if not resp or resp.strip().upper().startswith("NONE"):
        return []
    out = []
    for line in resp.splitlines():
        s = line.strip()
        if s.startswith(("- ", "* ", "• ")):
            out.append(s[2:].strip())
    return out


def _is_worth_saving(fact: str) -> bool:
    """A substantive, third-person fact about Adnan — not Kamil's musings, tasks,
    questions, or filler."""
    f = fact.strip()
    low = f.lower()
    if not (12 <= len(f) <= 200):
        return False
    if f.endswith("?"):
        return False
    if low.startswith(_BAD_STARTS):
        return False
    if "adnan" not in low:            # keep it to real facts about Adnan
        return False
    return True


def maybe_reflect(history: list[dict], model_fn) -> None:
    """Call after a conversation turn. Every REFLECT_EVERY turns, kick off a
    background reflection that may add new notes to memory.md. Never blocks."""
    global _turns_since
    _turns_since += 1
    if _turns_since < REFLECT_EVERY:
        return
    _turns_since = 0
    threading.Thread(target=_reflect, args=(list(history), model_fn), daemon=True).start()


def _reflect(history: list[dict], model_fn) -> None:
    with _lock:
        try:
            if self_memory.is_over_cap():
                return  # memory is full — Kamil should consolidate before adding more
            recent = [m for m in history[-8:]
                      if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")]
            if len(recent) < 2:
                return
            convo = "\n".join(f"{m['role']}: {str(m['content'])[:500]}" for m in recent)
            existing = self_memory.load_memory()
            resp = model_fn([
                {"role": "system", "content": _REFLECT_SYS},
                {"role": "user", "content": f"CURRENT MEMORY:\n{existing}\n\nRECENT CONVERSATION:\n{convo}\n\nNew durable facts to add (or NONE):"},
            ]) or ""
            existing_low = existing.lower()
            added = 0
            for fact in _parse_facts(resp):
                fact = " ".join(fact.split())
                if not _is_worth_saving(fact):
                    continue
                if _too_similar(fact, existing_low):
                    continue
                self_memory.append_note(fact)
                existing_low += "\n" + fact.lower()
                added += 1
                if added >= MAX_ADDS_PER_REFLECTION:
                    break
            if added:
                print(f"[memory] reflected — added {added} note(s) to memory.md")
        except Exception as e:
            print(f"[memory] reflect error: {e}")
