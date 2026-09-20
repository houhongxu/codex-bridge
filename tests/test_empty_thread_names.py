import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop_bridge import EmptyThreadNames, Relay, rollout_ready
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

THREAD = '00000000-0000-4000-8000-000000000001'
OTHER = '00000000-0000-4000-8000-000000000002'


class EmptyThreadNameTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'history.jsonl'
        self.backend = AsyncMock()
        self.cli = AsyncMock()
        self.names = EmptyThreadNames(self.backend, self.cli)
        self.names.TIMEOUT = 0.01
        self.addAsyncCleanup(self.names.close)
        self.thread = {'id': THREAD, 'path': str(self.path), 'historyMode': 'paginated', 'ephemeral': False}
        self.message = {'id': 7, 'method': 'thread/name/set', 'params': {'threadId': THREAD, 'name': 'empty'}}
        self.raw = json.dumps(self.message)

    async def test_timeout_rejects_naming_and_consumes_late_response(self):
        self.names.track(self.thread, False)
        await self.names.forward(self.raw, self.message)
        self.assertEqual(self.backend.await_count, 1)
        internal = json.loads(self.backend.call_args.args[0])
        self.assertEqual(internal['method'], 'thread/read')
        self.assertIn('first message', self.cli.call_args.args[0]['error']['message'])
        self.assertEqual(self.names.pending, {})
        self.assertTrue(self.names.response({'id': internal['id'], 'result': {}}))
        self.assertFalse(self.names.response({'id': 7, 'result': {}}))
        self.assertFalse(self.names.response({'id': internal['id'], 'method': 'approval'}))

    async def test_unsupported_read_does_not_publish_name(self):
        self.names.track(self.thread, False)
        async def respond(raw):
            self.names.response({'id': json.loads(raw)['id'], 'error': {'code': -32601}})
        self.backend.side_effect = respond
        await self.names.forward(self.raw, self.message)
        self.assertEqual(self.backend.await_count, 1)
        self.assertEqual(self.cli.call_args.args[0]['id'], 7)
        self.assertIn('error', self.cli.call_args.args[0])

    async def test_ephemeral_legacy_unknown_and_readable_history_pass_through(self):
        for changes, requested in [({'ephemeral': True}, False), ({}, True),
                                   ({'historyMode': 'legacy'}, False), ({'historyMode': None}, False),
                                   ({'path': 'relative'}, False), ({'id': OTHER}, False)]:
            with self.subTest(changes=changes, requested=requested):
                self.names.track({**self.thread, **changes}, requested)
                await self.names.forward(self.raw, self.message)
                self.backend.assert_awaited_with(self.raw)
        self.path.write_text('{"type":"session_meta","payload":{}}\n')
        self.names.track(self.thread, False)
        await self.names.forward(self.raw, self.message)
        self.backend.assert_awaited_with(self.raw)
        self.cli.assert_not_awaited()

    async def test_disconnect_cancels_preflight_without_naming(self):
        self.names.TIMEOUT = 10
        self.names.track(self.thread, False)
        started = asyncio.Event()
        async def send(raw):
            started.set()
        self.backend.side_effect = send
        self.names.schedule(self.raw, self.message)
        await asyncio.wait_for(started.wait(), 1)
        await self.names.close()
        self.assertEqual(self.names.pending, {})
        self.assertEqual(self.names.tasks, set())
        self.cli.assert_not_awaited()
        self.assertEqual(self.backend.await_count, 1)

    async def exercise_relay(self, approval=False):
        received = []
        attach = AsyncMock()
        read_id = None
        answer = '{"id":"approval","result":{"decision":"decline"}}'
        async def persist(ws):
            self.path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': THREAD}}) + '\n')
            # Persistence may finish before projection rejects an unindexed thread.
            await ws.send(json.dumps({'id': read_id, 'error': {'code': -32601}}))
        async def backend(ws):
            nonlocal read_id
            async for raw in ws:
                message = json.loads(raw)
                received.append(message)
                method = message.get('method')
                if method == 'thread/start':
                    await ws.send(json.dumps({'id': message['id'], 'result': {'thread': self.thread}}))
                elif method == 'thread/read':
                    self.assertEqual(message['params'], {'threadId': THREAD, 'includeTurns': True})
                    read_id = message['id']
                    if approval:
                        await ws.send('{"id":"approval","method":"item/commandExecution/requestApproval","params":{}}')
                    else:
                        await persist(ws)
                elif message.get('id') == 'approval':
                    self.assertEqual(raw, answer)
                    await persist(ws)
                elif method == 'thread/name/set':
                    self.assertTrue(rollout_ready(self.path))
                    await ws.send(json.dumps({'id': message['id'], 'result': {}}))
        async with serve(backend, '127.0.0.1', 0) as server:
            relay = Relay(f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}', attach)
            async with serve(relay.handle, '127.0.0.1', 0) as front:
                async with connect(f'ws://127.0.0.1:{front.sockets[0].getsockname()[1]}', proxy=None) as client:
                    await client.send('{"id":1,"method":"thread/start","params":{}}')
                    self.assertEqual(json.loads(await client.recv())['id'], 1)
                    for request_id in [2, 3]:
                        await client.send(json.dumps({**self.message, 'id': request_id}))
                        if approval and request_id == 2:
                            self.assertEqual(json.loads(await client.recv())['id'], 'approval')
                            await client.send(answer)
                        self.assertEqual(json.loads(await asyncio.wait_for(client.recv(), 2)),
                                         {'id': request_id, 'result': {}})
        attach.assert_not_awaited()
        methods = [m.get('method') for m in received if 'method' in m]
        self.assertEqual(methods, ['thread/start', 'thread/read', 'thread/name/set', 'thread/name/set'])

    async def test_persists_before_naming_without_desktop_navigation(self):
        await self.exercise_relay()

    async def test_waiting_for_history_does_not_block_approval_round_trip(self):
        await self.exercise_relay(approval=True)
