"""Local HTTP fixtures verify CLI and actual MCP stdio transport, never production writes."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
ID = '5ab2dc65-8151-4a9f-ab88-761208c596a3'
CONNECTION = 'ea4e4bd6-37fd-4d7f-96e5-f788bd4c043e'


class LoopBridgeTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def do_PUT(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                requests.append((self.command, self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                assert self.headers['Authorization'] == 'Bearer as_workspace_fixture'
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status":"local_fixture_only"}')

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.env = {**{k: v for k, v in os.environ.items() if k in {'HOME', 'PATH', 'USER', 'LANG', 'TMPDIR'}}, 'NATIVE_ANALYTICS_URL': 'http://127.0.0.1:' + str(self.server.server_port),
                    'AFTERSPAN_PROJECT': 'tracker', 'AFTERSPAN_WORKSPACE': 'fixture',
                    'AFTERSPAN_WORKSPACE_KEY': 'as_workspace_fixture'}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_cli_merged_proposal_and_exact_descendant_release_transport(self):
        payload = {'connection_id': CONNECTION, 'goal': 'catalog', 'pr_number': 156,
                   'base_sha': 'a' * 40, 'head_sha': 'b' * 40, 'merge_sha': 'c' * 40, 'goal_sha256': 'd' * 64}
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / 'request.json'
            for command, body, endpoint in [
                ('loop-merged-propose', payload, '/api/loop/merged-artifacts/'),
                ('loop-release', {'connection_id': CONNECTION, 'commit': 'e' * 40}, '/api/loop/actions/')]:
                file.write_text(json.dumps(body))
                run = subprocess.run([sys.executable, str(ROOT / 'cli/native_analytics.py'), command,
                                      '--project', 'tracker', '--id', ID, '--file', str(file)],
                                     env=self.env, capture_output=True, text=True, timeout=20)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(json.loads(run.stdout)['status'], 'local_fixture_only')
                self.assertTrue(self.requests[-1][1].startswith(endpoint + ID))
                self.assertEqual(self.requests[-1][2], body)

    def test_both_mcp_stdio_bridges_expose_proposal_and_pin_release_commit(self):
        async def check(script):
            params = StdioServerParameters(command=sys.executable, args=[str(ROOT / 'agent' / script)], env=self.env)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertIn('loop_merged_propose', {t.name for t in tools.tools})
                    project = {'project': 'tracker'} if script == 'workspace_server.py' else {}
                    result = await session.call_tool('loop_merged_propose', project | {
                        'proposal_id': ID, 'source': {'goal': 'catalog', 'connection_id': CONNECTION}})
                    self.assertFalse(result.isError)
                    result = await session.call_tool('loop_release', project | {
                        'action_id': ID, 'connection_id': CONNECTION, 'commit': 'e' * 40})
                    self.assertFalse(result.isError)

        for script in ['loop_server.py', 'workspace_server.py']:
            with self.subTest(script=script):
                asyncio.run(check(script))
                self.assertEqual(self.requests[-2][1], '/api/loop/merged-artifacts/' + ID + '?project=tracker')
                self.assertEqual(self.requests[-1][2], {'connection_id': CONNECTION, 'commit': 'e' * 40})
