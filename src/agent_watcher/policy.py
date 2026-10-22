"""Code-level approval rules, evaluated before anything goes to the GM.

Outcomes:
  APPROVE - authorize at once and report (git confined to the agent's own clone, except `git push`; never
            git in project-state; `auto_approve` patterns)
  REFUSE  - never authorize; report with the rule (`refuse` patterns). Only the owner can override.
  OWNER   - only the owner may decide (`owner_only` patterns)
  GM      - ask the GM in the agent-watcher channel (everything else)

A gap in the refuse list therefore means "the GM decides", never "waved through".
"""

from __future__ import annotations

import os
import re
import shlex
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .config import AgentConfig, WatcherConfig
from .templates import command_of, describe_action


class Verdict(str, Enum):
    APPROVE = "approve"
    REFUSE = "refuse"
    OWNER = "owner"
    GM = "gm"


@dataclass
class PolicyResult:
    verdict: Verdict
    rule: str  # short label shown in the channel
    notes: list[str] = field(default_factory=list)  # facts for the decider


# `git push` publishes to a shared remote (project-state main, a parent repo's main), where the project's own rules
# (fast-forward only, reviewed merges) apply; it is never auto-approved.
_GIT_PUSH = re.compile(r"\bgit\b[^;&|\n]*\spush\b")
_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\n|\|)\s*")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_READ_ONLY_HELPERS = {"echo", "printf", "true", "pwd", "head", "tail", "cat", "wc", "sort", "uniq", "grep", "sed",
                      "awk"}


def _segments(script: str) -> list[list[str]] | None:
    out = []
    for seg in _SPLIT.split(script.strip()):
        if not seg:
            continue
        try:
            words = shlex.split(seg)
        except ValueError:
            return None
        while words and _ENV_ASSIGN.match(words[0]):
            words = words[1:]
        if words:
            out.append(words)
    return out


# Commands that write to the paths they're given (all non-option arguments, or for copy-like ones the last).
_WRITES_ALL = {"rm", "rmdir", "mkdir", "touch", "chmod", "chown", "tee", "truncate"}
_WRITES_LAST = {"cp", "mv", "ln", "install", "rsync"}
_REDIRECT = re.compile(r"^\d?(>>?)(.*)$")
# Temporary and scratch locations any agent may write to.
_TEMP_ROOTS = [Path("/tmp"), Path("/private/tmp"), Path("/var/folders"), Path("/private/var/folders"), Path("/dev")]


def write_targets(script: str, cwd: Path) -> list[Path]:
    """Paths the shell `script` writes to: redirections, `tee`, `rm`/`mkdir`/`touch`/…, the destination of
    `cp`/`mv`/`ln`, and `sed -i` files. Paths through unexpanded variables are skipped (they can't be judged)."""
    segs = _segments(script) or []
    here, out = cwd, []

    def add(word: str) -> None:
        if not word or "$" in word or word.startswith("&") or "`" in word:
            return
        q = Path(word).expanduser()
        out.append(q if q.is_absolute() else here / q)

    for words in segs:
        cmd = words[0].rsplit("/", 1)[-1]
        if cmd == "cd" and len(words) > 1 and "$" not in words[1]:
            q = Path(words[1]).expanduser()
            here = q if q.is_absolute() else here / q
            continue
        args = []
        i = 1
        while i < len(words):
            w = words[i]
            if m := _REDIRECT.match(w):
                target = m.group(2) or (words[i + 1] if i + 1 < len(words) else "")
                add(target)
                i += 1 if m.group(2) else 2
                continue
            args.append(w)
            i += 1
        plain = [a for a in args if not a.startswith("-")]
        if cmd in _WRITES_ALL:
            for a in plain[1:] if cmd in ("chmod", "chown") else plain:
                add(a)
        elif cmd in _WRITES_LAST and len(plain) >= 2:
            add(plain[-1])
        elif cmd == "sed" and any(a == "-i" or a.startswith("-i") for a in args) and len(plain) >= 2:
            for a in plain[1:]:
                add(a)
    return out


def git_targets(script: str, cwd: Path) -> list[Path] | None:
    """Directories the git commands in `script` operate on, or None if it isn't purely git.

    Allows `cd <dir>` steps and read-only text helpers between git commands.
    """
    segs = _segments(script)
    if not segs:
        return None
    here, targets, saw_git = cwd, [], False
    for words in segs:
        cmd = words[0].rsplit("/", 1)[-1]
        if cmd == "cd":
            if len(words) < 2:
                return None
            p = Path(words[1]).expanduser()
            here = p if p.is_absolute() else here / p
            continue
        if cmd in _READ_ONLY_HELPERS:
            continue
        if cmd != "git":
            return None
        saw_git = True
        target = here
        i = 1
        while i < len(words) and words[i].startswith("-"):
            if words[i] == "-C" and i + 1 < len(words):
                p = Path(words[i + 1]).expanduser()
                target = p if p.is_absolute() else target / p
                i += 2
            elif words[i] in ("--git-dir", "--work-tree") and i + 1 < len(words):
                target = Path(words[i + 1]).expanduser()
                i += 2
            elif words[i] == "-c" and i + 1 < len(words):
                i += 2
            else:
                i += 1
        targets.append(target)
    return targets if saw_git else None


class Policy:
    def __init__(self, cfg: WatcherConfig, now=time.time):
        self.cfg = cfg
        self.now = now
        self._auto = [re.compile(p, re.I) for p in cfg.auto_approve]
        self._refuse = [re.compile(p, re.I) for p in cfg.refuse]
        self._owner = [re.compile(p, re.I) for p in cfg.owner_only]
        self._approvals: dict[str, deque[float]] = defaultdict(deque)

    def _owner_of(self, p: Path) -> AgentConfig | None:
        for a in self.cfg.agents.values():
            if a.owns_path(p):
                return a
        return None

    def _allowed_write(self, agent: AgentConfig, p: Path) -> bool:
        """Inside the agent's clone/worktrees, project-state, its own home, or a temporary location."""
        r = p.expanduser().resolve()
        roots = [self.cfg.project_state_dir, agent.config_dir, *_TEMP_ROOTS]
        if tmp := os.environ.get("TMPDIR"):
            roots.append(Path(tmp))
        return agent.owns_path(r) or any(r == x.resolve() or x.resolve() in r.parents for x in roots)

    def _writes_outside(self, agent: AgentConfig, tool_name: str, tool_input: dict, cmd: str, cwd: str) -> str:
        """The first written path outside the project's folders, or ""."""
        base = Path(tool_input.get("cwd") or cwd or agent.clone).expanduser()
        if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            path = tool_input.get("file_path") or tool_input.get("notebook_path")
            targets = [Path(path).expanduser()] if isinstance(path, str) and path else []
        else:
            targets = write_targets(cmd, base) if cmd else []
        return next((str(t) for t in targets if not self._allowed_write(agent, t)), "")

    def evaluate(self, agent: AgentConfig, tool_name: str, tool_input: dict, cwd: str) -> PolicyResult:
        cmd = command_of(tool_input) or ""
        blob = f"{tool_name} {cmd} {describe_action(tool_name, tool_input)}"
        for rx in self._owner:
            if rx.search(blob):
                return PolicyResult(Verdict.OWNER, f"owner-only: {rx.pattern}")
        for rx in self._refuse:
            if rx.search(blob):
                return PolicyResult(Verdict.REFUSE, f"refuse: {rx.pattern}")
        if outside := self._writes_outside(agent, tool_name, tool_input, cmd, cwd):
            return PolicyResult(Verdict.REFUSE, f"refuse: writes outside the project's folders ({outside})")

        auto: PolicyResult | None = None
        notes: list[str] = []
        base = Path(tool_input.get("cwd") or cwd or agent.clone).expanduser()
        targets = git_targets(cmd, base) if cmd else None
        if targets is not None:
            shared = self.cfg.project_state_dir.resolve()
            foreign, unknown, in_shared = [], [], False
            for t in targets:
                r = t.resolve()
                if r == shared or shared in r.parents:
                    in_shared = True  # shared, with its own publishing rules (e.g. fast-forward only)
                    continue
                if agent.owns_path(r):
                    continue
                other = self._owner_of(r)
                (foreign if other else unknown).append(f"{r} ({other.name})" if other else str(r))
            if _GIT_PUSH.search(cmd):
                notes.append("git push publishes to a shared remote; the project's push rules apply")
            elif in_shared:
                notes.append("git in project-state, which is shared; its own rules (e.g. fast-forward only) apply")
            elif not foreign and not unknown:
                auto = PolicyResult(Verdict.APPROVE, "git in own clone")
            elif foreign:
                notes.append(f"git in another agent's clone: {', '.join(foreign)}")
            else:
                notes.append(f"git outside any registered clone: {', '.join(unknown)}")
        if auto is None:
            for rx in self._auto:
                if rx.search(blob):
                    auto = PolicyResult(Verdict.APPROVE, f"auto-approve: {rx.pattern}")
                    break
        if auto is not None:
            if self.rate_capped(agent.name):
                return PolicyResult(Verdict.GM, "auto-approve rate cap reached", [auto.rule])
            return auto
        return PolicyResult(Verdict.GM, "needs a decision", notes)

    def rate_capped(self, agent: str) -> bool:
        q = self._approvals[agent]
        cutoff = self.now() - 3600
        while q and q[0] < cutoff:
            q.popleft()
        return len(q) >= self.cfg.max_approvals_per_agent_per_hour

    def record_approval(self, agent: str) -> None:
        self._approvals[agent].append(self.now())
