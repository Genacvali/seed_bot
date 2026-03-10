"""
Планировщик задач по расписанию (daily digest и другие).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Callable


class Scheduler:
    def __init__(self) -> None:
        self._jobs: list[tuple[str, Callable[[], None]]] = []
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")

    def add_daily(self, time_utc: str, job: Callable[[], None]) -> None:
        """time_utc — 'HH:MM'"""
        self._jobs.append((time_utc, job))

    def start(self) -> None:
        self._thread.start()

    def _loop(self) -> None:
        fired_today: set[str] = set()
        last_date = ""

        while True:
            now = datetime.now(tz=timezone.utc)
            today = now.strftime("%Y-%m-%d")
            hhmm = now.strftime("%H:%M")

            # Сбрасываем флаги в начале нового дня
            if today != last_date:
                fired_today.clear()
                last_date = today

            for time_str, job in self._jobs:
                key = f"{today}_{time_str}"
                if hhmm == time_str and key not in fired_today:
                    fired_today.add(key)
                    try:
                        print(f"[scheduler] running job at {time_str} UTC", flush=True)
                        job()
                    except Exception as e:
                        print(f"[scheduler] job error: {e}", flush=True)

            time.sleep(30)
