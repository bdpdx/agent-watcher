"""Quitting an agent for a restart: Codex /stop then /quit; Claude /quit and its background-work menu."""

from agent_watcher.actuator import ENTER, Actuator

from .conftest import FakeTerminal, make_cfg, record

RULE = "─" * 60
CODEX_IDLE = ("• Done.\n\n› Ask Codex to do anything\n\n"
              "  GPT-6.1-Sol medium · ~/code/x · Context 9% used{bg}\n  ? for shortcuts")
CLAUDE_IDLE = f"✻ Brewed for 8s · done 10:11 AM\n{RULE}\n❯\xa0{{draft}}\n{RULE}\n  ⏵⏵ auto mode on"
CLAUDE_MENU = """✻ Brewed for 8s · done 10:11 AM
▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔
   Background work is running
   The following will stop when you exit:

   scheduled task · Every day at 8:02 AM · GM daily triage (version …
   scheduled task · Every 2 hours at :23 · GM drift checkpoint …

   {a} 1. Exit and stop tasks
   {b} 2. Move to background and exit
     3. Stay"""


class FakeTUI(FakeTerminal):
    def __init__(self, kind, background=False, draft="", menu_on=1):
        super().__init__()
        self.kind, self.background, self.draft, self.menu_on = kind, background, draft, menu_on
        self.menu = False

    async def screen_text(self, agent):
        if self.kind == "codex":
            bg = " · 1 background terminal running · /ps to view · /stop to close" if self.background else ""
            return CODEX_IDLE.format(bg=bg).replace("Ask Codex to do anything", self.draft or "Ask Codex to do anything")
        if self.menu:
            return CLAUDE_MENU.format(a="❯" if self.menu_on == 1 else " ", b="❯" if self.menu_on == 2 else " ")
        return CLAUDE_IDLE.format(draft=self.draft)

    async def send_raw(self, agent, data):
        await super().send_raw(agent, data)
        if data == "/stop":
            self.background = False
        if data == "/quit" and self.kind == "claude" and self.background:
            self.menu = True


def make(tmp_path, tui, kind):
    cfg = make_cfg(tmp_path)
    name = f"{kind}-x"
    r = record(tmp_path, name, None, kind=kind)
    cfg.agents[name] = cfg.agent_config(name, kind, r.home, r.cwd, r.launch_command())
    return Actuator(cfg, tui, cfg.state_dir / "audit.log"), name


async def test_codex_stops_background_terminals_then_quits(tmp_path):
    tui = FakeTUI("codex", background=True)
    act, name = make(tmp_path, tui, "codex")
    ok, how = await act.quit(name, "restart")
    assert ok and how == "/stop, /quit" and tui.lines(name) == ["/stop", "/quit"]


async def test_codex_without_background_just_quits(tmp_path):
    tui = FakeTUI("codex")
    act, name = make(tmp_path, tui, "codex")
    assert await act.quit(name, "restart") == (True, "/quit") and tui.lines(name) == ["/quit"]


async def test_claude_background_menu_exits_and_stops_tasks(tmp_path):
    tui = FakeTUI("claude", background=True)
    act, name = make(tmp_path, tui, "claude")
    ok, how = await act.quit(name, "restart")
    assert ok and how == "/quit, Exit and stop tasks" and tui.sent[name][-1] == ENTER


async def test_claude_menu_on_another_option_presses_nothing(tmp_path):
    tui = FakeTUI("claude", background=True, menu_on=2)
    act, name = make(tmp_path, tui, "claude")
    ok, how = await act.quit(name, "restart")
    assert not ok and "isn't on" in how and tui.sent[name][-1] == "\r"  # only /quit's own Enter


async def test_a_draft_or_no_prompt_box_stops_the_quit(tmp_path):
    for kind in ("claude", "codex"):
        tui = FakeTUI(kind, draft="half-written note")
        act, name = make(tmp_path / kind, tui, kind)
        ok, how = await act.quit(name, "restart")
        assert not ok and "draft" in how and name not in tui.sent
    tui = FakeTerminal()  # blank screen: no prompt box
    act, name = make(tmp_path / "blank", tui, "claude")
    assert (await act.quit(name, "restart"))[0] is False and name not in tui.sent


async def test_dry_mode_types_nothing(tmp_path):
    tui = FakeTUI("codex", background=True)
    act, name = make(tmp_path, tui, "codex")
    act.cfg.modes["restart"] = "dry"
    assert (await act.quit(name, "restart"))[0] is False and name not in tui.sent


async def test_screen_idle_reads_the_prompt_box_and_working_indicator(tmp_path):
    tui = FakeTUI("codex", background=True)
    act, name = make(tmp_path, tui, "codex")
    assert await act.screen_idle(name)
    tui.draft = ""
    real = tui.screen_text

    async def working(agent):
        return (await real(agent)).replace("• Done.", "• Working (2m 3s • esc to interrupt)")
    tui.screen_text = working
    assert not await act.screen_idle(name)
