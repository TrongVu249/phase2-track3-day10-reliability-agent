from __future__ import annotations

from collections import Counter
import hashlib
import math
import re
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """Simple in-memory cache skeleton."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response by semantic similarity."""
        if _is_uncacheable(query):
            return None, 0.0

        now = time.time()
        self._entries = [e for e in self._entries if (now - e.created_at) < self.ttl_seconds]

        best_entry = None
        best_score = 0.0

        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_entry = entry

        if best_entry is not None and best_score >= self.similarity_threshold:
            if _looks_like_false_hit(query, best_entry.key):
                self.false_hit_log.append({
                    "query": query,
                    "cached_key": best_entry.key,
                    "score": best_score,
                    "reason": "date_or_number_mismatch",
                    "ts": time.time()
                })
                return None, best_score
            return best_entry.value, best_score

        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in cache."""
        if _is_uncacheable(query):
            return
        entry = CacheEntry(
            key=query,
            value=value,
            created_at=time.time(),
            metadata=metadata if metadata is not None else {}
        )
        self._entries.append(entry)

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Compute semantic similarity between two strings."""
        if a == b:
            return 1.0
        if a.strip().lower() == b.strip().lower():
            return 1.0

        def get_tokens(s: str) -> list[str]:
            s_clean = s.lower()
            words = s_clean.split()
            tokens = []
            for w in words:
                w_clean = re.sub(r"[^\w]", "", w)
                if w_clean:
                    tokens.append(w_clean)
                    if len(w_clean) >= 3:
                        for i in range(len(w_clean) - 2):
                            tokens.append(w_clean[i:i+3])
            return tokens

        tokens_a = get_tokens(a)
        tokens_b = get_tokens(b)

        if not tokens_a or not tokens_b:
            return 0.0

        count_a = Counter(tokens_a)
        count_b = Counter(tokens_b)

        dot_product = sum(count_a[t] * count_b[t] for t in count_a if t in count_b)
        norm_a = math.sqrt(sum(c**2 for c in count_a.values()))
        norm_b = math.sqrt(sum(c**2 for c in count_b.values()))

        if norm_a * norm_b == 0.0:
            return 0.0

        return dot_product / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments.

    TODO(student): Implement the get() and set() methods using Redis commands
    so that cache state is shared across multiple gateway instances.

    Data model (suggested):
        Key    = "{prefix}{query_hash}"   (Redis String namespace)
        Value  = Redis Hash with fields:  "query", "response"
        TTL    = Redis EXPIRE (automatic cleanup — no manual eviction)

    For similarity lookup: SCAN all keys with self.prefix, HGET each entry's
    "query" field, compute similarity locally via ResponseCache.similarity().

    Provided helpers:
        _is_uncacheable(query)          — True if privacy-sensitive
        _looks_like_false_hit(q, key)   — True if 4-digit numbers differ
        self._query_hash(query)         — deterministic short hash for Redis key
        ResponseCache.similarity(a, b)  — reuse your improved similarity function
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis."""
        if _is_uncacheable(query):
            return None, 0.0

        exact_key = f"{self.prefix}{self._query_hash(query)}"
        res = self._redis.hget(exact_key, "response")
        if res is not None:
            return res, 1.0

        best_score = 0.0
        best_response = None
        best_cached_query = None

        for key in self._redis.scan_iter(f"{self.prefix}*"):
            cached_query = self._redis.hget(key, "query")
            if cached_query is None:
                continue
            score = ResponseCache.similarity(query, cached_query)
            if score > best_score:
                best_score = score
                best_response = self._redis.hget(key, "response")
                best_cached_query = cached_query

        if best_response is not None and best_score >= self.similarity_threshold:
            if best_cached_query is not None and _looks_like_false_hit(query, best_cached_query):
                self.false_hit_log.append({
                    "query": query,
                    "cached_key": best_cached_query,
                    "score": best_score,
                    "reason": "date_or_number_mismatch",
                    "ts": time.time()
                })
                return None, best_score
            return best_response, best_score

        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with TTL."""
        if _is_uncacheable(query):
            return
        key = f"{self.prefix}{self._query_hash(query)}"
        self._redis.hset(key, mapping={"query": query, "response": value})
        self._redis.expire(key, self.ttl_seconds)

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
