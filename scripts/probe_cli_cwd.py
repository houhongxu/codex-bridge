#!/usr/bin/env python3
"""Probe remote TUI cwd propagation without credentials or model requests.

The probe starts a temporary App Server and recording relay on random loopback
ports. It records only method names, request IDs, thread IDs, and cwd fields.
Use --through-bridge to put this checkout's Relay and cli_arguments in the path.
"""

import argparse
import asyncio
import fcntl
import json
import os
import pty
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cb
from desktop_bridge import Relay


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def wait_port(port, process):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.returncode is not None:
            raise RuntimeError(f"isolated app-server exited with {process.returncode}")
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.05)
    raise TimeoutError("isolated app-server did not listen")


async def run(args):
    with tempfile.TemporaryDirectory(prefix="cb-cwd-probe-") as directory:
        root = Path(directory)
        home = root / "home"
        codex_home = root / "codex-home"
        project = root / "project A"
        for path in (home, codex_home, project):
            path.mkdir(parents=True)
        trusted = dict.fromkeys((str(project), str(project.resolve())))
        (codex_home / "config.toml").write_text("\n".join(
            f"[projects.{json.dumps(path)}]\ntrust_level = \"trusted\"\n" for path in trusted))

        upstream_port = free_port()
        recorder_port = free_port()
        bridge_port = free_port()
        environment = {
            "HOME": str(home),
            "CODEX_HOME": str(codex_home),
            "PATH": os.environ.get("PATH", ""),
            "TERM": "xterm-256color",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
        version_result = subprocess.run([args.cli, "--version"], cwd=root, env=environment,
                                        text=True, capture_output=True, timeout=10)
        cli_version = version_result.stdout.strip() or "unknown"
        log = (root / "app-server.log").open("wb")
        app_server = await asyncio.create_subprocess_exec(
            args.cli, "app-server", "--listen", f"ws://127.0.0.1:{upstream_port}",
            cwd=root, env=environment, stdout=log, stderr=log,
        )
        rows = []
        methods = []
        pending = {}
        complete = asyncio.Event()

        async def recorder(downstream):
            async with connect(f"ws://127.0.0.1:{upstream_port}", proxy=None,
                               compression=None, max_size=None) as upstream:
                async def forward(source, target, direction):
                    async for raw in source:
                        try:
                            message = json.loads(raw)
                        except (TypeError, ValueError):
                            message = {}
                        if isinstance(message, dict):
                            method = message.get("method")
                            request_id = message.get("id")
                            params = message.get("params")
                            params = params if isinstance(params, dict) else {}
                            row = {"direction": direction, "method": method, "id": request_id}
                            if method:
                                methods.append({"direction": direction, "method": method})
                            if direction == "cli->server" and method and request_id is not None:
                                pending[request_id] = method
                            if method in {"thread/start", "thread/resume", "thread/fork", "turn/start"}:
                                row.update({key: params.get(key) for key in ("cwd", "threadId") if key in params})
                                rows.append(row)
                            if direction == "server->cli" and request_id in pending:
                                response_to = pending.pop(request_id)
                                if response_to == "account/read":
                                    message["result"] = {
                                        "account": {"type": "apiKey"}, "requiresOpenaiAuth": False,
                                    }
                                    raw = json.dumps(message)
                                elif response_to in {"thread/start", "thread/resume", "thread/fork"}:
                                    thread = message.get("result", {}).get("thread", {})
                                    rows.append({
                                        "direction": direction,
                                        "id": request_id,
                                        "responseTo": response_to,
                                        "threadId": thread.get("id"),
                                        "cwd": thread.get("cwd"),
                                    })
                                    if sum(row.get("responseTo") == "thread/start" for row in rows) >= 2:
                                        complete.set()
                        await target.send(raw)

                tasks = (asyncio.create_task(forward(downstream, upstream, "cli->server")),
                         asyncio.create_task(forward(upstream, downstream, "server->cli")))
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        async def attach(_thread_id):
            return None

        try:
            await wait_port(upstream_port, app_server)
            async with serve(recorder, "127.0.0.1", recorder_port, origins=[None],
                             compression=None, max_size=None):
                relay = Relay(f"ws://127.0.0.1:{recorder_port}", attach, question_sync=False)
                bridge_context = (serve(relay.handle, "127.0.0.1", bridge_port, origins=[None],
                                        compression=None, max_size=None) if args.through_bridge else None)
                if bridge_context is not None:
                    await bridge_context.__aenter__()
                try:
                    endpoint = f"ws://127.0.0.1:{bridge_port if args.through_bridge else recorder_port}"
                    command = (cb.cli_arguments(args.cli, ["--no-alt-screen"], endpoint, str(project))
                               if args.through_bridge else
                               [args.cli, "--remote", endpoint, "--cd", str(project), "--no-alt-screen"])
                    master, slave = pty.openpty()
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
                    child = subprocess.Popen(command, cwd=project, env=environment, stdin=slave,
                                             stdout=slave, stderr=slave, close_fds=True)
                    os.close(slave)
                    try:
                        os.set_blocking(master, False)
                        first = False
                        first_ready_at = None
                        deadline = time.monotonic() + args.timeout
                        while time.monotonic() < deadline and child.poll() is None:
                            try:
                                os.read(master, 8192)
                            except BlockingIOError:
                                pass
                            responses = sum(row.get("responseTo") == "thread/start" for row in rows)
                            if responses and first_ready_at is None:
                                first_ready_at = time.monotonic()
                            if (first_ready_at is not None and time.monotonic() - first_ready_at >= 2
                                    and not first):
                                for byte in b"/new\r":
                                    os.write(master, bytes([byte]))
                                    await asyncio.sleep(0.05)
                                first = True
                            if complete.is_set():
                                break
                            await asyncio.sleep(0.1)
                        os.write(master, b"\x03")
                        await asyncio.sleep(0.2)
                        if child.poll() is None:
                            os.write(master, b"\x03")
                        try:
                            child.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            child.terminate()
                            child.wait(timeout=2)
                    finally:
                        os.close(master)
                finally:
                    if bridge_context is not None:
                        await bridge_context.__aexit__(None, None, None)
        finally:
            if app_server.returncode is None:
                app_server.terminate()
                try:
                    await asyncio.wait_for(app_server.wait(), 2)
                except asyncio.TimeoutError:
                    app_server.kill()
                    await app_server.wait()
            log.close()

        starts = [row for row in rows if row.get("method") == "thread/start"]
        responses = [row for row in rows if row.get("responseTo") == "thread/start"]
        output = {
            "mode": "bridge" if args.through_bridge else "remote",
            "cli": args.cli,
            "cliVersion": cli_version,
            "invocationCwd": str(project),
            "serverCwd": str(root),
            "rpc": rows,
        }
        if len(starts) != 2 or len(responses) != 2:
            output["methodTrace"] = methods
        print(json.dumps(output, ensure_ascii=False, indent=2))
        if len(starts) != 2 or len(responses) != 2:
            raise SystemExit("expected initial and /new thread/start requests and responses")
        if any(row.get("cwd") != str(project) for row in starts + responses):
            raise SystemExit("cwd chain did not remain in the invoking project")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", required=True)
    parser.add_argument("--through-bridge", action="store_true")
    parser.add_argument("--timeout", type=float, default=30)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
