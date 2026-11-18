"""
Pluggable Caching Layer for QuizFlow.
- Uses Redis when REDIS_URL is configured and reachable (shared across horizontal instances).
- Automatically falls back to a bounded, thread-safe TTL LRU cache when Redis is unavailable or unconfigured.
- Includes circuit-breaker protection, cache invalidation helpers, and live hit/miss metrics.
- Never stores student-specific private data under shared keys.
"""
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

from app.core.config import settings

logger = logging.getLogger("quizflow.cache")

try:
    import redis as redis_lib
except ImportError:
    redis_lib = None


class BoundedTTLCache:
    """Thread-safe bounded LRU cache with per-entry TTL expiration."""

    def __init__(self, max_entries: int = 512):
        self._max_entries = max(16, max_entries)
        self._store: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: str) -> Optional[str]:
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, raw_value = entry
            if now >= expires_at:
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return raw_value

    def set(self, key: str, raw_value: str, ttl: int) -> None:
        expires_at = time.monotonic() + max(1, ttl)
        with self._lock:
            self._store[key] = (expires_at, raw_value)
            self._store.move_to_end(key)
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def delete_prefix(self, prefix: str) -> int:
        with self._lock:
            keys = [k for k in self._store if k.startswith(prefix)]
            for k in keys:
                del self._store[k]
            return len(keys)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._store)


class CacheManager:
    """
    Unified Redis + Bounded Local LRU Fallback Cache.
    Gracefully handles Redis outages via a short cooldown circuit breaker.
    """

    NAMESPACE = "qf:v2:"

    def __init__(self) -> None:
        self._local = BoundedTTLCache(max_entries=settings.CACHE_LOCAL_MAX_ENTRIES)
        self._redis = None
        self._redis_available = False
        self._circuit_open_until = 0.0
        self._lock = threading.RLock()

        # Metrics
        self._hits = 0
        self._misses = 0
        self._sets = 0
        self._invalidations = 0
        self._errors = 0

        self._init_redis()

    def _init_redis(self) -> None:
        if not settings.REDIS_URL or redis_lib is None:
            return
        try:
            pool = redis_lib.ConnectionPool.from_url(
                settings.REDIS_URL,
                max_connections=50,
                socket_connect_timeout=settings.REDIS_SOCKET_TIMEOUT,
                socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
                decode_responses=True,
                health_check_interval=30,
            )
            client = redis_lib.Redis(connection_pool=pool)
            client.ping()
            self._redis = client
            self._redis_available = True
            logger.info("Redis cache connected successfully.")
        except Exception as exc:
            self._redis = None
            self._redis_available = False
            self._circuit_open_until = time.monotonic() + 30.0
            logger.warning("Redis unavailable at startup (%s). Using bounded local LRU cache fallback.", exc)

    def _use_redis(self) -> bool:
        if not settings.REDIS_URL or redis_lib is None:
            return False
        if self._redis is not None and time.monotonic() >= self._circuit_open_until:
            return True
        if self._redis is None and time.monotonic() >= self._circuit_open_until:
            with self._lock:
                if self._redis is None and time.monotonic() >= self._circuit_open_until:
                    self._init_redis()
        return self._redis is not None and self._redis_available

    def _trip_circuit(self, exc: Exception) -> None:
        self._errors += 1
        self._redis_available = False
        self._circuit_open_until = time.monotonic() + 15.0
        logger.warning("Redis operation failed (%s); falling back to bounded local cache for 15s.", exc)

    def _full_key(self, key: str) -> str:
        return f"{self.NAMESPACE}{key}"

    def is_enabled(self) -> bool:
        return settings.CACHE_ENABLED and settings.ENVIRONMENT != "testing"

    def get(self, key: str) -> Optional[Any]:
        if not self.is_enabled():
            return None

        full_key = self._full_key(key)
        raw: Optional[str] = None
        if self._use_redis():
            try:
                raw = self._redis.get(full_key)
            except Exception as exc:
                self._trip_circuit(exc)
                raw = self._local.get(full_key)
        else:
            raw = self._local.get(full_key)

        if raw is None:
            self._misses += 1
            return None

        try:
            val = json.loads(raw)
            self._hits += 1
            return val
        except Exception:
            self._misses += 1
            return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        if not self.is_enabled():
            return
        ttl_sec = ttl if ttl is not None else settings.CACHE_DEFAULT_TTL
        try:
            raw = json.dumps(value, default=str)
        except Exception as exc:
            logger.debug("Cache serialization skipped for key=%s: %s", key, exc)
            return

        full_key = self._full_key(key)
        self._sets += 1
        if self._use_redis():
            try:
                self._redis.setex(full_key, ttl_sec, raw)
                return
            except Exception as exc:
                self._trip_circuit(exc)
        self._local.set(full_key, raw, ttl_sec)

    def get_or_set(self, key: str, producer: Callable[[], Any], ttl: Optional[int] = None) -> Any:
        cached = self.get(key)
        if cached is not None:
            return cached
        value = producer()
        self.set(key, value, ttl=ttl)
        return value

    def delete(self, key: str) -> None:
        full_key = self._full_key(key)
        self._invalidations += 1
        self._local.delete(full_key)
        if self._use_redis():
            try:
                self._redis.delete(full_key)
            except Exception as exc:
                self._trip_circuit(exc)

    def delete_prefix(self, prefix: str) -> None:
        full_prefix = self._full_key(prefix)
        self._invalidations += 1
        self._local.delete_prefix(full_prefix)
        if self._use_redis():
            try:
                pattern = f"{full_prefix}*"
                batch = []
                for k in self._redis.scan_iter(match=pattern, count=100):
                    batch.append(k)
                    if len(batch) >= 100:
                        self._redis.delete(*batch)
                        batch.clear()
                if batch:
                    self._redis.delete(*batch)
            except Exception as exc:
                self._trip_circuit(exc)

    def clear_all(self) -> None:
        self._local.clear()
        if self._use_redis():
            self.delete_prefix("")

    # Domain-specific invalidation helpers (prevent stale data across instances)
    def invalidate_quizzes(self, quiz_id: Optional[int] = None) -> None:
        self.delete_prefix("quizzes:public:")
        self.delete_prefix("admin:dashboard:")
        self.delete_prefix("admin:reports:")
        if quiz_id is not None:
            self.delete(f"quiz:meta:{quiz_id}")

    def invalidate_attempts_and_stats(self) -> None:
        self.delete_prefix("admin:dashboard:")
        self.delete_prefix("admin:reports:")

    def invalidate_users(self) -> None:
        self.delete_prefix("admin:dashboard:")
        self.delete_prefix("admin:reports:")

    def ping_redis(self) -> Optional[bool]:
        if not settings.REDIS_URL:
            return None
        if self._redis is None:
            return False
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get_redis_client(self):
        return self._redis if self._use_redis() else None

    def stats(self) -> dict:
        total = self._hits + self._misses
        hit_ratio = round((self._hits / total) * 100.0, 2) if total > 0 else 0.0
        backend = "redis" if self._use_redis() else "local_lru"
        return {
            "enabled": self.is_enabled(),
            "backend": backend,
            "redis_configured": bool(settings.REDIS_URL),
            "redis_connected": self._redis_available,
            "hits": self._hits,
            "misses": self._misses,
            "sets": self._sets,
            "invalidations": self._invalidations,
            "errors": self._errors,
            "hit_ratio_percent": hit_ratio,
            "local_entries": self._local.size(),
            "local_max_entries": settings.CACHE_LOCAL_MAX_ENTRIES,
        }


cache_manager = CacheManager()
