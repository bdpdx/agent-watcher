"""Submitting an authorization mid-turn: the prompt box is read, a quiet draft is moved aside and put back."""

from agent_watcher.actuator import CLAUDE_STASH, CTRL_E, Actuator
from agent_watcher.detect import composer_draft
from agent_watcher.templates import TEXT, T

from .conftest import FakeTerminal, make_cfg, record

RULE = "─" * 60


class FakePromptBox(FakeTerminal):
    """A Claude or Codex prompt box that keeps a draft, stashes it (Claude ctrl+s), and submits on Enter."""

    def __init__(self, kind, draft="", working=True, overlay=None):
        super().__init__()
        self.kind, self.draft, self.working, self.overlay = kind, draft, working, overlay
        self.stash, self.submitted = None, []

    async def screen_text(self, agent):
        if self.overlay:
            return self.overlay
        if self.kind == "claude":
            spin = "✢ Roosting… (2m 5s · ↓ 3.1k tokens)" if self.working else "✻ Crunched for 4s"
            lines = self.draft.split("\n")
            box = [f"❯\xa0{lines[0]}"] + [f"  {ln}" for ln in lines[1:]]
            return "\n".join(["⏺ working on it", spin, RULE, *box, RULE, "  ⏵⏵ auto mode on"])
        top = "• Working (3m 10s • esc to interrupt)" if self.working else "• Done."
        shown = self.draft or "Ask Codex to do anything"
        return f"{top}\n\n› {shown}\n\n  GPT-6.1-Sol medium · ~/code/x · Context 40% used · Main [default]\n  ? for shortcuts"

    async def send_raw(self, agent, data):
        await super().send_raw(agent, data)
        if data == CLAUDE_STASH:
            self.stash, self.draft = self.draft, ""
        elif data == "\r":
            self.submitted.append(self.draft)
            self.draft = ""
            if self.stash is not None:
                self.draft, self.stash = self.stash, None
        elif data == CTRL_E:
            pass
        elif data and set(data) == {"\x7f"}:
            self.draft = self.draft[:-len(data)]
        else:
            self.draft += data


def make(tmp_path, tui, kind):
    cfg = make_cfg(tmp_path)
    cfg.timing.draft_watch_s, cfg.timing.menu_timeout_s, cfg.timing.menu_step_s = 0.1, 0.2, 0
    name = f"{kind}-x"
    r = record(tmp_path, name, None, kind=kind)
    cfg.agents[name] = cfg.agent_config(name, kind, r.home, r.cwd, r.launch_command())
    return Actuator(cfg, tui, cfg.state_dir / "audit.log"), name


AUTH = [TEXT[T.AUTH].format(actions="the command `git push`")]


async def test_empty_box_mid_turn_submits_at_once(tmp_path):
    for kind in ("claude", "codex"):
        tui = FakePromptBox(kind)
        act, name = make(tmp_path / kind, tui, kind)
        assert await act.type_now(name, "approvals_block", T.AUTH, ["the command `git push`"])
        assert tui.submitted == AUTH and tui.draft == ""


async def test_claude_draft_is_stashed_and_restored(tmp_path):
    tui = FakePromptBox("claude", draft="check the\nrun 4 timings")
    act, name = make(tmp_path, tui, "claude")
    assert await act.type_now(name, "approvals_block", T.AUTH, ["the command `git push`"])
    assert tui.submitted == AUTH and tui.draft == "check the\nrun 4 timings"
    assert tui.sent[name][0] == CLAUDE_STASH


async def test_codex_draft_is_moved_aside_and_retyped_unsubmitted(tmp_path):
    tui = FakePromptBox("codex", draft="look at PR 686 next")
    act, name = make(tmp_path, tui, "codex")
    assert await act.type_now(name, "approvals_block", T.AUTH, ["the command `git push`"])
    assert tui.submitted == AUTH and tui.draft == "look at PR 686 next"
    assert tui.sent[name][-1] == "look at PR 686 next"  # typed back, no Enter after it


async def test_codex_multiline_draft_waits(tmp_path):
    tui = FakePromptBox("codex", draft="first line\nsecond line")
    act, name = make(tmp_path, tui, "codex")
    assert not await act.type_now(name, "approvals_block", T.AUTH, ["the command `git push`"])
    assert name not in tui.sent


async def test_someone_typing_is_waited_out(tmp_path):
    tui = FakePromptBox("claude", draft="half a thou")
    act, name = make(tmp_path, tui, "claude")
    act.cfg.timing.owner_typing_grace_s = 0.05
    act.last_owner_key[name] = act.now() + 0.05  # a keystroke lands during the watch
    assert await act.type_now(name, "approvals_block", T.AUTH, ["the command `git push`"])
    assert tui.draft == "half a thou" and tui.submitted == AUTH


async def test_no_prompt_box_or_a_dialog_types_nothing(tmp_path):
    dialog = "Bash command\n  rm -rf build\n Do you want to proceed?\n ❯ 1. Yes\n   2. No"
    for overlay in (dialog, "some full-screen menu"):
        tui = FakePromptBox("claude", overlay=overlay)
        act, name = make(tmp_path, tui, "claude")
        assert not await act.type_now(name, "approvals_block", T.AUTH, ["the command `git push`"])
        assert name not in tui.sent


def test_composer_draft_reads_real_screens():
    claude = "✻ Crunched for 4s\n" + RULE + "\n❯\xa0go ahead and merge 686\n" + RULE + "\n  host:/x\n  ⏵⏵ auto mode on"
    assert composer_draft(claude, "claude") == "go ahead and merge 686"
    assert composer_draft(claude.replace("go ahead and merge 686", ""), "claude") == ""
    for hint in ('Try "write a test for app.py"', "Press up to edit queued messages, Enter to send them immediately"):
        assert composer_draft(claude.replace("go ahead and merge 686", hint), "claude") == ""
    codex = ("› earlier prompt\n• Working (3h 17m 51s • esc to interrupt)\n  └ Tip: Use /init\n"
             "› Ask Codex to do anything\n\x00\x00GPT-6.1-Sol medium · ~/code/x · Context 45% used · Main\n? for shortcuts")
    assert composer_draft(codex, "codex") == ""
    assert composer_draft(codex.replace("Ask Codex to do anything", "fix it"), "codex") == "fix it"
    assert composer_draft("Select Model and Effort\n› 1. GPT-6.1-Sol", "codex") is None
