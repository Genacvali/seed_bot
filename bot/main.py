"""
Точка входа: Mattermost -> на сообщение вызываем RAG pipeline -> ответ в чат.
"""
from __future__ import annotations

import re
import signal
import threading

from dotenv import load_dotenv

from .config import load_config
from .mattermost import MattermostClient, PostEvent
from .pipeline import run_rag

_MENTION_RE = re.compile(r"@\w+")


def _strip_mentions(text: str) -> str:
    return _MENTION_RE.sub("", (text or "").strip()).strip()


def main() -> None:
    load_dotenv()
    cfg = load_config()

    mm = MattermostClient(cfg)
    channel_id = mm.resolve_channel_id()

    stop = threading.Event()
    current_ws_ref: list = []

    def on_stop(_sig: int, _frame: object) -> None:
        stop.set()
        if current_ws_ref:
            try:
                current_ws_ref[0].close()
            except Exception:
                pass

    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGTERM, on_stop)

    def on_post(ev: PostEvent) -> None:
        raw = ev.message
        prompt = _strip_mentions(raw)
        if not prompt:
            return
        in_thread = bool(ev.root_id)
        root_id = ev.root_id or ev.post_id

        if not cfg.reply_all and not in_thread and cfg.mention_required:
            if "@" not in raw:
                print("[bot] skip: no @mention", flush=True)
                return

        print(f"[bot] query: {prompt[:80]!r}", flush=True)
        try:
            answer = run_rag(cfg, prompt)
        except Exception as e:
            print(f"[bot] error: {e}", flush=True)
            answer = f"Ошибка: {e}"
        mm.create_post(ev.channel_id, answer, root_id=root_id)

    scope = f"channel={channel_id}" if channel_id else "all"
    print(f"[bot] RAG ready | {scope} | Qdrant={cfg.qdrant_collection} | Mongo={cfg.mongo_db}", flush=True)
    mm.listen_posts(
        on_post,
        channel_id=channel_id,
        stop_event=stop,
        current_ws_ref=current_ws_ref,
    )
