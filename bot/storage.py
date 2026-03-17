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
from pymongo.errors import OperationFailure

from .config import Config

_DAY = 60 * 60 * 24


def _create_index_safe(collection: Collection[Any], keys: Any, **kwargs: Any) -> None:
    """Создаёт индекс; игнорирует ошибки «уже существует» / «другое имя» (85, 86)."""
    try:
        collection.create_index(keys, **kwargs)
    except OperationFailure as e:
        if e.code in (85, 86):  # IndexOptionsConflict, IndexKeySpecsConflict
            pass  # индекс уже есть или с другим именем — ок
        else:
            raise


class Storage:
    def __init__(self, cfg: Config) -> None:
        if not cfg.mongo_uri:
            raise RuntimeError("MONGO_URI is not set")
        self._client: MongoClient[Any] = MongoClient(cfg.mongo_uri)
        self._db: Database[Any] = self._client[cfg.mongo_db]
        # Сначала проверяем связь; исключение здесь → Storage недоступен
        self._client.admin.command("ping")
        # Индексы создаём с защитой — ошибка индекса не должна убивать бота
        try:
            self._ensure_indexes()
        except Exception as e:
            print(f"[storage] index init warning (non-fatal): {e}", flush=True)

    # ------------------------------------------------------------------
    # Index bootstrap
    # ------------------------------------------------------------------

    def _ensure_indexes(self) -> None:
        db = self._db
        idx = _create_index_safe

        # 1. dialog_history — TTL 7 дней (имена как в mongo_init.py, иначе конфликт)
        idx(db.dialog_history, [("created_at", ASCENDING)], expireAfterSeconds=_DAY * 7, name="ttl_7d")
        idx(db.dialog_history, [("thread_id", ASCENDING), ("created_at", ASCENDING)], name="thread_time")
        idx(db.dialog_history, [("user_id", ASCENDING)], name="user")

        # 2. alert_stats — уникальность по (alert_type, server)
        idx(db.alert_stats, [("alert_type", ASCENDING), ("server", ASCENDING)], unique=True, name="alert_server_unique")
        idx(db.alert_stats, [("last_fired_at", DESCENDING)], name="last_fired")

        # 3. alert_rules
        idx(db.alert_rules, [("pattern", ASCENDING)], unique=True, name="pattern_unique")
        idx(db.alert_rules, [("enabled", ASCENDING)], name="enabled")

        # 4. known_incidents
        idx(db.known_incidents, [("tags", ASCENDING)])
        idx(db.known_incidents, [("title", "text"), ("description", "text")])

        # 5. runbooks
        idx(db.runbooks, [("title", "text"), ("content", "text")])
        idx(db.runbooks, [("tags", ASCENDING)])

        # 6. server_registry
        idx(db.server_registry, [("hostname", ASCENDING)], unique=True)
        idx(db.server_registry, [("env", ASCENDING)])
        idx(db.server_registry, [("role", ASCENDING)])

        # 7. user_preferences
        idx(db.user_preferences, [("user_id", ASCENDING)], unique=True)

        # 8. channel_settings
        idx(db.channel_settings, [("channel_id", ASCENDING)], unique=True)

        # 9. command_shortcuts
        idx(db.command_shortcuts, [("user_id", ASCENDING), ("shortcut", ASCENDING)], unique=True)

        # 10. feedback
        idx(db.feedback, [("post_id", ASCENDING)], unique=True)
        idx(db.feedback, [("user_id", ASCENDING)])
        idx(db.feedback, [("rating", ASCENDING)])

        # 11. bot_errors — TTL 30 дней
        idx(db.bot_errors, [("created_at", ASCENDING)], expireAfterSeconds=_DAY * 30, name="ttl_30d")
        idx(db.bot_errors, [("error_type", ASCENDING)], name="error_type")
        idx(db.bot_errors, [("created_at", DESCENDING)], name="created_desc")

        # 12. on_call_schedule
        idx(db.on_call_schedule, [("start_at", ASCENDING), ("end_at", ASCENDING)], name="period")
        idx(db.on_call_schedule, [("user_id", ASCENDING)], name="user")

        # 13. escalation_log — TTL 90 дней
        idx(db.escalation_log, [("created_at", ASCENDING)], expireAfterSeconds=_DAY * 90, name="ttl_90d")
        idx(db.escalation_log, [("alert_type", ASCENDING)], name="alert_type")
        idx(db.escalation_log, [("resolved", ASCENDING)], name="resolved")
        idx(db.escalation_log, [("created_at", DESCENDING)], name="created_desc")

        # 14. metrics_snapshot — TTL 30 дней
        idx(db.metrics_snapshot, [("created_at", ASCENDING)], expireAfterSeconds=_DAY * 30, name="ttl_30d")
        idx(db.metrics_snapshot, [("server", ASCENDING), ("created_at", DESCENDING)], name="server_time")

        # 15. bot_usage_stats
        idx(db.bot_usage_stats, [("user_id", ASCENDING), ("date", ASCENDING)], unique=True, name="user_date_unique")

        # 16. facts — база знаний, извлечённая из диалогов
        idx(db.facts, [("summary", "text"), ("raw_text", "text")], name="fulltext")
        idx(db.facts, [("fact_type", ASCENDING)])
        idx(db.facts, [("entities.users", ASCENDING)])
        idx(db.facts, [("entities.servers", ASCENDING)])
        idx(db.facts, [("entities.tags", ASCENDING)])
        idx(db.facts, [("created_by", ASCENDING)])
        idx(db.facts, [("created_at", ASCENDING)])

        # 17. confluence_pages — страницы Confluence для поиска серверов
        idx(db.confluence_pages, [("page_id", ASCENDING)], unique=True)
        idx(db.confluence_pages, [("space_key", ASCENDING)])
        idx(db.confluence_pages, [("added_by", ASCENDING)])

        # 18. people — профили людей команды
        idx(db.people, [("username", ASCENDING)], unique=True)
        idx(db.people, [("display_name", "text"), ("role", "text")], name="people_fulltext")
        idx(db.people, [("display_name", ASCENDING)])

        # 19. glossary — словарь команды (аббревиатуры и термины)
        idx(db.glossary, [("term", ASCENDING)], unique=True)
        idx(db.glossary, [("term", "text"), ("expansion", "text"), ("context", "text")], name="glossary_fulltext")

        # 20. knowledge_gaps — пробелы в знаниях (что бот не смог ответить)
        idx(db.knowledge_gaps, [("question_hash", ASCENDING)], unique=True, name="gap_hash")
        idx(db.knowledge_gaps, [("resolved", ASCENDING)])
        idx(db.knowledge_gaps, [("times", DESCENDING)])
        idx(db.knowledge_gaps, [("last_asked", DESCENDING)])

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
        links: list[str] | None = None,
    ) -> str:
        """
        Сохраняет извлечённый факт.
        structured_updates — список {collection, filter, update}.
        links — list[str] связанных fact_id или summary (feature 11).
        Возвращает строковый _id.
        """
        doc = {
            "fact_type": fact_type,
            "summary": summary,
            "raw_text": raw_text,
            "entities": entities,
            "created_by": created_by,
            "created_at": _now(),
            "history": [],       # feature 12: версионирование
            "links": links or [], # feature 11: связи между сущностями
        }
        result = self._db.facts.insert_one(doc)

        for upd in (structured_updates or []):
            col = self._db[upd["collection"]]
            col.update_many(upd["filter"], upd["update"], upsert=upd.get("upsert", False))

        return str(result.inserted_id)

    def update_fact(self, fact_id: str, new_summary: str, updated_by: str) -> bool:
        """Обновляет факт, сохраняя старую версию в history (feature 12)."""
        from bson import ObjectId
        old = self._db.facts.find_one({"_id": ObjectId(fact_id)})
        if not old:
            return False
        history_entry = {
            "summary": old.get("summary", ""),
            "raw_text": old.get("raw_text", ""),
            "updated_at": _now(),
            "updated_by": updated_by,
        }
        self._db.facts.update_one(
            {"_id": ObjectId(fact_id)},
            {
                "$set": {"summary": new_summary, "updated_at": _now(), "updated_by": updated_by},
                "$push": {"history": history_entry},
            },
        )
        return True

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
    # Runbooks — RAG: полнотекстовый поиск по инструкциям
    # ------------------------------------------------------------------

    def search_runbooks(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Full-text поиск по runbooks (title + content). Для RAG."""
        if not query or not query.strip():
            return []
        try:
            cursor = self._db.runbooks.find(
                {"$text": {"$search": query.strip()}},
                {
                    "score": {"$meta": "textScore"},
                    "title": 1,
                    "content": 1,
                    "tags": 1,
                    "_id": 0,
                },
            ).sort([("score", {"$meta": "textScore"})]).limit(limit)
            return list(cursor)
        except Exception as e:
            print(f"[storage] search_runbooks error: {e}", flush=True)
            return []

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
    # 17. Confluence pages registry
    # ------------------------------------------------------------------

    def add_confluence_page(
        self,
        page_id: str,
        title: str,
        space_key: str,
        url: str,
        added_by: str,
    ) -> bool:
        """Добавляет страницу в реестр. Возвращает True если новая, False если уже была."""
        result = self._db.confluence_pages.update_one(
            {"page_id": page_id},
            {
                "$set": {
                    "title": title,
                    "space_key": space_key,
                    "url": url,
                    "added_by": added_by,
                    "updated_at": _now(),
                },
                "$setOnInsert": {"added_at": _now()},
            },
            upsert=True,
        )
        return result.upserted_id is not None

    def list_confluence_pages(self) -> list[dict[str, Any]]:
        return list(
            self._db.confluence_pages.find(
                {}, {"_id": 0, "page_id": 1, "title": 1, "space_key": 1, "url": 1, "added_by": 1}
            ).sort("added_at", ASCENDING)
        )

    def remove_confluence_page(self, page_id: str) -> bool:
        result = self._db.confluence_pages.delete_one({"page_id": page_id})
        return result.deleted_count > 0

    def get_confluence_page_ids(self) -> list[str]:
        return [
            d["page_id"]
            for d in self._db.confluence_pages.find({}, {"page_id": 1, "_id": 0})
        ]

    # ------------------------------------------------------------------
    # 18. People — профили людей команды
    # ------------------------------------------------------------------

    def save_person(
        self,
        username: str,
        display_name: str,
        role: str = "",
        expertise: list[str] | None = None,
        added_by: str = "",
    ) -> bool:
        """Сохраняет или обновляет профиль человека. True = новый."""
        result = self._db.people.update_one(
            {"username": username.lower().lstrip("@")},
            {
                "$set": {
                    "display_name": display_name,
                    "role": role,
                    "expertise": expertise or [],
                    "added_by": added_by,
                    "updated_at": _now(),
                },
                "$setOnInsert": {"created_at": _now()},
            },
            upsert=True,
        )
        return result.upserted_id is not None

    def find_person(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Ищет человека по имени, нику, роли."""
        q = query.strip()
        return list(
            self._db.people.find(
                {
                    "$or": [
                        {"username": {"$regex": q, "$options": "i"}},
                        {"display_name": {"$regex": q, "$options": "i"}},
                        {"role": {"$regex": q, "$options": "i"}},
                    ]
                },
                {"_id": 0},
            ).limit(limit)
        )

    def list_people(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(
            self._db.people.find({}, {"_id": 0}).sort("display_name", ASCENDING).limit(limit)
        )

    def get_user_profile(self, user_id: str) -> dict[str, Any] | None:
        return self._db.user_preferences.find_one({"user_id": user_id}, {"_id": 0})

    def upsert_user_profile(self, user_id: str, **fields: Any) -> None:
        """Обновляет поля профиля пользователя (feature 5)."""
        self._db.user_preferences.update_one(
            {"user_id": user_id},
            {"$set": {**fields, "updated_at": _now()}, "$setOnInsert": {"created_at": _now()}},
            upsert=True,
        )

    # ------------------------------------------------------------------
    # 19. Glossary — словарь команды
    # ------------------------------------------------------------------

    def save_term(
        self,
        term: str,
        expansion: str,
        context: str = "",
        added_by: str = "",
    ) -> bool:
        """Сохраняет термин/аббревиатуру. True = новый."""
        result = self._db.glossary.update_one(
            {"term": term.lower().strip()},
            {
                "$set": {
                    "expansion": expansion,
                    "context": context,
                    "added_by": added_by,
                    "updated_at": _now(),
                },
                "$setOnInsert": {"created_at": _now()},
            },
            upsert=True,
        )
        return result.upserted_id is not None

    def find_term(self, term: str) -> dict[str, Any] | None:
        return self._db.glossary.find_one({"term": term.lower().strip()}, {"_id": 0})

    def search_glossary(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        q = query.strip()
        return list(
            self._db.glossary.find(
                {
                    "$or": [
                        {"term": {"$regex": q, "$options": "i"}},
                        {"expansion": {"$regex": q, "$options": "i"}},
                    ]
                },
                {"_id": 0},
            ).limit(limit)
        )

    def list_terms(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(
            self._db.glossary.find({}, {"_id": 0}).sort("term", ASCENDING).limit(limit)
        )

    def build_glossary_context(self) -> str:
        """Строит блок контекста из словаря для инжекции в промпт LLM."""
        terms = self.list_terms(limit=30)
        if not terms:
            return ""
        lines = ["### Словарь команды (расшифровки аббревиатур):"]
        for t in terms:
            line = f"- `{t['term']}` = {t['expansion']}"
            if t.get("context"):
                line += f"  ({t['context']})"
            lines.append(line)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 20. Knowledge gaps — пробелы в знаниях
    # ------------------------------------------------------------------

    def log_gap(
        self,
        question: str,
        user_id: str,
        channel_id: str,
        thread_id: str,
    ) -> None:
        """Логирует вопрос, на который бот не смог ответить."""
        h = abs(hash(question.strip().lower())) % (10 ** 9)
        self._db.knowledge_gaps.update_one(
            {"question_hash": h},
            {
                "$set": {"question": question[:500], "last_asked": _now()},
                "$inc": {"times": 1},
                "$setOnInsert": {
                    "user_id": user_id,
                    "channel_id": channel_id,
                    "thread_id": thread_id,
                    "resolved": False,
                    "created_at": _now(),
                },
            },
            upsert=True,
        )

    def get_gaps(self, resolved: bool = False, limit: int = 10) -> list[dict[str, Any]]:
        return list(
            self._db.knowledge_gaps.find(
                {"resolved": resolved},
                {"_id": 0, "question": 1, "times": 1, "last_asked": 1, "channel_id": 1},
            )
            .sort([("times", DESCENDING), ("last_asked", DESCENDING)])
            .limit(limit)
        )

    def resolve_gap(self, question: str) -> int:
        """Помечает gap как решённый по точному вопросу. Возвращает кол-во обновлённых."""
        h = abs(hash(question.strip().lower())) % (10 ** 9)
        r = self._db.knowledge_gaps.update_one(
            {"question_hash": h}, {"$set": {"resolved": True}}
        )
        return r.modified_count

    def get_weekly_learning_digest(self) -> dict[str, Any]:
        """Данные для еженедельного дайджеста обучения (feature 14)."""
        from datetime import timedelta
        week_ago = _now() - timedelta(days=7)

        new_facts = self._db.facts.count_documents({"created_at": {"$gte": week_ago}})
        new_people = self._db.people.count_documents({"created_at": {"$gte": week_ago}})
        new_terms = self._db.glossary.count_documents({"created_at": {"$gte": week_ago}})

        # Кто больше всего учил бота
        top_teachers_cursor = self._db.facts.aggregate([
            {"$match": {"created_at": {"$gte": week_ago}}},
            {"$group": {"_id": "$created_by", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 5},
        ])
        top_teachers = list(top_teachers_cursor)

        # Топ пробелов
        gaps = self.get_gaps(resolved=False, limit=5)

        return {
            "new_facts": new_facts,
            "new_people": new_people,
            "new_terms": new_terms,
            "top_teachers": top_teachers,
            "top_gaps": gaps,
        }

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
