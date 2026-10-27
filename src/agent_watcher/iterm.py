"""iTerm2 Python API adapter: binds agents to tabs, reads screens and job names, sends keystrokes."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from typing import Awaitable, Callable

import iterm2

from .config import WatcherConfig
from .events import E, Event

log = logging.getLogger(__name__)

TAG = "user.agentWatcherAgent"


def session_guid(iterm_session_id: str | None) -> str | None:
    """$ITERM_SESSION_ID is "w0t0p0:<GUID>"; the API's session_id is the GUID."""
    if not iterm_session_id:
        return None
    return iterm_session_id.split(":", 1)[-1] or None


class ITermTerminal:
    def __init__(self, cfg: WatcherConfig, emit: Callable[[Event], Awaitable[None]],
                 on_change: Callable[[], None] = lambda: None):
        self.cfg = cfg
        self.emit = emit
        self.on_change = on_change  # called when tabs open or close, so discovery rescans
        self.conn: iterm2.Connection | None = None
        self.app: iterm2.App | None = None
        self.sessions: dict[str, str] = {}  # agent -> session_id
        self._key_tasks: dict[str, asyncio.Task] = {}
        self._retry_at = 0.0  # monotonic time before which a failed connection isn't retried

    # ---- Terminal protocol --------------------------------------------------------------------

    def bound(self, agent: str) -> bool:
        return agent in self.sessions and self._session(agent) is not None

    def _session(self, agent: str) -> iterm2.Session | None:
        sid = self.sessions.get(agent)
        return self.app.get_session_by_id(sid) if (sid and self.app) else None

    async def send_raw(self, agent: str, data: str) -> None:
        s = self._session(agent)
        if s is None:
            raise RuntimeError(f"no iTerm2 session bound for {agent}")
        await s.async_send_text(data, suppress_broadcast=True)

    async def job_name(self, agent: str) -> str | None:
        s = self._session(agent)
        return await s.async_get_variable("jobName") if s else None

    async def screen_text(self, agent: str) -> str:
        s = self._session(agent)
        if s is None:
            return ""
        contents = await s.async_get_screen_contents()
        return "\n".join(contents.line(i).string for i in range(contents.number_of_lines))

    # ---- binding ---------------------------------------------------------------------------------

    async def bind(self, agent: str, session_id: str) -> None:
        if self.sessions.get(agent) == session_id or not await self._connect():
            return
        await self.app.async_refresh()
        s = self.app.get_session_by_id(session_id)
        if s is None:
            return
        for other, sid in list(self.sessions.items()):
            if sid == session_id and other != agent:
                del self.sessions[other]
        self.sessions[agent] = session_id
        await s.async_set_variable(TAG, agent)
        log.info("bound %s to iTerm2 session %s", agent, session_id)
        self._watch_keys(agent, session_id)

    def unbind(self, agent: str) -> None:
        self.sessions.pop(agent, None)
        task = self._key_tasks.pop(agent, None)
        if task:
            task.cancel()

    def _watch_keys(self, agent: str, session_id: str) -> None:
        old = self._key_tasks.pop(agent, None)
        if old:
            old.cancel()

        async def run():
            async with iterm2.KeystrokeMonitor(self.conn, session=session_id) as mon:
                while True:
                    await mon.async_get()
                    await self.emit(Event(E.OWNER_KEYSTROKE, agent, source="iterm"))

        self._key_tasks[agent] = asyncio.create_task(run(), name=f"keys:{agent}")

    async def _watch_terminations(self) -> None:
        async with iterm2.SessionTerminationMonitor(self.conn) as mon:
            while True:
                sid = await mon.async_get()
                for agent, s in list(self.sessions.items()):
                    if s == sid:
                        self.unbind(agent)
                        log.warning("iTerm2 session for %s closed", agent)
                self.on_change()

    async def _watch_new_sessions(self) -> None:
        async with iterm2.NewSessionMonitor(self.conn) as mon:
            while True:
                await mon.async_get()
                await asyncio.sleep(3)  # let the shell and agent start
                self.on_change()

    async def _connect(self) -> bool:
        """Connect to iTerm2 if it's running. Never launches it (the API's cookie request would); a watcher
        whose agents are all in tmux doesn't need it. A failed attempt is retried after a minute."""
        if self.app is not None:
            return True
        if time.monotonic() < self._retry_at:
            return False
        self._retry_at = time.monotonic() + 60
        running = await asyncio.to_thread(subprocess.run, ["pgrep", "-x", "iTerm2"], capture_output=True)
        if running.returncode != 0:
            return False
        try:
            self.conn = await iterm2.Connection.async_create()
            self.app = await iterm2.async_get_app(self.conn)
        except Exception as e:  # noqa: BLE001 - API disabled, permission refused, ...
            log.warning("can't connect to iTerm2: %s", e)
            self.conn = self.app = None
            return False
        asyncio.create_task(self._watch_terminations(), name="iterm:terminations")
        asyncio.create_task(self._watch_new_sessions(), name="iterm:new")
        log.info("connected to iTerm2")
        return True

    async def start(self) -> None:
        if not await self._connect():
            log.info("iTerm2 not connected; agents in iTerm2 tabs connect it when found")
