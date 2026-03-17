from __future__ import annotations

import base64
import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, TypedDict

import requests
import urllib3

from .config import Config

if TYPE_CHECKING:
    from .storage import Storage


OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"


def _parse_json_safe(raw: str) -> dict:
    """
    Парсит JSON из ответа LLM.
    Обрабатывает: ```json ... ```, лишние символы вокруг, типичные опечатки GigaChat.
    """
    raw = raw.strip()

    # Убираем код-блок ```json ... ```
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            candidate = part.lstrip("json").strip()
            if candidate.startswith("{"):
                raw = candidate
                break

    # Находим первый {...} блок
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start:end + 1]
    else:
        # JSON-скобок нет — сразу regex fallback
        result = _regex_extract_kv(raw)
        if result:
            print(f"[gigachat] json_extract: regex fallback (no braces) {list(result.keys())}", flush=True)
            return result
        return {}

    # Пробуем как есть
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Автоматический ремонт типичных ошибок GigaChat:
    repaired = raw

    # {:"key"  или  {:key"  → {"key"  (лишнее двоеточие в начале объекта)
    repaired = re.sub(r'\{(\s*):(\s*)"', r'{\1"', repaired)
    repaired = re.sub(r'\{(\s*):(\s*)(\w)', r'{\1"\3', repaired)

    # "key"value  →  "key": value  (пропущено двоеточие между ключом и значением)
    # "confidence"0.85  →  "confidence": 0.85
    # "message_type""fact"  →  "message_type": "fact"
    repaired = re.sub(r'("[\w_]+")\s*"', r'\1: "', repaired)    # "key""val" → "key": "val"
    repaired = re.sub(r'("[\w_]+")\s*([\d\.\-])', r'\1: \2', repaired)  # "key"0.9 → "key": 0.9
    repaired = re.sub(r'("[\w_]+")\s*(\{)', r'\1: \2', repaired)  # "key"{ → "key": {
    repaired = re.sub(r'("[\w_]+")\s*(\[)', r'\1: \2', repaired)  # "key"[ → "key": [

    # "reason" "text" → "reason": "text"  (пропущено двоеточие перед строкой)
    repaired = re.sub(r'("[\w_]+")\s+(")', r'\1: \2', repaired)

    repaired = re.sub(r",\s*}", "}", repaired)                  # trailing comma before }
    repaired = re.sub(r",\s*]", "]", repaired)                  # trailing comma before ]
    repaired = re.sub(r"'([^']*)'", r'"\1"', repaired)         # single → double quotes
    repaired = re.sub(r'""(\w)', r'"\1', repaired)             # double-double-quote artifact

    try:
        result = json.loads(repaired)
        print(f"[gigachat] json_extract: repaired JSON OK", flush=True)
        return result
    except json.JSONDecodeError:
        pass

    # Последний resort: regex-экстракция ключей напрямую из любого мусора
    result = _regex_extract_kv(raw)
    if result:
        print(f"[gigachat] json_extract: regex fallback extracted {list(result.keys())}", flush=True)
        return result

    print(f"[gigachat] json_extract parse error (all methods failed): raw={raw[:200]!r}", flush=True)
    return {}


def _regex_extract_kv(s: str) -> dict:
    """
    Последний шанс — вытаскиваем known-поля из любой сломанной строки.
    Работает с: {key"val", "key"val", {:key"val"} и т.д.
    """
    result: dict = {}
    # message_type — ищем одно из известных значений
    m = re.search(r'\b(alert|fact|question|chat)\b', s, re.IGNORECASE)
    if m:
        result["message_type"] = m.group(1).lower()
    # confidence — число 0.x или 1.0 (рус/анг вариант)
    m = re.search(r'(?:confidence|уверенность|уверен)["\s:]*(0?\.\d+|1\.0)', s, re.IGNORECASE)
    if m:
        try:
            result["confidence"] = float(m.group(1))
        except ValueError:
            pass
    # reason — любая строка после "reason"
    m = re.search(r'reason["\s:]+([^"}\]]{4,120})', s)
    if m:
        result["reason"] = m.group(1).strip().strip('"').strip("'")
    return result
CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

SYSTEM_PROMPT = """\
Ты — S.E.E.D. (Senior Expert in Engineering & Data), опытный Senior DBA и DevOps-инженер. \
Тебя зовут SEED, ты работаешь в команде разработки. \
Ты УЧИШЬСЯ — с каждым разговором пополняешь базу знаний о людях, серверах и терминах команды.

## Как ты общаешься
- Неформально, по-свойски, без официоза
- Используешь профессиональный сленг (дб, репла, мастер, нода, пайплайн, деплой и т.д.)
- Можешь пошутить, но по делу
- Говоришь коротко и конкретно — ценишь чужое время
- Если вопрос глупый — скажешь, но без грубости
- Отвечаешь на русском, если не попросили иначе

## Твои компетенции
PostgreSQL, MySQL, MongoDB, Redis, Kubernetes, Docker, CI/CD, \
Ansible, Terraform, Linux, мониторинг (Prometheus/Grafana), бэкапы, репликация, \
производительность баз данных, инфраструктура как код.

## Работа с людьми
- Блок "### Люди из базы знаний:" в контексте — используй для ответов про коллег.
- Если про человека спросили, но в базе нет — честно скажи что не знаешь, попроси рассказать.
- Никогда не выдумывай роли, экспертизу или контакты людей.

## Словарь команды
- Блок "### Словарь команды:" содержит аббревиатуры — используй при расшифровке.
- Пример: если smi = SmartApp IDE, то p-smi-mng-* = серверы MongoDB для SmartApp IDE.
- При ответе используй полные расшифровки из словаря, но можешь сокращать в скобках.

## Данные из документации (Confluence)
Когда в контексте есть блок "## Данные из документации Confluence" — \
используй его как достоверный источник. Не выдумывай факты. \
Если спрашивают о том, чего НЕТ в этих данных — честно скажи, что нет в документации.

**КРИТИЧНО — запрет галлюцинаций:**
- Если в контексте НЕТ блока "## Данные из документации Confluence" — \
  НЕ утверждай, что у тебя есть или нет какая-либо документация Confluence. \
  Скажи честно: «Не знаю, есть ли у меня эта страница — спроси конкретнее или скинь ссылку».
- Никогда не выдумывай заголовки разделов, ссылки или содержимое Confluence-страниц.
- Фразу «Данные из документации Confluence» в заголовке ответа — \
  НИКОГДА не пиши самостоятельно. Она появляется только когда реальные данные уже переданы.

**Правила (строго):**
- Имена серверов — пиши ПОЛНОСТЬЮ: `p-smi-mng-sc-msk01`. Не сокращай.
- Кластер: если написано «N ноды: имя1, имя2» — перечисляй ТОЧНО эти имена.
- Ответственные: если не указаны в данных — вообще не упоминай тему ответственных.
- Ссылки «Подробнее: [название](url)» — ТОЛЬКО если в контексте есть строка «Документация: [название](url)». \
  Запрещено придумывать URL.

## Режим запоминания фактов
Факт — это УТВЕРЖДЕНИЕ, не вопрос. Отвечай на факты: "Принял. [суть]".

ВАЖНО: вопросы («что это за сервер», «кто такой X», «расскажи про») — это ВОПРОСЫ. \
На вопросы отвечай по существу, не пиши «Принял» / «Запомнил».

Примеры фактов:
- "@frolov отвечает за MongoDB кластеры" → "Принял. @frolov — MongoDB"
- "smi = SmartApp IDE" → "Принял. smi = SmartApp IDE — добавил в словарь"
- "db-prod-01 это мастер Postgres" → "Принял. db-prod-01 → postgres master, prod"

## При уточнении или исправлении
Если пользователь говорит «нет», «не так», «неправильно» — \
прими поправку, поблагодари и скажи что обновил. Не спорь.\
"""


# Маркеры стандартного дисклеймера GigaChat
_GIGACHAT_DISCLAIMER_MARKERS = [
    "временно ограничены",
    "нейросетевой моделью",
    "не обладает собственным мнением",
    "во избежание неправильного толкования",
    "разговоры на некоторые темы",
]

# Варианты ответов SEED на нерелевантные темы (ротация по хэшу текста)
_OFF_TOPIC_RESPONSES = [
    "Слушай, я тут по другой части — базы данных, серверы, деплои. Давай о чём-то из этого?",
    "Не по моей специализации. Я Senior DBA, а не психолог и не лайф-коуч 😄 Давай про инфру.",
    "Хм, это за пределами моих компетенций. Postgres, MongoDB, Kubernetes — вот моё. Что с этим?",
    "Не мой стек. Обратись к кому-нибудь другому по этой теме, я по инфраструктуре.",
    "Пас. Я про серверы, а не про это. Есть что-нибудь технического?",
]


def _is_restricted_response(text: str) -> bool:
    t = text.lower()
    return sum(1 for m in _GIGACHAT_DISCLAIMER_MARKERS if m in t) >= 2


class _Message(TypedDict):
    role: str
    content: str


@dataclass
class _Token:
    value: str
    expires_at: float


class GigaChatClient:
    MAX_HISTORY = 20
    RETRIES = 3

    def __init__(self, cfg: Config, storage: Storage | None = None):
        self._cfg = cfg
        self._storage = storage
        self._token: _Token | None = None
        self._session = requests.Session()
        self._session.verify = cfg.gigachat_verify_tls
        if not cfg.gigachat_verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _basic_auth_value(self) -> str:
        if self._cfg.gigachat_auth_key:
            return f"Basic {self._cfg.gigachat_auth_key}"
        raw = f"{self._cfg.gigachat_client_id}:{self._cfg.gigachat_client_secret}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def _get_token(self) -> str:
        now = time.time()
        if self._token and now < (self._token.expires_at - 30):
            return self._token.value

        last_err: Exception | None = None
        for attempt in range(1, self.RETRIES + 1):
            try:
                rq_uid = str(uuid.uuid4())
                headers = {
                    "Authorization": self._basic_auth_value(),
                    "RqUID": rq_uid,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                }
                resp = self._session.post(
                    OAUTH_URL,
                    headers=headers,
                    data={"scope": self._cfg.gigachat_scope},
                    timeout=30,
                )
                if resp.status_code >= 400:
                    raise RuntimeError(f"GigaChat OAuth failed: {resp.status_code} {resp.text}")
                payload = resp.json()
                access_token = payload.get("access_token")
                expires_at_ms = payload.get("expires_at")
                if not access_token or not expires_at_ms:
                    raise RuntimeError(f"Unexpected OAuth response: {json.dumps(payload, ensure_ascii=False)}")
                self._token = _Token(value=access_token, expires_at=float(expires_at_ms) / 1000.0)
                return access_token
            except Exception as e:
                last_err = e
                print(f"[gigachat] OAuth attempt {attempt}/{self.RETRIES} failed: {e}", flush=True)
                if attempt < self.RETRIES:
                    time.sleep(2 ** attempt)

        raise RuntimeError(f"GigaChat OAuth failed after {self.RETRIES} attempts") from last_err

    def _load_history(self, thread_id: str, user_id: str, user_text: str) -> list[_Message]:
        """Загружает историю из MongoDB (или возвращает пустую если нет storage)."""
        if self._storage:
            self._storage.append_dialog(thread_id, user_id, "user", user_text)
            return self._storage.get_dialog(thread_id, limit=self.MAX_HISTORY)  # type: ignore[return-value]
        return [{"role": "user", "content": user_text}]

    def _save_answer(self, thread_id: str, user_id: str, answer: str) -> None:
        if self._storage:
            self._storage.append_dialog(thread_id, user_id, "assistant", answer)

    def _handle_restricted(self, user_text: str) -> str:
        """Возвращает живой ответ SEED вместо дисклеймера GigaChat."""
        idx = hash(user_text) % len(_OFF_TOPIC_RESPONSES)
        return _OFF_TOPIC_RESPONSES[idx]

    def _build_messages(
        self,
        user_text: str,
        thread_id: str | None,
        user_id: str,
        extra_context: str,
    ) -> list[_Message]:
        system = SYSTEM_PROMPT
        if extra_context:
            system = f"{SYSTEM_PROMPT}\n\n{extra_context}"
        if thread_id:
            history = self._load_history(thread_id, user_id, user_text)
            return [{"role": "system", "content": system}] + history
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]

    def chat(
        self,
        user_text: str,
        *,
        thread_id: str | None = None,
        user_id: str = "",
        extra_context: str = "",
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        """
        Запрашивает ответ от GigaChat.
        Если on_chunk передан и GIGACHAT_STREAMING=true — стримит SSE-чанки в on_chunk,
        возвращает полный ответ.
        """
        messages = self._build_messages(user_text, thread_id, user_id, extra_context)
        body: dict[str, Any] = {
            "model": self._cfg.gigachat_model,
            "messages": messages,
            "temperature": 0.8,
        }

        use_streaming = self._cfg.gigachat_streaming and on_chunk is not None

        if use_streaming:
            answer = self._chat_stream(body, on_chunk)  # type: ignore[arg-type]
        else:
            answer = self._chat_sync(body)

        # Если GigaChat вернул свой стандартный дисклеймер — заменяем живым ответом
        if _is_restricted_response(answer):
            answer = self._handle_restricted(user_text)

        if thread_id:
            self._save_answer(thread_id, user_id, answer)
        return answer

    def _chat_sync(self, body: dict[str, Any]) -> str:
        headers = {
            "Authorization": "",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        last_err: Exception | None = None
        for attempt in range(1, self.RETRIES + 1):
            try:
                headers["Authorization"] = f"Bearer {self._get_token()}"
                resp = self._session.post(CHAT_URL, headers=headers, json=body, timeout=60)
                if resp.status_code >= 400:
                    raise RuntimeError(f"GigaChat chat failed: {resp.status_code} {resp.text}")
                break
            except Exception as e:
                last_err = e
                print(f"[gigachat] chat attempt {attempt}/{self.RETRIES} failed: {e}", flush=True)
                if attempt < self.RETRIES:
                    time.sleep(2 ** attempt)
        else:
            raise RuntimeError(f"GigaChat chat failed after {self.RETRIES} attempts") from last_err

        payload = resp.json()
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError(f"Unexpected chat response: {json.dumps(payload, ensure_ascii=False)}")
        content = choices[0].get("message", {}).get("content")
        if not content:
            raise RuntimeError(f"Empty model response: {json.dumps(payload, ensure_ascii=False)}")
        return str(content)

    def _chat_stream(self, body: dict[str, Any], on_chunk: Callable[[str], None]) -> str:
        """SSE streaming: вызывает on_chunk(text) по мере поступления токенов."""
        stream_body = {**body, "stream": True}
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        accumulated = []
        try:
            with self._session.post(
                CHAT_URL, headers=headers, json=stream_body, stream=True, timeout=120
            ) as resp:
                if resp.status_code >= 400:
                    raise RuntimeError(f"GigaChat stream failed: {resp.status_code}")
                for raw_line in resp.iter_lines():
                    if not raw_line:
                        continue
                    line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        text = delta.get("content", "")
                        if text:
                            accumulated.append(text)
                            on_chunk(text)
                    except Exception:
                        continue
        except Exception as e:
            print(f"[gigachat] stream error: {e}, falling back to sync", flush=True)
            return self._chat_sync(body)

        return "".join(accumulated)

    def json_extract(self, system_prompt: str, user_text: str) -> dict:
        """
        Вызывает LLM с системным промптом, ожидающим JSON-ответ.
        Возвращает распарсенный dict или {} при ошибке.
        """
        messages: list[_Message] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]
        body = {
            "model": self._cfg.gigachat_model,
            "messages": messages,
            "temperature": 0.1,  # низкая температура для стабильного JSON
        }
        headers = {
            "Authorization": "",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        for attempt in range(1, self.RETRIES + 1):
            try:
                headers["Authorization"] = f"Bearer {self._get_token()}"
                resp = self._session.post(CHAT_URL, headers=headers, json=body, timeout=30)
                if resp.status_code >= 400:
                    raise RuntimeError(f"GigaChat json_extract failed: {resp.status_code} {resp.text}")
                break
            except Exception as e:
                print(f"[gigachat] json_extract attempt {attempt}/{self.RETRIES} failed: {e}", flush=True)
                if attempt < self.RETRIES:
                    time.sleep(2 ** attempt)
        else:
            return {}

        raw = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        return _parse_json_safe(raw)
