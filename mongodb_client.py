from datetime import datetime, timezone
from pymongo import MongoClient, ASCENDING
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from config import (
    MONGODB_URI, MONGODB_DB, MONGODB_HISTORY_TTL_DAYS,
    COLLECTION_MESSAGES, COLLECTION_PROCESSED_POSTS,
)


def get_client() -> MongoClient:
    return MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)


def get_db(client: MongoClient) -> Database:
    return client[MONGODB_DB]


def get_messages_collection(db: Database) -> Collection:
    return db[COLLECTION_MESSAGES]


def get_processed_posts_collection(db: Database) -> Collection:
    return db[COLLECTION_PROCESSED_POSTS]


def ensure_indexes(messages_coll: Collection, processed_coll: Collection) -> None:
    ttl_seconds = MONGODB_HISTORY_TTL_DAYS * 24 * 60 * 60
    messages_coll.create_index("created_at", expireAfterSeconds=ttl_seconds, name="ttl_30_days")
    messages_coll.create_index(
        [("thread_id", ASCENDING), ("created_at", ASCENDING)],
        name="thread_created",
    )
    processed_coll.create_index("created_at", expireAfterSeconds=ttl_seconds, name="ttl_processed")
    # _id = post_id, уникальность не даёт обработать один post дважды
    # (ни вторым экземпляром бота, ни при двойной доставке события)


def try_claim_post(processed_coll: Collection, post_id: str) -> bool:
    """Взять пост в обработку. True — мы первые, False — уже обработан (дубль/другой инстанс)."""
    try:
        processed_coll.insert_one({
            "_id": post_id,
            "created_at": datetime.now(timezone.utc),
        })
        return True
    except DuplicateKeyError:
        return False


def release_claim(processed_coll: Collection, post_id: str) -> None:
    """Освободить бронь — вызывать если обработка завершилась ошибкой и нужен повтор."""
    try:
        processed_coll.delete_one({"_id": post_id})
    except Exception:
        pass


def save_message(collection: Collection, *, channel_id: str, thread_id: str,
                 user_id: str, role: str, content: str) -> None:
    collection.insert_one({
        "channel_id": channel_id,
        "thread_id": thread_id,
        "user_id": user_id,
        "role": role,
        "content": content,
        "created_at": datetime.now(timezone.utc),
    })


def get_thread_history(collection: Collection, channel_id: str,
                       thread_id: str, limit: int = 50) -> list[dict]:
    return list(
        collection.find(
            {"channel_id": channel_id, "thread_id": thread_id},
            projection={"role": 1, "content": 1, "_id": 0},
        )
        .sort("created_at", ASCENDING)
        .limit(limit)
    )
