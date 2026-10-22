from agent_watcher.policy import Policy, Verdict


def ev(cfg, agent, command, cwd=None):
    a = cfg.agents[agent]
    return Policy(cfg).evaluate(a, "Bash", {"command": command}, str(cwd or a.clone))


def test_git_in_own_clone_is_approved(cfg):
    assert ev(cfg, "claude-a", "git push --force-with-lease origin fix/ui").verdict is Verdict.APPROVE
    assert ev(cfg, "claude-a", "git branch -D old && git worktree prune").verdict is Verdict.APPROVE


def test_codex_wrapped_git_in_own_clone(cfg):
    a = cfg.agents["codex-a"]
    r = Policy(cfg).evaluate(a, "Bash", {"command": ["/bin/bash", "-lc", "git fetch origin\ngit reset --hard origin/main"],
                                         "cwd": str(a.clone)}, "")
    assert r.verdict is Verdict.APPROVE


def test_git_in_project_state_is_approved(cfg):
    assert ev(cfg, "codex-a", f"git -C {cfg.project_state_dir} push origin main").verdict is Verdict.APPROVE


def test_git_in_another_clone_goes_to_gm_with_note(cfg):
    other = cfg.agents["codex-g"].clone
    r = ev(cfg, "claude-a", f"cd {other} && git reset --hard origin/main")
    assert r.verdict is Verdict.GM and "another agent's clone" in r.notes[0]


def test_unlisted_actions_go_to_gm(cfg):
    assert ev(cfg, "claude-a", "git status && rm -rf build").verdict is Verdict.GM
    assert ev(cfg, "codex-a", "./sn reset dev12 --wipe-all").verdict is Verdict.GM
    assert ev(cfg, "claude-a", "find . -name '*.py'").verdict is Verdict.GM


def test_auto_approve_patterns(cfg):
    assert ev(cfg, "codex-a", "npm test -- --watch=false").verdict is Verdict.APPROVE


def test_refuse_patterns(cfg):
    for cmd in ("find / -name libfoo.dylib 2>/dev/null", "mdfind libfoo", "cat ~/.ssh/id_ed25519",
                "grep -r token ~", "security find-generic-password -s github", "sudo rm -rf /tmp/x",
                "ls /Users/dana/Documents/"):
        assert ev(cfg, "claude-a", cmd).verdict is Verdict.REFUSE, cmd


def test_owner_only(cfg):
    assert ev(cfg, "claude-a", "npm run reset-prod").verdict is Verdict.OWNER


def test_rate_cap_sends_auto_approvals_to_gm(cfg):
    p = Policy(cfg)
    a = cfg.agents["claude-a"]
    for _ in range(cfg.max_approvals_per_agent_per_hour):
        p.record_approval("claude-a")
    r = p.evaluate(a, "Bash", {"command": "git status"}, str(a.clone))
    assert r.verdict is Verdict.GM and "rate cap" in r.rule
