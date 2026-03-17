# Что создать в MongoDB и Qdrant

## MongoDB

**БД**: любая (например `seed_bot`, задаётся в `MONGO_DB`).

### Коллекция `docs`

| Поле       | Тип      | Обязательно |
|-----------|----------|-------------|
| `_id`     | ObjectId | да (авто)   |
| `title`   | string   | да          |
| `content` | string   | да          |
| `source`  | string   | нет         |
| `created_at` | date | нет         |

### Коллекция `cases`

| Поле          | Тип      | Обязательно |
|---------------|----------|-------------|
| `_id`         | ObjectId | да (авто)   |
| `title`       | string   | да          |
| `description` | string   | да          |
| `solution`    | string   | да          |
| `tags`        | array    | нет         |
| `created_at`  | date     | нет         |

Коллекции создаёт скрипт `scripts/setup_rag.py` — вручную создавать не нужно.

---

## Qdrant

**URL**: из `QDRANT_URL` (например `http://localhost:6333`).

### Коллекция (имя из `QDRANT_COLLECTION`, по умолчанию `knowledge`)

| Параметр   | Значение |
|------------|----------|
| Вектор     | размерность = размерность эмбеддингов GigaChat (скрипт узнает сам) |
| Distance   | Cosine   |

**Payload каждой точки** (заполняет скрипт индексации):

| Поле     | Тип    | Описание |
|----------|--------|----------|
| `type`   | string | `"doc"` или `"case"` |
| `doc_id` | string | для документов: `str(_id)` из MongoDB |
| `case_id`| string | для кейсов: `str(_id)` из MongoDB |

Коллекцию с нужной размерностью создаёт скрипт `scripts/setup_rag.py` — вручную создавать не нужно.

---

## Один скрипт

Запуск из корня проекта (должен быть настроен `.env`):

```bash
python scripts/setup_rag.py
```

Скрипт:
1. Создаёт в MongoDB коллекции `docs` и `cases` (если их ещё нет).
2. Узнаёт размерность вектора через GigaChat Embeddings.
3. Создаёт в Qdrant коллекцию `knowledge` (или из `QDRANT_COLLECTION`) с этой размерностью.
4. Если в MongoDB есть документы/кейсы — индексирует их в Qdrant.

Опция только структуры, без индексации:

```bash
python scripts/setup_rag.py --only-create
```
