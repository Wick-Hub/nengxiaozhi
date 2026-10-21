"""客服工作台 WebSocket 推送（任务 4.6 / M-S-03）。

单进程内的事件总线：坐席连接工作台后注册一个队列，工单创建 / 新消息 /
状态变更通过 :func:`publish` 推给所有在线坐席。同步接口运行在线程池，
因此发布时用 ``loop.call_soon_threadsafe`` 回到连接所在事件循环。

多 worker 部署需要换成 Redis Pub/Sub；本项目原型为单进程，文档已注明。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    seat_id: int | None = None


_subscribers: dict[int, _Subscriber] = {}
_lock = threading.Lock()
_next_id = 1


def subscribe(*, seat_id: int | None = None) -> tuple[int, asyncio.Queue]:
    """注册一个工作台连接，返回订阅 ID 与事件队列。"""
    global _next_id
    loop = asyncio.get_running_loop()
    with _lock:
        sub_id = _next_id
        _next_id += 1
        _subscribers[sub_id] = _Subscriber(loop=loop, seat_id=seat_id)
        return sub_id, _subscribers[sub_id].queue


def unsubscribe(sub_id: int) -> None:
    with _lock:
        _subscribers.pop(sub_id, None)


def publish(event: dict) -> None:
    """向所有工作台连接广播事件；同步线程安全。"""
    with _lock:
        subscribers = list(_subscribers.values())
    for sub in subscribers:
        try:
            sub.loop.call_soon_threadsafe(sub.queue.put_nowait, dict(event))
        except RuntimeError:  # 事件循环已关闭
            continue
        except Exception as exc:  # noqa: BLE001 - 推送失败不能影响业务写入
            logger.warning("客服事件推送失败：%s", exc)


def connection_count() -> int:
    with _lock:
        return len(_subscribers)


def seat_connection_count(seat_id: int) -> int:
    """某坐席当前的工作台连接数（用于最后一个连接断开时自动下线）。"""
    with _lock:
        return sum(1 for sub in _subscribers.values() if sub.seat_id == int(seat_id))
