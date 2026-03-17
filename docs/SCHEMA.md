# Схемы MongoDB и Qdrant

## Архитектура RAG

```
Mattermost/чат → Python bot → GigaChat Embeddings → Qdrant search → MongoDB docs/cases → GigaChat answer
```

1. Пользователь пишет в Mattermost.
2. Бот эмбеддит запрос (GigaChat Embeddings).
3. Qdrant возвращает ближайшие по вектору точки (payload: `doc_id` / `case_id`).
4. По id загружаются полные документы/кейсы из MongoDB.
5. Контекст + запрос отправляются в GigaChat → ответ в чат.

---

## MongoDB

БД задаётся через `MONGO_DB` (по умолчанию `seed_bot`).

### Коллекция `docs`

Документы для поиска (инструкции, справочники и т.п.).

| Поле      | Тип   | Описание                    |
|----------|--------|-----------------------------|
| `_id`    | ObjectId | Идентификатор (используется в payload Qdrant как `doc_id`) |
| `title`  | string | Заголовок                   |
| `content`| string | Текст документа             |
| `source` | string | (опционально) Источник/URL  |
| `created_at` | date | (опционально)              |

Пример:

```json
{
  "_id": ObjectId("..."),
  "title": "Настройка MongoDB",
  "content": "Подключение к кластеру...",
  "source": "https://confluence.example.com/..."
}
```

### Коллекция `cases`

Кейсы (типовые проблемы и решения).

| Поле          | Тип   | Описание                          |
|---------------|--------|-----------------------------------|
| `_id`         | ObjectId | Идентификатор (в Qdrant — `case_id`) |
| `title`       | string | Заголовок кейса                   |
| `description` | string | Описание проблемы                 |
| `solution`    | string | Решение / шаги                   |
| `tags`        | array  | (опционально) Теги                |
| `created_at`  | date   | (опционально)                     |

Пример:

```json
{
  "_id": ObjectId("..."),
  "title": "MongoDB не стартует",
  "description": "После обновления сервер не поднимается...",
  "solution": "1. Проверить логи... 2. Увеличить память...",
  "tags": ["mongodb", "startup"]
}
```

При индексации в Qdrant для каждой записи из `docs`/`cases` создаётся точка: вектор от эмбеддинга поля `content` (или `title` + `content` + `solution`), в payload — `type: "doc"` и `doc_id: str(_id)` (или `type: "case"`, `case_id`).

---

## Qdrant

- **URL**: `QDRANT_URL` (например `http://localhost:6333`).
- **Коллекция**: `QDRANT_COLLECTION` (по умолчанию `knowledge`).

### Параметры коллекции

- **vector size**: размерность эмбеддингов GigaChat (зависит от модели, например 1024 для Embeddings).
- **distance**: обычно `Cosine`.

### Payload точки

| Поле     | Тип   | Описание |
|----------|--------|----------|
| `type`   | string | `"doc"` или `"case"` |
| `doc_id` | string | Для документов: `str(MongoDB _id)` |
| `case_id`| string | Для кейсов: `str(MongoDB _id)` |

Поиск возвращает точки с этими payload; бот по `doc_id`/`case_id` подгружает полные объекты из MongoDB.

### Создание коллекции и индексация

Коллекцию нужно создать с правильной размерностью вектора (узнать из ответа GigaChat Embeddings для одного тестового запроса). Скрипт индексации (отдельно): читает все документы из MongoDB, получает эмбеддинги, записывает точки в Qdrant.
