#!/usr/bin/env python3
"""
Инициализация MongoDB для RAG-бота: коллекции docs и cases.
Безопасно запускать повторно.

Использование:
    python scripts/mongo_init.py
    python scripts/mongo_init.py --uri mongodb://localhost:27017 --db seed_bot
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING

load_dotenv()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", default="mongodb://localhost:27017", help="MongoDB URI")
    ap.add_argument("--db", default="seed_bot", help="Database name")
    args = ap.parse_args()

    client = MongoClient(args.uri)
    db = client[args.db]

    for name in ("docs", "cases"):
        if name not in db.list_collection_names():
            db.create_collection(name)
            print(f"Created collection: {name}")
        else:
            print(f"Collection exists: {name}")

    # Индекс по created_at для сортировки (опционально)
    if "docs" in db.list_collection_names():
        db.docs.create_index([("created_at", ASCENDING)], name="created_at")
    if "cases" in db.list_collection_names():
        db.cases.create_index([("created_at", ASCENDING)], name="created_at")

    print("Done.")


if __name__ == "__main__":
    main()
