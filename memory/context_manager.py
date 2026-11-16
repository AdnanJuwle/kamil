"""
memory/context_manager.py

Predictive context management with three tiers:

RAM   — raw messages always injected into context window
Cache — scored messages, promoted to RAM on cache hit
ROM   — ChromaDB cold storage, retrieved on explicit query

The predictor scores all cached messages against the current query
before every response. High-scoring messages get promoted to RAM
for that response — a cache hit. Decision markers never fully evict.

This replaces the simple "compress every 8 messages and drop" approach
with a system that predicts what context will be needed and keeps it
accessible without blowing the context window.
"""

import os
import json
import math
import time
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime

from core.paths import data_path
CACHE_PATH = data_path("context_cache.json")

# Token budget estimates (rough: 1 token ≈ 4 chars)
# ~10k tokens of raw recent conversation kept live (we run a 32k context window),
# so Kamil remembers the whole current conversation, not just a few turns.
MAX_RAM_CHARS = int(os.getenv("KAMIL_RAM_CHARS", "44000"))
MAX_CACHE_SIZE = 120        # max messages in cache
CACHE_HIT_THRESHOLD = 0.55  # score above this → promote to RAM
EVICT_THRESHOLD = 0.35      # score below this → move to ROM
DECISION_FLOOR = 0.60       # decision markers never score below this in cache


@dataclass
class CachedMessage:
    """A message in the cache tier."""
    role: str
    content: str
    timestamp: float          # unix timestamp when added
    score: float              # last computed relevance score
    is_decision_marker: bool  # never fully evict
    embedding: Optional[list] = None  # cached embedding vector
    compressed: str = ""     # compressed version for ROM handoff
    hit_count: int = 0        # how many times this has been a cache hit


def _load_cache() -> list[CachedMessage]:
    if not os.path.exists(CACHE_PATH):
        return []
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [CachedMessage(**item) for item in data]
    except Exception:
        return []


def _save_cache(cache: list[CachedMessage]):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump([asdict(c) for c in cache], f, indent=2)


class ContextPredictor:
    """
    Scores messages for relevance to current query.
    Uses four signals: semantic similarity, recency, thread coherence,
    and decision marker status.
    """

    def __init__(self):
        self._embedder = None

    def _get_embedder(self):
        if self._embedder is None:
            from context.embedder import Embedder
            self._embedder = Embedder()
        return self._embedder

    def _embed(self, text: str) -> list[float]:
        embedder = self._get_embedder()
        return embedder.model.encode([text[:500]])[0].tolist()

    def _cosine(self, a: list[float], b: list[float]) -> float:
        import numpy as np
        a, b = np.array(a), np.array(b)
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        if na == 0 or nb == 0:
            return 0.0
        return float(np.dot(a, b) / (na * nb))

    def _recency_score(self, timestamp: float, half_life_minutes: float = 20.0) -> float:
        """Exponential decay — half-life of 20 minutes by default."""
        age_minutes = (time.time() - timestamp) / 60.0
        return math.exp(-0.693 * age_minutes / half_life_minutes)

    def _thread_coherence(self, message_content: str, current_type: str) -> float:
        """Messages matching current thread type get a boost."""
        from memory.thread_state import detect_thread_type
        msg_type = detect_thread_type(message_content)
        if msg_type == current_type:
            return 1.0
        elif msg_type == "general" or current_type == "general":
            return 0.6
        else:
            return 0.2

    def score(
        self,
        message: CachedMessage,
        query: str,
        query_embedding: Optional[list],
        current_thread_type: str,
    ) -> float:
        """Score a single cached message against current query."""

        # Get or compute embedding for message
        if message.embedding is None:
            message.embedding = self._embed(message.content)

        if query_embedding is None:
            query_embedding = self._embed(query)

        # Signal 1 — semantic similarity (0-1)
        semantic = max(0.0, self._cosine(message.embedding, query_embedding))

        # Signal 2 — recency decay (0-1, half-life 20min)
        recency = self._recency_score(message.timestamp)

        # Signal 3 — thread coherence (0.2/0.6/1.0)
        coherence = self._thread_coherence(message.content, current_thread_type)

        # Combined score
        raw_score = (
            semantic * 0.40 +
            recency  * 0.30 +
            coherence * 0.30
        )

        # Signal 4 — decision marker floor
        if message.is_decision_marker:
            return max(raw_score, DECISION_FLOOR)

        return raw_score

    def score_all(
        self,
        cache: list[CachedMessage],
        query: str,
        current_thread_type: str,
    ) -> dict[int, float]:
        """Score all cached messages. Returns {index: score}."""
        if not cache:
            return {}

        query_embedding = self._embed(query)
        scores = {}
        for i, msg in enumerate(cache):
            scores[i] = self.score(msg, query, query_embedding, current_thread_type)
        return scores


class ContextManager:
    """
    Manages three-tier context: RAM / Cache / ROM

    Usage in orchestrator:
        ctx = ContextManager()
        context_messages = ctx.build_context(user_message)
        # use context_messages as the conversation history
        ctx.add_message({"role": "user", "content": user_message})
        ctx.add_message({"role": "assistant", "content": response})
    """

    def __init__(self):
        self.ram: list[dict] = []          # raw messages, always in context
        self.cache: list[CachedMessage] = _load_cache()
        self.predictor = ContextPredictor()
        self._ram_restored = False

    def _ram_chars(self) -> int:
        return sum(len(m.get("content", "")) for m in self.ram)

    def _is_decision_marker(self, content: str) -> bool:
        from memory.thread_state import is_decision_marker
        return is_decision_marker(content)

    def add_message(self, message: dict):
        """Add a new message to RAM."""
        self.ram.append(message)

        # Update thread state
        from memory.thread_state import update_thread
        update_thread(
            message.get("content", ""),
            role=message.get("role", "user")
        )

        # Evict from RAM if over budget
        while self._ram_chars() > MAX_RAM_CHARS and len(self.ram) > 2:
            self._evict_oldest_to_cache()

    def _evict_oldest_to_cache(self):
        """Move the oldest non-critical RAM message to cache."""
        # Find oldest non-critical message (skip last 2 for recency)
        evict_idx = 0
        for i in range(len(self.ram) - 2):
            if not self._is_decision_marker(self.ram[i].get("content", "")):
                evict_idx = i
                break

        msg = self.ram.pop(evict_idx)
        content = msg.get("content", "")

        cached = CachedMessage(
            role=msg.get("role", "user"),
            content=content,
            timestamp=time.time(),
            score=0.5,
            is_decision_marker=self._is_decision_marker(content),
        )
        self.cache.append(cached)

        # Trim cache if too large — evict lowest scoring non-decision messages
        if len(self.cache) > MAX_CACHE_SIZE:
            self._evict_cache_to_rom()

        _save_cache(self.cache)

    def _evict_cache_to_rom(self):
        """Move lowest-scoring cache entries to ROM (ChromaDB)."""
        # Sort by score, keep decision markers
        non_decision = [
            (i, c) for i, c in enumerate(self.cache)
            if not c.is_decision_marker
        ]
        if not non_decision:
            return

        # Evict lowest scoring
        non_decision.sort(key=lambda x: x[1].score)
        evict_count = len(self.cache) - MAX_CACHE_SIZE + 5  # evict 5 at a time

        evict_indices = set(i for i, _ in non_decision[:evict_count])
        to_evict = [self.cache[i] for i in evict_indices]

        # Send to ROM
        self._send_to_rom(to_evict)

        # Remove from cache
        self.cache = [c for i, c in enumerate(self.cache) if i not in evict_indices]
        _save_cache(self.cache)

    def _send_to_rom(self, messages: list[CachedMessage]):
        """Store messages in ChromaDB long-term memory."""
        try:
            from memory.long_term import LongTermMemory
            ltm = LongTermMemory()
            for msg in messages:
                if len(msg.content) > 50:
                    ltm.store(
                        summary=f"[{msg.role}] {msg.content[:400]}",
                        topics=[],
                        project=""
                    )
        except Exception as e:
            print(f"[context_manager] ROM storage failed: {e}")

    def build_context(self, query: str) -> list[dict]:
        """
        Build the context list for the current query.
        Includes RAM + promoted cache hits.
        """
        from memory.thread_state import get_current_thread_type

        current_type = get_current_thread_type()

        # Score all cached messages
        if self.cache:
            scores = self.predictor.score_all(self.cache, query, current_type)

            # Update scores in cache
            for i, score in scores.items():
                self.cache[i].score = score

            # Promote cache hits to context for this response
            promoted = []
            for i, score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
                if score >= CACHE_HIT_THRESHOLD:
                    msg = self.cache[i]
                    msg.hit_count += 1
                    promoted.append({
                        "role": msg.role,
                        "content": f"[retrieved context] {msg.content}"
                    })
                    if len(promoted) >= 4:  # max 4 cache hits per response
                        break

            _save_cache(self.cache)

            # Combine: promoted cache hits + RAM (recent messages last)
            if promoted:
                return promoted + self.ram
            
        return self.ram

    def get_summary_stats(self) -> dict:
        """Debug info about context tiers."""
        return {
            "ram_messages": len(self.ram),
            "ram_chars": self._ram_chars(),
            "cache_messages": len(self.cache),
            "cache_decision_markers": sum(1 for c in self.cache if c.is_decision_marker),
        }

    def clear(self):
        """Reset all tiers — call on conversation reset."""
        self.ram = []
        self.cache = []
        _save_cache([])

        from memory.thread_state import close_thread
        close_thread()


# ── Singleton ──────────────────────────────────────────────────────

_context_manager: Optional[ContextManager] = None


def get_context_manager() -> ContextManager:
    """Get or create the singleton context manager."""
    global _context_manager
    if _context_manager is None:
        _context_manager = ContextManager()
    return _context_manager