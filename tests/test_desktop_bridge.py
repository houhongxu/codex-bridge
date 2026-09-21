import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop_bridge import (DesktopAttacher, DesktopSubscriptions, PersistedAttachments,
                            Relay, relay_authorized, rollout_ready)
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve


THREAD_A = "00000000-0000-4000-8000-000000000001"
THREAD_B = "00000000-0000-4000-8000-000000000002"


class RelayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for thread_id in (THREAD_A, THREAD_B):
            (self.root / thread_id).write_text(json.dumps({
                "type": "session_meta", "payload": {"id": thread_id}}) + "\n")

    def thread(self, thread_id):
        return {"id": thread_id, "ephemeral": False, "path": str(self.root / thread_id)}

    async def test_plain_address_accepts_bearer_and_rejects_missing_or_wrong_token(self):
        accepted = []

        async def handler(ws):
            if not relay_authorized(ws.request, "test-token"):
                await ws.close(1008, "Unknown local relay")
                return
            accepted.append(True)
            await ws.send("accepted")

        async with serve(handler, "127.0.0.1", 0, origins=[None]) as server:
            endpoint = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            for header in [None, "Bearer wrong"]:
                headers = {} if header is None else {"Authorization": header}
                async with connect(endpoint, proxy=None, additional_headers=headers) as client:
                    await client.wait_closed()
                    self.assertEqual(client.close_code, 1008)
            async with connect(endpoint, proxy=None, additional_headers={"Authorization": "Bearer test-token"}) as client:
                self.assertEqual(await client.recv(), "accepted")
            self.assertEqual(accepted, [True])

    async def test_routes_own_threads_and_preserves_rpc_and_approvals(self):
        received = []
        attached = []
        attached_event = asyncio.Event()
        progress = '{"method":"item/agentMessage/delta","params":{"threadId":"' + THREAD_A + '","delta":"working"}}'

        async def attach(thread_id):
            attached.append(thread_id)
            attached_event.set()

        async def backend(ws):
            async for raw in ws:
                received.append(raw)
                message = json.loads(raw)
                if message.get("method") == "thread/start":
                    # Global notifications for a different CLI must not attach it.
                    await ws.send(json.dumps({"method": "thread/started", "params": {"thread": {"id": THREAD_B}}}))
                    await ws.send(json.dumps({"id": message["id"], "result": {"thread": self.thread(THREAD_A)}}))
                elif message.get("method") == "turn/start":
                    await ws.send(json.dumps({"id": message["id"], "result": {"turn": {"id": "turn-1"}}}))
                    await ws.send('{"id":"approval-1","method":"item/commandExecution/requestApproval","params":{"threadId":"' + THREAD_A + '"}}')
                elif message.get("id") == "approval-1":
                    await ws.send('{"method":"serverRequest/resolved","params":{"requestId":"approval-1"}}')
                    await ws.send(progress)

        async with serve(backend, "127.0.0.1", 0) as server:
            endpoint = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            relay = Relay(endpoint, attach)
            async with serve(relay.handle, "127.0.0.1", 0) as front:
                async with connect(f"ws://127.0.0.1:{front.sockets[0].getsockname()[1]}", proxy=None) as client:
                    start = '{"id":1, "method":"thread/start", "params":{"cwd":"/a project"}}'
                    await client.send(start)
                    notification = json.loads(await client.recv())
                    self.assertEqual(notification["params"]["thread"]["id"], THREAD_B)
                    self.assertEqual(json.loads(await client.recv())["id"], 1)
                    await asyncio.wait_for(attached_event.wait(), 2)
                    self.assertEqual(attached, [THREAD_A])
                    attached_event.clear()
                    turn = json.dumps({"id": 2, "method": "turn/start", "params": {"threadId": THREAD_A, "input": []}})
                    await client.send(turn)
                    self.assertEqual(json.loads(await client.recv())["id"], 2)
                    approval = json.loads(await client.recv())
                    self.assertEqual(approval["id"], "approval-1")
                    answer = '{"id":"approval-1", "result":{"decision":"decline"}}'
                    await client.send(answer)
                    await client.recv()
                    self.assertEqual(await client.recv(), progress)
                    self.assertEqual(received, [start, turn, answer])
                    await asyncio.wait_for(attached_event.wait(), 2)
                    self.assertEqual(attached, [THREAD_A, THREAD_A])

    async def test_concurrent_clients_do_not_mix_response_ids_or_error_results(self):
        attached = []
        events = {thread_id: asyncio.Event() for thread_id in (THREAD_A, THREAD_B)}

        async def attach(thread_id):
            attached.append(thread_id)
            events[thread_id].set()

        async def backend(ws):
            async for raw in ws:
                message = json.loads(raw)
                params = message["params"]
                response = {"id": message["id"], "result": {"thread": self.thread(params["threadId"])}}
                if params.get("fail"):
                    response = {"id": message["id"], "error": {"code": -1, "message": "test failure"}}
                await ws.send(json.dumps(response))

        async with serve(backend, "127.0.0.1", 0) as server:
            relay = Relay(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", attach)
            async with serve(relay.handle, "127.0.0.1", 0) as front:
                endpoint = f"ws://127.0.0.1:{front.sockets[0].getsockname()[1]}"

                async def resume(thread_id, fail=False):
                    async with connect(endpoint, proxy=None) as client:
                        await client.send(json.dumps({"id": 1, "method": "thread/resume", "params": {"threadId": thread_id, "fail": fail}}))
                        response = json.loads(await client.recv())
                        if not fail:
                            await asyncio.wait_for(events[thread_id].wait(), 2)
                        return response

                a, b = await asyncio.gather(resume(THREAD_A), resume(THREAD_B))
                self.assertEqual(a["result"]["thread"]["id"], THREAD_A)
                self.assertEqual(b["result"]["thread"]["id"], THREAD_B)
                self.assertCountEqual(attached, [THREAD_A, THREAD_B])
                error = await resume(THREAD_A, True)
                self.assertIn("error", error)
                self.assertEqual(len(attached), 2)

    async def test_concurrent_cli_cwds_and_new_threads_pass_through_without_rewriting(self):
        received = []

        async def backend(ws):
            async for raw in ws:
                message = json.loads(raw)
                params = message.get("params", {})
                received.append(dict(params))
                cwd = params.get("cwd")
                thread_id = THREAD_A if cwd == "/project A" else THREAD_B
                thread = self.thread(thread_id)
                thread["cwd"] = cwd if cwd is not None else "/backend service"
                await ws.send(json.dumps({"id": message["id"], "result": {"thread": thread}}))

        async def attach(_thread_id):
            pass

        async with serve(backend, "127.0.0.1", 0) as server:
            relay = Relay(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", attach)
            async with serve(relay.handle, "127.0.0.1", 0) as front:
                endpoint = f"ws://127.0.0.1:{front.sockets[0].getsockname()[1]}"

                async def start_twice(cwd):
                    results = []
                    async with connect(endpoint, proxy=None) as client:
                        for request_id in (1, 2):
                            await client.send(json.dumps({
                                "id": request_id, "method": "thread/start", "params": {"cwd": cwd}}))
                            results.append(json.loads(await client.recv())["result"]["thread"]["cwd"])
                    return results

                a, b = await asyncio.gather(start_twice("/project A"), start_twice("/project B"))
                self.assertEqual(a, ["/project A", "/project A"])
                self.assertEqual(b, ["/project B", "/project B"])
                self.assertCountEqual([params.get("cwd") for params in received],
                                      ["/project A", "/project A", "/project B", "/project B"])

                async with connect(endpoint, proxy=None) as client:
                    await client.send(json.dumps({"id": 3, "method": "thread/start", "params": {}}))
                    response = json.loads(await client.recv())
                    self.assertEqual(response["result"]["thread"]["cwd"], "/backend service")
                self.assertNotIn("cwd", received[-1])

    async def test_ephemeral_and_unknown_threads_never_trigger_desktop_links(self):
        attached = []
        received = []

        async def attach(thread_id):
            attached.append(thread_id)

        async def backend(ws):
            async for raw in ws:
                received.append(raw)
                message = json.loads(raw)
                params = message.get("params", {})
                if message["method"] == "thread/start":
                    thread = {"id": THREAD_B, "path": str(self.root / THREAD_B)}
                    if params.get("omitEphemeral") is not True:
                        thread["ephemeral"] = params.get("responseEphemeral", True)
                    result = {"thread": thread}
                else:
                    result = {"turn": {"id": "temporary-turn"}}
                await ws.send(json.dumps({"id": message["id"], "result": result}))

        async with serve(backend, "127.0.0.1", 0) as server:
            relay = Relay(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", attach)
            async with serve(relay.handle, "127.0.0.1", 0) as front:
                async with connect(f"ws://127.0.0.1:{front.sockets[0].getsockname()[1]}", proxy=None) as client:
                    sent = []
                    async def request(request_id, method, params):
                        raw = json.dumps({"id": request_id, "method": method, "params": params})
                        sent.append(raw)
                        await client.send(raw)
                        response = json.loads(await client.recv())
                        self.assertEqual(response["id"], request_id)

                    # Reproduces the reported bug: start was ignored correctly,
                    # but the very next turn/start still opened the temporary ID.
                    await request(1, "thread/start", {"ephemeral": True})
                    self.assertEqual(attached, [])
                    await request(2, "turn/start", {"threadId": THREAD_B, "input": []})
                    self.assertEqual(attached, [])
                    # No successful start/resume/fork for this ID on this client.
                    await request(3, "turn/start", {"threadId": THREAD_A, "input": []})
                    self.assertEqual(attached, [])
                    # Missing type metadata must not make a task attachable.
                    await request(4, "thread/start", {"omitEphemeral": True})
                    await request(5, "turn/start", {"threadId": THREAD_B, "input": []})
                    self.assertEqual(attached, [])
                    # Explicit temporary requests remain temporary even if a
                    # mismatched response unexpectedly says otherwise.
                    await request(6, "thread/start", {"ephemeral": True, "responseEphemeral": False})
                    await request(7, "turn/start", {"threadId": THREAD_B, "input": []})
                    self.assertEqual(attached, [])
                    self.assertEqual(received, sent)

    async def test_new_thread_waits_for_first_turn_history_without_blocking_rpc(self):
        path = self.root / THREAD_A
        path.unlink()
        attached = asyncio.Event()
        cancelled = asyncio.Event()
        relay_closed = asyncio.Event()
        received = []
        calls = []

        async def attach(thread_id):
            self.assertTrue(rollout_ready(path))
            calls.append(thread_id)
            attached.set()
            try:
                await asyncio.Event().wait()  # Desktop subscription is slow.
            finally:
                cancelled.set()

        async def backend(ws):
            async for raw in ws:
                received.append(raw)
                msg = json.loads(raw)
                if msg.get("method") == "thread/start":
                    result = {"thread": self.thread(THREAD_A)}
                else:
                    result = {"turn": {"id": "test-turn"}}
                await ws.send(json.dumps({"id": msg["id"], "result": result}))
                if msg["id"] == 2:
                    await ws.send(json.dumps({"id": "approval", "method": "item/commandExecution/requestApproval",
                                              "params": {"threadId": THREAD_A}}))

        async with serve(backend, "127.0.0.1", 0) as server:
            relay = Relay(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", attach)

            async def handle(ws):
                try:
                    await relay.handle(ws)
                finally:
                    relay_closed.set()

            async with serve(handle, "127.0.0.1", 0) as front:
                async with connect(f"ws://127.0.0.1:{front.sockets[0].getsockname()[1]}", proxy=None) as cli:
                    start = '{"id":1,"method":"thread/start","params":{}}'
                    turn = json.dumps({"id": 2, "method": "turn/start", "params": {"threadId": THREAD_A}})
                    answer = '{"id":"approval","result":{"decision":"decline"}}'
                    await cli.send(start)
                    self.assertEqual(json.loads(await asyncio.wait_for(cli.recv(), 2))["id"], 1)
                    self.assertFalse(attached.is_set())
                    await cli.send(turn)
                    self.assertEqual(json.loads(await asyncio.wait_for(cli.recv(), 2))["id"], 2)
                    self.assertEqual(json.loads(await cli.recv())["id"], "approval")
                    await cli.send(answer)
                    self.assertEqual(json.loads(await asyncio.wait_for(cli.recv(), 2))["id"], "approval")
                    self.assertEqual(received, [start, turn, answer])
                    self.assertFalse(attached.is_set())
                    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": THREAD_A}}) + "\n")
                    await asyncio.wait_for(attached.wait(), 2)
                    # A pending desktop open must not delay another CLI input.
                    await cli.send(turn)
                    self.assertEqual(json.loads(await asyncio.wait_for(cli.recv(), 2))["id"], 2)
                    self.assertEqual(calls, [THREAD_A])
                await asyncio.wait_for(relay_closed.wait(), 2)
                self.assertTrue(cancelled.is_set())


class PersistedAttachmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "rollout.jsonl"
        self.attach = AsyncMock()
        self.manager = PersistedAttachments(self.attach)
        self.manager.POLL_INTERVAL = 0.001
        self.addAsyncCleanup(self.manager.close)
        self.thread = {"id": THREAD_A, "ephemeral": False, "path": str(self.path)}

    def persist(self):
        self.path.write_text(json.dumps({"type": "session_meta", "payload": {"id": THREAD_A}}) + "\n")

    async def finish(self):
        await asyncio.wait_for(asyncio.gather(*self.manager.tasks.values()), 2)

    def test_missing_empty_partial_malformed_and_non_regular_rollouts_are_not_ready(self):
        self.assertFalse(rollout_ready(self.path))
        for value in [b"", b'{"type":"session_meta","payload":{}}', b"broken\n", b"\xff\n",
                      b'{"type":"event_msg","payload":{}}\n', b'{"type":"session_meta","payload":null}\n']:
            self.path.write_bytes(value)
            self.assertFalse(rollout_ready(self.path), value)
        self.path.unlink()
        self.path.mkdir()
        self.assertFalse(rollout_ready(self.path))
        self.path.rmdir()
        os.mkfifo(self.path)
        self.assertFalse(rollout_ready(self.path))
        self.path.unlink()
        self.persist()
        self.assertTrue(rollout_ready(self.path))
        with patch("desktop_bridge.os.open", side_effect=PermissionError):
            self.assertFalse(rollout_ready(self.path))

    async def test_empty_thread_stays_idle_then_waits_for_complete_metadata(self):
        self.manager.track(self.thread, False)
        self.assertEqual(self.manager.tasks, {})
        self.manager.schedule(THREAD_A)
        self.manager.schedule(THREAD_A)
        self.assertEqual(len(self.manager.tasks), 1)
        self.path.write_text('{"type":"session_meta","payload":{}}')
        await asyncio.sleep(0.01)
        self.attach.assert_not_awaited()
        with self.path.open("a") as stream:
            stream.write("\n")
        await self.finish()
        self.attach.assert_awaited_once_with(THREAD_A)

    async def test_persisted_resume_or_fork_can_attach_immediately(self):
        self.persist()
        self.manager.track(self.thread, False)
        await self.finish()
        self.attach.assert_awaited_once_with(THREAD_A)
        # A later turn still invokes the existing subscription reuse/recovery logic.
        self.manager.schedule(THREAD_A)
        await self.finish()
        self.assertEqual(self.attach.await_count, 2)

    async def test_missing_path_and_ephemeral_metadata_never_attach_even_after_turn(self):
        self.persist()
        for path in [None, "", "relative.jsonl", 123]:
            self.manager.track(dict(self.thread, path=path), False)
            self.manager.schedule(THREAD_A)
            self.assertEqual(self.manager.tasks, {})
        for thread, requested in [(dict(self.thread, ephemeral=True), False),
                                  (dict(self.thread, ephemeral=None), False), (self.thread, True)]:
            self.manager.track(thread, requested)
            self.manager.schedule(THREAD_A)
            self.assertEqual(self.manager.tasks, {})
        self.manager.schedule(THREAD_B)
        self.attach.assert_not_awaited()

    async def test_timeout_skips_open_and_later_turn_retries(self):
        self.manager.WAIT_TIMEOUT = 0
        self.manager.track(self.thread, False)
        self.manager.schedule(THREAD_A)
        await self.finish()
        self.attach.assert_not_awaited()
        self.persist()
        self.manager.schedule(THREAD_A)
        await self.finish()
        self.attach.assert_awaited_once_with(THREAD_A)

    async def test_reclassification_and_disconnect_cancel_pending_open(self):
        self.manager.track(self.thread, False)
        self.manager.schedule(THREAD_A)
        await asyncio.sleep(0)
        self.manager.track(dict(self.thread, ephemeral=True), False)
        self.persist()
        await self.finish()
        self.attach.assert_not_awaited()
        self.path.unlink()
        self.manager.track(self.thread, False)
        self.manager.schedule(THREAD_A)
        await self.manager.close()
        self.persist()
        self.assertEqual(self.manager.tasks, {})
        self.attach.assert_not_awaited()

    async def test_open_failure_is_isolated_and_can_be_retried(self):
        self.persist()
        self.attach.side_effect = OSError("synthetic open failure")
        with patch("sys.stderr"):
            self.manager.track(self.thread, False)
            await self.finish()
        self.assertEqual(self.manager.tasks, {})
        self.attach.side_effect = None
        self.manager.schedule(THREAD_A)
        await self.finish()
        self.assertEqual(self.attach.await_count, 2)


class DesktopAttachmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.logs = self.home / "Library/Logs/com.openai.codex/2026/09/16"
        self.logs.mkdir(parents=True)
        self.path = self.logs / f"codex-desktop-session-one-{os.getpid()}-t0-i1-120000-0.log"
        self.path.touch()
        self.tick = 0
        self.attacher = DesktopAttacher("/Applications/Test.app", self.home)
        self.attacher.CONFIRM_TIMEOUT = 0
        self.now = 1000
        timer = patch("desktop_bridge.time", Mock(monotonic=lambda: self.now))
        timer.start()
        self.addCleanup(timer.stop)
        self.opened = []
        self.confirm_on_open = True
        self.open_code = 0

        async def open_task(*args, **kwargs):
            thread_id = args[-1].rsplit("/", 1)[1]
            self.opened.append(thread_id)
            if self.confirm_on_open and self.open_code == 0:
                self.emit(f"maybe_resume_success conversationId={thread_id} assignedStreamRole=owner")
            return Mock(wait=AsyncMock(return_value=self.open_code))

        opener = patch("desktop_bridge.asyncio.create_subprocess_exec", side_effect=open_task)
        opener.start()
        self.addCleanup(opener.stop)

    def emit(self, event, complete=True):
        self.tick += 1
        with self.path.open("a") as stream:
            stream.write(f"2026-09-16T12:00:00.{self.tick:03d}Z info [test] {event}" + ("\n" if complete else ""))

    async def test_continuous_turns_and_switching_views_open_only_once(self):
        await self.attacher(THREAD_A)
        self.emit(f"thread_stream_view_activity_changed conversationId={THREAD_A} active=false resumeState=resumed streamRole=owner")
        for elapsed in [3, 60, 3600]:
            self.now += elapsed
            await self.attacher(THREAD_A)
        self.assertEqual(self.opened, [THREAD_A])
        record = self.home / f"Library/Application Support/Codex CLI Bridge/attachments/{THREAD_A}.json"
        self.assertEqual(json.loads(record.read_text())["status"], "subscription_confirmed")

    async def test_unsubscribe_reopens_only_the_affected_task(self):
        await self.attacher(THREAD_A)
        await self.attacher(THREAD_B)
        self.emit(f"inactive_thread_unsubscribed conversationId={THREAD_A} status=unsubscribed")
        await self.attacher(THREAD_B)
        await self.attacher(THREAD_A)
        await self.attacher(THREAD_A)
        self.assertEqual(self.opened, [THREAD_A, THREAD_B, THREAD_A])

    async def test_local_reconnect_invalidates_but_remote_host_does_not(self):
        await self.attacher(THREAD_A)
        self.emit("app_server_connection.state_changed hostId=remote next=disconnected")
        await self.attacher(THREAD_A)
        self.assertEqual(self.opened, [THREAD_A])
        self.emit("app_server_connection.state_changed hostId=local next=disconnected")
        self.emit("app_server_connection.state_changed hostId=local next=connected")
        await self.attacher(THREAD_A)
        await self.attacher(THREAD_A)
        self.assertEqual(self.opened, [THREAD_A, THREAD_A])

    async def test_desktop_restart_reopens_and_old_records_are_not_trusted(self):
        await self.attacher(THREAD_A)
        self.path = self.logs / f"codex-desktop-session-two-{os.getpid()}-t0-i1-120100-0.log"
        self.emit("app_server_connection.state_changed hostId=local next=connected")
        await self.attacher(THREAD_A)
        self.assertEqual(self.opened, [THREAD_A, THREAD_A])

    async def test_cli_restart_reuses_current_desktop_evidence(self):
        await self.attacher(THREAD_A)
        another = DesktopAttacher("/Applications/Test.app", self.home)
        await another(THREAD_A)
        self.assertEqual(self.opened, [THREAD_A])
        self.emit(f"inactive_thread_unsubscribed conversationId={THREAD_A} status=unsubscribed")
        another = DesktopAttacher("/Applications/Test.app", self.home)
        await another(THREAD_A)
        self.assertEqual(self.opened, [THREAD_A, THREAD_A])

    async def test_unconfirmed_open_backs_off_and_late_confirmation_is_reused(self):
        self.confirm_on_open = False
        await self.attacher(THREAD_A)
        self.now += 10
        await self.attacher(THREAD_A)
        self.assertEqual(self.opened, [THREAD_A])
        self.now += 60
        await self.attacher(THREAD_A)
        self.assertEqual(self.opened, [THREAD_A, THREAD_A])
        self.emit(f"maybe_resume_success conversationId={THREAD_A} assignedStreamRole=owner")
        self.now += 100
        await self.attacher(THREAD_A)
        self.assertEqual(self.opened, [THREAD_A, THREAD_A])

    async def test_failed_open_can_recover_without_opening_on_each_input(self):
        self.open_code = 1
        await self.attacher(THREAD_A)
        self.now += 3
        await self.attacher(THREAD_A)
        self.assertEqual(self.opened, [THREAD_A])
        self.open_code = 0
        self.now += 60
        await self.attacher(THREAD_A)
        await self.attacher(THREAD_A)
        self.assertEqual(self.opened, [THREAD_A, THREAD_A])

    async def test_invalid_thread_never_opens_desktop(self):
        await self.attacher("invalid")
        await self.attacher(None)
        self.assertEqual(self.opened, [])

    def test_partial_lines_and_success_then_unsubscribe_in_one_read(self):
        state = self.attacher.subscriptions
        self.emit(f"maybe_resume_success conversationId={THREAD_A} assignedStreamRole=owner", complete=False)
        state.refresh()
        self.assertNotIn(THREAD_A, state.confirmed)
        with self.path.open("a") as stream:
            stream.write("\n")
        state.refresh()
        self.assertIn(THREAD_A, state.confirmed)
        self.emit(f"maybe_resume_success conversationId={THREAD_B} assignedStreamRole=owner")
        self.emit(f"inactive_thread_unsubscribed conversationId={THREAD_A} status=unsubscribed")
        state.refresh()
        self.assertNotIn(THREAD_A, state.confirmed)
        self.assertIn(THREAD_B, state.confirmed)

    async def test_rotation_keeps_subscription_but_reads_unsubscribe_in_new_file(self):
        await self.attacher(THREAD_A)
        self.path = self.logs / f"codex-desktop-session-one-{os.getpid()}-t0-i1-120000-1.log"
        self.emit("irrelevant_event detail=rotation")
        await self.attacher(THREAD_A)
        self.assertEqual(self.opened, [THREAD_A])
        self.emit(f"inactive_thread_unsubscribed conversationId={THREAD_A} status=unsubscribed")
        await self.attacher(THREAD_A)
        self.assertEqual(self.opened, [THREAD_A, THREAD_A])

    async def test_truncation_does_not_keep_old_confirmation(self):
        await self.attacher(THREAD_A)
        self.path.write_text("")
        await self.attacher(THREAD_A)
        self.assertEqual(self.opened, [THREAD_A, THREAD_A])

    def test_dead_desktop_does_not_restore_historical_confirmation(self):
        self.emit(f"maybe_resume_success conversationId={THREAD_A} assignedStreamRole=owner")
        state = self.attacher.subscriptions
        state.refresh()
        self.assertIn(THREAD_A, state.confirmed)
        with patch("desktop_bridge.os.kill", side_effect=ProcessLookupError):
            state.refresh()
        self.assertEqual(state.confirmed, set())

    async def test_missing_rotations_do_not_keep_unverifiable_subscription(self):
        await self.attacher(THREAD_A)
        # More than four new files: the unsubscribe may be outside the retained
        # scan window. Reattach conservatively instead of trusting cached state.
        for i in range(1, 6):
            self.path = self.logs / f"codex-desktop-session-one-{os.getpid()}-t0-i1-120000-{i}.log"
            self.emit("irrelevant_event detail=rotation")
        await self.attacher(THREAD_A)
        self.assertEqual(self.opened, [THREAD_A, THREAD_A])

    def test_skipped_log_range_does_not_replay_older_confirmation(self):
        self.emit(f"maybe_resume_success conversationId={THREAD_A} assignedStreamRole=owner")
        self.path = self.logs / f"codex-desktop-session-one-{os.getpid()}-t0-i1-120000-1.log"
        self.emit(f"inactive_thread_unsubscribed conversationId={THREAD_A} status=unsubscribed")
        for _ in range(8):
            self.emit("irrelevant_event detail=padding")
        state = self.attacher.subscriptions
        state.MAX_READ = 200
        state.refresh()
        self.assertNotIn(THREAD_A, state.confirmed)
