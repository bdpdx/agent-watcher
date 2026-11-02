"""Routes each agent to the terminal it runs in: a tmux pane or, on macOS, an iTerm2 tab."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Awaitable, Callable

from .config import WatcherConfig
from .events import Event
from .tmux import TmuxTerminal

if TYPE_CHECKING:
    from .iterm import ITermTerminal


class Terminals:
    """The Terminal for the actuator and supervisor. An agent's terminal id says which backend drives it:
    "iterm:<session GUID>" or "tmux:<pane>:<socket>" (see discovery)."""

    def __init__(self, cfg: WatcherConfig, emit: Callable[[Event], Awaitable[None]],
                 on_change: Callable[[], None] = lambda: None):
        self.iterm: ITermTerminal | None = None
        if sys.platform == "darwin":  # the iterm2 package is only installed there
            from .iterm import ITermTerminal
            self.iterm = ITermTerminal(cfg, emit, on_change)
        self.tmux = TmuxTerminal(cfg, emit, on_change)
        self._backend: dict[str, ITermTerminal | TmuxTerminal] = {}  # agent -> the backend it's bound through

    def bound(self, agent: str) -> bool:
        b = self._backend.get(agent)
        return b is not None and b.bound(agent)

    async def send_raw(self, agent: str, data: str) -> None:
        b = self._backend.get(agent)
        if b is None:
            raise RuntimeError(f"no terminal bound for {agent}")
        await b.send_raw(agent, data)

    async def job_name(self, agent: str) -> str | None:
        b = self._backend.get(agent)
        return await b.job_name(agent) if b else None

    async def screen_text(self, agent: str) -> str:
        b = self._backend.get(agent)
        return await b.screen_text(agent) if b else ""

    async def bind(self, agent: str, term_id: str) -> None:
        kind, _, ref = term_id.partition(":")
        backend = {"iterm": self.iterm, "tmux": self.tmux}.get(kind)
        if backend is None or not ref:
            return
        old = self._backend.get(agent)
        if old is not None and old is not backend:
            old.unbind(agent)  # the agent was relaunched in the other kind of terminal
        self._backend[agent] = backend
        await backend.bind(agent, ref)

    def unbind(self, agent: str) -> None:
        b = self._backend.pop(agent, None)
        if b:
            b.unbind(agent)

    async def start(self) -> None:
        await self.tmux.start()
        if self.iterm:
            await self.iterm.start()
