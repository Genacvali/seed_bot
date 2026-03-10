#!/usr/bin/env python3
"""
Инициализация MongoDB для seed-bot.

Создаёт все коллекции с индексами и TTL.
Безопасно запускать повторно — существующие индексы не затрагиваются.

Использование:
    python scripts/mongo_init.py
    python scripts/mongo_init.py --uri mongodb://user:pass@host:27017 --db seed_bot
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Позволяем запускать из любой директории
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from pymongo import ASCENDING, DESCENDING, MongoClient, TEXT

_DAY = 60 * 60 * 24

COLLECTIONS: dict[str, dict] = {
    # 1. История диалогов — TTL 7 дней
    "dialog_history": {
        "indexes": [
            {"keys": [("created_at", ASCENDING)], "expireAfterSeconds": _DAY * 7, "name": "ttl_7d"},
            {"keys": [("thread_id", ASCENDING), ("created_at", ASCENDING)], "name": "thread_time"},
            {"keys": [("user_id", ASCENDING)], "name": "user"},
        ],
        "validator": {
            "$jsonSchema": {
                "bsonType": "object",
                "required": ["thread_id", "user_id", "role", "content", "created_at"],
                "properties": {
                    "thread_id": {"bsonType": "string"},
                    "user_id":   {"bsonType": "string"},
                    "role":      {"bsonType": "string", "enum": ["user", "assistant", "system"]},
                    "content":   {"bsonType": "string"},
                    "created_at": {"bsonType": "date"},
                },
            }
        },
    },

    # 2. Статистика алертов
    "alert_stats": {
        "indexes": [
            {
                "keys": [("alert_type", ASCENDING), ("server", ASCENDING)],
                "unique": True,
                "name": "alert_server_unique",
            },
            {"keys": [("last_fired_at", DESCENDING)], "name": "last_fired"},
            {"keys": [("count", DESCENDING)], "name": "count_desc"},
        ],
        "validator": {
            "$jsonSchema": {
                "bsonType": "object",
                "required": ["alert_type", "server", "count"],
                "properties": {
                    "alert_type":    {"bsonType": "string"},
                    "server":        {"bsonType": "string"},
                    "count":         {"bsonType": "int"},
                    "first_fired_at": {"bsonType": "date"},
                    "last_fired_at":  {"bsonType": "date"},
                },
            }
        },
    },

    # 3. Правила классификации алертов
    "alert_rules": {
        "indexes": [
            {"keys": [("pattern", ASCENDING)], "unique": True, "name": "pattern_unique"},
            {"keys": [("enabled", ASCENDING)], "name": "enabled"},
            {"keys": [("priority", DESCENDING)], "name": "priority"},
        ],
    },

    # 4. База известных инцидентов
    "known_incidents": {
        "indexes": [
            {"keys": [("tags", ASCENDING)], "name": "tags"},
            {"keys": [("title", TEXT), ("description", TEXT)], "name": "fulltext"},
            {"keys": [("created_at", DESCENDING)], "name": "created_desc"},
        ],
    },

    # 5. Runbooks
    "runbooks": {
        "indexes": [
            {"keys": [("title", TEXT), ("content", TEXT)], "name": "fulltext"},
            {"keys": [("tags", ASCENDING)], "name": "tags"},
            {"keys": [("updated_at", DESCENDING)], "name": "updated_desc"},
        ],
    },

    # 6. Реестр серверов
    "server_registry": {
        "indexes": [
            {"keys": [("hostname", ASCENDING)], "unique": True, "name": "hostname_unique"},
            {"keys": [("env", ASCENDING)], "name": "env"},
            {"keys": [("role", ASCENDING)], "name": "role"},
            {"keys": [("env", ASCENDING), ("role", ASCENDING)], "name": "env_role"},
        ],
        "validator": {
            "$jsonSchema": {
                "bsonType": "object",
                "required": ["hostname"],
                "properties": {
                    "hostname": {"bsonType": "string"},
                    "env":      {"bsonType": "string", "enum": ["prod", "staging", "dev", "test"]},
                    "role":     {"bsonType": "string"},
                    "ip":       {"bsonType": "string"},
                    "tags":     {"bsonType": "array"},
                },
            }
        },
    },

    # 7. Настройки пользователей
    "user_preferences": {
        "indexes": [
            {"keys": [("user_id", ASCENDING)], "unique": True, "name": "user_unique"},
        ],
    },

    # 8. Настройки каналов
    "channel_settings": {
        "indexes": [
            {"keys": [("channel_id", ASCENDING)], "unique": True, "name": "channel_unique"},
        ],
    },

    # 9. Кастомные шорткаты
    "command_shortcuts": {
        "indexes": [
            {
                "keys": [("user_id", ASCENDING), ("shortcut", ASCENDING)],
                "unique": True,
                "name": "user_shortcut_unique",
            },
        ],
    },

    # 10. Фидбек / оценки
    "feedback": {
        "indexes": [
            {"keys": [("post_id", ASCENDING)], "unique": True, "name": "post_unique"},
            {"keys": [("user_id", ASCENDING)], "name": "user"},
            {"keys": [("rating", ASCENDING)], "name": "rating"},
            {"keys": [("created_at", DESCENDING)], "name": "created_desc"},
        ],
        "validator": {
            "$jsonSchema": {
                "bsonType": "object",
                "required": ["post_id", "user_id", "rating"],
                "properties": {
                    "post_id": {"bsonType": "string"},
                    "user_id": {"bsonType": "string"},
                    "rating":  {"bsonType": "int", "minimum": 1, "maximum": 5},
                    "comment": {"bsonType": "string"},
                },
            }
        },
    },

    # 11. Лог ошибок бота — TTL 30 дней
    "bot_errors": {
        "indexes": [
            {"keys": [("created_at", ASCENDING)], "expireAfterSeconds": _DAY * 30, "name": "ttl_30d"},
            {"keys": [("error_type", ASCENDING)], "name": "error_type"},
            {"keys": [("created_at", DESCENDING)], "name": "created_desc"},
        ],
    },

    # 12. Расписание дежурств
    "on_call_schedule": {
        "indexes": [
            {"keys": [("start_at", ASCENDING), ("end_at", ASCENDING)], "name": "period"},
            {"keys": [("user_id", ASCENDING)], "name": "user"},
        ],
    },

    # 13. Лог эскалаций — TTL 90 дней
    "escalation_log": {
        "indexes": [
            {"keys": [("created_at", ASCENDING)], "expireAfterSeconds": _DAY * 90, "name": "ttl_90d"},
            {"keys": [("alert_type", ASCENDING)], "name": "alert_type"},
            {"keys": [("resolved", ASCENDING)], "name": "resolved"},
            {"keys": [("created_at", DESCENDING)], "name": "created_desc"},
        ],
    },

    # 14. Снимки метрик при алертах — TTL 30 дней
    "metrics_snapshot": {
        "indexes": [
            {"keys": [("created_at", ASCENDING)], "expireAfterSeconds": _DAY * 30, "name": "ttl_30d"},
            {
                "keys": [("server", ASCENDING), ("created_at", DESCENDING)],
                "name": "server_time",
            },
        ],
    },

    # 15. Дневная статистика использования
    "bot_usage_stats": {
        "indexes": [
            {
                "keys": [("user_id", ASCENDING), ("date", ASCENDING)],
                "unique": True,
                "name": "user_date_unique",
            },
            {"keys": [("date", ASCENDING)], "name": "date"},
        ],
    },
}


def init_db(uri: str, db_name: str, drop: bool = False) -> None:
    client: MongoClient = MongoClient(uri, serverSelectionTimeoutMS=5000)

    # Проверяем соединение
    try:
        client.admin.command("ping")
        print(f"✓ Connected to MongoDB")
    except Exception as e:
        print(f"✗ Cannot connect to MongoDB: {e}")
        sys.exit(1)

    db = client[db_name]

    if drop:
        print(f"  Dropping database '{db_name}'...")
        client.drop_database(db_name)
        print(f"  Dropped.")

    existing = set(db.list_collection_names())

    for col_name, spec in COLLECTIONS.items():
        # Создаём коллекцию с валидатором если её нет
        if col_name not in existing:
            kwargs = {}
            if "validator" in spec:
                kwargs["validator"] = spec["validator"]
                kwargs["validationAction"] = "warn"  # warn, не reject — не блокируем старые записи
            db.create_collection(col_name, **kwargs)
            print(f"  [+] Created collection '{col_name}'")
        else:
            print(f"  [~] Collection '{col_name}' already exists")

        col = db[col_name]
        existing_idx = {i["name"] for i in col.list_indexes()}

        for idx_spec in spec.get("indexes", []):
            idx_spec = dict(idx_spec)
            name = idx_spec.pop("name", None)
            keys = idx_spec.pop("keys")

            if name in existing_idx:
                print(f"      [~] index '{name}' already exists")
                continue

            col.create_index(keys, name=name, **idx_spec)
            print(f"      [+] index '{name}'")

    print(f"\n✓ Done. Database '{db_name}' has {len(COLLECTIONS)} collections.")
    client.close()


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Init MongoDB for seed-bot")
    parser.add_argument(
        "--uri",
        default=os.getenv("MONGO_URI", "mongodb://localhost:27017"),
        help="MongoDB URI (default: $MONGO_URI or mongodb://localhost:27017)",
    )
    parser.add_argument(
        "--db",
        default=os.getenv("MONGO_DB", "seed_bot"),
        help="Database name (default: $MONGO_DB or seed_bot)",
    )
    parser.add_argument(
        "--drop",
        action="store_true",
        help="DROP and recreate the database (осторожно!)",
    )
    args = parser.parse_args()

    print(f"MongoDB URI : {args.uri}")
    print(f"Database    : {args.db}")
    if args.drop:
        confirm = input("⚠ Это удалит ВСЕ данные. Введите 'yes' для подтверждения: ")
        if confirm.strip().lower() != "yes":
            print("Отменено.")
            sys.exit(0)
    print()

    init_db(args.uri, args.db, drop=args.drop)


if __name__ == "__main__":
    main()
