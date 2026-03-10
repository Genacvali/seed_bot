"""
Priority queue + rate limiting для запросов к GigaChat.

Приоритеты (меньше = важнее):
  0 — alert
  1 — fact / command
  2 — normal chat

Rate limiting: token bucket — max N запросов в минуту.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(order=True)
class _QueueItem:
    priority: int
    seq: int = field(compare=True)
    task: Callable[[], None] = field(compare=False)


class TokenBucket:
    def __init__(self, rate_per_minute: int) -> None:
        self._max = float(rate_per_minute)
        self._tokens = float(rate_per_minute)
        self._rate = rate_per_minute / 60.0   # tokens per second
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Блокирует пока не будет доступен токен."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._max, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
            time.sleep(0.1)


class QueueManager:
    PRIORITY_ALERT = 0
    PRIORITY_COMMAND = 1
    PRIORITY_CHAT = 2

    def __init__(self, rate_per_minute: int = 20, workers: int = 2) -> None:
        self._q: queue.PriorityQueue[_QueueItem] = queue.PriorityQueue()
        self._bucket = TokenBucket(rate_per_minute)
        self._seq = 0
        self._seq_lock = threading.Lock()
        for _ in range(workers):
            t = threading.Thread(target=self._worker, daemon=True)
            t.start()

    def submit(self, task: Callable[[], None], priority: int = PRIORITY_CHAT) -> None:
        with self._seq_lock:
            seq = self._seq
            self._seq += 1
        self._q.put(_QueueItem(priority=priority, seq=seq, task=task))

    def _worker(self) -> None:
        while True:
            item = self._q.get()
            try:
                self._bucket.acquire()
                item.task()
            except Exception as e:
                print(f"[queue] task error: {e}", flush=True)
            finally:
                self._q.task_done()
