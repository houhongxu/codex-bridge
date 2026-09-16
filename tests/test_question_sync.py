import asyncio
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop_bridge import Relay
from question_sync import CLOSE, OPEN, QuestionSync, answered_ids, question_key
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve


THREAD = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
TURN = "00000000-0000-4000-8000-000000000003"


def question_event(count=1, method="item/completed", thread=THREAD, item_id="call-example"):
    return {"method": method, "params": {"threadId": thread, "turnId": TURN, "item": {
        "type": "agentMessage", "id": item_id, "delivery": "async", "phase": "final_answer",
        "text": "Original question text", "questions": [
            {"title": "Choose scope " + str(i), "options": ["Current task", "All tasks"]}
            for i in range(count)]}}}


def desktop_answer(indices=(0,), thread=THREAD, item_id="call-example"):
    values = [{"questionItemId": question_key(item_id, i), "question": "Choose scope " + str(i),
               "answer": "Current task"} for i in indices]
    return {"method": "item/completed", "params": {"threadId": thread, "turnId": TURN,
        "item": {"type": "userMessage", "id": "user-answer", "content": [{"type": "text",
                  "text": OPEN + "\n" + json.dumps(values) + "\n" + CLOSE}]}}}


def cli_answer(request, answer="Current task"):
    return {"id": request["id"], "result": {"answers": {"answer": {"answers": [answer]}}}}


class QuestionSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.front = []
        self.back = []
        self.send_front = AsyncMock(side_effect=self.front.append)
        self.send_back = AsyncMock(side_effect=self.back.append)
        self.sync = QuestionSync(self.send_front, self.send_back)
        self.sync.track_thread({"id": THREAD})
        self.addAsyncCleanup(self.sync.close)

    def create(self, **kwargs):
        return self.sync.from_backend(question_event(**kwargs))[1]

    async def flush(self):
        await asyncio.sleep(0)

    async def test_desktop_answer_dismisses_only_matching_question_without_submission(self):
        requests = self.create(count=2)
        original = desktop_answer(indices=(1,))
        forwarded, extra = self.sync.from_backend(original)
        self.assertIs(forwarded, original)
        self.assertEqual(extra, [{"method": "serverRequest/resolved", "params": {
            "threadId": THREAD, "requestId": requests[1]["id"]}}])
        self.assertEqual(len(self.sync.questions), 1)
        self.assertEqual(self.back, [])
        self.assertEqual(self.sync.from_backend(original)[1], [])

    async def test_other_thread_and_uncommitted_message_cannot_dismiss_question(self):
        self.create()
        for event in [desktop_answer(thread=OTHER), desktop_answer(item_id="different")]:
            self.assertEqual(self.sync.from_backend(event)[1], [])
        started = desktop_answer()
        started["method"] = "item/started"
        self.assertEqual(self.sync.from_backend(started)[1], [])
        self.assertEqual(len(self.sync.questions), 1)

    async def test_completed_answer_received_before_question_prevents_reopening(self):
        self.sync.from_backend(desktop_answer())
        self.assertEqual(self.create(), [])

    async def test_start_and_complete_keep_text_but_only_open_one_request(self):
        original = question_event(method="item/started")
        saved = copy.deepcopy(original)
        forwarded, extra = self.sync.from_backend(original)
        self.assertIsNone(forwarded["params"]["item"]["questions"])
        self.assertEqual(forwarded["params"]["item"]["text"], "Original question text")
        self.assertEqual(original, saved)
        self.assertEqual(extra, [])
        self.assertEqual(len(self.create()), 1)
        self.assertEqual(self.create(), [])

    async def test_cli_answer_steers_with_exact_identity_and_no_permission_overrides(self):
        event = question_event()
        event["params"]["item"]["questions"][0].pop("options")
        request = self.sync.from_backend(event)[1][0]
        self.assertIsNone(request["params"]["questions"][0]["options"])
        value = "中文答案\nwith notes"
        self.assertTrue(await self.sync.from_cli(cli_answer(request, value)))
        await self.flush()
        self.assertEqual(len(self.back), 1)
        rpc = self.back[0]
        self.assertEqual(rpc["method"], "turn/steer")
        self.assertEqual(rpc["params"]["expectedTurnId"], TURN)
        self.assertEqual(set(rpc["params"]), {"threadId", "expectedTurnId", "input", "clientUserMessageId"})
        text = rpc["params"]["input"][0]["text"]
        answer = json.loads(text[len(OPEN):-len(CLOSE)])[0]
        self.assertEqual(answer, {"questionItemId": question_key("call-example", 0),
                                 "question": "Choose scope 0", "answer": value})
        self.assertEqual(self.sync.from_backend({"id": rpc["id"], "result": {"turnId": TURN}}), (None, []))
        await asyncio.wait_for(asyncio.gather(*self.sync.tasks), 1)
        self.assertEqual(self.front[-1]["method"], "serverRequest/resolved")
        self.assertEqual(self.sync.questions, {})

    async def test_cli_answer_starts_new_turn_after_original_turn_completed(self):
        request = self.create()[0]
        self.sync.from_backend({"method": "turn/completed", "params": {
            "threadId": THREAD, "turn": {"id": TURN, "status": "completed"}}})
        await self.sync.from_cli(cli_answer(request))
        await self.flush()
        self.assertEqual(self.back[0]["method"], "turn/start")
        self.assertNotIn("expectedTurnId", self.back[0]["params"])

    async def test_known_desktop_answer_and_double_click_never_submit_again(self):
        request = self.create()[0]
        self.sync.from_backend(desktop_answer())
        self.assertTrue(await self.sync.from_cli(cli_answer(request)))
        self.assertTrue(await self.sync.from_cli(cli_answer(request)))
        await self.flush()
        self.assertEqual(self.back, [])
        second = self.create(item_id="next-question")[0]
        await self.sync.from_cli(cli_answer(second))
        await self.sync.from_cli(cli_answer(second))
        await self.flush()
        self.assertEqual(len(self.back), 1)

    async def test_empty_cancel_does_not_become_an_answer(self):
        request = self.create()[0]
        await self.sync.from_cli({"id": request["id"], "result": {"answers": {}}})
        self.assertEqual(self.back, [])
        self.assertEqual(self.sync.questions, {})
        self.assertEqual(self.front[-1]["method"], "serverRequest/resolved")

    async def test_desktop_receipt_before_scheduled_cli_submission_wins(self):
        request = self.create()[0]
        await self.sync.from_cli(cli_answer(request))
        self.sync.from_backend(desktop_answer())
        await self.flush()
        self.assertEqual(self.back, [])

    async def test_real_requests_approvals_and_unknown_notifications_pass_unchanged(self):
        for message in [
            {"id": "real-request", "result": {"decision": "accept"}},
            {"id": "real-request", "result": {"answers": {}}},
            {"id": "real-request", "method": "item/permissions/requestApproval", "params": {"threadId": THREAD}},
            {"method": "serverRequest/resolved", "params": {"threadId": THREAD, "requestId": "real-request"}},
        ]:
            self.assertFalse(await self.sync.from_cli(message))
            forwarded, extra = self.sync.from_backend(message)
            self.assertIs(forwarded, message)
            self.assertEqual(extra, [])

    async def test_nullable_and_non_object_rpc_results_do_not_break_passthrough(self):
        for value in [None, False, "ok", [], 0]:
            response = {"id": "ordinary-request", "result": value}
            self.assertEqual(self.sync.from_backend(response), (response, []))
        request = self.create()[0]
        self.assertTrue(await self.sync.from_cli({"id": request["id"], "result": None}))
        self.assertEqual(self.back, [])
        self.assertEqual(self.sync.questions, {})

    async def test_rejection_warns_and_does_not_retry(self):
        request = self.create()[0]
        await self.sync.from_cli(cli_answer(request))
        await self.flush()
        rpc = self.back[0]
        self.sync.from_backend({"id": rpc["id"], "error": {"code": -32600, "message": "No active turn"}})
        await asyncio.wait_for(asyncio.gather(*self.sync.tasks), 1)
        self.assertEqual(len(self.back), 1)
        self.assertEqual(self.front[-1]["method"], "warning")
        self.assertEqual(self.sync.questions, {})

    async def test_timeout_does_not_leak_late_rpc_response_or_retry(self):
        self.sync.RPC_TIMEOUT = 0.01
        request = self.create()[0]
        await self.sync.from_cli(cli_answer(request))
        await asyncio.wait_for(asyncio.gather(*self.sync.tasks), 1)
        self.assertEqual(len(self.back), 1)
        self.assertEqual(self.front[-1]["method"], "warning")
        self.assertEqual(self.sync.from_backend({"id": self.back[0]["id"], "result": {}}), (None, []))

    async def test_multiple_connections_do_not_consume_each_others_responses(self):
        request = self.create()[0]
        other = QuestionSync(AsyncMock(), AsyncMock())
        self.addAsyncCleanup(other.close)
        self.assertFalse(await other.from_cli(cli_answer(request)))

    async def test_history_keeps_text_and_nested_tool_payload_but_no_stale_widgets(self):
        agent = question_event()["params"]["item"]
        tool = {"type": "dynamicToolCall", "arguments": {"nested": copy.deepcopy(agent)}}
        await self.sync.from_cli({"id": 7, "method": "thread/items/list", "params": {"threadId": THREAD}})
        response = {"id": 7, "result": {"data": [{"turnId": TURN, "item": agent}, {"turnId": TURN, "item": tool}]}}
        forwarded, extra = self.sync.from_backend(response)
        self.assertIsNone(forwarded["result"]["data"][0]["item"]["questions"])
        self.assertEqual(forwarded["result"]["data"][1]["item"], tool)
        self.assertEqual(extra, [])
        self.assertEqual(self.sync.questions, {})

    async def test_malformed_or_unrelated_question_retains_native_behavior(self):
        event = question_event(thread=OTHER)
        self.assertIs(self.sync.from_backend(event)[0], event)
        for questions in [[], [{"title": None}], [{"title": "test", "options": []}]]:
            event = question_event()
            event["params"]["item"]["questions"] = questions
            self.assertIs(self.sync.from_backend(event)[0], event)

    def test_agent_quotes_and_malformed_reply_are_not_answer_receipts(self):
        item = desktop_answer()["params"]["item"]
        self.assertEqual(answered_ids(item), [question_key("call-example", 0)])
        item["type"] = "agentMessage"
        self.assertEqual(answered_ids(item), [])
        item["type"] = "userMessage"
        item["content"][0]["text"] = "Example: " + item["content"][0]["text"]
        self.assertEqual(answered_ids(item), [])


class QuestionRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_wire_flow_with_cli_answer_and_desktop_answer(self):
        received = []
        answer_rpc = asyncio.Event()

        async def backend(ws):
            async for raw in ws:
                msg = json.loads(raw)
                received.append(msg)
                if msg.get("method") == "thread/resume":
                    await ws.send(json.dumps({"id": msg["id"], "result": {"thread": {"id": THREAD, "ephemeral": False}}}))
                    await ws.send(json.dumps(question_event(count=2)))
                    await ws.send(json.dumps(desktop_answer(indices=(0,))))
                elif msg.get("method") == "turn/steer":
                    answer_rpc.set()
                    await ws.send(json.dumps({"id": msg["id"], "result": {"turnId": TURN}}))

        async with serve(backend, "127.0.0.1", 0) as server:
            relay = Relay(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", AsyncMock())
            async with serve(relay.handle, "127.0.0.1", 0) as front:
                async with connect(f"ws://127.0.0.1:{front.sockets[0].getsockname()[1]}", proxy=None) as cli:
                    await cli.send(json.dumps({"id": 1, "method": "thread/resume", "params": {"threadId": THREAD}}))
                    await cli.recv()
                    item = json.loads(await cli.recv())
                    self.assertIsNone(item["params"]["item"]["questions"])
                    first, second = [json.loads(await cli.recv()) for _ in range(2)]
                    await cli.recv()  # Original desktop answer.
                    resolved = json.loads(await cli.recv())
                    self.assertEqual(resolved["params"]["requestId"], first["id"])
                    await cli.send(json.dumps(cli_answer(first)))  # Stale answer is consumed.
                    await cli.send(json.dumps(cli_answer(second)))
                    await asyncio.wait_for(answer_rpc.wait(), 2)
                    resolved = json.loads(await asyncio.wait_for(cli.recv(), 2))
                    self.assertEqual(resolved["params"]["requestId"], second["id"])
        self.assertEqual([m.get("method") for m in received], ["thread/resume", "turn/steer"])

    async def test_opt_out_preserves_native_question_and_rpc(self):
        event = json.dumps(question_event())
        async def backend(ws):
            await ws.recv()
            await ws.send(event)
            await ws.wait_closed()
        async with serve(backend, "127.0.0.1", 0) as server:
            relay = Relay(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", AsyncMock(), question_sync=False)
            async with serve(relay.handle, "127.0.0.1", 0) as front:
                async with connect(f"ws://127.0.0.1:{front.sockets[0].getsockname()[1]}", proxy=None) as cli:
                    await cli.send('{"id":1,"method":"initialize","params":{}}')
                    self.assertEqual(await cli.recv(), event)
