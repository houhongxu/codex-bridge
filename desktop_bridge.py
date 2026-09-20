"""Per-CLI WebSocket relay that opens that CLI's task in the desktop app.

Ordinary RPC messages pass through unchanged. Only responses to this connection's own
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
import stat
import time
import uuid

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from question_sync import QuestionSync


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


def rollout_ready(path):
    """The advertised path can precede creation of a new thread's JSONL file."""
    try:
        if not path.is_file():
            return False
        # Nonblocking open also avoids hanging RPC if the path becomes a FIFO.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return False
            first = stream.readline(1024 * 1024)
        if not first.endswith(b"\n"):
            return False
        metadata = object_message(first)
        return metadata.get("type") == "session_meta" and isinstance(metadata.get("payload"), dict)
    except (OSError, ValueError):
        return False


class PersistedAttachments:
    """Wait for local history without blocking either RPC forwarding loop."""
    WAIT_TIMEOUT = 10
    POLL_INTERVAL = 0.1

    def __init__(self, attach):
        self.attach = attach
        self.paths = {}
        self.tasks = {}

    def track(self, thread, requested_ephemeral):
        thread_id = thread.get("id")
        self.paths.pop(thread_id, None)
        path = thread.get("path")
        if (thread.get("ephemeral") is False and not requested_ephemeral
                and isinstance(path, str) and path and Path(path).is_absolute()):
            self.paths[thread_id] = Path(path)
            # Empty new threads should stay idle until their first turn.
            self.schedule(thread_id, wait=False)

    def schedule(self, thread_id, wait=True):
        path = self.paths.get(thread_id)
        if path is None or thread_id in self.tasks:
            return
        if not wait and not rollout_ready(path):
            return
        self.tasks[thread_id] = asyncio.create_task(self.run(thread_id))

    async def run(self, thread_id):
        try:
            deadline = time.monotonic() + self.WAIT_TIMEOUT
            while thread_id in self.paths:
                if rollout_ready(self.paths[thread_id]):
                    await self.attach(thread_id)
                    return
                if time.monotonic() >= deadline:
                    return  # A later CLI turn can retry; never open missing history.
                await asyncio.sleep(self.POLL_INTERVAL)
        except Exception as exc:
            print(f"desktop attach failed: {type(exc).__name__}", file=__import__("sys").stderr, flush=True)
        finally:
            self.tasks.pop(thread_id, None)

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()


class EmptyThreadNames:
    """Materialize owned paginated threads before publishing a name.

    Metadata-only naming can make an empty thread visible to Desktop while its
    rollout still exists only in memory. Ask the server to persist its history;
    never synthesize a message, write its files, or resume another client's thread.
    """
    TIMEOUT = 3

    def __init__(self, send_backend, send_cli):
        self.send_backend = send_backend
        self.send_cli = send_cli
        self.paths = {}
        self.prefix = "cpet-history-" + uuid.uuid4().hex + "-"
        self.pending = {}
        self.tasks = set()
        self.lock = asyncio.Lock()

    def track(self, thread, requested_ephemeral):
        thread_id = thread.get("id")
        self.paths.pop(thread_id, None)
        path = thread.get("path")
        if (valid_thread_id(thread_id) and not requested_ephemeral
                and thread.get("ephemeral") is False
                and thread.get("historyMode") == "paginated"
                and isinstance(path, str) and Path(path).is_absolute()):
            self.paths[thread_id] = Path(path)

    def response(self, message):
        request_id = message.get("id")
        if ("method" in message or not isinstance(request_id, str)
                or not request_id.startswith(self.prefix)):
            return False
        future = self.pending.get(request_id)
        if future is not None and not future.done():
            future.set_result(None)
        return True  # Also consume late replies after our timeout.

    def schedule(self, raw, message):
        task = asyncio.create_task(self.forward(raw, message))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def forward(self, raw, message):
        try:
            async with self.lock:
                thread_id = message.get("params", {}).get("threadId")
                path = self.paths.get(thread_id)
                if path is not None and not rollout_ready(path):
                    request_id = self.prefix + uuid.uuid4().hex
                    future = asyncio.get_running_loop().create_future()
                    self.pending[request_id] = future
                    try:
                        await self.send_backend(json.dumps({
                            "id": request_id, "method": "thread/read",
                            "params": {"threadId": thread_id, "includeTurns": True}}))
                        await asyncio.wait_for(future, self.TIMEOUT)
                    except asyncio.TimeoutError:
                        pass
                    finally:
                        self.pending.pop(request_id, None)
                    # Persistence may succeed before history projection rejects
                    # a not-yet-indexed thread. Verify the actual rollout.
                    if not rollout_ready(path):
                        await self.send_cli({"id": message["id"], "error": {
                            "code": -32603,
                            "message": "cpet could not persist this empty thread before naming it. "
                                       "Send the first message in CLI, then retry naming; "
                                       "do not open the empty thread in Desktop yet."}})
                        return
                await self.send_backend(raw)
        except ConnectionClosed:
            pass

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()


class Relay:
    def __init__(self, upstream, attach, question_sync=True):
        self.upstream = upstream
        self.attach = attach
        self.question_sync = question_sync

    async def handle(self, downstream):
        pending = {}
        # Only tasks this CLI successfully created/resumed/forked, with an
        # explicit non-ephemeral response, can be opened in the desktop.
        attachments = PersistedAttachments(self.attach)
        async with connect(self.upstream, proxy=None, compression=None,
                           max_size=None, close_timeout=2) as upstream:
            async def send_cli(message):
                await downstream.send(json.dumps(message, ensure_ascii=False))

            async def send_backend(message):
                await upstream.send(json.dumps(message, ensure_ascii=False))
                if message.get("method") == "turn/start":
                    attachments.schedule(message.get("params", {}).get("threadId"))

            questions = QuestionSync(send_cli, send_backend) if self.question_sync else None
            names = EmptyThreadNames(upstream.send, send_cli)

            async def from_cli():
                async for raw in downstream:
                    message = object_message(raw)
                    if questions is not None and await questions.from_cli(message):
                        continue
                    method = message.get("method")
                    if method == "thread/name/set" and "id" in message:
                        names.schedule(raw, message)
                        continue
                    if method in {"thread/start", "thread/resume", "thread/fork"} and "id" in message:
                        pending[message["id"]] = message.get("params", {}).get("ephemeral") is True
                    await upstream.send(raw)
                    if method == "turn/start":
                        attachments.schedule(message.get("params", {}).get("threadId"))

            async def from_backend():
                async for raw in upstream:
                    message = object_message(raw)
                    if names.response(message):
                        continue
                    request_id = message.get("id")
                    if request_id in pending and "method" not in message:
                        requested_ephemeral = pending.pop(request_id)
                        thread = message.get("result", {}).get("thread", {})
                        thread_id = thread.get("id")
                        if valid_thread_id(thread_id):
                            names.track(thread, requested_ephemeral)
                            if questions is not None:
                                questions.track_thread(thread)
                            attachments.track(thread, requested_ephemeral)
                    if questions is None:
                        await downstream.send(raw)
                    else:
                        forwarded, extra = questions.from_backend(message)
                        if forwarded is message:
                            await downstream.send(raw)
                        elif forwarded is not None:
                            await send_cli(forwarded)
                        for notification in extra:
                            await send_cli(notification)

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
                if questions is not None:
                    await questions.close()
                await names.close()
                await attachments.close()
                await downstream.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--app", required=True)
    args = parser.parse_args()
    token = secrets.token_urlsafe(24)
    relay = Relay(args.upstream, DesktopAttacher(args.app),
                  question_sync=os.environ.get("CPET_QUESTION_SYNC", "1") != "0")

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
