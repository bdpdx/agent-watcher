"""Per-agent state and the behaviors.

Agents are discovered (see discovery.py). An agent is managed exactly while its current session is registered in
a project this watcher serves (a `<project>__agent-watcher` channel its bot belongs to). Everything about an agent
is scoped to its project: its channel, its GM, its commands, its cohort.

The watcher reports events and acts on commands and decisions from the GM, the owner, and humans the owner
authorized. It decides nothing on its own beyond the code-level policy lists and automatic capacity retries.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

from .actuator import Actuator, Notifier, Terminal
from .assess import Assessor
from .channel import Poster
from .commands import HELP, Command, parse
from .config import CHANNEL_SUFFIX, ModelRotation, WatcherConfig
from .detect import codex_screen_capacity, codex_status_model, screen_lines
from .discovery import AgentRecord
from .events import E, Event, Tier
from .policy import Policy, Verdict
from .templates import T, clean, describe_action
from .usage import LABELS, AgentUsage, fmt_reset, fmt_window, scrape_interval, summary, update

log = logging.getLogger(__name__)

LOCAL_PROJECT = "local"  # agents of the [local] manage kinds that haven't joined a watched project


def local_name(home: Path) -> str:
    """An unregistered agent's name, from its home: ~/.codex-g -> codex-g."""
    return home.name.lstrip(".") or str(home)


class Phase(str, Enum):
    WORKING = "working"
    EXITING = "exiting"
    LAUNCHING = "launching"
    COHORT = "cohort_restart"


class Status(str, Enum):
    MANAGED = "managed"  # current session registered in a watched project
    DETACHED = "detached"  # same session, but its registration is gone (e.g. stopped its Slack watcher)


@dataclass
class AgentState:
    name: str
    project: str
    home: Path
    session_id: str | None = None
    term_id: str = ""
    status: Status = Status.MANAGED
    detached_since: float = 0
    phase: Phase = Phase.WORKING
    idle: bool = False
    last_idle: float = 0
    last_working: float = 0
    last_good_turn: float = 0
    last_error: float = 0
    capacity_tries: int = 0
    limit_posted: tuple | None = None  # (limit, resets at) last posted, so each limit is posted once
    safety_flags: deque = field(default_factory=lambda: deque(maxlen=20))
    pending_auth: list[tuple[str | None, str]] = field(default_factory=list)  # (session, action) approved, not typed
    recent_blocks: dict[str, float] = field(default_factory=dict)
    approved_blocks: dict[str, tuple[float, str, str | None]] = field(default_factory=dict)  # desc -> (when, id, thread)
    launched_at: float = 0
    compactions: int = 0
    compaction_floor: int | None = None  # % of the window in use right after the last compaction
    awaiting_floor: bool = False  # a Codex compaction gives no post size; the next context reading does
    context_tokens: int = 0
    context_window: int = 0
    context_reported: set = field(default_factory=set)
    last_restart: float = 0
    paused: bool = False
    flow: asyncio.Task | None = None
    retry_task: asyncio.Task | None = None
    usage: AgentUsage = field(default_factory=AgentUsage)
    usage_since: float = 0  # when its usage join line became due (attach or new session)
    usage_posted: float = 0  # when its usage was last posted in full (join line, online line, or for the GM)
    usage_task: asyncio.Task | None = None  # a /usage read in progress

    def busy(self) -> bool:
        return self.flow is not None and not self.flow.done()

    def context_pct(self) -> int | None:
        return round(100 * self.context_tokens / self.context_window) if self.context_window else None


@dataclass
class Pending:
    id: str
    agent: str
    project: str
    kind: str  # "block" | "dialog"
    desc: str
    rule: str
    owner_only: bool
    created: float
    session: str | None = None  # the agent session that was blocked; a decision never reaches a later session
    thread_ts: str | None = None
    resolved: str = ""  # "", "approved", "denied", "closed"
    reminder: asyncio.Task | None = None


_PENDING_SAVED = ("id", "agent", "project", "kind", "desc", "rule", "owner_only", "created", "session", "thread_ts")

# Posts that only a watcher makes, giving its name: "<name> online: watching …", "cohort done: <name> restarted …",
# "[<name>/B-0142] …".
_PEER_POST = re.compile(r"^(?:(\S+) online: watching |cohort done: (\S+) restarted |\[([a-z0-9][a-z0-9_-]*)/[A-Z]-\d+\])",
                        re.I)


# Another watcher's posts that mean an agent just joined the project: "<agent> restarted.", "<agent> moved to <p>.",
# "Now watching <agent>."
_PEER_ARRIVAL = re.compile(r"^(?:Now watching (\S+)\.|(\S+) (?:restarted|moved to \S+)\.)$")


@dataclass
class Changes:
    """What the daemon must do after applying a discovery scan."""
    attached: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def _k(n: int | None) -> str:
    if not n:
        return "?"
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{round(n / 1000)}k"


def _hhmm(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "?"


class Supervisor:
    def __init__(self, cfg: WatcherConfig, terminal: Terminal, actuator: Actuator, channel: Poster,
                 notifier: Notifier, policy: Policy | None = None, now: Callable[[], float] = time.time):
        self.cfg = cfg
        self.term = terminal
        self.act = actuator
        self.channel = channel
        self.notifier = notifier
        self.policy = policy or Policy(cfg, now)
        self.now = now
        self.refresh: Callable[[], Awaitable[None]] | None = None  # discovery scan, run before typing a decision
        self.assessor = Assessor(cfg.assess)  # tests replace it
        self.agents: dict[str, AgentState] = {}
        self.pending: dict[str, Pending] = {}
        self.seq = 0
        self.paused = False
        self.paused_names: set[str] = set()  # pause survives restarts and rediscovery
        self.next_cohort_at: dict[str, float] = {}
        self.cohort_ready_at: dict[str, float] = {}
        self.cohort_flow: dict[str, asyncio.Task] = {}
        self.peers: dict[str, dict[str, set[str]]] = {}  # project -> other watcher bot -> its agents
        self.peer_done: dict[str, dict[str, float]] = {}  # project -> other watcher bot -> "cohort done" time
        self.peer_watchers: dict[str, list[str]] = {}  # project -> other watcher bots ever seen posting (saved)
        self.last_gm: dict[str, str] = {}
        self.gm_announced: dict[str, str] = {}  # project -> GM named by a `gm <agent>` post; wins over gm.json
        self.gm_file_seen: dict[str, str | None] = {}  # project -> gm.json's GM when last read; a change there wins
        self.gm_usage_task: dict[str, asyncio.Task] = {}  # project -> usage post due for a GM that just arrived
        # compaction baseline: per-agent counts in the current cohort cycle (the 12h schedule), logged at its end
        self.cycle_start, self.cycle_end = 0.0, 0.0
        self.cycle_compactions: dict[str, int] = {}
        self.cyber_return: dict[str, str] = {}  # agent -> the model it goes back to after a good turn on [codex_cyber]
        self.compaction_log = cfg.state_dir / "compactions.jsonl"
        self.warned: set[str] = set()  # 1:1:1 warnings already sent
        self._cond = asyncio.Condition()
        self._gm_cache: dict[str, tuple[float, str | None]] = {}
        self.state_path = cfg.state_dir / "state.json"
        self._load_state()

    # ---- persistence -------------------------------------------------------------------------

    def _load_state(self) -> None:
        try:
            raw = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        nxt = raw.get("next_cohort_at")
        self.next_cohort_at = nxt if isinstance(nxt, dict) else {}
        self.paused = raw.get("paused", False)
        self.paused_names = set(raw.get("paused_agents", []))
        self.seq = raw.get("seq", 0)
        gm = raw.get("last_gm")
        self.last_gm = gm if isinstance(gm, dict) else {}
        ann = raw.get("gm_announced")
        self.gm_announced = ann if isinstance(ann, dict) else {}
        seen = raw.get("gm_file_seen")
        self.gm_file_seen = seen if isinstance(seen, dict) else {}
        peers = raw.get("peer_watchers")
        self.peer_watchers = peers if isinstance(peers, dict) else {}
        for d in raw.get("pending") or []:  # open requests survive a restart; a later session still never gets one
            try:
                self.pending[d["id"]] = Pending(**{k: d[k] for k in _PENDING_SAVED if k in d})
            except (KeyError, TypeError):
                log.warning("dropping an unreadable saved request: %r", d)
        cyc = raw.get("compaction_cycle") or {}
        self.cycle_start, self.cycle_end = cyc.get("start", 0.0), cyc.get("end", 0.0)
        self.cycle_compactions = dict(cyc.get("counts", {}))
        back = raw.get("cyber_return")
        self.cyber_return = back if isinstance(back, dict) else {}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        raw = {"next_cohort_at": self.next_cohort_at, "paused": self.paused, "paused_agents": sorted(self.paused_names),
               "seq": self.seq, "last_gm": self.last_gm, "gm_announced": self.gm_announced,
               "gm_file_seen": self.gm_file_seen, "peer_watchers": self.peer_watchers,
               "compaction_cycle": {"start": self.cycle_start, "end": self.cycle_end, "counts": self.cycle_compactions},
               "cyber_return": self.cyber_return,
               "pending": [{k: getattr(p, k) for k in _PENDING_SAVED} for p in self.pending.values() if not p.resolved]}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2))
        tmp.replace(self.state_path)

    # ---- helpers -------------------------------------------------------------------------------

    def gm(self, project: str) -> str | None:
        """A project's GM: the latest of a `gm <agent>` post in its channel and a change to gm.json."""
        name = self._gm_file(project)
        if self.gm_file_seen.get(project, name) != name:  # gm.json changed after the announcement: it wins
            self.gm_announced.pop(project, None)
        if self.gm_file_seen.get(project, object()) != name:
            self.gm_file_seen[project] = name
            self._save_state()
        return self.gm_announced.get(project) or name

    def _gm_file(self, project: str) -> str | None:
        """The GM in project-state's gm.json (origin/main, else the local file)."""
        ts, name = self._gm_cache.get(project, (0, None))
        if self.now() - ts < 60:
            return name
        text = None
        try:
            text = subprocess.run(
                ["git", "-C", str(self.cfg.project_state_dir), "show", f"origin/main:{project}/gm.json"],
                capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            pass
        if not text and self.cfg.gm_json(project).exists():
            text = self.cfg.gm_json(project).read_text()
        try:
            name = json.loads(text).get("gm") if text else None
        except json.JSONDecodeError:
            name = None
        self._gm_cache[project] = (self.now(), name)
        return name

    def in_project(self, project: str) -> list[str]:
        return [n for n, s in self.agents.items() if s.project == project]

    def hosts_gm(self, project: str) -> bool:
        gm = self.gm(project)
        return gm is not None and gm in self.agents and self.agents[gm].project == project

    def active(self, agent: str) -> bool:
        return not self.paused and not self.agents[agent].paused

    async def _notify_waiters(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    async def wait_until(self, pred: Callable[[], bool], timeout: float) -> bool:
        deadline = self.now() + timeout
        async with self._cond:
            while not pred():
                remaining = deadline - self.now()
                if remaining <= 0:
                    return False
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=min(remaining, 2.0))
                except TimeoutError:
                    pass
        return True

    async def _wait_idle(self, agent: str, timeout: float) -> bool:
        """Until the agent is idle: from its hooks and rollout, or (e.g. right after a watcher restart, before any
        turn has ended) from its screen."""
        st = self.agents[agent]
        deadline = self.now() + timeout
        while not st.idle:
            if await self.act.screen_idle(agent):
                st.idle = True
                break
            if self.now() >= deadline:
                return False
            await self.wait_until(lambda: st.idle, min(2.0, max(0.0, deadline - self.now())))
        return True

    def _spawn(self, coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(self._task_done)
        return task

    @staticmethod
    def _task_done(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            log.error("task %s failed", task.get_name(), exc_info=task.exception())

    async def say(self, project: str, text: str, thread_ts: str | None = None) -> str | None:
        return await self.channel.post(project, text, thread_ts)

    def _post(self, project: str, text: str) -> None:
        self._spawn(self.say(project, text), "post")

    def _new_id(self) -> str:
        self.seq += 1
        self._save_state()
        return f"B-{self.seq:04d}"

    def _qualified(self, pid: str) -> str:
        return f"{self.channel.name}/{pid}"

    # ---- discovery -------------------------------------------------------------------------------

    def attach(self, rec: AgentRecord, name: str, project: str) -> AgentState:
        """Register or refresh a managed agent from a discovery record."""
        self.cfg.agents[name] = self.cfg.agent_config(name, rec.kind, rec.home, rec.cwd, rec.launch_command())
        st = self.agents.get(name)
        if st is None:
            st = AgentState(name, project, rec.home)
            self.agents[name] = st
        if st.usage_since == 0 or st.session_id != rec.session_id or st.project != project:
            st.usage.introduced, st.usage_since = False, self.now()  # joined: one usage line is due
        if (st.session_id and st.session_id != rec.session_id) or any(  # or since a saved request was posted
                p.agent == name and not p.resolved and p.session and p.session != rec.session_id
                for p in self.pending.values()):
            self._retire_session(st, "it restarted", keep=rec.session_id)
        st.project, st.home, st.term_id = project, rec.home, rec.term_id
        st.session_id, st.status, st.detached_since = rec.session_id, Status.MANAGED, 0
        st.paused = name in self.paused_names
        return st

    def drop(self, name: str, why: str) -> None:
        st = self.agents.pop(name, None)
        self.cfg.agents.pop(name, None)
        if st is None:
            return
        for task in (st.flow, st.retry_task):
            if task and not task.done():
                task.cancel()
        self._retire_session(st, "it is no longer watched")
        self._post(st.project, f"No longer watching {name} ({why}).")

    def apply_discovery(self, records: list[AgentRecord]) -> Changes:
        """Reconcile managed agents with a fresh scan. Managed = current session registered in a watched project."""
        ch = Changes()
        watched = self.channel.projects()
        by_home: dict[Path, list[AgentRecord]] = {}
        for r in records:
            by_home.setdefault(r.home, []).append(r)
        seen: set[str] = set()

        for home, recs in by_home.items():
            mine = [n for n, st in self.agents.items() if st.home == home]
            if len(recs) > 1:
                names = sorted({g.agent for r in recs for g in r.regs})
                self._warn(f"home:{home}", next((r.regs[0].project for r in recs if r.regs), None),
                           f"1:1:1 violation: {len(recs)} agent sessions are running from {home}"
                           + (f" ({', '.join(names)})" if names else "") + "; not acting on them until one exits.")
                seen.update(mine)  # leave existing state alone
                continue
            rec = recs[0]
            if len({g.project for g in rec.regs}) > 1:
                self._warn(f"multi:{rec.session_id}", rec.regs[0].project,
                           f"1:1:1 violation: {rec.regs[0].agent}'s session is registered in "
                           f"{', '.join(sorted(g.project for g in rec.regs))}; not acting on it.")
                seen.update(mine)
                continue
            reg = next((g for g in rec.regs if g.project in watched), None)
            existing = mine[0] if mine else None
            if reg is not None:
                if existing and existing != reg.agent:
                    self.drop(existing, f"its home now runs {reg.agent}")
                    ch.dropped.append(existing)
                prev = self.agents.get(reg.agent)
                same_session = prev is not None and prev.session_id == rec.session_id
                new = prev is None or prev.status is not Status.MANAGED or not same_session \
                    or prev.project != reg.project
                st = self.attach(rec, reg.agent, reg.project)
                seen.add(reg.agent)
                if new:
                    ch.attached.append(reg.agent)
                    if reg.agent == self.gm(reg.project):
                        self._usage_for_gm(reg.project)
                    if not st.busy():
                        if not same_session:
                            st.launched_at = 0
                        if prev is None or prev.status is Status.MANAGED or prev.project != reg.project:
                            self._post(reg.project, f"Now watching {reg.agent}.")
                continue
            if rec.kind in self.cfg.local_kinds and (existing is None
                                                     or self.agents[existing].project == LOCAL_PROJECT):
                name = local_name(home)
                prev = self.agents.get(name)
                if prev is not None and prev.busy():
                    prev.session_id, prev.term_id = rec.session_id, rec.term_id
                else:
                    self.attach(rec, name, LOCAL_PROJECT)
                    if prev is None or prev.project != LOCAL_PROJECT:
                        ch.attached.append(name)
                        self._post(LOCAL_PROJECT, f"Now watching {name}.")
                seen.add(name)
                continue
            if existing is None:
                continue  # not in a watched project: not ours to watch
            st = self.agents[existing]
            seen.add(existing)
            if st.busy():  # mid-restart: a new session is starting; wait for it to register
                if rec.session_id != st.session_id:
                    st.status, st.detached_since = Status.DETACHED, self.now()  # not joined until it registers
                st.session_id, st.term_id = rec.session_id, rec.term_id
                continue
            if rec.session_id == st.session_id:
                if st.status is not Status.DETACHED:
                    st.status, st.detached_since = Status.DETACHED, self.now()
            else:
                self.drop(existing, "a new session started and hasn't joined a project")
                seen.discard(existing)
                ch.dropped.append(existing)

        for name in [n for n in self.agents if n not in seen]:
            if self.agents[name].busy():
                continue  # exiting for a restart
            self.drop(name, "the agent exited")
            ch.dropped.append(name)
        return ch

    def _warn(self, key: str, project: str | None, text: str) -> None:
        if key in self.warned:
            return
        self.warned.add(key)
        log.warning(text)
        if project:
            self._post(project, text)
        self._spawn(self.notifier.notify("agent-watcher", text[:200]), "notify")

    # ---- event dispatch --------------------------------------------------------------------------

    async def handle(self, ev: Event) -> None:
        log.debug("event %r", ev)
        if ev.kind is E.MESSAGE:
            await self._on_message(ev)
        elif ev.kind is E.CHANNEL_READY:
            if not ev.data.get("gone"):
                self._spawn(self._on_channel_ready(ev.data["project"]), "channel-ready")
        elif ev.agent in self.agents:
            st = self.agents[ev.agent]
            if ev.kind is E.OWNER_KEYSTROKE:
                self.act.last_owner_key[st.name] = ev.ts
            elif ev.kind in (E.IDLE, E.WORKING, E.SESSION_START, E.SESSION_END):
                await self._on_activity(st, ev)
            elif ev.kind is E.COMPACTION:
                self._on_compaction(st, ev)
            elif ev.kind is E.CONTEXT and ev.data.get("baseline"):
                self._on_context(st, ev)
            elif ev.kind in (E.CAPACITY, E.RATE_LIMIT) and self._just_relaunched(st):
                self._on_capacity(st, ev)  # its first turn (the join) can hit capacity before it has registered
            elif st.status is Status.DETACHED or st.phase is not Phase.WORKING:
                pass  # not in its project right now, or restarting: no reports, no actions
            else:
                match ev.kind:
                    case E.CAPACITY | E.RATE_LIMIT:
                        self._on_capacity(st, ev)
                    case E.SAFETY_FLAG:
                        self._on_safety(st, ev)
                    case E.SESSION_REFUSAL:
                        st.idle, st.last_error = True, ev.ts
                        self._post(st.project, f"{st.name}: session refused by a safety review "
                                               f"(\"{clean(ev.data.get('text', ''), 160)}\"); it cannot continue.")
                        self._start_cant_continue(st, hard=True, why="session-level refusal")
                    case E.BLOCKED:
                        self._spawn(self._on_permission(st, ev, "block"), f"blocked:{st.name}")
                    case E.DIALOG:
                        self._spawn(self._on_permission(st, ev, "dialog"), f"dialog:{st.name}")
                    case E.CONTEXT:
                        self._on_context(st, ev)
                    case E.USAGE:
                        self._on_usage(st, ev.data.get("windows") or {})
        await self._notify_waiters()

    async def _on_activity(self, st: AgentState, ev: Event) -> None:
        match ev.kind:
            case E.WORKING:
                st.idle, st.last_working = False, ev.ts
            case E.SESSION_START:
                st.idle = True
            case E.SESSION_END:
                st.idle = True
                self._retire_session(st, "its session ended")
            case E.IDLE:
                st.idle, st.last_idle = True, ev.ts
                if ev.source in ("hook:Stop", "rollout") and not ev.data.get("aborted") and not ev.data.get("error") \
                        and ev.ts > st.last_error:
                    st.last_good_turn = ev.ts
                    st.capacity_tries = 0
                    st.safety_flags.clear()
                    if st.name in self.cyber_return and not (st.retry_task and not st.retry_task.done()):
                        st.retry_task = self._spawn(self._cyber_switch_back(st), f"cyber-back:{st.name}")
                if st.pending_auth:
                    await self._flush_auth(st)

    async def _on_channel_ready(self, project: str) -> None:
        names = ", ".join(self.in_project(project)) or "none"
        lines = [f"{self.channel.name} online: watching {names}"]
        for n in self.in_project(project):
            u = self.agents[n].usage
            if u.windows:
                lines.append(summary(n, u, self._usage_order(n), self.now()))
                u.introduced, self.agents[n].usage_posted = True, self.now()
        await self.say(project, "\n".join(lines))
        for p in self.pending.values():
            if p.project == project and not p.resolved:
                await self._post_pending(p)
        gm = self.gm(project)
        if gm:
            await self.channel.invite(project, gm)
            self.last_gm[project] = gm
            self._save_state()

    # ---- plan usage limits ---------------------------------------------------------------------------

    def _usage_order(self, name: str) -> list[str]:
        known = list(self.agents[name].usage.windows)
        return [k for k in ("session", "week", "weekly") if k in known] + sorted(
            k for k in known if k not in ("session", "week", "weekly"))

    def _usage_expected(self, name: str) -> set[str]:
        if self.cfg.agents[name].kind == "codex":
            return {"weekly"}
        return {"session", "week"} | ({"fable"} if self.cfg.mode("usage_check") == "live" else set())

    def _on_usage(self, st: AgentState, windows: dict) -> None:
        for key, kind in update(st.usage, windows, self.cfg.usage_report_left_pct):
            w = st.usage.windows[key]
            if kind == "low":
                self._post(st.project, f"{st.name}: {fmt_window(key, w, self.now())}.")
            else:
                self._post(st.project, f"{st.name}: {LABELS.get(key, key)} limit reset; {w.left_pct}% left.")

    def _usage_intro(self, st: AgentState, force: bool = False) -> None:
        """The one usage line when an agent joins: once everything expected is known (or, forced, whatever is)."""
        u = st.usage
        if u.introduced or not u.windows or st.status is not Status.MANAGED:
            return
        if not force and not self._usage_expected(st.name) <= set(u.windows):
            return
        u.introduced, st.usage_posted = True, self.now()
        self._post(st.project, summary(st.name, u, self._usage_order(st.name), self.now()))

    def _usage_for_gm(self, project: str) -> None:
        """The GM just joined (here, through another watcher, or as a new GM). It doesn't read the channel's backlog,
        so post every agent's usage again, once readings have had a moment to arrive."""
        task = self.gm_usage_task.get(project)
        if task and not task.done():
            return
        self.gm_usage_task[project] = self._spawn(self._post_usage_for_gm(project, self.now()),
                                                  f"gm-usage:{project}")

    async def _post_usage_for_gm(self, project: str, arrived: float) -> None:
        await asyncio.sleep(self.cfg.gm_usage_delay_s)
        due = [st for st in map(self.agents.get, self.in_project(project))
               if st and st.usage.windows and st.status is Status.MANAGED and st.usage_posted < arrived]
        if not due:
            return  # posted since the GM arrived, or no readings yet (its own join line follows)
        lines = [f"Usage, for {self.gm(project)}:"]
        for st in due:
            lines.append(summary(st.name, st.usage, self._usage_order(st.name), self.now()))
            st.usage.introduced, st.usage_posted = True, self.now()  # this is its join line
        await self.say(project, "\n".join(lines))

    def _usage_due(self, st: AgentState) -> bool:
        return (self.cfg.agents[st.name].kind == "claude" and self.cfg.mode("usage_check") != "off"
                and st.status is Status.MANAGED and st.phase is Phase.WORKING and self.active(st.name)
                and st.idle and not st.busy() and not (st.usage_task and not st.usage_task.done())
                and self.now() >= st.usage.next_scrape)

    async def _read_usage(self, st: AgentState) -> None:
        windows = await self.act.claude_usage(st.name, "usage_check")
        per_model = [w.used_pct for k, w in (windows or {}).items() if k not in ("session", "week")]
        st.usage.next_scrape = self.now() + scrape_interval(max(per_model) if per_model else None)
        if windows:
            self._on_usage(st, windows)
        elif self.cfg.mode("usage_check") != "live":
            st.usage.next_scrape = self.now() + 24 * 3600  # dry: one audit line a day

    # ---- capacity / disconnects --------------------------------------------------------------------

    def _just_relaunched(self, st: AgentState) -> bool:
        """Relaunched by the watcher in the last 2 hours and not back in its project yet."""
        return bool(st.launched_at) and self.now() - st.launched_at < 7200 and \
            (st.phase is Phase.LAUNCHING or st.status is Status.DETACHED)

    def _on_capacity(self, st: AgentState, ev: Event) -> None:
        st.idle, st.last_error = True, ev.ts
        if st.retry_task and not st.retry_task.done():
            return
        t = self.cfg.timing
        text = clean(ev.data.get("text", ""), 160)
        claude = self.cfg.agents[st.name].kind == "claude"
        # back off exponentially up to the cap; never give up (an outage can last an hour)
        delay = min(t.capacity_first_delay_s * (2 ** min(st.capacity_tries, 20)), t.capacity_max_delay_s)
        resume_at = self._limit_lifts(st, ev) if ev.kind is E.RATE_LIMIT else None
        if resume_at:  # Claude continues by itself when a plan limit lifts; `continue` is only the fallback
            delay = max(30.0, resume_at - self.now() + (t.claude_limit_grace_s if claude else 60))
        elif ev.kind is E.RATE_LIMIT:
            delay = max(delay, 1800)
        st.capacity_tries += 1
        if st.capacity_tries == t.capacity_max_tries:
            self._spawn(self.notifier.notify(f"agent-watcher: {st.name}", f"Still failing after "
                                             f"{st.capacity_tries} retries, still retrying: {text[:100]}"), "notify")
        then = "Switching model and sending" if self.cfg.codex_rotation and not claude else "Sending"
        per_hour = max(1, round(3600 / max(delay, 1)))
        if ev.kind is E.RATE_LIMIT:
            limit = f"its {ev.data['limit']} limit" if ev.data.get("limit") else "a plan limit"
            fallback = fmt_reset(self.now() + delay, self.now()).removeprefix("resets ")
            if not resume_at:
                plan = f"Reset time unknown; {then.lower()} `continue` in {round(delay / 60)} min."
            elif claude:
                plan = (f"Claude continues by itself then; if it hasn't by {fallback}, the watcher "
                        "sends `continue`.")
            else:
                plan = f"{then} `continue` at {fallback}."
            posted = (ev.data.get("limit"), resume_at)
            if posted != st.limit_posted or not resume_at and st.capacity_tries % per_hour == 0:
                st.limit_posted = posted
                self._post(st.project, f"{st.name} hit {limit} ({fmt_reset(resume_at, self.now())}). {plan}")
        elif st.capacity_tries <= 3 or st.capacity_tries % per_hour == 0:  # don't flood the channel in an outage
            self._post(st.project, f"{st.name}: turn stopped (\"{text}\"). {then} `continue` in {round(delay)}s "
                                   f"(try {st.capacity_tries}; retrying until it works).")
        st.retry_task = self._spawn(self._continue_after(st, ev.ts, delay, text, ev.source == "screen"),
                                    f"retry:{st.name}")

    def _limit_lifts(self, st: AgentState, ev: Event) -> float | None:
        """When a plan limit lifts: from the error text, else from the window the status line or rollout reported
        (the one named, or else any that is used up)."""
        now = self.now()
        if (at := ev.data.get("resume_at")) and at > now:
            return at
        name = ev.data.get("limit") or ""
        keys = {"weekly": ("week", "weekly"), "session": ("session",)}.get(name, (name,))
        windows = [w for k, w in st.usage.windows.items() if k in keys] or \
            [w for w in st.usage.windows.values() if w.left_pct == 0]
        resets = [w.resets_at for w in windows if w.resets_at and w.resets_at > now]
        return max(resets) if resets else None

    async def _continue_after(self, st: AgentState, err_ts: float, delay: float, text: str,
                              from_screen: bool = False) -> None:
        await asyncio.sleep(delay)
        if st.last_working > err_ts or not st.idle or st.name not in self.agents or not self.active(st.name):
            return  # the turn restarted already (by someone, or by Claude itself when a plan limit lifts)
        why = f"after error: {text[:100]}"
        rot = self.cfg.codex_rotation
        if rot and self.cfg.agents[st.name].kind == "codex":
            if from_screen and self.cfg.mode("capacity") == "live" and \
                    codex_screen_capacity(await self.term.screen_text(st.name)) is None:
                log.info("%s: capacity error no longer on screen; not retrying", st.name)
                return
            if not await self.act.codex_switch_model(st.name, "capacity", rot, why=why) \
                    and self.cfg.mode("capacity") == "live":
                self._post(st.project, f"{st.name}: couldn't switch models after \"{text}\"; nothing typed. "
                                       "Owner notified.")
                self._spawn(self.notifier.notify(f"agent-watcher: {st.name}", "Model switch failed; see audit.log"),
                            "notify")
                return
        await self.act.type(st.name, "capacity", T.CONTINUE, why=why)

    # ---- permissions -----------------------------------------------------------------------------

    async def _on_permission(self, st: AgentState, ev: Event, kind: str) -> None:
        if not self.active(st.name):
            return
        if st.project == LOCAL_PROJECT:  # no channel, so no GM to decide; the agent's own reviewer stands
            log.info("%s: %s not tracked (no project): %s", st.name, kind,
                     describe_action(ev.data.get("tool_name", ""), ev.data.get("tool_input") or {})[:200])
            return
        a = self.cfg.agents[st.name]
        tool_name, tool_input = ev.data.get("tool_name", ""), ev.data.get("tool_input") or {}
        desc = describe_action(tool_name, tool_input)
        if kind == "block":
            when, pid, thread = st.approved_blocks.pop(desc, (0, "", None))
            if self.now() - when < 600:  # the approved retry was refused: say so where the decision was made
                self._spawn(self.say(st.project, f"{st.name} was blocked again after the authorization: its reviewer "
                                     f"refused the retry ({clean(ev.data.get('reason', ''), 120) or 'no reason given'})."
                                     " Approving again won't change that; it needs another approach or "
                                     f"<@{self.cfg.owner_slack_id}>.", thread), "post")
                self.act.audit(st.name, "approvals_block", self.cfg.mode("approvals_block"), "REFUSED_AFTER_AUTH",
                               desc, pid)
                return
            if self.now() - st.recent_blocks.get(desc, 0) < 600:
                return
            st.recent_blocks[desc] = self.now()
        pr = self.policy.evaluate(a, tool_name, tool_input, ev.data.get("cwd", ""))
        reviewer = {"claude_auto_mode": "Claude auto mode", "codex_auto_review": "Codex auto-review"}.get(
            ev.data.get("reviewer", ""), "a permission dialog" if kind == "dialog" else "the reviewer")
        if st.name == self.gm(st.project):  # the GM doesn't approve itself
            if self.cfg.mode("gm_assess") == "off" or pr.verdict in (Verdict.OWNER, Verdict.REFUSE):
                self.act.audit(st.name, f"approvals_{kind}", self.cfg.mode(f"approvals_{kind}"), "GM_OWN", desc,
                               f"{pr.verdict.value}: {pr.rule}; left to the owner")
                return
            await self._gm_request(st, ev, kind, desc, reviewer)
            return
        p = Pending(self._new_id(), st.name, st.project, kind, desc, pr.rule,
                    owner_only=pr.verdict in (Verdict.OWNER, Verdict.REFUSE), created=self.now(),
                    session=st.session_id)
        self.act.audit(st.name, f"approvals_{kind}", self.cfg.mode(f"approvals_{kind}"), f"POLICY_{pr.verdict.value}",
                       desc, pr.rule)
        if pr.verdict is Verdict.APPROVE:  # not posted: the audit line above is the record
            p.resolved = "approved"
            self.policy.record_approval(st.name)
            await self._execute(p, approve=True)
            return
        self.pending[p.id] = p
        self._save_state()
        reason = clean(ev.data.get("reason", ""), 300)
        head = f"[{self._qualified(p.id)}] {st.name} blocked by {reviewer}: {desc}."
        if reason:
            head += f" Reason: {reason.rstrip('.')}."
        for note in pr.notes:
            head += f" Note: {clean(note, 200)}."
        opinion = None
        if self.cfg.mode("assess") != "off":  # one post: the request with its second opinion, if that's quick
            opinion = self._spawn(self._assess(p, a, reviewer, reason), f"assess:{p.id}")
            done, _ = await asyncio.wait({opinion}, timeout=self.cfg.assess.wait_s)
            if done:
                text, opinion = opinion.result(), None
                if text and self.cfg.mode("assess") == "live":
                    head += f" {self._opinion_line(text)}"
        p.thread_ts = await self._post_pending(p, head)
        self._save_state()  # with its thread, so a reply there still decides it after a restart
        if opinion is not None:
            self._spawn(self._late_opinion(p, opinion), f"assess-late:{p.id}")
        if pr.verdict is Verdict.REFUSE and kind == "dialog":
            await self._execute(p, approve=False)
        if pr.verdict is Verdict.GM:
            p.reminder = self._spawn(self._remind(p), f"remind:{p.id}")
        elif pr.verdict is Verdict.OWNER:
            await self.notifier.notify(f"agent-watcher: {st.name}", f"Needs your decision: {desc[:120]}")

    async def _gm_request(self, st: AgentState, ev: Event, kind: str, desc: str, reviewer: str) -> None:
        """The GM's own request: the second opinion rules. "Looks project-related" approves it; anything else (or
        no answer) leaves it to the owner, in the channel."""
        p = Pending(self._new_id(), st.name, st.project, kind, desc, "gm_own", owner_only=True, created=self.now(),
                    session=st.session_id)
        reason = clean(ev.data.get("reason", ""), 300)
        mode = self.cfg.mode("gm_assess")
        text = await self._assess(p, self.cfg.agents[st.name], reviewer, reason)
        head = f"[{self._qualified(p.id)}] {st.name} (GM) blocked by {reviewer}: {desc}."
        if reason:
            head += f" Reason: {reason.rstrip('.')}."
        head += f" Second opinion ({self.cfg.assess.model}): " + \
            (text.rstrip(".") + "." if text else "no answer.")
        if text and text.lower().startswith("looks project-related"):
            if mode == "live":
                p.resolved = "approved"
                self.act.audit(st.name, f"approvals_{kind}", self.cfg.mode(f"approvals_{kind}"), "APPROVED", desc,
                               f"by the second opinion: {text}")
                done = await self._execute(p, approve=True)
                if self.cfg.mode(f"approvals_{kind}") != "live":
                    done += f" ({self.cfg.mode(f'approvals_{kind}')} mode: nothing typed)"
                p.thread_ts = await self.say(st.project, f"{head} Approved by the second opinion; {done}.")
                if kind == "block":  # a refused retry is reported in this thread
                    st.approved_blocks[desc] = (self.now(), p.id, p.thread_ts)
                return
            self.act.audit(st.name, "gm_assess", mode, "WOULD_APPROVE", desc, text)
        self.pending[p.id] = p
        await self._post_pending(p, head)
        self._save_state()
        await self.notifier.notify(f"agent-watcher: {st.name}", f"GM needs your decision: {desc[:120]}")

    async def _assess(self, p: Pending, a, reviewer: str, reason: str) -> str | None:
        """A one-line second opinion from a small model: advisory, except on the GM's own requests."""
        text = await self.assessor(agent=p.agent, clone=str(a.clone), state=str(self.cfg.project_state_dir),
                                   reviewer=reviewer, reason=reason, action=p.desc)
        if text:
            self.act.audit(p.agent, "assess", self.cfg.mode("assess"), "ASSESS", text, p.id)
        return text

    def _opinion_line(self, text: str) -> str:
        return f"Second opinion ({self.cfg.assess.model}, advisory): {text.rstrip('.')}."

    async def _late_opinion(self, p: Pending, task: asyncio.Task) -> None:
        """An opinion that missed the request's post goes in its thread."""
        text = await task
        if text and self.cfg.mode("assess") == "live" and not p.resolved:
            await self.say(p.project, self._opinion_line(text), p.thread_ts)

    async def _post_pending(self, p: Pending, head: str | None = None) -> str | None:
        head = head or f"[{self._qualified(p.id)}] still pending for {p.agent}: {p.desc}."
        if p.owner_only and p.rule.startswith("refuse"):
            tail = f" Refused by rule ({p.rule}). Only <@{self.cfg.owner_slack_id}> can override: reply `approve`."
        elif p.owner_only:
            tail = f" Owner-only ({p.rule}): <@{self.cfg.owner_slack_id}>, reply `approve` or `deny` in this thread."
        else:
            tail = " Reply `approve` or `deny` in this thread."
            if p.kind == "dialog" and p.agent in self.cfg.agents and self.cfg.agents[p.agent].kind == "claude":
                tail += " Urgent: Claude Code denies an unanswered dialog by itself after about 2 minutes."
        ts = await self.say(p.project, head + tail)
        if ts:
            p.thread_ts = ts
        return ts

    async def _remind(self, p: Pending) -> None:
        await asyncio.sleep(self.cfg.timing.gm_answer_timeout_s)
        if p.resolved:
            return
        await self.say(p.project, f"No answer after {round(self.cfg.timing.gm_answer_timeout_s / 60)} min; "
                                  f"<@{self.cfg.owner_slack_id}> notified.", p.thread_ts)
        await self.notifier.notify(f"agent-watcher: {p.agent}", f"Unanswered: {p.desc[:120]}")

    async def _decide(self, p: Pending, approve: bool, sender: str, tier: Tier, note: str) -> None:
        if p.resolved:
            await self.say(p.project, f"Already {p.resolved}.", p.thread_ts)
            return
        if p.owner_only and tier is not Tier.OWNER:
            await self.say(p.project, f"Only <@{self.cfg.owner_slack_id}> can decide this one.", p.thread_ts)
            return
        p.resolved = "approved" if approve else "denied"
        self._save_state()
        if approve and p.kind == "block" and p.agent in self.agents:
            self.agents[p.agent].approved_blocks[p.desc] = (self.now(), p.id, p.thread_ts)
        if p.reminder:
            p.reminder.cancel()
        if approve:
            self.policy.record_approval(p.agent)
        self.act.audit(p.agent, f"approvals_{p.kind}", self.cfg.mode(f"approvals_{p.kind}"),
                       "APPROVED" if approve else "DENIED", p.desc, f"by {sender}: {note}")
        if not await self._same_session(p.agent, p.session):
            p.resolved = "closed"
            await self.say(p.project, f"Closed: {p.agent} has restarted since this was posted, so nothing will be "
                                      f"typed. If it still needs this, it will be blocked again.", p.thread_ts)
            return
        done = await self._execute(p, approve)
        what = done if approve or p.kind == "dialog" else "the block stands"
        if self.cfg.mode(f"approvals_{p.kind}") != "live":
            what += f" ({self.cfg.mode(f'approvals_{p.kind}')} mode: nothing typed)"
        await self.say(p.project, f"{'Approved' if approve else 'Denied'} by {sender}; {what}.", p.thread_ts)

    async def _execute(self, p: Pending, approve: bool) -> str:
        st = self.agents[p.agent]
        if p.kind == "block":
            if approve:
                st.pending_auth.append((p.session, p.desc))
                if await self._flush_auth(st, now=True):
                    return "authorization submitted; the agent reads it at its next tool call or when its turn ends"
                self._spawn(self._flush_auth_later(st), f"auth:{st.name}")
                return "authorization will be typed when the agent is idle"
            return ""
        # dialog: only press a key when the dialog is really on screen
        rx = re.compile(self.cfg.keys[self.cfg.agents[p.agent].kind].dialog_regex)
        for _ in range(10):
            if rx.search("\n".join(screen_lines(await self.term.screen_text(p.agent)))):  # NULs as spaces
                break
            await asyncio.sleep(0.5)
        else:
            self.act.audit(p.agent, "approvals_dialog", self.cfg.mode("approvals_dialog"), "SKIP", p.desc,
                           "dialog not visible on screen")
            return "the dialog was no longer on screen (already answered?)"
        await self.act.press(p.agent, "approvals_dialog", "allow" if approve else "deny", p.id)
        return "pressed " + ("Allow" if approve else "Deny") + " in the terminal"

    async def _grant(self, st: AgentState, actions: list[str], sender: str) -> str:
        """Type a GM or owner grant into the agent: now if its prompt box allows, else once it is idle."""
        why = f"grant by {sender}"
        if await self.act.type_now(st.name, "approvals_block", T.GRANT, actions=actions, why=why):
            return "submitted; the agent reads it at its next tool call or when its turn ends"
        session = st.session_id

        async def later() -> None:
            await self._wait_idle(st.name, self.cfg.timing.idle_wait_max_s)
            if await self._same_session(st.name, session):
                await self.act.type(st.name, "approvals_block", T.GRANT, actions=actions, why=why)
        self._spawn(later(), f"grant:{st.name}")
        return "it will be typed when the agent is idle"

    async def _flush_auth_later(self, st: AgentState) -> None:
        await self._wait_idle(st.name, self.cfg.timing.idle_wait_max_s)
        await self._flush_auth(st)

    async def _flush_auth(self, st: AgentState, now: bool = False) -> bool:
        """Type the approved authorizations. now: submit mid-turn if the prompt box allows it, else leave them
        queued (False) for the next idle."""
        if not st.pending_auth or st.name not in self.agents:
            return False
        queued, st.pending_auth = st.pending_auth, []
        current = [(sid, desc) for sid, desc in queued if await self._same_session(st.name, sid)]
        if not current:
            return True
        actions = [desc for _, desc in current]
        if now:
            if await self.act.type_now(st.name, "approvals_block", T.AUTH, actions=actions, why="block approved"):
                return True
            st.pending_auth = current + st.pending_auth
            return False
        await self.act.type(st.name, "approvals_block", T.AUTH, actions=actions, why="block approved")
        return True

    async def _same_session(self, agent: str, session: str | None) -> bool:
        """Is the agent still in `session`? Rescans first, since a restart outside the watcher shows up only there."""
        if self.refresh:
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001 - a failed scan must not let a decision through
                log.exception("discovery scan before typing a decision")
                return False
        st = self.agents.get(agent)
        return st is not None and session is not None and st.session_id == session

    def _retire_session(self, st: AgentState, why: str, keep: str | None = None) -> None:
        """An agent's session is over: close its open requests (except session `keep`'s) and drop approvals not
        yet typed."""
        st.pending_auth.clear()
        for p in self.pending.values():
            if p.agent == st.name and not p.resolved and not (keep and p.session == keep):
                p.resolved = "closed"
                if p.reminder:
                    p.reminder.cancel()
                self._spawn(self.say(p.project, f"Closed: {why}, so nothing will be typed for this request.",
                                     p.thread_ts), f"close:{p.id}")
        self._save_state()

    # ---- context and compactions (tracked, not posted) --------------------------------------------

    def _on_compaction(self, st: AgentState, ev: Event) -> None:
        d = ev.data
        if d.get("baseline"):
            st.compactions = d.get("count") or 0
            st.compaction_floor, st.awaiting_floor = None, False
            if d.get("started_at"):
                st.launched_at = st.launched_at or d["started_at"]
            return
        st.compactions = d.get("count") or st.compactions + 1
        st.context_reported.clear()
        kind = self._kind(st)
        window = st.context_window or (self.cfg.claude_context_window if kind == "claude" else 0)
        if d.get("post") and window:
            st.compaction_floor = round(100 * d["post"] / window)
        else:
            st.awaiting_floor = True
        self.cycle_compactions[st.name] = self.cycle_compactions.get(st.name, 0) + 1
        self._save_state()
        self._log_compaction({"ts": ev.ts, "agent": st.name, "kind": kind, "project": st.project,
                              "count": st.compactions, "pre": d.get("pre"), "post": d.get("post"),
                              "trigger": d.get("trigger")})

    def _kind(self, st: AgentState) -> str:
        a = self.cfg.agents.get(st.name)
        return a.kind if a else ("claude" if st.name.startswith("claude") else "codex")

    def _log_compaction(self, rec: dict) -> None:
        try:
            self.compaction_log.parent.mkdir(parents=True, exist_ok=True)
            with self.compaction_log.open("a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError as e:
            log.warning("compaction log: %s", e)

    def _check_compaction_cycle(self) -> None:
        """At each cohort-schedule boundary, log the cycle's compactions by kind and agent, then start a new one."""
        now = self.now()
        if not self.cycle_end:
            self.cycle_start, self.cycle_end = now, self._next_cohort_time(now)
            self._save_state()
            return
        if now < self.cycle_end:
            return
        by_kind: dict[str, int] = {}
        for name, n in self.cycle_compactions.items():
            k = self.cfg.agents[name].kind if name in self.cfg.agents else (
                "claude" if name.startswith("claude") else "codex")
            by_kind[k] = by_kind.get(k, 0) + n
        summary = {"cycle_start": self.cycle_start, "cycle_end": self.cycle_end, "by_kind": by_kind,
                   "by_agent": dict(sorted(self.cycle_compactions.items()))}
        self._log_compaction(summary)
        log.info("compaction cycle %s-%s: %s", _hhmm(self.cycle_start), _hhmm(self.cycle_end), by_kind or "none")
        self.cycle_start, self.cycle_end = self.cycle_end, self._next_cohort_time(now)
        self.cycle_compactions = {}
        self._save_state()

    def _on_context(self, st: AgentState, ev: Event) -> None:
        st.context_tokens, st.context_window = ev.data.get("tokens") or 0, ev.data.get("window") or 0
        pct = st.context_pct()
        if pct is None:
            return
        if st.awaiting_floor and not ev.data.get("baseline"):
            st.compaction_floor, st.awaiting_floor = pct, False
        if ev.data.get("baseline"):
            st.context_reported = {m for m in self.cfg.context_report_pct if pct >= m}
            return
        crossed = [m for m in self.cfg.context_report_pct if pct >= m and m not in st.context_reported]
        if pct < min(self.cfg.context_report_pct, default=100):
            st.context_reported.clear()
        if crossed:
            st.context_reported.update(crossed)
            self._post(st.project, f"{st.name} context at {pct}% ({_k(st.context_tokens)}/{_k(st.context_window)}); "
                                   f"{st.compactions} compactions since {_hhmm(st.launched_at)}.")

    # ---- safety ----------------------------------------------------------------------------------

    def _on_safety(self, st: AgentState, ev: Event) -> None:
        st.idle, st.last_error = True, ev.ts
        st.safety_flags.append(ev.ts)
        t = self.cfg.timing
        recent = [f for f in st.safety_flags if f > max(ev.ts - t.cant_continue_window_s, st.last_good_turn)]
        rot = self.cfg.codex_cyber
        switch = rot and ev.data.get("error") == "cyber_policy" and self.cfg.agents[st.name].kind == "codex" \
            and len(recent) == 1 and self.active(st.name) and not (st.retry_task and not st.retry_task.done())
        retry = (f" Switching it to {rot.model} and retrying the turn in the next "
                 f"{max(1, round(t.capacity_first_delay_s))} seconds, so don't reassign its work."
                 if switch and self.cfg.mode("cyber_switch") == "live" else "")
        self._post(st.project, f"{st.name}: turn refused by a safety review "
                               f"(\"{clean(ev.data.get('text', ''), 160)}\"), {len(recent)} without a successful turn."
                               + retry)
        if switch:
            st.retry_task = self._spawn(self._cyber_switch(st, ev.ts, rot), f"cyber:{st.name}")
            return
        if st.project == LOCAL_PROJECT:  # no project to restart into: leave it to the owner
            why = "again after switching models" if len(recent) > 1 else "no [codex_cyber] model to switch to"
            self._tell(st, f"Stopped by a cybersecurity block ({why}). It needs you.")
            return
        if len(recent) >= t.cant_continue_flags:
            self._start_cant_continue(st, hard=False, why=f"{len(recent)} safety refusals without a successful turn")

    def _tell(self, st: AgentState, text: str) -> None:
        """The project's channel, or the owner for an agent that isn't in a project."""
        if st.project == LOCAL_PROJECT:
            self._spawn(self.notifier.notify(f"agent-watcher: {st.name}", text), "notify")
        else:
            self._post(st.project, f"{st.name}: {text}")

    async def _cyber_switch(self, st: AgentState, err_ts: float, rot: ModelRotation) -> None:
        """Retry a Codex turn stopped by a cyber block on [codex_cyber]; the next good turn switches it back."""
        await asyncio.sleep(self.cfg.timing.capacity_first_delay_s)
        if st.last_working > err_ts or st.name not in self.agents or not self.active(st.name):
            return  # someone already restarted the turn
        behavior, live = "cyber_switch", self.cfg.mode("cyber_switch") == "live"
        before = codex_status_model(await self.term.screen_text(st.name)) if self.term.bound(st.name) else None
        switched = await self.act.codex_switch_model(st.name, behavior, rot, why="cybersecurity block")
        if not switched and live:
            self._tell(st, f"Cybersecurity block, and the switch to {rot.model} failed; nothing typed. It needs you.")
            return
        if switched and before != rot.model:
            regular = self.cfg.codex_rotation.model if self.cfg.codex_rotation else None
            if back := before or regular:
                self.cyber_return[st.name] = back
                self._save_state()
        await self.act.type(st.name, behavior, T.CONTINUE_CYBER, why="cybersecurity block")
        if live:
            back = self.cyber_return.get(st.name)
            self._tell(st, f"Cybersecurity block: switched to {rot.model} and asked it to continue"
                           + (f"; it goes back to {back} after a successful turn." if back else "."))

    async def _cyber_switch_back(self, st: AgentState) -> None:
        """A good turn on [codex_cyber]: back to the model it had before the block, at the session's effort."""
        back = self.cyber_return.pop(st.name, None)
        self._save_state()
        if not back or st.name not in self.agents or not self.active(st.name):
            return
        rot = ModelRotation(back, [], self.cfg.codex_cyber.confirm_key if self.cfg.codex_cyber else "s")
        if await self.act.codex_switch_model(st.name, "cyber_switch", rot, why="successful turn after a cyber block"):
            self._tell(st, f"Switched back to {back} after a successful turn.")
        elif self.cfg.mode("cyber_switch") == "live":
            self._tell(st, f"Could not switch back to {back}; it is still on the cybersecurity model.")

    def _cohort_running(self, project: str) -> bool:
        f = self.cohort_flow.get(project)
        return f is not None and not f.done()

    def _start_cant_continue(self, st: AgentState, hard: bool, why: str) -> None:
        if st.busy() or not self.active(st.name) or self._cohort_running(st.project):
            return
        st.flow = self._spawn(self._cant_continue(st, hard, why), f"recover:{st.name}")

    async def _cant_continue(self, st: AgentState, hard: bool, why: str) -> None:
        behavior = "cant_continue"
        if self.cfg.mode(behavior) != "live":
            self.act.audit(st.name, behavior, self.cfg.mode(behavior), "RECOVER", why)
            return
        project = st.project
        try:
            if not hard:
                await self.say(project, f"{st.name} can't continue ({why}); asking it to sync carefully, then "
                                        f"restarting.")
                t0 = self.now()
                await self.act.type(st.name, behavior, T.SYNC_CAREFUL, why=why)
                await self.wait_until(lambda: st.last_idle > t0, self.cfg.timing.careful_sync_timeout_s)
            else:
                await self.say(project, f"{st.name}: relaunching in a fresh session with the recovery prompt ({why}).")
            await self._exit_and_launch(st.name, behavior, project, recovery=True)
        finally:
            st.phase = Phase.WORKING

    # ---- restarts and moves ------------------------------------------------------------------------

    def request_restart(self, agent: str, why: str, project: str | None = None) -> str | None:
        """Restart `agent` into `project` (default: its current project; another project means a move)."""
        st = self.agents[agent]
        if st.busy():
            return f"{agent} is already restarting."
        if self._cohort_running(st.project):
            return "a cohort restart is in progress."
        if not self.active(agent):
            return f"{agent} is paused."
        st.flow = self._spawn(self._restart(agent, why, project or st.project), f"restart:{agent}")
        return None

    async def _restart(self, agent: str, why: str, project: str) -> None:
        st = self.agents[agent]
        src = st.project
        moving = project != src
        mode = self.cfg.mode("restart")
        verb = f"move {agent} to {project}" if moving else f"restart {agent}"
        if mode != "live":
            self.act.audit(agent, "restart", mode, "MOVE" if moving else "RESTART", f"{project}: {why}")
            await self.say(src, f"({mode}) would {verb}.")
            return
        try:
            await self._wait_idle(agent, self.cfg.timing.idle_wait_max_s)
            await self.say(src, f"{'Moving' if moving else 'Restarting'} {agent}"
                                f"{f' to {project}' if moving else ''} ({why}).")
            if moving:
                await self.say(project, f"{agent} is moving here from {src} ({why}).")
            ok = await self._exit_and_launch(agent, "restart", project, recovery=False)
            done = f"{agent} {'moved to ' + project if moving else 'restarted'}." if ok else \
                f"{agent} relaunched but hasn't joined {project} yet; owner notified."
            for p in dict.fromkeys([src, project]):
                await self.say(p, done)
        finally:
            st.phase = Phase.WORKING

    async def _exit_and_launch(self, agent: str, behavior: str, project: str, recovery: bool) -> bool:
        """Quit (see Actuator.quit), pre-launch, relaunch with "join project <project>". Done when the new session
        registers there."""
        st, t, a = self.agents[agent], self.cfg.timing, self.cfg.agents[agent]
        st.phase = Phase.EXITING
        self._retire_session(st, "it is being restarted")
        if st.retry_task:
            st.retry_task.cancel()
        old_session = st.session_id

        async def agent_running() -> bool:
            job = (await self.term.job_name(agent) or "").lstrip("-")
            return job in a.process_names

        quit_ok, how = await self.act.quit(agent, behavior, why="restart")
        if not quit_ok:
            await self.say(project, f"{agent}: not restarting; couldn't quit it ({how}).")
            await self.notifier.notify(f"agent-watcher: {agent}", f"Restart stopped: couldn't quit ({how})")
            st.phase = Phase.WORKING
            return False
        if not await self._poll(agent_running, want=False, timeout=t.exit_timeout_s):
            await self.act.interrupt(agent, behavior, why="agent did not exit")
            if not await self._poll(agent_running, want=False, timeout=t.exit_timeout_s):
                await self.notifier.notify(f"agent-watcher: {agent}", "Agent did not exit; restart stopped")
                return False
        st.phase = Phase.LAUNCHING
        await self.act.shell_pre_launch(agent, behavior)
        t1 = self.now()
        await self.act.shell_launch(agent, behavior, project, recovery=recovery)
        st.idle, st.capacity_tries, st.compactions = False, 0, 0
        st.safety_flags.clear()
        st.compaction_floor, st.awaiting_floor = None, False
        st.context_reported.clear()
        st.launched_at = t1
        # its usage line is due once it rejoins; attach() can't tell, since the new session is often seen (and its
        # id taken) before it registers
        st.usage.introduced, st.usage_since = False, t1
        joined = await self.wait_until(
            lambda: st.status is Status.MANAGED and st.project == project and st.session_id != old_session,
            t.online_timeout_s)
        if not joined:
            await self.notifier.notify(f"agent-watcher: {agent}", f"Relaunched, but it hasn't joined {project}")
        st.last_restart = self.now()
        st.phase = Phase.WORKING
        return joined

    async def _poll(self, check, want: bool, timeout: float, every: float = 1.0) -> bool:
        deadline = self.now() + timeout
        while self.now() < deadline:
            if (await check()) == want:
                return True
            await asyncio.sleep(every)
        return (await check()) == want

    # ---- cohort restart (per project) ----------------------------------------------------------

    async def start_cohort(self, project: str, why: str, ask_gm: bool) -> str | None:
        """GM-host role: ask the project's GM to have the cohort sync (unless the GM itself asked)."""
        if not self.hosts_gm(project):
            return None  # another watcher hosts this project's GM
        gm = self.gm(project)
        mode = self.cfg.mode("cohort_restart")
        if mode != "live" or self.paused:
            self.act.audit(gm, "cohort_restart", mode, "COHORT_START", f"{project}: {why}")
            return f"({mode}) would start a cohort restart."
        if ask_gm:
            await self._wait_idle(gm, self.cfg.timing.idle_wait_max_s)
            await self.act.type(gm, "cohort_restart", T.SYNC_COHORT, why=why)
        await self.say(project, f"Cohort restart started ({why}). Waiting for the GM's `cohort ready`.")
        return None

    def on_cohort_ready(self, project: str, ts: float) -> None:
        self.cohort_ready_at[project] = ts
        if self._cohort_running(project):
            return
        self.cohort_flow[project] = self._spawn(self._cohort_restart(project, ts), f"cohort:{project}")

    async def _cohort_restart(self, project: str, ready_ts: float) -> None:
        mode = self.cfg.mode("cohort_restart")
        gm = self.gm(project)
        local = [n for n in self.in_project(project) if n != gm and self.term.bound(n) and self.active(n)]
        if mode != "live" or self.paused:
            self.act.audit(gm or "-", "cohort_restart", mode, "COHORT_RESTART", f"{project}: {', '.join(local)}")
            await self.say(project, f"({mode}) would restart {', '.join(local) or 'no agents'} for the cohort.")
            return
        t = self.cfg.timing
        for n in local:
            self.agents[n].phase = Phase.COHORT
        try:
            tasks = []
            for i, n in enumerate(local):
                tasks.append(self._spawn(self._exit_and_launch(n, "cohort_restart", project, recovery=False),
                                         f"cohort:{n}"))
                if i < len(local) - 1:
                    await asyncio.sleep(t.cohort_stagger_s)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            ok = [n for n, r in zip(local, results) if r is True]
            failed = [n for n, r in zip(local, results) if r is not True]
            msg = f"cohort done: {self.channel.name} restarted {', '.join(ok) or 'none'}"
            if failed:
                msg += f"; failed: {', '.join(failed)}"
            await self.say(project, msg)
            if self.hosts_gm(project) and self.active(gm):
                peers = set(self.peers.get(project, {}))
                done = self.peer_done.setdefault(project, {})
                await self.wait_until(lambda: all(done.get(p, 0) >= ready_ts for p in peers), t.cohort_peer_timeout_s)
                self.agents[gm].phase = Phase.COHORT
                await self.say(project, f"Restarting the GM, {gm}, last.")
                gm_ok = await self._exit_and_launch(gm, "cohort_restart", project, recovery=False)
                await self.say(project, f"{gm} restarted." if gm_ok else f"{gm} restart failed; owner notified.")
        finally:
            for n in self.in_project(project):
                self.agents[n].phase = Phase.WORKING

    def _next_cohort_time(self, after: float) -> float:
        t = self.cfg.timing
        hh, mm = (int(x) for x in t.cohort_restart_anchor.split(":"))
        now = dt.datetime.fromtimestamp(after).astimezone()
        anchor = now.replace(hour=hh, minute=mm, second=0, microsecond=0) - dt.timedelta(days=1)
        step = dt.timedelta(hours=t.cohort_restart_every_h)
        while anchor.timestamp() <= after:
            anchor += step
        return anchor.timestamp()

    async def tick(self) -> None:
        """Once a minute: per-project schedules, GM changes, detached grace, the compaction cycle."""
        self._check_compaction_cycle()
        for project in self.channel.projects():
            await self._check_gm_change(project)
            nxt = self.next_cohort_at.get(project, 0)
            if not nxt or nxt < self.now() - 3600:
                self.next_cohort_at[project] = self._next_cohort_time(self.now())
                self._save_state()
            elif self.now() >= nxt:
                self.next_cohort_at[project] = self._next_cohort_time(self.now())
                self._save_state()
                if self.cfg.timing.cohort_restart_scheduled and self.cfg.mode("cohort_restart") != "off" \
                        and self.hosts_gm(project):
                    await self.start_cohort(project, "scheduled", ask_gm=True)
        for st in list(self.agents.values()):
            if not st.usage.introduced:  # from the tick, so readings from every source have arrived
                self._usage_intro(st, force=self.now() - st.usage_since > self.cfg.usage_intro_wait_s)
            if self._usage_due(st):
                st.usage_task = self._spawn(self._read_usage(st), f"usage:{st.name}")
        for name, st in list(self.agents.items()):
            if st.status is Status.DETACHED and not st.busy() and \
                    self.now() - st.detached_since > self.cfg.timing.detached_grace_s:
                self.drop(name, "its session left the project")

    async def scheduler(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
            await asyncio.sleep(60)

    async def _check_gm_change(self, project: str) -> None:
        gm = self.gm(project)
        if gm and gm != self.last_gm.get(project) and self.channel.ready(project):
            await self.channel.invite(project, gm)
            open_items = [p for p in self.pending.values() if p.project == project and not p.resolved]
            await self.say(project, f"The GM is now {gm}." + (
                f" Pending: {', '.join(self._qualified(p.id) for p in open_items)}." if open_items else ""))
            self.last_gm[project] = gm
            self._save_state()
            self._usage_for_gm(project)

    # ---- channel messages ---------------------------------------------------------------------------

    async def _on_message(self, ev: Event) -> None:
        d = ev.data
        project, text, sender, tier = d["project"], d.get("text", ""), d.get("sender", "?"), d["tier"]
        cmd = parse(text, self.channel.user_id, in_thread=bool(d.get("thread_ts")))
        if cmd and cmd.verb == "gm" and not d.get("thread_ts"):
            await self._on_gm_post(project, cmd.agent, sender, tier, bool(d.get("is_bot")))
            return
        if d.get("is_bot") and tier is not Tier.GM:
            self._on_peer(project, sender, text, ev.ts)
            return
        if cmd is None:
            return
        if tier not in (Tier.OWNER, Tier.AUTHORIZED_HUMAN, Tier.GM):
            log.info("ignoring %s from %s (not the owner, an authorized human, or the GM)", cmd.verb, sender)
            return
        reply_to = d.get("thread_ts") or d.get("ts")
        await self._dispatch(cmd, project, sender, tier, d.get("thread_ts"), reply_to, ev.ts)

    async def _on_gm_post(self, project: str, agent: str | None, sender: str, tier: Tier, is_bot: bool) -> None:
        """`gm <agent>`: from the owner or an authorized human, the current GM handing over, or a cohort agent
        announcing itself. It names the GM until the next such post or a change to gm.json."""
        known = set(self.in_project(project)) | set().union(*self.peers.get(project, {}).values())
        if agent and agent == self.gm(project):
            if sender == agent or tier in (Tier.OWNER, Tier.AUTHORIZED_HUMAN, Tier.GM):
                self._usage_for_gm(project)  # the GM announces itself when it joins
            return
        if not agent:
            return
        if tier in (Tier.OWNER, Tier.AUTHORIZED_HUMAN, Tier.GM):
            pass
        elif not (is_bot and sender == agent and agent in known):
            log.info("ignoring `gm %s` from %s (not the owner, the GM, or %s itself in %s)", agent, sender, agent,
                     project)
            return
        self.gm_announced[project] = agent
        self._save_state()
        await self._check_gm_change(project)

    def _on_peer(self, project: str, sender: str, text: str, ts: float) -> None:
        body = text.strip()
        if (m := _PEER_ARRIVAL.match(body)) and (m.group(1) or m.group(2)) == self.gm(project):
            self._usage_for_gm(project)  # the GM joined through another watcher
        if m := _PEER_POST.match(body):
            name = (m.group(1) or m.group(2) or m.group(3)).lower()
            if name != self.channel.name.lower() and name not in self.peer_watchers.get(project, []):
                self.peer_watchers.setdefault(project, []).append(name)
                self._save_state()
        if " online: watching " in body:
            agents = body.split(" online: watching ", 1)[1]
            self.peers.setdefault(project, {})[sender] = \
                set() if agents.strip() == "none" else {a.strip() for a in agents.split(",")}
        elif body.startswith("cohort done:"):
            self.peers.setdefault(project, {}).setdefault(sender, set())
            self.peer_done.setdefault(project, {})[sender] = ts

    def _target(self, agent: str | None, project: str, tier: Tier) -> AgentState | None:
        """The agent a command in `project`'s channel may act on: in that project, or any of ours for humans."""
        st = self.agents.get(agent or "")
        if st is None:
            return None
        if st.project == project or tier in (Tier.OWNER, Tier.AUTHORIZED_HUMAN):
            return st
        return None

    async def _dispatch(self, cmd: Command, project: str, sender: str, tier: Tier, thread_ts: str | None,
                        reply_to: str | None, ts: float) -> None:
        match cmd.verb:
            case "approve" | "deny":
                if cmd.event_bot not in (None, self.channel.name.lower()):
                    return  # another watcher's event
                in_thread = next((x for x in self.pending.values() if thread_ts and x.thread_ts == thread_ts), None)
                p = self.pending.get(cmd.event_id) if cmd.event_id else in_thread
                if p is not None and cmd.event_id and cmd.event_bot is None and p is not in_thread \
                        and self.peer_watchers.get(project):
                    # Every watcher numbers its own requests, so with another watcher here a bare id outside
                    # the request's own thread could name either watcher's request.
                    if not thread_ts and p.project == project:
                        await self.say(project, f"More than one watcher is in this channel, so `{cmd.event_id}` "
                                                f"alone is ambiguous: post `{cmd.verb} {self._qualified(p.id)}` or "
                                                "reply in the request's thread.", reply_to)
                    return
                if p is None or p.project != project:
                    return  # another watcher's event, another project's, or not an event thread
                await self._decide(p, cmd.verb == "approve", sender, tier, cmd.note)
            case "restart" | "move":
                st = self._target(cmd.agent, project, tier)
                if st is None:
                    return
                dest = st.project
                if cmd.verb == "move":
                    dest = (cmd.target or "").removesuffix(CHANNEL_SUFFIX)
                    if dest not in self.channel.projects():
                        await self.say(project, f"Can't move {st.name}: no #{dest}{CHANNEL_SUFFIX} here.", reply_to)
                        return
                err = self.request_restart(st.name, f"requested by {sender}", dest)
                if err:
                    await self.say(project, f"Can't {cmd.verb} {st.name}: {err}", reply_to)
            case "restart_cohort":
                msg = await self.start_cohort(project, f"requested by {sender}", ask_gm=tier is not Tier.GM)
                if msg:
                    await self.say(project, msg, reply_to)
            case "cohort_ready":
                if tier in (Tier.GM, Tier.OWNER):
                    self.on_cohort_ready(project, ts)
            case "grant":
                st = self._target(cmd.agent, project, tier)
                if st is None:
                    return  # another watcher's agent, or not in this project
                if tier is Tier.GM and st.name == self.gm(project):
                    await self.say(project, f"You can't grant yourself authorization; ask <@{self.cfg.owner_slack_id}>.",
                                   reply_to)
                    return
                who = f"{sender}, the project's GM," if tier is Tier.GM else f"{sender}, the project's owner,"
                task = clean(cmd.note, 600).rstrip(".")
                msg = await self._grant(st, [f"{who} has authorized this task for you: {task}."], sender)
                await self.say(project, f"Granted to {st.name}: {msg}.", reply_to)
            case "continue":
                st = self._target(cmd.agent, project, tier)
                if st:
                    await self.act.type(st.name, "capacity", T.CONTINUE, why=f"requested by {sender}")
                    await self.say(project, f"Sent `continue` to {st.name}.", reply_to)
            case "pause" | "resume":
                val = cmd.verb == "pause"
                if cmd.agent == "all":
                    if tier is Tier.GM:
                        for n in self.in_project(project):
                            self._set_paused(n, val)
                    else:
                        self.paused = val
                else:
                    st = self._target(cmd.agent, project, tier)
                    if st is None:
                        return
                    self._set_paused(st.name, val)
                self._save_state()
                await self.say(project, f"{'Paused' if val else 'Resumed'} {cmd.agent} on {self.channel.name}.",
                               reply_to)
            case "status":
                if cmd.agent and self._target(cmd.agent, project, tier) is None:
                    return
                await self.say(project, self.status_text(project, cmd.agent), reply_to)
            case "unknown":
                await self.say(project, f"Not a command I know. {HELP}", reply_to)

    def _set_paused(self, name: str, val: bool) -> None:
        self.agents[name].paused = val
        (self.paused_names.add if val else self.paused_names.discard)(name)

    # ---- status / local control --------------------------------------------------------------------

    def status_text(self, project: str, agent: str | None = None) -> str:
        lines = [f"{self.channel.name}{' (paused)' if self.paused else ''} in {project}, GM {self.gm(project) or '?'}:"]
        names = [agent] if agent else self.in_project(project)
        for n in names:
            s = self.agents[n]
            pct = s.context_pct()
            open_n = sum(1 for p in self.pending.values() if p.agent == n and not p.resolved)
            bits = [
                "unbound" if not self.term.bound(n) else ("idle" if s.idle else "working"),
                s.status.value if s.status is not Status.MANAGED else "",
                s.phase.value if s.phase is not Phase.WORKING else "",
                "paused" if s.paused else "",
                f"context {pct}%" if pct is not None else "",
                f"{s.compactions} compactions since {_hhmm(s.launched_at)}"
                + (f" (floor {s.compaction_floor}%)" if s.compaction_floor is not None else ""),
                f"{open_n} pending" if open_n else "",
                "; ".join(fmt_window(k, s.usage.windows[k], self.now()) for k in self._usage_order(n)),
            ]
            lines.append(f"• {n}: " + ", ".join(b for b in bits if b))
        if not names:
            lines.append("• no agents of mine in this project")
        return "\n".join(lines)

    def status(self) -> dict:
        return {
            "paused": self.paused,
            "projects": {p: {"gm": self.gm(p), "agents": self.in_project(p),
                             "next_cohort_restart": dt.datetime.fromtimestamp(self.next_cohort_at[p]).isoformat()
                             if self.next_cohort_at.get(p) and self.cfg.timing.cohort_restart_scheduled else None}
                         for p in sorted(self.channel.projects())},
            "modes": self.cfg.modes,
            "agents": {
                n: {"project": s.project, "status": s.status.value, "home": str(s.home), "session": s.session_id,
                    "bound": self.term.bound(n), "phase": s.phase.value, "idle": s.idle, "paused": s.paused,
                    "context_pct": s.context_pct(), "compactions": s.compactions,
                    "compactions_this_cycle": self.cycle_compactions.get(n, 0),
                    "usage": {k: {"left_pct": w.left_pct, "resets_at": w.resets_at} for k, w in s.usage.windows.items()},
                    "pending": [p.id for p in self.pending.values() if p.agent == n and not p.resolved]}
                for n, s in self.agents.items()
            },
        }

    def control(self, cmd: str, arg: str | None) -> dict:
        """Local CLI control (the owner at the console)."""
        if cmd == "status":
            return self.status()
        if cmd == "restart":
            if arg and arg.startswith("cohort:"):
                project = arg.split(":", 1)[1]
                self._spawn(self.start_cohort(project, "requested locally", ask_gm=True), "cohort-start")
                return {"ok": True}
            if arg not in self.agents:
                return {"ok": False, "error": f"unknown agent {arg}"}
            err = self.request_restart(arg, "requested locally")
            return {"ok": err is None, "error": err}
        if cmd in ("pause", "resume"):
            val = cmd == "pause"
            if arg:
                if arg not in self.agents:
                    return {"ok": False, "error": f"unknown agent {arg}"}
                self._set_paused(arg, val)
            else:
                self.paused = val
            self._save_state()
            return {"ok": True}
        if cmd == "usage":
            if arg not in self.agents:
                return {"ok": False, "error": f"unknown agent {arg}"}
            if self.cfg.agents[arg].kind != "claude" or self.cfg.mode("usage_check") == "off":
                return {"ok": False, "error": f"no /usage reads for {arg} (Claude agents, usage_check not off)"}
            self.agents[arg].usage.next_scrape = 0  # due now; the tick reads it once the agent is idle
            return {"ok": True, "note": f"{arg}: /usage is read on the next tick it's idle"}
        return {"ok": False, "error": f"unknown command {cmd}"}
