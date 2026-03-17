from __future__ import annotations

import json
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests
import websocket

from .config import Config


@dataclass(frozen=True)
class PostEvent:
    post_id: str
    channel_id: str
    user_id: str
    message: str
    root_id: str | None


@dataclass(frozen=True)
class ReactionEvent:
    post_id: str
    user_id: str
    emoji_name: str


class MattermostClient:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._base = cfg.mattermost_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {cfg.mattermost_token}"})
        self._session.verify = cfg.mattermost_verify_tls
        if not cfg.mattermost_verify_tls:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._on_reaction: Callable[[ReactionEvent], None] | None = None

    def _api(self, path: str) -> str:
        return f"{self._base}/api/v4{path}"

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        username = username.lstrip("@")
        try:
            r = self._session.get(self._api(f"/users/username/{username}"), timeout=10)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def resolve_mention(self, username: str) -> str | None:
        """Возвращает user_id по @username, или None."""
        user = self.get_user_by_username(username)
        return user["id"] if user else None

    # ------------------------------------------------------------------
    # Teams / Channels
    # ------------------------------------------------------------------

    def get_team_by_name(self, name: str) -> dict[str, Any]:
        r = self._session.get(self._api(f"/teams/name/{name}"), timeout=30)
        r.raise_for_status()
        return r.json()

    def get_channel_by_name(self, team_id: str, channel_name: str) -> dict[str, Any]:
        r = self._session.get(self._api(f"/teams/{team_id}/channels/name/{channel_name}"), timeout=30)
        r.raise_for_status()
        return r.json()

    def resolve_channel_id(self) -> str | None:
        if self._cfg.mattermost_channel_id:
            return self._cfg.mattermost_channel_id
        if self._cfg.mattermost_team and self._cfg.mattermost_channel:
            team = self.get_team_by_name(self._cfg.mattermost_team)
            channel = self.get_channel_by_name(team["id"], self._cfg.mattermost_channel)
            return channel["id"]
        return None

    # ------------------------------------------------------------------
    # Posts
    # ------------------------------------------------------------------

    def create_post(self, channel_id: str, message: str, root_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"channel_id": channel_id, "message": message}
        if root_id:
            payload["root_id"] = root_id
        r = self._session.post(self._api("/posts"), json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    def edit_post(self, post_id: str, message: str) -> dict[str, Any]:
        r = self._session.put(self._api(f"/posts/{post_id}/patch"), json={"message": message}, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_post(self, post_id: str) -> dict[str, Any]:
        r = self._session.get(self._api(f"/posts/{post_id}"), timeout=30)
        r.raise_for_status()
        return r.json()

    def get_thread_posts(self, root_post_id: str) -> list[dict[str, Any]]:
        """Возвращает все посты треда в хронологическом порядке."""
        r = self._session.get(self._api(f"/posts/{root_post_id}/thread"), timeout=30)
        r.raise_for_status()
        data = r.json()
        posts = data.get("posts", {})
        order = data.get("order", list(posts.keys()))
        return [posts[pid] for pid in order if pid in posts]

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------

    def add_reaction(self, post_id: str, emoji_name: str) -> None:
        payload = {
            "user_id": self._cfg.mattermost_bot_user_id,
            "post_id": post_id,
            "emoji_name": emoji_name,
        }
        try:
            r = self._session.post(self._api("/reactions"), json=payload, timeout=10)
            r.raise_for_status()
        except Exception as e:
            print(f"[mattermost] add_reaction failed: {e}", flush=True)

    def remove_reaction(self, post_id: str, emoji_name: str) -> None:
        try:
            r = self._session.delete(
                self._api(f"/users/{self._cfg.mattermost_bot_user_id}/posts/{post_id}/reactions/{emoji_name}"),
                timeout=10,
            )
            r.raise_for_status()
        except Exception as e:
            print(f"[mattermost] remove_reaction failed: {e}", flush=True)

    def set_reaction_callback(self, callback: Callable[[ReactionEvent], None]) -> None:
        self._on_reaction = callback

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    def ws_url(self) -> str:
        if self._base.startswith("https://"):
            return self._base.replace("https://", "wss://", 1) + "/api/v4/websocket"
        if self._base.startswith("http://"):
            return self._base.replace("http://", "ws://", 1) + "/api/v4/websocket"
        return "wss://" + self._base.lstrip("/") + "/api/v4/websocket"

    def _sslopt(self) -> dict[str, Any] | None:
        if not self._cfg.mattermost_verify_tls and self.ws_url().startswith("wss://"):
            return {"cert_reqs": ssl.CERT_NONE}
        return None

    def listen_posts(
        self,
        on_post: Callable[[PostEvent], None],
        *,
        channel_id: str | None,
        stop_event: threading.Event,
        current_ws_ref: list | None = None,
    ) -> None:
        """current_ws_ref: optional list to hold the active WebSocketApp; if set, caller may
        call .close() on it (e.g. from a signal handler) to make run_forever() return."""
        delay = 2
        seq = [1]

        def on_open(ws: websocket.WebSocketApp) -> None:
            auth_msg = json.dumps({
                "seq": seq[0],
                "action": "authentication_challenge",
                "data": {"token": self._cfg.mattermost_token},
            })
            seq[0] += 1
            ws.send(auth_msg)
            print("[mattermost] ws connected, auth sent", flush=True)

        def on_error(_ws: websocket.WebSocketApp, err: object) -> None:
            print(f"[mattermost] ws error: {err}", flush=True)

        def on_close(_ws: websocket.WebSocketApp, code: object, reason: object) -> None:
            print(f"[mattermost] ws closed: {code} {reason}", flush=True)

        while not stop_event.is_set():
            try:
                ws = websocket.WebSocketApp(
                    self.ws_url(),
                    on_open=on_open,
                    on_message=lambda _ws, msg: self._on_ws_message(msg, on_post, channel_id),
                    on_error=on_error,
                    on_close=on_close,
                )
                if current_ws_ref is not None:
                    current_ws_ref.clear()
                    current_ws_ref.append(ws)
                try:
                    ws.run_forever(ping_interval=20, ping_timeout=10, sslopt=self._sslopt())
                finally:
                    if current_ws_ref is not None:
                        current_ws_ref.clear()
                delay = 2
            except Exception as e:
                print(f"[mattermost] ws exception: {e}", flush=True)

            if not stop_event.is_set():
                print(f"[mattermost] reconnecting in {delay}s...", flush=True)
                time.sleep(delay)
                delay = min(delay * 2, 60)

    def _on_ws_message(self, msg: str, on_post: Callable[[PostEvent], None], channel_id: str | None) -> None:
        try:
            payload = json.loads(msg)
        except Exception:
            return

        event = payload.get("event")

        if event in ("hello", "status_change"):
            print(f"[mattermost] ws event: {event}", flush=True)
            return

        # Реакции
        if event == "reaction_added":
            self._handle_reaction_event(payload)
            return

        if event != "posted":
            return

        data = payload.get("data") or {}
        post_raw = data.get("post")
        if not post_raw:
            return

        try:
            post = json.loads(post_raw)
        except Exception:
            return

        post_channel_id = post.get("channel_id")
        user_id = post.get("user_id")
        message = post.get("message") or ""

        print(
            f"[mattermost] posted: channel={post_channel_id} user={user_id} "
            f"msg={message[:80]!r}",
            flush=True,
        )

        if not post_channel_id:
            return
        if channel_id is not None and post_channel_id != channel_id:
            print(f"[mattermost] skip: wrong channel (want {channel_id})", flush=True)
            return

        if not user_id or user_id == self._cfg.mattermost_bot_user_id:
            print("[mattermost] skip: own message", flush=True)
            return

        post_id = post.get("id")
        root_id = post.get("root_id") or None

        if not post_id:
            return

        on_post(PostEvent(
            post_id=post_id,
            channel_id=post_channel_id,
            user_id=user_id,
            message=message,
            root_id=root_id,
        ))

    def _handle_reaction_event(self, payload: dict[str, Any]) -> None:
        if not self._on_reaction:
            return
        data = payload.get("data") or {}
        reaction_raw = data.get("reaction")
        if not reaction_raw:
            return
        try:
            r = json.loads(reaction_raw) if isinstance(reaction_raw, str) else reaction_raw
        except Exception:
            return
        user_id = r.get("user_id")
        post_id = r.get("post_id")
        emoji = r.get("emoji_name")
        if user_id and post_id and emoji and user_id != self._cfg.mattermost_bot_user_id:
            self._on_reaction(ReactionEvent(post_id=post_id, user_id=user_id, emoji_name=emoji))
