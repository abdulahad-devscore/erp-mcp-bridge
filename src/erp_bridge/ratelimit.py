"""Fixed-window rate limits per OAuth client.

Redis in production so limits hold across bridge replicas; an in-memory
fallback for tests and single-process runs.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Protocol

from redis.asyncio import Redis


class RateLimited(Exception):
    def __init__(self, bucket: str, limit: int, retry_after: int):
        super().__init__(f"Rate limit exceeded for {bucket}: {limit}/min. Retry in {retry_after}s.")
        self.retry_after = retry_after


class RateLimiter(Protocol):
    async def hit(self, key: str, limit: int, window: int = 60) -> None: ...


class MemoryRateLimiter:
    def __init__(self) -> None:
        self._counts: dict[tuple[str, int], int] = defaultdict(int)

    async def hit(self, key: str, limit: int, window: int = 60) -> None:
        bucket = int(time.time() // window)
        self._counts[(key, bucket)] += 1
        if self._counts[(key, bucket)] > limit:
            raise RateLimited(key, limit, window - int(time.time() % window))


class RedisRateLimiter:
    def __init__(self, redis: Redis):
        self.redis = redis

    async def hit(self, key: str, limit: int, window: int = 60) -> None:
        bucket = int(time.time() // window)
        rkey = f"rl:{key}:{bucket}"
        async with self.redis.pipeline(transaction=True) as pipe:
            count, _ = await pipe.incr(rkey).expire(rkey, window + 5).execute()
        if count > limit:
            raise RateLimited(key, limit, window - int(time.time() % window))
