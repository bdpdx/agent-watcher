import datetime as dt
import json

from agent_watcher.events import E
from agent_watcher.tails import RolloutTailer


def _write(path, *objs):
    with path.open("a") as f:
        for o in objs:
            f.write(json.dumps(o) + "\n")


async def test_tailer_follows_main_and_guardian_threads(cfg):
    agent = cfg.agents["codex-a"]
    day = dt.date.today()
    d = agent.config_dir / "sessions" / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    d.mkdir(parents=True)
    got = []

    async def emit(ev):
        got.append(ev)

    t = RolloutTailer(agent, emit)
    await t.scan_once()  # first scan: nothing yet

    main = d / "rollout-1-main.jsonl"
    _write(main, {"type": "session_meta", "payload": {"id": "M1", "thread_source": "user", "cwd": str(agent.clone)}})
    other = d / "rollout-2-other.jsonl"
    _write(other, {"type": "session_meta", "payload": {"id": "O1", "thread_source": "user", "cwd": "/elsewhere"}})
    await t.scan_once()
    _write(main, {"type": "event_msg", "payload": {"type": "error", "message": "Selected model is at capacity.",
                                                    "codex_error_info": "server_overloaded"}})
    _write(other, {"type": "event_msg", "payload": {"type": "error", "message": "at capacity"}})
    guardian = d / "rollout-3-guardian.jsonl"
    _write(guardian,
           {"type": "session_meta", "payload": {"id": "G1", "thread_source": "guardian_review", "parent_thread_id": "M1"}},
           {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
               {"text": "Planned action JSON:"},
               {"text": json.dumps({"tool": "exec_command", "command": ["bash", "-lc", "git push -f"], "cwd": str(agent.clone)})}]}},
           {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
               {"text": json.dumps({"outcome": "deny", "rationale": "force push"})}]}})
    await t.scan_once()
    got = [e for e in got if not e.data.get("baseline")]
    kinds = [e.kind for e in got]
    assert kinds == [E.CAPACITY, E.BLOCKED]
    assert got[1].data["tool_input"]["command"] == ["bash", "-lc", "git push -f"]


def _denial(command):
    return ({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"text": "Planned action JSON:"},
                {"text": json.dumps({"tool": "exec_command", "command": ["bash", "-lc", command]})}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
                {"text": json.dumps({"outcome": "deny", "rationale": "risky"})}]}})


async def test_old_guardian_denials_are_not_replayed_at_startup(cfg):
    """A review file older than its main thread is seen before its parent is known; it must not replay."""
    import os
    agent = cfg.agents["codex-a"]
    day = dt.date.today()
    d = agent.config_dir / "sessions" / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    d.mkdir(parents=True)
    guardian = d / "rollout-1-guardian.jsonl"
    _write(guardian, {"type": "session_meta", "payload": {"id": "G1", "thread_source": "guardian_review",
                                                          "parent_thread_id": "M1"}}, *_denial("git push -f"))
    main = d / "rollout-2-main.jsonl"
    _write(main, {"type": "session_meta", "payload": {"id": "M1", "thread_source": "user", "cwd": str(agent.clone)}})
    now = dt.datetime.now().timestamp()
    os.utime(guardian, (now - 60, now - 60))  # older than the main thread, so scanned first
    got = []

    async def emit(ev):
        got.append(ev)

    t = RolloutTailer(agent, emit)
    await t.scan_once()
    await t.scan_once()
    assert not [e for e in got if e.kind is E.BLOCKED]
    _write(guardian, *_denial("rm -rf build"))  # a new review in the same file still counts
    await t.scan_once()
    [ev] = [e for e in got if e.kind is E.BLOCKED]
    assert ev.data["tool_input"]["command"] == ["bash", "-lc", "rm -rf build"]


async def test_reviews_of_subagent_actions_are_followed(cfg):
    """A subagent's actions are reviewed in guardian threads whose parent is the subagent, not the main thread."""
    agent = cfg.agents["codex-a"]
    day = dt.date.today()
    d = agent.config_dir / "sessions" / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    d.mkdir(parents=True)
    got = []

    async def emit(ev):
        got.append(ev)

    t = RolloutTailer(agent, emit)
    _write(d / "rollout-1-main.jsonl",
           {"type": "session_meta", "payload": {"id": "M1", "thread_source": "user", "cwd": str(agent.clone)}})
    await t.scan_once()
    _write(d / "rollout-2-sub.jsonl", {"type": "session_meta", "payload": {
        "id": "S1", "thread_source": "subagent", "parent_thread_id": "M1"}})
    _write(d / "rollout-3-subsub.jsonl", {"type": "session_meta", "payload": {
        "id": "S2", "thread_source": "subagent", "parent_thread_id": "S1"}})
    _write(d / "rollout-4-guardian.jsonl", {"type": "session_meta", "payload": {
        "id": "G1", "thread_source": "guardian_review", "parent_thread_id": "S2"}}, *_denial("wipe dev100001"))
    _write(d / "rollout-5-other.jsonl", {"type": "session_meta", "payload": {
        "id": "G2", "thread_source": "guardian_review", "parent_thread_id": "SOMEONE-ELSE"}}, *_denial("nope"))
    await t.scan_once()
    await t.scan_once()
    [ev] = [e for e in got if e.kind is E.BLOCKED]
    assert ev.data["tool_input"]["command"] == ["bash", "-lc", "wipe dev100001"]


async def test_codex_baseline_counts_existing_compactions(cfg):
    agent = cfg.agents["codex-a"]
    day = dt.date.today()
    d = agent.config_dir / "sessions" / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    d.mkdir(parents=True)
    main = d / "rollout-1-main.jsonl"
    _write(main, {"type": "session_meta", "timestamp": "2026-10-14T09:12:00Z",
                  "payload": {"id": "M1", "thread_source": "user", "cwd": str(agent.clone),
                              "timestamp": "2026-10-14T09:12:00Z"}},
           {"type": "compacted", "payload": {"window_number": 1}},
           {"type": "compacted", "payload": {"window_number": 2}},
           {"type": "event_msg", "payload": {"type": "token_count", "info": {
               "last_token_usage": {"input_tokens": 300000}, "model_context_window": 475000}}})
    got = []

    async def emit(ev):
        got.append(ev)

    t = RolloutTailer(agent, emit)
    await t.scan_once()
    assert got[0].kind is E.COMPACTION and got[0].data["count"] == 2 and got[0].data["baseline"]
    assert got[1].kind is E.CONTEXT and got[1].data["tokens"] == 300000
    _write(main, {"type": "compacted", "payload": {"window_number": 3}})
    await t.scan_once()
    assert got[-1].kind is E.COMPACTION and got[-1].data["count"] == 3 and not got[-1].data.get("baseline")


async def test_claude_transcript_tailer(cfg, tmp_path):
    from agent_watcher.tails import ClaudeTranscriptTailer
    path = tmp_path / "t.jsonl"
    _write(path, {"type": "user", "timestamp": "2026-10-14T09:00:00Z"},
           {"type": "system", "subtype": "compact_boundary", "compactMetadata": {"preTokens": 9, "postTokens": 1}})
    got = []

    async def emit(ev):
        got.append(ev)

    t = ClaudeTranscriptTailer(cfg, emit)
    await t.follow("claude-a", str(path))
    assert got[0].data["count"] == 1 and got[0].data["baseline"]
    _write(path, {"type": "system", "subtype": "compact_boundary", "compactMetadata": {"preTokens": 900000,
                                                                                         "postTokens": 20000}},
           {"type": "assistant", "message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 30000}}})
    await t.scan_once()
    assert got[-2].data["count"] == 2 and got[-1].kind is E.CONTEXT and got[-1].data["tokens"] == 30005
