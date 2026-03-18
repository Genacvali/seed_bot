import os
from dotenv import load_dotenv

load_dotenv()

# Mattermost
MATTERMOST_URL = os.getenv("MATTERMOST_URL", "https://mm.sberdevices.ru")
MATTERMOST_TOKEN = os.getenv("MATTERMOST_TOKEN", "")
MATTERMOST_BOT_USER_ID = os.getenv("MATTERMOST_BOT_USER_ID", "")
MATTERMOST_BOT_USERNAME = os.getenv("MATTERMOST_BOT_USERNAME", "seed")
MENTION_REQUIRED = os.getenv("MENTION_REQUIRED", "true").lower() == "true"

# Авто-саммари алертов
MONITORING_BOT_USERNAME = os.getenv("MONITORING_BOT_USERNAME", "")
ALERT_AUTO_SUMMARY = os.getenv("ALERT_AUTO_SUMMARY", "true").lower() == "true"

# GigaChat
GIGACHAT_CLIENT_ID = os.getenv("GIGACHAT_CLIENT_ID", "")
GIGACHAT_CLIENT_SECRET = os.getenv("GIGACHAT_CLIENT_SECRET", "")
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-2")
GIGACHAT_VERIFY_TLS = os.getenv("GIGACHAT_VERIFY_TLS", "false").lower() == "true"
GIGACHAT_RATE_LIMIT = int(os.getenv("GIGACHAT_RATE_LIMIT", "20"))

# MongoDB (принимаем оба варианта: MONGO_URI и MONGODB_URI)
MONGODB_URI = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB = os.getenv("MONGO_DB") or os.getenv("MONGODB_DB", "seed_bot")
MONGODB_HISTORY_TTL_DAYS = int(os.getenv("MONGODB_HISTORY_TTL_DAYS", "30"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "50"))

COLLECTION_MESSAGES = "dialog_messages"
COLLECTION_PROCESSED_POSTS = "processed_posts"  # защита от дублей ответа
