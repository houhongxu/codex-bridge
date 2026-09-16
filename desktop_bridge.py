"""Per-CLI WebSocket relay that opens that CLI's task in the desktop app.

RPC messages pass through unchanged. Only responses to this connection's own
start/resume/fork requests and its turn/start requests trigger desktop linking.
"""

import argparse
import asyncio
import contextlib
import datetime
import json
import os
from pathlib import Path
import re
import secrets
import signal
import time
import uuid

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed


def object_message(raw):
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def valid_thread_id(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def relay_authorized(request, token):
    """Codex remote addresses cannot contain a path; use its bearer auth."""
    if request.path != "/":
        return False
    values = request.headers.get_all("Authorization")
    return len(values) == 1 and secrets.compare_digest(values[0], "Bearer " + token)


class DesktopSubscriptions:
    """Follow native subscription evidence for the current desktop process.

    Attachment JSON is diagnostic only: it must never restore a subscription
    from a previous desktop process or connection.
    """
    MAX_READ = 2 * 1024 * 1024

    def __init__(self, home):
        self.home = Path(home)
        self.session = None
        self.positions = {}
        self.confirmed = set()
        self.retry_after = {}

    def invalidate(self, thread_id=None):
        if thread_id is None:
            self.confirmed.clear()
            self.retry_after.clear()
        else:
            self.confirmed.discard(thread_id)
            self.retry_after.pop(thread_id, None)

    def current_logs(self):
        root = self.home / "Library/Logs/com.openai.codex"
        candidates = []
        for path in root.glob("*/*/*/*-t0-*.log"):
            match = re.match(r"^(codex-desktop-.+)-(\d+)-t0-.*\.log$", path.name)
            if match is None:
                continue
            try:
                stat = path.stat()
                try:
                    os.kill(int(match[2]), 0)  # Existence check; sends no signal.
                except PermissionError:
                    pass
            except OSError:
                continue
            candidates.append((stat.st_mtime_ns, path, stat, (match[1], match[2])))
        if not candidates:
            return None, []
        candidates.sort(key=lambda item: item[0], reverse=True)
        session = candidates[0][3]
        return session, [(p, stat) for _, p, stat, owner in candidates if owner == session][:4]

    def apply_line(self, line):
        match = re.match(r"^\S+ (?:info|warning|error|debug) \[[^\]]+\] (\S+) (.*)$", line)
        if match is None:
            return
        event, rest = match.groups()
        fields = dict(re.findall(r"(?:^|\s)([A-Za-z]+)=([^\s]+)", rest))
        if (event == "app_server_connection.state_changed" and fields.get("hostId") == "local"
                and fields.get("next") in {"connecting", "connected", "disconnected"}):
            self.invalidate()
            return
        thread_id = fields.get("conversationId")
        if not valid_thread_id(thread_id):
            return
        if event == "inactive_thread_unsubscribed":
            self.invalidate(thread_id)
        elif event == "maybe_resume_success" and fields.get("assignedStreamRole") in {"owner", "follower"}:
            self.confirmed.add(thread_id)
            self.retry_after.pop(thread_id, None)
        elif event == "thread_stream_view_activity_changed":
            if fields.get("resumeState") == "resumed" and fields.get("streamRole") in {"owner", "follower"}:
                # Switching away from a view does not itself unsubscribe it.
                self.confirmed.add(thread_id)
                self.retry_after.pop(thread_id, None)
            elif fields.get("resumeState") == "needs_resume" or fields.get("streamRole") == "null":
                self.invalidate(thread_id)

    def refresh(self):
        session, logs = self.current_logs()
        if session != self.session:
            self.invalidate()
            self.positions.clear()
            self.session = session
        if self.positions and logs and not any(path in self.positions for path, _ in logs):
            # All observed files rolled away while this CLI was idle.
            self.invalidate()
        lines = []
        read_failed = False
        for path, stat in reversed(logs):
            inode, offset = self.positions.get(path, (stat.st_ino, 0))
            if inode != stat.st_ino or stat.st_size < offset:
                self.invalidate()
                lines.clear()
                offset = 0
            # Bound reads after a long CLI idle. If evidence was skipped, rebuild
            # conservatively from the retained tail, never from attachment JSON.
            skip_partial = stat.st_size - offset > self.MAX_READ
            if skip_partial:
                self.invalidate()
                lines.clear()
                offset = stat.st_size - self.MAX_READ
            try:
                with path.open("rb") as stream:
                    stream.seek(offset)
                    data = stream.read(self.MAX_READ)
                end = data.rfind(b"\n") + 1
                self.positions[path] = (stat.st_ino, offset + end)
                complete = data[:end].decode(errors="replace").splitlines()
                lines.extend(complete[1:] if skip_partial else complete)
            except OSError:
                read_failed = True
        # Timestamps order events across daily/size rotations of the same process.
        for line in sorted(lines, key=lambda value: value.split(" ", 1)[0]):
            self.apply_line(line)
        if read_failed:
            self.confirmed.clear()


class DesktopAttacher:
    CONFIRM_TIMEOUT = 4
    RETRY_DELAY = 60

    def __init__(self, app, home=None):
        self.app = app
        self.home = Path(home or Path.home())
        self.subscriptions = DesktopSubscriptions(self.home)
        self.lock = asyncio.Lock()

    def record(self, thread_id, status):
        root = self.home / "Library/Application Support/Codex CLI Bridge/attachments"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = root / f"{thread_id}.json"
        temp = root / f".{thread_id}.{os.getpid()}.tmp"
        payload = {"thread_id": thread_id, "status": status,
                   "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        temp.write_text(json.dumps(payload) + "\n")
        temp.chmod(0o600)
        os.replace(temp, target)

    async def __call__(self, thread_id):
        if not valid_thread_id(thread_id):
            return
        async with self.lock:
            state = self.subscriptions
            state.refresh()
            if thread_id in state.confirmed:
                self.record(thread_id, "subscription_confirmed")
                return
            if time.monotonic() < state.retry_after.get(thread_id, 0):
                return
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/open", "-g", "-a", self.app, f"codex://threads/{thread_id}",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                code = await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                code = -1
            if code:
                state.retry_after[thread_id] = time.monotonic() + self.RETRY_DELAY
                self.record(thread_id, "open_failed")
                return
            deadline = time.monotonic() + self.CONFIRM_TIMEOUT
            while True:
                state.refresh()
                if thread_id in state.confirmed:
                    self.record(thread_id, "subscription_confirmed")
                    return
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.15)
            # Missing log evidence is not proof of a lost subscription. Back off
            # instead of opening the desktop on every input; still allow recovery.
            state.retry_after[thread_id] = time.monotonic() + self.RETRY_DELAY
            self.record(thread_id, "opened_unconfirmed")


class Relay:
    def __init__(self, upstream, attach):
        self.upstream = upstream
        self.attach = attach

    async def safely_attach(self, thread_id):
        try:
            await self.attach(thread_id)
        except Exception as exc:
            # A UI failure must not break CLI RPC or alter an approval decision.
            print(f"desktop attach failed: {type(exc).__name__}", file=__import__("sys").stderr, flush=True)

    async def handle(self, downstream):
        pending = {}
        # Only tasks this CLI successfully created/resumed/forked, with an
        # explicit non-ephemeral response, can be opened in the desktop.
        attachable = set()
        async with connect(self.upstream, proxy=None, compression=None,
                           max_size=None, close_timeout=2) as upstream:
            async def from_cli():
                async for raw in downstream:
                    message = object_message(raw)
                    method = message.get("method")
                    if method in {"thread/start", "thread/resume", "thread/fork"} and "id" in message:
                        pending[message["id"]] = message.get("params", {}).get("ephemeral") is True
                    elif method == "turn/start":
                        thread_id = message.get("params", {}).get("threadId")
                        if thread_id in attachable:
                            await self.safely_attach(thread_id)
                    await upstream.send(raw)

            async def from_backend():
                async for raw in upstream:
                    message = object_message(raw)
                    request_id = message.get("id")
                    if request_id in pending and "method" not in message:
                        requested_ephemeral = pending.pop(request_id)
                        thread = message.get("result", {}).get("thread", {})
                        thread_id = thread.get("id")
                        if valid_thread_id(thread_id):
                            attachable.discard(thread_id)
                            if thread.get("ephemeral") is False and not requested_ephemeral:
                                attachable.add(thread_id)
                                await self.safely_attach(thread_id)
                    await downstream.send(raw)

            tasks = {asyncio.create_task(from_cli()), asyncio.create_task(from_backend())}
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    with contextlib.suppress(ConnectionClosed):
                        task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await downstream.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--app", required=True)
    args = parser.parse_args()
    token = secrets.token_urlsafe(24)
    relay = Relay(args.upstream, DesktopAttacher(args.app))

    async def handle(connection):
        if not relay_authorized(connection.request, token):
            await connection.close(1008, "Unknown local relay")
            return
        await relay.handle(connection)

    stopped = asyncio.Event()
    owner_pid = os.getppid()

    async def watch_owner():
        while os.getppid() == owner_pid:
            await asyncio.sleep(1)
        stopped.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    async with serve(handle, "127.0.0.1", 0, origins=[None], compression=None,
                     max_size=None, close_timeout=2) as server:
        port = server.sockets[0].getsockname()[1]
        print(json.dumps({"endpoint": f"ws://127.0.0.1:{port}", "auth_token": token}), flush=True)
        watcher = asyncio.create_task(watch_owner())
        try:
            await stopped.wait()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
