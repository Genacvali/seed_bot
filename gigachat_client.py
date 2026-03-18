import base64
import time
import uuid
import logging
import threading
import requests
from typing import Optional
from config import (
    GIGACHAT_CLIENT_ID, GIGACHAT_CLIENT_SECRET, GIGACHAT_SCOPE,
    GIGACHAT_MODEL, GIGACHAT_VERIFY_TLS, GIGACHAT_RATE_LIMIT,
)

log = logging.getLogger(__name__)

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_BASE = "https://gigachat.devices.sberbank.ru/api/v1"

_RETRY_ATTEMPTS = 5
_RETRY_BASE_DELAY = 2.0


class GigaChatClient:
    def __init__(self):
        self._token: Optional[str] = None
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()  # защита от race condition при обновлении токена
        self._rate_lock = threading.Lock()
        self._last_request_time = 0.0
        self._min_interval = 60.0 / GIGACHAT_RATE_LIMIT if GIGACHAT_RATE_LIMIT else 0

    def _rate_limit(self) -> None:
        if self._min_interval <= 0:
            return
        with self._rate_lock:
            elapsed = time.monotonic() - self._last_request_time
            wait = self._min_interval - elapsed
            if wait > 0:
                time.sleep(wait)
            self._last_request_time = time.monotonic()

    def _get_token(self) -> str:
        # Быстрая проверка без блокировки
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        # Медленная проверка с блокировкой — только один поток обновляет токен
        with self._token_lock:
            if self._token and time.time() < self._token_expires_at - 60:
                return self._token
            self._refresh_token()
            return self._token

    def _refresh_token(self) -> None:
        key = base64.b64encode(
            f"{GIGACHAT_CLIENT_ID}:{GIGACHAT_CLIENT_SECRET}".encode()
        ).decode()
        r = requests.post(
            OAUTH_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "RqUID": str(uuid.uuid4()),
                "Authorization": f"Basic {key}",
            },
            data={"scope": GIGACHAT_SCOPE},
            verify=GIGACHAT_VERIFY_TLS,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        self._token = data["access_token"]
        self._token_expires_at = data.get("expires_at", int(time.time()) + 1800)
        log.debug("GigaChat: токен обновлён")

    def _post(self, url: str, **kwargs) -> requests.Response:
        delay = _RETRY_BASE_DELAY
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            self._rate_limit()
            token = self._get_token()
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                verify=GIGACHAT_VERIFY_TLS,
                **kwargs,
            )
            if r.status_code == 429:
                wait = max(float(r.headers.get("Retry-After", delay)), delay)
                log.warning("GigaChat 429 — ждём %.1fs (попытка %d/%d)", wait, attempt, _RETRY_ATTEMPTS)
                time.sleep(wait)
                delay *= 2
                continue
            if r.status_code == 401:
                # Токен протух — сбрасываем и повторяем
                log.warning("GigaChat 401 — сброс токена (попытка %d/%d)", attempt, _RETRY_ATTEMPTS)
                with self._token_lock:
                    self._token = None
                    self._token_expires_at = 0.0
                time.sleep(delay)
                delay *= 2
                continue
            r.raise_for_status()
            return r
        r.raise_for_status()
        return r

    def chat(self, messages: list[dict]) -> str:
        r = self._post(
            f"{API_BASE}/chat/completions",
            json={"model": GIGACHAT_MODEL, "messages": messages, "stream": False},
            timeout=120,
        )
        choice = r.json().get("choices", [{}])[0]
        return (choice.get("message") or {}).get("content", "").strip()
