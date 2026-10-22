"""The only path from agent-watcher to a terminal. Accepts template ids, never free text."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Literal, Protocol

from .config import Mode, ModelRotation, WatcherConfig
from .detect import codex_status_effort, composer_draft, menu_items, screen_lines
from .usage import Window, parse_usage_screen
from .templates import T, launch_command, render

log = logging.getLogger(__name__)

UP, DOWN, ESC, ENTER, BACKSPACE = "\x1b[A", "\x1b[B", "\x1b", "\r", "\x7f"
CTRL_E, CLAUDE_STASH = "\x05", "\x13"  # end of line; Claude's prompt stash (ctrl+s), restored after the next submit
# Claude's /usage screen is open while its tab header shows, or its "Esc to cancel" footer just below its own last
# lines (permission dialogs say "Esc to cancel" too). It can be taller than the terminal, so the header may have
# scrolled away; the prompt box stays visible below it.
_USAGE_OPEN = re.compile(r"^\s*Settings\s+Status\s+Config\s+Usage|"
                         r"(Usage credits|d to day · w to week|% used)[^\n]*\n([^\n]*\n){0,4}\s*Esc to cancel\s*$", re.M)
# A Claude turn in progress: "esc to interrupt", or the spinner line, e.g. "✢ Roosting… (20m 52s · ↓ 32.1k tokens)"
_CLAUDE_WORKING = re.compile(r"esc to interrupt|^\s*\S\s+\S+…\s+\((\d+h\s*)?(\d+m\s*)?\d+s\b", re.I | re.M)


class Terminal(Protocol):
    """What the actuator and supervisor need from iTerm2 (faked in tests)."""

    def bound(self, agent: str) -> bool: ...

    async def send_raw(self, agent: str, data: str) -> None: ...

    async def job_name(self, agent: str) -> str | None: ...

    async def screen_text(self, agent: str) -> str: ...


class Notifier(Protocol):
    async def notify(self, title: str, message: str) -> None: ...


class MacNotifier:
    async def notify(self, title: str, message: str) -> None:
        def q(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')[:200]
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", f'display notification "{q(message)}" with title "{q(title)}"',
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()


class Actuator:
    def __init__(self, cfg: WatcherConfig, terminal: Terminal, audit_path: Path, now=time.time):
        self.cfg = cfg
        self.term = terminal
        self.audit_path = audit_path
        self.now = now
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.last_owner_key: dict[str, float] = {}
        audit_path.parent.mkdir(parents=True, exist_ok=True)

    def audit(self, agent: str, behavior: str, mode: Mode, action: str, detail: str, why: str = "") -> None:
        rec = {"ts": round(self.now(), 3), "agent": agent, "behavior": behavior, "mode": mode,
               "action": action, "detail": detail, "why": why}
        with self.audit_path.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        log.info("%s %s %s %s: %s %s", "DID" if mode == "live" else "WOULD", agent, behavior, action, detail[:200], why)

    async def _wait_owner_quiet(self, agent: str) -> None:
        grace = self.cfg.timing.owner_typing_grace_s
        for _ in range(240):  # at most ~ grace*240; owner typing continuously is unusual
            last = self.last_owner_key.get(agent, 0)
            remaining = last + grace - self.now()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, grace))

    async def _send_line(self, agent: str, text: str) -> None:
        await self.term.send_raw(agent, text)
        await asyncio.sleep(self.cfg.timing.submit_delay_s)
        await self.term.send_raw(agent, "\r")

    async def type(self, agent: str, behavior: str, t: T, actions: list[str] | None = None, why: str = "") -> bool:
        """Type a template and submit it. Returns True if actually sent."""
        text = render(t, actions)
        return await self._do(agent, behavior, t.value, text, why, lambda: self._send_line(agent, text))

    async def type_now(self, agent: str, behavior: str, t: T, actions: list[str] | None = None,
                       why: str = "") -> bool:
        """Submit a template now, even mid-turn: the agent reads it at its next tool call or turn end (Enter queues
        it; nothing interrupts the turn). A quiet draft in the prompt box is moved aside and put back. False, with
        nothing typed, when the prompt box isn't on screen or a draft can't be moved safely; type() later instead."""
        mode = self.cfg.mode(behavior)
        if mode != "live" or not self.term.bound(agent):
            await self.type(agent, behavior, t, actions, why)  # audits what it would type; done either way
            return True
        text = render(t, actions)
        kind = self.cfg.agents[agent].kind
        async with self.locks[agent]:
            await self._wait_owner_quiet(agent)
            draft = await self._quiet_draft(agent, kind)
            if draft is None:
                return False
            ok, how = await self._submit_around(agent, kind, text, draft)
        if ok:
            self.audit(agent, behavior, mode, t.value, text, why + (f" [{how}]" if how else ""))
        else:
            log.info("%s: not typing now (%s); waiting for idle", agent, how)
        return ok

    async def _quiet_draft(self, agent: str, kind: str) -> str | None:
        """The prompt box's draft once nobody is changing it; None if the box isn't showing (or a dialog is)."""
        dialog = re.compile(self.cfg.keys[kind].dialog_regex)
        for _ in range(12):
            text = await self.term.screen_text(agent)
            draft = composer_draft(text, kind)
            plain = "\n".join(screen_lines(text))
            if draft is None or dialog.search(plain) or (kind == "claude" and _USAGE_OPEN.search(plain)):
                return None
            if not draft:
                return ""
            start = self.now()
            while self.now() - start < self.cfg.timing.draft_watch_s:
                await asyncio.sleep(min(0.5, self.cfg.timing.draft_watch_s))
                if self.last_owner_key.get(agent, 0) >= start or \
                        composer_draft(await self.term.screen_text(agent), kind) != draft:
                    break
            else:
                return draft
            await self._wait_owner_quiet(agent)  # someone is typing: let them finish, then look again
        return None

    async def _submit_around(self, agent: str, kind: str, text: str, draft: str) -> tuple[bool, str]:
        if not draft:
            await self._send_line(agent, text)
            return True, ""
        if kind == "claude":  # the stash keeps the draft exactly (newlines, pastes) and restores it after Enter
            await self.term.send_raw(agent, CLAUDE_STASH)
            if await self._screen_shows_draft(agent, kind, "") is None:
                return False, f"the draft did not stash: {draft[:80]!r}"
            await self._send_line(agent, text)
            if await self._screen_shows_draft(agent, kind, draft) is None:
                log.warning("%s: the stashed draft did not come back: %r", agent, draft[:200])
                return True, "draft stashed; it did not come back"
            return True, "draft stashed and restored"
        if "\n" in draft:  # Codex has no stash; a wrapped or multi-line draft can't be retyped exactly
            return False, "a multi-line draft is in the prompt box"
        await self.term.send_raw(agent, CTRL_E)
        await self.term.send_raw(agent, BACKSPACE * len(draft))
        if await self._screen_shows_draft(agent, kind, "") is None:
            log.warning("%s: the draft did not clear; nothing typed", agent)
            return False, "the draft did not clear"
        await self._send_line(agent, text)
        await self.term.send_raw(agent, draft)
        if await self._screen_shows_draft(agent, kind, draft) is None:
            log.warning("%s: the retyped draft doesn't match: %r", agent, draft[:200])
            return True, "draft moved aside; retyping it may not match"
        return True, "draft moved aside and retyped"

    async def _screen_shows_draft(self, agent: str, kind: str, draft: str) -> str | None:
        deadline = self.now() + self.cfg.timing.menu_timeout_s
        while True:
            text = await self.term.screen_text(agent)
            if composer_draft(text, kind) == draft:
                return text
            if self.now() >= deadline:
                return None
            await asyncio.sleep(self.cfg.timing.menu_step_s)

    async def press(self, agent: str, behavior: str, key: Literal["allow", "deny"], why: str = "") -> bool:
        kind = self.cfg.agents[agent].kind
        seq = getattr(self.cfg.keys[kind], key)
        return await self._do(agent, behavior, f"KEY_{key.upper()}", repr(seq), why,
                              lambda: self.term.send_raw(agent, seq))

    async def interrupt(self, agent: str, behavior: str, why: str = "") -> bool:
        async def go():
            await self.term.send_raw(agent, "\x03")
            await asyncio.sleep(0.5)
            await self.term.send_raw(agent, "\x03")
        return await self._do(agent, behavior, "KEY_INTERRUPT", "ctrl-c x2", why, go)

    async def shell_pre_launch(self, agent: str, behavior: str) -> None:
        for cmd in self.cfg.agents[agent].pre_launch:
            await self._do(agent, behavior, "PRE_LAUNCH", cmd, "", lambda c=cmd: self._send_line(agent, c))
            await asyncio.sleep(0.5)

    async def shell_launch(self, agent: str, behavior: str, project: str, recovery: bool = False) -> bool:
        cmd = launch_command(self.cfg.agents[agent].launch, project, recovery)
        return await self._do(agent, behavior, "LAUNCH_RECOVERY" if recovery else "LAUNCH", cmd, "",
                              lambda: self._send_line(agent, cmd))

    async def codex_switch_model(self, agent: str, behavior: str, rot: ModelRotation, why: str = "") -> bool:
        """Pick `rot.model` at the effort after the current one in `rot.efforts`, through Codex's /model menus.

        Every step is checked on screen; on anything unexpected it backs out and returns False.
        """
        mode = self.cfg.mode(behavior)
        plan = f"/model -> {rot.model}, next effort of {rot.efforts}"
        if mode == "off":
            return False
        if mode == "dry":
            self.audit(agent, behavior, mode, "MODEL_SWITCH", plan, why)
            return False
        if not self.term.bound(agent):
            self.audit(agent, behavior, "dry", "MODEL_SWITCH", plan, why + " [no terminal bound]")
            return False
        async with self.locks[agent]:
            await self._wait_owner_quiet(agent)
            ok, detail = await self._model_menus(agent, rot)
        self.audit(agent, behavior, mode, "MODEL_SWITCH" if ok else "MODEL_SWITCH_FAILED", detail, why)
        return ok

    async def claude_usage(self, agent: str, behavior: str) -> dict[str, Window] | None:
        """Read Claude's /usage screen (plan usage limits), then close it. None if not read.

        Only from an idle prompt: "/usage" must show as the highlighted slash command (so the prompt was empty)
        before Enter, and Esc is sent only while the usage screen is showing (Esc interrupts a working session).
        """
        mode = self.cfg.mode(behavior)
        if mode == "off":
            return None
        if mode == "dry":
            self.audit(agent, behavior, mode, "USAGE_READ", "/usage, read the limits, Esc")
            return None
        if not self.term.bound(agent):
            return None
        async with self.locks[agent]:
            await self._wait_owner_quiet(agent)
            ok, detail, windows = await self._usage_screen(agent)
        self.audit(agent, behavior, mode, "USAGE_READ" if ok else "USAGE_READ_FAILED", detail)
        return windows if ok else None

    async def _usage_screen(self, agent: str) -> tuple[bool, str, dict[str, Window]]:
        if _CLAUDE_WORKING.search("\n".join(screen_lines(await self.term.screen_text(agent)))):
            return False, "agent is working", {}
        cmd = render(T.USAGE_SCREEN)
        await self.term.send_raw(agent, cmd)
        if await self._screen_shows(agent, r"^\s*❯\s*/usage\s{2,}\S") is None:
            await self.term.send_raw(agent, BACKSPACE * len(cmd))  # the prompt wasn't empty, or no popup
            return False, "the /usage suggestion did not show", {}
        await self.term.send_raw(agent, ENTER)
        text = await self._screen_shows(agent, r"^\s*Current session\s*\n.*% used",
                                         timeout=3 * self.cfg.timing.menu_timeout_s)  # the limits load
        windows = parse_usage_screen(text) if text else {}
        if not await self._close_usage_screen(agent):
            log.warning("%s: the /usage screen did not close", agent)
            return False, "the usage screen did not close", windows
        if not windows:
            return False, "no plan usage on the usage screen", {}
        return True, ", ".join(f"{k} {w.left_pct}% left" for k, w in windows.items()), windows

    async def _close_usage_screen(self, agent: str) -> bool:
        """Esc until the /usage screen is gone; each Esc only while it shows (at the prompt, Esc Esc rewinds)."""
        for _ in range(3):
            if await self._screen_shows(agent, _USAGE_OPEN.pattern, want=False, timeout=0) is not None:
                return True
            await self.term.send_raw(agent, ESC)
            if await self._screen_shows(agent, _USAGE_OPEN.pattern, want=False,
                                        timeout=2 * self.cfg.timing.menu_timeout_s) is not None:
                return True
        return False

    async def _screen_shows(self, agent: str, pattern: str, want: bool = True,
                            timeout: float | None = None) -> str | None:
        """The screen once `pattern` is (or, want=False, is no longer) on it; None on timeout."""
        rx = re.compile(pattern, re.M)
        deadline = self.now() + (self.cfg.timing.menu_timeout_s if timeout is None else timeout)
        while True:
            text = "\n".join(screen_lines(await self.term.screen_text(agent)))
            if bool(rx.search(text)) == want:
                return text
            if self.now() >= deadline:
                return None
            await asyncio.sleep(self.cfg.timing.menu_step_s)

    async def _select(self, agent: str, label: str) -> bool:
        """Move a Codex menu's selection onto `label` with the arrow keys."""
        for _ in range(20):
            items = menu_items(await self.term.screen_text(agent))
            target = next((i for i, (lbl, _, _) in enumerate(items) if lbl == label), None)
            sel = next((i for i, (_, s, _) in enumerate(items) if s), None)
            if target is None or sel is None:
                return False
            if target == sel:
                return True
            await self.term.send_raw(agent, DOWN if target > sel else UP)
            await asyncio.sleep(self.cfg.timing.menu_step_s)
        return False

    async def _model_menus(self, agent: str, rot: ModelRotation) -> tuple[bool, str]:
        async def back_out(times: int, why: str) -> tuple[bool, str]:
            for _ in range(times):
                await self.term.send_raw(agent, ESC)
                await asyncio.sleep(self.cfg.timing.menu_step_s)
            return False, why

        session_effort = codex_status_effort(await self.term.screen_text(agent))
        cmd = render(T.MODEL_MENU)
        await self.term.send_raw(agent, cmd)
        if await self._screen_shows(agent, r"^\s*›\s*/model\s+choose what model") is None:
            await self.term.send_raw(agent, BACKSPACE * len(cmd))  # the composer wasn't empty, or no popup
            return False, "the /model popup did not open"
        await self.term.send_raw(agent, ENTER)
        if await self._screen_shows(agent, r"Select Model and Effort") is None:
            return await back_out(1, "the model menu did not open")
        if not await self._select(agent, rot.model):
            return await back_out(1, f"{rot.model} not found in the model menu")
        await self.term.send_raw(agent, ENTER)
        if await self._screen_shows(agent, rf"Select Reasoning Level for {re.escape(rot.model)}") is None:
            return await back_out(2, "the effort menu did not open")
        items = menu_items(await self.term.screen_text(agent))
        current = next((lbl for lbl, _, cur in items if cur), None) or session_effort
        if not rot.efforts:  # keep the session's effort
            if session_effort is None:
                return await back_out(2, "the session's effort isn't on its status line")
            nxt = session_effort
        elif current in rot.efforts:
            nxt = rot.efforts[(rot.efforts.index(current) + 1) % len(rot.efforts)]
        else:
            nxt = rot.efforts[0]
        if not await self._select(agent, nxt):
            return await back_out(2, f"effort {nxt!r} not found in the effort menu")
        await self.term.send_raw(agent, rot.confirm_key)
        if await self._screen_shows(agent, r"Select Reasoning Level", want=False) is None:
            return await back_out(2, "the effort menu did not close")
        return True, f"{rot.model} {current or '?'} -> {rot.model} {nxt}"

    async def _do(self, agent: str, behavior: str, action: str, detail: str, why: str, fn) -> bool:
        mode = self.cfg.mode(behavior)
        if mode == "off":
            return False
        if mode == "dry":
            self.audit(agent, behavior, mode, action, detail, why)
            return False
        if not self.term.bound(agent):
            self.audit(agent, behavior, "dry", action, detail, why + " [no terminal bound]")
            return False
        async with self.locks[agent]:
            await self._wait_owner_quiet(agent)
            await fn()
        self.audit(agent, behavior, mode, action, detail, why)
        return True
