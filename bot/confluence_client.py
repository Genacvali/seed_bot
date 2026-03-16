"""
Confluence REST API клиент.

Умеет:
- Получать страницу по ID или title+spaceKey
- Резолвить URL страницы в page_id (display/SPACE/Title, viewpage.action?pageId=...)
- Парсить таблицы из storage-формата (HTML/XML)
- Искать серверы по точному имени или маске (wildcard/regex)
- Возвращать структурированные данные о сервере

Пример таблицы Confluence (MongoDB Documentation):

| Среда | Сервер | IP | ОС | Память | CPU | Version | ЦОД | Сеть |
|-------|--------|----|----|--------|-----|---------|-----|------|
| ПРОД  | p-xx-mng-xx | 10.x.x.x | SberLinux 8 | 16GB | 4 | 6.0.20 | SK | iAz |
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


def _short_hostname(fqdn: str) -> str:
    """p-smi-mng-sc-msk01.sberdevices.ru → p-smi-mng-sc-msk01"""
    return fqdn.split(".")[0].lower()


@dataclass
class ServerRecord:
    """Одна строка из таблицы серверов."""
    env: str = ""
    server: str = ""
    ip: str = ""
    os: str = ""
    memory: str = ""
    cpu: str = ""
    version: str = ""
    dc: str = ""
    network: str = ""
    # оригинальные заголовки колонок и значения (для неизвестных полей)
    extra: dict[str, str] = field(default_factory=dict)
    # источник
    page_title: str = ""
    page_id: str = ""

    def short_name(self) -> str:
        """Возвращает короткое имя хоста (без домена)."""
        return _short_hostname(self.server) if self.server else ""

    def to_markdown(self) -> str:
        lines = []
        if self.server:
            short = self.short_name()
            if short and short != self.server.lower():
                lines.append(f"**Сервер:** `{short}` (`{self.server}`)")
            else:
                lines.append(f"**Сервер:** `{self.server}`")
        if self.env:
            lines.append(f"**Среда:** {self.env}")
        if self.ip:
            lines.append(f"**IP:** `{self.ip}`")
        if self.os:
            lines.append(f"**ОС:** {self.os}")
        if self.memory:
            lines.append(f"**Память:** {self.memory}")
        if self.cpu:
            lines.append(f"**CPU:** {self.cpu}")
        if self.version:
            lines.append(f"**Версия:** {self.version}")
        if self.dc:
            lines.append(f"**ЦОД:** {self.dc}")
        if self.network:
            lines.append(f"**Сеть:** {self.network}")
        for k, v in self.extra.items():
            if v:
                lines.append(f"**{k}:** {v}")
        if self.page_title:
            lines.append(f"*Источник: {self.page_title}*")
        return "\n".join(lines)


# Маппинг заголовков колонок (рус/анг → поле ServerRecord)
_COLUMN_MAP: dict[str, str] = {
    "среда": "env",
    "environment": "env",
    "env": "env",
    "сервер": "server",
    "server": "server",
    "hostname": "server",
    "host": "server",
    "хост": "server",
    "ip": "ip",
    "ip адрес": "ip",
    "ip-адрес": "ip",
    "addr": "ip",
    "address": "ip",
    "ос": "os",
    "os": "os",
    "операционная система": "os",
    "память": "memory",
    "memory": "memory",
    "ram": "memory",
    "cpu": "cpu",
    "процессор": "cpu",
    "cores": "cpu",
    "version": "version",
    "versions": "version",
    "версия": "version",
    "ver": "version",
    "vesion": "version",    # опечатка в таблице — намеренно
    "verson": "version",    # ещё один вариант опечатки
    "цод": "dc",
    "dc": "dc",
    "дата-центр": "dc",
    "datacenter": "dc",
    "data center": "dc",
    "сеть": "network",
    "network": "network",
    "net": "network",
    "vlan": "network",
    "cluster": "extra",     # если есть колонка cluster — в extra
}


class ConfluenceClient:
    def __init__(
        self,
        url: str,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        verify_tls: bool = True,
    ) -> None:
        self._base = url.rstrip("/")
        self._session = requests.Session()
        self._session.verify = verify_tls
        if token:
            self._session.headers["Authorization"] = f"Bearer {token}"
        elif username and password:
            self._session.auth = (username, password)

        # Кеш: page_id -> list[ServerRecord]
        self._cache: dict[str, list[ServerRecord]] = {}
        # Кеш raw body: page_id -> html string
        self._body_cache: dict[str, str] = {}
        # Кеш контактов со страницы: page_id -> list[str] (имена)
        self._contacts_cache: dict[str, list[str]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_page(self, page_id: str, force_refresh: bool = False) -> dict[str, Any]:
        """Возвращает сырой JSON страницы с body.storage и version."""
        r = self._session.get(
            f"{self._base}/rest/api/content/{page_id}",
            params={"expand": "body.storage,version,title"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def get_page_by_title(self, space_key: str, title: str) -> dict[str, Any] | None:
        r = self._session.get(
            f"{self._base}/rest/api/content",
            params={"spaceKey": space_key, "title": title, "expand": "version"},
            timeout=30,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        return results[0] if results else None

    def get_child_pages(self, page_id: str) -> list[dict[str, Any]]:
        """Возвращает дочерние страницы."""
        r = self._session.get(
            f"{self._base}/rest/api/content/{page_id}/child/page",
            params={"limit": 50, "expand": "version"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get("results", [])

    def parse_servers(self, page_id: str, force_refresh: bool = False) -> list[ServerRecord]:
        """
        Загружает страницу и парсит все таблицы на ней.
        Возвращает список ServerRecord. Кеширует результат.
        """
        if not force_refresh and page_id in self._cache:
            return self._cache[page_id]

        page = self.get_page(page_id)
        title = page.get("title", "")
        body = page.get("body", {}).get("storage", {}).get("value", "")
        self._body_cache[page_id] = body

        records = _parse_tables(body, page_title=title, page_id=page_id)
        self._cache[page_id] = records
        self._contacts_cache[page_id] = _parse_contacts(body)
        return records

    def search_servers(
        self,
        page_id: str,
        query: str,
        force_refresh: bool = False,
    ) -> list[ServerRecord]:
        """
        Ищет серверы по маске или подстроке.

        Поддерживает:
        - Точное совпадение: `p-ihubcs-mng-adv-msk12`
        - Подстрока: `mng-adv`
        - Wildcard: `p-ihubcs-mng-*`
        - Regex: `r:p-.*-mng-.*`  (префикс r:)
        """
        records = self.parse_servers(page_id, force_refresh=force_refresh)
        return _filter_records(records, query)

    def search_all_children(
        self,
        parent_page_id: str,
        query: str,
    ) -> list[ServerRecord]:
        """
        Ищет по всем дочерним страницам + самой родительской.
        Полезно когда каждый кластер — отдельная дочерняя страница.
        """
        results: list[ServerRecord] = []

        # Сама родительская
        try:
            results.extend(self.search_servers(parent_page_id, query))
        except Exception as e:
            print(f"[confluence] parent page error: {e}", flush=True)

        # Дочерние
        try:
            children = self.get_child_pages(parent_page_id)
        except Exception as e:
            print(f"[confluence] get_child_pages error: {e}", flush=True)
            return results

        for child in children:
            child_id = child.get("id")
            if not child_id:
                continue
            try:
                found = self.search_servers(child_id, query)
                results.extend(found)
            except Exception as e:
                print(f"[confluence] child {child_id} error: {e}", flush=True)

        return results

    def resolve_page_url(self, url: str) -> dict[str, str]:
        """
        Принимает любую ссылку на страницу Confluence и возвращает
        {"page_id": "...", "title": "...", "space_key": "...", "url": "..."}

        Поддерживает форматы:
          https://conf.example.com/display/SPACE/Page+Title
          https://conf.example.com/pages/viewpage.action?pageId=12345
          https://conf.example.com/wiki/display/SPACE/Title
          https://conf.example.com/wiki/spaces/SPACE/pages/12345
        """
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)

        page_id: str | None = None
        space_key: str | None = None
        title_slug: str | None = None

        # Вариант 1: ?pageId=12345
        if "pageId" in qs:
            page_id = qs["pageId"][0]

        # Вариант 2: /display/SPACE/Title  или  /wiki/display/SPACE/Title
        if not page_id:
            m = re.search(r"/display/([^/]+)/([^?#]+)", parsed.path)
            if m:
                space_key = m.group(1)
                title_slug = m.group(2).replace("+", " ").replace("%20", " ")

        # Вариант 3: /wiki/spaces/SPACE/pages/12345
        if not page_id:
            m = re.search(r"/spaces/([^/]+)/pages/(\d+)", parsed.path)
            if m:
                space_key = m.group(1)
                page_id = m.group(2)

        # Вариант 4: просто число в path — /pages/12345
        if not page_id:
            m = re.search(r"/pages/(\d+)", parsed.path)
            if m:
                page_id = m.group(1)

        if page_id:
            page = self.get_page(page_id)
            return {
                "page_id": page_id,
                "title": page.get("title", ""),
                "space_key": page.get("space", {}).get("key", space_key or ""),
                "url": url,
            }

        if space_key and title_slug:
            page = self.get_page_by_title(space_key, title_slug)
            if not page:
                raise ValueError(
                    f"Страница '{title_slug}' не найдена в пространстве '{space_key}'"
                )
            return {
                "page_id": page["id"],
                "title": page.get("title", title_slug),
                "space_key": space_key,
                "url": url,
            }

        raise ValueError(
            f"Не удалось разобрать URL Confluence: {url!r}\n"
            "Ожидаемые форматы:\n"
            "  /display/SPACE/PageTitle\n"
            "  /pages/viewpage.action?pageId=12345"
        )

    def get_contacts(self, page_id: str) -> list[str]:
        """Возвращает список контактов (имён) со страницы. Кеш заполняется при parse_servers."""
        if page_id not in self._contacts_cache:
            self.parse_servers(page_id)
        return self._contacts_cache.get(page_id, [])

    def ping(self) -> bool:
        try:
            r = self._session.get(
                f"{self._base}/rest/api/user/current",
                timeout=10,
            )
            return r.status_code == 200
        except Exception:
            return False


# ------------------------------------------------------------------
# Contacts parser (блок "Контакты" на странице)
# ------------------------------------------------------------------

def _parse_contacts(html: str) -> list[str]:
    """
    Ищет на странице блок "Контакты" / "Contacts" и извлекает список имён.
    Поддерживает: заголовок + список/таблица/параграфы ниже.
    """
    soup = BeautifulSoup(html, "html.parser")
    names: list[str] = []
    # Confluence: заголовок "Контакты" / "Contacts"
    contact_heading = soup.find(string=re.compile(r"Контакты?|Contacts?", re.IGNORECASE))
    if not contact_heading:
        return names
    parent = contact_heading.parent
    if not parent:
        return names
    # Ищем следующий контент: ul, table, или соседние p/div
    for elem in parent.find_next_siblings():
        tag = elem.name
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            break
        if tag == "ul":
            for li in elem.find_all("li"):
                t = re.sub(r"\s+", " ", li.get_text(separator=" ")).strip()
                if t and len(t) > 2 and not t.startswith("@"):
                    names.append(t)
            for p in elem.find_all("p"):
                t = re.sub(r"\s+", " ", p.get_text(separator=" ")).strip()
                if t and len(t) > 2:
                    names.append(t)
            break
        if tag == "table":
            for cell in elem.find_all(["td", "th"]):
                t = re.sub(r"\s+", " ", cell.get_text(separator=" ")).strip()
                if t and len(t) > 4 and t not in ("Контакты", "Contacts", "Имя", "Name"):
                    names.append(t)
            break
        if tag in ("p", "div") or (tag and "body" in tag.lower()):
            for p in elem.find_all(["p", "li"]):
                t = re.sub(r"\s+", " ", p.get_text(separator=" ")).strip()
                if t and len(t) > 2 and not t.startswith("@"):
                    names.append(t)
            if not names:
                t = elem.get_text(separator=" ", strip=True)
                if t and len(t) > 2:
                    for part in re.split(r"[,;]|\s+и\s+", t):
                        part = part.strip()
                        if part and len(part) > 3 and not part.startswith("@"):
                            names.append(part)
            break
    return list(dict.fromkeys(names))  # deduplicate


# ------------------------------------------------------------------
# HTML table parser
# ------------------------------------------------------------------

def _parse_tables(html: str, page_title: str = "", page_id: str = "") -> list[ServerRecord]:
    """Парсит все таблицы из Confluence Storage Format HTML."""
    soup = BeautifulSoup(html, "html.parser")
    records: list[ServerRecord] = []

    for table in soup.find_all("table"):
        table_records = _parse_single_table(table, page_title, page_id)
        records.extend(table_records)

    return records


def _parse_single_table(
    table: Any,
    page_title: str,
    page_id: str,
) -> list[ServerRecord]:
    rows = table.find_all("tr")
    if not rows:
        return []

    # Находим заголовки (первая строка с <th> или первая строка)
    header_row = None
    data_start = 0
    for i, row in enumerate(rows):
        if row.find("th"):
            header_row = row
            data_start = i + 1
            break

    if header_row is None:
        # Попробуем первую строку как заголовок
        header_row = rows[0]
        data_start = 1

    headers = [_cell_text(th) for th in header_row.find_all(["th", "td"])]
    if not headers or not any(_normalize_header(h) in _COLUMN_MAP for h in headers):
        # Таблица не похожа на таблицу серверов
        return []

    col_map = {i: _COLUMN_MAP.get(_normalize_header(h), h) for i, h in enumerate(headers)}
    records: list[ServerRecord] = []
    current_env = ""

    for row in rows[data_start:]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        values = [_cell_text(c) for c in cells]

        # Пропускаем пустые строки
        if not any(v.strip() for v in values):
            continue

        rec = ServerRecord(page_title=page_title, page_id=page_id)

        for i, val in enumerate(values):
            field_name = col_map.get(i)
            if not field_name:
                continue
            if field_name == "env":
                if val.strip():
                    current_env = val.strip()
                rec.env = current_env
            elif hasattr(rec, field_name):
                setattr(rec, field_name, val.strip())
            else:
                rec.extra[headers[i] if i < len(headers) else f"col{i}"] = val.strip()

        # Если env не было явно — используем накопленное
        if not rec.env:
            rec.env = current_env

        # Пропускаем строки без сервера
        if rec.server:
            records.append(rec)

    return records


def _cell_text(cell: Any) -> str:
    """Извлекает чистый текст из ячейки, убирает лишние пробелы."""
    return re.sub(r"\s+", " ", cell.get_text(separator=" ")).strip()


def _normalize_header(h: str) -> str:
    return h.lower().strip()


# ------------------------------------------------------------------
# Search / filter
# ------------------------------------------------------------------

def _filter_records(records: list[ServerRecord], query: str) -> list[ServerRecord]:
    query = query.strip()

    # Режим regex: r:паттерн
    if query.lower().startswith("r:"):
        try:
            pat = re.compile(query[2:], re.IGNORECASE)
            return [
                r for r in records
                if pat.search(r.server)
                or pat.search(_short_hostname(r.server))
                or pat.search(r.ip)
            ]
        except re.error:
            pass

    # Wildcard: содержит * или ?
    if "*" in query or "?" in query:
        pattern = query.lower()
        return [
            r for r in records
            if fnmatch.fnmatch(r.server.lower(), pattern)
            or fnmatch.fnmatch(_short_hostname(r.server), pattern)
        ]

    # Подстрока (без учёта регистра)
    # Ищем и по полному FQDN, и по короткому имени
    q = query.lower()
    return [
        r for r in records
        if q in r.server.lower()
        or q in _short_hostname(r.server)
        or q in r.ip.lower()
        or q in r.env.lower()
        or q in r.dc.lower()
    ]
