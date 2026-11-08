#!/usr/bin/env bash
# Runs agent-watcher in the foreground, appending its output to run.out in the config's state_dir.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if pid=$(pgrep -f 'python[0-9.]* .*agent-watcher run'); then
    echo "agent-watcher is already running (PID ${pid//$'\n'/ })" >&2
    exit 1
fi

state_dir=$(.venv/bin/python -c '
import tomllib
from pathlib import Path
from agent_watcher.cli import default_config
c = default_config()
print(Path(tomllib.loads(c.read_text()).get("state_dir", "~/.agent-watcher") if c.is_file() else "~/.agent-watcher").expanduser())
')
mkdir -p "$state_dir"

.venv/bin/agent-watcher run 2>&1 | tee -a "$state_dir/run.out"
