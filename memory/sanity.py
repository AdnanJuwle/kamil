"""
memory/sanity.py

Validates compressed memories before storage and tracks drift over time.

Drift monitoring tracks:
- Output distribution changes (response length, vocabulary shift)
- User correction frequency
- Memory quality scores over time

If drift exceeds threshold:
- Auto-disables learning (stops storing new memories)
- Flags for manual review
- Reverts to stable state on demand
"""

import os
import json
import re
import hashlib
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional

SANITY_LOG_PATH = r"C:\kamil\data\sanity_log.json"
DRIFT_STATE_PATH = r"C:\kamil\data\drift_state.json"

# Thresholds
MIN_COMPRESSION_LENGTH = 60       # chars — below this = model gave up
MAX_COMPRESSION_LENGTH = 2000     # chars — above this = model rambled
MIN_MEANINGFUL_WORD_OVERLAP = 3   # words from original that must appear in compression
DRIFT_CORRECTION_THRESHOLD = 0.3  # 30% of recent responses corrected = drift
DRIFT_WINDOW = 20                 # look at last 20 interactions for drift
QUALITY_SCORE_FLOOR = 0.4        # memories below this score are rejected


@dataclass
class MemoryValidationResult:
    valid: bool
    score: float          # 0.0 - 1.0
    reason: str
    checks_passed: list[str]
    checks_failed: list[str]


@dataclass
class DriftState:
    learning_enabled: bool
    correction_count: int
    total_interactions: int
    last_drift_check: str
    drift_score: float            # 0.0 = stable, 1.0 = high drift
    stable_snapshot_hash: str     # hash of last known-good memory state
    flagged: bool
    flag_reason: str


# ── Validation ────────────────────────────────────────────────────

REFUSAL_MARKERS = [
    "i cannot", "i am unable", "as an ai", "i don't have access",
    "i can't help", "not able to", "i apologize", "i'm sorry but",
    "no information", "cannot process", "error occurred"
]

HALLUCINATION_MARKERS = [
    "as we discussed earlier",      # claiming false prior context
    "as i mentioned before",
    "in our previous conversation",
    "you told me that",             # fabricating user statements
    "you mentioned that",
    "i remember you saying",
]

STOPWORDS = {
    "the", "a", "an", "is", "was", "were", "i", "you", "and", "or",
    "to", "of", "in", "it", "that", "this", "for", "on", "with",
    "he", "she", "they", "we", "be", "at", "by", "from", "as",
    "are", "have", "has", "had", "not", "but", "so", "if", "do"
}


def validate_compression(
    compressed: str,
    original_messages: list[dict]
) -> MemoryValidationResult:
    """
    Validate a compressed memory before storing it.
    Returns a MemoryValidationResult with score and detailed checks.
    """
    passed = []
    failed = []
    score = 1.0

    text = compressed.strip()

    # Check 1 — not empty
    if not text:
        return MemoryValidationResult(
            valid=False, score=0.0,
            reason="compression is empty",
            checks_passed=[], checks_failed=["not_empty"]
        )
    passed.append("not_empty")

    # Check 2 — minimum length
    if len(text) < MIN_COMPRESSION_LENGTH:
        failed.append("min_length")
        score -= 0.4
    else:
        passed.append("min_length")

    # Check 3 — maximum length (model rambled)
    if len(text) > MAX_COMPRESSION_LENGTH:
        failed.append("max_length")
        score -= 0.1
    else:
        passed.append("max_length")

    # Check 4 — not a refusal
    lower = text.lower()
    refusal_found = [m for m in REFUSAL_MARKERS if m in lower]
    if refusal_found:
        failed.append("no_refusal")
        score -= 0.5
    else:
        passed.append("no_refusal")

    # Check 5 — no hallucination markers
    halluc_found = [m for m in HALLUCINATION_MARKERS if m in lower]
    if halluc_found:
        failed.append("no_hallucination_markers")
        score -= 0.3
    else:
        passed.append("no_hallucination_markers")

    # Check 6 — meaningful overlap with source messages
    original_words = set()
    for msg in original_messages:
        content = msg.get("content", "")
        original_words.update(
            w.lower().strip(".,!?;:") for w in content.split()
            if len(w) > 3
        )
    original_words -= STOPWORDS

    compressed_words = set(
        w.lower().strip(".,!?;:") for w in text.split()
        if len(w) > 3
    )
    compressed_words -= STOPWORDS

    overlap = original_words & compressed_words
    if len(overlap) < MIN_MEANINGFUL_WORD_OVERLAP:
        failed.append("word_overlap")
        score -= 0.3
    else:
        passed.append("word_overlap")

    # Check 7 — not just repeating the prompt
    # If compression is more than 80% similar to just concatenating messages
    concat = " ".join(m.get("content", "")[:100] for m in original_messages)
    concat_words = set(w.lower() for w in concat.split() if len(w) > 3) - STOPWORDS
    if len(compressed_words) > 0:
        similarity = len(compressed_words & concat_words) / len(compressed_words)
        if similarity > 0.85:
            failed.append("not_verbatim_copy")
            score -= 0.2
        else:
            passed.append("not_verbatim_copy")

    # Clamp score
    score = max(0.0, min(1.0, score))
    valid = score >= QUALITY_SCORE_FLOOR and "no_refusal" not in failed

    reason = "ok"
    if not valid:
        if failed:
            reason = f"failed checks: {', '.join(failed)}"

    return MemoryValidationResult(
        valid=valid,
        score=round(score, 3),
        reason=reason,
        checks_passed=passed,
        checks_failed=failed
    )


# ── Drift monitoring ──────────────────────────────────────────────

def _load_drift_state() -> DriftState:
    """Load drift state from disk, create default if not exists."""
    if os.path.exists(DRIFT_STATE_PATH):
        try:
            with open(DRIFT_STATE_PATH, "r") as f:
                data = json.load(f)
            return DriftState(**data)
        except Exception:
            pass

    return DriftState(
        learning_enabled=True,
        correction_count=0,
        total_interactions=0,
        last_drift_check=datetime.now().isoformat(),
        drift_score=0.0,
        stable_snapshot_hash="",
        flagged=False,
        flag_reason=""
    )


def _save_drift_state(state: DriftState):
    os.makedirs(os.path.dirname(DRIFT_STATE_PATH), exist_ok=True)
    with open(DRIFT_STATE_PATH, "w") as f:
        json.dump(asdict(state), f, indent=2)


def _load_sanity_log() -> list[dict]:
    if os.path.exists(SANITY_LOG_PATH):
        try:
            with open(SANITY_LOG_PATH, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_sanity_log(log: list[dict]):
    os.makedirs(os.path.dirname(SANITY_LOG_PATH), exist_ok=True)
    # Keep last 500 entries
    log = log[-500:]
    with open(SANITY_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)


def record_interaction(
    user_message: str,
    response: str,
    was_corrected: bool = False,
    validation_result: Optional[MemoryValidationResult] = None
):
    """
    Record an interaction for drift monitoring.
    Call this after every response.
    """
    state = _load_drift_state()
    log = _load_sanity_log()

    entry = {
        "timestamp": datetime.now().isoformat(),
        "response_length": len(response),
        "was_corrected": was_corrected,
        "memory_quality": validation_result.score if validation_result else None,
        "memory_valid": validation_result.valid if validation_result else None,
    }
    log.append(entry)

    state.total_interactions += 1
    if was_corrected:
        state.correction_count += 1

    # Recalculate drift score every 5 interactions
    if state.total_interactions % 5 == 0:
        _recalculate_drift(state, log)

    _save_drift_state(state)
    _save_sanity_log(log)


def _recalculate_drift(state: DriftState, log: list[dict]):
    """Recalculate drift score from recent interactions."""
    recent = log[-DRIFT_WINDOW:]
    if len(recent) < 5:
        return

    # Metric 1 — correction rate
    corrections = sum(1 for e in recent if e.get("was_corrected"))
    correction_rate = corrections / len(recent)

    # Metric 2 — memory quality trend
    quality_scores = [
        e["memory_quality"] for e in recent
        if e.get("memory_quality") is not None
    ]
    avg_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 1.0
    quality_drift = max(0.0, 1.0 - avg_quality)

    # Metric 3 — response length variance
    lengths = [e["response_length"] for e in recent if e.get("response_length")]
    if len(lengths) > 3:
        mean_len = sum(lengths) / len(lengths)
        variance = sum((l - mean_len) ** 2 for l in lengths) / len(lengths)
        # Normalize — high variance = potential drift
        length_drift = min(1.0, variance / (mean_len ** 2 + 1))
    else:
        length_drift = 0.0

    # Combined drift score (weighted)
    state.drift_score = round(
        correction_rate * 0.5 +
        quality_drift * 0.35 +
        length_drift * 0.15,
        3
    )

    state.last_drift_check = datetime.now().isoformat()

    # Auto-disable learning if drift too high
    if state.drift_score >= DRIFT_CORRECTION_THRESHOLD:
        if state.learning_enabled:
            print(f"[sanity] DRIFT DETECTED: score={state.drift_score:.3f} — disabling memory learning")
            state.learning_enabled = False
            state.flagged = True
            state.flag_reason = (
                f"drift_score={state.drift_score:.3f}, "
                f"correction_rate={correction_rate:.2f}, "
                f"avg_memory_quality={avg_quality:.2f}"
            )
    elif state.drift_score < DRIFT_CORRECTION_THRESHOLD * 0.5:
        # Re-enable if drift has recovered
        if not state.learning_enabled:
            print(f"[sanity] Drift recovered: score={state.drift_score:.3f} — re-enabling learning")
            state.learning_enabled = True
            state.flagged = False
            state.flag_reason = ""


def is_learning_enabled() -> bool:
    """Check if memory learning is currently enabled."""
    state = _load_drift_state()
    return state.learning_enabled


def get_drift_report() -> dict:
    """Get current drift monitoring report."""
    state = _load_drift_state()
    log = _load_sanity_log()
    recent = log[-DRIFT_WINDOW:]

    return {
        "learning_enabled": state.learning_enabled,
        "drift_score": state.drift_score,
        "flagged": state.flagged,
        "flag_reason": state.flag_reason,
        "total_interactions": state.total_interactions,
        "correction_rate": (
            sum(1 for e in recent if e.get("was_corrected")) / len(recent)
            if recent else 0.0
        ),
        "recent_memory_quality": (
            sum(e["memory_quality"] for e in recent if e.get("memory_quality") is not None) /
            max(1, sum(1 for e in recent if e.get("memory_quality") is not None))
        ),
        "last_check": state.last_drift_check,
        "window_size": len(recent)
    }


def reset_drift_state():
    """Manually reset drift state — call when reverting to stable."""
    state = _load_drift_state()
    state.learning_enabled = True
    state.drift_score = 0.0
    state.flagged = False
    state.flag_reason = ""
    state.correction_count = 0
    state.last_drift_check = datetime.now().isoformat()
    _save_drift_state(state)
    print("[sanity] Drift state reset — learning re-enabled")


def record_correction():
    """
    Call this when user explicitly corrects Kamil.
    Triggers: user says 'that's wrong', 'no', 'incorrect', edits a response.
    """
    state = _load_drift_state()
    log = _load_sanity_log()

    log.append({
        "timestamp": datetime.now().isoformat(),
        "response_length": 0,
        "was_corrected": True,
        "memory_quality": None,
        "memory_valid": None,
    })

    state.correction_count += 1
    state.total_interactions += 1

    _recalculate_drift(state, log)
    _save_drift_state(state)
    _save_sanity_log(log)
    print(f"[sanity] Correction recorded. Drift score: {state.drift_score:.3f}")