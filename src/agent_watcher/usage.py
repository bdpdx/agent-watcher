"""Plan usage limits: what each agent has left, when it resets, and when that is worth posting.

Sources (no keystrokes except the last):
  Codex   rollout token_count events carry rate_limits.primary (the weekly window).
  Claude  the status line command's JSON carries rate_limits.five_hour and .seven_day; `agent-watcher statusline`
          forwards it. The per-model weekly window (Fable) is only on the /usage screen, read on a schedule.

Posting: one line per agent when it joins (or the watcher comes online), then quiet until a window has 25% or
less left; one post at each crossing of the `report_left_pct` marks, and one when a reported window resets.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

LABELS = {"session": "session", "week": "week", "weekly": "weekly", "fable": "Fable week"}
CLAUDE_WINDOWS = {"five_hour": "session", "seven_day": "week"}


@dataclass
class Window:
    used_pct: float
    resets_at: float | None  # epoch seconds
    observed_at: float = 0  # when this reading was taken; older readings never replace newer ones

    @property
    def left_pct(self) -> int:
        return max(0, round(100 - self.used_pct))


@dataclass
class AgentUsage:
    windows: dict[str, Window] = field(default_factory=dict)
    reported_level: dict[str, int] = field(default_factory=dict)  # window -> lowest mark posted (101 = none)
    introduced: bool = False  # the join line was posted
    next_scrape: float = 0  # when /usage is next read (Claude, per-model window)


def codex_rate_limits(obj: dict[str, Any]) -> dict[str, Window] | None:
    """{"weekly": Window} from a rollout token_count event's rate_limits, or None."""
    p = obj.get("payload", {})
    if obj.get("type") != "event_msg" or p.get("type") != "token_count":
        return None
    prim = (p.get("rate_limits") or {}).get("primary")
    if not isinstance(prim, dict) or prim.get("used_percent") is None:
        return None
    key = "weekly" if (prim.get("window_minutes") or 0) >= 7 * 24 * 60 else "session"
    observed = dt.datetime.fromisoformat(obj["timestamp"].replace("Z", "+00:00")).timestamp() \
        if obj.get("timestamp") else 0
    return {key: Window(float(prim["used_percent"]), prim.get("resets_at"), observed)}


def claude_rate_limits(payload: dict[str, Any], now: float | None = None) -> dict[str, Window]:
    """{"session": …, "week": …} from a Claude status line payload (each window may be absent)."""
    now = dt.datetime.now().timestamp() if now is None else now
    out = {}
    for src, key in CLAUDE_WINDOWS.items():
        w = (payload.get("rate_limits") or {}).get(src)
        if isinstance(w, dict) and w.get("used_percentage") is not None:
            out[key] = Window(float(w["used_percentage"]), w.get("resets_at"), now)
    return out


# ---- the /usage screen (Claude) ---------------------------------------------------------------------

_SECTION = re.compile(r"^\s*Current (session|week) ?(?:\((.+?)\))?\s*$")
_USED = re.compile(r"(\d{1,3})% used")
_RESETS = re.compile(r"^\s*Resets (?:(\w{3}) (\d{1,2}) at )?(\d{1,2})(?::(\d{2}))?\s*([ap]m)", re.I)
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"), 1)}


def _reset_epoch(m: re.Match, now: dt.datetime) -> float:
    hour = int(m.group(3)) % 12 + (12 if m.group(5).lower() == "pm" else 0)
    at = now.replace(hour=hour, minute=int(m.group(4) or 0), second=0, microsecond=0)
    if m.group(1):
        at = at.replace(month=_MONTHS[m.group(1).lower()], day=int(m.group(2)))
        if at < now - dt.timedelta(days=1):
            at = at.replace(year=at.year + 1)
    elif at <= now:
        at += dt.timedelta(days=1)
    return at.timestamp()


def parse_usage_screen(text: str, now: dt.datetime | None = None) -> dict[str, Window]:
    """Windows from Claude's /usage screen: "Current session", "Current week (all models)", "Current week (Fable)".

    Keys: "session", "week", and the lowercased model name for per-model weeks ("fable"). The first block of each
    name wins (the screen repeats some further down).
    """
    from .detect import screen_lines  # detect imports this module
    now = now or dt.datetime.now().astimezone()
    out: dict[str, Window] = {}
    lines = screen_lines(text)
    for i, ln in enumerate(lines):
        m = _SECTION.match(ln)
        if not m:
            continue
        model = (m.group(2) or "").strip()
        key = m.group(1) if not model or model == "all models" else model.lower()
        if key in out:
            continue
        used = resets = None
        for nxt in lines[i + 1:i + 4]:
            if used is None and (u := _USED.search(nxt)):
                used = float(u.group(1))
            elif (r := _RESETS.match(nxt)):
                resets = _reset_epoch(r, now)
        if used is not None:
            out[key] = Window(used, resets, now.timestamp())
    return out


# ---- when to look, when to post -------------------------------------------------------------------

# (used % at or above, seconds between /usage reads): daily below 50% used, then 6h, 2h, 1h, 30m.
SCRAPE_EVERY = [(95, 1800), (90, 3600), (80, 7200), (50, 6 * 3600), (0, 24 * 3600)]


def scrape_interval(used_pct: float | None) -> float:
    if used_pct is None:
        return 3600  # unknown yet (e.g. last read failed): retry within the hour
    return next(every for at, every in SCRAPE_EVERY if used_pct >= at)


def fmt_reset(ts: float | None, now: float) -> str:
    if not ts:
        return "reset time unknown"
    at = dt.datetime.fromtimestamp(ts)
    today = dt.datetime.fromtimestamp(now).date()
    clock = at.strftime("%-I:%M %p")
    return f"resets {clock}" if at.date() == today else f"resets {at.strftime('%a %b %-d')} {clock}"


def fmt_window(key: str, w: Window, now: float) -> str:
    return f"{LABELS.get(key, key)} {w.left_pct}% left ({fmt_reset(w.resets_at, now)})"


def summary(name: str, u: AgentUsage, order: list[str], now: float) -> str:
    parts = [fmt_window(k, u.windows[k], now) for k in order if k in u.windows]
    return f"{name} usage: " + ("; ".join(parts) if parts else "not known yet")


def update(u: AgentUsage, fresh: dict[str, Window], marks: list[int]) -> list[tuple[str, str]]:
    """Merge fresh readings; return (window, "low" | "reset") changes worth posting.

    "low": the window crossed one of `marks` (% left, descending) below the last one posted. "reset": a window
    that had been posted as low came back above the highest mark.
    """
    changes = []
    top = max(marks)
    for key, w in fresh.items():
        prev = u.windows.get(key)
        if prev is not None and w.observed_at < prev.observed_at:
            continue  # e.g. an older session's rollout read after the current one
        u.windows[key] = w
        level = u.reported_level.get(key, 101)
        crossed = [m for m in marks if w.left_pct <= m < level]
        if crossed:
            u.reported_level[key] = min(crossed)
            if u.introduced:  # before that, the join line shows it
                changes.append((key, "low"))
        elif level <= top and w.left_pct > top:
            u.reported_level[key] = 101
            changes.append((key, "reset"))
    return changes
