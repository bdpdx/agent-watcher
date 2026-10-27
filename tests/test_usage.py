"""Plan usage limits: parsing (rollouts, status line, /usage screen), crossings, cadence, reporting, the reader."""

import datetime as dt
import json
from pathlib import Path

from agent_watcher import cli
from agent_watcher.actuator import Actuator
from agent_watcher.detect import from_codex_main_line
from agent_watcher.events import E, Event
from agent_watcher.supervisor import Supervisor
from agent_watcher.usage import (AgentUsage, Window, claude_rate_limits, fmt_reset, parse_usage_screen,
                                 scrape_interval, update)

from .conftest import FakeChannel, FakeNotifier, FakeTerminal, make_cfg, record
from .test_supervisor import settle

SCREEN = (Path(__file__).parent / "claude_usage_screen.txt").read_text()  # captured from Claude Code 2.1.303
# The bottom of a /usage screen taller than the terminal, as Claude Code 2.1.303 shows it: the tab header and the
# limits have scrolled away, and the prompt box (with a queued message) stays below.
TALL_SCREEN_BOTTOM = """  Loops                                      every  runs  tokens  per run last run
  dev100002 PDI keep-alive (demo-project\u2026every 5 h\u2026     3    1.3m   437.9k   4h ago
  d to day \u00b7 w to week
  Usage credits
  Usage credits are off \u00b7 /usage-credits to turn them on
  Esc to cancel
\u2500\u2500\u2500\u2500
\u276f\u00a0Press up to edit queued messages
\u2500\u2500\u2500\u2500
  \u23f5\u23f5 auto mode on"""
NOW = dt.datetime(2026, 10, 19, 11, 0).astimezone()
MARKS = [25, 15, 10, 5]


def token_count(used, resets=1792883772, ts="2026-10-19T18:54:36.632Z"):
    return json.dumps({"timestamp": ts, "type": "event_msg", "payload": {
        "type": "token_count", "info": {"last_token_usage": {"input_tokens": 100_000}, "model_context_window": 475_000},
        "rate_limits": {"primary": {"used_percent": used, "window_minutes": 10080, "resets_at": resets}}}})


# ---- parsing ----------------------------------------------------------------------------------------

def test_parse_real_usage_screen():
    w = parse_usage_screen(SCREEN, NOW)
    assert {k: v.left_pct for k, v in w.items()} == {"session": 88, "week": 59, "fable": 87}
    assert dt.datetime.fromtimestamp(w["session"].resets_at) == dt.datetime(2026, 10, 19, 13, 9)
    assert dt.datetime.fromtimestamp(w["fable"].resets_at) == dt.datetime(2026, 10, 23, 23, 59)


def test_codex_rollout_carries_weekly_limit():
    evs = from_codex_main_line(token_count(46.0), "codex-g")
    usage = next(e for e in evs if e.kind is E.USAGE)
    assert usage.data["windows"]["weekly"].left_pct == 54
    assert any(e.kind is E.CONTEXT for e in evs)


def test_claude_status_line_limits():
    w = claude_rate_limits({"rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": 1},
                                            "seven_day": {"used_percentage": 41.2, "resets_at": 2}}})
    assert {k: v.left_pct for k, v in w.items()} == {"session": 76, "week": 59}
    assert claude_rate_limits({}) == {}


# ---- crossings and cadence ------------------------------------------------------------------------

def test_quiet_until_25_left_then_each_mark_once_then_reset():
    u = AgentUsage(introduced=True)
    assert update(u, {"weekly": Window(60, 1)}, MARKS) == []
    assert update(u, {"weekly": Window(76, 1)}, MARKS) == [("weekly", "low")]  # 24% left
    assert update(u, {"weekly": Window(78, 1)}, MARKS) == []  # still above 15
    assert update(u, {"weekly": Window(92, 1)}, MARKS) == [("weekly", "low")]  # skipped 15, now 8 left
    assert u.reported_level["weekly"] == 10
    assert update(u, {"weekly": Window(3, 2)}, MARKS) == [("weekly", "reset")]
    assert update(u, {"weekly": Window(10, 2)}, MARKS) == []


def test_low_before_join_line_is_not_posted_separately():
    u = AgentUsage()
    assert update(u, {"weekly": Window(80, 1)}, MARKS) == []
    assert u.reported_level["weekly"] == 25
    u.introduced = True
    assert update(u, {"weekly": Window(81, 1)}, MARKS) == []


def test_scrape_interval_follows_used():
    assert [scrape_interval(x) for x in (10, 50, 85, 92, 97)] == [86400, 21600, 7200, 3600, 1800]


# ---- supervisor -----------------------------------------------------------------------------------

def make_sup(tmp_path, term, kind_names=("codex-g",), mode="live"):
    cfg = make_cfg(tmp_path, mode)
    cfg.modes["usage_check"] = mode
    cfg.timing.menu_step_s, cfg.timing.menu_timeout_s = 0, 0.1
    chan = FakeChannel()
    sup = Supervisor(cfg, term, Actuator(cfg, term, cfg.state_dir / "audit.log"), chan, FakeNotifier())
    sup.gm = lambda p: None
    sup.apply_discovery([record(tmp_path, n, "2026-10-06-ui-fixes") for n in kind_names])
    return cfg, sup, chan


async def test_codex_join_line_then_quiet_then_crossing(tmp_path):
    cfg, sup, chan = make_sup(tmp_path, FakeTerminal())
    await settle(3)
    chan.posts.clear()
    newer, older = token_count(46.0, ts="2026-10-19T18:54:36Z"), token_count(30.0, ts="2026-10-18T09:00:00Z")
    for line in (newer, older):  # an older session's rollout read after the current one
        for ev in from_codex_main_line(line, "codex-g"):
            await sup.handle(ev)
    await sup.tick()
    await settle(5)
    reset = fmt_reset(1792883772, sup.now())  # "resets Sat Oct 24 4:16 PM", or just the time on that day
    assert [t for t in chan.texts() if "usage" in t or "left" in t] == [f"codex-g usage: weekly 54% left ({reset})"]
    for ev in from_codex_main_line(token_count(77.0, ts="2026-10-19T20:00:00Z"), "codex-g"):
        await sup.handle(ev)
    await settle(5)
    assert chan.texts()[-1] == f"codex-g: weekly 23% left ({reset})."


async def test_tick_skips_usage_read_while_working(tmp_path):
    tui = FakeClaudeTUI()
    cfg, sup, chan = make_sup(tmp_path, tui, ("claude-a",))
    sup.agents["claude-a"].idle = False
    await sup.tick()
    await settle(20)
    assert "claude-a" not in tui.sent


async def test_hello_line_includes_known_usage(tmp_path):
    cfg, sup, chan = make_sup(tmp_path, FakeTerminal())
    sup.agents["codex-g"].usage.windows["weekly"] = Window(46.0, 1792883772)
    await sup._on_channel_ready("2026-10-06-ui-fixes")
    reset = fmt_reset(1792883772, sup.now())
    assert chan.texts()[-1].splitlines()[1] == f"codex-g usage: weekly 54% left ({reset})"
    assert sup.agents["codex-g"].usage.introduced


# ---- the /usage reader -----------------------------------------------------------------------------

class FakeClaudeTUI(FakeTerminal):
    def __init__(self, working=False, screen=SCREEN, transcript=""):
        super().__init__()
        self.buf, self.open, self.working = "", False, working
        self.screen, self.transcript = screen, transcript

    async def screen_text(self, agent):
        if self.open:
            return self.screen
        popup = "  ❯ /usage                      Show session cost, plan usage, and activity stats\n" \
            if self.buf == "/usage" else ""
        spin = "✻ Working… (12s · esc to interrupt)\n" if self.working else ""
        return f"{self.transcript}{spin}{popup}────\n❯\xa0{self.buf}\n────\n  ⏵⏵ auto mode on"

    async def send_raw(self, agent, data):
        await super().send_raw(agent, data)
        if self.open:
            self.open = data != "\x1b" and self.open
        elif data == "\r" and self.buf == "/usage":
            self.open, self.buf = True, ""
        elif data == "\x7f" * len(data):
            self.buf = self.buf[:-len(data)]
        else:
            self.buf += data


async def test_usage_reader_reads_and_closes(tmp_path):
    tui = FakeClaudeTUI()
    cfg, sup, _ = make_sup(tmp_path, tui, ("claude-a",))
    windows = await sup.act.claude_usage("claude-a", "usage_check")
    assert {k: v.left_pct for k, v in windows.items()} == {"session": 88, "week": 59, "fable": 87}
    assert not tui.open and tui.sent["claude-a"] == ["/usage", "\r", "\x1b"]


async def test_usage_reader_closes_a_screen_taller_than_the_terminal(tmp_path):
    """The tab header has scrolled off the top: the rest of the screen still shows it is open."""
    tui = FakeClaudeTUI(screen=TALL_SCREEN_BOTTOM)
    cfg, sup, _ = make_sup(tmp_path, tui, ("claude-a",))
    await sup.act.claude_usage("claude-a", "usage_check")
    assert not tui.open and tui.sent["claude-a"] == ["/usage", "\r", "\x1b"]


async def test_usage_reader_sends_one_esc_when_usage_text_stays_above_the_prompt(tmp_path):
    """Usage text left in the transcript isn't the screen: no second Esc (that would open the rewind menu)."""
    tui = FakeClaudeTUI(transcript="  Current week (all models)  41% used\n")
    cfg, sup, _ = make_sup(tmp_path, tui, ("claude-a",))
    assert await sup.act.claude_usage("claude-a", "usage_check")
    assert tui.sent["claude-a"].count("\x1b") == 1


async def test_usage_reader_never_touches_a_spinner_without_esc_hint(tmp_path):
    """Claude 2.1.303's spinner line no longer says "esc to interrupt"."""
    tui = FakeClaudeTUI(transcript="\u2722 Roosting\u2026 (20m 52s \u00b7 \u2193 32.1k tokens)\n")
    cfg, sup, _ = make_sup(tmp_path, tui, ("claude-a",))
    assert await sup.act.claude_usage("claude-a", "usage_check") is None
    assert "claude-a" not in tui.sent


async def test_usage_reader_leaves_a_draft_alone(tmp_path):
    tui = FakeClaudeTUI()
    tui.buf = "half a thought"
    cfg, sup, _ = make_sup(tmp_path, tui, ("claude-a",))
    assert await sup.act.claude_usage("claude-a", "usage_check") is None
    assert tui.buf == "half a thought" and "\x1b" not in tui.sent["claude-a"]


async def test_usage_reader_never_touches_a_working_session(tmp_path):
    tui = FakeClaudeTUI(working=True)
    cfg, sup, _ = make_sup(tmp_path, tui, ("claude-a",))
    assert await sup.act.claude_usage("claude-a", "usage_check") is None
    assert "claude-a" not in tui.sent


async def test_tick_reads_claude_usage_and_schedules_the_next(tmp_path):
    tui = FakeClaudeTUI()
    cfg, sup, chan = make_sup(tmp_path, tui, ("claude-a",))
    st = sup.agents["claude-a"]
    st.idle = True
    await sup.tick()
    await settle(20)
    assert set(st.usage.windows) == {"session", "week", "fable"}
    assert st.usage.next_scrape - sup.now() > 23 * 3600  # Fable 13% used: daily
    await sup.tick()  # the join line goes out on the tick after the readings
    await settle(5)
    assert any(t.startswith("claude-a usage: session 88% left") for t in chan.texts())


# ---- status line command ---------------------------------------------------------------------------

def test_install_statusline_wraps_existing_command(tmp_path, capsys):
    home = tmp_path / ".claude-x"
    home.mkdir()
    old = "input=$(cat); printf '%s' \"$(echo \"$input\" | jq -r '.workspace.current_dir')\""
    (home / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": old, "padding": 0},
                                                    "model": "opus"}))
    assert cli.install_statusline(home, dry_run=False) == 0
    s = json.loads((home / "settings.json").read_text())
    assert s["model"] == "opus" and s["statusLine"]["padding"] == 0
    assert " statusline --then " in s["statusLine"]["command"]
    assert list(home.glob("settings.json.bak-agent-watcher-*"))
    assert cli.install_statusline(home, dry_run=False) == 0  # idempotent
    assert "already wrapped" in capsys.readouterr().out


def test_statusline_prints_wrapped_line_without_a_daemon(tmp_path, monkeypatch, capsys):
    import io
    monkeypatch.setenv("AGENT_WATCHER_SOCK", str(tmp_path / "none.sock"))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"rate_limits": {"five_hour": {"used_percentage": 5}},
                                                             "workspace": {"current_dir": "/x"}})))
    assert cli.statusline("cat >/dev/null; printf 'host:/x'") == 0
    assert capsys.readouterr().out == "host:/x"


def test_fmt_reset_shows_the_date_unless_it_is_today():
    at = dt.datetime(2026, 10, 24, 16, 16).timestamp()
    assert fmt_reset(at, dt.datetime(2026, 10, 19, 9, 0).timestamp()) == "resets Sat Oct 24 4:16 PM"
    assert fmt_reset(at, dt.datetime(2026, 10, 24, 9, 0).timestamp()) == "resets 4:16 PM"


async def test_local_usage_command_makes_the_read_due_now(tmp_path):
    tui = FakeClaudeTUI()
    cfg, sup, _ = make_sup(tmp_path, tui, ("claude-a",))
    st = sup.agents["claude-a"]
    st.idle = True
    st.usage.next_scrape = sup.now() + 24 * 3600
    await sup.tick()
    await settle(5)
    assert "claude-a" not in tui.sent  # not due yet
    assert sup.control("usage", "claude-a")["ok"]
    await sup.tick()
    await settle(20)
    assert set(st.usage.windows) == {"session", "week", "fable"}
    assert not sup.control("usage", "nobody")["ok"]
