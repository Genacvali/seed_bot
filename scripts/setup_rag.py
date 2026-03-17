#!/usr/bin/env python3
"""
Один скрипт настройки RAG: создаёт коллекции в MongoDB и Qdrant, при необходимости индексирует данные.

Что делает:
  - MongoDB: создаёт коллекции docs и cases (если нет).
  - Qdrant: узнаёт размерность вектора через GigaChat, создаёт коллекцию (если нет).
  - Если в MongoDB есть данные — индексирует их в Qdrant.

Для Qdrant с API ключом задай в .env: QDRANT_API_KEY=...

Запуск из корня проекта:
    python scripts/setup_rag.py

Нужен .env с: MONGO_URI, QDRANT_URL, GIGACHAT_* (и MATTERMOST_* для load_config; можно заглушки).

Опции:
    --only-create   только создать коллекции (MongoDB + Qdrant), не индексировать
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from pymongo import MongoClient, ASCENDING
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance

# конфиг и эмбеддинги из бота
from bot.config import load_config
from bot.local_embeddings import LocalEmbeddings
from bot.gigachat import GigaChat


def main() -> None:
    ap = argparse.ArgumentParser(description="Setup MongoDB + Qdrant for RAG bot")
    ap.add_argument("--only-create", action="store_true", help="Только создать коллекции, не индексировать")
    ap.add_argument("--recreate", action="store_true", help="Удалить коллекцию Qdrant и создать заново (при смене размерности эмбеддингов)")
    args = ap.parse_args()

    print("Загрузка конфига...")
    cfg = load_config()

    # ─── MongoDB ─────────────────────────────────────────────────────
    print("\n[MongoDB] Подключение...")
    mongo = MongoClient(cfg.mongo_uri)
    db = mongo[cfg.mongo_db]

    for name in ("docs", "cases", "conversations"):
        if name not in db.list_collection_names():
            db.create_collection(name)
            print(f"  Создана коллекция: {name}")
        else:
            print(f"  Коллекция уже есть: {name}")

    for col, idx, fields in [
        ("docs",          "created_at",   [("created_at", ASCENDING)]),
        ("cases",         "created_at",   [("created_at", ASCENDING)]),
        ("conversations", "user_history", [("user_id", ASCENDING), ("created_at", -1)]),
        ("conversations", "thread_order", [("thread_id", ASCENDING), ("created_at", ASCENDING)]),
    ]:
        try:
            db[col].create_index(fields, name=idx)
        except Exception:
            pass

    # ─── Qdrant: размерность вектора и коллекция ──────────────────────
    print("\n[Qdrant] Подключение...")
    qdrant_kwargs: dict = {"url": cfg.qdrant_url}
    if cfg.qdrant_api_key:
        qdrant_kwargs["api_key"] = cfg.qdrant_api_key
    qdrant = QdrantClient(**qdrant_kwargs)
    collection = cfg.qdrant_collection

    if cfg.use_local_embeddings:
        embedder = LocalEmbeddings(cfg)
        vector_size = embedder.dimension()
        print(f"  Размерность вектора (локальная модель): {vector_size}")
    else:
        embedder = GigaChat(cfg)
        test_vectors = embedder.embed(["test"])
        if not test_vectors or not test_vectors[0]:
            print("  Ошибка: не удалось получить эмбеддинг (проверь GIGACHAT_* в .env)")
            sys.exit(1)
        vector_size = len(test_vectors[0])
        print(f"  Размерность вектора GigaChat: {vector_size}")

    if args.recreate and qdrant.collection_exists(collection):
        qdrant.delete_collection(collection)
        print(f"  Удалена коллекция: {collection}")

    if not qdrant.collection_exists(collection):
        qdrant.create_collection(
            collection,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )
        print(f"  Создана коллекция: {collection} (size={vector_size})")
    else:
        print(f"  Коллекция уже есть: {collection}")

    if args.only_create:
        print("\nГотово (--only-create: индексация пропущена).")
        return

    # ─── Индексация docs и cases в Qdrant ────────────────────────────
    texts_for_embed = []
    meta_batch = []  # (type, id_str)

    for doc in db.docs.find({}):
        oid = doc["_id"]
        text = (doc.get("title") or "") + "\n" + (doc.get("content") or "")
        if not text.strip():
            continue
        texts_for_embed.append(text[:8000])
        meta_batch.append(("doc", str(oid)))

    for case in db.cases.find({}):
        oid = case["_id"]
        text = (
            (case.get("title") or "")
            + "\n" + (case.get("description") or "")
            + "\n" + (case.get("solution") or "")
        )
        if not text.strip():
            continue
        texts_for_embed.append(text[:8000])
        meta_batch.append(("case", str(oid)))

    if not texts_for_embed:
        print("\nВ MongoDB нет документов/кейсов для индексации. Добавь данные и запусти скрипт снова.")
        return

    print(f"\n[Индексация] Эмбеддинг {len(texts_for_embed)} фрагментов...")
    vectors = embedder.embed(texts_for_embed)
    if len(vectors) != len(meta_batch):
        print(f"  Ошибка: получено {len(vectors)} векторов, ожидалось {len(meta_batch)}")
        sys.exit(1)

    points = []
    for vec, (typ, id_str) in zip(vectors, meta_batch):
        payload = {"type": typ}
        if typ == "doc":
            payload["doc_id"] = id_str
        else:
            payload["case_id"] = id_str
        point_id = int(hashlib.sha256(f"{typ}:{id_str}".encode()).hexdigest()[:14], 16)
        points.append(PointStruct(id=point_id, vector=vec, payload=payload))

    qdrant.upsert(collection, points)
    print(f"  Загружено {len(points)} точек в {collection}")

    print("\nГотово.")


if __name__ == "__main__":
    main()
