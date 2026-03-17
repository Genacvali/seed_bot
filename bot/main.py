"""
Точка входа: Mattermost -> на сообщение вызываем RAG pipeline -> ответ в чат.
"""
from __future__ import annotations

import re
import signal
import threading

from dotenv import load_dotenv

from .config import load_config
from .conversation_store import ConversationStore
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
    conv_store = ConversationStore(cfg)

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

        # 1. Кросс-тредовая история из MongoDB (долгосрочная память)
        cross_history: list[dict] = []
        try:
            cross_history = conv_store.get_cross_thread_history(
                ev.user_id, current_thread_id=root_id, limit=20
            )
        except Exception as e:
            print(f"[bot] cross-history error: {e}", flush=True)

        # 2. История текущего треда из Mattermost (последние 20 сообщений)
        thread_history: list[dict] = []
        try:
            thread_posts = mm.get_thread_posts(root_id)
            bot_id = cfg.mattermost_bot_user_id
            for p in thread_posts:
                if p.get("id") == ev.post_id:
                    continue
                role = "assistant" if p.get("user_id") == bot_id else "user"
                text = _strip_mentions(p.get("message") or "")
                if text:
                    thread_history.append({"role": role, "content": text})
            thread_history = thread_history[-20:]
        except Exception as e:
            print(f"[bot] thread-history error: {e}", flush=True)

        # Объединяем: сначала кросс-тредовая память, потом текущий тред
        history = cross_history + thread_history

        print(
            f"[bot] query: {prompt[:80]!r} "
            f"(cross={len(cross_history)}, thread={len(thread_history)})",
            flush=True,
        )
        try:
            answer = run_rag(cfg, prompt, history=history)
        except Exception as e:
            print(f"[bot] error: {e}", flush=True)
            answer = f"Ошибка: {e}"

        # Сохраняем диалог в MongoDB для долгосрочной памяти
        try:
            conv_store.save(
                user_id=ev.user_id,
                thread_id=root_id,
                channel_id=ev.channel_id,
                role="user",
                content=prompt,
            )
            conv_store.save(
                user_id=ev.user_id,
                thread_id=root_id,
                channel_id=ev.channel_id,
                role="assistant",
                content=answer,
            )
        except Exception as e:
            print(f"[bot] save-history error: {e}", flush=True)

        mm.create_post(ev.channel_id, answer, root_id=root_id)

    scope = f"channel={channel_id}" if channel_id else "all"
    print(f"[bot] RAG ready | {scope} | Qdrant={cfg.qdrant_collection} | Mongo={cfg.mongo_db}", flush=True)
    mm.listen_posts(
        on_post,
        channel_id=channel_id,
        stop_event=stop,
        current_ws_ref=current_ws_ref,
    )
