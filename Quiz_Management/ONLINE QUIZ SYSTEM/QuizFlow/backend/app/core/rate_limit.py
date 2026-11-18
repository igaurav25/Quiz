"""
Distributed Rate Limiter for QuizFlow.
Uses Redis INCR + EXPIRE for multi-instance consistency when configured,
and falls back to a bounded thread-safe in-memory window counter for single-node local dev.
"""
import threading
import time
from collections import OrderedDict
from typing import Tuple

from app.core.cache import cache_manager
from app.core.config import settings


class RateLimiter:
    def __init__(self, max_local_keys: int = 10000):
        self._lock = threading.Lock()
        self._local: OrderedDict[str, tuple[int, int]] = OrderedDict()  # key -> (window_id, count)
        self._max_local_keys = max_local_keys

    def check(self, bucket: str, identifier: str, limit_per_minute: int) -> Tuple[bool, int, int]:
        """
        Check and increment rate limit counter.
        Returns (allowed, remaining, reset_seconds).
        """
        if not settings.RATE_LIMIT_ENABLED or limit_per_minute <= 0:
            return True, limit_per_minute, 60

        window_id = int(time.time() // 60)
        reset_seconds = 60 - int(time.time() % 60)
        key = f"qf:rl:{bucket}:{identifier}:{window_id}"

        redis_client = cache_manager.get_redis_client()
        if redis_client is not None:
            try:
                pipe = redis_client.pipeline(transaction=True)
                pipe.incr(key, 1)
                pipe.expire(key, 65)
                current_count, _ = pipe.execute()
                allowed = int(current_count) <= limit_per_minute
                remaining = max(0, limit_per_minute - int(current_count))
                return allowed, remaining, reset_seconds
            except Exception:
                pass

        # Bounded local fallback
        local_key = f"{bucket}:{identifier}"
        with self._lock:
            entry = self._local.get(local_key)
            if entry is None or entry[0] != window_id:
                count = 1
            else:
                count = entry[1] + 1
            self._local[local_key] = (window_id, count)
            self._local.move_to_end(local_key)
            while len(self._local) > self._max_local_keys:
                self._local.popitem(last=False)

        allowed = count <= limit_per_minute
        remaining = max(0, limit_per_minute - count)
        return allowed, remaining, reset_seconds

    def reset_local(self) -> None:
        with self._lock:
            self._local.clear()


rate_limiter = RateLimiter()
