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
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .detect import rollout_meta
from .iterm import session_guid
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
    def __init__(self, run: Runner = run_cmd, now: Callable[[], float] | None = None):
        self.run = run
        self.now = now or (lambda: dt.datetime.now().timestamp())
        self.hook_sessions: dict[Path, str] = {}  # Claude home -> session id from its latest hook
        self._rollout_meta: dict[Path, dict] = {}

    # ---- processes ----------------------------------------------------------------------------

    def _pids(self, name: str) -> list[int]:
        return [int(x) for x in self.run(["pgrep", "-x", name]).split() if x.isdigit()]

    def _proc(self, pid: int, kind: str) -> AgentRecord | None:
        tty = self.run(["ps", "-p", str(pid), "-o", "tty="]).strip()
        if not tty or tty.startswith("?"):
            return None  # no terminal: a daemon (Codex app-server, `claude daemon`), not an agent session
        args = self.run(["ps", "-ww", "-p", str(pid), "-o", "command="]).rstrip("\n")
        full = self.run(["ps", "-wwE", "-p", str(pid), "-o", "command="]).rstrip("\n")
        if not args or not full:
            return None
        env = dict(_ENV.findall(full[len(args):] if full.startswith(args) else full))
        # tmux first: a tmux server started from iTerm2 hands that tab's ITERM_SESSION_ID to every pane.
        pane, guid = pane_ref(env.get("TMUX"), env.get("TMUX_PANE")), session_guid(env.get("ITERM_SESSION_ID"))
        term_id = f"tmux:{pane}" if pane else f"iterm:{guid}" if guid else None
        if not term_id:
            return None  # not in a tmux pane or an iTerm2 tab (e.g. the Codex app-server)
        home = Path(env.get(HOME_VARS[kind]) or DEFAULT_HOMES[kind]).expanduser()
        cwd_out = self.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
        cwd = next((Path(ln[1:]) for ln in cwd_out.splitlines() if ln.startswith("n")), None)
        if cwd is None:
            return None
        try:
            argv = shlex.split(args)
        except ValueError:
            argv = args.split()
        return AgentRecord(kind, home, pid, term_id, cwd, argv)

    # ---- sessions -----------------------------------------------------------------------------

    def claude_session(self, home: Path, cwd: Path) -> str | None:
        if home in self.hook_sessions:
            return self.hook_sessions[home]
        d = transcript_dir(home, cwd)
        files = list(d.glob("*.jsonl")) if d.is_dir() else []
        return max(files, key=lambda p: p.stat().st_mtime).stem if files else None

    def codex_sessions(self, home: Path, cwd: Path) -> list[tuple[float, float, str]]:
        """The user sessions under cwd from the last few days, as (mtime, started, id), newest written first."""
        root = home / "sessions"
        today = dt.date.today()
        found: list[tuple[float, float, str]] = []
        for delta in (-2, -1, 0, 1):
            d = today + dt.timedelta(days=delta)
            day = root / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}"
            if not day.is_dir():
                continue
            for p in day.glob("rollout-*.jsonl"):
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
                mcwd = Path(meta.get("cwd", "/nonexistent"))
                if mcwd != cwd and cwd not in mcwd.parents:
                    continue
                sid = meta.get("id") or meta.get("session_id")
                if sid:
                    mtime = p.stat().st_mtime
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
