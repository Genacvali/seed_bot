"""
RAG pipeline: запрос -> (локальные или GigaChat) эмбеддинги -> Qdrant -> MongoDB docs/cases -> GigaChat answer.
"""
from __future__ import annotations

from .config import Config
from .gigachat import GigaChat
from .local_embeddings import LocalEmbeddings
from .mongo_store import MongoStore
from .qdrant_store import QdrantStore


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


def _get_embedder(cfg: Config):
    """Возвращает объект с методом embed(texts) -> list[list[float]]."""
    if cfg.use_local_embeddings:
        return LocalEmbeddings(cfg)
    return GigaChat(cfg)


def run_rag(cfg: Config, query: str) -> str:
    """
    Один проход RAG: эмбеддинг запроса -> поиск в Qdrant -> загрузка docs/cases из MongoDB -> ответ GigaChat.
    """
    embedder = _get_embedder(cfg)
    gigachat = GigaChat(cfg)
    qdrant = QdrantStore(cfg)
    mongo = MongoStore(cfg)

    vectors = embedder.embed([query])
    if not vectors or not vectors[0]:
        return "Не удалось получить эмбеддинг запроса."

    try:
        payloads = qdrant.search(vectors[0], limit=cfg.qdrant_limit)
    except Exception as e:
        err_text = str(e)
        if "dimension" in err_text.lower() or "expected dim" in err_text:
            return (
                "Несовпадение размерности векторов с коллекцией Qdrant: текущая модель даёт другую размерность, "
                "чем при создании коллекции. Пересоздай коллекцию и переиндексируй: "
                "`python scripts/setup_rag.py --recreate`"
            )
        raise

    context = ""
    if payloads:
        docs, cases = mongo.get_by_ids(payloads)
        context = _format_context(docs, cases)

    return gigachat.chat(query, context)
