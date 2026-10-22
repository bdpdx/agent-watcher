import datetime as dt
import json

import pytest

from agent_watcher.detect import (GuardianReview, claude_compaction, claude_context_tokens, from_codex_main_line,
                                  from_hook, reset_time)
from agent_watcher.events import E
from agent_watcher.templates import T, describe_action, launch_command, render


def test_auth_renders_exact_wording():
    text = render(T.AUTH, ["the command `git push origin fix/x`"])
    assert text == ("You are authorized to perform the command `git push origin fix/x`. "
                    "If the action still needs to be performed retry it.")


def test_describe_action_strips_control_chars_and_backticks():
    d = describe_action("Bash", {"command": "git status\n\x1b[2J`rm -rf /`\r"})
    assert "\n" not in d and "\x1b" not in d and "\r" not in d
    assert d.count("`") == 2  # only our own quoting


def test_describe_codex_bash_lc_unwraps_script():
    d = describe_action("Bash", {"command": ["/opt/homebrew/bin/bash", "-lc", "git fetch origin main"]})
    assert d == "the command `git fetch origin main`"


def test_templates_reject_params_and_bad_project():
    with pytest.raises(ValueError):
        render(T.CONTINUE, ["anything"])
    with pytest.raises(ValueError):
        launch_command("claude {prompt}", "x; rm -rf ~")
    assert launch_command("claude {prompt}", "2026-10-06-ui-fixes") == "claude 'join project 2026-10-06-ui-fixes'"


def test_stop_failure_classification():
    ev = from_hook("StopFailure", {"error": "overloaded", "error_details": "API Error: 529 Overloaded"}, "a", "claude")[0]
    assert ev.kind is E.CAPACITY
    ev = from_hook("StopFailure", {"error": "unknown",
                                   "last_assistant_message": "API Error: Opus's safeguards flagged this session"},
                   "a", "claude")[0]
    assert ev.kind is E.SESSION_REFUSAL
    ev = from_hook("StopFailure", {"error": "invalid_request",
                                   "last_assistant_message": "API Error: Opus's safeguards flagged this message"},
                   "a", "claude")[0]
    assert ev.kind is E.SAFETY_FLAG
    ev = from_hook("StopFailure", {"error": "rate_limit",
                                   "last_assistant_message": "You've hit your session limit · resets 3:45pm"},
                   "a", "claude")[0]
    assert ev.kind is E.RATE_LIMIT and ev.data["resume_at"]


def test_reset_time_rolls_to_tomorrow():
    now = dt.datetime(2026, 10, 14, 16, 0).astimezone()
    t = reset_time("resets 3:45pm", now)
    assert dt.datetime.fromtimestamp(t).astimezone().day == 15


def test_permission_denied_and_dialog_hooks():
    p = {"tool_name": "Bash", "tool_input": {"command": "git push -f"}, "reason": "[Force Push]", "cwd": "/x"}
    assert from_hook("PermissionDenied", p, "a", "claude")[0].kind is E.BLOCKED
    assert from_hook("PermissionRequest", p, "a", "claude")[0].kind is E.DIALOG
    assert from_hook("PermissionRequest", p, "a", "codex") == []


def test_codex_rollout_errors_and_idle():
    line = json.dumps({"type": "event_msg", "payload": {
        "type": "error", "message": "Selected model is at capacity. Please try a different model.",
        "codex_error_info": "server_overloaded"}})
    assert from_codex_main_line(line, "c")[0].kind is E.CAPACITY
    assert from_codex_main_line(json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}),
                                "c")[0].kind is E.IDLE


def test_guardian_denial_from_real_shape():
    g = GuardianReview("parent")
    action = {"command": ["/opt/homebrew/bin/bash", "-lc", "git push --force origin main"],
              "cwd": "/repo", "tool": "exec_command", "sandbox_permissions": "require_escalated"}
    user = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "Planned action JSON:\n"},
        {"type": "input_text", "text": json.dumps(action)}]}}
    verdict = {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": json.dumps({"risk_level": "high", "outcome": "deny", "rationale": "force push"})}]}}
    assert g.feed(json.dumps(user), "c") == []
    ev = g.feed(json.dumps(verdict), "c")[0]
    assert ev.kind is E.BLOCKED and ev.data["tool_name"] == "Bash" and ev.data["cwd"] == "/repo"
    allow = json.loads(json.dumps(verdict))
    allow["payload"]["content"][0]["text"] = json.dumps({"outcome": "allow"})
    g.feed(json.dumps(user), "c")
    assert g.feed(json.dumps(allow), "c") == []


def test_codex_compaction_and_context_lines():
    comp = json.dumps({"type": "compacted", "payload": {"window_number": 3, "latest_token_usage_record": {
        "usage": {"input_tokens": 219021}}}})
    ev = from_codex_main_line(comp, "c")[0]
    assert ev.kind is E.COMPACTION and ev.data["count"] == 3 and ev.data["pre"] == 219021
    tok = json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": {"input_tokens": 26950}, "model_context_window": 475000}}})
    ev = from_codex_main_line(tok, "c")[0]
    assert ev.kind is E.CONTEXT and ev.data == {"tokens": 26950, "window": 475000}


def test_claude_transcript_parsing():
    boundary = {"type": "system", "subtype": "compact_boundary",
                "compactMetadata": {"trigger": "auto", "preTokens": 968156, "postTokens": 24924}}
    assert claude_compaction(boundary)["post"] == 24924
    assert claude_compaction({**boundary, "isSidechain": True}) is None
    asst = {"type": "assistant", "message": {"usage": {"input_tokens": 2, "cache_creation_input_tokens": 23910,
                                                      "cache_read_input_tokens": 26168}}}
    assert claude_context_tokens(asst) == 50080


def test_chat_ack_steps_are_left_out_of_the_described_command():
    ack = "/usr/local/bin/slack-mcp-server chat ack C0000000001 1792536623.352299 >/dev/null; "
    cmd = (ack + ack.replace("C0000000001", "C0000000002") + "cd /x && git worktree remove /x-wt && "
           "git branch -D b1 && git push -q origin --delete b1")
    assert describe_action("Bash", {"command": cmd}) == \
        "the command `cd /x && git worktree remove /x-wt && git branch -D b1 && git push -q origin --delete b1`"
    assert describe_action("Bash", {"command": "slack-mcp-server chat ack C1 1.2 && git push"}) == \
        "the command `git push`"
    only = "/usr/local/bin/slack-mcp-server chat ack C1 1.2 >/dev/null"
    assert describe_action("Bash", {"command": only}) == f"the command `{only}`"
    assert describe_action("Bash", {"command": "echo hi; slack-mcp-server chat ack C1 1.2; rm -rf x"}) == \
        "the command `echo hi; rm -rf x`"


def test_line_breaks_are_shown_as_command_separators():
    cmd = "git push -u origin feat\n gh repo view --json isPrivate\ncd x &&\n  make \\\n  test | \n tail -2"
    assert describe_action("Bash", {"command": ["/opt/homebrew/bin/bash", "-lc", cmd]}) == \
        "the command `git push -u origin feat; gh repo view --json isPrivate; cd x && make test | tail -2`"
