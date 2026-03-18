# Seed Bot — бот для Mattermost

Бот отвечает в Mattermost через GigaChat и хранит историю диалогов и контекст в MongoDB (по тредам).

## Возможности

- **Mattermost** — реагирует на сообщения (по умолчанию только при упоминании `@seed` или в треде, где уже отвечал)
- **GigaChat** — генерация ответов
- **MongoDB** — хранение истории по каналу/треду с TTL 30 дней, контекст подставляется в запрос к модели

## Требования

- Python 3.10+
- MongoDB (например: `MONGODB_URI=mongodb://localhost:27017`)
- Учётные данные Mattermost и GigaChat в `.env`

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Заполни .env (токены Mattermost, GigaChat, MONGODB_URI)
```

## Запуск

```bash
python bot.py
```

Индексы MongoDB (включая TTL по `created_at`) создаются при старте.

## Структура БД (MongoDB)

- **База**: `seed_bot` (или `MONGODB_DB`)
- **Коллекция**: `dialog_messages`
  - `channel_id`, `thread_id`, `user_id`, `role` (user/assistant), `content`, `created_at`
  - TTL: документы удаляются через `MONGODB_HISTORY_TTL_DAYS` дней

## Переменные окружения

См. `.env.example`: `MATTERMOST_*`, `GIGACHAT_*`, `MONGODB_URI`, `MONGODB_DB`, `MONGODB_HISTORY_TTL_DAYS`, `HISTORY_LIMIT`, `MENTION_REQUIRED`.
