"""Pure functions that turn raw signals (hook payloads, rollout lines, text) into events.

Everything here is deterministic and unit-tested against real payload shapes.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from .events import E, Event
from .usage import codex_rate_limits

# Claude: "API Error: <model>'s safeguards flagged this session" / "...can't help with this. Start a new session"
SESSION_REFUSAL = re.compile(r"flagged this session|start a new session to continue", re.I)
SAFETY = re.compile(r"safeguards flagged|flagged this message|usage polic|cyber(security)? (review|safeguard|policy)|"
                    r"blocked by (the )?(cyber|safety)", re.I)
CAPACITY = re.compile(
    r"overloaded|at capacity|\b529\b|high load|\b50[0234]\b|internal server error|server[_ ]error|"
    r"connection (lost|dropped|error|reset)|ECONNRESET|unable to connect|stream disconnected|"
    r"went to sleep mid-response|no (usable|safety) verdict|auto mode is unavailable|timed? ?out",
    re.I,
)
RATE_LIMIT = re.compile(r"hit your .*limit|rate.?limit|usage limit|\b429\b", re.I)
RESET_AT = re.compile(r"(?:resets|try again at|reset at|available again at)\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m", re.I)

CLAUDE_ERROR_CAPACITY = {"overloaded", "server_error", "unknown"}
CODEX_ERROR_CAPACITY = {"server_overloaded", "internal_server_error", "response_stream_disconnected",
                        "response_stream_connection_failed", "http_connection_failed", "other"}
# A turn's fatal error is recorded in its task_complete. "other" there covers non-capacity errors too, so its text
# decides.
CODEX_TURN_ERROR_CAPACITY = CODEX_ERROR_CAPACITY - {"other"}
CODEX_SAFETY_TYPES = {"cyber_policy", "bio_policy", "misalignment_policy_violation"}


def iso_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def reset_time(text: str, now: dt.datetime | None = None) -> float | None:
    """Epoch seconds of the next "resets 3:45pm" in local time, or None."""
    m = RESET_AT.search(text)
    if not m:
        return None
    now = now or dt.datetime.now().astimezone()
    hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
    at = now.replace(hour=hour, minute=int(m.group(2) or 0), second=0, microsecond=0)
    if at <= now:
        at += dt.timedelta(days=1)
    return at.timestamp()


def classify_error_text(text: str, error_type: str | None, agent: str | None, source: str,
                        capacity_types: set[str] = CLAUDE_ERROR_CAPACITY) -> Event | None:
    """Map an error that ended a turn to an event, most specific first."""
    if SESSION_REFUSAL.search(text):
        return Event(E.SESSION_REFUSAL, agent, {"text": text}, source=source)
    if SAFETY.search(text) or error_type in ("refusal", "safety") or error_type in CODEX_SAFETY_TYPES:
        return Event(E.SAFETY_FLAG, agent, {"text": text}, source=source)
    if error_type == "rate_limit" or error_type == "usage_limit_exceeded" or RATE_LIMIT.search(text):
        return Event(E.RATE_LIMIT, agent, {"text": text, "resume_at": reset_time(text)}, source=source)
    if (error_type in capacity_types) or CAPACITY.search(text):
        return Event(E.CAPACITY, agent, {"text": text, "error": error_type}, source=source)
    return None


def from_hook(event_name: str, payload: dict[str, Any], agent: str | None, kind: str) -> list[Event]:
    """Events from a Claude Code or Codex hook payload."""
    src = f"hook:{event_name}"
    match event_name:
        case "Stop":
            text = payload.get("last_assistant_message") or ""
            if isinstance(text, str) and SESSION_REFUSAL.search(text):
                return [Event(E.SESSION_REFUSAL, agent, {"text": text}, source=src)]
            return [Event(E.IDLE, agent, {"text": text if isinstance(text, str) else ""}, source=src)]
        case "StopFailure":
            text = " ".join(str(payload.get(k) or "") for k in ("error_details", "last_assistant_message"))
            ev = classify_error_text(text, payload.get("error"), agent, src)
            return [ev] if ev else [Event(E.IDLE, agent, {"text": text}, source=src)]
        case "UserPromptSubmit":
            return [Event(E.WORKING, agent, source=src)]
        case "PermissionDenied":
            return [Event(E.BLOCKED, agent, {
                "tool_name": payload.get("tool_name", ""),
                "tool_input": payload.get("tool_input") or {},
                "reason": payload.get("reason", ""),
                "cwd": payload.get("cwd", ""),
                "reviewer": "claude_auto_mode",
            }, source=src)]
        case "PermissionRequest":
            if kind != "claude":
                return []  # Codex: auto_review handles requests; denials come from rollouts
            return [Event(E.DIALOG, agent, {
                "tool_name": payload.get("tool_name", ""),
                "tool_input": payload.get("tool_input") or {},
                "cwd": payload.get("cwd", ""),
            }, source=src)]
        case "Notification":
            if payload.get("notification_type") == "idle_prompt":
                return [Event(E.IDLE, agent, source=src)]
            return []
        case "SessionStart":
            return [Event(E.SESSION_START, agent, {"source": payload.get("source", "")}, source=src)]
        case "SessionEnd":
            return [Event(E.SESSION_END, agent, {"reason": payload.get("reason", "")}, source=src)]
    return []


def rollout_meta(first_line: str) -> dict[str, Any]:
    try:
        obj = json.loads(first_line)
    except json.JSONDecodeError:
        return {}
    return obj.get("payload", {}) if obj.get("type") == "session_meta" else {}


def codex_compaction(obj: dict[str, Any]) -> dict[str, Any] | None:
    """{count, pre} from a rollout `compacted` line; window_number counts compactions in the thread."""
    if obj.get("type") != "compacted":
        return None
    p = obj.get("payload", {})
    usage = (p.get("latest_token_usage_record") or {}).get("usage") or {}
    return {"count": p.get("window_number"), "pre": usage.get("input_tokens"), "post": None}


def codex_context(obj: dict[str, Any]) -> dict[str, Any] | None:
    p = obj.get("payload", {})
    if obj.get("type") != "event_msg" or p.get("type") != "token_count" or not p.get("info"):
        return None
    info = p["info"]
    tokens = (info.get("last_token_usage") or {}).get("input_tokens")
    if not tokens or not info.get("model_context_window"):
        return None
    return {"tokens": tokens, "window": info["model_context_window"]}


def from_codex_main_line(line: str, agent: str | None) -> list[Event]:
    """Events from a line of a Codex main-thread rollout, timed by the line's own timestamp.

    The rollout is read every couple of seconds, so read time would put a turn's start after a capacity error
    seen on screen in the meantime.
    """
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return []
    events = _codex_main_events(obj, agent)
    if (ts := iso_ts(obj.get("timestamp"))) is not None:
        for ev in events:
            ev.ts = ts
    return events


def _codex_main_events(obj: dict[str, Any], agent: str | None) -> list[Event]:
    if (c := codex_compaction(obj)) is not None:
        return [Event(E.COMPACTION, agent, c, source="rollout")]
    if (c := codex_context(obj)) is not None:
        out = [Event(E.CONTEXT, agent, c, source="rollout")]
        if (w := codex_rate_limits(obj)) is not None:
            out.append(Event(E.USAGE, agent, {"windows": w}, source="rollout"))
        return out
    if (w := codex_rate_limits(obj)) is not None:
        return [Event(E.USAGE, agent, {"windows": w}, source="rollout")]
    if obj.get("type") != "event_msg":
        return []
    p = obj.get("payload", {})
    src = "rollout"
    match p.get("type"):
        case "task_started":
            return [Event(E.WORKING, agent, source=src)]
        case "task_complete":
            err = p.get("error")
            if isinstance(err, dict) and err:
                info = err.get("codex_error_info")
                ev = classify_error_text(err.get("message") or "", info, agent, src, CODEX_TURN_ERROR_CAPACITY)
                if ev:
                    ev.data["error"] = info
                    return [ev]
                return [Event(E.IDLE, agent, {"text": err.get("message") or "", "error": info}, source=src)]
            return [Event(E.IDLE, agent, {"text": p.get("last_agent_message") or ""}, source=src)]
        case "turn_aborted":
            return [Event(E.IDLE, agent, {"aborted": p.get("reason", "")}, source=src)]
        case "error" | "stream_error":
            info = p.get("codex_error_info")
            if isinstance(info, dict):
                info = next(iter(info), None)
            ev = classify_error_text(p.get("message", ""), info, agent, src, CODEX_ERROR_CAPACITY)
            return [ev] if ev else []
    return []


class GuardianReview:
    """Incrementally parses a Codex auto-review (guardian) rollout.

    The reviewer receives "Planned action JSON:" followed by the action, and
    answers with {"outcome": "allow"|"deny", "rationale": ...}.
    """

    def __init__(self, parent_thread_id: str | None):
        self.parent_thread_id = parent_thread_id
        self._planned: dict[str, Any] | None = None

    def feed(self, line: str, agent: str | None) -> list[Event]:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return []
        p = obj.get("payload", {})
        if obj.get("type") != "response_item" or p.get("type") != "message":
            return []
        content = p.get("content") or []
        if p.get("role") == "user":
            for i, c in enumerate(content):
                if c.get("text", "").strip().startswith("Planned action JSON") and i + 1 < len(content):
                    try:
                        self._planned = json.loads(content[i + 1].get("text", ""))
                    except json.JSONDecodeError:
                        self._planned = {"raw": content[i + 1].get("text", "")}
            return []
        if p.get("role") != "assistant":
            return []
        text = "".join(c.get("text", "") for c in content)
        try:
            verdict = json.loads(text)
        except json.JSONDecodeError:
            return []
        if not isinstance(verdict, dict) or verdict.get("outcome") != "deny" or self._planned is None:
            return []
        planned, self._planned = self._planned, None
        tool_name, tool_input = codex_planned_action(planned)
        return [Event(E.BLOCKED, agent, {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "reason": verdict.get("rationale", ""),
            "cwd": planned.get("cwd", "") if isinstance(planned, dict) else "",
            "reviewer": "codex_auto_review",
        }, source="guardian")]


def codex_planned_action(planned: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Normalize a guardian planned action to (tool_name, tool_input) like Claude's hooks."""
    if not isinstance(planned, dict):
        return "unknown", {"raw": str(planned)}
    tool = planned.get("tool", "")
    if "command" in planned or tool in ("shell", "exec_command", "local_shell", "unified_exec"):
        return "Bash", {"command": planned.get("command") or planned.get("cmd") or planned.get("arguments", {}).get("command"),
                        "cwd": planned.get("cwd")}
    if tool == "mcp_tool_call":
        name = f"mcp__{planned.get('server', '')}__{planned.get('tool_name', '')}"
        return name, {"tool_name": name, "arguments": planned.get("arguments", {})}
    if tool in ("apply_patch", "write_file"):
        return "Edit", {k: v for k, v in planned.items() if k != "tool"}
    return tool or "unknown", {k: v for k, v in planned.items() if k != "tool"}


def claude_compaction(obj: dict[str, Any]) -> dict[str, Any] | None:
    if obj.get("type") != "system" or obj.get("subtype") != "compact_boundary" or obj.get("isSidechain"):
        return None
    meta = obj.get("compactMetadata") or {}
    return {"pre": meta.get("preTokens"), "post": meta.get("postTokens"), "trigger": meta.get("trigger")}


def claude_context_tokens(obj: dict[str, Any]) -> int | None:
    """Context size after a main-thread assistant message: all input tokens of that request."""
    if obj.get("type") != "assistant" or obj.get("isSidechain") or obj.get("isApiErrorMessage"):
        return None
    u = (obj.get("message") or {}).get("usage") or {}
    total = sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return total or None


# ---- Codex TUI screen -----------------------------------------------------------------------------
# Codex (0.163, `--remote`) shows a capacity error in the TUI but doesn't write it to the rollout, so the
# screen is read. Its messages: "Selected model is at capacity. Please try a different model.",
# "Server overloaded; retry later."

# The message must start the line (after a marker such as "■" or "└"): the agent's own replies ("• ...") may
# quote it.
CODEX_SCREEN_CAPACITY = re.compile(
    r"^[^\w•]*(?:error:?\s*)?(?:selected model is at capacity|(?:the )?model (?:is )?at capacity|server overloaded"
    r"|flex capacity unavailable)"
    r"|^\s*[■⚠✗]\s*.*(?:at capacity|overloaded|try again later)", re.I)
CODEX_WORKING = re.compile(r"esc to interrupt", re.I)
_MENU_ITEM = re.compile(r"^\s*(›)?\s*(\d+)\.\s*(.+?)(?:\s+\(|\s{2,}|$)")


def screen_lines(text: str) -> list[str]:
    """iTerm2 screen lines with the NULs it uses for untouched cells turned into spaces."""
    return [ln.replace("\x00", " ").rstrip() for ln in text.splitlines()]


_RULE = re.compile(r"^\s*─{20,}\s*$")
_CODEX_STATUS = re.compile(r" · Context \d+% used|^\s*\? for shortcuts")
CODEX_PLACEHOLDERS = {"Ask Codex to do anything", "Ask a follow-up question"}
# Claude's dim text in an empty prompt box: an example prompt, or the hint shown while messages are queued
_CLAUDE_PLACEHOLDER = re.compile(r'^(Try ".*"|Press up to (edit|select) (a )?queued messages?\b.*)$')


def composer_draft(text: str, kind: str) -> str | None:
    """The text in the agent's prompt box ("" when empty), or None when the box isn't on screen (a dialog, menu or
    other overlay has replaced it). Wrapped and multi-line drafts come back joined by newlines.

    Claude: "❯ <text>" between two rules, continuation lines indented. Codex: the last "› <text>" with its status
    line just below; its placeholder text counts as empty.
    """
    lines = screen_lines(text)
    if kind == "claude":
        for i in range(len(lines) - 1, 0, -1):
            if re.match(r"^❯(\s|$)", lines[i]) and _RULE.match(lines[i - 1]):
                end = next((j for j in range(i + 1, len(lines)) if _RULE.match(lines[j])), None)
                if end is None:
                    return None
                body = [lines[i][1:].strip()] + [ln.strip() for ln in lines[i + 1:end]]
                draft = "\n".join(body).strip()
                return "" if _CLAUDE_PLACEHOLDER.match(draft) else draft
        return None
    for i in range(len(lines) - 1, -1, -1):
        if re.match(r"^›(\s|$)", lines[i]):
            below = lines[i + 1:i + 8]
            status = next((k for k, ln in enumerate(below) if _CODEX_STATUS.search(ln)), None)
            if status is None:
                return None
            body = [lines[i][1:].strip()] + [ln.strip() for ln in below[:status]]  # every line counts
            draft = "\n".join(b for b in body if b).strip()
            return "" if draft in CODEX_PLACEHOLDERS else draft
    return None


def codex_screen_capacity(text: str) -> str | None:
    """The capacity error line when it is the last thing in the transcript of an idle Codex TUI, else None.

    The composer is the last line starting with "›" and the user's prompts above it start with "›" too; only the
    few lines after the latest prompt and just above the composer count, so an older error, or one followed by
    a new prompt ("› continue"), doesn't.
    """
    lines = screen_lines(text)
    if any(CODEX_WORKING.search(ln) for ln in lines):
        return None
    prompts = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("›")]
    if not prompts:
        return None
    start = prompts[-2] + 1 if len(prompts) > 1 else 0
    tail = [ln.strip() for ln in lines[start:prompts[-1]] if ln.strip()][-3:]
    return next((ln for ln in reversed(tail) if CODEX_SCREEN_CAPACITY.search(ln)), None)


def menu_items(text: str) -> list[tuple[str, bool, bool]]:
    """(label, selected, current) for each numbered item of a Codex selection menu on screen."""
    out = []
    for ln in screen_lines(text):
        m = _MENU_ITEM.match(ln)
        if m:
            out.append((m.group(3).strip(), bool(m.group(1)), "(current)" in ln))
    return out


# The status line under the composer starts "<model> <effort> · <dir> · ...", e.g. "GPT-6.1-Sol medium · ~/code/x".
_STATUS_EFFORT = re.compile(r"^\s*(\S.*?) (minimal|low|medium|high|xhigh|max|ultra) · ")
_EFFORT_LABELS = {"xhigh": "Extra high"}


def codex_status_effort(text: str) -> str | None:
    """The session's effort from the Codex status line, as the /model effort menu labels it ("Extra high")."""
    for ln in reversed(screen_lines(text)):
        if m := _STATUS_EFFORT.match(ln):
            word = m.group(2)
            return _EFFORT_LABELS.get(word, word.capitalize())
    return None


def codex_status_model(text: str) -> str | None:
    """The session's model from the Codex status line ("GPT-6.1-Sol", "Daybreak Blue")."""
    for ln in reversed(screen_lines(text)):
        if m := _STATUS_EFFORT.match(ln):
            return m.group(1)
    return None
