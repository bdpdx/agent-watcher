"""The agent-watcher channels: one private Slack channel per project, `<project>__agent-watcher`.

The watcher never creates them; each project's GM does and invites the watcher bots. The projects this
watcher serves are exactly the `*__agent-watcher` channels its bot belongs to, found at startup and
whenever the bot is added to or removed from a channel. Messages from any other channel are ignored.

Bot scopes used: channels:history, groups:history, groups:read, channels:read, users:read, chat:write,
groups:write (to invite a new GM). Events: message.channels, message.groups, member_joined_channel.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Awaitable, Callable, Protocol

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from .config import CHANNEL_SUFFIX, WatcherConfig
from .events import E, Event, Tier

log = logging.getLogger(__name__)


class Poster(Protocol):
    """What the supervisor needs from the channel (faked in tests)."""

    name: str  # this watcher's bot name, e.g. agent-watcher-a
    user_id: str

    def projects(self) -> set[str]: ...

    def ready(self, project: str) -> bool: ...

    async def post(self, project: str, text: str, thread_ts: str | None = None) -> str | None: ...

    async def invite(self, project: str, agent_name: str) -> bool: ...


def read_env_file(path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out


def slack_tokens(cfg: WatcherConfig) -> tuple[str, str] | None:
    """(bot token, app token) from the environment, else from the slack env file.

    Only the bot (xoxb) and app (xapp) tokens are used. A user token (xoxp) in the
    same file is deliberately ignored: it could act as the owner.
    """
    bot, app = os.environ.get(cfg.slack_bot_token_env), os.environ.get(cfg.slack_app_token_env)
    if (not bot or not app) and cfg.slack_env_file and cfg.slack_env_file.exists():
        env = read_env_file(cfg.slack_env_file)
        bot = bot or env.get("SLACK_MCP_XOXB_TOKEN")
        app = app or env.get("SLACK_MCP_XAPP_TOKEN")
    if bot and bot.startswith("xoxb-") and app and app.startswith("xapp-"):
        return bot, app
    return None


def identity_name(user: dict) -> str:
    """The name agents go by. slack-mcp-server agent bots carry it in real_name (e.g. "claude-a"),
    while `name` is a mangled handle ("claudeb"). Top-level real_name wins over the profile's,
    which stale duplicate bots also carry."""
    prof = user.get("profile") or {}
    return user.get("real_name") or prof.get("real_name") or prof.get("display_name") or user.get("name") or ""


class SlackChannel:
    def __init__(self, cfg: WatcherConfig, tokens: tuple[str, str], emit: Callable[[Event], Awaitable[None]],
                 gm: Callable[[str], str | None], audit: Callable[..., None]):
        self.cfg = cfg
        self.emit = emit
        self.gm = gm
        self.audit = audit
        self._app_token = tokens[1]
        self.app = AsyncApp(token=tokens[0])
        self.name = "agent-watcher"
        self.user_id = ""
        self.bot_id = ""
        self.channels: dict[str, str] = {}  # project -> channel id
        self._names: dict[str, tuple[str, bool]] = {}  # user id -> (name, is_bot)
        self._ids_by_name: dict[str, str] = {}
        self.app.event("message")(self._on_message)
        for ev in ("member_joined_channel", "member_left_channel", "channel_left", "group_left"):
            self.app.event(ev)(self._on_membership)

    # ---- Poster -----------------------------------------------------------------------------------

    def projects(self) -> set[str]:
        return set(self.channels)

    def ready(self, project: str) -> bool:
        return project in self.channels

    def _project_of(self, channel_id: str) -> str | None:
        return next((p for p, c in self.channels.items() if c == channel_id), None)

    async def post(self, project: str, text: str, thread_ts: str | None = None) -> str | None:
        mode = self.cfg.mode("channel")
        if mode == "off":
            return None
        cid = self.channels.get(project)
        if mode == "dry" or not cid:
            self.audit("-", "channel", mode if cid else "dry", "POST", f"[{project}] {text}",
                       "" if cid else "[no channel]")
            return None
        try:
            resp = await self.app.client.chat_postMessage(channel=cid, text=text, thread_ts=thread_ts,
                                                          unfurl_links=False, unfurl_media=False)
            return resp.get("ts")
        except Exception:  # noqa: BLE001
            log.exception("posting to #%s%s failed", project, CHANNEL_SUFFIX)
            return None

    async def invite(self, project: str, agent_name: str) -> bool:
        cid = self.channels.get(project)
        if not cid or self.cfg.mode("channel") != "live":
            return False
        uid = await self._user_id_for(agent_name)
        if not uid:
            log.warning("no Slack user named %s to invite", agent_name)
            return False
        try:
            await self.app.client.conversations_invite(channel=cid, users=uid)
            return True
        except Exception as e:  # noqa: BLE001 - already_in_channel is the common case
            if "already_in_channel" not in str(e):
                log.warning("inviting %s failed: %s", agent_name, e)
            return False

    # ---- discovery --------------------------------------------------------------------------------

    async def refresh_channels(self) -> None:
        found: dict[str, str] = {}
        cursor = None
        while True:
            resp = await self.app.client.users_conversations(types="private_channel,public_channel",
                                                             exclude_archived=True, limit=200, cursor=cursor)
            for ch in resp.get("channels", []):
                name = ch.get("name", "")
                if name.endswith(CHANNEL_SUFFIX) and len(name) > len(CHANNEL_SUFFIX):
                    found[name[: -len(CHANNEL_SUFFIX)]] = ch["id"]
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        added = {p: c for p, c in found.items() if self.channels.get(p) != c}
        removed = set(self.channels) - set(found)
        self.channels = found
        for p in removed:
            log.info("no longer in #%s%s", p, CHANNEL_SUFFIX)
            await self.emit(Event(E.CHANNEL_READY, None, {"project": p, "gone": True}, source="slack"))
        for p, c in added.items():
            log.info("serving project %s via #%s%s (%s)", p, p, CHANNEL_SUFFIX, c)
            await self.emit(Event(E.CHANNEL_READY, None, {"project": p, "channel": c}, source="slack"))

    async def _user_id_for(self, name: str) -> str | None:
        if name in self._ids_by_name:
            return self._ids_by_name[name]
        cursor = None
        while True:
            resp = await self.app.client.users_list(limit=200, cursor=cursor)
            for u in resp.get("members", []):
                if u.get("deleted"):
                    continue
                if u.get("real_name"):  # authoritative; overrides a stale duplicate's profile name
                    self._ids_by_name[u["real_name"]] = u["id"]
                for n in ((u.get("profile") or {}).get("real_name"), u.get("name")):
                    if n:
                        self._ids_by_name.setdefault(n, u["id"])
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor or name in self._ids_by_name:
                return self._ids_by_name.get(name)

    async def _who(self, user_id: str) -> tuple[str, bool]:
        if user_id not in self._names:
            try:
                info = (await self.app.client.users_info(user=user_id))["user"]
                self._names[user_id] = (identity_name(info) or user_id, bool(info.get("is_bot")))
            except Exception:  # noqa: BLE001
                log.exception("users.info failed for %s", user_id)
                return user_id, False
        return self._names[user_id]

    def tier_of(self, user_id: str, name: str, project: str) -> Tier:
        if user_id == self.cfg.owner_slack_id:
            return Tier.OWNER
        if user_id in self.cfg.authorized_humans:
            return Tier.AUTHORIZED_HUMAN
        if name and name == self.gm(project):
            return Tier.GM
        return Tier.OTHER

    # ---- Slack events -----------------------------------------------------------------------------

    async def _on_membership(self, event: dict) -> None:
        if event.get("type") == "channel_left" or event.get("user") == self.user_id:
            await self.refresh_channels()

    async def _on_message(self, event: dict) -> None:
        project = self._project_of(event.get("channel", ""))
        if project is None:
            return
        if event.get("subtype") not in (None, "bot_message", "thread_broadcast"):
            return
        user_id = event.get("user") or ""
        if user_id == self.user_id or (self.bot_id and event.get("bot_id") == self.bot_id):
            return
        name, is_bot = await self._who(user_id) if user_id else (event.get("username") or "?", True)
        await self.emit(Event(E.MESSAGE, None, {
            "project": project,
            "text": event.get("text", ""),
            "sender": name,
            "user": user_id,
            "is_bot": is_bot or bool(event.get("bot_id")),
            "tier": self.tier_of(user_id, name, project),
            "ts": event.get("ts"),
            "thread_ts": event.get("thread_ts") if event.get("thread_ts") != event.get("ts") else None,
        }, ts=time.time(), source="slack"))

    async def run(self) -> None:
        auth = await self.app.client.auth_test()
        self.user_id, self.bot_id = auth["user_id"], auth.get("bot_id", "")
        self.name = auth.get("user") or self.name  # this bot's handle, e.g. agent-watcher-a
        await self.refresh_channels()
        if not self.channels:
            log.warning("not a member of any *%s channel yet; waiting for an invite", CHANNEL_SUFFIX)
        handler = AsyncSocketModeHandler(self.app, self._app_token)
        await handler.start_async()
