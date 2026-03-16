"""
Обработчик команд бота.

Команды:
  @seed help              — список команд
  @seed find <query>      — поиск по базе знаний
  @seed tl;dr             — резюме треда
  @seed oncall            — кто сейчас дежурит
  @seed stats             — топ алертов
  @seed save              — сохранить тред как known issue
  @seed server <mask>     — поиск сервера в Confluence
  @seed confluence add <url>    — добавить страницу Confluence
  @seed confluence list         — список добавленных страниц
  @seed confluence remove <url|id> — удалить страницу
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .confluence_client import ConfluenceClient
    from .gigachat import GigaChatClient
    from .mattermost import MattermostClient, PostEvent
    from .storage import Storage

HELP_TEXT = """\
### SEED — команды

| Команда | Что делает |
|---------|-----------|
| `@seed help` | Это сообщение |
| `@seed server <маска>` | Поиск сервера в Confluence (`*`, `?`, `r:regex`) |
| `@seed confluence add <url>` | Добавить страницу Confluence для поиска серверов |
| `@seed confluence list` | Список подключённых страниц Confluence |
| `@seed confluence remove <url/id>` | Отключить страницу Confluence |
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
    r"(?:^|\s)(?:find|найди|поищи)\s+(.+)",
    re.IGNORECASE,
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
    confluence: "ConfluenceClient | None" = None,
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

    # find / найди — роутим в Confluence если запрос похож на сервер или явно упоминает доки
    m = _CMD_RE.search(clean_prompt)
    if m:
        query = m.group(1).strip()
        # Убираем слова-уточнения: "в документации", "в доках", "in docs" и т.д.
        query_clean = re.sub(
            r"\b(?:в\s+документации|в\s+доках?|in\s+docs?|in\s+documentation|из\s+документации)\b",
            "", query, flags=re.IGNORECASE,
        ).strip()
        # Если похоже на хостнейм — идём в Confluence (ищем по имени сервера)
        from .confluence_intent import extract_hostnames
        hostnames = extract_hostnames(query_clean)
        if hostnames and confluence:
            return _cmd_server(hostnames[0], confluence, gc, storage)
        return _cmd_find(query_clean or query, storage, confluence, gc)

    # save — сохранить тред как known issue
    if p in ("save", "сохранить", "записать", "save issue"):
        return _cmd_save(ev, mm, gc, storage)

    # server — поиск в Confluence
    if p.startswith(("server ", "сервер ")):
        query = clean_prompt.strip().split(None, 1)[1].strip() if " " in clean_prompt.strip() else ""
        return _cmd_server(query, confluence, gc, storage)

    # confluence add / list / remove
    if p.startswith("confluence"):
        rest = clean_prompt.strip().split(None, 1)[1].strip() if " " in clean_prompt.strip() else ""
        return _cmd_confluence(rest, ev.user_id, confluence, storage)

    # Естественный запрос о сервере через Confluence
    if confluence and _is_server_query(p):
        query = _extract_server_query(clean_prompt)
        if query:
            return _cmd_server(query, confluence, gc, storage)

    return None


def _get_page_ids(
    confluence: "ConfluenceClient",
    storage: "Storage | None",
) -> list[str]:
    """Возвращает page_ids: сначала из MongoDB, fallback — из _cfg_pages."""
    if storage:
        try:
            ids = storage.get_confluence_page_ids()
            if ids:
                return ids
        except Exception as e:
            print(f"[confluence] get_page_ids from mongo error: {e}", flush=True)
    # fallback: статически заданные в конфиге
    return getattr(confluence, "_cfg_pages", [])


def _cmd_confluence(
    rest: str,
    user_id: str,
    confluence: "ConfluenceClient | None",
    storage: "Storage | None",
) -> CommandResult:
    """
    Управление страницами Confluence.
      confluence add <url>
      confluence list
      confluence remove <url|page_id>
    """
    if not confluence:
        return CommandResult(
            "Confluence не подключён — добавь `CONFLUENCE_URL` и `CONFLUENCE_TOKEN` в `.env`."
        )

    parts = rest.strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""

    # ── list ──────────────────────────────────────────────────────────
    if sub in ("list", "список", "ls", ""):
        if not storage:
            ids = getattr(confluence, "_cfg_pages", [])
            if ids:
                return CommandResult(
                    "MongoDB не подключён. Страницы из конфига:\n"
                    + "\n".join(f"- `{i}`" for i in ids)
                )
            return CommandResult("Нет подключённых страниц. MongoDB не подключён.")

        pages = storage.list_confluence_pages()
        if not pages:
            return CommandResult(
                "Нет добавленных страниц Confluence.\n"
                "Добавь: `@seed confluence add <url>`"
            )
        lines = ["**Страницы Confluence для поиска серверов:**\n"]
        for p in pages:
            lines.append(
                f"- `{p['page_id']}` | **{p['title']}** ({p['space_key']}) — {p['url']}"
            )
        return CommandResult("\n".join(lines))

    # ── add ───────────────────────────────────────────────────────────
    if sub in ("add", "добавить", "добавь"):
        if not arg:
            return CommandResult(
                "Укажи URL страницы:\n"
                "`@seed confluence add https://confluence.example.com/display/SPACE/Page`"
            )
        try:
            info = confluence.resolve_page_url(arg)
        except Exception as e:
            return CommandResult(f"Не смог получить страницу: `{e}`")

        page_id = info["page_id"]
        title = info["title"]
        space = info["space_key"]

        if not storage:
            # Без MongoDB — добавляем только в память
            cfg_pages: list[str] = getattr(confluence, "_cfg_pages", [])
            if page_id not in cfg_pages:
                cfg_pages.append(page_id)
                confluence._cfg_pages = cfg_pages  # type: ignore[attr-defined]
                return CommandResult(
                    f"Добавил `{title}` (`{page_id}`) в список поиска.\n"
                    "⚠️ MongoDB не подключён — после перезапуска бота придётся добавить снова."
                )
            return CommandResult(f"Страница `{title}` уже в списке.")

        is_new = storage.add_confluence_page(
            page_id=page_id,
            title=title,
            space_key=space,
            url=arg,
            added_by=user_id,
        )
        if is_new:
            return CommandResult(
                f"✅ Добавил в поиск:\n"
                f"**{title}** (`{space}` / `{page_id}`)\n"
                f"{arg}"
            )
        return CommandResult(
            f"Страница **{title}** уже была добавлена. Данные обновлены."
        )

    # ── remove ────────────────────────────────────────────────────────
    if sub in ("remove", "delete", "rm", "удалить", "удали"):
        if not arg:
            return CommandResult(
                "Укажи URL или page_id:\n"
                "`@seed confluence remove https://...` или `@seed confluence remove 277910996`"
            )
        # Если это число — используем как page_id напрямую
        if re.match(r"^\d+$", arg):
            page_id = arg
        else:
            # Резолвим URL
            try:
                info = confluence.resolve_page_url(arg)
                page_id = info["page_id"]
            except Exception as e:
                return CommandResult(f"Не смог получить страницу: `{e}`")

        if not storage:
            cfg_pages = getattr(confluence, "_cfg_pages", [])
            if page_id in cfg_pages:
                cfg_pages.remove(page_id)
                return CommandResult(f"Удалил `{page_id}` из списка.")
            return CommandResult(f"Страница `{page_id}` не найдена в списке.")

        removed = storage.remove_confluence_page(page_id)
        if removed:
            return CommandResult(f"✅ Страница `{page_id}` удалена из поиска.")
        return CommandResult(f"Страница `{page_id}` не найдена в базе.")

    return CommandResult(
        f"Неизвестная подкоманда: `{sub}`\n"
        "Доступно: `add`, `list`, `remove`"
    )


_SERVER_QUERY_PATTERNS = re.compile(
    r"(?:расскажи|инфо|информация|что знаешь|покажи|найди|где|на каком|кластер|"
    r"tell me about|info about|show|find|which cluster|environment|env)\s+"
    r"(?:про\s+|о\s+|об\s+|сервер[еа]?\s+|по\s+)?([a-z0-9][a-z0-9\-_.]+)",
    re.IGNORECASE,
)


def _is_server_query(prompt: str) -> bool:
    """Грубая эвристика: содержит ли запрос имя сервера/маску?"""
    return bool(_SERVER_QUERY_PATTERNS.search(prompt))


def _extract_server_query(prompt: str) -> str:
    m = _SERVER_QUERY_PATTERNS.search(prompt)
    return m.group(1) if m else ""


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


def _cmd_find(
    query: str,
    storage: "Storage | None",
    confluence: "ConfluenceClient | None" = None,
    gc: "GigaChatClient | None" = None,
) -> CommandResult:
    results: list[str] = []

    # 1. Поиск в MongoDB (факты из диалогов)
    if storage:
        try:
            facts = storage.search_facts(query, limit=7)
            if facts:
                results.append(f"**Из базы знаний по «{query}»:**")
                for f in facts:
                    ft = f.get("fact_type", "?")
                    summary = f.get("summary", "")
                    results.append(f"- `[{ft}]` {summary}")
        except Exception as e:
            results.append(f"_Ошибка поиска в базе знаний: {e}_")

    # 2. Поиск в Confluence
    if confluence:
        page_ids = _get_page_ids(confluence, storage)
        if page_ids:
            all_records = []
            for page_id in page_ids:
                try:
                    found = confluence.search_all_children(page_id, query)
                    all_records.extend(found)
                except Exception as e:
                    print(f"[confluence] find error page={page_id}: {e}", flush=True)

            # Дедупликация
            seen: set[str] = set()
            unique: list = []
            for rec in all_records:
                if rec.server.lower() not in seen:
                    seen.add(rec.server.lower())
                    unique.append(rec)

            if unique:
                results.append(f"\n**Из Confluence по «{query}»:**")
                results.append(f"Нашёл {len(unique)} сервер(ов):\n")
                for rec in unique[:10]:
                    line = f"- `{rec.short_name()}` | {rec.env} | {rec.ip} | {rec.version} | ЦОД {rec.dc}"
                    results.append(line)
                if len(unique) > 10:
                    results.append(f"_…и ещё {len(unique) - 10}. Уточни запрос._")

    if not results:
        hint = ""
        if not storage and not confluence:
            hint = "\n_MongoDB и Confluence не подключены._"
        elif not storage:
            hint = "\n_MongoDB не подключён — факты из диалогов не сохраняются._"
        return CommandResult(f"По запросу «{query}» ничего не найдено.{hint}")

    return CommandResult("\n".join(results))


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


# ---------------------------------------------------------------------------
# server — поиск в Confluence
# ---------------------------------------------------------------------------

_MAX_RESULTS = 10  # не заспамить чат


def _cmd_server(
    query: str,
    confluence: "ConfluenceClient | None",
    gc: "GigaChatClient",
    storage: "Storage | None" = None,
) -> CommandResult:
    if not confluence:
        return CommandResult(
            "Confluence не подключён. Добавь в `.env`:\n"
            "```\nCONFLUENCE_URL=https://confluence.example.com\n"
            "CONFLUENCE_TOKEN=...\n```\n"
            "Затем: `@seed confluence add <url>`"
        )
    if not query:
        return CommandResult(
            "Укажи маску: `@seed server p-*-mng-*` или `@seed server db-prod-01`"
        )

    page_ids = _get_page_ids(confluence, storage)
    if not page_ids:
        return CommandResult(
            "Ни одной страницы Confluence не добавлено.\n"
            "Используй: `@seed confluence add https://confluence.example.com/display/SPACE/Page`"
        )

    # Поиск по всем настроенным страницам
    all_records = []
    for page_id in page_ids:
        try:
            found = confluence.search_all_children(page_id, query)
            all_records.extend(found)
        except Exception as e:
            print(f"[confluence] search error page={page_id}: {e}", flush=True)

    if not all_records:
        return CommandResult(
            f"По маске `{query}` ничего не нашёл в Confluence.\n"
            "Попробуй другую маску или `r:regex`."
        )

    # Дедупликация по server
    seen: set[str] = set()
    unique = []
    for r in all_records:
        key = r.server.lower()
        if key not in seen:
            seen.add(key)
            unique.append(r)

    total = len(unique)
    shown = unique[:_MAX_RESULTS]

    if total == 1:
        # Один результат — полная карточка
        return CommandResult(shown[0].to_markdown())

    # Несколько — таблица + LLM-комментарий
    lines = [f"Нашёл **{total}** сервер(ов) по `{query}`:\n"]
    lines.append("| Сервер | Среда | IP | Версия | ЦОД |")
    lines.append("|--------|-------|-----|--------|-----|")
    for rec in shown:
        lines.append(
            f"| `{rec.server}` | {rec.env} | {rec.ip} | {rec.version} | {rec.dc} |"
        )
    if total > _MAX_RESULTS:
        lines.append(f"\n_…и ещё {total - _MAX_RESULTS}. Уточни маску._")

    # Если точный хит — добавить полную карточку
    exact = [r for r in shown if r.server.lower() == query.lower()]
    if exact:
        lines.append(f"\n---\n**Точное совпадение:**\n{exact[0].to_markdown()}")

    return CommandResult("\n".join(lines))
