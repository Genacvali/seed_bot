"""
Qdrant: векторный поиск по эмбеддингам.
Payload точек: type ("doc" | "case"), doc_id / case_id (str, MongoDB _id).
"""
from __future__ import annotations

from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

from .config import Config


class QdrantStore:
    def __init__(self, cfg: Config) -> None:
        self._client = QdrantClient(url=cfg.qdrant_url)
        self._collection = cfg.qdrant_collection
        self._limit = cfg.qdrant_limit

    def search(
        self,
        vector: list[float],
        limit: int | None = None,
        type_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Поиск по вектору. Возвращает список payload (без score в элементах;
        score можно получить из result если нужно).
        type_filter: "doc" | "case" | None — фильтр по полю type в payload.
        """
        lim = limit or self._limit
        query_filter = None
        if type_filter:
            query_filter = Filter(
                must=[FieldCondition(key="type", match=MatchValue(value=type_filter))]
            )
        results = self._client.search(
            collection_name=self._collection,
            query_vector=vector,
            limit=lim,
            query_filter=query_filter,
        )
        payloads = []
        for r in results:
            payload = dict(r.payload or {})
            payload["_score"] = r.score
            payloads.append(payload)
        return payloads
