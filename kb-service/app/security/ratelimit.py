"""基础限流（任务 1.12）。

内存**滑动窗口**限流器，用于 ``/api/chat*`` 与管理接口。原型无此能力，属新增。

- **零第三方依赖**（Redis 方案留待批2；本模块已按 ``hit(key)`` 抽象，替换实现不影响调用方）。
- 客户端标识：优先 ``X-Forwarded-For`` 首段，否则 TCP 对端 IP。
- 超限抛 **429**，``detail`` 含「稍后再试」，并带 ``Retry-After`` 头。
- ``RATE_LIMIT_ENABLED=false`` 时直接放行（开发/测试用）。
- 进程内计数：多 worker 部署时各 worker 独立计数；生产多实例需换共享存储。
"""

from __future__ import annotations

import threading
import time
from collections import deque

from fastapi import HTTPException, Request

from app.config import settings

#: 统一话术：验收要求「限流触发返回『稍后再试』」
TOO_MANY = "请求过于频繁，请稍后再试"


class SlidingWindowLimiter:
    """按 key 计数的滑动窗口限流器（线程安全）。"""

    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self.limit = max(1, int(limit))
        self.window = max(1, int(window_seconds))
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(
        self, key: str, limit: int | None = None, now: float | None = None
    ) -> tuple[bool, int, int]:
        """记录一次访问。

        返回 ``(allowed, remaining, retry_after_seconds)``；被拒时 ``remaining=0``。
        """
        cap = max(1, int(limit)) if limit else self.limit
        ts = time.monotonic() if now is None else now
        cutoff = ts - self.window

        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= cap:
                retry_after = max(1, int(self.window - (ts - bucket[0])) + 1)
                return False, 0, retry_after

            bucket.append(ts)
            return True, cap - len(bucket), 0

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)

    def stats(self) -> dict:
        with self._lock:
            return {
                "limit": self.limit,
                "window_seconds": self.window,
                "active_keys": len(self._hits),
            }


#: 全局默认限流器；limit 每次调用时从 settings 读，便于运行期调整与测试
_default_limiter = SlidingWindowLimiter(settings.rate_limit_per_minute)


def get_limiter() -> SlidingWindowLimiter:
    return _default_limiter


def client_key(request: Request) -> str:
    """客户端标识：``X-Forwarded-For`` 首段 → 对端 IP → ``unknown``。"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"


def rate_limit(scope: str):
    """生成一个 FastAPI 依赖，对 ``scope`` 维度限流。"""

    def _dependency(request: Request) -> None:
        if not settings.rate_limit_enabled:
            return
        limiter = _default_limiter
        allowed, _remaining, retry_after = limiter.hit(
            f"{scope}:{client_key(request)}", limit=settings.rate_limit_per_minute
        )
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"{TOO_MANY}（{retry_after} 秒后重试）",
                headers={"Retry-After": str(retry_after)},
            )

    return _dependency
