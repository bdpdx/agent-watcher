from __future__ import annotations

from pathlib import Path

import pytest

from agent_watcher.config import DEFAULT_KEYS, DEFAULT_REFUSE, Timing, WatcherConfig
from agent_watcher.discovery import AgentRecord, Registration

P1 = "2026-10-06-ui-fixes"
P2 = "2026-10-08-ui-matrix"


def make_cfg(tmp: Path, mode: str = "live") -> WatcherConfig:
    (tmp / "code" / "project-state").mkdir(parents=True, exist_ok=True)
    timing = Timing(capacity_first_delay_s=0.05, capacity_max_tries=2, idle_wait_max_s=1, exit_timeout_s=1,
                    online_timeout_s=0.5, cohort_ready_timeout_s=2, cohort_peer_timeout_s=1,
                    careful_sync_timeout_s=0.3, owner_typing_grace_s=0.05, submit_delay_s=0, cohort_stagger_s=0.01,
                    gm_answer_timeout_s=60, detached_grace_s=60)
    return WatcherConfig(
        project_state_dir=tmp / "code" / "project-state",
        owner_slack_id="UOWNER", authorized_humans=["UDANA"], state_dir=tmp / "state",
        modes={b: mode for b in ("channel", "capacity", "approvals_block", "approvals_dialog", "restart",
                                 "cohort_restart", "cant_continue", "cyber_switch")},
        timing=timing, auto_approve=[r"^Bash .*\bnpm (test|run lint)\b"],
        refuse=[r.replace("{home_user}", "alex") for r in DEFAULT_REFUSE], owner_only=["reset-prod"],
        max_approvals_per_agent_per_hour=3, keys=dict(DEFAULT_KEYS),
        slack_bot_token_env="X", slack_app_token_env="Y", agents={},
        overrides={"claude-a": {"pre_launch": ["clear"]}},
        context_report_pct=[50, 80], claude_context_window=1_000_000,
    )


def record(tmp: Path, name: str, project: str | None, session: str = "s1", kind: str | None = None,
           tab: str | None = None, home: Path | None = None) -> AgentRecord:
    kind = kind or ("claude" if name.startswith("claude") else "codex")
    clone = tmp / "code" / name
    clone.mkdir(parents=True, exist_ok=True)
    home = home or tmp / f"home-{name}"
    argv = ["claude"] if kind == "claude" else ["codex", "--remote", "unix://", "-C", str(clone)]
    regs = [Registration(project, name, session)] if project else []
    return AgentRecord(kind, home, 100, tab or f"TAB-{name}", clone, argv, session, regs)


class FakeTerminal:
    def __init__(self):
        self.sent: dict[str, list[str]] = {}
        self.jobs: dict[str, str] = {}
        self.screens: dict[str, str] = {}
        self.unbound: set[str] = set()

    def bound(self, agent):
        return agent not in self.unbound

    async def send_raw(self, agent, data):
        self.sent.setdefault(agent, []).append(data)
        if data == "/exit":
            self.jobs[agent] = "zsh"
        if "claude " in data or "codex " in data:
            self.jobs[agent] = "claude" if "claude " in data else "codex"

    async def job_name(self, agent):
        return self.jobs.get(agent, "claude" if agent.startswith("claude") else "codex")

    async def screen_text(self, agent):
        return self.screens.get(agent, "")

    def lines(self, agent):
        """Submitted lines: text followed by a separate \\r."""
        out, s = [], self.sent.get(agent, [])
        for i, x in enumerate(s):
            if x == "\r" and i > 0:
                out.append(s[i - 1])
        return out


class FakeChannel:
    def __init__(self, projects=(P1,), name="agent-watcher-a"):
        self.name = name
        self.user_id = "UWATCHER"
        self._projects = set(projects)
        self.posts: list[tuple[str, str, str | None]] = []
        self.invites: list[tuple[str, str]] = []
        self._ts = 0

    def projects(self):
        return set(self._projects)

    def ready(self, project):
        return project in self._projects

    async def post(self, project, text, thread_ts=None):
        self._ts += 1
        self.posts.append((project, text, thread_ts))
        return f"ts{self._ts}"

    async def invite(self, project, agent_name):
        self.invites.append((project, agent_name))
        return True

    def texts(self, project=None):
        return [t for p, t, _ in self.posts if project is None or p == project]


class FakeNotifier:
    def __init__(self):
        self.notes: list[str] = []

    async def notify(self, title, message):
        self.notes.append(message)


@pytest.fixture
def cfg(tmp_path):
    """A config with three agents registered as discovery would (for policy and tailing tests)."""
    c = make_cfg(tmp_path)
    for name in ("claude-a", "codex-a", "codex-g"):
        r = record(tmp_path, name, P1)
        c.agents[name] = c.agent_config(name, r.kind, r.home, r.cwd, r.launch_command())
    return c
