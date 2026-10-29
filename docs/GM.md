# The agent-watcher channel: guide for the GM

Each developer machine in the project runs an agent-watcher bot (e.g. `agent-watcher-a`). It watches that
machine's agents through their hooks and session logs, reports to `#<project>__agent-watcher`, and acts on
your commands by typing into the agents' terminals. It is a process helper: it doesn't do project work and
never relays free text to agents. You decide; it carries out the decision.

Only the GM, the project's users and the watcher bots belong in this channel. Cohort agents don't.

A watcher watches an agent while the agent's current session has joined this project. When an agent stops its
Slack watcher (e.g. before a cohort restart), the watcher keeps it as **detached**: no more reports, but you can
still restart or move it. Watchers say `Now watching <agent>` and `No longer watching <agent> (why)` here.

## What watchers post

- **Blocked actions**: `[agent-watcher-a/B-0142] codex-a blocked by Codex auto-review: the command
  `./sn reset dev12 --wipe`. Reason: … Reply approve or deny in this thread.`
  These need your decision.
- Auto-approved actions (git confined to the agent's own clone, except `git push`, plus configured patterns; git in project-state always comes to you) are
  not posted; the watcher's audit log records them.
- **Refused actions**: matched a refuse rule (broad searches outside the workspace, credential stores, sudo,
  other users' files…). Only the owner can override these. **Owner-only** items (e.g. prod resets) also go
  to the owner.
- Your own blocked actions and permission dialogs are decided by the second opinion (below), not by you. When it
  says "Looks project-related", the watcher approves and posts that; otherwise (or with no answer) it posts the
  request as owner-only for the owner. Ones refused or owner-only by rule go straight to the owner. Without
  `gm_assess` on, they aren't posted: they reach the owner through your own Slack approvals.
- **Capacity / disconnect retries**, **safety refusals**, **session refusals and recovery relaunches**. On a Codex
  cybersecurity block the watcher switches the agent to the cybersecurity model, asks it to continue, and switches
  it back after its next successful turn; it posts each step. If the agent is blocked again before a successful turn,
  it's yours to handle.
- **Restart progress**, a hello line when a watcher comes online (`agent-watcher-a online: watching …`),
  and `cohort done: …` reports.

## Deciding a blocked action

Each request also gets a one-line **second opinion** in its thread from a small model ("Looks project-related",
"Outside the project" or "Unclear", and why). For other agents' requests it's advisory and can be wrong: you
decide.
Writes outside the project's folders (the agent's clone and worktrees, project-state, its own home, temporary
folders) are refused by rule, like credential stores and broad searches; only the owner can override those.

Reply in the event's thread with `approve` or `deny` (a short note may follow), or post
`approve agent-watcher-a/B-0142` / `deny agent-watcher-a/B-0142` at top level. Each watcher numbers its own
requests, so once more than one watcher has posted in this channel a bare `approve B-0142` is refused as
ambiguous; include the watcher's name or reply in the thread.

On approve, the watcher submits right away, even mid-turn: "You are authorized to perform <the blocked
action>. If the action still needs to be performed retry it." The agent reads it at its next tool call or when its
turn ends; nothing interrupts the turn. If someone is typing in the agent's prompt box it waits for them to stop,
and a draft left there is set aside and put back unsent. If the prompt box isn't on screen, it types once the agent
is idle. On deny, nothing is typed and the block stands. For a permission dialog, it presses Allow or Deny in the
terminal.

What to approve: work that's germane to the project. All systems the cohort works on are disposable
development infrastructure with test data only. Destructive operations on the ServiceNow dev instances
(wipes, credential resets, reinstalls) are normal project work. Git in another agent's clone is appropriate
only when that agent was tasked with cleaning up bad state there. Deny probing outside the workspace and
anything that sends project data to destinations the project doesn't own.

A request belongs to the agent session that was blocked. When that agent restarts or its session ends, the
watcher closes the request in its thread and never types the decision into the new session; if the new session
needs the action, it gets blocked again and posts a fresh request.

Unanswered items notify the owner after 15 minutes.

## Commands (top-level messages)

| Command | Effect |
|---|---|
| `restart <agent>` | Exit the agent and relaunch it with "join project <project>". Post this only after the agent has synced and reported `SYNCED:`. |
| `move <agent> <project>` | Like `restart`, but relaunches the agent into another project ("join project <project>"). That project must have its own `#<project>__agent-watcher` with this watcher in it, and the agent's bot must already be invited to the new project's channels. Coordinate with that project's GM first. |
| `restart cohort` | Start a cohort restart. If the owner posts it, the GM's watcher types "Tell the cohort to … sync, cleanup, and prepare for restart" into your terminal. If you post it, coordinate the sync yourself. |
| `cohort ready` | After every agent has reported `SYNCED:`. Each watcher restarts its machine's agents and posts `cohort done: …`. The watcher hosting you restarts you last, after the other watchers report. |
| `grant <agent> authorization for <task>` | Type an authorization for `<task>` into the agent before it gets blocked, e.g. `grant codex-g1 authorization for the dev100001 Wipe Data run`. The watcher submits it right away (the agent reads it at its next tool call or when its turn ends): "<you>, the project's GM, has authorized this task for you: <task>. This covers only that task…". Name the task specifically; it applies to the agent's current session only. You can't grant yourself. |
| `continue <agent>` | Type `continue` into the agent's terminal. |
| `pause <agent>` / `resume <agent>` | Stop or resume watcher actions for one agent. `pause all` / `resume all` pauses or resumes one watcher entirely. |
| `gm <agent>` | `<agent>` is now the project's GM. Post it when you become GM (`gm <your name>`), or when you hand over (`gm <successor>`); the owner can post it too. Each watcher confirms with `The GM is now <agent>.` and invites that agent to this channel. |
| `status` / `status <agent>` | Each watcher replies with its agents' state, context use, compaction count, and pending items. |

Commands only reach agents in this channel's project, and are handled by the watcher that runs the agent; the
others ignore them. Anything that
isn't a command is ignored, unless it @mentions a watcher, which then replies with the command list.

## Cohort restarts on a schedule

The watcher on the machine hosting the GM starts a cohort restart every 12 hours by typing the sync request
into your terminal: "Tell the cohort to finish work in progress, then sync, cleanup, and prepare for restart."
The timing is yours: let agents finish what they're in the middle of, coordinate the sync as usual, then post
`cohort ready`. Nothing restarts until you do.

## When the GM changes

Watchers follow the latest of two signals: a `gm <agent>` post in this channel, and a change to the project's
`gm.json`. A post counts when it comes from the owner, the current GM, or the named agent itself (a cohort agent
announcing that it is now GM; it must already be in this channel to post). On a change, each watcher invites the new
GM to this channel and posts `The GM is now <agent>.` with the items still pending.

Handing over (a standing rule):
1. The outgoing GM invites the incoming GM to this channel and tells it to join and watch it.
2. The incoming GM posts `gm <its own name>` at top level, exactly that and nothing else.
3. Once a watcher replies `The GM is now <agent>.`, the outgoing GM leaves the channel.
