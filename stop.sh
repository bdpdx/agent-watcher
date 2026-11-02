#!/usr/bin/env bash
# Stops a running agent-watcher: SIGTERM, then SIGKILL if it hasn't exited within 10 seconds.
set -euo pipefail

# Anchored so it can't match (and kill) a shell whose command line merely mentions the watcher.
pattern='^[^ ]*python[0-9.]* [^ ]*agent-watcher run( |$)'

if ! pid=$(pgrep -f "$pattern"); then
    echo "agent-watcher is not running"
    exit 0
fi

echo "Stopping agent-watcher (PID ${pid//$'\n'/ })"
kill $pid 2>/dev/null || true

for _ in {1..100}; do
    pgrep -f "$pattern" >/dev/null || exit 0
    sleep 0.1
done

if pid=$(pgrep -f "$pattern"); then
    echo "agent-watcher did not stop; killing PID ${pid//$'\n'/ }" >&2
    kill -KILL $pid 2>/dev/null || true
fi
