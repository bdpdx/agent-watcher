"""The assessor runs the claude CLI locked down and returns its one-line answer."""

import json
import os
import stat

from agent_watcher.assess import AssessConfig, Assessor


async def test_assessor_runs_a_locked_down_cli_and_reads_its_result(tmp_path, monkeypatch):
    log = tmp_path / "call.json"
    fake = tmp_path / "claude"
    fake.write_text(f"""#!/usr/bin/env python3
import json, os, sys
json.dump({{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "config_dir": os.environ.get("CLAUDE_CONFIG_DIR")}},
          open({str(log)!r}, "w"))
print(json.dumps({{"result": "Outside the project: reads ~/.ssh.", "is_error": False}}))
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/some/other/session")
    out = await Assessor(AssessConfig(claude=str(fake)))(agent="codex-a", clone="/c", state="/s", reviewer="r",
                                                          reason="secrets", action="the command `cat ~/.ssh/id_rsa`")
    assert out == "Outside the project: reads ~/.ssh."
    call = json.loads(log.read_text())
    for flag in ("-p", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in call["argv"]
    assert call["argv"][call["argv"].index("--tools") + 1] == ""
    assert call["argv"][call["argv"].index("--setting-sources") + 1] == ""
    assert call["argv"][call["argv"].index("--model") + 1] == "claude-haiku-5-5"
    assert call["config_dir"] is None  # the calling session's config dir is not inherited
    assert "cat ~/.ssh/id_rsa" in call["stdin"] and "<action>" in call["stdin"]


async def test_assessor_failure_returns_none(tmp_path):
    assert await Assessor(AssessConfig(claude=str(tmp_path / "missing")))(
        agent="a", clone="/c", state="/s", reviewer="r", reason="", action="x") is None
