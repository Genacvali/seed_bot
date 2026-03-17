"""
GigaChat: OAuth, Embeddings, Chat.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any

import requests

from .config import Config

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
EMBEDDINGS_URL = "https://gigachat.devices.sberbank.ru/api/v1/embeddings"
CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

SYSTEM_PROMPT = """Ты — помощник. Отвечай на основе ТОЛЬКО переданного контекста (документы и кейсы).
Если ответа нет в контексте — скажи «В переданном контексте этого нет».
Не выдумывай факты. Кратко и по делу."""


@dataclass
class _Token:
    value: str
    expires_at: float


class GigaChat:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._session = requests.Session()
        self._session.verify = cfg.gigachat_verify_tls
        if not cfg.gigachat_verify_tls:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._token: _Token | None = None

    def _get_token(self) -> str:
        if self._token and time.monotonic() < self._token.expires_at - 60:
            return self._token.value
        if self._cfg.gigachat_auth_key:
            self._token = _Token(value=self._cfg.gigachat_auth_key, expires_at=time.monotonic() + 3600)
            return self._token.value
        assert self._cfg.gigachat_client_id and self._cfg.gigachat_client_secret
        credentials = base64.b64encode(
            f"{self._cfg.gigachat_client_id}:{self._cfg.gigachat_client_secret}".encode()
        ).decode()
        r = self._session.post(
            OAUTH_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {credentials}",
                "RqUID": "1",
            },
            data={
                "scope": self._cfg.gigachat_scope,
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        if r.status_code >= 400:
            raise RuntimeError(
                f"GigaChat OAuth {r.status_code}: {r.text[:500]}. "
                "Проверь GIGACHAT_CLIENT_ID, GIGACHAT_CLIENT_SECRET, GIGACHAT_SCOPE и GIGACHAT_VERIFY_TLS."
            )
        r.raise_for_status()
        data = r.json()
        access_token = data.get("access_token")
        expires_in = float(data.get("expires_in", 3600))
        if not access_token:
            raise RuntimeError(f"No access_token in OAuth response: {data}")
        self._token = _Token(value=access_token, expires_at=time.monotonic() + expires_in)
        return self._token.value

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Эмбеддинги для списка текстов. При ошибке — пустой список."""
        if not texts:
            return []
        body = {
            "model": self._cfg.gigachat_embeddings_model,
            "input": [t[:8000] for t in texts],
        }
        try:
            r = self._session.post(
                EMBEDDINGS_URL,
                headers={
                    "Authorization": f"Bearer {self._get_token()}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=30,
            )
            if r.status_code >= 400:
                print(f"[gigachat] embeddings {r.status_code} {r.text[:200]}", flush=True)
                return []
            data = r.json()
            out = data.get("data") or []
            return [item.get("embedding", []) for item in out if isinstance(item.get("embedding"), list)]
        except Exception as e:
            print(f"[gigachat] embed error: {e}", flush=True)
            return []

    def chat(self, user_message: str, context: str) -> str:
        """Ответ GigaChat с контекстом (RAG)."""
        system = f"{SYSTEM_PROMPT}\n\n## Контекст:\n{context}" if context else SYSTEM_PROMPT
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ]
        body = {
            "model": self._cfg.gigachat_model,
            "messages": messages,
            "temperature": 0.3,
        }
        try:
            r = self._session.post(
                CHAT_URL,
                headers={
                    "Authorization": f"Bearer {self._get_token()}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=60,
            )
            if r.status_code >= 400:
                return f"Ошибка GigaChat: {r.status_code}"
            data = r.json()
            choices = data.get("choices") or []
            if not choices:
                return "Пустой ответ модели."
            content = choices[0].get("message", {}).get("content", "")
            return content or "Пустой ответ модели."
        except Exception as e:
            print(f"[gigachat] chat error: {e}", flush=True)
            return f"Ошибка: {e}"
