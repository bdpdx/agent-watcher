# agent-watcher

agent-watcher is a process helper for CLI coding agents (Claude Code and Codex) running in iTerm2 tabs. It
learns what each agent is doing from its **hooks and session logs**, reports to one Slack channel per project,
`#<project>__agent-watcher`, and carries out decisions from that project's **GM** (and the owner) by typing a
small fixed set of strings into the agents' terminals. It is not a project worker. No Slack text and no
model output is ever typed into a terminal.

Each developer machine runs its own watcher and bot (e.g. `agent-watcher-a`); all of them share each project's
channel. The GM's side is documented in [docs/GM.md](docs/GM.md).

## Discovery (no agent list, no project setting)

The watcher follows a **1:1:1 rule: one agent home ↔ one bot ↔ one project** at a time.

- **Projects** are the `*__agent-watcher` channels this watcher's bot belongs to. Invite the bot to a
  project's channel and the watcher serves it; remove it and it stops.
- **Agents** are found by scanning running `claude` / `codex` processes that have a terminal in an iTerm2 tab.
  Each process's environment gives its home (`CLAUDE_CONFIG_DIR` / `CODEX_HOME`, default `~/.claude` /
  `~/.codex`) and tab (`ITERM_SESSION_ID`); its working directory is its clone.
- An agent is **managed exactly while its current session is registered in a watched project**, per its
  Slack listener's state (`<home>/slack-agent-chat/state.json`), i.e. after `chat project join`. Before
  that, nothing is reported. If the registration disappears but the same session keeps running (agents stop
  their Slack watcher before a cohort restart), the agent is **detached**: reports stop, but it can still be
  restarted or moved, for up to `detached_grace_s`.
- Scans run on iTerm2 tab changes, on hook events from unknown sessions, on listener state changes, and every
  `discovery_sweep_s`.
- 1:1:1 violations (two sessions on one home, one session in two projects) are reported and left alone.
- Relaunch commands are rebuilt from the running process. `agents.toml` is optional per-agent overrides
  (`launch`, `pre_launch`, `worktree_roots`).

## Local mode (no Slack)

`[local] manage = ["codex"]` manages every running agent of those kinds even when it hasn't joined a watched
project. Each one is named after its home (`~/.codex-g` → `codex-g`), and its reports go to `audit.log`.

Codex records a turn's fatal error in the rollout's `task_complete` (`error.codex_error_info`:
`server_overloaded`, `cyber_policy`, …). The watcher reads it there, and also reads each Codex screen every
`screen_poll_s` as a fallback, counting an error only when it is the last thing on screen and Codex is idle. With `[codex_capacity]` set, the retry first moves the session to the
next effort of `model` (round-robin) through `/model`, then types `continue`. Each menu step is checked on
screen; if anything is unexpected it backs out, types nothing, and notifies the owner.

On a cybersecurity block (`cyber_policy`), with `[codex_cyber]` set and `cyber_switch` live, it switches the
session to that model (e.g. Daybreak Blue) at the session's current effort and asks it to continue in plain
development terms, whether or not the agent is in a project. After the next successful turn it switches back to the
model the session had (from the status line; else `[codex_capacity]` model). A second block before a successful
turn gets no second switch: in a project the GM sees the refusals in the channel (and `cant_continue` applies),
otherwise the owner is notified.

## Usage limits

The watcher tracks each agent's plan limits without asking the agent:

- **Codex:** the weekly window comes from the rollout's `token_count` events (`rate_limits.primary`).
- **Claude:** the session (5-hour) and weekly windows come from the status line JSON (`rate_limits`). Run
  `agent-watcher install-statusline <home>` once per Claude home. It backs up `settings.json` and wraps any
  existing status line command, which still prints as before.
- **Claude per-model weeks (Fable):** these are only on the `/usage` screen. With `usage_check` live, the
  watcher types `/usage` into an idle Claude, reads it, and closes it with Esc. It does this daily below 50%
  used, then every 6h, 2h (80%), 1h (90%) and 30m (95%).

It posts one usage line per agent when the agent joins (and in its `online` line), then stays quiet until a
window has 25% or less left. After that it posts once at each of 25/15/10/5% left, and once when a reported
window resets.

## What it does

| Behavior (`[modes]`) | Trigger | Action |
|---|---|---|
| `approvals_block` | Claude `PermissionDenied` hook; Codex auto-review denial (guardian rollout) | Policy lists first: own-clone git (except `git push`; git in project-state always goes to the GM) and `auto_approve` → authorize now; `refuse` / `owner_only` → owner decides; everything else → posted for the GM. On approve, it submits right away (mid-turn if needed; a draft in the prompt box is set aside and restored) "You are authorized to perform … If the action still needs to be performed retry it." |
| `approvals_dialog` | Claude `PermissionRequest` (a real dialog) | Same decision; presses Allow or Deny, only when the dialog is visibly on screen |
| `capacity` | `StopFailure` (overload, 5xx, disconnect, plan limit); Codex rollout errors; Codex capacity errors on screen | Posts it, waits (30s, backing off, or until the limit resets), switches the Codex model if `[codex_capacity]` is set, types `continue` |
| `restart` | `restart <agent>` or `move <agent> <project>` from the GM/owner | `/stop` (Codex, background terminals) and `/quit` (Claude: "Exit and stop tasks") → pre-launch commands → relaunch through the start script with "join project <project>"; done when the new session registers there |
| `cohort_restart` | every 12h (GM-host watcher only), `restart cohort`, `cohort ready` | GM-host types the cohort sync request into the GM's terminal; on `cohort ready` every watcher restarts its agents; the GM restarts last |
| `cant_continue` | Claude session-level refusal, or repeated safety refusals with no good turn | Careful-sync request (repeated case), then exit + relaunch with a recovery prompt |
| `channel` | everything above | Posts reports and restart progress (compactions are only tracked, in `compactions.jsonl`) |

Every keystroke behavior starts in `dry` mode ("would do X" in `~/.agent-watcher/audit.log`). Switch them to
`live` one at a time. `channel = "live"` with keystrokes `dry` lets the GM see reports without anything being
typed; acknowledgements say "dry mode: nothing typed".

## Authority

A watcher accepts commands and decisions from its owner (`owner_slack_id`), the humans that owner listed in
`authorized_humans`, and each project's current GM (the latest `gm <agent>` post in its channel, else project-state `<project>/gm.json`). A GM can only
command agents in its own project; the owner and authorized humans can command any of this watcher's agents.
Thread replies count only under the watcher's own posts. Refused and owner-only items need the owner.

## Setup

1. **Install.** `python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'`
2. **Configure.** Copy `watcher.toml.example` to `watcher.toml` and fill in the owner's Slack id and authorized
   humans. `agents.toml` (overrides) is optional.
3. **Slack.** One Slack app per machine with Socket Mode on. Bot scopes: `channels:history`,
   `groups:history`, `channels:read`, `groups:read`, `users:read`, `chat:write`, `groups:write`. Events:
   `message.channels`, `message.groups`, `member_joined_channel`. Tokens come from `[slack] env_file`
   (`SLACK_MCP_XOXB_TOKEN`, `SLACK_MCP_XAPP_TOKEN`) or the `AGENT_WATCHER_SLACK_*` variables; a user token
   in the env file is never used. Don't run a slack-mcp-server listener on the same app. Each project's GM
   creates `#<project>__agent-watcher` and invites the watcher bot. Also subscribe to `member_left_channel`
   and `group_left` so the watcher notices when it's removed.
4. **iTerm2.** Run the daemon in its own tab: `.venv/bin/agent-watcher run`. Choose "Always allow" when
   iTerm2 asks.
5. **Claude hooks.** Add the bridge next to the existing slack-mcp-server hooks in each agent's
   `settings.json`, under `PermissionDenied`, `PermissionRequest`, `StopFailure`, `Stop`, `UserPromptSubmit`,
   `Notification`, `SessionStart`, `SessionEnd`:
   ```json
   {"hooks": [{"type": "command", "command": "<repo>/.venv/bin/agent-watcher hook --kind claude", "timeout": 5}]}
   ```
   It exits 0 at once, prints nothing and decides nothing. Codex needs no hooks: its errors, denials,
   context size and compactions come from its rollout files.
6. **Local control.** `agent-watcher status`, `restart <agent>|cohort:<project>`, `pause [agent]`,
   `resume [agent]`.

## Before going live

- [ ] `agent-watcher status` lists each project with its agents, every agent `bound: true`, and the hello line
      appears in each channel.
- [ ] A watcher-typed `continue` submits in both Claude and Codex.
- [ ] A watcher-typed AUTH line clears a Claude classifier block and a Codex auto-review denial.
- [ ] `[keys.*]` Allow/Deny keys and `dialog_regex` match the real dialogs.
- [ ] `CLAUDE_CODE_RETRY_WATCHDOG=1` is in the Claude launch commands.
- [ ] A day in dry mode, reviewing `audit.log` and the channel.

## Layout

```
src/agent_watcher/
  cli.py         run / hook / local control
  daemon.py      wires hooks socket, session logs, channel, iTerm2 to the supervisor
  discovery.py   running agents → home, tab, clone, session, registration; relaunch commands
  supervisor.py  managed agents; approvals; restarts and moves; per-project cohorts; reports
  channel.py     the per-project Slack channels: discovery by name, posting, inviting, incoming messages
  commands.py    the fixed command grammar
  policy.py      auto_approve / refuse / owner_only lists and git containment
  actuator.py    the only path to keystrokes; template ids only; audit log; dry/live
  templates.py   the closed set of typed strings
  detect.py      hook payload / rollout / transcript / error-text classification
  tails.py       Codex rollouts (main + auto-review threads) and Claude transcripts
  iterm.py       iTerm2 adapter (binding, screen, job name, keystroke monitor)
docs/GM.md       the GM's guide to the channel and commands
```

Tests: `.venv/bin/pytest`.
