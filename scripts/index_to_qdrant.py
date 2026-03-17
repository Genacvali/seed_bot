"""
Индексация коллекций docs и cases из MongoDB в Qdrant.
Запуск из корня проекта: PYTHONPATH=. python scripts/index_to_qdrant.py
Требует: MONGO_URI, QDRANT_URL, GIGACHAT_* в .env
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

# корень проекта
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv()

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance
from pymongo import MongoClient
from bson import ObjectId

# импорт бота для config и GigaChat
from bot.config import load_config
from bot.gigachat import GigaChat


def main() -> None:
    cfg = load_config()
    mongo = MongoClient(cfg.mongo_uri)
    db = mongo[cfg.mongo_db]
    qdrant = QdrantClient(url=cfg.qdrant_url)
    giga = GigaChat(cfg)

    collection = cfg.qdrant_collection
    points_batch = []
    texts_for_embed = []
    meta_batch = []  # (type, id_str) for each text

    # Документы
    for doc in db.docs.find({}):
        oid = doc["_id"]
        text = (doc.get("title") or "") + "\n" + (doc.get("content") or "")
        if not text.strip():
            continue
        texts_for_embed.append(text[:8000])
        meta_batch.append(("doc", str(oid)))

    # Кейсы
    for case in db.cases.find({}):
        oid = case["_id"]
        text = (case.get("title") or "") + "\n" + (case.get("description") or "") + "\n" + (case.get("solution") or "")
        if not text.strip():
            continue
        texts_for_embed.append(text[:8000])
        meta_batch.append(("case", str(oid)))

    if not texts_for_embed:
        print("Нет документов/кейсов для индексации.")
        return

    print(f"Эмбеддинг {len(texts_for_embed)} фрагментов...")
    vectors = giga.embed(texts_for_embed)
    if len(vectors) != len(meta_batch):
        print(f"Ошибка: получили {len(vectors)} векторов, ожидали {len(meta_batch)}")
        return

    size = len(vectors[0])
    if not qdrant.collection_exists(collection):
        qdrant.create_collection(collection, vectors_config=VectorParams(size=size, distance=Distance.COSINE))
        print(f"Создана коллекция {collection} с size={size}")

    for vec, (typ, id_str) in zip(vectors, meta_batch):
        payload = {"type": typ}
        if typ == "doc":
            payload["doc_id"] = id_str
        else:
            payload["case_id"] = id_str
        # Детерминированный id по типу и Mongo _id, чтобы повторный запуск обновлял точки
        point_id = int(hashlib.sha256(f"{typ}:{id_str}".encode()).hexdigest()[:14], 16)
        points_batch.append(PointStruct(id=point_id, vector=vec, payload=payload))

    qdrant.upsert(collection, points_batch)
    print(f"Загружено {len(points_batch)} точек в {collection}")


if __name__ == "__main__":
    main()
