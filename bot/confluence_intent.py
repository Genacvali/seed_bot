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
_CONFLUENCE_URL_RE = re.compile(
    r"https?://[^\s\"'<>]+/"
    r"(?:display/[A-Z][^/\s]+/[^\s\"'<>?#]+|"
    r"pages/viewpage\.action\?pageId=\d+|"
    r"wiki/spaces/[^/\s]+/pages/\d+)",
    re.IGNORECASE,
)

# Запросы о доступных документациях
_DOCS_LIST_RE = re.compile(
    r"(?:"
    r"какие.{0,20}(?:документаци|доки|страниц|базы|источник)|"
    r"что.{0,15}(?:у тебя|в базе|знаешь|подключен|добавлен)|"
    r"(?:покажи|список|перечисли).{0,25}(?:документаци|страниц|источник|базы|доков|документов)|"
    r"(?:документаци|доки|страниц).{0,20}(?:есть|добавлен|подключен|в базе)|"
    r"какую.{0,20}документаци|"
    r"есть ли.{0,20}документаци|"
    r"what.{0,15}(?:docs|documentation|pages|sources)|"
    r"list.{0,15}(?:docs|pages|sources)"
    r")",
    re.IGNORECASE,
)

# Типичные имена хостов (минимум одно тире → инфра-хостнейм)
# Например: p-ihubcs-mng-adv-msk12, db-prod-01, t-core-pg-02
# Первый сегмент может быть 1 символ (p-, t-, d- — префиксы prod/test/dev)
_HOSTNAME_RE = re.compile(
    r"\b([a-z][a-z0-9]{0,20}(?:-[a-z0-9]{1,20}){1,8})\b",
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
    SERVER_LOOKUP    = "server_lookup"     # вопрос о конкретном сервере
    NONE             = "none"


@dataclass
class ConfluenceIntent:
    intent: IntentType
    # для CONFLUENCE_URL
    urls: list[str] | None = None
    # для SERVER_LOOKUP
    hostnames: list[str] | None = None


def detect(message: str) -> ConfluenceIntent:
    """Определяет намерение пользователя связанное с Confluence."""
    urls = find_confluence_urls(message)
    if urls:
        return ConfluenceIntent(intent=IntentType.CONFLUENCE_URL, urls=urls)

    if _DOCS_LIST_RE.search(message):
        return ConfluenceIntent(intent=IntentType.DOCS_LIST)

    hostnames = extract_hostnames(message)
    if hostnames:
        return ConfluenceIntent(intent=IntentType.SERVER_LOOKUP, hostnames=hostnames)

    return ConfluenceIntent(intent=IntentType.NONE)


def find_confluence_urls(text: str) -> list[str]:
    return _CONFLUENCE_URL_RE.findall(text)


def extract_hostnames(text: str) -> list[str]:
    """Извлекает потенциальные хостнеймы из текста (минимум 1 дефис)."""
    found = []
    for m in _HOSTNAME_RE.finditer(text):
        name = m.group(1).lower()
        if name in _HOSTNAME_STOPWORDS:
            continue
        if "-" not in name:
            continue
        # Отсеиваем слишком короткие и слишком длинные
        if len(name) < 5 or len(name) > 60:
            continue
        found.append(name)
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
    return (
        f"Страница **{title}** уже была у меня. Обновил данные."
    )


def build_server_context(records: "list[ServerRecord]", query: str) -> str:
    """
    Формирует текстовый контекст из записей ServerRecord для инжекции в LLM.
    """
    if not records:
        return ""

    lines = [
        f"## Данные из документации Confluence по запросу «{query}»\n"
    ]
    for rec in records[:5]:  # не перегружаем контекст
        lines.append(f"### {rec.server}")
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
        if rec.page_title:
            lines.append(f"- Источник: {rec.page_title}")
        lines.append("")

    if len(records) > 5:
        lines.append(f"_(и ещё {len(records) - 5} записей)_")

    return "\n".join(lines)
