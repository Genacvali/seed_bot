# RAG-бот для Mattermost (GigaChat + Qdrant + MongoDB)

Бот отвечает на сообщения в Mattermost, используя RAG: запрос эмбеддится (GigaChat Embeddings), по вектору ищется в Qdrant, по id подгружаются документы/кейсы из MongoDB, ответ генерирует GigaChat.

## Схема

```
Mattermost/чат → Python bot → GigaChat Embeddings → Qdrant search → MongoDB docs/cases → GigaChat answer
```

## Быстрый старт

1. Установка зависимостей в `/data/py_libs`:

```bash
bash scripts/install_deps.sh
```

По умолчанию ставит в `/data/py_libs`. Другой каталог: `bash scripts/install_deps.sh /путь/к/папке`.

2. Настройка `.env` (см. `.env.example`):

- **Mattermost**: `MATTERMOST_URL`, `MATTERMOST_TOKEN`, `MATTERMOST_BOT_USER_ID`
- **GigaChat**: `GIGACHAT_CLIENT_ID` + `GIGACHAT_CLIENT_SECRET` (или `GIGACHAT_AUTH_KEY`)
- **MongoDB**: `MONGO_URI`, `MONGO_DB` — коллекции `docs` и `cases`
- **Qdrant**: `QDRANT_URL`, `QDRANT_COLLECTION` (по умолчанию `knowledge`)

3. Создание коллекций в MongoDB и Qdrant + индексация (один скрипт):

```bash
python scripts/setup_rag.py
```

Скрипт создаёт коллекции `docs` и `cases` в MongoDB, коллекцию в Qdrant с нужной размерностью вектора и при наличии данных в MongoDB индексирует их в Qdrant. Подробнее: [docs/SETUP_QUICK.md](docs/SETUP_QUICK.md).

4. Запуск бота (с библиотеками из `/data/py_libs`):

```bash
PYTHONPATH=/data/py_libs python3 -m bot
```

Или через скрипт (подставляет PYTHONPATH сам):

```bash
bash run.sh
```

Скрипт настройки RAG с тем же PYTHONPATH:

```bash
PYTHONPATH=/data/py_libs python3 scripts/setup_rag.py
```

## Схемы данных

Подробно: [docs/SCHEMA.md](docs/SCHEMA.md).

- **MongoDB**: коллекции `docs` (title, content) и `cases` (title, description, solution). По `_id` из payload Qdrant подгружаются полные объекты.
- **Qdrant**: коллекция с векторами (размерность = размерность эмбеддингов GigaChat). Payload: `type` ("doc" | "case"), `doc_id` или `case_id` (строка `str(MongoDB _id)`).

## Переменные окружения

| Переменная | Описание |
|------------|----------|
| MATTERMOST_URL | URL сервера Mattermost |
| MATTERMOST_TOKEN | Токен бота |
| MATTERMOST_BOT_USER_ID | user_id бота |
| GIGACHAT_CLIENT_ID, GIGACHAT_CLIENT_SECRET | OAuth GigaChat (или GIGACHAT_AUTH_KEY) |
| GIGACHAT_MODEL | Модель для ответов (по умолчанию GigaChat) |
| GIGACHAT_EMBEDDINGS_MODEL | Модель эмбеддингов (по умолчанию Embeddings) |
| MONGO_URI, MONGO_DB | MongoDB для docs/cases |
| QDRANT_URL | URL Qdrant (например http://localhost:6333) |
| QDRANT_COLLECTION | Имя коллекции (по умолчанию knowledge) |
| QDRANT_LIMIT | Сколько документов подтягивать (по умолчанию 5) |
| USE_LOCAL_EMBEDDINGS | true = sentence-transformers локально, false = GigaChat Embeddings API |
| LOCAL_EMBEDDINGS_MODEL | Модель для эмбеддингов (по умолчанию paraphrase-multilingual-MiniLM-L12-v2, 384 dim) |

**Важно:** если переключаешься с GigaChat Embeddings на локальные (или меняешь модель), заново создай коллекцию в Qdrant и переиндексируй: удали коллекцию в Qdrant и запусти `python scripts/setup_rag.py`.

## Использование

- В канале/треде напишите сообщение с упоминанием бота (`@botname`) или включите `REPLY_ALL=true`.
- Бот эмбеддит запрос, ищет ближайшие точки в Qdrant, подгружает тексты из MongoDB и формирует ответ через GigaChat по этому контексту.
