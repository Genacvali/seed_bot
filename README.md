# Mattermost AI bot (GigaChat)

Бот слушает новые сообщения в Mattermost и отвечает с помощью GigaChat.

## Быстрый старт

1) Установите Python 3.10+ и зависимости:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

2) Создайте файл `.env` по примеру:

```bash
cp .env.example .env
```

3) Запустите:

```bash
python3 -m bot
```

## Переменные окружения

### Mattermost

- `MATTERMOST_URL` — например `https://mm.example.com`
- `MATTERMOST_TOKEN` — токен бота (Personal Access Token)
- `MATTERMOST_BOT_USER_ID` — user_id бота (чтобы не отвечать самому себе)
- (опционально) `MATTERMOST_TEAM` / `MATTERMOST_CHANNEL` или `MATTERMOST_CHANNEL_ID` — чтобы слушать только один канал. Если не заданы — бот слушает все доступные каналы.

### GigaChat

Вариант A (рекомендуется): client id/secret
- `GIGACHAT_CLIENT_ID`
- `GIGACHAT_CLIENT_SECRET`
- `GIGACHAT_SCOPE` (по умолчанию `GIGACHAT_API_PERS`)

Вариант B: готовый ключ для Basic (если вам так выдали)
- `GIGACHAT_AUTH_KEY` — строка после `Basic ` (base64)

Дополнительно:
- `GIGACHAT_MODEL` (по умолчанию `GigaChat`)
- `GIGACHAT_VERIFY_TLS` (`true/false`, по умолчанию `true`)
- `MATTERMOST_WS_VERIFY_TLS` (`true/false`, по умолчанию `true`) — проверка TLS при WebSocket-подключении к Mattermost

## Как пользоваться

- Бот отвечает, когда его упоминают (`@botname`) или если включить режим `REPLY_ALL=true`.
- Ответы отправляются в тред исходного сообщения (если возможно).

