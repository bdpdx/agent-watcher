"""tmux adapter: binds agents to panes, reads screens and the foreground command, sends keystrokes.

An agent's pane comes from its environment: $TMUX ("<socket>,<server pid>,<session>") names the server and
$TMUX_PANE ("%3") the pane, which keeps its id for the life of the server. Text goes in through a paste buffer
(raw bytes, no bracketed paste); control keys (Esc, Enter, arrows, ctrl-x) go through send-keys as key presses.

The owner's keystrokes are read from the attached clients: a client showing the agent's pane whose
client_activity moves on, or the pane in copy mode (the owner scrolling), counts as the owner typing there.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from .config import WatcherConfig
from .events import E, Event

log = logging.getLogger(__name__)

TAG = "@agent-watcher-agent"  # pane option naming the agent, for `tmux show -p`

Run = Callable[..., Awaitable[tuple[int, str]]]


def pane_ref(tmux_env: str | None, tmux_pane: str | None) -> str | None:
    """'<pane>:<socket>' from an agent's $TMUX and $TMUX_PANE; None outside tmux."""
    socket = (tmux_env or "").split(",", 1)[0]
    if not socket or not (tmux_pane or "").startswith("%"):
        return None
    return f"{tmux_pane}:{socket}"


# Control keys go in through send-keys: a pasted lone ESC isn't read as the Esc key (a /usage screen stays open)
_KEYS = {"\x1b[A": "Up", "\x1b[B": "Down", "\x1b[C": "Right", "\x1b[D": "Left", "\x1b": "Escape",
         "\r": "Enter", "\t": "Tab", "\x7f": "BSpace"}


def split_keys(data: str) -> list[tuple[bool, str | tuple[str, ...]]]:
    """`data` as runs of (True, text to paste) and (False, tmux key names for send-keys)."""
    out: list[tuple[bool, str | tuple[str, ...]]] = []
    i = 0
    while i < len(data):
        key = next((k for k in sorted(_KEYS, key=len, reverse=True) if data.startswith(k, i)), None)
        if key is None and data[i] < " " and data[i] != "\n":
            key = data[i]  # other control characters: C-a .. C-z
        if key is None:
            j = i
            while j < len(data) and (data[j] == "\n" or " " <= data[j] != "\x7f"):
                j += 1
            out.append((True, data[i:j]))
            i = j
            continue
        name = _KEYS.get(key) or f"C-{chr(ord(key) + 96)}"
        if out and not out[-1][0]:
            out[-1] = (False, (*out[-1][1], name))
        else:
            out.append((False, (name,)))
        i += len(key)
    return out


async def run_tmux(socket: str, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec("tmux", "-S", socket, *args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except TimeoutError:
        proc.kill()
        return 1, ""
    return proc.returncode or 0, out.decode(errors="replace")


class TmuxTerminal:
    def __init__(self, cfg: WatcherConfig, emit: Callable[[Event], Awaitable[None]],
                 on_change: Callable[[], None] = lambda: None, run: Run = run_tmux, poll_s: float = 1.0):
        self.cfg = cfg
        self.emit = emit
        self.on_change = on_change  # called when panes open or close, so discovery rescans
        self.run = run
        self.poll_s = poll_s
        self.panes: dict[str, tuple[str, str]] = {}  # agent -> (socket, pane id)
        self._live: dict[str, set[str]] = {}  # socket -> pane ids alive at the last poll
        self._activity: dict[str, int] = {}  # agent -> latest client_activity seen on a client showing its pane

    # ---- Terminal protocol --------------------------------------------------------------------

    def bound(self, agent: str) -> bool:
        socket, pane = self.panes.get(agent, ("", ""))
        return pane in self._live.get(socket, ())

    async def send_raw(self, agent: str, data: str) -> None:
        if agent not in self.panes:
            raise RuntimeError(f"no tmux pane bound for {agent}")
        socket, pane = self.panes[agent]
        rc, mode = await self.run(socket, "display-message", "-p", "-t", pane, "#{pane_in_mode}")
        if rc != 0:
            raise RuntimeError(f"tmux pane {pane} for {agent} is gone")
        if mode.strip() == "1":
            raise RuntimeError(f"tmux pane {pane} for {agent} is in copy mode")
        buf = f"agent-watcher-{agent}"
        for is_text, part in split_keys(data):
            if is_text:
                args = ("set-buffer", "-b", buf, "--", part, ";", "paste-buffer", "-d", "-r", "-b", buf, "-t", pane)
            else:
                args = ("send-keys", "-t", pane, *part)
            rc, _ = await self.run(socket, *args)
            if rc != 0:
                raise RuntimeError(f"tmux couldn't type into pane {pane} for {agent}")

    async def job_name(self, agent: str) -> str | None:
        if agent not in self.panes:
            return None
        socket, pane = self.panes[agent]
        rc, out = await self.run(socket, "display-message", "-p", "-t", pane, "#{pane_current_command}")
        return (out.strip() or None) if rc == 0 else None

    async def screen_text(self, agent: str) -> str:
        if agent not in self.panes:
            return ""
        socket, pane = self.panes[agent]
        rc, out = await self.run(socket, "capture-pane", "-p", "-t", pane)
        return out.rstrip("\n") if rc == 0 else ""

    # ---- binding ---------------------------------------------------------------------------------

    async def bind(self, agent: str, ref: str) -> None:
        pane, _, socket = ref.partition(":")
        if self.panes.get(agent) == (socket, pane):
            return
        rc, out = await self.run(socket, "display-message", "-p", "-t", pane, "#{pane_id}")
        if rc != 0 or out.strip() != pane:
            return
        for other, p in list(self.panes.items()):
            if p == (socket, pane) and other != agent:
                self.unbind(other)
        self.panes[agent] = (socket, pane)
        self._live.setdefault(socket, set()).add(pane)
        self._activity.pop(agent, None)
        await self.run(socket, "set-option", "-p", "-t", pane, TAG, agent)
        log.info("bound %s to tmux pane %s (%s)", agent, pane, socket)

    def unbind(self, agent: str) -> None:
        self.panes.pop(agent, None)
        self._activity.pop(agent, None)

    # ---- polling -----------------------------------------------------------------------------------

    async def poll_once(self) -> None:
        """Notice panes opening and closing, and the owner typing or scrolling in an agent's pane."""
        changed = False
        for socket in {s for s, _ in self.panes.values()}:
            rc, out = await self.run(socket, "list-panes", "-a", "-F", "#{pane_id} #{pane_in_mode}")
            rows = [ln.split() for ln in out.splitlines() if ln.strip()] if rc == 0 else []
            live = {r[0] for r in rows}
            in_mode = {r[0] for r in rows if len(r) > 1 and r[1] == "1"}
            if live != self._live.get(socket):
                changed = True
            self._live[socket] = live
            rc, out = await self.run(socket, "list-clients", "-F", "#{pane_id} #{client_activity}")
            shown: dict[str, int] = {}
            for ln in out.splitlines() if rc == 0 else []:
                pane, _, act = ln.partition(" ")
                if act.strip().isdigit():
                    shown[pane] = max(shown.get(pane, 0), int(act))
            for agent, (s, pane) in list(self.panes.items()):
                if s != socket:
                    continue
                if pane not in live:
                    self.unbind(agent)
                    log.warning("tmux pane %s for %s closed", pane, agent)
                    continue
                act, seen = shown.get(pane), self._activity.get(agent)
                if act is not None:
                    self._activity[agent] = max(act, seen or 0)
                if pane in in_mode or (act is not None and seen is not None and act > seen):
                    await self.emit(Event(E.OWNER_KEYSTROKE, agent, source="tmux"))
        if changed:
            self.on_change()

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 - a tmux hiccup must not stop the watcher
                log.exception("tmux poll failed")
            await asyncio.sleep(self.poll_s)

    async def start(self) -> None:
        asyncio.create_task(self._poll_loop(), name="tmux:poll")
