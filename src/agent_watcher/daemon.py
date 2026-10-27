"""The daemon: wires discovery, hooks, session logs, the channels and the terminals (iTerm2, tmux) to the supervisor."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from pathlib import Path

from .actuator import Actuator, MacNotifier
from .channel import SlackChannel, slack_tokens
from .config import CHANNEL_SUFFIX, WatcherConfig
from .detect import codex_screen_capacity, from_hook
from .discovery import Discovery, transcript_dir
from .events import E, Event
from .supervisor import Supervisor
from .terminals import Terminals
from .tails import ClaudeTranscriptTailer, RolloutTailer
from .usage import claude_rate_limits

log = logging.getLogger(__name__)


def socket_path(state_dir: Path | None = None) -> Path:
    env = os.environ.get("AGENT_WATCHER_SOCK")
    if env:
        return Path(env).expanduser()
    return (state_dir or Path("~/.agent-watcher").expanduser()) / "watcher.sock"


class _NoChannel:
    """Stand-in when Slack tokens are missing: no projects are served, posts go to the audit log."""

    name = "agent-watcher"
    user_id = ""

    def __init__(self, audit):
        self.audit = audit

    def projects(self) -> set[str]:
        return set()

    def ready(self, project: str) -> bool:
        return False

    async def post(self, project: str, text: str, thread_ts: str | None = None) -> str | None:
        self.audit("-", "channel", "dry", "POST", f"[{project}] {text}", "[no Slack tokens]")
        return None

    async def invite(self, project: str, agent_name: str) -> bool:
        return False


class Daemon:
    def __init__(self, cfg: WatcherConfig):
        self.cfg = cfg
        self.queue: asyncio.Queue[Event] = asyncio.Queue()
        self._rescan = asyncio.Event()
        self.term = Terminals(cfg, self.emit, on_change=self._rescan.set)
        self.actuator = Actuator(cfg, self.term, cfg.state_dir / "audit.log")
        self.discovery = Discovery()
        tokens = slack_tokens(cfg)
        self.slack: SlackChannel | None = None
        self.sup: Supervisor
        channel = _NoChannel(self.actuator.audit)
        if tokens:
            self.slack = SlackChannel(cfg, tokens, self.emit, lambda p: self.sup.gm(p), self.actuator.audit)
            channel = self.slack
        else:
            log.warning("no Slack bot/app tokens (env %s/%s or [slack] env_file); no projects can be served",
                        cfg.slack_bot_token_env, cfg.slack_app_token_env)
        self.sup = Supervisor(cfg, self.term, self.actuator, channel, MacNotifier())
        self.sup.refresh = self._discover_once
        self.transcripts = ClaudeTranscriptTailer(cfg, self.emit)
        self.rollouts: dict[str, asyncio.Task] = {}  # agent -> its Codex rollout tailer
        self._state_mtimes: dict[Path, float] = {}
        self._screen_hits: dict[str, str | None] = {}  # agent -> capacity error line last seen on its screen

    async def emit(self, ev: Event) -> None:
        await self.queue.put(ev)

    async def _consume(self) -> None:
        while True:
            ev = await self.queue.get()
            try:
                await self.sup.handle(ev)
                if ev.kind is E.CHANNEL_READY:
                    self._rescan.set()  # a project appeared or went away; re-evaluate running agents
            except Exception:  # noqa: BLE001 - one bad event must not stop the watcher
                log.exception("handling %r failed", ev)

    # ---- discovery ----------------------------------------------------------------------------

    async def _discover_once(self) -> None:
        records = await asyncio.to_thread(self.discovery.scan)
        self._state_mtimes = {}
        for r in records:
            f = r.home / "slack-agent-chat" / "state.json"
            try:
                self._state_mtimes[f] = f.stat().st_mtime
            except OSError:
                pass
        changes = self.sup.apply_discovery(records)
        for name in changes.dropped:
            self.term.unbind(name)
            self.transcripts.unfollow(name)
            task = self.rollouts.pop(name, None)
            if task:
                task.cancel()
        for name, st in list(self.sup.agents.items()):
            await self.term.bind(name, st.term_id)
            acfg = self.cfg.agents[name]
            if acfg.kind == "claude" and st.session_id:
                path = transcript_dir(st.home, acfg.clone) / f"{st.session_id}.jsonl"
                await self.transcripts.follow(name, str(path))
            elif acfg.kind == "codex" and (name not in self.rollouts or self.rollouts[name].done()):
                self.rollouts[name] = asyncio.create_task(RolloutTailer(acfg, self.emit).run(), name=f"tail:{name}")
        await self.sup._notify_waiters()

    def _state_changed(self) -> bool:
        for f, m in self._state_mtimes.items():
            try:
                if f.stat().st_mtime != m:
                    return True
            except OSError:
                return True
        return False

    async def _discovery_loop(self) -> None:
        sweep = self.cfg.timing.discovery_sweep_s
        while True:
            try:
                await self._discover_once()
            except Exception:  # noqa: BLE001
                log.exception("discovery failed")
            self._rescan.clear()
            waited = 0.0
            while waited < sweep and not self._rescan.is_set() and not self._state_changed():
                try:
                    await asyncio.wait_for(self._rescan.wait(), timeout=2)
                except TimeoutError:
                    waited += 2

    # ---- Codex screens ---------------------------------------------------------------------------

    async def _screen_once(self) -> None:
        """Emit CAPACITY when a Codex screen newly shows a capacity error (see detect.codex_screen_capacity)."""
        for name in list(self.sup.agents):
            acfg = self.cfg.agents.get(name)
            if acfg is None or acfg.kind != "codex" or not self.term.bound(name) or self.actuator.locks[name].locked():
                continue  # not a Codex tab, or the watcher is typing into it
            hit = codex_screen_capacity(await self.term.screen_text(name))
            if hit and not self._screen_hits.get(name):
                log.info("%s: capacity error on screen: %s", name, hit)
                await self.emit(Event(E.CAPACITY, name, {"text": hit}, source="screen"))
            self._screen_hits[name] = hit

    async def _screen_loop(self) -> None:
        while True:
            try:
                await self._screen_once()
            except Exception:  # noqa: BLE001
                log.exception("screen scan failed")
            await asyncio.sleep(self.cfg.timing.screen_poll_s)

    # ---- control socket ------------------------------------------------------------------------

    async def _on_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            msg = json.loads(line or b"{}")
            if msg.get("type") == "hook":
                await self._on_hook(msg)
            elif msg.get("type") == "statusline":
                await self._on_statusline(msg)
            elif msg.get("type") == "cmd":
                reply = self.sup.control(msg.get("cmd", ""), msg.get("arg"))
                writer.write(json.dumps(reply, default=str).encode() + b"\n")
                await writer.drain()
        except (json.JSONDecodeError, TimeoutError, ConnectionError) as e:
            log.warning("bad control connection: %s", e)
        finally:
            writer.close()

    async def _on_hook(self, msg: dict) -> None:
        home = Path(msg.get("config_dir", "")).expanduser()
        payload = msg.get("payload") or {}
        sid = payload.get("session_id")
        if msg.get("kind") == "claude" and sid and self.discovery.hook_sessions.get(home) != sid:
            self.discovery.hook_sessions[home] = sid
            self._rescan.set()  # new session for this home
        name = next((n for n, st in self.sup.agents.items() if st.home == home), None)
        if name is None:
            self._rescan.set()  # an agent we don't know yet; it may have just joined a project
            return
        event = payload.get("hook_event_name") or msg.get("event", "")
        for ev in from_hook(event, payload, name, self.cfg.agents[name].kind):
            await self.emit(ev)

    async def _on_statusline(self, msg: dict) -> None:
        home = Path(msg.get("config_dir", "")).expanduser()
        name = next((n for n, st in self.sup.agents.items() if st.home == home), None)
        windows = claude_rate_limits(msg.get("payload") or {})
        log.debug("status line from %s (%s): %s", home, name or "not watched",
                  {k: f"{w.left_pct}% left" for k, w in windows.items()})
        if name and windows:
            await self.emit(Event(E.USAGE, name, {"windows": windows}, source="statusline"))

    @staticmethod
    def _exit(sock: Path) -> None:
        """Stop at once. Cancelling the iTerm2 connection's tasks can hang, and all state is already on disk."""
        log.info("agent-watcher stopping")
        sock.unlink(missing_ok=True)
        logging.shutdown()
        os._exit(0)

    async def run(self) -> None:
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        sock = socket_path(self.cfg.state_dir)
        if sock.exists():
            sock.unlink()
        server = await asyncio.start_unix_server(self._on_conn, path=str(sock))
        os.chmod(sock, 0o600)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):  # SIGHUP: its tab was closed
            loop.add_signal_handler(sig, self._exit, sock)
        await self.term.start()
        tasks = [asyncio.create_task(self._consume(), name="consume"),
                 asyncio.create_task(self.sup.scheduler(), name="scheduler"),
                 asyncio.create_task(self.transcripts.run(), name="transcripts")]
        if self.slack:
            tasks.append(asyncio.create_task(self.slack.run(), name="slack"))
        tasks.append(asyncio.create_task(self._discovery_loop(), name="discovery"))
        tasks.append(asyncio.create_task(self._screen_loop(), name="screens"))
        log.info("agent-watcher running; serving *%s channels; modes=%s; socket=%s", CHANNEL_SUFFIX,
                 self.cfg.modes, sock)
        async with server:
            await asyncio.gather(*tasks)
