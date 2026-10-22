"""Code-level approval rules, evaluated before anything goes to the GM.

Outcomes:
  APPROVE - authorize at once and report (git confined to the agent's own clone, `auto_approve` patterns)
  REFUSE  - never authorize; report with the rule (`refuse` patterns). Only the owner can override.
  OWNER   - only the owner may decide (`owner_only` patterns)
  GM      - ask the GM in the agent-watcher channel (everything else)

A gap in the refuse list therefore means "the GM decides", never "waved through".
"""

from __future__ import annotations

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

    def evaluate(self, agent: AgentConfig, tool_name: str, tool_input: dict, cwd: str) -> PolicyResult:
        cmd = command_of(tool_input) or ""
        blob = f"{tool_name} {cmd} {describe_action(tool_name, tool_input)}"
        for rx in self._owner:
            if rx.search(blob):
                return PolicyResult(Verdict.OWNER, f"owner-only: {rx.pattern}")
        for rx in self._refuse:
            if rx.search(blob):
                return PolicyResult(Verdict.REFUSE, f"refuse: {rx.pattern}")

        auto: PolicyResult | None = None
        notes: list[str] = []
        base = Path(tool_input.get("cwd") or cwd or agent.clone).expanduser()
        targets = git_targets(cmd, base) if cmd else None
        if targets is not None:
            shared = self.cfg.project_state_dir.resolve()
            foreign, unknown = [], []
            for t in targets:
                r = t.resolve()
                if agent.owns_path(r) or r == shared or shared in r.parents:
                    continue
                other = self._owner_of(r)
                (foreign if other else unknown).append(f"{r} ({other.name})" if other else str(r))
            if not foreign and not unknown:
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
