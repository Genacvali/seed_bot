"""
Планировщик задач по расписанию.
Поддерживает ежедневные и еженедельные задачи.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Callable


class Scheduler:
    def __init__(self) -> None:
        self._daily_jobs: list[tuple[str, Callable[[], None]]] = []
        # (hhmm, weekday 0=Mon…6=Sun, job)
        self._weekly_jobs: list[tuple[str, int, Callable[[], None]]] = []
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")

    def add_daily(self, time_utc: str, job: Callable[[], None]) -> None:
        """time_utc — 'HH:MM'"""
        self._daily_jobs.append((time_utc, job))

    def add_weekly(self, time_utc: str, weekday: int, job: Callable[[], None]) -> None:
        """time_utc — 'HH:MM', weekday — 0=Mon … 6=Sun"""
        self._weekly_jobs.append((time_utc, weekday, job))

    def start(self) -> None:
        self._thread.start()

    def _loop(self) -> None:
        fired_today: set[str] = set()
        last_date = ""

        while True:
            now = datetime.now(tz=timezone.utc)
            today = now.strftime("%Y-%m-%d")
            hhmm = now.strftime("%H:%M")
            weekday = now.weekday()  # 0=Mon

            if today != last_date:
                fired_today.clear()
                last_date = today

            for time_str, job in self._daily_jobs:
                key = f"daily_{today}_{time_str}"
                if hhmm == time_str and key not in fired_today:
                    fired_today.add(key)
                    self._run(job, time_str)

            for time_str, wd, job in self._weekly_jobs:
                key = f"weekly_{today}_{time_str}_{wd}"
                if hhmm == time_str and weekday == wd and key not in fired_today:
                    fired_today.add(key)
                    self._run(job, f"{time_str} (weekly wd={wd})")

            time.sleep(30)

    def _run(self, job: Callable[[], None], label: str) -> None:
        try:
            print(f"[scheduler] running job at {label} UTC", flush=True)
            job()
        except Exception as e:
            print(f"[scheduler] job error at {label}: {e}", flush=True)
