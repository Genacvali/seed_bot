"""
Модуль извлечения и применения знаний.

Классификация входящего сообщения:
  alert    — алерт мониторинга → анализ + рекомендации + инфо из базы знаний
  fact     → структурировать и сохранить
  question → обычный чат с контекстом из базы знаний
  chat     → обычный чат без поиска

Работает и без MongoDB — хранит факты в памяти процесса.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .gigachat import GigaChatClient
    from .storage import Storage

# ---------------------------------------------------------------------------
# Классификация типа сообщения
# ---------------------------------------------------------------------------

_CLASSIFY_PROMPT = """\
Классифицируй сообщение пользователя по одному из типов:

  alert    — алерт мониторинга/пейджера (содержит hostname, метрику, статус WARN/CRIT/OK,
             время, теги инфраструктуры, ссылки на тикеты вида #T...)
  fact     — утверждение об инфраструктуре, которое нужно запомнить
             ("серверы X принадлежат @vasya", "db-01 — мастер Postgres")
  question — вопрос или просьба о помощи
  chat     — приветствие, болтовня, благодарность, всё остальное

Ответь ТОЛЬКО валидным JSON:
{"message_type": "alert", "confidence": 0.97, "reason": "содержит hostname, метрику и время алерта"}
"""

# ---------------------------------------------------------------------------
# Промпт: структурировать факт
# ---------------------------------------------------------------------------

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
  general           — прочее

entities:
  servers  — hostname или паттерны (например "-mng-", "db-prod-*")
  users    — Mattermost username БЕЗ @ (например "ggmikvabiya")
  tags     — теги (например ["mongodb", "prod"])
  services — названия сервисов

structured_updates — апдейты в server_registry (MongoDB синтаксис).

Ответь ТОЛЬКО валидным JSON:
{
  "fact_type": "server_ownership",
  "summary": "...",
  "entities": {"servers": [], "users": [], "tags": [], "services": []},
  "structured_updates": []
}
"""

# ---------------------------------------------------------------------------
# Промпт: разбор алерта
# ---------------------------------------------------------------------------

_ANALYZE_ALERT_PROMPT = """\
Ты — S.E.E.D., Senior DBA/DevOps. Пришёл алерт мониторинга.

Ниже — контекст из базы знаний о затронутых серверах (если есть).
{context}

Твоя задача:
1. Коротко объясни суть проблемы (1-2 предложения, без пересказа алерта)
2. Назови ответственного если знаешь из контекста
3. Дай 2-3 конкретных первых шага для диагностики (команды, что проверить)
4. Оцени severity: критично прямо сейчас или можно разобраться спокойно?

Стиль: неформально, как опытный коллега — не как автоматический отчёт.
Не повторяй дословно текст алерта. Не пиши "Запомнил".
"""


class KnowledgeManager:
    CONFIDENCE_THRESHOLD = 0.5

    def __init__(self, gc: GigaChatClient, storage: Storage | None = None) -> None:
        self._gc = gc
        self._storage = storage
        self._mem_facts: list[dict[str, Any]] = []
        self._last_msg_type: str = "chat"  # последний классифицированный тип

    # ------------------------------------------------------------------
    # Основной метод — классифицирует и обрабатывает сообщение
    # ------------------------------------------------------------------

    def process(self, text: str, user_id: str) -> str | None:
        """
        Возвращает готовый ответ если сообщение — факт или алерт.
        Возвращает None если это обычный вопрос/чат (бот продолжает обычным путём).
        """
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

        if confidence < self.CONFIDENCE_THRESHOLD:
            return None

        self._last_msg_type = msg_type

        if msg_type == "fact":
            return self._handle_fact(text, user_id)

        if msg_type == "alert":
            return self._handle_alert(text)

        return None

    def build_context_prompt(self, text: str) -> str:
        """Инжектирует релевантные факты в системный промпт."""
        facts = self._find_relevant_facts(text)
        if not facts:
            return ""
        lines = ["### Факты из базы знаний (используй при ответе):"]
        for f in facts:
            lines.append(f"- [{f.get('fact_type', '?')}] {f.get('summary', '')}")
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
        if len(self._mem_facts) > 100:
            self._mem_facts = self._mem_facts[-100:]

        return self._build_fact_confirmation(summary, entities, structured_updates)

    # ------------------------------------------------------------------
    # Обработка алерта
    # ------------------------------------------------------------------

    def _handle_alert(self, text: str) -> str:
        # Ищем контекст о затронутых серверах из базы знаний
        context_facts = self._find_relevant_facts(text)
        context_block = ""
        if context_facts:
            lines = []
            for f in context_facts:
                lines.append(f"- {f.get('summary', '')}")
            context_block = "Что известно о серверах из алерта:\n" + "\n".join(lines)

        system = _ANALYZE_ALERT_PROMPT.format(context=context_block or "(нет данных в базе знаний)")

        # Сохраняем алерт в статистику
        if self._storage:
            try:
                self._try_save_alert_stat(text)
            except Exception as e:
                print(f"[knowledge] alert_stat error: {e}", flush=True)

        return self._gc.chat(text, extra_context=system)

    def _try_save_alert_stat(self, text: str) -> None:
        """Пробует распознать сервер и тип алерта и сохранить в alert_stats."""
        import re
        # Простая эвристика: hostname — первое слово перед ':'
        m = re.match(r"^([a-zA-Z0-9._-]+):\s*(.+?)(?:\n|$)", text.strip())
        if m and self._storage:
            server = m.group(1)
            alert_type = m.group(2)[:100]
            self._storage.inc_alert(alert_type, server)

    # ------------------------------------------------------------------
    # Поиск релевантных фактов
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
        if entities.get("tags"):
            details.append(f"теги: {', '.join(entities['tags'])}")
        if updates and self._storage:
            cols = list({u["collection"] for u in updates})
            details.append(f"обновил: {', '.join(cols)}")
        if details:
            parts.append("(" + ", ".join(details) + ")")
        return " ".join(parts)
