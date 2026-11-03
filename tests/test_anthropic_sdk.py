from contextlib import closing
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import test_tracing_sdk

EXAMPLE=Path(__file__).resolve().parents[1]/'examples/anthropic-sdk-fixture/emit.py'


class AnthropicSDKTests(unittest.TestCase):
    setUp=test_tracing_sdk.TracingSDKTests.setUp
    tearDown=test_tracing_sdk.TracingSDKTests.tearDown
    events=test_tracing_sdk.TracingSDKTests.events

    def test_official_sdk_parses_fixture_streams_then_real_service_preserves_evidence(self):
        m=runpy.run_path(str(EXAMPLE))
        with patch.dict('os.environ',{'ANTHROPIC_API_KEY':'DO_NOT_LEAK_AMBIENT_KEY','ANTHROPIC_AUTH_TOKEN':'DO_NOT_LEAK_AMBIENT_AUTH',
                                    'ANTHROPIC_BASE_URL':'https://do-not-connect.invalid','ANTHROPIC_CUSTOM_HEADERS':'X-Fixture-Secret: DO_NOT_LEAK_HEADERS'}):
            result=m['exercise'](self.tracer)
        self.assertEqual(result['anthropic_sdk_version'],'2.4.0')
        self.assertEqual(result['native_provider_calls'],0);self.assertEqual(result['loopback_fixture_requests'],6)
        events=self.events();self.assertEqual(len(events),24)
        encoded=json.dumps(events)
        self.assertNotIn('DO_NOT_LEAK_',encoded);self.assertNotIn(m['DUMMY_KEY'],encoded)
        self.assertEqual(self.fixture.ingest(events).status_code,200)
        report=self.fixture.client.post('/api/traces/review?project=alpha',headers=self.fixture.admin,json={'environment':'test'}).json()
        self.assertEqual(len(report['traces']),6)
        traces={t['workflow'].removeprefix('anthropic_fixture_'):t for t in report['traces']}
        self.assertEqual(set(traces),set(m['CASES']))
        for case,t in traces.items():
            expected='succeeded' if case in {'complete','reasoning_only'} else 'failed'
            self.assertEqual(len(t['spans']),2);self.assertEqual(t['issues'],[])
            self.assertTrue(all(s['state']==expected and not s['parent_missing'] and not s['missing_start'] for s in t['spans']))
            self.assertEqual(sum(s['parent'] is not None for s in t['spans']),1)
            self.assertEqual(t['checks'],[]);self.assertEqual(t['business_outcome'],'not_established')
            self.assertTrue(all(s['milestone'] is None and s['disposition'] is None for s in t['spans']))
            model=next(s for s in t['spans'] if s['step']=='model');usage=model['model_usage']
            self.assertEqual(usage['provider'],'anthropic_fixture');self.assertEqual(usage['model'],'fixture/messages')
            self.assertIsNone(usage.get('cost_usd_decimal'));self.assertIsNone(usage.get('reasoning_count'))
            if case in {'complete','missing_stop'}:
                self.assertEqual(usage['input_count'],20);self.assertEqual(usage['output_count'],5);self.assertEqual(usage['cache_read_count'],10)
            if case=='reasoning_only':
                self.assertIsNone(usage.get('input_count'));self.assertIsNone(usage.get('cache_read_count'));self.assertIsNone(usage.get('first_output_ms'))
                self.assertEqual(usage['output_count'],3)
            elif case=='auth_rejected':
                self.assertIsNone(usage.get('input_count'));self.assertIsNone(usage.get('output_count'));self.assertIsNone(usage.get('first_output_ms'))
            else:self.assertTrue(0<=usage['first_output_ms']<=model['duration_ms'])
        failures={r['case']:r['failure_type'] for r in result['results']}
        self.assertEqual(failures['auth_rejected'],'AuthenticationError');self.assertEqual(failures['missing_stop'],'IncompleteFixtureStream')
        self.assertEqual(failures['cancelled'],'FixtureCancelled')

    def test_fixture_entrypoint_refuses_an_existing_outbox_before_reading_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'existing.db';path.write_text('preserve sentinel')
            result=subprocess.run([sys.executable,str(EXAMPLE),str(path)],input='',capture_output=True,text=True,
                                  env={'PATH':__import__('os').environ['PATH'],'PYTHONPATH':str(EXAMPLE.parents[2]/'sdk/python')})
            self.assertNotEqual(result.returncode,0);self.assertIn('existing outbox refused',result.stderr)
            self.assertEqual(path.read_text(),'preserve sentinel')
