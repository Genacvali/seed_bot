"""
RAG pipeline: запрос -> GigaChat Embeddings -> Qdrant -> MongoDB docs/cases -> GigaChat answer.
"""
from __future__ import annotations

from .gigachat import GigaChat
from .mongo_store import MongoStore
from .qdrant_store import QdrantStore
from .config import Config


def _format_context(docs: list, cases: list) -> str:
    parts = []
    for d in docs:
        title = d.get("title", "—")
        content = d.get("content", "")
        parts.append(f"### Документ: {title}\n{content}")
    for c in cases:
        title = c.get("title", "—")
        description = c.get("description", "")
        solution = c.get("solution", "")
        parts.append(f"### Кейс: {title}\n{description}\n{solution}")
    return "\n\n".join(parts) if parts else ""


def run_rag(cfg: Config, query: str) -> str:
    """
    Один проход RAG: эмбеддинг запроса -> поиск в Qdrant -> загрузка docs/cases из MongoDB -> ответ GigaChat.
    """
    gigachat = GigaChat(cfg)
    qdrant = QdrantStore(cfg)
    mongo = MongoStore(cfg)

    vectors = gigachat.embed([query])
    if not vectors or not vectors[0]:
        return "Не удалось получить эмбеддинг запроса."

    payloads = qdrant.search(vectors[0], limit=cfg.qdrant_limit)
    if not payloads:
        return "По запросу ничего не найдено в базе. Попробуй переформулировать или добавь документы."

    docs, cases = mongo.get_by_ids(payloads)
    context = _format_context(docs, cases)
    if not context.strip():
        return "Найдены ссылки, но не удалось загрузить тексты из базы."

    return gigachat.chat(query, context)
