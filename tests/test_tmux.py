from __future__ import annotations

import pytest

from agent_watcher.discovery import Discovery
from agent_watcher.events import E
from agent_watcher.terminals import Terminals
from agent_watcher.tmux import TmuxTerminal, pane_ref, split_keys

from .conftest import make_cfg
from .test_discovery import runner

SOCK = "/private/tmp/tmux-502/default"


class FakeTmux:
    """One tmux server: its panes (id -> current command, screen, copy mode) and attached clients."""

    def __init__(self):
        self.panes = {"%0": {"cmd": "claude", "screen": "hello\n> \n\n", "mode": "0"}}
        self.clients: list[tuple[str, int]] = []  # (pane shown, client_activity)
        self.calls: list[tuple[str, ...]] = []

    async def run(self, socket, *args):
        assert socket == SOCK
        self.calls.append(args)
        cmd = args[0]
        if cmd == "display-message":
            pane = args[args.index("-t") + 1]
            if pane not in self.panes:
                return 1, ""
            fmt = args[-1]
            p = self.panes[pane]
            return 0, {"#{pane_id}": pane, "#{pane_in_mode}": p["mode"],
                       "#{pane_current_command}": p["cmd"]}[fmt] + "\n"
        if cmd == "capture-pane":
            return 0, self.panes[args[args.index("-t") + 1]]["screen"]
        if cmd == "list-panes":
            return 0, "".join(f"{k} {v['mode']}\n" for k, v in self.panes.items())
        if cmd == "list-clients":
            return 0, "".join(f"{p} {a}\n" for p, a in self.clients)
        return 0, ""


async def bound_term(tmp_path):
    fake, events, changes = FakeTmux(), [], []

    async def emit(ev):
        events.append(ev)

    t = TmuxTerminal(make_cfg(tmp_path), emit, lambda: changes.append(1), run=fake.run)
    await t.bind("claude-c", f"%0:{SOCK}")
    return t, fake, events, changes


def test_pane_ref():
    assert pane_ref(f"{SOCK},18682,0", "%3") == f"%3:{SOCK}"
    assert pane_ref(None, "%3") is None and pane_ref(f"{SOCK},1,0", None) is None


def test_discovery_prefers_tmux_over_inherited_iterm(tmp_path):
    procs = {10: {"name": "claude", "args": "claude", "cwd": tmp_path,
                  "env": {"ITERM_SESSION_ID": "w0t0p0:GUID-A", "TMUX": f"{SOCK},18682,0", "TMUX_PANE": "%7"}},
             11: {"name": "codex", "args": "codex", "cwd": tmp_path,
                  "env": {"CODEX_HOME": str(tmp_path / ".codex"), "TERM": "screen"}}}  # GNU screen: not driven
    recs = Discovery(runner(procs)).scan()
    assert [r.term_id for r in recs] == [f"tmux:%7:{SOCK}"]


async def test_bind_read_and_type(tmp_path):
    t, fake, _, _ = await bound_term(tmp_path)
    assert t.bound("claude-c")
    assert ("set-option", "-p", "-t", "%0", "@agent-watcher-agent", "claude-c") in fake.calls
    assert await t.job_name("claude-c") == "claude"
    assert await t.screen_text("claude-c") == "hello\n> "
    await t.send_raw("claude-c", "/usage")
    assert fake.calls[-1] == ("set-buffer", "-b", "agent-watcher-claude-c", "--", "/usage", ";",
                              "paste-buffer", "-d", "-r", "-b", "agent-watcher-claude-c", "-t", "%0")
    await t.send_raw("claude-c", "\x1b")
    assert fake.calls[-1] == ("send-keys", "-t", "%0", "Escape")


def test_split_keys():
    assert split_keys("continue") == [(True, "continue")]
    assert split_keys("\x1b") == [(False, ("Escape",))]
    assert split_keys("\x1b[B\x1b[A\r") == [(False, ("Down", "Up", "Enter"))]
    assert split_keys("\x7f" * 3) == [(False, ("BSpace",) * 3)]
    assert split_keys("\x05\x13\x03") == [(False, ("C-e", "C-s", "C-c"))]
    assert split_keys("a\nb\rc") == [(True, "a\nb"), (False, ("Enter",)), (True, "c")]


async def test_bind_skips_missing_pane(tmp_path):
    t, _, _, _ = await bound_term(tmp_path)
    await t.bind("codex-g", f"%9:{SOCK}")
    assert not t.bound("codex-g") and await t.screen_text("codex-g") == ""


async def test_no_typing_in_copy_mode_or_closed_pane(tmp_path):
    t, fake, _, _ = await bound_term(tmp_path)
    fake.panes["%0"]["mode"] = "1"
    with pytest.raises(RuntimeError, match="copy mode"):
        await t.send_raw("claude-c", "continue")
    del fake.panes["%0"]
    with pytest.raises(RuntimeError, match="gone"):
        await t.send_raw("claude-c", "continue")
    assert not any(c[0] == "set-buffer" for c in fake.calls)


async def test_poll_owner_activity_copy_mode_and_close(tmp_path):
    t, fake, events, changes = await bound_term(tmp_path)
    fake.clients = [("%0", 100)]
    await t.poll_once()
    assert events == []  # the first reading is a baseline
    fake.clients = [("%0", 100), ("%5", 300)]  # typing in another pane doesn't count
    await t.poll_once()
    assert events == []
    fake.clients = [("%0", 101)]
    await t.poll_once()
    assert [e.kind for e in events] == [E.OWNER_KEYSTROKE]
    fake.panes["%0"]["mode"] = "1"  # scrolling back: hold off for as long as it lasts
    await t.poll_once()
    await t.poll_once()
    assert len(events) == 3
    assert changes == []
    fake.panes = {"%1": fake.panes["%0"]}  # %0 closed, %1 opened
    await t.poll_once()
    assert not t.bound("claude-c") and changes == [1]


async def test_router_picks_backend_by_term_id(tmp_path):
    fake = FakeTmux()

    async def emit(ev):
        pass

    terms = Terminals(make_cfg(tmp_path), emit)
    terms.tmux.run = fake.run
    await terms.bind("claude-c", f"tmux:%0:{SOCK}")
    assert terms.bound("claude-c") and await terms.job_name("claude-c") == "claude"
    await terms.bind("claude-x", "bogus")
    assert not terms.bound("claude-x") and await terms.screen_text("claude-x") == ""
    terms.unbind("claude-c")
    assert not terms.bound("claude-c") and "claude-c" not in terms.tmux.panes
