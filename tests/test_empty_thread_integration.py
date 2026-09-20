"""Opt-in real App Server regression; no model calls or real Desktop actions.

CPET_TEST_CODEX=/path/to/codex python -m unittest discover -s tests -p test_empty_thread_integration.py -v
"""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop_bridge import Relay, rollout_ready
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve


@unittest.skipUnless(os.environ.get('CPET_TEST_CODEX'), 'set CPET_TEST_CODEX for isolated real-server test')
class EmptyThreadIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @contextlib.asynccontextmanager
    async def client(self, home, attach):
        with tempfile.TemporaryFile() as stderr:
            process = await asyncio.create_subprocess_exec(
                os.environ['CPET_TEST_CODEX'], 'app-server',
                env={**os.environ, 'CODEX_HOME': str(home)},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=stderr)
            async def backend(ws):
                async def inbound():
                    async for raw in ws:
                        process.stdin.write((raw + '\n').encode())
                        await process.stdin.drain()
                async def outbound():
                    while line := await process.stdout.readline():
                        await ws.send(line.decode().strip())
                tasks = [asyncio.create_task(inbound()), asyncio.create_task(outbound())]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            try:
                async with serve(backend, '127.0.0.1', 0) as upstream:
                    relay = Relay(f'ws://127.0.0.1:{upstream.sockets[0].getsockname()[1]}', attach)
                    async with serve(relay.handle, '127.0.0.1', 0) as front:
                        async with connect(f'ws://127.0.0.1:{front.sockets[0].getsockname()[1]}', proxy=None) as ws:
                            request_id = 0
                            async def rpc(method, params):
                                nonlocal request_id
                                request_id += 1
                                await ws.send(json.dumps({'id': request_id, 'method': method, 'params': params}))
                                while True:
                                    message = json.loads(await asyncio.wait_for(ws.recv(), 20))
                                    if message.get('id') == request_id:
                                        self.assertNotIn('error', message, message)
                                        return message['result']
                            await rpc('initialize', {'clientInfo': {'name': 'cpet-isolated-test', 'version': '0'},
                                                     'capabilities': {'experimentalApi': True}})
                            yield rpc
            finally:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

    async def test_named_empty_thread_supports_history_and_cold_resume(self):
        with tempfile.TemporaryDirectory(prefix='cpet-empty-test-') as directory:
            home = Path(directory)
            (home / 'config.toml').write_text(
                'model="test"\nmodel_provider="test"\n'
                '[model_providers.test]\nname="test"\n'
                'base_url="http://127.0.0.1:1/v1"\nwire_api="responses"\n'
                '[features]\nshell_snapshot=false\n')
            attach = AsyncMock()
            async with self.client(home, attach) as rpc:
                result = await rpc('thread/start', {'cwd': directory, 'historyMode': 'paginated'})
                thread = result['thread']
                path = Path(thread['path'])
                self.assertFalse(path.exists(), 'fixture must exercise unmaterialized history')
                await rpc('thread/name/set', {'threadId': thread['id'], 'name': 'empty-regression'})
                self.assertTrue(rollout_ready(path))
                attach.assert_not_awaited()
                saved = await rpc('thread/read', {'threadId': thread['id'], 'includeTurns': True})
                self.assertEqual(saved['thread']['turns'], [])
                resumed = await rpc('thread/resume', {'threadId': thread['id'], 'includeTurns': True})
                self.assertEqual(resumed['thread']['turns'], [])
            async with self.client(home, AsyncMock()) as rpc:
                resumed = await rpc('thread/resume', {'threadId': thread['id'], 'includeTurns': True})
                self.assertEqual(resumed['thread']['name'], 'empty-regression')
                self.assertEqual(resumed['thread']['turns'], [])
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertFalse(any(r.get('payload', {}).get('type') == 'user_message' for r in records))
