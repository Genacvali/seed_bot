from __future__ import annotations

import re
import signal
import threading
from datetime import datetime, timezone
from html import unescape

from dotenv import load_dotenv

from .alerts import AlertCorrelator, parse_alert_server
from .commands import CommandResult, handle as handle_command
from .config import load_config
from .confluence_client import ConfluenceClient
from .gigachat import GigaChatClient
from .knowledge import KnowledgeManager
from .mattermost import MattermostClient, PostEvent, ReactionEvent
from .queue_manager import QueueManager
from .scheduler import Scheduler
from .storage import Storage
from .webhook_server import start_webhook_server

_MENTION_RE = re.compile(r"@\w+")


def _strip_mentions(text: str) -> str:
    return _MENTION_RE.sub("", unescape(text or "")).strip()


def main() -> None:
    load_dotenv()
    cfg = load_config()

    # ── MongoDB ────────────────────────────────────────────────────────
    storage: Storage | None = None
    if cfg.mongo_uri:
        try:
            storage = Storage(cfg)
            if storage.ping():
                print(f"[storage] MongoDB connected: {cfg.mongo_db}", flush=True)
            else:
                print("[storage] MongoDB ping failed, running without storage", flush=True)
                storage = None
        except Exception as e:
            print(f"[storage] MongoDB init failed: {e}", flush=True)
            storage = None
    else:
        print("[storage] MONGO_URI not set, running without persistent storage", flush=True)

    # ── Confluence ─────────────────────────────────────────────────────
    confluence: ConfluenceClient | None = None
    if cfg.confluence_url and (cfg.confluence_token or (cfg.confluence_user and cfg.confluence_password)):
        try:
            confluence = ConfluenceClient(
                url=cfg.confluence_url,
                token=cfg.confluence_token,
                username=cfg.confluence_user,
                password=cfg.confluence_password,
                verify_tls=cfg.confluence_verify_tls,
            )
            # Прикрепляем список page_id для команды server
            confluence._cfg_pages = cfg.confluence_server_pages  # type: ignore[attr-defined]
            print(
                f"[confluence] enabled: {cfg.confluence_url} | pages={cfg.confluence_server_pages}",
                flush=True,
            )
        except Exception as e:
            print(f"[confluence] init failed: {e}", flush=True)
    else:
        print("[confluence] not configured (CONFLUENCE_URL / CONFLUENCE_TOKEN missing)", flush=True)

    # ── Core services ─────────────────────────────────────────────────
    mm = MattermostClient(cfg)
    gc = GigaChatClient(cfg, storage=storage)
    km = KnowledgeManager(gc, storage)
    correlator = AlertCorrelator()
    queue_mgr = QueueManager(rate_per_minute=cfg.gigachat_rate_limit)

    target_channel_id = mm.resolve_channel_id()

    # ── Reaction feedback ─────────────────────────────────────────────
    def on_reaction(ev: ReactionEvent) -> None:
        if ev.emoji_name in ("thumbsup", "+1", "thumbsdown", "-1", "white_check_mark"):
            if storage:
                storage.save_reaction_feedback(ev.post_id, ev.user_id, ev.emoji_name)
            print(f"[feedback] {ev.emoji_name} on post={ev.post_id[:8]} by {ev.user_id[:8]}", flush=True)

    mm.set_reaction_callback(on_reaction)

    # ── Stop signal ───────────────────────────────────────────────────
    stop = threading.Event()

    def _handle_stop(_sig: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    # ── Streaming helper ──────────────────────────────────────────────
    def post_streaming(
        channel_id: str,
        root_id: str,
        prompt: str,
        thread_id: str,
        user_id: str,
        extra_context: str,
    ) -> None:
        """Создаёт пост-заглушку, стримит ответ, редактирует по мере поступления."""
        placeholder = mm.create_post(channel_id, "⏳", root_id=root_id)
        post_id = placeholder["id"]

        buf: list[str] = []
        last_edit: list[float] = [0.0]

        def on_chunk(text: str) -> None:
            buf.append(text)
            now = __import__("time").monotonic()
            if now - last_edit[0] > 1.0:
                mm.edit_post(post_id, "".join(buf))
                last_edit[0] = now

        try:
            answer = gc.chat(
                prompt,
                thread_id=thread_id,
                user_id=user_id,
                extra_context=extra_context,
                on_chunk=on_chunk if cfg.gigachat_streaming else None,
            )
            mm.edit_post(post_id, answer)
        except Exception as e:
            print(f"[bot] GigaChat error: {e}", flush=True)
            if storage:
                storage.log_error(type(e).__name__, str(e), {"thread_id": thread_id})
            mm.edit_post(post_id, f"Не смог получить ответ от ИИ: `{type(e).__name__}`\n\n{e}")

    # ── Message handler ───────────────────────────────────────────────
    def on_post(ev: PostEvent) -> None:
        raw = ev.message
        prompt = _strip_mentions(raw)
        in_thread = bool(ev.root_id)
        thread_id = ev.root_id or ev.post_id
        root_id = ev.root_id or ev.post_id

        # Фильтр: отвечаем только при @упоминании (если не в треде / reply_all)
        if not cfg.reply_all and not in_thread:
            if cfg.mention_required and "@" not in raw:
                print("[bot] skip: no @mention in channel message", flush=True)
                return

        if not prompt:
            return

        if storage:
            storage.inc_usage(ev.user_id)

        def process() -> None:
            # ── 1. Команды (@seed help, find, tl;dr…) ─────────────────
            cmd_result = handle_command(
                raw_message=raw,
                clean_prompt=prompt,
                ev=ev,
                mm=mm,
                gc=gc,
                storage=storage,
                bot_username=cfg.mattermost_bot_username,
                confluence=confluence,
            )
            if cmd_result is not None:
                post = mm.create_post(ev.channel_id, cmd_result.text, root_id=root_id)
                if cmd_result.react_ok:
                    mm.add_reaction(post["id"], "white_check_mark")
                return

            # ── 2. Проверка: продолжение алерт-треда / resolution ──────
            if correlator.is_alert_thread(thread_id):
                if correlator.check_resolution(thread_id, prompt):
                    ask = mm.create_post(
                        ev.channel_id,
                        "Проблему решили? Сохранить как known issue? (`@seed save` чтобы записать)",
                        root_id=root_id,
                    )
                    return

            # ── 3. Классификация LLM: факт / алерт / вопрос ────────────
            km_response = km.process(prompt, ev.user_id)

            if km_response is not None:
                # Факт → ставим ✅ реакцию на исходный пост
                mm.add_reaction(ev.post_id, "white_check_mark")
                mm.create_post(ev.channel_id, km_response, root_id=root_id)

                # Если это был алерт — добавляем корреляцию и помечаем тред
                if km._last_msg_type == "alert":
                    server, atype = parse_alert_server(prompt)
                    corr = correlator.record(server, atype)
                    correlator.mark_pending_resolution(thread_id, {"server": server})
                    if storage:
                        storage.inc_alert(atype, server)
                    if corr.note:
                        mm.create_post(ev.channel_id, corr.note, root_id=root_id)
                return

            # ── 4. Обычный чат с контекстом из базы знаний ────────────
            extra_context = ""
            try:
                extra_context = km.build_context_prompt(prompt)
            except Exception as e:
                print(f"[knowledge] context error: {e}", flush=True)

            post_streaming(ev.channel_id, root_id, prompt, thread_id, ev.user_id, extra_context)

        priority = queue_mgr.PRIORITY_CHAT
        queue_mgr.submit(process, priority=priority)

    # ── Webhook server ─────────────────────────────────────────────────
    if cfg.webhook_port:
        def on_webhook_alert(text: str, source: str) -> None:
            if not target_channel_id:
                print("[webhook] no target_channel_id to post alert", flush=True)
                return
            # Создаём виртуальный PostEvent и кидаем в обработчик
            fake_post = mm.create_post(
                target_channel_id,
                f"🔔 Алерт из `{source}`:\n{text}",
            )
            ev = PostEvent(
                post_id=fake_post["id"],
                channel_id=target_channel_id,
                user_id="webhook",
                message=text,
                root_id=None,
            )
            on_post(ev)

        start_webhook_server(cfg.webhook_port, on_webhook_alert, secret=cfg.webhook_secret)

    # ── Daily digest ──────────────────────────────────────────────────
    if cfg.digest_channel_id and storage:
        scheduler = Scheduler()

        def send_digest() -> None:
            today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
            try:
                data = storage.get_daily_digest(today)
            except Exception as e:
                print(f"[digest] error: {e}", flush=True)
                return

            lines = [
                f"### 📊 Дайджест SEED за {today}",
                "",
                f"- Запросов к боту: **{data['total_requests']}**",
                f"- Новых фактов в базе знаний: **{data['new_facts']}**",
                f"- Ошибок бота: **{data['errors_count']}**",
            ]
            if data["top_alerts"]:
                lines.append("\n**Топ алертов за день:**")
                for a in data["top_alerts"]:
                    lines.append(f"- `{a.get('server')}` — {a.get('alert_type', '')[:50]} ({a.get('count')}x)")

            mm.create_post(cfg.digest_channel_id, "\n".join(lines))  # type: ignore[arg-type]

        scheduler.add_daily(cfg.digest_time, send_digest)
        scheduler.start()
        print(f"[scheduler] daily digest at {cfg.digest_time} UTC → channel {cfg.digest_channel_id}", flush=True)

    # ── Start ─────────────────────────────────────────────────────────
    scope = f"channel={target_channel_id}" if target_channel_id else "all channels"
    print(
        f"[bot] ready | {scope} | reply_all={cfg.reply_all} | "
        f"streaming={cfg.gigachat_streaming} | "
        f"rate={cfg.gigachat_rate_limit}/min | "
        f"knowledge={'mongo' if storage else 'memory'} | "
        f"confluence={'on' if confluence else 'off'} | "
        f"webhook={':{cfg.webhook_port}' if cfg.webhook_port else 'off'}",
        flush=True,
    )
    mm.listen_posts(on_post, channel_id=target_channel_id, stop_event=stop)


if __name__ == "__main__":
    main()
