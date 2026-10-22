"""Configuration: agents.toml (who runs where) and watcher.toml (policy, timing, modes)."""

from __future__ import annotations

import os
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Kind = Literal["claude", "codex"]
Mode = Literal["off", "dry", "live"]

# "channel" governs posting to Slack; the rest govern keystrokes.
BEHAVIORS = ("channel", "capacity", "approvals_block", "approvals_dialog", "restart", "cohort_restart",
             "cant_continue", "cyber_switch", "usage_check")

# Searches and reads that are never project work. Matched against the command and action summary.
DEFAULT_REFUSE = [
    r"(^|[\s;&|(])(find|fd|du|tree)\s+(\S+\s+)*?(/|~|\$HOME|/Users|/System|/Library|/Volumes|/private|/opt|/usr)/?(\s|$)",
    r"(^|[\s;&|(])(grep|rg|ag)\s+(-\S+\s+)*\S+\s+(/|~|\$HOME|/Users)/?(\s|$)",
    r"(^|[\s;&|(])(mdfind|locate)\s",
    r"(~|\$HOME|/Users/[^/\s]+)/\.(ssh|aws|gnupg|kube|docker/config\.json|netrc)",
    r"Library/(Keychains|Application Support/(Google/Chrome|Firefox|Microsoft Edge|BraveSoftware))",
    r"(^|[\s;&|(])security\s+(find-(generic|internet)-password|dump-keychain)",
    r"(^|[\s;&|(])sudo\s",
    r"/Users/(?!{home_user}\b)[^/\s]+/",  # another user's home directory
]


def _path(p: str | Path) -> Path:
    return Path(p).expanduser()


@dataclass
class AgentConfig:
    name: str  # agent identity; matches its Slack bot name
    kind: Kind
    config_dir: Path  # CLAUDE_CONFIG_DIR or CODEX_HOME
    clone: Path
    launch: str  # shell command typed at the tab's shell; must contain {prompt}
    worktree_roots: list[Path] = field(default_factory=list)
    pre_launch: list[str] = field(default_factory=list)  # shell commands typed before launch
    process_names: list[str] = field(default_factory=list)  # jobName values while the agent runs

    def owns_path(self, p: Path) -> bool:
        """True when p is inside this agent's clone or one of its worktree roots."""
        p = p.expanduser().resolve()
        for root in [self.clone, *self.worktree_roots]:
            root = root.expanduser().resolve()
            if p == root or root in p.parents:
                return True
        return False


@dataclass
class Keys:
    allow: str
    deny: str
    dialog_regex: str  # screen text that proves the permission dialog is showing


@dataclass
class Timing:
    capacity_first_delay_s: float = 30
    capacity_max_tries: int = 5
    idle_wait_max_s: float = 20 * 60  # how long to wait for idle before typing anyway
    exit_timeout_s: float = 90
    online_timeout_s: float = 10 * 60  # how long a relaunched agent has to rejoin its project
    cohort_ready_timeout_s: float = 90 * 60
    cohort_peer_timeout_s: float = 20 * 60  # GM host waits this long for other watchers' reports
    cohort_restart_scheduled: bool = False  # True: the GM-host watcher asks the GM for a cohort restart on schedule
    cohort_restart_every_h: float = 12  # the schedule (also the compaction-count period)
    cohort_restart_anchor: str = "06:00"  # local time the 12h cycle aligns to
    cohort_stagger_s: float = 5  # gap between agent relaunches in a cohort restart
    cant_continue_flags: int = 3  # safety flags without a successful turn
    cant_continue_window_s: float = 45 * 60
    careful_sync_timeout_s: float = 15 * 60
    gm_answer_timeout_s: float = 15 * 60  # then the owner is notified
    owner_typing_grace_s: float = 15  # don't type into a tab the owner typed in this recently
    draft_watch_s: float = 5  # a draft in the prompt box must sit unchanged, untouched, this long before it's moved aside
    submit_delay_s: float = 0.15
    detached_grace_s: float = 2 * 3600  # an agent whose session left its project stays restartable this long
    discovery_sweep_s: float = 30
    screen_poll_s: float = 2  # how often Codex screens are read for capacity errors
    menu_step_s: float = 0.4  # pause between keystrokes in Codex's /model menus
    menu_timeout_s: float = 5  # how long a /model menu has to appear before the switch backs out


@dataclass
class ModelRotation:
    """A Codex /model switch: `model` at the effort after the current one in `efforts` (labels as the menus show
    them), or at the session's current effort when `efforts` is empty. `confirm_key` confirms the effort: "s" =
    this session only; Enter would save it as the home's default in config.toml."""
    model: str
    efforts: list[str]
    confirm_key: str = "s"


@dataclass
class WatcherConfig:
    project_state_dir: Path
    owner_slack_id: str
    authorized_humans: list[str]
    state_dir: Path
    modes: dict[str, Mode]
    timing: Timing
    auto_approve: list[str]
    refuse: list[str]
    owner_only: list[str]
    max_approvals_per_agent_per_hour: int
    keys: dict[Kind, Keys]
    slack_bot_token_env: str
    slack_app_token_env: str
    agents: dict[str, AgentConfig]  # runtime registry of managed agents, keyed by bot name (filled by discovery)
    overrides: dict[str, dict] = field(default_factory=dict)  # agents.toml, keyed by bot name
    slack_env_file: Path | None = None  # slack-mcp-server style env file holding the bot + app tokens
    context_report_pct: list[int] = field(default_factory=list)  # [context] report_pct; empty = don't post
    claude_context_window: int = 1_000_000
    local_kinds: list[str] = field(default_factory=list)  # kinds managed without a Slack project ([local] manage)
    codex_rotation: ModelRotation | None = None  # [codex_capacity]
    codex_cyber: ModelRotation | None = None  # [codex_cyber]: the model a Codex agent moves to after a cyber block
    usage_report_left_pct: list[int] = field(default_factory=lambda: [25, 15, 10, 5])  # [usage] report_left_pct
    usage_intro_wait_s: float = 15 * 60  # post the join line with what's known after this long
    start_script: str | None = None  # [launch] start_script, e.g. "~/.bin/start-{agent}": relaunch through it

    def start_script_for(self, name: str) -> Path | None:
        """The agent's start script, when [launch] start_script is set and the file is there and executable."""
        if not self.start_script:
            return None
        path = _path(self.start_script.replace("{agent}", name))
        return path if path.is_file() and os.access(path, os.X_OK) else None

    def gm_json(self, project: str) -> Path:
        return self.project_state_dir / project / "gm.json"

    def agent_config(self, name: str, kind: Kind, home: Path, clone: Path, launch: str) -> AgentConfig:
        """A runtime AgentConfig for a discovered agent, with agents.toml overrides applied."""
        o = self.overrides.get(name, {})
        if "launch" not in o and (script := self.start_script_for(name)):
            launch = f"{shlex.quote(str(script))} {{prompt}}"  # it sets the home and repo itself
        return AgentConfig(
            name=name, kind=kind, config_dir=home, clone=clone,
            launch=o.get("launch", launch),
            worktree_roots=[_path(p) for p in o.get("worktree_roots", [])],
            pre_launch=list(o.get("pre_launch", [])),
            process_names=list(o.get("process_names", DEFAULT_PROCESS_NAMES[kind])),
        )

    def mode(self, behavior: str) -> Mode:
        return self.modes.get(behavior, "off")

    def agent_for_config_dir(self, config_dir: str | Path) -> AgentConfig | None:
        d = _path(config_dir).resolve()
        for a in self.agents.values():
            if a.config_dir.resolve() == d:
                return a
        return None


DEFAULT_KEYS: dict[Kind, Keys] = {
    # Claude Code permission dialog: "1. Yes" is the first option; Esc declines.
    "claude": Keys(allow="1", deny="\x1b", dialog_regex=r"Do you want to (proceed|make this edit|create|run)"),
    # Codex approval overlay: y approves once, Esc declines.
    "codex": Keys(allow="y", deny="\x1b", dialog_regex=r"(Allow|Approve).*\?"),
}

DEFAULT_PROCESS_NAMES: dict[Kind, list[str]] = {
    "claude": ["claude", "node"],
    "codex": ["codex"],
}


CHANNEL_SUFFIX = "__agent-watcher"


def load_overrides(path: Path | None) -> dict[str, dict]:
    """agents.toml is optional: per-bot overrides for launch, pre_launch, worktree_roots, process_names."""
    if path is None or not path.exists():
        return {}
    raw = tomllib.loads(path.read_text())
    out = {}
    for name, o in raw.get("agents", {}).items():
        if "launch" in o and "{prompt}" not in o["launch"]:
            raise ValueError(f"agent {name}: launch must contain {{prompt}}")
        out[name] = dict(o)
    return out


def load(watcher_path: Path, agents_path: Path | None = None) -> WatcherConfig:
    raw = tomllib.loads(watcher_path.read_text())
    modes = {b: "off" for b in BEHAVIORS}
    for k, v in raw.get("modes", {}).items():
        if k not in BEHAVIORS or v not in ("off", "dry", "live"):
            raise ValueError(f"bad mode {k}={v}")
        modes[k] = v
    keys = dict(DEFAULT_KEYS)
    for kind, k in raw.get("keys", {}).items():
        d = DEFAULT_KEYS[kind]
        keys[kind] = Keys(allow=k.get("allow", d.allow), deny=k.get("deny", d.deny),
                          dialog_regex=k.get("dialog_regex", d.dialog_regex))
    pol = raw.get("policy", {})
    home_user = Path.home().name
    refuse = pol.get("refuse", DEFAULT_REFUSE) + pol.get("refuse_extra", [])
    slack = raw.get("slack", {})
    local_kinds = list(raw.get("local", {}).get("manage", []))
    if bad := [k for k in local_kinds if k not in DEFAULT_KEYS]:
        raise ValueError(f"[local] manage: unknown agent kinds {bad}")

    def rotation(section: str, need_efforts: bool) -> ModelRotation | None:
        rot = raw.get(section)
        if not rot or not rot.get("model"):
            return None
        if need_efforts and not rot.get("efforts"):
            raise ValueError(f"[{section}] needs efforts")
        return ModelRotation(rot["model"], list(rot.get("efforts", [])), rot.get("confirm_key", "s"))

    return WatcherConfig(
        project_state_dir=_path(raw["project_state_dir"]),
        owner_slack_id=raw["owner_slack_id"],
        authorized_humans=list(raw.get("authorized_humans", [])),
        state_dir=_path(raw.get("state_dir", "~/.agent-watcher")),
        modes=modes,
        timing=Timing(**raw.get("timing", {})),
        auto_approve=list(pol.get("auto_approve", [])),
        refuse=[r.replace("{home_user}", home_user) for r in refuse],
        owner_only=list(pol.get("owner_only", [])),
        max_approvals_per_agent_per_hour=int(pol.get("max_approvals_per_agent_per_hour", 10)),
        keys=keys,
        slack_bot_token_env=slack.get("bot_token_env", "AGENT_WATCHER_SLACK_BOT_TOKEN"),
        slack_app_token_env=slack.get("app_token_env", "AGENT_WATCHER_SLACK_APP_TOKEN"),
        slack_env_file=_path(slack["env_file"]) if slack.get("env_file") else None,
        agents={},
        overrides=load_overrides(agents_path),
        context_report_pct=list(raw.get("context", {}).get("report_pct", [])),
        claude_context_window=int(raw.get("context", {}).get("claude_window", 1_000_000)),
        local_kinds=local_kinds,
        codex_rotation=rotation("codex_capacity", need_efforts=True),
        codex_cyber=rotation("codex_cyber", need_efforts=False),
        start_script=raw.get("launch", {}).get("start_script") or None,
        usage_report_left_pct=sorted((int(x) for x in raw.get("usage", {}).get("report_left_pct", [25, 15, 10, 5])),
                                     reverse=True),
    )
