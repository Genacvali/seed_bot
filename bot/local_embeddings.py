"""
Локальные эмбеддинги через sentence-transformers (без GigaChat API).
"""
from __future__ import annotations

from typing import Any

from .config import Config


class LocalEmbeddings:
    """Эмбеддинги на основе sentence-transformers (многоязычные модели)."""

    def __init__(self, cfg: Config) -> None:
        self._model_name = cfg.local_embeddings_model
        self._model: Any = None

    def _get_model(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            print(f"[embeddings] загрузка модели {self._model_name}...", flush=True)
            self._model = SentenceTransformer(self._model_name)
            print(f"[embeddings] модель загружена, размерность {self._model.get_sentence_embedding_dimension()}", flush=True)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Возвращает список векторов. Пустой список при ошибке."""
        if not texts:
            return []
        try:
            model = self._get_model()
            vectors = model.encode(
                [t[:8000] for t in texts],
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            return vectors.tolist()
        except Exception as e:
            print(f"[embeddings] error: {e}", flush=True)
            return []

    def dimension(self) -> int:
        """Размерность вектора (для создания коллекции Qdrant)."""
        return self._get_model().get_sentence_embedding_dimension()
