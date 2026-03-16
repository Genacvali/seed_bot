from __future__ import annotations

import re
import signal
import threading
import time
from datetime import datetime, timezone
from html import unescape

from dotenv import load_dotenv

from .alerts import AlertCorrelator, parse_alert_server
from .commands import CommandResult, handle as handle_command
from .config import load_config
from .confluence_client import ConfluenceClient
from .confluence_intent import (
    IntentType,
    build_server_context,
    detect as detect_confluence_intent,
    format_docs_list,
    format_indexed_page,
    format_server_not_found,
)
from .gigachat import GigaChatClient
from .knowledge import KnowledgeManager
from .mattermost import MattermostClient, PostEvent, ReactionEvent
from .queue_manager import QueueManager
from .scheduler import Scheduler
from .storage import Storage
from .webhook_server import start_webhook_server

_MENTION_RE = re.compile(r"@\w+")

# Кеш: thread_id → (list[ServerRecord], timestamp)
# Хранит последние найденные серверы в треде для поддержки follow-up вопросов
_THREAD_SERVER_CTX: dict[str, tuple[list, float]] = {}
_CTX_TTL = 3600.0  # 1 час


def _cache_thread_records(thread_id: str, records: list) -> None:
    _THREAD_SERVER_CTX[thread_id] = (records, time.monotonic())


def _get_thread_records(thread_id: str) -> list:
    entry = _THREAD_SERVER_CTX.get(thread_id)
    if entry and time.monotonic() - entry[1] < _CTX_TTL:
        return entry[0]
    return []


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
            # Fallback: страницы из .env (если MongoDB нет или пуста)
            confluence._cfg_pages = cfg.confluence_server_pages  # type: ignore[attr-defined]

            # Считаем сколько страниц в MongoDB
            mongo_pages = storage.get_confluence_page_ids() if storage else []
            total_pages = len(mongo_pages) or len(cfg.confluence_server_pages)
            print(
                f"[confluence] enabled: {cfg.confluence_url} | "
                f"pages in db={len(mongo_pages)} | fallback config={len(cfg.confluence_server_pages)}",
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

            # ── 2. Confluence: естественный язык ──────────────────────
            if confluence:
                intent = detect_confluence_intent(prompt)

                # 2а. Пользователь скинул ссылку на Confluence
                if intent.intent == IntentType.CONFLUENCE_URL and intent.urls:
                    for url in intent.urls:
                        try:
                            info = confluence.resolve_page_url(url)
                            is_new = True
                            if storage:
                                is_new = storage.add_confluence_page(
                                    page_id=info["page_id"],
                                    title=info["title"],
                                    space_key=info["space_key"],
                                    url=url,
                                    added_by=ev.user_id,
                                )
                            else:
                                # fallback: в памяти
                                pages = getattr(confluence, "_cfg_pages", [])
                                if info["page_id"] not in pages:
                                    pages.append(info["page_id"])
                                    confluence._cfg_pages = pages  # type: ignore[attr-defined]
                                else:
                                    is_new = False
                            reply = format_indexed_page(
                                info["title"], info["page_id"], url, is_new
                            )
                            mm.add_reaction(ev.post_id, "white_check_mark")
                        except Exception as e:
                            reply = f"Не смог проиндексировать страницу: `{e}`"
                        mm.create_post(ev.channel_id, reply, root_id=root_id)
                    return

                # 2б. «Какие у тебя документации?»
                if intent.intent == IntentType.DOCS_LIST:
                    if storage:
                        try:
                            pages = storage.list_confluence_pages()
                        except Exception:
                            pages = []
                    else:
                        pages = [
                            {"page_id": pid, "title": pid, "space_key": "", "url": ""}
                            for pid in getattr(confluence, "_cfg_pages", [])
                        ]
                    mm.create_post(ev.channel_id, format_docs_list(pages), root_id=root_id)
                    return

                # 2в. Вопрос о конкретном сервере (явное имя в сообщении)
                if intent.intent == IntentType.SERVER_LOOKUP and intent.hostnames:
                    page_ids: list[str] = []
                    if storage:
                        try:
                            page_ids = storage.get_confluence_page_ids()
                        except Exception:
                            pass
                    if not page_ids:
                        page_ids = getattr(confluence, "_cfg_pages", [])

                    # Нет ни одной страницы — объясняем как добавить, не падаем дальше
                    if not page_ids:
                        names = ", ".join(f"`{h}`" for h in intent.hostnames)
                        mm.create_post(
                            ev.channel_id,
                            f"Хочу найти {names} в документации, но ни одной страницы Confluence ещё не добавлено.\n"
                            f"Скинь ссылку — проиндексирую:\n"
                            f"> @{cfg.mattermost_bot_username} вот доки https://confluence.example.com/display/SPACE/Page",
                            root_id=root_id,
                        )
                        return

                    if page_ids:
                        all_records = []
                        for hostname in intent.hostnames:
                            for page_id in page_ids:
                                try:
                                    found = confluence.search_all_children(page_id, hostname)
                                    all_records.extend(found)
                                except Exception as e:
                                    print(f"[confluence] search error: {e}", flush=True)

                        # Дедупликация
                        seen: set[str] = set()
                        unique_records = []
                        for rec in all_records:
                            key = rec.server.lower()
                            if key not in seen:
                                seen.add(key)
                                unique_records.append(rec)

                        if unique_records:
                            # Сохраняем в кеш треда для follow-up вопросов
                            _cache_thread_records(thread_id, unique_records)

                            # Загружаем ВСЕ записи со страниц, где нашли сервер —
                            # чтобы определить полный кластер, а не только найденные ноды
                            page_ids_found = list({
                                rec.page_id for rec in unique_records if rec.page_id
                            })
                            full_page_records: list = []
                            contacts_by_page: dict[str, list[str]] = {}
                            for pid in page_ids_found:
                                try:
                                    full_page_records.extend(confluence.parse_servers(pid))
                                    contacts_by_page[pid] = confluence.get_contacts(pid)
                                except Exception as e:
                                    print(f"[confluence] full page load error pid={pid}: {e}", flush=True)

                            confluence_ctx = build_server_context(
                                unique_records,
                                ", ".join(intent.hostnames),
                                all_records=full_page_records or unique_records,
                                contacts_by_page=contacts_by_page or None,
                            )
                            extra_context = confluence_ctx
                            try:
                                extra_context = confluence_ctx + "\n\n" + km.build_context_prompt(prompt)
                            except Exception:
                                pass
                            post_streaming(
                                ev.channel_id, root_id, prompt, thread_id, ev.user_id, extra_context
                            )
                            return
                        elif page_ids:
                            # Страниц нет, но сервер не найден
                            mm.create_post(
                                ev.channel_id,
                                format_server_not_found(intent.hostnames),
                                root_id=root_id,
                            )
                            return

                # 2г. Follow-up в треде: нет имени сервера, но в кеше есть контекст
                # Например: "а он в кластере?" после вопроса о конкретном сервере
                if intent.intent == IntentType.NONE:
                    cached = _get_thread_records(thread_id)
                    if cached and in_thread:
                        contacts_by_page_cached: dict[str, list[str]] = {}
                        for rec in cached:
                            if rec.page_id and rec.page_id not in contacts_by_page_cached:
                                try:
                                    contacts_by_page_cached[rec.page_id] = confluence.get_contacts(rec.page_id)
                                except Exception:
                                    contacts_by_page_cached[rec.page_id] = []
                        confluence_ctx = build_server_context(
                            cached,
                            cached[0].short_name() if cached else "",
                            all_records=cached,
                            contacts_by_page=contacts_by_page_cached or None,
                        )
                        extra_context = confluence_ctx
                        try:
                            extra_context = confluence_ctx + "\n\n" + km.build_context_prompt(prompt)
                        except Exception:
                            pass
                        post_streaming(
                            ev.channel_id, root_id, prompt, thread_id, ev.user_id, extra_context
                        )
                        return

            # ── 3. Проверка: продолжение алерт-треда / resolution ──────
            if correlator.is_alert_thread(thread_id):
                if correlator.check_resolution(thread_id, prompt):
                    mm.create_post(
                        ev.channel_id,
                        "Проблему решили? Сохранить как known issue? (`@seed save` чтобы записать)",
                        root_id=root_id,
                    )
                    return

            # ── 4. Классификация LLM: факт / алерт / вопрос ────────────
            km_response = km.process(prompt, ev.user_id)

            if km_response is not None:
                mm.add_reaction(ev.post_id, "white_check_mark")
                mm.create_post(ev.channel_id, km_response, root_id=root_id)

                if km._last_msg_type == "alert":
                    server, atype = parse_alert_server(prompt)
                    corr = correlator.record(server, atype)
                    correlator.mark_pending_resolution(thread_id, {"server": server})
                    if storage:
                        storage.inc_alert(atype, server)
                    if corr.note:
                        mm.create_post(ev.channel_id, corr.note, root_id=root_id)
                return

            # ── 5. Обычный чат с контекстом из базы знаний ────────────
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
