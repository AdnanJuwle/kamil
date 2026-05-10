"""
memory/thread_state.py

Tracks the active conversation thread — what's currently being discussed,
what decisions have been made, what must not be forgotten.

Separate from long-term memory (what happened in the past) and
separate from compression (lossy summaries).

Thread state is the live document of what's actively in progress.
It survives compression cycles and model restarts.
It is never compressed — only archived when explicitly closed.
"""

import os
import json
from datetime import datetime
from dataclasses import dataclass, asdict, field
from typing import Optional

THREAD_STATE_PATH = r"C:\kamil\data\active_thread.json"

# Thread types — determines context injection strategy
THREAD_TYPES = {
    "code":       "coding, debugging, architecture, file operations",
    "emotional":  "personal struggles, mental health, feelings, life situations",
    "planning":   "goals, schedules, roadmaps, decisions about future",
    "research":   "learning, reading, understanding concepts",
    "religious":  "Islamic questions, Quran, hadith, ethics",
    "general":    "casual conversation, general questions",
}

# Keywords that signal a thread type
THREAD_TYPE_KEYWORDS = {
    "code": [
        "code", "function", "class", "bug", "error", "file", "import",
        "implement", "debug", "fix", "ekf", "prana", "navigation",
        "sensor", ".py", "method", "variable", "module", "algorithm"
    ],
    "emotional": [
        "depressed", "sad", "anxious", "stressed", "overwhelmed",
        "haven't", "can't", "don't want", "weeks", "days", "stuck",
        "unmotivated", "tired", "struggling", "hard time", "falling apart",
        "not doing", "haven't moved", "haven't eaten", "haven't slept"
    ],
    "planning": [
        "plan", "goal", "roadmap", "schedule", "deadline", "priority",
        "next week", "next month", "should i", "thinking about",
        "career", "internship", "apply", "strategy"
    ],
    "religious": [
        "quran", "hadith", "allah", "prayer", "islam", "halal", "haram",
        "muslim", "salah", "dua", "sunnah", "prophet", "verse", "surah"
    ],
    "research": [
        "explain", "what is", "how does", "learn", "understand",
        "paper", "research", "concept", "theory", "study"
    ],
}


@dataclass
class ThreadState:
    """
    Live state of the current conversation thread.
    Updated every few messages, never compressed away.
    """
    thread_id: str = ""
    thread_type: str = "general"
    
    # What's actually being worked on right now
    root_issue: str = ""          # the real problem (not symptoms)
    current_focus: str = ""       # what we're actively doing
    
    # What must survive compression
    critical_context: list[str] = field(default_factory=list)
    decisions_made: list[str] = field(default_factory=list)
    do_not_fixate_on: list[str] = field(default_factory=list)
    
    # Tone and approach for this thread
    tone: str = ""                # supportive / technical / direct / etc
    
    # Metadata
    started_at: str = ""
    last_updated: str = ""
    message_count: int = 0
    is_active: bool = True
    
    # Whether this thread's context can be compressed
    # False = preserve raw context longer
    compression_safe: bool = True


def _make_thread_id() -> str:
    return f"thread_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def load_thread_state() -> ThreadState:
    """Load active thread from disk."""
    if not os.path.exists(THREAD_STATE_PATH):
        return _new_thread()
    try:
        with open(THREAD_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return ThreadState(**data)
    except Exception:
        return _new_thread()


def save_thread_state(state: ThreadState):
    """Save thread state to disk."""
    os.makedirs(os.path.dirname(THREAD_STATE_PATH), exist_ok=True)
    with open(THREAD_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(asdict(state), f, indent=2)


def _new_thread() -> ThreadState:
    now = datetime.now().isoformat()
    return ThreadState(
        thread_id=_make_thread_id(),
        started_at=now,
        last_updated=now,
    )


def detect_thread_type(message: str) -> str:
    """Detect thread type from message content."""
    msg = message.lower()
    
    scores = {t: 0 for t in THREAD_TYPE_KEYWORDS}
    for thread_type, keywords in THREAD_TYPE_KEYWORDS.items():
        for kw in keywords:
            if kw in msg:
                scores[thread_type] += 1
    
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "general"
    return best


def is_decision_marker(message: str) -> bool:
    """
    Returns True if this message contains a decision or critical context
    that should never be evicted from cache.
    """
    msg = message.lower()
    markers = [
        "we decided", "the decision", "going with", "final approach",
        "don't use", "never use", "always use", "the fix is",
        "root cause", "the problem is", "i haven't", "i can't",
        "for weeks", "for days", "the reason", "because of",
        "we agreed", "confirmed", "settled on", "the issue is",
        "turns out", "realized", "discovered", "found out",
    ]
    return any(m in msg for m in markers)


def extract_decisions(message: str) -> list[str]:
    """Extract explicit decisions from a message for thread state."""
    decisions = []
    msg_lower = message.lower()
    
    triggers = [
        "we decided", "going with", "the fix is", "settled on",
        "we agreed", "confirmed", "will use", "won't use",
    ]
    
    for trigger in triggers:
        if trigger in msg_lower:
            # Extract the sentence containing the trigger
            sentences = message.split(".")
            for s in sentences:
                if trigger in s.lower() and len(s.strip()) > 10:
                    decisions.append(s.strip())
    
    return decisions


def update_thread(
    message: str,
    role: str = "user",
    force_type: Optional[str] = None
) -> ThreadState:
    """
    Update thread state based on a new message.
    Call this after every user message.
    """
    state = load_thread_state()
    state.message_count += 1
    state.last_updated = datetime.now().isoformat()
    
    # Detect or update thread type
    if force_type:
        state.thread_type = force_type
    elif state.message_count <= 3 or state.thread_type == "general":
        # Re-detect on early messages or if still general
        detected = detect_thread_type(message)
        if detected != "general":
            state.thread_type = detected
    
    # Set compression safety based on thread type
    if state.thread_type == "emotional":
        state.compression_safe = False  # never compress emotional threads aggressively
        if not state.tone:
            state.tone = "supportive — address root causes not symptoms"
    elif state.thread_type == "code":
        state.compression_safe = False  # preserve code context longer
        if not state.tone:
            state.tone = "technical and direct"
    
    # Extract and store decisions
    if role == "assistant":
        new_decisions = extract_decisions(message)
        for d in new_decisions:
            if d not in state.decisions_made:
                state.decisions_made.append(d)
        # Keep last 10 decisions
        state.decisions_made = state.decisions_made[-10:]
    
    # Extract critical context markers
    if is_decision_marker(message) and role == "user":
        # Store first 200 chars as critical context
        snippet = message[:200].strip()
        if snippet not in state.critical_context:
            state.critical_context.append(snippet)
        state.critical_context = state.critical_context[-8:]
    
    save_thread_state(state)
    return state


def set_root_issue(issue: str, focus: str = "", tone: str = ""):
    """
    Manually set the root issue for the current thread.
    Called by the orchestrator when it identifies the core problem.
    """
    state = load_thread_state()
    state.root_issue = issue
    if focus:
        state.current_focus = focus
    if tone:
        state.tone = tone
    state.last_updated = datetime.now().isoformat()
    save_thread_state(state)


def add_do_not_fixate(topic: str):
    """Mark a topic as a symptom that should not become the focus."""
    state = load_thread_state()
    if topic not in state.do_not_fixate_on:
        state.do_not_fixate_on.append(topic)
    save_thread_state(state)


def close_thread():
    """Archive current thread and start fresh."""
    state = load_thread_state()
    
    # Archive to data/threads_archive/
    archive_dir = r"C:\kamil\data\threads_archive"
    os.makedirs(archive_dir, exist_ok=True)
    archive_path = os.path.join(archive_dir, f"{state.thread_id}.json")
    with open(archive_path, "w", encoding="utf-8") as f:
        json.dump(asdict(state), f, indent=2)
    
    # Start fresh
    new_state = _new_thread()
    save_thread_state(new_state)
    return new_state


def get_thread_context_block() -> str:
    """
    Returns a formatted string for injection into the system prompt.
    This is what the orchestrator injects every message.
    """
    state = load_thread_state()
    
    if state.message_count == 0:
        return ""
    
    lines = []
    
    if state.thread_type and state.thread_type != "general":
        lines.append(f"Active thread type: {state.thread_type}")
    
    if state.root_issue:
        lines.append(f"ROOT ISSUE: {state.root_issue}")
    
    if state.current_focus:
        lines.append(f"Current focus: {state.current_focus}")
    
    if state.tone:
        lines.append(f"Tone: {state.tone}")
    
    if state.decisions_made:
        lines.append("Decisions made this session:")
        for d in state.decisions_made[-5:]:
            lines.append(f"  - {d}")
    
    if state.critical_context:
        lines.append("Critical context (do not lose):")
        for c in state.critical_context[-4:]:
            lines.append(f"  - {c}")
    
    if state.do_not_fixate_on:
        lines.append(f"Do NOT fixate on: {', '.join(state.do_not_fixate_on)}")
        lines.append("These are symptoms — keep focus on the root issue.")
    
    if not lines:
        return ""
    
    return "\n".join(lines)


def get_current_thread_type() -> str:
    """Quick accessor for current thread type."""
    return load_thread_state().thread_type