#!/usr/bin/env bash
# Runs agent-watcher in the foreground, appending its output to ~/.agent-watcher-run.out.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if pid=$(pgrep -f 'python[0-9.]* .*agent-watcher run'); then
    echo "agent-watcher is already running (PID ${pid//$'\n'/ })" >&2
    exit 1
fi

.venv/bin/agent-watcher run 2>&1 | tee -a "$HOME/.agent-watcher-run.out"
