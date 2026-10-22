"""agent-watcher command line.

  agent-watcher run [--config watcher.toml] [--agents agents.toml]
  agent-watcher hook --kind claude|codex      (called by agent hooks; reads the payload on stdin)
  agent-watcher statusline [--then CMD]       (Claude statusLine command: forwards usage limits, prints CMD's line)
  agent-watcher install-statusline HOME [--dry-run]
  agent-watcher status | restart <agent>|cohort:<project> | pause [agent] | resume [agent]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from .daemon import socket_path


def _send(msg: dict, want_reply: bool, timeout: float) -> dict | None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(socket_path()))
        s.sendall(json.dumps(msg).encode() + b"\n")
        if not want_reply:
            return None
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data or b"{}")
    finally:
        s.close()


def hook(kind: str) -> int:
    """Forward a hook payload to the daemon. Always exits 0 and prints nothing: it never decides."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if kind == "claude":
            config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
        else:
            config_dir = os.environ.get("CODEX_HOME") or "~/.codex"
        _send({"type": "hook", "kind": kind, "config_dir": str(Path(config_dir).expanduser()),
               "iterm_session": os.environ.get("ITERM_SESSION_ID"), "payload": payload},
              want_reply=False, timeout=1.0)
    except Exception:  # noqa: BLE001 - a hook must never break the agent
        pass
    return 0


def statusline(then: str | None) -> int:
    """Claude's statusLine command: forward the payload to the daemon, then print `then`'s status line (if any)."""
    data = sys.stdin.read()
    try:
        payload = json.loads(data or "{}")
        if payload.get("rate_limits"):
            config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
            _send({"type": "statusline", "kind": "claude", "config_dir": str(Path(config_dir).expanduser()),
                   "payload": {"session_id": payload.get("session_id"), "rate_limits": payload["rate_limits"]}},
                  want_reply=False, timeout=0.5)
    except Exception:  # noqa: BLE001 - the status line must never break
        pass
    if then:
        try:
            out = subprocess.run(then, shell=True, input=data, capture_output=True, text=True, timeout=5).stdout
            sys.stdout.write(out)
        except (OSError, subprocess.SubprocessError):
            pass
    return 0


def install_statusline(home: Path, dry_run: bool) -> int:
    """Wrap HOME/settings.json's statusLine command with `agent-watcher statusline --then <old command>`."""
    path = home.expanduser() / "settings.json"
    settings = json.loads(path.read_text()) if path.exists() else {}
    sl = settings.get("statusLine") or {}
    old = sl.get("command") if sl.get("type") == "command" else None
    if old and " statusline" in old and "agent-watcher" in old:
        print(f"{path}: already wrapped")
        return 0
    exe = shutil.which("agent-watcher") or str(Path(sys.argv[0]).resolve())
    cmd = f"{shlex.quote(exe)} statusline" + (f" --then {shlex.quote(old)}" if old else "")
    settings["statusLine"] = {**sl, "type": "command", "command": cmd}
    print(f"{path}:\n  before: {old!r}\n  after:  {cmd!r}")
    if dry_run:
        return 0
    if path.exists():
        backup = path.with_name(f"settings.json.bak-agent-watcher-{time.strftime('%Y%m%d%H%M%S')}")
        shutil.copy2(path, backup)
        print(f"  backup: {backup}")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    tmp.replace(path)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agent-watcher")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--config", default="watcher.toml")
    r.add_argument("--agents", default="agents.toml")
    r.add_argument("-v", "--verbose", action="store_true")
    h = sub.add_parser("hook")
    h.add_argument("--kind", choices=["claude", "codex"], required=True)
    sl = sub.add_parser("statusline")
    sl.add_argument("--then", help="the status line command to run and print after forwarding")
    ins = sub.add_parser("install-statusline")
    ins.add_argument("home", help="a Claude config dir, e.g. ~/.claude-a")
    ins.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
    rs = sub.add_parser("restart")
    rs.add_argument("agent")
    for c in ("pause", "resume"):
        sub.add_parser(c).add_argument("agent", nargs="?")
    args = p.parse_args(argv)

    if args.cmd == "hook":
        return hook(args.kind)
    if args.cmd == "statusline":
        return statusline(args.then)
    if args.cmd == "install-statusline":
        return install_statusline(Path(args.home), args.dry_run)
    if args.cmd == "run":
        from . import config
        from .daemon import Daemon
        cfg = config.load(Path(args.config).expanduser(), Path(args.agents).expanduser())
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.DEBUG if args.verbose else logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[logging.StreamHandler(), logging.FileHandler(cfg.state_dir / "watcher.log")],
        )
        for noisy in ("websockets", "iterm2"):  # -v is for the watcher, not every iTerm2 API frame
            logging.getLogger(noisy).setLevel(logging.INFO)
        os.environ.setdefault("AGENT_WATCHER_SOCK", str(cfg.state_dir / "watcher.sock"))
        asyncio.run(Daemon(cfg).run())
        return 0
    try:
        reply = _send({"type": "cmd", "cmd": args.cmd, "arg": getattr(args, "agent", None)}, True, 10.0)
    except OSError as e:
        print(f"agent-watcher daemon not reachable at {socket_path()}: {e}", file=sys.stderr)
        return 1
    print(json.dumps(reply, indent=2))
    return 0 if reply and reply.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
