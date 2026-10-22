import json
from pathlib import Path

from agent_watcher.discovery import Discovery, transcript_dir

HOME = str(Path.home())


def runner(procs):
    """procs: pid -> dict(name, tty, args, env, cwd)."""
    def run(cmd):
        if cmd[0] == "pgrep":
            return "\n".join(str(p) for p, d in procs.items() if d["name"] == cmd[-1])
        pid = int(cmd[cmd.index("-p") + 1])
        d = procs[pid]
        if cmd[0] == "lsof":
            return f"p{pid}\nfcwd\nn{d['cwd']}\n"
        if cmd[-1] == "tty=":
            return d.get("tty", "ttys001") + "\n"
        if cmd[1] == "-wwE":
            return d["args"] + " " + " ".join(f"{k}={v}" for k, v in d["env"].items()) + "\n"
        return d["args"] + "\n"
    return run


def write_state(home: Path, regs):
    d = home / "slack-agent-chat"
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({"cohort": {
        f"{r['session_id']}|{r['project']}": r for r in regs}}))


def test_claude_default_home_session_and_registration(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".claude"
    cwd = tmp_path / "code" / "proj-claude"
    tdir = transcript_dir(home, cwd)
    tdir.mkdir(parents=True)
    (tdir / "old.jsonl").write_text("{}\n")
    (tdir / "9c54.jsonl").write_text("{}\n")
    write_state(home, [{"project": "p1", "agent": "claude-a", "session_id": "9c54"},
                       {"project": "p0", "agent": "claude-a", "session_id": "old-session"}])
    procs = {10: {"name": "claude", "args": "claude", "cwd": cwd,
                  "env": {"ITERM_SESSION_ID": "w0t0p0:GUID-A", "TERM": "xterm"}}}
    recs = Discovery(runner(procs)).scan()
    assert len(recs) == 1
    r = recs[0]
    assert r.home == home and r.iterm_guid == "GUID-A" and r.cwd == cwd
    assert [(g.agent, g.project) for g in r.regs] == [("claude-a", "p1")]  # stale registration ignored
    assert "CLAUDE_CONFIG_DIR" not in r.launch_command()


def test_hook_session_wins_over_newest_transcript(tmp_path):
    home = tmp_path / ".claude-g"
    cwd = tmp_path / "c"
    procs = {10: {"name": "claude", "args": "claude", "cwd": cwd,
                  "env": {"CLAUDE_CONFIG_DIR": str(home), "ITERM_SESSION_ID": "w0t1p0:G"}}}
    write_state(home, [{"project": "p1", "agent": "claude-g", "session_id": "from-hook"}])
    d = Discovery(runner(procs))
    d.hook_sessions[home] = "from-hook"
    assert d.scan()[0].regs[0].agent == "claude-g"


def test_codex_session_from_main_rollout_and_launch(tmp_path):
    import datetime as dt
    home = tmp_path / ".codex-a1"
    cwd = tmp_path / "code" / "proj-codex-a1"
    day = dt.date.today()
    rd = home / "sessions" / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    rd.mkdir(parents=True)
    (rd / "rollout-a.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "T-MAIN", "thread_source": "user", "cwd": str(cwd)}}) + "\n")
    (rd / "rollout-b.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {
        "id": "T-GUARD", "thread_source": "guardian_review", "cwd": str(cwd)}}) + "\n")
    write_state(home, [{"project": "p1", "agent": "codex-a1", "session_id": "T-MAIN"}])
    procs = {
        20: {"name": "codex", "args": f"codex --remote unix:// -C {cwd} resume T-MAIN", "cwd": cwd,
             "env": {"CODEX_HOME": str(home), "ITERM_SESSION_ID": "w1t0p0:G2"}},
        21: {"name": "codex", "tty": "??", "args": "codex app-server daemon", "cwd": "/",
             "env": {"CODEX_HOME": str(home), "ITERM_SESSION_ID": "w1t0p0:G2"}},
        22: {"name": "codex", "args": "codex app-server", "cwd": "/", "env": {"CODEX_HOME": str(home)}},
    }
    recs = Discovery(runner(procs)).scan()
    assert len(recs) == 1 and recs[0].session_id == "T-MAIN" and recs[0].regs[0].agent == "codex-a1"
    launch = recs[0].launch_command()
    assert f"CODEX_HOME={home}" in launch and "--remote unix:// -C" in launch and "resume" not in launch
    assert launch.endswith("{prompt}")


def test_unregistered_session_has_no_regs(tmp_path):
    home = tmp_path / ".codex-x"
    procs = {30: {"name": "codex", "args": "codex", "cwd": tmp_path,
                  "env": {"CODEX_HOME": str(home), "ITERM_SESSION_ID": "w0t3p0:G3"}}}
    recs = Discovery(runner(procs)).scan()
    assert recs[0].regs == [] and recs[0].session_id is None
