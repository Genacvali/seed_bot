"""
MongoDB storage layer for seed-bot.

Collections (15):
  1.  dialog_history      – сообщения диалогов (TTL 7 дней)
  2.  alert_stats         – статистика алертов (тип, сервер, кол-во)
  3.  alert_rules         – правила классификации алертов
  4.  known_incidents     – база известных инцидентов и решений
  5.  runbooks            – инструкции по устранению типовых проблем
  6.  server_registry     – реестр серверов/сервисов
  7.  user_preferences    – настройки пользователей
  8.  channel_settings    – настройки каналов
  9.  command_shortcuts   – пользовательские шорткаты/команды
 10.  feedback            – оценки ответов бота
 11.  bot_errors          – лог ошибок бота (TTL 30 дней)
 12.  on_call_schedule    – расписание дежурств
 13.  escalation_log      – лог эскалаций (TTL 90 дней)
 14.  metrics_snapshot    – снимки метрик при алертах (TTL 30 дней)
 15.  bot_usage_stats     – агрегированная статистика использования бота
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from .config import Config

_DAY = 60 * 60 * 24


class Storage:
    def __init__(self, cfg: Config) -> None:
        if not cfg.mongo_uri:
            raise RuntimeError("MONGO_URI is not set")
        self._client: MongoClient[Any] = MongoClient(cfg.mongo_uri)
        self._db: Database[Any] = self._client[cfg.mongo_db]
        self._ensure_indexes()

    # ------------------------------------------------------------------
    # Index bootstrap
    # ------------------------------------------------------------------

    def _ensure_indexes(self) -> None:
        db = self._db

        # 1. dialog_history — TTL 7 дней
        db.dialog_history.create_index("created_at", expireAfterSeconds=_DAY * 7)
        db.dialog_history.create_index([("thread_id", ASCENDING), ("created_at", ASCENDING)])
        db.dialog_history.create_index("user_id")

        # 2. alert_stats — уникальность по (alert_type, server)
        db.alert_stats.create_index(
            [("alert_type", ASCENDING), ("server", ASCENDING)],
            unique=True,
        )
        db.alert_stats.create_index("last_fired_at")

        # 3. alert_rules
        db.alert_rules.create_index("pattern", unique=True)
        db.alert_rules.create_index("enabled")

        # 4. known_incidents
        db.known_incidents.create_index([("tags", ASCENDING)])
        db.known_incidents.create_index([("title", "text"), ("description", "text")])

        # 5. runbooks
        db.runbooks.create_index([("title", "text"), ("content", "text")])
        db.runbooks.create_index("tags")

        # 6. server_registry
        db.server_registry.create_index("hostname", unique=True)
        db.server_registry.create_index("env")   # prod / staging / dev
        db.server_registry.create_index("role")  # db / app / cache / ...

        # 7. user_preferences
        db.user_preferences.create_index("user_id", unique=True)

        # 8. channel_settings
        db.channel_settings.create_index("channel_id", unique=True)

        # 9. command_shortcuts
        db.command_shortcuts.create_index(
            [("user_id", ASCENDING), ("shortcut", ASCENDING)],
            unique=True,
        )

        # 10. feedback
        db.feedback.create_index("post_id", unique=True)
        db.feedback.create_index("user_id")
        db.feedback.create_index("rating")

        # 11. bot_errors — TTL 30 дней
        db.bot_errors.create_index("created_at", expireAfterSeconds=_DAY * 30)
        db.bot_errors.create_index("error_type")

        # 12. on_call_schedule
        db.on_call_schedule.create_index([("start_at", ASCENDING), ("end_at", ASCENDING)])
        db.on_call_schedule.create_index("user_id")

        # 13. escalation_log — TTL 90 дней
        db.escalation_log.create_index("created_at", expireAfterSeconds=_DAY * 90)
        db.escalation_log.create_index("alert_type")
        db.escalation_log.create_index("resolved")

        # 14. metrics_snapshot — TTL 30 дней
        db.metrics_snapshot.create_index("created_at", expireAfterSeconds=_DAY * 30)
        db.metrics_snapshot.create_index([("server", ASCENDING), ("created_at", DESCENDING)])

        # 15. bot_usage_stats
        db.bot_usage_stats.create_index(
            [("user_id", ASCENDING), ("date", ASCENDING)],
            unique=True,
        )

        # 16. facts — база знаний, извлечённая из диалогов
        db.facts.create_index([("summary", "text"), ("raw_text", "text")], name="fulltext")
        db.facts.create_index("fact_type")
        db.facts.create_index("entities.users")
        db.facts.create_index("entities.servers")
        db.facts.create_index("entities.tags")
        db.facts.create_index("created_by")
        db.facts.create_index("created_at")

    # ------------------------------------------------------------------
    # 1. Dialog history
    # ------------------------------------------------------------------

    def append_dialog(self, thread_id: str, user_id: str, role: str, content: str) -> None:
        self._db.dialog_history.insert_one({
            "thread_id": thread_id,
            "user_id": user_id,
            "role": role,           # "user" | "assistant"
            "content": content,
            "created_at": _now(),
        })

    def get_dialog(self, thread_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Возвращает последние `limit` сообщений треда в хронологическом порядке."""
        cursor = (
            self._db.dialog_history
            .find({"thread_id": thread_id}, {"role": 1, "content": 1, "_id": 0})
            .sort("created_at", ASCENDING)
        )
        msgs = list(cursor)
        return msgs[-limit:] if len(msgs) > limit else msgs

    # ------------------------------------------------------------------
    # 2. Alert stats
    # ------------------------------------------------------------------

    def inc_alert(self, alert_type: str, server: str) -> None:
        """Атомарно увеличивает счётчик и обновляет last_fired_at."""
        self._db.alert_stats.update_one(
            {"alert_type": alert_type, "server": server},
            {
                "$inc": {"count": 1},
                "$set": {"last_fired_at": _now()},
                "$setOnInsert": {"first_fired_at": _now()},
            },
            upsert=True,
        )

    def top_alerts(self, limit: int = 10) -> list[dict[str, Any]]:
        return list(
            self._db.alert_stats
            .find({}, {"_id": 0})
            .sort("count", DESCENDING)
            .limit(limit)
        )

    # ------------------------------------------------------------------
    # 10. Feedback
    # ------------------------------------------------------------------

    def save_feedback(self, post_id: str, user_id: str, rating: int, comment: str = "") -> None:
        self._db.feedback.update_one(
            {"post_id": post_id},
            {"$set": {"user_id": user_id, "rating": rating, "comment": comment, "created_at": _now()}},
            upsert=True,
        )

    # ------------------------------------------------------------------
    # 11. Bot errors
    # ------------------------------------------------------------------

    def log_error(self, error_type: str, message: str, context: dict[str, Any] | None = None) -> None:
        self._db.bot_errors.insert_one({
            "error_type": error_type,
            "message": message,
            "context": context or {},
            "created_at": _now(),
        })

    # ------------------------------------------------------------------
    # 15. Bot usage stats (daily upsert)
    # ------------------------------------------------------------------

    def inc_usage(self, user_id: str) -> None:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        self._db.bot_usage_stats.update_one(
            {"user_id": user_id, "date": today},
            {"$inc": {"requests": 1}},
            upsert=True,
        )

    # ------------------------------------------------------------------
    # 16. Facts — база знаний
    # ------------------------------------------------------------------

    def save_fact(
        self,
        fact_type: str,
        summary: str,
        raw_text: str,
        entities: dict[str, Any],
        created_by: str,
        structured_updates: list[dict[str, Any]] | None = None,
    ) -> str:
        """
        Сохраняет извлечённый факт.
        structured_updates — список {collection, filter, update} для записи
        в структурированные коллекции (server_registry и т.д.).
        Возвращает строковый _id.
        """
        doc = {
            "fact_type": fact_type,
            "summary": summary,
            "raw_text": raw_text,
            "entities": entities,
            "created_by": created_by,
            "created_at": _now(),
        }
        result = self._db.facts.insert_one(doc)

        # Применяем структурированные апдейты в нужные коллекции
        for upd in (structured_updates or []):
            col = self._db[upd["collection"]]
            col.update_many(upd["filter"], upd["update"], upsert=upd.get("upsert", False))

        return str(result.inserted_id)

    def search_facts(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Full-text поиск по базе знаний."""
        cursor = self._db.facts.find(
            {"$text": {"$search": query}},
            {"score": {"$meta": "textScore"}, "summary": 1, "fact_type": 1, "entities": 1, "_id": 0},
        ).sort([("score", {"$meta": "textScore"})]).limit(limit)
        return list(cursor)

    def get_facts_by_type(self, fact_type: str, limit: int = 20) -> list[dict[str, Any]]:
        return list(
            self._db.facts
            .find({"fact_type": fact_type}, {"summary": 1, "entities": 1, "_id": 0})
            .sort("created_at", DESCENDING)
            .limit(limit)
        )

    def get_all_facts(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(
            self._db.facts
            .find({}, {"summary": 1, "fact_type": 1, "entities": 1, "_id": 0})
            .sort("created_at", DESCENDING)
            .limit(limit)
        )

    # ------------------------------------------------------------------
    # On-call schedule
    # ------------------------------------------------------------------

    def get_current_oncall(self, now: datetime) -> dict[str, Any] | None:
        return self._db.on_call_schedule.find_one(
            {"start_at": {"$lte": now}, "end_at": {"$gte": now}},
        )

    def set_oncall(self, username: str, user_id: str, start_at: datetime, end_at: datetime) -> None:
        self._db.on_call_schedule.insert_one({
            "username": username,
            "user_id": user_id,
            "start_at": start_at,
            "end_at": end_at,
            "created_at": _now(),
        })

    # ------------------------------------------------------------------
    # Known incidents
    # ------------------------------------------------------------------

    def save_known_issue(
        self,
        title: str,
        description: str,
        tags: list[str],
        created_by: str,
        solution: str = "",
    ) -> str:
        doc = {
            "title": title,
            "description": description,
            "solution": solution,
            "tags": tags,
            "created_by": created_by,
            "created_at": _now(),
        }
        result = self._db.known_incidents.insert_one(doc)
        return str(result.inserted_id)

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------

    def save_reaction_feedback(self, post_id: str, user_id: str, emoji: str) -> None:
        """Сохраняет реакцию пользователя на пост бота."""
        self._db.feedback.update_one(
            {"post_id": post_id, "user_id": user_id},
            {"$set": {"emoji": emoji, "created_at": _now()}},
            upsert=True,
        )

    # ------------------------------------------------------------------
    # Daily digest
    # ------------------------------------------------------------------

    def get_daily_digest(self, date_str: str) -> dict[str, Any]:
        """Возвращает агрегированные данные для дайджеста за дату 'YYYY-MM-DD'."""
        from datetime import date
        day_start = datetime.fromisoformat(f"{date_str}T00:00:00+00:00")
        day_end = datetime.fromisoformat(f"{date_str}T23:59:59+00:00")

        total_requests = sum(
            d.get("requests", 0)
            for d in self._db.bot_usage_stats.find({"date": date_str})
        )
        top_alerts = list(
            self._db.alert_stats
            .find({"last_fired_at": {"$gte": day_start, "$lte": day_end}}, {"_id": 0})
            .sort("count", DESCENDING)
            .limit(5)
        )
        errors_count = self._db.bot_errors.count_documents(
            {"created_at": {"$gte": day_start, "$lte": day_end}}
        )
        new_facts = self._db.facts.count_documents(
            {"created_at": {"$gte": day_start, "$lte": day_end}}
        )

        return {
            "date": date_str,
            "total_requests": total_requests,
            "top_alerts": top_alerts,
            "errors_count": errors_count,
            "new_facts": new_facts,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        try:
            self._client.admin.command("ping")
            return True
        except Exception:
            return False

    def close(self) -> None:
        self._client.close()


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)
