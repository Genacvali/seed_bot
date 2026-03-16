from __future__ import annotations

from dataclasses import dataclass
import os


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

    reply_all: bool
    mention_required: bool

    # GigaChat
    gigachat_client_id: str | None
    gigachat_client_secret: str | None
    gigachat_auth_key: str | None
    gigachat_scope: str
    gigachat_model: str
    gigachat_verify_tls: bool
    gigachat_rate_limit: int   # max requests per minute
    gigachat_streaming: bool   # stream response via SSE

    # Mattermost WS TLS verify
    mattermost_ws_verify_tls: bool

    # MongoDB
    mongo_uri: str | None
    mongo_db: str

    # Webhook server
    webhook_port: int | None   # None = disabled
    webhook_secret: str | None

    # Daily digest
    digest_channel_id: str | None
    digest_time: str            # "HH:MM" UTC

    # Confluence
    confluence_url: str | None
    confluence_token: str | None
    confluence_user: str | None
    confluence_password: str | None
    confluence_verify_tls: bool
    # page IDs для поиска серверов (comma-separated)
    confluence_server_pages: list[str]


def load_config() -> Config:
    mattermost_url = _req("MATTERMOST_URL").rstrip("/")
    mattermost_token = _req("MATTERMOST_TOKEN")
    mattermost_bot_user_id = _req("MATTERMOST_BOT_USER_ID")
    mattermost_bot_username = os.getenv("MATTERMOST_BOT_USERNAME", "seed")

    mattermost_team = os.getenv("MATTERMOST_TEAM")
    mattermost_channel = os.getenv("MATTERMOST_CHANNEL")
    mattermost_channel_id = os.getenv("MATTERMOST_CHANNEL_ID")

    reply_all = _bool("REPLY_ALL", False)
    mention_required = _bool("MENTION_REQUIRED", True)

    gigachat_client_id = os.getenv("GIGACHAT_CLIENT_ID")
    gigachat_client_secret = os.getenv("GIGACHAT_CLIENT_SECRET")
    gigachat_auth_key = os.getenv("GIGACHAT_AUTH_KEY")
    gigachat_scope = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
    gigachat_model = os.getenv("GIGACHAT_MODEL", "GigaChat")
    gigachat_verify_tls = _bool("GIGACHAT_VERIFY_TLS", True)
    gigachat_rate_limit = _int("GIGACHAT_RATE_LIMIT", 20)
    gigachat_streaming = _bool("GIGACHAT_STREAMING", True)

    mattermost_ws_verify_tls = _bool("MATTERMOST_WS_VERIFY_TLS", True)

    mongo_uri = os.getenv("MONGO_URI")
    mongo_db = os.getenv("MONGO_DB", "seed_bot")

    webhook_port_raw = os.getenv("WEBHOOK_PORT")
    webhook_port = int(webhook_port_raw) if webhook_port_raw else None
    webhook_secret = os.getenv("WEBHOOK_SECRET")

    digest_channel_id = os.getenv("DIGEST_CHANNEL_ID")
    digest_time = os.getenv("DIGEST_TIME", "09:00")

    confluence_url = os.getenv("CONFLUENCE_URL")
    confluence_token = os.getenv("CONFLUENCE_TOKEN")
    confluence_user = os.getenv("CONFLUENCE_USER")
    confluence_password = os.getenv("CONFLUENCE_PASSWORD")
    confluence_verify_tls = _bool("CONFLUENCE_VERIFY_TLS", True)
    _pages_raw = os.getenv("CONFLUENCE_SERVER_PAGES", "")
    confluence_server_pages = [p.strip() for p in _pages_raw.split(",") if p.strip()]

    if not ((gigachat_client_id and gigachat_client_secret) or gigachat_auth_key):
        raise RuntimeError(
            "GigaChat credentials missing: set GIGACHAT_CLIENT_ID+GIGACHAT_CLIENT_SECRET "
            "or GIGACHAT_AUTH_KEY"
        )

    return Config(
        mattermost_url=mattermost_url,
        mattermost_token=mattermost_token,
        mattermost_bot_user_id=mattermost_bot_user_id,
        mattermost_bot_username=mattermost_bot_username,
        mattermost_team=mattermost_team,
        mattermost_channel=mattermost_channel,
        mattermost_channel_id=mattermost_channel_id,
        reply_all=reply_all,
        mention_required=mention_required,
        gigachat_client_id=gigachat_client_id,
        gigachat_client_secret=gigachat_client_secret,
        gigachat_auth_key=gigachat_auth_key,
        gigachat_scope=gigachat_scope,
        gigachat_model=gigachat_model,
        gigachat_verify_tls=gigachat_verify_tls,
        gigachat_rate_limit=gigachat_rate_limit,
        gigachat_streaming=gigachat_streaming,
        mattermost_ws_verify_tls=mattermost_ws_verify_tls,
        mongo_uri=mongo_uri,
        mongo_db=mongo_db,
        webhook_port=webhook_port,
        webhook_secret=webhook_secret,
        digest_channel_id=digest_channel_id,
        digest_time=digest_time,
        confluence_url=confluence_url,
        confluence_token=confluence_token,
        confluence_user=confluence_user,
        confluence_password=confluence_password,
        confluence_verify_tls=confluence_verify_tls,
        confluence_server_pages=confluence_server_pages,
    )
