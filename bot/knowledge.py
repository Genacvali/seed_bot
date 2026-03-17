"""
Модуль извлечения и применения знаний.

Классификация входящего сообщения:
  alert      — алерт мониторинга → анализ + рекомендации
  fact       → структурировать и сохранить
  question   → обычный чат с контекстом из базы знаний
  chat       → обычный чат без поиска
  correction → пользователь поправляет предыдущий ответ бота
  uncertain  → непонятно, спросить уточнение

Работает и без MongoDB — хранит факты в памяти процесса.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .gigachat import GigaChatClient
    from .storage import Storage

# ---------------------------------------------------------------------------
# Регулярки
# ---------------------------------------------------------------------------

# Фрустрация: маты, негатив, "не то", "не так"
_FRUSTRATION_RE = re.compile(
    r"(?:блять|бля[,!]|нахуй|нахер|пиздец|хуйня|ёбаный|ебаный|дебил|идиот"
    r"|тупой|тупица|не то\b|не так\b|опять не так|снова не так|wtf\b|bullshit)",
    re.IGNORECASE,
)

# Исправление: "нет, ...", "не так, ...", "неправильно"
_CORRECTION_RE = re.compile(
    r"^(?:нет[,!.\s]|не так[,!.\s]|неправильно[,!.\s]|неверно[,!.\s]"
    r"|не верно[,!.\s]|ошибся[,!.\s]|это не так)",
    re.IGNORECASE,
)

# Глоссарий: "SMI = SmartApp IDE", "mng — это MongoDB нода", "smi это ..."
_GLOSSARY_RE = re.compile(
    r"\b([a-zA-Z0-9_\-]{2,20})\s*(?:=|—\s*это|это\b|is\b|means?\b)\s+(.{3,120}?)(?:[.,;!?]|$)",
    re.IGNORECASE,
)

# Упоминание человека с контекстом: "@frolov отвечает за X"
_PERSON_MENTION_RE = re.compile(
    r"@([\w._-]+)\s+(?:отвечает|занимается|владеет|ответственный|ответственна|leads?|owns?|manages?)"
    r".{0,80}",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# LLM-промпты
# ---------------------------------------------------------------------------

_CLASSIFY_PROMPT = """\
Классифицируй сообщение пользователя по одному из типов:

  alert      — алерт мониторинга/пейджера (hostname + метрика + статус WARN/CRIT/OK)
  fact       — ЧИСТОЕ утверждение об инфраструктуре без вопроса: "db-01 — мастер Postgres",
               "серверы -mng- принадлежат @vasya". НЕ факт если есть "?", "что это", "расскажи".
  question   — любой вопрос об инфраструктуре, сервере, человеке, документации;
               "что это за сервер X?", "кто такой Фролов?", "расскажи про X" — question.
               Если сообщение содержит hostname И вопросительные слова/знаки — question, не fact.
  correction — начинается с "нет", "не так", "неправильно", "ошибся" и исправляет что-то сказанное ранее
  chat       — приветствие, болтовня, благодарность, не связано с инфраструктурой
  uncertain  — смысл неоднозначен, нельзя точно определить тип (confidence < 0.6)

Примеры:
  "а вот этот p-smi-mng-sc-msk02?"          → question
  "что это за сервер d-ihubcs-mng-adv-msk01" → question
  "кто такой @Фролов?"                       → question
  "db-prod-01 это мастер Postgres в проде"   → fact
  "нет, это не так — он в Dev, не в Prod"    → correction
  CRITICAL: High disk usage on db-01 at 14:05 → alert

Ответь ТОЛЬКО валидным JSON:
{"message_type": "question", "confidence": 0.97, "reason": "вопрос о конкретном сервере"}
"""

_EXTRACT_FACT_PROMPT = """\
Извлеки структурированный факт из сообщения для базы знаний DevOps/DBA команды.

fact_type:
  server_ownership  — кто отвечает за сервер/сервис/БД
  server_tags       — теги, паттерны именования
  server_info       — роль, окружение, конфигурация
  db_info           — информация о базах данных
  alert_rule        — правило алертинга
  known_issue       — известная проблема и решение
  runbook           — инструкция по действиям
  on_call           — дежурства
  person_info       — информация о человеке из команды
  glossary_term     — расшифровка аббревиатуры/термина
  general           — прочее

entities:
  servers  — hostname или паттерны
  users    — Mattermost username БЕЗ @
  tags     — теги
  services — названия сервисов
  people   — полные имена людей (если упоминаются)

structured_updates — апдейты в server_registry (MongoDB синтаксис).

Ответь ТОЛЬКО валидным JSON:
{
  "fact_type": "server_ownership",
  "summary": "...",
  "entities": {"servers": [], "users": [], "tags": [], "services": [], "people": []},
  "structured_updates": []
}
"""

_PERSON_EXTRACT_PROMPT = """\
Извлеки информацию о человеке из сообщения.
Если есть имя, ник, роль или зона ответственности — извлеки.
Иначе верни {"found": false}.

Ответь ТОЛЬКО валидным JSON:
{"found": true, "username": "frolov_a", "display_name": "Александр Фролов", "role": "DBA", "expertise": ["PostgreSQL"]}
{"found": false}

Примеры:
  "@frolov отвечает за PostgreSQL кластеры" → {"found": true, "username": "frolov", "display_name": "Frolov", "role": "DBA", "expertise": ["PostgreSQL"]}
  "Александр Фролов — наш тимлид DBA"      → {"found": true, "username": "", "display_name": "Александр Фролов", "role": "тимлид DBA", "expertise": []}
  "как настроить Nginx?"                    → {"found": false}
"""

_ANALYZE_ALERT_PROMPT = """\
Ты — S.E.E.D., Senior DBA/DevOps. Пришёл алерт мониторинга.

Ниже — контекст из базы знаний о затронутых серверах (если есть).
{context}

Твоя задача:
1. Коротко объясни суть проблемы (1-2 предложения, без пересказа алерта)
2. Назови ответственного если знаешь из контекста
3. Дай 2-3 конкретных первых шага для диагностики (команды, что проверить)
4. Оцени severity: критично прямо сейчас или можно разобраться спокойно?

Стиль: неформально, как опытный коллега. Не пиши "Запомнил" или "Принял".
"""


class KnowledgeManager:
    CONFIDENCE_THRESHOLD = 0.5

    def __init__(self, gc: GigaChatClient, storage: Storage | None = None) -> None:
        self._gc = gc
        self._storage = storage
        self._mem_facts: list[dict[str, Any]] = []
        self._mem_people: list[dict[str, Any]] = []
        self._mem_terms: list[dict[str, Any]] = []
        self._last_msg_type: str = "chat"

    # ------------------------------------------------------------------
    # Детекторы (быстрые, без LLM)
    # ------------------------------------------------------------------

    def is_frustrated(self, text: str) -> bool:
        """True если сообщение выражает фрустрацию/негатив (feature 6)."""
        return bool(_FRUSTRATION_RE.search(text))

    def is_correction(self, text: str) -> bool:
        """True если сообщение начинается с исправления (feature 3)."""
        return bool(_CORRECTION_RE.match(text.strip()))

    def extract_glossary_terms(self, text: str) -> list[tuple[str, str]]:
        """Извлекает пары (термин, расшифровка) из текста (feature 4)."""
        results = []
        for m in _GLOSSARY_RE.finditer(text):
            term = m.group(1).strip()
            expansion = m.group(2).strip()
            # Фильтр: термин ≤ 20 символов, расшифровка > 3
            if 2 <= len(term) <= 20 and len(expansion) > 3:
                results.append((term, expansion))
        return results

    # ------------------------------------------------------------------
    # Основной метод — классифицирует и обрабатывает сообщение
    # ------------------------------------------------------------------

    def process(
        self,
        text: str,
        user_id: str,
        channel_id: str = "",
        thread_id: str = "",
        in_thread: bool = False,
    ) -> str | None:
        """
        Возвращает готовый ответ если сообщение — факт, алерт, коррекция.
        Возвращает None если это обычный вопрос/чат.
        Логирует пробелы в знаниях.
        """
        # Автодетект терминов глоссария (без LLM, быстро)
        terms = self.extract_glossary_terms(text)
        for term, expansion in terms:
            self._save_term(term, expansion, context=text[:100], added_by=user_id)

        # Автодетект упоминаний людей (@username отвечает за X)
        for m in _PERSON_MENTION_RE.finditer(text):
            self._try_save_person_from_mention(m.group(0), user_id)

        classified = self._gc.json_extract(_CLASSIFY_PROMPT, text)
        if not classified:
            return None

        msg_type = classified.get("message_type", "chat")
        confidence = float(classified.get("confidence", 0))

        print(
            f"[knowledge] type={msg_type!r} confidence={confidence:.2f} "
            f"reason={classified.get('reason', '')!r}",
            flush=True,
        )

        self._last_msg_type = msg_type

        # feature 8: уточняющий вопрос при неуверенности
        if msg_type == "uncertain" or (confidence < 0.6 and msg_type == "chat"):
            return (
                "Не уверен что правильно понял. Это вопрос про инфраструктуру "
                "или просто поболтать? Уточни — отвечу точнее."
            )

        if confidence < self.CONFIDENCE_THRESHOLD:
            return None

        # feature 3: коррекция
        if msg_type == "correction" or self.is_correction(text):
            return self._handle_correction(text, user_id, in_thread)

        if msg_type == "fact":
            return self._handle_fact(text, user_id)

        if msg_type == "alert":
            return self._handle_alert(text)

        # feature 13: если вопрос остался без ответа (question/chat без данных)
        if msg_type == "question" and self._storage and channel_id:
            # Логируем как пробел — будет заполнен если бот ответил по Confluence/facts
            # (resolve_gap вызывается в main.py после успешного ответа)
            try:
                self._storage.log_gap(text, user_id, channel_id, thread_id)
            except Exception:
                pass

        return None

    def resolve_gap_if_answered(self, question: str) -> None:
        """Помечает пробел решённым после успешного ответа бота."""
        if self._storage:
            try:
                self._storage.resolve_gap(question)
            except Exception:
                pass

    def build_context_prompt(self, text: str) -> str:
        """
        RAG: извлекает релевантные фрагменты из БД и формирует контекст для ответа.
        Модель должна отвечать ТОЛЬКО на основе этого контекста.
        """
        parts: list[str] = []

        # 1. Факты из базы знаний (full-text)
        facts = self._find_relevant_facts(text)
        if facts:
            lines = ["### Факты из базы знаний:"]
            for f in facts:
                lines.append(f"- [{f.get('fact_type', '?')}] {f.get('summary', '')}")
            parts.append("\n".join(lines))

        # 2. Runbooks — инструкции (full-text)
        if self._storage:
            try:
                runbooks = self._storage.search_runbooks(text, limit=5)
                if runbooks:
                    rb_lines = ["### Runbooks (инструкции):"]
                    for r in runbooks:
                        title = r.get("title", "—")
                        content = (r.get("content") or "")[:800].strip()
                        if content:
                            rb_lines.append(f"**{title}**\n{content}")
                        else:
                            rb_lines.append(f"- **{title}** (без текста)")
                    parts.append("\n\n".join(rb_lines))
            except Exception as e:
                print(f"[knowledge] search_runbooks error: {e}", flush=True)

        # 3. Глоссарий (feature 4)
        if self._storage:
            try:
                gloss = self._storage.build_glossary_context()
                if gloss:
                    parts.append(gloss)
            except Exception:
                pass
        elif self._mem_terms:
            lines = ["### Словарь команды:"]
            for t in self._mem_terms[-20:]:
                lines.append(f"- `{t['term']}` = {t['expansion']}")
            parts.append("\n".join(lines))

        if not parts:
            return ""

        # RAG-обёртка: явно говорим модели использовать только этот контекст
        header = (
            "## Контекст для ответа (RAG)\n"
            "Ниже — извлечённые из базы знания. Отвечай ТОЛЬКО на основе этого контекста. "
            "Если ответа нет в контексте — честно скажи «В контексте этого нет» или «Не нашёл в базе знаний». "
            "Не выдумывай факты и ссылки.\n\n"
        )
        return header + "\n\n".join(parts)

    def build_people_context(self, text: str) -> str:
        """Ищет упомянутых людей в базе и возвращает контекст."""
        # Ищем @username и имена в тексте
        mentions = re.findall(r"@([\w._-]+)", text)
        names = re.findall(r"\b([А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+)\b", text)

        results: list[dict[str, Any]] = []
        if self._storage:
            for m in mentions + names:
                try:
                    found = self._storage.find_person(m)
                    results.extend(found)
                except Exception:
                    pass
        else:
            q_lower = text.lower()
            for p in self._mem_people:
                if (p.get("username", "").lower() in q_lower or
                        p.get("display_name", "").lower() in q_lower):
                    results.append(p)

        if not results:
            return ""

        seen: set[str] = set()
        lines = ["### Люди из базы знаний:"]
        for p in results:
            key = p.get("username", p.get("display_name", ""))
            if key in seen:
                continue
            seen.add(key)
            line = f"- {p.get('display_name', p.get('username', '?'))}"
            if p.get("role"):
                line += f" — {p['role']}"
            if p.get("expertise"):
                line += f" (эксперт: {', '.join(p['expertise'])})"
            lines.append(line)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Обработка факта
    # ------------------------------------------------------------------

    def _handle_fact(self, text: str, user_id: str) -> str | None:
        extracted = self._gc.json_extract(_EXTRACT_FACT_PROMPT, text)
        if not extracted or "fact_type" not in extracted:
            extracted = {
                "fact_type": "general",
                "summary": text[:300],
                "entities": {},
                "structured_updates": [],
            }

        fact_type = extracted.get("fact_type", "general")
        summary = extracted.get("summary", text[:200])
        entities = extracted.get("entities", {})
        structured_updates = extracted.get("structured_updates", [])

        print(f"[knowledge] saving fact type={fact_type!r} summary={summary[:80]!r}", flush=True)

        # Автосохранение профиля человека из person_info (feature 1)
        if fact_type == "person_info":
            self._try_extract_and_save_person(text, entities, user_id)

        # Автосохранение термина глоссария (feature 4)
        if fact_type == "glossary_term":
            for server in entities.get("services", []):
                pass  # уже обработано через _GLOSSARY_RE выше

        if self._storage:
            try:
                self._storage.save_fact(
                    fact_type=fact_type,
                    summary=summary,
                    raw_text=text,
                    entities=entities,
                    created_by=user_id,
                    structured_updates=structured_updates,
                )
            except Exception as e:
                print(f"[knowledge] storage.save_fact error: {e}", flush=True)

        self._mem_facts.append({"fact_type": fact_type, "summary": summary, "entities": entities})
        if len(self._mem_facts) > 200:
            self._mem_facts = self._mem_facts[-200:]

        return self._build_fact_confirmation(summary, entities, structured_updates)

    # ------------------------------------------------------------------
    # Обработка коррекции (feature 3)
    # ------------------------------------------------------------------

    def _handle_correction(self, text: str, user_id: str, in_thread: bool) -> str:
        # Извлекаем исправленный факт
        correction_text = _CORRECTION_RE.sub("", text).strip()
        if not correction_text:
            return "Что именно неверно? Поправь — я обновлю в базе."

        # Сохраняем исправление как новый факт с пометкой
        if self._storage:
            try:
                self._storage.save_fact(
                    fact_type="correction",
                    summary=f"[ИСПРАВЛЕНИЕ] {correction_text[:300]}",
                    raw_text=text,
                    entities={},
                    created_by=user_id,
                )
            except Exception:
                pass

        return f"Принял поправку. Обновил в базе: {correction_text[:150]}"

    # ------------------------------------------------------------------
    # Обработка алерта
    # ------------------------------------------------------------------

    def _handle_alert(self, text: str) -> str:
        context_facts = self._find_relevant_facts(text)
        context_block = ""
        if context_facts:
            lines = [f"- {f.get('summary', '')}" for f in context_facts]
            context_block = "Что известно о серверах из алерта:\n" + "\n".join(lines)

        system = _ANALYZE_ALERT_PROMPT.format(context=context_block or "(нет данных в базе знаний)")

        if self._storage:
            try:
                self._try_save_alert_stat(text)
            except Exception as e:
                print(f"[knowledge] alert_stat error: {e}", flush=True)

        return self._gc.chat(text, extra_context=system)

    def _try_save_alert_stat(self, text: str) -> None:
        m = re.match(r"^([a-zA-Z0-9._-]+):\s*(.+?)(?:\n|$)", text.strip())
        if m and self._storage:
            self._storage.inc_alert(m.group(2)[:100], m.group(1))

    # ------------------------------------------------------------------
    # Люди и глоссарий (без LLM)
    # ------------------------------------------------------------------

    def _save_term(self, term: str, expansion: str, context: str, added_by: str) -> None:
        if self._storage:
            try:
                self._storage.save_term(term, expansion, context=context, added_by=added_by)
            except Exception:
                pass
        else:
            self._mem_terms.append({"term": term.lower(), "expansion": expansion})
            if len(self._mem_terms) > 100:
                self._mem_terms = self._mem_terms[-100:]

    def _try_save_person_from_mention(self, text: str, user_id: str) -> None:
        """Быстрое сохранение из '@username отвечает за X' (без LLM)."""
        m = re.match(r"@([\w._-]+)\s+(.{5,80})", text, re.IGNORECASE)
        if not m:
            return
        username = m.group(1)
        context = m.group(2).strip()
        role = context[:80]
        if self._storage:
            try:
                self._storage.save_person(
                    username=username,
                    display_name=username,
                    role=role,
                    added_by=user_id,
                )
            except Exception:
                pass
        else:
            self._mem_people.append({"username": username, "display_name": username, "role": role})

    def _try_extract_and_save_person(
        self, text: str, entities: dict[str, Any], user_id: str
    ) -> None:
        """LLM-экстракция профиля человека (feature 1)."""
        try:
            extracted = self._gc.json_extract(_PERSON_EXTRACT_PROMPT, text)
        except Exception:
            return
        if not extracted or not extracted.get("found"):
            return
        username = extracted.get("username", "").strip().lstrip("@") or "unknown"
        display_name = extracted.get("display_name", username)
        role = extracted.get("role", "")
        expertise = extracted.get("expertise", [])

        if self._storage:
            try:
                self._storage.save_person(
                    username=username,
                    display_name=display_name,
                    role=role,
                    expertise=expertise,
                    added_by=user_id,
                )
                print(f"[knowledge] saved person: {display_name!r} role={role!r}", flush=True)
            except Exception as e:
                print(f"[knowledge] save_person error: {e}", flush=True)
        else:
            self._mem_people.append({
                "username": username,
                "display_name": display_name,
                "role": role,
                "expertise": expertise,
            })

    # ------------------------------------------------------------------
    # Поиск фактов
    # ------------------------------------------------------------------

    def _find_relevant_facts(self, text: str) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        if self._storage:
            try:
                facts = self._storage.search_facts(text, limit=5)
            except Exception:
                pass

        if not facts and self._mem_facts:
            text_lower = text.lower()
            for f in reversed(self._mem_facts):
                entities = f.get("entities", {})
                all_terms = (
                    [s.strip("-*").lower() for s in entities.get("servers", []) if s]
                    + [t.lower() for t in entities.get("tags", []) if t]
                    + [u.lower() for u in entities.get("users", []) if u]
                    + [p.lower() for p in entities.get("people", []) if p]
                )
                if any(t and t in text_lower for t in all_terms):
                    facts.append(f)
                if len(facts) >= 5:
                    break
        return facts

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_fact_confirmation(
        self,
        summary: str,
        entities: dict[str, Any],
        updates: list[dict[str, Any]],
    ) -> str:
        parts = [f"Принял. {summary}"]
        details = []
        if entities.get("servers"):
            details.append(f"паттерн: `{'`, `'.join(entities['servers'])}`")
        if entities.get("users"):
            details.append(f"ответственный: {', '.join('@' + u for u in entities['users'])}")
        if entities.get("people"):
            details.append(f"люди: {', '.join(entities['people'])}")
        if entities.get("tags"):
            details.append(f"теги: {', '.join(entities['tags'])}")
        if updates and self._storage:
            cols = list({u["collection"] for u in updates})
            details.append(f"обновил: {', '.join(cols)}")
        if details:
            parts.append("(" + ", ".join(details) + ")")
        return " ".join(parts)
