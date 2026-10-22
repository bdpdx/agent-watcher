"""Session-log tailing.

Codex: rollouts in $CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl. The first line is session_meta;
`thread_source` says what the thread is ("user" for the agent's main thread, "guardian_review" for an
auto-review, "subagent" for spawned helpers). Main threads give errors, idle, context size and
compactions; guardian threads give reviewer denials.

Claude: the transcript named by `transcript_path` in hook payloads gives context size and compactions.

When a log is first seen mid-session, it is scanned once so the compaction count and context size
since the session started are known (a "baseline" event), then followed from the end.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import AgentConfig, WatcherConfig
from .detect import (GuardianReview, claude_compaction, claude_context_tokens, codex_compaction, codex_context,
                     from_codex_main_line, iso_ts, rollout_meta)
from .events import E, Event
from .usage import codex_rate_limits

log = logging.getLogger(__name__)
Emit = Callable[[Event], Awaitable[None]]


@dataclass
class _Tail:
    path: Path
    offset: int
    buf: str = ""
    kind: str = "main"  # "main" | "guardian" | "ignore"
    guardian: GuardianReview | None = None

    def read_lines(self) -> list[str]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:  # truncated or replaced
            self.offset, self.buf = 0, ""
        if size == self.offset:
            return []
        with self.path.open("rb") as f:
            f.seek(self.offset)
            chunk = f.read(size - self.offset)
        self.offset = size
        lines = (self.buf + chunk.decode("utf-8", errors="replace")).split("\n")
        self.buf = lines.pop()
        return [ln for ln in lines if ln]


@dataclass
class Baseline:
    count: int = 0
    pre: int | None = None
    tokens: int | None = None
    window: int | None = None
    started_at: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def events(self, agent: str, source: str) -> list[Event]:
        out = [Event(E.COMPACTION, agent, {"count": self.count, "baseline": True, "started_at": self.started_at},
                     source=source)]
        if self.tokens and self.window:
            out.append(Event(E.CONTEXT, agent, {"tokens": self.tokens, "window": self.window, "baseline": True},
                             source=source))
        if self.extra.get("usage"):
            out.append(Event(E.USAGE, agent, {"windows": self.extra["usage"]}, source=source))
        return out


def codex_baseline(path: Path) -> Baseline:
    b = Baseline()
    with path.open() as f:
        for i, line in enumerate(f):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if i == 0:
                b.started_at = iso_ts(obj.get("payload", {}).get("timestamp") or obj.get("timestamp"))
            if (c := codex_compaction(obj)) is not None:
                b.count = c["count"] or b.count + 1
            elif (c := codex_context(obj)) is not None:
                b.tokens, b.window = c["tokens"], c["window"]
            if (w := codex_rate_limits(obj)) is not None:
                b.extra["usage"] = w
    return b


def claude_baseline(path: Path, window: int) -> Baseline:
    b = Baseline(window=window)
    with path.open() as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if b.started_at is None:
                b.started_at = iso_ts(obj.get("timestamp"))
            if claude_compaction(obj) is not None:
                b.count += 1
            elif (t := claude_context_tokens(obj)) is not None:
                b.tokens = t
    return b


class RolloutTailer:
    def __init__(self, agent: AgentConfig, emit: Emit, poll_s: float = 2.0):
        self.agent = agent
        self.emit = emit
        self.poll_s = poll_s
        self.root = agent.config_dir / "sessions"
        self.files: dict[Path, _Tail] = {}
        self.main_ids: set[str] = set()
        self.sub_ids: set[str] = set()  # the main threads' subagents (and theirs): their actions are reviewed too
        self.compactions: dict[Path, int] = {}
        self._first_scan = True
        self._preexisting: set[Path] = set()  # files there at the first scan: what they hold already happened

    def _day_dirs(self) -> list[Path]:
        today = dt.date.today()
        days = [today - dt.timedelta(days=1), today, today + dt.timedelta(days=1)]  # rollouts are UTC-dated
        return [self.root / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}" for d in days]

    async def _track(self, path: Path) -> _Tail | None:
        try:
            with path.open() as f:
                first = f.readline()
        except OSError:
            return None
        if not first.endswith("\n"):
            return None  # meta not fully written yet
        meta = rollout_meta(first)
        src = meta.get("thread_source")
        if src == "user" and self.agent.owns_path(Path(meta.get("cwd", "/nonexistent"))):
            self.main_ids.add(meta.get("id") or meta.get("session_id", ""))
            if self._first_scan:
                base = codex_baseline(path)
                start = path.stat().st_size
            else:
                base = Baseline(started_at=iso_ts(meta.get("timestamp")))
                start = len(first.encode())
            self.compactions[path] = base.count
            for ev in base.events(self.agent.name, "rollout"):
                await self.emit(ev)
            return _Tail(path, start)
        if src == "subagent":
            if meta.get("parent_thread_id") in self.main_ids | self.sub_ids:
                self.sub_ids.add(meta.get("id") or "")
                return _Tail(path, 0, kind="ignore")
            return _Tail(path, 0, kind="ignore") if self._stale(path) else None
        if src == "guardian_review":
            if meta.get("parent_thread_id") in self.main_ids | self.sub_ids:
                old = self._first_scan or path in self._preexisting  # its parent may be tracked after it
                start = path.stat().st_size if old else len(first.encode())
                return _Tail(path, start, kind="guardian", guardian=GuardianReview(meta.get("parent_thread_id")))
            # parent may not be known yet; keep checking while the file is fresh
            return _Tail(path, 0, kind="ignore") if self._stale(path) else None
        return _Tail(path, 0, kind="ignore")

    @staticmethod
    def _stale(path: Path) -> bool:
        try:
            return path.stat().st_mtime < dt.datetime.now().timestamp() - 3600
        except OSError:
            return True

    async def scan_once(self) -> None:
        candidates: list[Path] = []
        for d in self._day_dirs():
            if d.is_dir():
                candidates.extend(sorted(d.glob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime))
        if self._first_scan:
            self._preexisting = set(candidates)
        for path in candidates:
            if path not in self.files:
                tail = await self._track(path)
                if tail is not None:
                    self.files[path] = tail
        self._first_scan = False
        for path, tail in list(self.files.items()):
            if tail.kind == "ignore":
                continue
            for line in tail.read_lines():
                if tail.kind == "main":
                    events = from_codex_main_line(line, self.agent.name)
                    for ev in events:
                        if ev.kind is E.COMPACTION:
                            n = ev.data.get("count") or self.compactions.get(path, 0) + 1
                            self.compactions[path] = n
                            ev.data["count"] = n
                else:
                    events = tail.guardian.feed(line, self.agent.name)
                for ev in events:
                    await self.emit(ev)

    async def run(self) -> None:
        while True:
            try:
                await self.scan_once()
            except Exception:  # noqa: BLE001 - one bad file must not stop tailing
                log.exception("rollout scan failed for %s", self.agent.name)
            await asyncio.sleep(self.poll_s)


class ClaudeTranscriptTailer:
    """Follows the current transcript of each managed Claude agent (path from discovery or hook payloads)."""

    def __init__(self, cfg: WatcherConfig, emit: Emit, poll_s: float = 2.0):
        self.cfg = cfg
        self.emit = emit
        self.poll_s = poll_s
        self.tails: dict[str, _Tail] = {}
        self.counts: dict[str, int] = {}

    async def follow(self, agent: str, transcript_path: str | None) -> None:
        if not transcript_path:
            return
        path = Path(transcript_path).expanduser()
        cur = self.tails.get(agent)
        if (cur and cur.path == path) or not path.exists():
            return
        base = claude_baseline(path, self.cfg.claude_context_window)
        self.counts[agent] = base.count
        self.tails[agent] = _Tail(path, path.stat().st_size)
        for ev in base.events(agent, "transcript"):
            await self.emit(ev)

    def unfollow(self, agent: str) -> None:
        self.tails.pop(agent, None)
        self.counts.pop(agent, None)

    async def scan_once(self) -> None:
        for agent, tail in list(self.tails.items()):
            for line in tail.read_lines():
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (c := claude_compaction(obj)) is not None:
                    self.counts[agent] = self.counts.get(agent, 0) + 1
                    await self.emit(Event(E.COMPACTION, agent, {**c, "count": self.counts[agent]}, source="transcript"))
                elif (t := claude_context_tokens(obj)) is not None:
                    await self.emit(Event(E.CONTEXT, agent, {"tokens": t, "window": self.cfg.claude_context_window},
                                          source="transcript"))

    async def run(self) -> None:
        while True:
            try:
                await self.scan_once()
            except Exception:  # noqa: BLE001
                log.exception("transcript scan failed")
            await asyncio.sleep(self.poll_s)
