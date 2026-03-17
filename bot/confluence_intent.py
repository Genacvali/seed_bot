"""
Определяет намерения пользователя связанные с Confluence:
  - Ссылка на документацию → автоиндексирование
  - Вопрос «какие доки есть?» → список страниц
  - Вопрос о сервере → поиск в Confluence + контекст для LLM
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .confluence_client import ConfluenceClient, ServerRecord
    from .storage import Storage


# ------------------------------------------------------------------
# Паттерны для определения намерений
# ------------------------------------------------------------------

# Confluence URL: /display/SPACE/Title или ?pageId=... или /wiki/spaces/...
# Исключаем Markdown-символы []() из URL-захвата
_CONFLUENCE_URL_RE = re.compile(
    r"https?://[^\s\"'<>\[\]()]+/"
    r"(?:display/[A-Z][^/\s\[\]()]+/[^\s\"'<>\[\]()#?]+|"
    r"pages/viewpage\.action\?pageId=\d+|"
    r"wiki/spaces/[^\s\[\]()/]+/pages/\d+)",
    re.IGNORECASE,
)

# Markdown-ссылка: [текст](url) или [url](url)
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)]+)\)")

# Запросы о доступных документациях (общий список)
_DOCS_LIST_RE = re.compile(
    r"(?:"
    r"какие.{0,20}(?:документаци|доки|страниц|базы|источник)|"
    r"что.{0,15}(?:у тебя|в базе|знаешь|подключен|добавлен)|"
    r"(?:покажи|список|перечисли).{0,25}(?:документаци|страниц|источник|базы|доков|документов)|"
    r"(?:документаци|доки|страниц).{0,20}(?:есть|добавлен|подключен|в базе)|"
    r"какую.{0,20}документаци|"
    r"what.{0,15}(?:docs|documentation|pages|sources)|"
    r"list.{0,15}(?:docs|pages|sources)"
    r")",
    re.IGNORECASE,
)

# Вопрос «есть ли у тебя документация / доки по X?»
# Примеры: "у тебя есть документация по монге?", "есть ли доки по mongodb?",
#          "а дай ссылку на доки", "есть документация по postgres?"
_DOCS_TOPIC_RE = re.compile(
    r"(?:"
    r"(?:у\s+тебя|у\s+вас)\s+есть\s+(?:документаци|доки|страниц)|"
    r"есть\s+(?:ли\s+)?(?:у\s+тебя\s+)?(?:документаци|доки)|"
    r"(?:есть|имеется)\s+(?:документаци|доки)|"
    r"(?:дай|скинь|дайте).{0,15}(?:ссылку|доки|документаци).{0,15}(?:на|по|про)"
    r")",
    re.IGNORECASE,
)

# Извлечение ключевого слова темы: "по монге" → "монге", "про mongodb" → "mongodb"
_DOCS_TOPIC_KW_RE = re.compile(
    r"(?:по|про|для|о\b|об\b|на)\s+([а-яёa-z][а-яёa-z0-9\-_]{1,40})",
    re.IGNORECASE,
)

# Типичные имена хостов — поддерживает короткие и FQDN
# Примеры: p-smi-mng-sc-msk01, p-smi-mng-sc-msk01.sberdevices.ru, db-prod-01
_HOSTNAME_RE = re.compile(
    r"\b([a-z][a-z0-9]{0,20}(?:-[a-z0-9]{1,20}){1,8}(?:\.[a-z][a-z0-9\-.]{2,50})?)\b",
    re.IGNORECASE,
)

# Стоп-слова — не являются хостнеймами даже если подходят по паттерну
_HOSTNAME_STOPWORDS = {
    "tl-dr", "ci-cd", "re-check", "re-run", "re-start", "roll-back",
    "step-by-step", "out-of", "up-to-date", "hands-on",
}


# ------------------------------------------------------------------
# Публичный интерфейс
# ------------------------------------------------------------------

class IntentType(str, Enum):
    CONFLUENCE_URL   = "confluence_url"    # сообщение содержит ссылку
    DOCS_LIST        = "docs_list"         # «какие доки есть?»
    DOCS_TOPIC_SEARCH = "docs_topic_search"  # «есть ли у тебя документация по X?»
    SERVER_LOOKUP    = "server_lookup"     # вопрос о конкретном сервере
    NONE             = "none"


@dataclass
class ConfluenceIntent:
    intent: IntentType
    # для CONFLUENCE_URL
    urls: list[str] | None = None
    # для SERVER_LOOKUP
    hostnames: list[str] | None = None
    # для DOCS_TOPIC_SEARCH — ключевое слово темы (может быть None если не извлечено)
    topic_keyword: str | None = None


def detect(message: str) -> ConfluenceIntent:
    """Определяет намерение пользователя связанное с Confluence."""
    urls = find_confluence_urls(message)
    if urls:
        return ConfluenceIntent(intent=IntentType.CONFLUENCE_URL, urls=urls)

    if _DOCS_LIST_RE.search(message):
        return ConfluenceIntent(intent=IntentType.DOCS_LIST)

    if _DOCS_TOPIC_RE.search(message):
        kw_match = _DOCS_TOPIC_KW_RE.search(message)
        kw = kw_match.group(1).lower() if kw_match else None
        return ConfluenceIntent(intent=IntentType.DOCS_TOPIC_SEARCH, topic_keyword=kw)

    hostnames = extract_hostnames(message)
    if hostnames:
        return ConfluenceIntent(intent=IntentType.SERVER_LOOKUP, hostnames=hostnames)

    return ConfluenceIntent(intent=IntentType.NONE)


def find_confluence_urls(text: str) -> list[str]:
    """
    Извлекает Confluence URL из текста.
    Поддерживает:
      - Чистые URL: https://confluence.example.com/display/SPACE/Title
      - Markdown-ссылки: [текст](url) или [url](url)
      - Mattermost auto-link: <url>
    """
    urls: list[str] = []

    # 1. Markdown [text](url) — берём url из скобок
    for m in _MARKDOWN_LINK_RE.finditer(text):
        url = m.group(2).strip()
        if _CONFLUENCE_URL_RE.search(url) or _is_confluence_url(url):
            urls.append(url)

    # 2. Чистые URL (не в Markdown)
    # Убираем Markdown-ссылки перед поиском чтобы не задваивать
    text_no_md = _MARKDOWN_LINK_RE.sub(" ", text)
    for url in _CONFLUENCE_URL_RE.findall(text_no_md):
        if url not in urls:
            urls.append(url)

    return urls


def _is_confluence_url(url: str) -> bool:
    """Проверяет что URL выглядит как Confluence-страница (без строгого regex)."""
    return bool(re.search(r"/display/|/wiki/spaces/|pageId=", url, re.IGNORECASE))


def extract_hostnames(text: str) -> list[str]:
    """
    Извлекает потенциальные хостнеймы из текста.
    Поддерживает FQDN: p-smi-mng-sc-msk01.sberdevices.ru → возвращает 'p-smi-mng-sc-msk01'.
    Требует минимум один дефис (признак инфра-хостнейма).
    """
    found = []
    for m in _HOSTNAME_RE.finditer(text):
        name = m.group(1).lower()
        # Если это FQDN — берём только первую часть (до точки)
        short = name.split(".")[0]
        if short in _HOSTNAME_STOPWORDS:
            continue
        if "-" not in short:
            continue
        if len(short) < 5 or len(short) > 63:
            continue
        found.append(short)
    return list(dict.fromkeys(found))  # deduplicate, preserve order


# ------------------------------------------------------------------
# Форматирование ответов
# ------------------------------------------------------------------

def format_docs_list(pages: list[dict[str, Any]]) -> str:
    if not pages:
        return (
            "В базе пока нет ни одной страницы документации.\n"
            "Скинь ссылку — я проиндексирую, например:\n"
            "> @seed вот документация по MongoDB https://confluence.example.com/display/CDB/MongoDB"
        )
    lines = ["Вот что у меня проиндексировано:\n"]
    for p in pages:
        title = p.get("title", "—")
        space = p.get("space_key", "")
        url = p.get("url", "")
        lines.append(f"- **{title}** ({space}) — {url}")
    lines.append(
        "\nЧтобы добавить ещё — просто скинь ссылку на страницу."
    )
    return "\n".join(lines)


def format_server_not_found(hostnames: list[str]) -> str:
    names = ", ".join(f"`{h}`" for h in hostnames)
    return (
        f"Поискал в документации — не нашёл ничего по {names}.\n"
        "Возможно, страница ещё не добавлена. Скинь ссылку на документацию — проиндексирую."
    )


def format_indexed_page(title: str, page_id: str, url: str, is_new: bool) -> str:
    if is_new:
        return (
            f"Проиндексировал 📄 **{title}** (`{page_id}`).\n"
            f"Теперь могу искать по этой документации — просто спрашивай про серверы."
        )
    link = f"[{title}]({url})" if url else f"**{title}**"
    return f"Страница {link} уже у меня в базе."


def format_docs_topic_found(pages: list[dict[str, Any]], keyword: str | None) -> str:
    """Ответ когда нашли страницы по ключевому слову."""
    topic = f" по «{keyword}»" if keyword else ""
    lines = [f"Нашёл в базе документацию{topic}:\n"]
    for p in pages:
        title = p.get("title", "—")
        url = p.get("url", "")
        space = p.get("space_key", "")
        if url:
            lines.append(f"- [{title}]({url})" + (f" ({space})" if space else ""))
        else:
            lines.append(f"- **{title}**" + (f" ({space})" if space else ""))
    return "\n".join(lines)


def format_docs_topic_not_found(keyword: str | None) -> str:
    """Ответ когда по ключевому слову ничего не нашли."""
    topic = f" по «{keyword}»" if keyword else ""
    return (
        f"Не нашёл в своей базе документацию{topic}.\n"
        "Скинь ссылку — проиндексирую, например:\n"
        f"> @seed вот документация https://confluence.example.com/display/SPACE/Page"
    )


def _cluster_base(short_hostname: str) -> str:
    """p-smi-mng-sc-msk01 → p-smi-mng-sc (база для группировки кластера)."""
    parts = short_hostname.split("-")
    if len(parts) <= 1:
        return short_hostname
    return "-".join(parts[:-1])


def _cluster_key(rec: "ServerRecord") -> tuple[str, str, str]:
    return (rec.env or "", rec.dc or "", _cluster_base(rec.short_name()))


def build_server_context(
    records: "list[ServerRecord]",
    query: str,
    all_records: "list[ServerRecord] | None" = None,
    contacts_by_page: "dict[str, list[str]] | None" = None,
) -> str:
    """
    Формирует текстовый контекст из записей ServerRecord для инжекции в LLM.
    all_records — все найденные записи (для определения кластера); если None, используется records.
    contacts_by_page — page_id -> список имён контактов со страницы.
    """
    if not records:
        return ""

    pool = all_records if all_records is not None else records
    by_cluster: dict[tuple[str, str, str], list["ServerRecord"]] = {}
    for rec in pool:
        key = _cluster_key(rec)
        by_cluster.setdefault(key, []).append(rec)

    lines = [
        f"## Данные из документации Confluence по запросу «{query}»",
        "",
        "ВАЖНО: имена серверов — полные, не сокращай их. "
        "Пиши `p-smi-mng-sc-msk01`, а НЕ «msk01» или «первый».",
        "",
    ]
    for rec in records[:8]:
        short = rec.short_name()
        lines.append(f"### {short}  ({rec.server})")
        if rec.env:
            lines.append(f"- Среда: {rec.env}")
        if rec.ip:
            lines.append(f"- IP: {rec.ip}")
        if rec.os:
            lines.append(f"- ОС: {rec.os}")
        if rec.version:
            lines.append(f"- Версия БД: {rec.version}")
        if rec.dc:
            lines.append(f"- ЦОД: {rec.dc}")

        # Кластер: берём все ноды с тем же (env, dc, prefix) из полного списка страницы
        key = _cluster_key(rec)
        cluster_nodes = by_cluster.get(key, [])
        if len(cluster_nodes) > 1:
            # Сортируем, выводим полные короткие имена
            node_names = sorted(r.short_name() for r in cluster_nodes)
            other_names = [n for n in node_names if n != short]
            lines.append(
                f"- Кластер (replica set): ДА — {len(cluster_nodes)} ноды: "
                f"{', '.join(node_names)}"
            )
            if other_names:
                lines.append(
                    f"  Остальные ноды кластера: {', '.join(other_names)}"
                )
        else:
            lines.append("- Кластер: нет данных о других нодах с таким же префиксом")

        if rec.memory:
            lines.append(f"- Память: {rec.memory}")
        if rec.cpu:
            lines.append(f"- CPU: {rec.cpu}")
        if rec.network:
            lines.append(f"- Сеть: {rec.network}")
        if rec.extra:
            for k, v in rec.extra.items():
                if v:
                    lines.append(f"- {k}: {v}")
        # Ответственные: выводим если найдены, иначе — не упоминаем
        if contacts_by_page is not None:
            contacts = contacts_by_page.get(rec.page_id or "", [])
            if contacts:
                lines.append(f"- Ответственные: {', '.join(contacts)}")

        # Ссылка на страницу документации
        if rec.page_url:
            title_label = rec.page_title or rec.page_id or "документация"
            lines.append(f"- Документация: [{title_label}]({rec.page_url})")
        elif rec.page_title:
            lines.append(f"- Источник: {rec.page_title}")
        lines.append("")

    if len(records) > 8:
        lines.append(f"_(и ещё {len(records) - 8} записей)_")

    # Напоминание: в ответе вывести ссылку из строк «Документация:» выше
    if any(getattr(r, "page_url", None) for r in records[:8]):
        lines.append("")
        lines.append("В конце ответа обязательно выведи ссылку: Подробнее: [название](url) — скопируй из строки «Документация:» выше.")

    return "\n".join(lines)
