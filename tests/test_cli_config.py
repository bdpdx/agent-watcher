import os

from agent_watcher import cli


def test_hooks_find_the_socket_in_the_configs_state_dir(tmp_path, monkeypatch):
    cfg = tmp_path / "watcher.toml"
    cfg.write_text(f'state_dir = "{tmp_path / "home"}"\n')
    monkeypatch.delenv("AGENT_WATCHER_SOCK", raising=False)
    monkeypatch.setenv("AGENT_WATCHER_CONFIG", str(cfg))
    monkeypatch.chdir(tmp_path.parent)
    assert cli.default_config() == cfg
    cli._use_config_socket()
    assert os.environ["AGENT_WATCHER_SOCK"] == str(tmp_path / "home" / "watcher.sock")


def test_default_config_falls_back_to_the_checkout(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_WATCHER_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.default_config() == cli.REPO_CONFIG
