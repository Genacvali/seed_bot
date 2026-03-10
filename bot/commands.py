"""
Обработчик команд бота.

Команды:
  @seed help              — список команд
  @seed find <query>      — поиск по базе знаний
  @seed tl;dr             — резюме треда
  @seed oncall            — кто сейчас дежурит
  @seed stats             — топ алертов
  @seed save              — сохранить тред как known issue
  @seed forget            — забыть факт (TODO)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .gigachat import GigaChatClient
    from .mattermost import MattermostClient, PostEvent
    from .storage import Storage

HELP_TEXT = """\
### SEED — команды

| Команда | Что делает |
|---------|-----------|
| `@seed help` | Это сообщение |
| `@seed find <запрос>` | Поиск по базе знаний |
| `@seed tl;dr` | Резюме текущего треда |
| `@seed oncall` | Кто сейчас дежурит |
| `@seed stats` | Топ-10 алертов за всё время |
| `@seed save` | Сохранить тред как known issue |

**Автоматически** (без команды):
- Любой **алерт** → анализ + первые шаги диагностики
- Любой **факт** ("серверы с X — это Y, отвечает @vasya") → запоминает
- В **треде** — отвечает без @упоминания, помнит историю разговора
"""

_CMD_RE = re.compile(
    r"(?:^|\s)"
    r"(?:find|найди|поищи)\s+(.+)",
    re.IGNORECASE,
)


@dataclass
class CommandResult:
    text: str
    react_ok: bool = False  # ставить ✅ на исходный пост?


def handle(
    raw_message: str,
    clean_prompt: str,
    ev: "PostEvent",
    mm: "MattermostClient",
    gc: "GigaChatClient",
    storage: "Storage | None",
    bot_username: str,
) -> CommandResult | None:
    """
    Возвращает CommandResult если команда найдена, иначе None.
    raw_message — оригинальное сообщение с @mentions.
    clean_prompt — stripped mentions.
    """
    p = clean_prompt.strip().lower()

    # help
    if p in ("help", "помощь", "команды", "?"):
        return CommandResult(HELP_TEXT)

    # tl;dr
    if p in ("tl;dr", "tldr", "резюме", "кратко", "summary"):
        return _cmd_tldr(ev, mm, gc)

    # oncall
    if p in ("oncall", "on-call", "дежурный", "кто дежурит", "кто сейчас дежурит"):
        return _cmd_oncall(storage)

    # stats
    if p in ("stats", "статистика", "алерты"):
        return _cmd_stats(storage)

    # find / найди
    m = _CMD_RE.search(clean_prompt)
    if m:
        query = m.group(1).strip()
        return _cmd_find(query, storage)

    # save — сохранить тред как known issue
    if p in ("save", "сохранить", "записать", "save issue"):
        return _cmd_save(ev, mm, gc, storage)

    return None


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

def _cmd_tldr(
    ev: "PostEvent",
    mm: "MattermostClient",
    gc: "GigaChatClient",
) -> CommandResult:
    root_id = ev.root_id or ev.post_id
    try:
        posts = mm.get_thread_posts(root_id)
    except Exception as e:
        return CommandResult(f"Не смог получить тред: {e}")

    if len(posts) <= 2:
        return CommandResult("Тред слишком короткий — нечего резюмировать.")

    lines = []
    bot_uid = mm._cfg.mattermost_bot_user_id
    for p in posts:
        if p.get("user_id") == bot_uid:
            role = "SEED"
        else:
            role = "User"
        msg = (p.get("message") or "").strip()
        if msg:
            lines.append(f"{role}: {msg[:300]}")

    thread_text = "\n".join(lines)
    prompt = f"Сделай краткое резюме этого треда (3-5 предложений, главные факты и решения):\n\n{thread_text}"

    try:
        summary = gc.chat(prompt)
        return CommandResult(f"**TL;DR:**\n{summary}")
    except Exception as e:
        return CommandResult(f"Не смог сделать резюме: {e}")


def _cmd_oncall(storage: "Storage | None") -> CommandResult:
    if not storage:
        return CommandResult("MongoDB не подключён — расписание дежурств недоступно.")

    try:
        now = datetime.now(tz=timezone.utc)
        schedule = storage.get_current_oncall(now)
    except Exception as e:
        return CommandResult(f"Ошибка при получении расписания: {e}")

    if not schedule:
        return CommandResult("Дежурный не назначен. Заполни `on_call_schedule` в MongoDB.")

    user = schedule.get("username") or schedule.get("user_id", "?")
    end = schedule.get("end_at")
    end_str = f" до {end.strftime('%d.%m %H:%M UTC')}" if end else ""
    return CommandResult(f"Сейчас дежурит: @{user}{end_str}")


def _cmd_stats(storage: "Storage | None") -> CommandResult:
    if not storage:
        return CommandResult("MongoDB не подключён — статистика недоступна.")
    try:
        top = storage.top_alerts(limit=10)
    except Exception as e:
        return CommandResult(f"Ошибка при получении статистики: {e}")

    if not top:
        return CommandResult("Статистика алертов пуста.")

    lines = ["**Топ-10 алертов:**", ""]
    for i, a in enumerate(top, 1):
        server = a.get("server", "?")
        alert_type = a.get("alert_type", "?")[:60]
        count = a.get("count", 0)
        last = a.get("last_fired_at")
        last_str = f" | последний: {last.strftime('%d.%m %H:%M')}" if last else ""
        lines.append(f"{i}. `{server}` — {alert_type} — **{count}x**{last_str}")

    return CommandResult("\n".join(lines))


def _cmd_find(query: str, storage: "Storage | None") -> CommandResult:
    if not storage:
        return CommandResult("MongoDB не подключён — поиск по базе знаний недоступен.")
    try:
        facts = storage.search_facts(query, limit=7)
    except Exception as e:
        return CommandResult(f"Ошибка поиска: {e}")

    if not facts:
        return CommandResult(f"По запросу «{query}» ничего не найдено в базе знаний.")

    lines = [f"**Результаты поиска «{query}»:**", ""]
    for f in facts:
        ft = f.get("fact_type", "?")
        summary = f.get("summary", "")
        lines.append(f"- `[{ft}]` {summary}")

    return CommandResult("\n".join(lines))


def _cmd_save(
    ev: "PostEvent",
    mm: "MattermostClient",
    gc: "GigaChatClient",
    storage: "Storage | None",
) -> CommandResult:
    if not storage:
        return CommandResult("MongoDB не подключён — сохранение недоступно.")

    root_id = ev.root_id or ev.post_id
    try:
        posts = mm.get_thread_posts(root_id)
    except Exception as e:
        return CommandResult(f"Не смог получить тред: {e}")

    thread_text = "\n".join(
        (p.get("message") or "").strip()
        for p in posts
        if (p.get("message") or "").strip()
    )

    prompt = (
        "Составь запись known issue из этого треда:\n"
        "- title: краткое название\n"
        "- description: суть проблемы\n"
        "- solution: как решили\n"
        "- tags: список тегов\n\n"
        f"{thread_text}"
    )

    try:
        summary_text = gc.chat(prompt)
    except Exception as e:
        return CommandResult(f"Не смог составить запись: {e}")

    try:
        storage.save_known_issue(
            title=f"Issue from thread {root_id[:8]}",
            description=summary_text,
            tags=["auto-saved"],
            created_by=ev.user_id,
        )
    except Exception as e:
        return CommandResult(f"Не смог сохранить: {e}")

    return CommandResult(
        f"Сохранил как known issue ✅\n\n{summary_text}",
        react_ok=True,
    )
