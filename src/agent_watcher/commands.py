"""Parsing of messages in the agent-watcher channel into a fixed command set.

Commands (top level, case-insensitive; a leading @mention is allowed):
  restart <agent>            restart one agent (the GM has already had it sync)
  move <agent> <project>     restart the agent into another watched project (after it has synced)
  restart cohort             start a cohort restart (the GM-host watcher asks the GM to sync the cohort)
  cohort ready               the GM confirms the cohort has synced; every watcher restarts its agents
  continue <agent>           type "continue" into the agent's terminal
  pause <agent|all>          stop acting for an agent (or this whole watcher)
  resume <agent|all>
  status [agent]
  approve <event-id> [note]  decide a pending approval by id
  gm <agent>                 <agent> is now the project's GM (the owner, the current GM, or <agent> itself)
  grant <agent> <task>       type an authorization for <task> into <agent> ("grant codex-g1 authorization for ...")
  deny <event-id> [note]
In a thread under a watcher's event: `approve` or `deny` (an optional note may follow).

Anything else is not a command. Command text is never typed into a terminal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MENTION = re.compile(r"<@([A-Z0-9]+)(\|[^>]*)?>")
CHANNEL_LINK = re.compile(r"<#[A-Z0-9]+\|([^>]*)>")  # Slack's form of a typed #channel
URL_LINK = re.compile(r"<((?:https?|mailto):[^|>]+)(?:\|[^>]*)?>")  # Slack's form of a pasted link
EVENT_ID = re.compile(r"^(?:(?P<bot>[a-z0-9][a-z0-9-]*)/)?(?P<id>[A-Z]-\d{1,6})$", re.I)
APPROVE_WORDS = {"approve", "approved", "allow", "yes", "y", "ok", "authorize", "authorized"}
DENY_WORDS = {"deny", "denied", "no", "n", "reject", "decline"}

VERBS = ("restart", "move", "cohort", "continue", "pause", "resume", "status", "approve", "deny", "authorize", "gm",
         "grant")


@dataclass
class Command:
    verb: str  # approve | deny | restart | move | restart_cohort | cohort_ready | continue | pause | resume | status | gm | grant | unknown
    agent: str | None = None  # agent name, or "all" for pause/resume
    event_id: str | None = None  # "B-0142"
    event_bot: str | None = None  # "agent-watcher-a" when the id was qualified
    note: str = ""
    target: str | None = None  # move: the destination project
    mentioned: bool = False  # this watcher's bot was @mentioned


def _clean(text: str) -> tuple[str, set[str]]:
    mentioned = {m.group(1) for m in MENTION.finditer(text)}
    text = MENTION.sub(" ", text)
    text = CHANNEL_LINK.sub(lambda m: m.group(1), text)
    text = URL_LINK.sub(lambda m: m.group(1), text)
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")  # Slack's escapes, after the links
    text = text.replace("`", " ").replace("*", " ")
    return re.sub(r"\s+", " ", text).strip(), mentioned


def _event(word: str) -> tuple[str | None, str | None]:
    m = EVENT_ID.match(word.strip("[]().,:"))
    if not m:
        return None, None
    return (m.group("bot").lower() if m.group("bot") else None), m.group("id").upper()


def parse(text: str, self_user_id: str = "", in_thread: bool = False) -> Command | None:
    body, mentioned = _clean(text)
    is_mentioned = bool(self_user_id) and self_user_id in mentioned
    words = body.split(" ") if body else []
    if not words:
        return None
    w0 = words[0].lower().rstrip(".,:!")
    rest = words[1:]

    if in_thread and (w0 in APPROVE_WORDS or w0 in DENY_WORDS):
        # "approve" alone, or "approve <note>"; an explicit id in the thread is also accepted.
        bot, eid = _event(rest[0]) if rest else (None, None)
        note_words = rest[1:] if eid else rest
        return Command("approve" if w0 in APPROVE_WORDS else "deny", event_id=eid, event_bot=bot,
                       note=" ".join(note_words), mentioned=is_mentioned)

    if w0 in ("approve", "authorize", "deny"):
        # approve <id> [note]  |  authorize <agent> <id> [note]
        args = rest
        agent = None
        if w0 == "authorize" and len(args) >= 2 and _event(args[0]) == (None, None):
            agent, args = args[0].lower(), args[1:]
        bot, eid = _event(args[0]) if args else (None, None)
        if eid:
            return Command("deny" if w0 == "deny" else "approve", agent=agent, event_id=eid, event_bot=bot,
                           note=" ".join(args[1:]), mentioned=is_mentioned)
    elif w0 == "restart" and len(rest) >= 1:
        target = rest[0].lower().rstrip(".,!")
        if target in ("cohort", "all"):
            return Command("restart_cohort", mentioned=is_mentioned)
        return Command("restart", agent=target, note=" ".join(rest[1:]), mentioned=is_mentioned)
    elif w0 == "move" and len(rest) >= 2:
        dest = rest[1]
        if dest.lower() == "to" and len(rest) >= 3:
            dest = rest[2]
        return Command("move", agent=rest[0].lower().rstrip(".,!"), target=dest.lstrip("#").rstrip(".,!"),
                       mentioned=is_mentioned)
    elif w0 == "cohort" and rest and rest[0].lower().rstrip(".,!") == "ready":
        return Command("cohort_ready", note=" ".join(rest[1:]), mentioned=is_mentioned)
    elif w0 in ("continue", "pause", "resume") and len(rest) >= 1:
        return Command(w0, agent=rest[0].lower().rstrip(".,!"), mentioned=is_mentioned)
    elif w0 == "grant" and len(rest) >= 2:
        task = rest[1:]
        if task and task[0].lower().rstrip(":") in ("authorization", "authorisation", "permission"):
            task = task[1:]
        if task and task[0].lower() in ("for", "to"):
            task = task[1:]
        if task:
            return Command("grant", agent=rest[0].lower().rstrip(".,:!"), note=" ".join(task), mentioned=is_mentioned)
    elif w0 == "gm" and len(rest) == 1:
        return Command("gm", agent=rest[0].lower().rstrip(".,!"), mentioned=is_mentioned)
    elif w0 == "status":
        return Command("status", agent=rest[0].lower().rstrip(".,!") if rest else None, mentioned=is_mentioned)

    if is_mentioned:
        return Command("unknown", note=body, mentioned=True)
    return None


HELP = ("Commands: `restart <agent>`, `move <agent> <project>`, `restart cohort`, `cohort ready`, `continue <agent>`, "
        "`pause <agent|all>`, `resume <agent|all>`, `status [agent]`, `approve <id>`, `deny <id>`, `gm <agent>`, "
        "`grant <agent> <task>`; "
        "or reply `approve` / `deny` in an event's thread.")
