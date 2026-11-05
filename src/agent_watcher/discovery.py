"""Discovery of running agents (no configured list).

Chain, per running `claude` / `codex` process that lives in a tmux pane or an iTerm2 tab:
  process env  -> AGENT_HOME (CLAUDE_CONFIG_DIR / CODEX_HOME, default ~/.claude / ~/.codex),
                  terminal (TMUX + TMUX_PANE, else ITERM_SESSION_ID)
  process cwd  -> clone
  home + cwd   -> current session id (Claude: hook session_id, else newest transcript; Codex: newest main rollout)
  state.json   -> the listener's cohort registrations for that session: bot name + project

Shell commands go through an injectable runner so everything can be tested with fixtures.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .detect import rollout_meta
from .tmux import pane_ref

Runner = Callable[[list[str]], str]

HOME_VARS = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}
DEFAULT_HOMES = {"claude": "~/.claude", "codex": "~/.codex"}
_ENV = re.compile(r"(?:^|\s)(CLAUDE_CONFIG_DIR|CODEX_HOME|ITERM_SESSION_ID|TMUX|TMUX_PANE)=(\S+)")

# Codex flags worth keeping when rebuilding a launch command (flag -> takes a value).
_CODEX_FLAGS = {"--remote": True, "-C": True, "--cd": True, "-m": True, "--model": True, "-c": True,
                "--config": True, "-p": True, "--profile": True, "-s": True, "--sandbox": True,
                "-a": True, "--ask-for-approval": True, "--search": False, "--oss": False}


def run_cmd(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def session_guid(iterm_session_id: str | None) -> str | None:
    """$ITERM_SESSION_ID is "w0t0p0:<GUID>"; iTerm2's API calls the GUID the session_id."""
    if not iterm_session_id:
        return None
    return iterm_session_id.split(":", 1)[-1] or None


@dataclass
class ProcInfo:
    argv: list[str]
    env: dict[str, str]
    cwd: Path


def linux_proc_info(pid: int, proc: Path = Path("/proc")) -> ProcInfo | None:
    """A process's argv, environment and cwd from /proc; None if it's gone or belongs to another user."""
    base = proc / str(pid)
    try:
        argv = [os.fsdecode(a) for a in (base / "cmdline").read_bytes().split(b"\0") if a]
        pairs = (os.fsdecode(e).partition("=") for e in (base / "environ").read_bytes().split(b"\0"))
        env = {k: v for k, _, v in pairs if k}
        cwd = Path(os.readlink(base / "cwd"))
    except OSError:
        return None
    return ProcInfo(argv, env, cwd) if argv else None


def mac_proc_info(pid: int, run: Runner) -> ProcInfo | None:
    """The same from `ps` and `lsof`: macOS has no /proc."""
    args = run(["ps", "-ww", "-p", str(pid), "-o", "command="]).rstrip("\n")
    full = run(["ps", "-wwE", "-p", str(pid), "-o", "command="]).rstrip("\n")
    if not args or not full:
        return None
    env = dict(_ENV.findall(full[len(args):] if full.startswith(args) else full))
    cwd_out = run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
    cwd = next((Path(ln[1:]) for ln in cwd_out.splitlines() if ln.startswith("n")), None)
    if cwd is None:
        return None
    try:
        argv = shlex.split(args)
    except ValueError:
        argv = args.split()
    return ProcInfo(argv, env, cwd)


# Codex files a rollout under the local date its session started, and a session can run for days. Files in the day
# folders around today are always candidates; an older folder's file counts while it is still being written.
ROLLOUT_ACTIVE_S = 3 * 86400


def day_dirs(root: Path, deltas: tuple[int, ...] = (-2, -1, 0, 1)) -> list[Path]:
    today = dt.date.today()
    days = [today + dt.timedelta(days=n) for n in deltas]
    return [root / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}" for d in days]


def rollout_files(root: Path, deltas: tuple[int, ...] = (-2, -1, 0, 1), every_day: bool = False,
                  known: set[Path] | None = None) -> list[Path]:
    """Rollouts under a Codex sessions root, oldest written first: those in the day folders around today, plus
    any in an older folder (every one of them when `every_day`, else those in `known`) written in the last
    ROLLOUT_ACTIVE_S."""
    recent: set[Path] = set()
    for d in day_dirs(root, deltas):
        if d.is_dir():
            recent.update(d.glob("rollout-*.jsonl"))
    older = set(root.glob("*/*/*/rollout-*.jsonl")) if every_day and root.is_dir() else set(known or ())
    since = dt.datetime.now().timestamp() - ROLLOUT_ACTIVE_S
    out: list[tuple[float, Path]] = []
    for p in recent | older:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if p in recent or mtime >= since:
            out.append((mtime, p))
    return [p for _, p in sorted(out)]


def transcript_dir(home: Path, cwd: Path) -> Path:
    return home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


@dataclass
class Registration:
    project: str
    agent: str  # bot name
    session_id: str


@dataclass
class AgentRecord:
    kind: str
    home: Path
    pid: int
    term_id: str  # "tmux:<pane>:<socket>" or "iterm:<session GUID>"
    cwd: Path
    argv: list[str]
    session_id: str | None = None
    regs: list[Registration] = field(default_factory=list)

    def launch_command(self) -> str:
        """A shell command that relaunches this agent the same way; `{prompt}` marks the prompt."""
        def q(s) -> str:
            return shlex.quote(str(s)).replace("{", "{{").replace("}", "}}")
        default = Path(DEFAULT_HOMES[self.kind]).expanduser()
        env = "" if self.home == default else f"{HOME_VARS[self.kind]}={q(self.home)} "
        if self.kind == "claude":
            return f"cd {q(self.cwd)} && {env}claude {{prompt}}"
        kept = [self.argv[0] if self.argv else "codex"]
        i = 1
        while i < len(self.argv):
            a = self.argv[i]
            flag, _, val = a.partition("=")
            if flag in _CODEX_FLAGS:
                if _CODEX_FLAGS[flag] and not val:
                    if i + 1 < len(self.argv):
                        kept += [a, self.argv[i + 1]]
                    i += 2
                    continue
                kept.append(a)
            i += 1
        env = f"{HOME_VARS['codex']}={q(self.home)} "  # explicit: start-codex scripts set it
        return f"cd {q(self.cwd)} && {env}{' '.join(q(x) for x in kept)} {{prompt}}"


class Discovery:
    def __init__(self, run: Runner = run_cmd, now: Callable[[], float] | None = None, platform: str = sys.platform):
        self.run = run
        self.platform = platform
        self.now = now or (lambda: dt.datetime.now().timestamp())
        self.hook_sessions: dict[Path, str] = {}  # Claude home -> session id from its latest hook
        self._rollout_meta: dict[Path, dict] = {}
        self._user_rollouts: dict[Path, set[Path]] = {}  # sessions root -> main-thread rollouts seen there

    # ---- processes ----------------------------------------------------------------------------

    def _pids(self, name: str) -> list[int]:
        return [int(x) for x in self.run(["pgrep", "-x", name]).split() if x.isdigit()]

    def _proc(self, pid: int, kind: str) -> AgentRecord | None:
        tty = self.run(["ps", "-p", str(pid), "-o", "tty="]).strip()
        if not tty or tty.startswith("?"):
            return None  # no terminal: a daemon (Codex app-server, `claude daemon`), not an agent session
        info = linux_proc_info(pid) if self.platform == "linux" else mac_proc_info(pid, self.run)
        if info is None:
            return None
        env = info.env
        # tmux first: a tmux server started from iTerm2 hands that tab's ITERM_SESSION_ID to every pane.
        # iTerm2 runs only on macOS; anywhere else the variable came along over ssh and there's no tab to drive.
        pane = pane_ref(env.get("TMUX"), env.get("TMUX_PANE"))
        guid = session_guid(env.get("ITERM_SESSION_ID")) if self.platform == "darwin" else None
        term_id = f"tmux:{pane}" if pane else f"iterm:{guid}" if guid else None
        if not term_id:
            return None  # not in a tmux pane or an iTerm2 tab (e.g. the Codex app-server)
        home = Path(env.get(HOME_VARS[kind]) or DEFAULT_HOMES[kind]).expanduser()
        return AgentRecord(kind, home, pid, term_id, info.cwd, info.argv)

    # ---- sessions -----------------------------------------------------------------------------

    def claude_session(self, home: Path, cwd: Path) -> str | None:
        if home in self.hook_sessions:
            return self.hook_sessions[home]
        d = transcript_dir(home, cwd)
        files = list(d.glob("*.jsonl")) if d.is_dir() else []
        return max(files, key=lambda p: p.stat().st_mtime).stem if files else None

    def codex_sessions(self, home: Path, cwd: Path) -> list[tuple[float, float, str]]:
        """The user sessions under cwd that are recent or still written, as (mtime, started, id), newest written
        first. The first look at a home searches every day folder (see rollout_files)."""
        root = home / "sessions"
        first = root not in self._user_rollouts
        known = self._user_rollouts.setdefault(root, set())
        found: list[tuple[float, float, str]] = []
        for p in rollout_files(root, every_day=first, known=known):
            meta = self._rollout_meta.get(p)
            if meta is None:
                try:
                    with p.open() as f:
                        meta = rollout_meta(f.readline())
                except OSError:
                    continue
                if not meta:
                    continue
                self._rollout_meta[p] = meta
            if meta.get("thread_source") != "user":
                continue
            known.add(p)  # still a candidate once its day folder is no longer near today
            mcwd = Path(meta.get("cwd", "/nonexistent"))
            if mcwd != cwd and cwd not in mcwd.parents:
                continue
            sid = meta.get("id") or meta.get("session_id")
            if sid:
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                found.append((mtime, _started(meta.get("timestamp"), mtime), sid))
        return sorted(found, reverse=True)

    def codex_session(self, home: Path, cwd: Path) -> str | None:
        """The newest-written session, ignoring any that started before the one registered in a project: the
        Codex app-server can keep writing to an agent's earlier sessions, which must not displace its registered
        one. A session started after it (a restart) still takes over."""
        found = self.codex_sessions(home, cwd)
        registered = [f for f in found if self.registrations(home, f[2])]
        if registered:
            since = max(f[1] for f in registered)
            found = [f for f in found if f[1] >= since]
        return found[0][2] if found else None

    @staticmethod
    def registrations(home: Path, session_id: str | None) -> list[Registration]:
        if not session_id:
            return []
        try:
            state = json.loads((home / "slack-agent-chat" / "state.json").read_text())
        except (OSError, json.JSONDecodeError):
            return []
        out = []
        for reg in (state.get("cohort") or {}).values():
            if isinstance(reg, dict) and reg.get("session_id") == session_id and reg.get("project") \
                    and reg.get("agent"):
                out.append(Registration(reg["project"], reg["agent"], session_id))
        return out

    # ---- scan ---------------------------------------------------------------------------------

    def scan(self) -> list[AgentRecord]:
        records: dict[tuple[Path, str], AgentRecord] = {}
        for kind in ("claude", "codex"):
            for pid in self._pids(kind):
                rec = self._proc(pid, kind)
                if rec is None:
                    continue
                key = (rec.home, rec.term_id)
                if key in records and records[key].pid < pid:
                    continue  # one tab may hold a wrapper and its child; keep the parent
                records[key] = rec
        for rec in records.values():
            rec.session_id = (self.claude_session(rec.home, rec.cwd) if rec.kind == "claude"
                              else self.codex_session(rec.home, rec.cwd))
            rec.regs = self.registrations(rec.home, rec.session_id)
        return list(records.values())


def _started(stamp: str | None, fallback: float) -> float:
    try:
        return dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return fallback


def state_files(records: list[AgentRecord]) -> list[Path]:
    return [r.home / "slack-agent-chat" / "state.json" for r in records]
