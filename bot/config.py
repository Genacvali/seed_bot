"""Конфигурация бота: Mattermost, GigaChat, MongoDB, Qdrant."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _req(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


@dataclass(frozen=True)
class Config:
    # Mattermost
    mattermost_url: str
    mattermost_token: str
    mattermost_bot_user_id: str
    mattermost_bot_username: str
    mattermost_team: str | None
    mattermost_channel: str | None
    mattermost_channel_id: str | None
    mattermost_verify_tls: bool
    reply_all: bool
    mention_required: bool

    # GigaChat (OAuth для embeddings + chat)
    gigachat_client_id: str | None
    gigachat_client_secret: str | None
    gigachat_auth_key: str | None
    gigachat_scope: str
    gigachat_model: str
    gigachat_verify_tls: bool
    gigachat_embeddings_model: str

    # MongoDB (docs + cases)
    mongo_uri: str
    mongo_db: str

    # Qdrant (векторный поиск)
    qdrant_url: str
    qdrant_api_key: str | None
    qdrant_collection: str
    qdrant_limit: int


def load_config() -> Config:
    mattermost_url = _req("MATTERMOST_URL").rstrip("/")
    mattermost_token = _req("MATTERMOST_TOKEN")
    mattermost_bot_user_id = _req("MATTERMOST_BOT_USER_ID")
    mattermost_bot_username = os.getenv("MATTERMOST_BOT_USERNAME", "seed")
    mattermost_team = os.getenv("MATTERMOST_TEAM")
    mattermost_channel = os.getenv("MATTERMOST_CHANNEL")
    mattermost_channel_id = os.getenv("MATTERMOST_CHANNEL_ID")
    mattermost_verify_tls = _bool("MATTERMOST_VERIFY_TLS", True)
    reply_all = _bool("REPLY_ALL", False)
    mention_required = _bool("MENTION_REQUIRED", True)

    gigachat_client_id = os.getenv("GIGACHAT_CLIENT_ID")
    gigachat_client_secret = os.getenv("GIGACHAT_CLIENT_SECRET")
    gigachat_auth_key = os.getenv("GIGACHAT_AUTH_KEY")
    gigachat_scope = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
    gigachat_model = os.getenv("GIGACHAT_MODEL", "GigaChat")
    gigachat_verify_tls = _bool("GIGACHAT_VERIFY_TLS", True)
    gigachat_embeddings_model = os.getenv("GIGACHAT_EMBEDDINGS_MODEL", "Embeddings")

    mongo_uri = _req("MONGO_URI")
    mongo_db = os.getenv("MONGO_DB", "seed_bot")

    qdrant_url = _req("QDRANT_URL").rstrip("/")
    qdrant_api_key = os.getenv("QDRANT_API_KEY") or None
    qdrant_collection = os.getenv("QDRANT_COLLECTION", "knowledge")
    qdrant_limit = _int("QDRANT_LIMIT", 5)

    if not ((gigachat_client_id and gigachat_client_secret) or gigachat_auth_key):
        raise RuntimeError(
            "GigaChat: set GIGACHAT_CLIENT_ID+GIGACHAT_CLIENT_SECRET or GIGACHAT_AUTH_KEY"
        )

    return Config(
        mattermost_url=mattermost_url,
        mattermost_token=mattermost_token,
        mattermost_bot_user_id=mattermost_bot_user_id,
        mattermost_bot_username=mattermost_bot_username,
        mattermost_team=mattermost_team,
        mattermost_channel=mattermost_channel,
        mattermost_channel_id=mattermost_channel_id,
        mattermost_verify_tls=mattermost_verify_tls,
        reply_all=reply_all,
        mention_required=mention_required,
        gigachat_client_id=gigachat_client_id,
        gigachat_client_secret=gigachat_client_secret,
        gigachat_auth_key=gigachat_auth_key,
        gigachat_scope=gigachat_scope,
        gigachat_model=gigachat_model,
        gigachat_verify_tls=gigachat_verify_tls,
        gigachat_embeddings_model=gigachat_embeddings_model,
        mongo_uri=mongo_uri,
        mongo_db=mongo_db,
        qdrant_url=qdrant_url,
        qdrant_api_key=qdrant_api_key,
        qdrant_collection=qdrant_collection,
        qdrant_limit=qdrant_limit,
    )
