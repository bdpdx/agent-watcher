"""Codex capacity errors read from the TUI screen, and the /model switch before `continue`."""

import json

from agent_watcher.actuator import Actuator
from agent_watcher.config import ModelRotation
from agent_watcher.detect import codex_screen_capacity, menu_items
from agent_watcher.events import E, Event
from agent_watcher.detect import from_codex_main_line
from agent_watcher.supervisor import LOCAL_PROJECT, Supervisor
from agent_watcher.templates import TEXT, T

from .conftest import FakeChannel, FakeNotifier, FakeTerminal, make_cfg, record
from .test_supervisor import settle

ROT = ModelRotation("GPT-6.1-Sol", ["Medium", "High", "Extra high"], "s")
STATUS = "  GPT-6.1-Sol {effort} · ~/code/proj-codex-g · Context 67% used · weekly 70% left · Main [default]"

IDLE_ERROR = """\
  The fresh 2.1.1 binding is pinned. Full walk verification awaits C's handoff and jobs.

■ Selected model is at capacity. Please try a different model.

› Ask Codex to do anything

""" + STATUS.format(effort="medium") + "\n  ? for shortcuts"

MODELS = [("GPT-6.1-Sol", "Latest workhorse model for coding and everyday work."),
          ("GPT-6-Astra", "Frontier intelligence for the most demanding work."),
          ("GPT-6-Sol", "Previous generation workhorse model."),
          ("Daybreak Blue", "Latest frontier agentic coding model for broad defensive cybersecurity work.")]
EFFORTS = [("Low", "Fast responses with lighter reasoning"), ("Medium", "Balances speed and reasoning depth"),
           ("High", "Greater reasoning depth for complex problems"),
           ("Extra high", "Extra high reasoning depth for complex problems"),
           ("More reasoning…", "Max and Ultra consume usage limits faster")]


class FakeCodexTUI(FakeTerminal):
    """Enough of Codex's composer and /model menus to drive them. Renders NULs where iTerm2 does."""

    def __init__(self, effort="Medium", error=True):
        super().__init__()
        self.effort, self.error = effort, error
        self.state, self.buf, self.sel, self.model = "composer", "", 0, "GPT-6.1-Sol"

    def _menu(self, title, items, current):
        out = [" ", f"  {title}", " "]
        for i, (label, desc) in enumerate(items):
            mark = "›" if i == self.sel else " "
            cur = " (current)" if label == current else ""
            out.append(f"{mark}\x00{i + 1}.\x00{label}{cur}\x00\x00\x00{desc}")
        return "\n".join(out + [" ", "  enter select · esc back"])

    async def screen_text(self, agent):
        if self.state == "models":
            return self._menu("Select Model and Effort", MODELS, self.model)
        if self.state == "efforts":
            return self._menu(f"Select Reasoning Level for {self.model}", EFFORTS, self.effort)
        top = "■ Selected model is at capacity. Please try a different model." if self.error else "• Done."
        popup = "› /model  choose what model and reasoning effort to use\n \n" if self.buf.startswith("/model") else ""
        word = {"Extra high": "xhigh"}.get(self.effort, self.effort.lower())
        return f"{top}\n \n{popup}› {self.buf or 'Ask Codex to do anything'}\n \n{STATUS.format(effort=word)}"

    async def send_raw(self, agent, data):
        await super().send_raw(agent, data)
        if self.state == "composer":
            if data == "\r":
                if self.buf == "/model":
                    self.state, self.sel, self.buf = "models", 0, ""
                elif self.buf:
                    self.buf, self.error = "", False
            elif data == "\x7f" * len(data):
                self.buf = self.buf[:-len(data)]
            else:
                self.buf += data
            return
        items = MODELS if self.state == "models" else EFFORTS
        if data == "\x1b[B":
            self.sel = min(self.sel + 1, len(items) - 1)
        elif data == "\x1b[A":
            self.sel = max(self.sel - 1, 0)
        elif data == "\x1b":
            self.state = "composer" if self.state == "models" else "models"
        elif data == "\r" and self.state == "models":
            self.model = MODELS[self.sel][0]
            self.state, self.sel = "efforts", [e for e, _ in EFFORTS].index(self.effort)
        elif data == "s" and self.state == "efforts":
            self.effort, self.state = EFFORTS[self.sel][0], "composer"


def make(tmp_path, tui, mode="live"):
    cfg = make_cfg(tmp_path, mode)
    cfg.timing.menu_step_s, cfg.timing.menu_timeout_s = 0, 0.1
    cfg.local_kinds = ["codex"]
    cfg.codex_rotation = ROT
    act = Actuator(cfg, tui, cfg.state_dir / "audit.log")
    r = record(tmp_path, "codex-g", None, home=tmp_path / ".codex-g")
    cfg.agents["codex-g"] = cfg.agent_config("codex-g", "codex", r.home, r.cwd, r.launch_command())
    return cfg, act, r


def audit(cfg):
    return [json.loads(ln) for ln in (cfg.state_dir / "audit.log").read_text().splitlines()]


# ---- detection ------------------------------------------------------------------------------------

def test_capacity_error_detected_when_last_and_idle():
    assert codex_screen_capacity(IDLE_ERROR).startswith("■ Selected model is at capacity")
    assert codex_screen_capacity(IDLE_ERROR.replace("at capacity", "fine")) is None


def test_capacity_error_ignored_while_working_or_after_a_new_prompt():
    working = IDLE_ERROR.replace("› Ask Codex", "• Working (3s • esc to interrupt)\n \n› Ask Codex")
    assert codex_screen_capacity(working) is None
    error = "■ Selected model is at capacity. Please try a different model."
    retried = IDLE_ERROR.replace(error, f"{error}\n\n› continue\n\n• Looking at the job state")
    assert retried != IDLE_ERROR
    assert codex_screen_capacity(retried) is None


def test_agent_reply_quoting_the_error_is_not_detected():
    reply = ("• The previous command only printed “Selected model is at capacity.” There’s no unfinished task in "
             "this conversation. What would you like me to continue?")
    assert codex_screen_capacity(IDLE_ERROR.replace("■ Selected model is at capacity. Please try a different model.",
                                                    reply)) is None


def test_marked_error_variants_detected():
    for line in ("■ Model at capacity. Try again later.", "⚠ stream error: server overloaded",
                 "  └ Selected model is at capacity. Please try a different model."):
        assert codex_screen_capacity(IDLE_ERROR.replace(
            "■ Selected model is at capacity. Please try a different model.", line)), line


def test_server_overloaded_detected():
    assert codex_screen_capacity(IDLE_ERROR.replace(
        "Selected model is at capacity. Please try a different model.", "Server overloaded; retry later."))


def test_menu_items_from_real_effort_screen():
    # As captured from Codex 0.163 through iTerm2: NULs for untouched cells, aligned columns.
    screen = ("  Select Reasoning Level for GPT-6.1-Sol\n \n \n"
              "  1. Low (default)     Fast responses with lighter reasoning\n"
              "› 2. Medium (current)  Balances speed and reasoning depth for everyday tasks\n"
              " \x003.\x00High         \x00\x00\x00\x00\x00Greater reasoning depth for complex problems\n"
              "  4. Extra high  \x00\x00\x00\x00\x00\x00Extra high reasoning depth for complex problems\n"
              " \x005.\x00More reasoning…\x00\x00\x00Max and Ultra consume usage limits faster\n \n"
              "\x00\x00enter default · s session · esc back")
    assert menu_items(screen) == [("Low", False, False), ("Medium", True, True), ("High", False, False),
                                  ("Extra high", False, False), ("More reasoning…", False, False)]


# ---- the switch -----------------------------------------------------------------------------------

async def test_switch_moves_to_next_effort_for_this_session(tmp_path):
    tui = FakeCodexTUI(effort="Medium")
    cfg, act, _ = make(tmp_path, tui)
    assert await act.codex_switch_model("codex-g", "capacity", ROT)
    assert tui.effort == "High" and tui.state == "composer" and tui.sent["codex-g"][-1] == "s"
    assert audit(cfg)[-1]["detail"] == "GPT-6.1-Sol, effort Medium -> High"


async def test_switch_wraps_from_extra_high_to_medium(tmp_path):
    tui = FakeCodexTUI(effort="Extra high")
    _, act, _ = make(tmp_path, tui)
    assert await act.codex_switch_model("codex-g", "capacity", ROT)
    assert tui.effort == "Medium"


async def test_switch_backs_out_when_composer_has_text(tmp_path):
    tui = FakeCodexTUI()
    tui.buf = "half-typed note"
    cfg, act, _ = make(tmp_path, tui)
    assert not await act.codex_switch_model("codex-g", "capacity", ROT)
    assert tui.buf == "half-typed note" and tui.state == "composer"
    assert audit(cfg)[-1]["action"] == "MODEL_SWITCH_FAILED"


async def test_switch_in_dry_mode_types_nothing(tmp_path):
    tui = FakeCodexTUI()
    cfg, act, _ = make(tmp_path, tui, mode="dry")
    assert not await act.codex_switch_model("codex-g", "capacity", ROT)
    assert "codex-g" not in tui.sent and audit(cfg)[-1]["action"] == "MODEL_SWITCH"


# ---- supervisor -----------------------------------------------------------------------------------

def supervisor(tmp_path, tui, mode="live"):
    cfg, act, r = make(tmp_path, tui, mode)
    sup = Supervisor(cfg, tui, act, FakeChannel(projects=()), FakeNotifier())
    claude = record(tmp_path, "claude-g", None, home=tmp_path / ".claude-g")
    sup.apply_discovery([r, claude])
    return cfg, sup


async def test_local_mode_manages_unregistered_codex_only(tmp_path):
    _, sup = supervisor(tmp_path, FakeCodexTUI())
    assert list(sup.agents) == ["codex-g"] and sup.agents["codex-g"].project == LOCAL_PROJECT


async def test_screen_capacity_switches_model_then_continues(tmp_path):
    tui = FakeCodexTUI(effort="High")
    cfg, sup = supervisor(tmp_path, tui)
    await sup.handle(Event(E.CAPACITY, "codex-g", {"text": "Selected model is at capacity."}, source="screen"))
    await settle(30)
    assert tui.effort == "Extra high"
    sent = tui.sent["codex-g"]
    assert sent[0] == "/model" and sent[-2:] == ["continue", "\r"]
    assert [a["action"] for a in audit(cfg)][-2:] == ["MODEL_SWITCH", "CONTINUE"]


async def test_failed_switch_types_no_continue(tmp_path):
    tui = FakeCodexTUI()
    tui.buf = "draft"
    cfg, sup = supervisor(tmp_path, tui)
    await sup.handle(Event(E.CAPACITY, "codex-g", {"text": "at capacity"}, source="rollout"))
    await settle(30)
    assert "continue" not in tui.sent["codex-g"] and sup.notifier.notes


async def test_error_gone_from_screen_cancels_retry(tmp_path):
    tui = FakeCodexTUI()
    cfg, sup = supervisor(tmp_path, tui)
    await sup.handle(Event(E.CAPACITY, "codex-g", {"text": "at capacity"}, source="screen"))
    tui.error = False  # the owner already dealt with it
    await settle(30)
    assert "codex-g" not in tui.sent



def test_rollout_events_carry_the_line_timestamp():
    from agent_watcher.detect import from_codex_main_line
    line = json.dumps({"timestamp": "2026-10-18T20:46:16.500Z", "type": "event_msg",
                       "payload": {"type": "task_started"}})
    [ev] = from_codex_main_line(line, "codex-g")
    assert ev.kind is E.WORKING and ev.ts == 1792356376.5


async def test_local_agent_blocks_are_not_tracked(tmp_path):
    tui = FakeCodexTUI(error=False)
    cfg, sup = supervisor(tmp_path, tui)
    await sup.handle(Event(E.BLOCKED, "codex-g", {"tool_name": "Bash", "tool_input": {"command": "rm -rf build"},
                                                  "reason": "risky", "reviewer": "codex_auto_review"}))
    await settle(10)
    assert not sup.pending and not sup.notifier.notes and "codex-g" not in tui.sent



# ---- turn errors recorded in the rollout's task_complete ------------------------------------------

def task_complete(info, message, ts="2026-10-19T14:27:59.000Z"):
    return json.dumps({"timestamp": ts, "type": "event_msg", "payload": {
        "type": "task_complete", "last_agent_message": None,
        "error": {"message": message, "codex_error_info": info}}})


def test_task_complete_errors_are_classified():
    [cyber] = from_codex_main_line(task_complete(
        "cyber_policy", "This content was flagged for possible cybersecurity risk. If this seems wrong, try "
                        "rephrasing your request."), "codex-g")
    assert cyber.kind is E.SAFETY_FLAG and cyber.data["error"] == "cyber_policy" and cyber.source == "rollout"
    [cap] = from_codex_main_line(task_complete(
        "server_overloaded", "Selected model is at capacity. Please try a different model."), "codex-g")
    assert cap.kind is E.CAPACITY
    [other] = from_codex_main_line(task_complete(
        "other", "Guardian needs compaction; restart from the parent checkpoint"), "codex-g")
    assert other.kind is E.IDLE and other.data["error"] == "other"


async def test_failed_turn_is_not_a_good_turn(tmp_path):
    cfg, sup = supervisor(tmp_path, FakeCodexTUI(error=False))
    st = sup.agents["codex-g"]
    st.capacity_tries = 2
    [ev] = from_codex_main_line(task_complete("other", "Guardian needs compaction"), "codex-g")
    await sup.handle(ev)
    assert st.capacity_tries == 2


CYBER = ModelRotation("Daybreak Blue", [], "s")  # keep the session's effort


def test_status_line_effort():
    from agent_watcher.detect import codex_status_effort
    assert codex_status_effort(IDLE_ERROR) == "Medium"
    assert codex_status_effort(STATUS.format(effort="xhigh")) == "Extra high"
    assert codex_status_effort("› Ask Codex\n  GPT-6.1-Sol default · ~/code/x · Context 0% used") is None


async def test_cyber_block_switches_to_daybreak_once(tmp_path):
    tui = FakeCodexTUI(effort="High", error=False)
    cfg, sup = supervisor(tmp_path, tui)
    cfg.codex_cyber = CYBER
    [ev] = from_codex_main_line(task_complete("cyber_policy", "flagged for possible cybersecurity risk"), "codex-g")
    await sup.handle(ev)
    await settle(30)
    assert tui.model == "Daybreak Blue" and tui.effort == "High"
    assert TEXT[T.CONTINUE_CYBER] in tui.lines("codex-g")
    assert "switched to Daybreak Blue" in sup.notifier.notes[-1]

    sent = len(tui.sent["codex-g"])  # blocked again with no good turn in between: leave it to the owner
    [ev] = from_codex_main_line(task_complete("cyber_policy", "flagged", ts="2026-10-19T14:40:00.000Z"), "codex-g")
    await sup.handle(ev)
    await settle(30)
    assert len(tui.sent["codex-g"]) == sent and "again after switching" in sup.notifier.notes[-1]


def task_done(ts):
    return json.dumps({"timestamp": ts, "type": "event_msg",
                       "payload": {"type": "task_complete", "last_agent_message": "Done."}})


async def test_cyber_switch_goes_back_after_a_good_turn(tmp_path):
    tui = FakeCodexTUI(effort="High", error=False)
    cfg, sup = supervisor(tmp_path, tui)
    cfg.codex_cyber = CYBER
    [ev] = from_codex_main_line(task_complete("cyber_policy", "flagged for possible cybersecurity risk"), "codex-g")
    await sup.handle(ev)
    await settle(30)
    assert tui.model == "Daybreak Blue" and sup.cyber_return == {"codex-g": "GPT-6.1-Sol"}
    assert "goes back to GPT-6.1-Sol" in sup.notifier.notes[-1]

    [ok] = from_codex_main_line(task_done("2026-10-19T14:35:00.000Z"), "codex-g")
    await sup.handle(ok)
    await settle(30)
    assert tui.model == "GPT-6.1-Sol" and tui.effort == "High" and not sup.cyber_return
    assert "Switched back to GPT-6.1-Sol" in sup.notifier.notes[-1]
    assert [a["detail"] for a in audit(cfg) if a["action"] == "MODEL_SWITCH"][-1] == \
        "GPT-6.1-Sol, effort High -> High"


async def test_cyber_block_in_a_project_switches_and_posts(tmp_path):
    from .conftest import P1
    tui = FakeCodexTUI(effort="Medium", error=False)
    cfg, act, _ = make(tmp_path, tui)
    cfg.codex_cyber = CYBER
    sup = Supervisor(cfg, tui, act, FakeChannel(), FakeNotifier())
    sup.apply_discovery([record(tmp_path, "codex-g", P1, home=tmp_path / ".codex-g")])
    assert sup.agents["codex-g"].project == P1
    [ev] = from_codex_main_line(task_complete("cyber_policy", "flagged for possible cybersecurity risk"), "codex-g")
    await sup.handle(ev)
    await settle(30)
    assert tui.model == "Daybreak Blue" and TEXT[T.CONTINUE_CYBER] in tui.lines("codex-g")
    posts = [text for _, text, _ in sup.channel.posts]
    assert any("turn refused by a safety review" in p and "Switching it to Daybreak Blue and retrying the turn in "
               "the next 1 seconds, so don't reassign its work." in p for p in posts)
    assert any("switched to Daybreak Blue" in p and "goes back to GPT-6.1-Sol" in p for p in posts)
    assert not sup.notifier.notes


async def test_cyber_block_without_a_configured_model_notifies(tmp_path):
    tui = FakeCodexTUI(error=False)
    cfg, sup = supervisor(tmp_path, tui)
    [ev] = from_codex_main_line(task_complete("cyber_policy", "flagged"), "codex-g")
    await sup.handle(ev)
    await settle(10)
    assert "codex-g" not in tui.sent and "It needs you" in sup.notifier.notes[-1]
