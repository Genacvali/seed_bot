"""
MongoDB: коллекции docs и cases.
Используются для хранения документов/кейсов; по id из Qdrant подтягиваем полные тексты.
"""
from __future__ import annotations

from typing import Any

from pymongo import MongoClient
from bson import ObjectId

from .config import Config


class MongoStore:
    def __init__(self, cfg: Config) -> None:
        self._client = MongoClient(cfg.mongo_uri)
        self._db = self._client[cfg.mongo_db]
        self._docs = self._db.docs
        self._cases = self._db.cases

    def get_docs_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """Возвращает документы из коллекции docs по списку id (из payload Qdrant)."""
        if not ids:
            return []
        object_ids = []
        for i in ids:
            try:
                object_ids.append(ObjectId(i))
            except Exception:
                pass
        if not object_ids:
            return []
        cursor = self._docs.find({"_id": {"$in": object_ids}})
        return list(cursor)

    def get_cases_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """Возвращает кейсы из коллекции cases по списку id."""
        if not ids:
            return []
        object_ids = []
        for i in ids:
            try:
                object_ids.append(ObjectId(i))
            except Exception:
                pass
        if not object_ids:
            return []
        cursor = self._cases.find({"_id": {"$in": object_ids}})
        return list(cursor)

    def get_by_ids(self, payloads: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
        """
        По списку payload из Qdrant (поля type, doc_id или case_id) возвращает
        (list[doc], list[case]).
        """
        doc_ids = [p["doc_id"] for p in payloads if p.get("type") == "doc" and p.get("doc_id")]
        case_ids = [p["case_id"] for p in payloads if p.get("type") == "case" and p.get("case_id")]
        docs = self.get_docs_by_ids(doc_ids)
        cases = self.get_cases_by_ids(case_ids)
        return docs, cases
