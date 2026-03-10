"""
Корреляция алертов.

Отслеживает потоки алертов за последние N минут и обнаруживает:
- Повторные алерты одного типа
- Каскадные сбои (много разных серверов за короткое время)
- Флаппинг (быстрые on/off переключения)
"""
from __future__ import annotations

import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field


_WINDOW_SEC = 10 * 60   # скользящее окно 10 минут
_CASCADE_THRESHOLD = 3  # >= 3 разных сервера за окно = каскад
_FLAP_THRESHOLD = 3     # >= 3 срабатываний одного алерта за окно = флаппинг


@dataclass
class AlertEntry:
    server: str
    alert_type: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class CorrelationResult:
    is_cascade: bool = False
    is_flapping: bool = False
    cascade_servers: list[str] = field(default_factory=list)
    flap_count: int = 0
    related_alerts: list[AlertEntry] = field(default_factory=list)
    note: str = ""


class AlertCorrelator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # server -> list of (timestamp, alert_type)
        self._recent: list[AlertEntry] = []
        # pending resolutions: thread_id -> alert info
        self._pending_resolution: dict[str, dict] = {}

    def record(self, server: str, alert_type: str) -> CorrelationResult:
        """Записывает алерт и возвращает результат корреляции."""
        now = time.time()
        entry = AlertEntry(server=server, alert_type=alert_type, timestamp=now)

        with self._lock:
            self._recent.append(entry)
            # чистим старые
            self._recent = [e for e in self._recent if now - e.timestamp < _WINDOW_SEC]
            window = list(self._recent)

        return self._correlate(entry, window)

    def mark_pending_resolution(self, thread_id: str, alert_info: dict) -> None:
        """Помечает тред как ожидающий подтверждения решения."""
        self._pending_resolution[thread_id] = alert_info

    def check_resolution(self, thread_id: str, text: str) -> bool:
        """
        Проверяет: выглядит ли текст как сообщение о решении проблемы.
        Если да — возвращает True, очищает pending.
        """
        if thread_id not in self._pending_resolution:
            return False
        if _is_resolution_message(text):
            del self._pending_resolution[thread_id]
            return True
        return False

    def is_alert_thread(self, thread_id: str) -> bool:
        return thread_id in self._pending_resolution

    # ------------------------------------------------------------------

    def _correlate(self, entry: AlertEntry, window: list[AlertEntry]) -> CorrelationResult:
        result = CorrelationResult()

        # Флаппинг: тот же сервер + тот же тип алерта >= FLAP_THRESHOLD раз
        same = [e for e in window if e.server == entry.server and e.alert_type == entry.alert_type]
        if len(same) >= _FLAP_THRESHOLD:
            result.is_flapping = True
            result.flap_count = len(same)

        # Каскад: >= CASCADE_THRESHOLD разных серверов за окно
        unique_servers = list({e.server for e in window})
        if len(unique_servers) >= _CASCADE_THRESHOLD:
            result.is_cascade = True
            result.cascade_servers = unique_servers

        # Похожие алерты в окне (кроме текущего)
        result.related_alerts = [e for e in window if e is not entry]

        # Формируем заметку
        notes = []
        if result.is_flapping:
            notes.append(f"⚡ Флаппинг: этот алерт сработал {result.flap_count}x за 10 минут")
        if result.is_cascade:
            servers_str = ", ".join(f"`{s}`" for s in result.cascade_servers[:5])
            notes.append(f"🌊 Похоже на каскадный сбой: {len(unique_servers)} серверов за 10 минут ({servers_str})")
        result.note = "\n".join(notes)

        return result


def parse_alert_server(text: str) -> tuple[str, str]:
    """
    Пытается извлечь (server, alert_type) из текста алерта.
    Формат: "hostname: Alert Type\n..."
    """
    m = re.match(r"^([a-zA-Z0-9._:-]+):\s*(.+?)(?:\n|$)", text.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()[:100]
    return "unknown", text.strip()[:100]


_RESOLUTION_KEYWORDS = [
    "починил", "исправил", "решили", "решил", "решено", "fixed", "resolved",
    "закрыл", "закрыто", "ок", "всё ок", "всё норм", "норм", "работает",
    "проблема устранена", "помогло", "помогли", "разобрался",
]


def _is_resolution_message(text: str) -> bool:
    t = text.lower().strip()
    return any(kw in t for kw in _RESOLUTION_KEYWORDS)
