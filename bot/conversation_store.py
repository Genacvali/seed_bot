"""
Хранение истории диалогов в MongoDB.

Коллекция `conversations`:
  user_id    — Mattermost user_id человека (не бота)
  thread_id  — root_post_id треда
  channel_id — channel_id
  role       — "user" | "assistant"
  content    — текст сообщения
  created_at — datetime UTC

Позволяет боту помнить контекст между разными тредами одного пользователя.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pymongo import MongoClient, ASCENDING, DESCENDING

from .config import Config

_MAX_CONTENT_LEN = 2000  # обрезаем очень длинные сообщения при сохранении


class ConversationStore:
    def __init__(self, cfg: Config) -> None:
        self._client: MongoClient[Any] = MongoClient(cfg.mongo_uri)
        self._col = self._client[cfg.mongo_db]["conversations"]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        try:
            self._col.create_index(
                [("user_id", ASCENDING), ("created_at", DESCENDING)],
                name="user_history",
            )
            self._col.create_index(
                [("thread_id", ASCENDING), ("created_at", ASCENDING)],
                name="thread_order",
            )
        except Exception:
            pass

    def save(
        self,
        *,
        user_id: str,
        thread_id: str,
        channel_id: str,
        role: str,
        content: str,
    ) -> None:
        """Сохраняет одно сообщение диалога."""
        self._col.insert_one({
            "user_id": user_id,
            "thread_id": thread_id,
            "channel_id": channel_id,
            "role": role,
            "content": content[:_MAX_CONTENT_LEN],
            "created_at": datetime.utcnow(),
        })

    def get_cross_thread_history(
        self,
        user_id: str,
        current_thread_id: str,
        limit: int = 20,
    ) -> list[dict[str, str]]:
        """
        Возвращает последние `limit` сообщений этого пользователя из других тредов
        в хронологическом порядке (для передачи в GigaChat как history).
        """
        cursor = self._col.find(
            {"user_id": user_id, "thread_id": {"$ne": current_thread_id}},
            sort=[("created_at", DESCENDING)],
            limit=limit,
        )
        msgs = list(cursor)
        msgs.reverse()
        return [{"role": m["role"], "content": m["content"]} for m in msgs]
