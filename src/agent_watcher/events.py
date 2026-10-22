"""Events flowing from the sources (hooks, rollouts, Slack, iTerm2) to the supervisor."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class E(str, Enum):
    IDLE = "idle"  # turn finished; agent waiting for input
    WORKING = "working"  # a turn started
    CAPACITY = "capacity"  # overload / server / connection error stopped the turn
    RATE_LIMIT = "rate_limit"  # plan limit; data.resume_at (epoch) when known
    SAFETY_FLAG = "safety_flag"  # a turn was refused by a safety reviewer
    SESSION_REFUSAL = "session_refusal"  # the whole session refuses; it cannot continue
    BLOCKED = "blocked"  # an auto reviewer denied an action; the agent keeps going
    DIALOG = "dialog"  # a real permission dialog is waiting in the terminal
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    COMPACTION = "compaction"  # the agent's context was compacted; data.count since session start
    CONTEXT = "context"  # current context size: data.tokens, data.window
    USAGE = "usage"  # plan usage limits: data.windows {name: usage.Window}
    MESSAGE = "message"  # a message in the agent-watcher channel
    CHANNEL_READY = "channel_ready"  # the watcher found (or was invited to) its channel
    OWNER_KEYSTROKE = "owner_keystroke"  # the owner typed in the agent's tab


class Tier(int, Enum):
    """Who sent a message, in precedence order (lower wins)."""

    OWNER = 1
    AUTHORIZED_HUMAN = 2
    GM = 3
    AGENT = 4
    OTHER = 9


@dataclass
class Event:
    kind: E
    agent: str | None
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    source: str = ""

    def __repr__(self) -> str:  # compact for logs
        d = {k: (v if len(str(v)) < 120 else str(v)[:117] + "...") for k, v in self.data.items()}
        return f"Event({self.kind.value}, {self.agent}, {self.source}, {d})"
