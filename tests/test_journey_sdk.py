import json
import tempfile
import unittest
from pathlib import Path

from test_analytics import Collector


class JourneySDKTests(unittest.TestCase):
    def test_durable_queue_preserves_context_and_invalid_metadata_does_not_break_application(self):
        with tempfile.TemporaryDirectory() as directory:
            collector=Collector('https://analytics.example','test-key',Path(directory)/'queue.db')
            context={'id':'task','workflow':'summary','environment':'local','attempt_id':'attempt-1','attempt_number':1,'state':'started'}
            eid=collector.track_journey('summary.started',context)
            self.assertIsNotNone(eid)
            context['attempt_number']=2
            with collector.db() as conn:queued=json.loads(conn.execute('SELECT payload FROM outbox').fetchone()[0])
            self.assertEqual(queued['journey']['attempt_number'],1)
            context['attempt_number']=0
            self.assertIsNone(collector.track_journey('summary.started',context))
            self.assertEqual(collector.status()['queued'],1)

    def test_ai_usage_survives_durable_queue_without_exposing_token_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            collector=Collector('https://analytics.example','test-key',Path(directory)/'queue.db')
            collector.track('ai.generation',properties={'ai_input_tokens':123,'output_tokens':8,
                'input_tokens':'credential','total_tokens':True,'access_token':'private'})
            with collector.db() as conn:properties=json.loads(conn.execute('SELECT payload FROM outbox').fetchone()[0])['properties']
            self.assertEqual(properties,{'ai_input_tokens':123,'output_tokens':8,
                'input_tokens':'[redacted]','total_tokens':'[redacted]','access_token':'[redacted]'})
