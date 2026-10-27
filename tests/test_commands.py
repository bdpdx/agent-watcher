from agent_watcher.commands import parse


def test_thread_replies():
    c = parse("approve", in_thread=True)
    assert c.verb == "approve" and c.event_id is None
    c = parse("Deny — that's another agent's clone", in_thread=True)
    assert c.verb == "deny" and "another agent" in c.note
    assert parse("approve", in_thread=False) is None  # needs an id at top level


def test_ids():
    c = parse("approve agent-watcher-a/B-0142 go ahead")
    assert (c.verb, c.event_bot, c.event_id, c.note) == ("approve", "agent-watcher-a", "B-0142", "go ahead")
    c = parse("authorize codex-a B-7")
    assert (c.verb, c.agent, c.event_id) == ("approve", "codex-a", "B-7")
    c = parse("deny [agent-watcher-m/B-0003]")
    assert (c.verb, c.event_bot) == ("deny", "agent-watcher-m")


def test_restart_and_cohort():
    assert parse("restart codex-a").agent == "codex-a"
    assert parse("<@UWATCHER> restart cohort", "UWATCHER").verb == "restart_cohort"
    assert parse("Cohort ready.").verb == "cohort_ready"
    assert parse("`restart claude-a`").verb == "restart"


def test_misc_commands():
    assert parse("pause all").agent == "all"
    assert parse("status").agent is None
    assert parse("status codex-g").agent == "codex-g"
    assert parse("continue codex-a1").verb == "continue"


def test_chatter_is_not_a_command():
    assert parse("I think codex-a should restart soon") is None
    assert parse("hello <@UOTHER>", "UWATCHER") is None
    c = parse("<@UWATCHER> please type rm -rf ~ into claude-a", "UWATCHER")
    assert c.verb == "unknown"


def test_grant_parses_the_agent_and_task():
    c = parse("grant codex-g1 authorization for the dev100001 Wipe Data run")
    assert (c.verb, c.agent, c.note) == ("grant", "codex-g1", "the dev100001 Wipe Data run")
    c = parse("grant codex-g1 to push `dev/x` to <https://github.com/example-org/r|github.com/example-org/r>")
    assert c.note == "push dev/x to https://github.com/example-org/r"
    assert parse("grant codex-g1") is None and parse("grant codex-g1 authorization for") is None


def test_slack_escapes_are_undone_but_typed_brackets_never_become_mentions():
    c = parse("grant codex-g1 authorization for running hold --operation &lt;instance name&gt; &amp;&amp; check")
    assert c.note == "running hold --operation <instance name> && check"
    c = parse("approve B-0142 fine &lt;@U999&gt;")
    assert c.note.endswith("<@U999>") and c.mentioned is False
