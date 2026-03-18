import asyncio
import json
import re
import ssl
import time
import logging
import urllib3
import websockets
from mattermostdriver import Driver
from mattermostdriver.websocket import Websocket
from pymongo.errors import ServerSelectionTimeoutError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from config import (
    MATTERMOST_URL, MATTERMOST_TOKEN, MATTERMOST_BOT_USER_ID,
    MATTERMOST_BOT_USERNAME, MENTION_REQUIRED,
    MONGODB_URI, HISTORY_LIMIT,
    MONITORING_BOT_USERNAME, ALERT_AUTO_SUMMARY,
)
from gigachat_client import GigaChatClient
from mongodb_client import (
    get_client, get_db, get_messages_collection, get_processed_posts_collection,
    ensure_indexes, try_claim_post, release_claim, save_message, get_thread_history,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Ты — опытный DevOps/SRE-инженер и ассистент команды эксплуатации. \
Твоя задача — помогать коллегам: писать bash/python/ansible скрипты, \
анализировать логи и алерты, предлагать решения проблем с серверами, \
базами данных (MongoDB, PostgreSQL), Kubernetes, мониторингом и CI/CD. \
Пиши рабочий код без лишних оговорок. \
Используй markdown: блоки кода оформляй в ```bash / ```python / ```yaml. \
Отвечай по-русски, кратко и по делу. Учитывай историю переписки.\
"""

ALERT_OPEN_PROMPT = """Ты — DevOps-ассистент. Пришёл новый алерт мониторинга.
Сформируй анализ в формате Mattermost Markdown. Структура СТРОГО такая:

#### 🚨 [Тип проблемы] — [хост]
**Сервис:** ...  **Критичность:** WARN/CRIT  **IP:** ...

**Проблема**
Одно предложение — что происходит и почему это важно.

**Вероятная причина**
Маркированный список (2–4 пункта) — наиболее вероятные причины исходя из типа алерта и сервиса.

**Рекомендуемые действия**
Нумерованный список (3–5 шагов) — конкретные команды или действия, с `inline code` где уместно.

Если есть ссылка на Grafana — добавь отдельной строкой: **📊 Grafana:** <ссылка>
Отвечай только на русском. Не добавляй ничего лишнего вне структуры."""

ALERT_CLOSE_PROMPT = """Ты — DevOps-ассистент. Алерт мониторинга закрыт.
Напиши краткое итоговое саммари в формате Mattermost Markdown:

#### ✅ Событие закрыто — [хост]
**Длительность:** ...  **Статус:** Устранено автоматически / Требовало вмешательства

Одно-два предложения: что произошло и чем завершилось.

Отвечай только на русском. Очень кратко."""

ALERT_CLOSED_RE = re.compile(r"событие закрыто", re.IGNORECASE)


def strip_mention(text: str, username: str) -> str:
    return re.sub(rf"^\s*@{re.escape(username)}\s*", "", text, flags=re.IGNORECASE).strip()


def get_thread_posts_from_mm(mm: Driver, root_id: str, exclude_post_id: str) -> str:
    """Возвращает текстовый дамп всех постов треда (кроме текущего) для контекста."""
    try:
        thread = mm.posts.get_thread(root_id)
        order = thread.get("order", [])
        posts_map = thread.get("posts", {})
        lines = []
        for pid in order:
            if pid == exclude_post_id:
                continue
            p = posts_map.get(pid, {})
            msg = (p.get("message") or "").strip()
            if not msg:
                continue
            lines.append(msg)
        if not lines:
            return ""
        return "Контекст треда:\n" + "\n---\n".join(lines)
    except Exception as e:
        log.warning("Не удалось получить тред из Mattermost: %s", e)
        return ""


class FixedWebsocket(Websocket):
    """Websocket с патчем SSL и надёжным переподключением."""
    async def connect(self, event_handler):
        ctx = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        url = "wss://{url}:{port}{basepath}/websocket".format(**self.options)
        self._alive = True
        while self._alive:
            try:
                async with websockets.connect(url, ssl=ctx) as ws:
                    await self._authenticate_websocket(ws, event_handler)
                    while self._alive:
                        try:
                            await self._start_loop(ws, event_handler)
                        except websockets.ConnectionClosedError:
                            break
            except Exception as e:
                log.warning("WebSocket error: %s — переподключение через 5с", e)
            if self._alive:
                await asyncio.sleep(5)


def run_bot():
    mm_host = MATTERMOST_URL.replace("https://", "").replace("http://", "").rstrip("/")
    mm = Driver({
        "url": mm_host,
        "token": MATTERMOST_TOKEN,
        "scheme": "https",
        "port": 443,
        "verify": False,
    })
    mm.login()
    log.info("Mattermost: подключён как @%s", MATTERMOST_BOT_USERNAME)

    try:
        mongo = get_client()
        db = get_db(mongo)
        coll = get_messages_collection(db)
        processed_coll = get_processed_posts_collection(db)
        ensure_indexes(coll, processed_coll)
        log.info("MongoDB: история диалогов подключена")
    except ServerSelectionTimeoutError as e:
        log.error("MongoDB недоступна (%s). Проверь MONGODB_URI в .env", MONGODB_URI)
        raise SystemExit(1) from e

    giga = GigaChatClient()

    monitoring_bot_user_id = None
    if ALERT_AUTO_SUMMARY and MONITORING_BOT_USERNAME:
        try:
            u = mm.users.get_user_by_username(MONITORING_BOT_USERNAME)
            monitoring_bot_user_id = u.get("id")
            log.info("Мониторинг-бот: @%s (%s)", MONITORING_BOT_USERNAME, monitoring_bot_user_id)
        except Exception as e:
            log.warning("Не удалось найти @%s: %s", MONITORING_BOT_USERNAME, e)

    async def post_alert_reaction(channel_id: str, root_id: str, current_post_id: str,
                                  claim_key: str, system_prompt: str, label: str):
        claimed = await asyncio.to_thread(try_claim_post, processed_coll, claim_key)
        if not claimed:
            return
        # Пауза: дать мониторинг-боту дослать остальные посты в тред
        await asyncio.sleep(2)
        thread_context = await asyncio.to_thread(
            get_thread_posts_from_mm, mm, root_id, current_post_id
        )
        if not thread_context:
            log.warning("%s: пустой тред для root_id=%s", label, root_id)
            return
        try:
            reply = await asyncio.to_thread(giga.chat, [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": thread_context},
            ])
            await asyncio.to_thread(
                mm.posts.create_post,
                {"channel_id": channel_id, "root_id": root_id, "message": reply},
            )
            log.info("%s в канале %s (root=%s)", label, channel_id, root_id)
        except Exception as e:
            log.exception("Ошибка %s: %s", label, e)
            # Освобождаем бронь чтобы следующий пост в треде смог повторить попытку
            await asyncio.to_thread(release_claim, processed_coll, claim_key)

    async def on_message(raw: str):
        try:
            event_data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return
        if event_data.get("event") != "posted":
            return

        post = event_data.get("data", {}).get("post")
        if isinstance(post, str):
            try:
                post = json.loads(post)
            except Exception:
                return
        if not post or not isinstance(post, dict):
            return

        user_id = post.get("user_id")
        channel_id = post.get("channel_id")
        message = post.get("message", "").strip()
        post_id = post.get("id")
        root_id = post.get("root_id") or post_id

        if user_id == MATTERMOST_BOT_USER_ID:
            return

        # Авто-реакция на алерты мониторинг-бота
        if monitoring_bot_user_id and user_id == monitoring_bot_user_id:
            if post.get("root_id"):  # только посты внутри треда, не root
                if ALERT_CLOSED_RE.search(message):
                    asyncio.ensure_future(post_alert_reaction(
                        channel_id, root_id, post_id,
                        claim_key=f"alert_close_{root_id}",
                        system_prompt=ALERT_CLOSE_PROMPT,
                        label="Итоговое саммари",
                    ))
                elif message:
                    # Детальный пост с текстом (Z-номер, описание) — анализ при открытии
                    # Пустые посты (Grafana-картинки) пропускаем
                    asyncio.ensure_future(post_alert_reaction(
                        channel_id, root_id, post_id,
                        claim_key=f"alert_open_{root_id}",
                        system_prompt=ALERT_OPEN_PROMPT,
                        label="Анализ алерта",
                    ))
            return

        # Обычный диалог
        is_thread = bool(post.get("root_id"))
        in_our_thread = False
        if is_thread:
            hist = await asyncio.to_thread(
                get_thread_history, coll, channel_id, root_id, 5
            )
            in_our_thread = any(m["role"] == "assistant" for m in hist)

        has_mention = f"@{MATTERMOST_BOT_USERNAME}".lower() in message.lower()
        if MENTION_REQUIRED and not has_mention and not in_our_thread:
            return

        user_message = strip_mention(message, MATTERMOST_BOT_USERNAME)
        if not user_message:
            return

        claimed = await asyncio.to_thread(try_claim_post, processed_coll, post_id)
        if not claimed:
            return

        try:
            thread_context, mongo_history = await asyncio.gather(
                asyncio.to_thread(get_thread_posts_from_mm, mm, root_id, post_id),
                asyncio.to_thread(get_thread_history, coll, channel_id, root_id, HISTORY_LIMIT),
            )

            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            if thread_context:
                messages.append({"role": "user", "content": thread_context})
                messages.append({"role": "assistant", "content": "Понял, вижу контекст треда."})
            messages += [{"role": m["role"], "content": m["content"]} for m in mongo_history]
            messages.append({"role": "user", "content": user_message})

            reply = await asyncio.to_thread(giga.chat, messages)

            await asyncio.gather(
                asyncio.to_thread(
                    save_message, coll,
                    channel_id=channel_id, thread_id=root_id,
                    user_id=user_id, role="user", content=user_message,
                ),
                asyncio.to_thread(
                    save_message, coll,
                    channel_id=channel_id, thread_id=root_id,
                    user_id=MATTERMOST_BOT_USER_ID, role="assistant", content=reply,
                ),
            )

            await asyncio.to_thread(
                mm.posts.create_post,
                {"channel_id": channel_id, "root_id": root_id, "message": reply},
            )
            log.info("Ответ в канале %s", channel_id)
        except Exception as e:
            log.exception("Ошибка: %s", e)
            try:
                await asyncio.to_thread(
                    mm.posts.create_post,
                    {"channel_id": channel_id, "root_id": root_id, "message": f"Ошибка: {e}"},
                )
            except Exception:
                pass

    try:
        mm.init_websocket(on_message, websocket_cls=FixedWebsocket)
    except KeyboardInterrupt:
        log.info("Остановка.")
    finally:
        mongo.close()
        mm.logout()


if __name__ == "__main__":
    while True:
        try:
            run_bot()
        except SystemExit:
            raise
        except KeyboardInterrupt:
            log.info("Выход.")
            break
        except Exception as e:
            log.exception("Критическая ошибка, перезапуск через 10с: %s", e)
            time.sleep(10)
