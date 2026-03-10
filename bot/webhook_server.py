"""
HTTP-сервер для приёма входящих алертов от Grafana / Alertmanager / Zabbix.

POST /alert  — принимает JSON алерт, постит в Mattermost и анализирует
POST /hook   — raw Mattermost outgoing webhook
GET  /health — healthcheck

Запускается в отдельном потоке.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .config import Config


class _Handler(BaseHTTPRequestHandler):
    on_alert: Callable[[str, str], None]  # (text, source) -> None
    secret: str | None

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[webhook] {fmt % args}", flush=True)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._respond(200, b'{"status":"ok"}')
        else:
            self._respond(404, b'{"error":"not found"}')

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        if self.path in ("/alert", "/hook"):
            if self.secret and not self._verify_sig(body):
                self._respond(403, b'{"error":"forbidden"}')
                return
            self._handle_alert_body(body)
            self._respond(200, b'{"status":"ok"}')
        else:
            self._respond(404, b'{"error":"not found"}')

    def _verify_sig(self, body: bytes) -> bool:
        sig = self.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(
            self.secret.encode(), body, hashlib.sha256  # type: ignore[arg-type]
        ).hexdigest()
        return hmac.compare_digest(sig, expected)

    def _handle_alert_body(self, body: bytes) -> None:
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}

        text = _extract_alert_text(data) or body.decode(errors="replace")[:1000]
        source = self.headers.get("X-Alert-Source", "webhook")

        try:
            self.on_alert(text, source)
        except Exception as e:
            print(f"[webhook] on_alert error: {e}", flush=True)

    def _respond(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


def _extract_alert_text(data: dict[str, Any]) -> str:
    """Поддерживает форматы Grafana, Alertmanager, Zabbix."""
    # Grafana
    if "alerts" in data:
        parts = []
        for a in data["alerts"][:5]:
            name = a.get("labels", {}).get("alertname", "alert")
            status = a.get("status", "")
            instance = a.get("labels", {}).get("instance", "")
            summary = a.get("annotations", {}).get("summary", "")
            parts.append(f"{instance}: {name} [{status}] {summary}".strip(": "))
        return "\n".join(parts)

    # Alertmanager
    if "commonLabels" in data:
        name = data.get("commonLabels", {}).get("alertname", "alert")
        status = data.get("status", "")
        return f"{name} [{status}]"

    # Generic
    if isinstance(data.get("text"), str):
        return data["text"]
    if isinstance(data.get("message"), str):
        return data["message"]

    return ""


def start_webhook_server(
    port: int,
    on_alert: Callable[[str, str], None],
    secret: str | None = None,
) -> None:
    """Запускает HTTP сервер в daemon-потоке."""

    class Handler(_Handler):
        pass

    Handler.on_alert = staticmethod(on_alert)  # type: ignore[assignment]
    Handler.secret = secret  # type: ignore[assignment]

    server = HTTPServer(("0.0.0.0", port), Handler)

    t = threading.Thread(target=server.serve_forever, daemon=True, name="webhook-server")
    t.start()
    print(f"[webhook] listening on :{port}", flush=True)
