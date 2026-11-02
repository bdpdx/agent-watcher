import sys

from agent_watcher import actuator
from agent_watcher.actuator import CommandNotifier, MacNotifier, make_notifier
from agent_watcher.config import load


async def test_command_gets_title_and_message_as_its_last_arguments(tmp_path):
    script, out = tmp_path / "n.sh", tmp_path / "out"
    script.write_text(f'#!/bin/sh\nprintf "%s|" "$@" > {out}\n')
    script.chmod(0o755)
    await CommandNotifier([str(script), "--flag"]).notify("title", 'a "b" $(touch x); c')
    assert out.read_text() == '--flag|title|a "b" $(touch x); c|'


async def test_missing_command_only_logs(caplog):
    await CommandNotifier(["/nonexistent/notify"]).notify("t", "m")
    assert "notify: t: m" in caplog.text and "failed" in caplog.text


async def test_hung_command_is_killed(caplog):
    await CommandNotifier(["sh", "-c", "sleep 30"], timeout_s=0.05).notify("t", "m")
    assert "timed out" in caplog.text


def test_notifier_choice(monkeypatch):
    assert make_notifier(["x"]).argv == ["x"]
    monkeypatch.setattr(sys, "platform", "darwin")
    assert isinstance(make_notifier([]), MacNotifier)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(actuator.shutil, "which", lambda name: None)
    assert make_notifier([]).argv == []
    monkeypatch.setattr(actuator.shutil, "which", lambda name: "/usr/bin/notify-send")
    assert make_notifier([]).argv == ["notify-send", "--"]


def test_notify_command_from_config(tmp_path):
    f = tmp_path / "w.toml"
    f.write_text('project_state_dir = "/x"\nowner_slack_id = "U"\n[notify]\ncommand = ["a", "b"]\n')
    assert load(f).notify_command == ["a", "b"]
