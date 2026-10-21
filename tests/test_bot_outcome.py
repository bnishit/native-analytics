import copy
import time
import unittest
import uuid

import test_loop_onboarding as onboarding
from loop_onboarding import ScanInput

stamp = onboarding.stamp


class BotOutcomeTests(unittest.TestCase):
    user = onboarding.OnboardingTests.user
    token = onboarding.OnboardingTests.token
    workspace = onboarding.OnboardingTests.workspace
    project = onboarding.OnboardingTests.project
    key = onboarding.OnboardingTests.key
    call = onboarding.OnboardingTests.call
    adopt = onboarding.OnboardingTests.adopt

    def setUp(self):
        onboarding.OnboardingTests.setUp(self)
        self.provider.snapshot['findings']['events'].append({'name': 'slack_message_received', 'path': 'src/analytics.ts', 'line': 5, 'sink': 'native'})
        self.scan = self.loop.onboarding.scan(self.p['id'], str(uuid.uuid4()), ScanInput(connection_id=self.connection['id']))
        self.definition.update(scan_id=self.scan['id'], question='Did the observed bot read get a source-checked reply within 60 seconds?')
        self.definition['measurement'] = {'kind': 'slack-read-outcome-v1', 'unit': 'observed_read_request', 'event': 'slack_message_received',
                                          'command_kind': 'list_experiments', 'verifier': 'growthbook-catalog-v1', 'deadline_seconds': 60, 'min_sample_size': 2}
        self.adopt()
        self.t = int(time.time()) - 600

    def tearDown(self):
        onboarding.OnboardingTests.tearDown(self)

    def send_request(self, *, at=0, delay=10, quality='succeeded', delivered=True, missing=(), changes=None, ref=None, attempt=None, number=1):
        facts = {'schema_version': 'slack-request-v1', 'environment': 'production', 'request_ref': ref or str(uuid.uuid4()),
                 'attempt_ref': attempt or str(uuid.uuid4()), 'attempt_number': number, 'release_ref': str(uuid.uuid4()),
                 'release_sha': 'a' * 40, 'request_started_at_ms': (self.t + at) * 1000,
                 'request_kind': 'read', 'command_kind': 'list_experiments', 'comparison_eligible': None,
                 'confirmation_required': False, 'tool_required': True, 'reply_required': True}
        facts.update(changes or {})
        events = []
        for stage, offset in [('parser', 1), ('planner', 2), ('tool', 3), ('reply', delay), ('quality', delay + 1)]:
            if stage in missing:
                continue
            row = {**facts, 'stage': stage, 'reported_outcome': 'failed' if stage == 'reply' and not delivered else 'succeeded'}
            if stage == 'quality':
                row.update(check_version='growthbook-catalog-v1', checked_claims=0 if quality == 'unknown' else 2,
                           mismatched_claims=1 if quality == 'failed' else 0, unknown_claims=1 if quality == 'unknown' else 0,
                           reported_outcome=quality)
            events.append({'id': str(uuid.uuid4()), 'name': 'slack.audit.' + stage, 'timestamp': stamp(self.t + at + offset), 'properties': {'afterspan_audit': row}})
        result = self.client.post('/v1/events', headers={'Authorization': 'Bearer ' + self.p['write_key']}, json={'events': events})
        self.assertEqual(result.status_code, 200, result.text)
        return events

    def observe(self, through=200, release=None, start=-1, end=None):
        body = {'entry_start': stamp(self.t + start), 'entry_end': stamp(self.t + (through if end is None else end)),
                'observed_through': stamp(self.t + through), 'as_of': stamp(time.time()), 'release': release}
        result = self.call('PUT', '/goals/api-read-outcomes/observations/' + str(uuid.uuid4()), body)
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_delivery_and_source_check_are_both_needed_and_events_do_not_multiply_requests(self):
        events = self.send_request()
        self.client.post('/v1/events', headers={'Authorization': 'Bearer ' + self.p['write_key']}, json={'events': events})
        self.send_request(at=50, quality='failed')
        self.send_request(at=100, missing=('quality',))
        result = self.observe()
        self.assertEqual(result['counts'], {'total': 3, 'success': 1, 'failure': 1, 'unclassified': 1})
        self.assertEqual(result['coverage']['delivered'], 3)
        self.assertIsNone(result['rate'])
        self.assertEqual(result['verdict'], 'coverage_gap')

    def test_known_late_or_failed_delivery_fails_and_does_not_need_a_quality_pass(self):
        self.send_request(delay=70)
        self.send_request(at=80, delivered=False, missing=('quality',))
        result = self.observe()
        self.assertEqual(result['counts'], {'total': 2, 'success': 0, 'failure': 2, 'unclassified': 0})
        self.assertEqual(result['coverage']['late_replies'], 1)
        self.assertEqual(result['rate'], 0)

    def test_missing_delivery_is_unknown_and_recent_requests_wait_for_the_full_deadline(self):
        self.send_request(missing=('reply', 'quality'))
        self.send_request(at=180)
        result = self.observe()
        self.assertEqual(result['counts'], {'total': 1, 'success': 0, 'failure': 0, 'unclassified': 1})
        self.assertEqual(result['coverage']['waiting'], 1)
        self.assertIsNone(result['rate'])

    def test_release_and_original_start_control_the_cohort_without_inventing_old_metadata(self):
        self.send_request()
        self.send_request(at=20, changes={'release_sha': 'b' * 40})
        self.send_request(at=40, changes={'release_sha': None})
        self.send_request(at=60, changes={'command_kind': None})
        self.send_request(at=80, changes={'request_started_at_ms': None})
        result = self.observe(release='a' * 40)
        self.assertEqual(result['counts']['total'], 1)
        self.assertEqual(result['coverage']['release_missing'], 1)
        self.assertEqual(result['coverage']['command_missing'], 1)
        self.assertEqual(result['coverage']['start_missing'], 1)
        self.assertIsNone(result['rate'])
        self.assertEqual(self.observe(start=15)['counts']['success'], 2)

    def test_other_reads_do_not_become_catalog_answers_and_enough_complete_requests_can_be_observed(self):
        self.send_request()
        self.send_request(at=50)
        self.send_request(at=100, changes={'command_kind': 'list_users'})
        result = self.observe()
        self.assertEqual(result['counts'], {'total': 2, 'success': 2, 'failure': 0, 'unclassified': 0})
        self.assertEqual(result['coverage']['other_reads'], 1)
        self.assertEqual(result['rate'], 1)
        self.assertEqual(result['verdict'], 'observed')
        self.assertFalse(result['release_bound'])

    def test_conflicting_duplicate_stages_cannot_turn_into_a_pass(self):
        events = self.send_request()
        duplicate = copy.deepcopy(events[-1]); duplicate['id'] = str(uuid.uuid4())
        self.client.post('/v1/events', headers={'Authorization': 'Bearer ' + self.p['write_key']}, json={'events': [duplicate]})
        result = self.observe()
        self.assertEqual(result['counts']['unclassified'], 1)
        self.assertIsNone(result['rate'])

    def test_business_retry_uses_one_request_and_does_not_restart_the_original_deadline(self):
        ref = str(uuid.uuid4())
        self.send_request(ref=ref, missing=('reply', 'quality'))
        self.send_request(ref=ref, number=2, at=80, changes={'request_started_at_ms': self.t * 1000})
        result = self.observe()
        self.assertEqual(result['counts'], {'total': 1, 'success': 0, 'failure': 1, 'unclassified': 0})
        self.assertEqual(self.observe(start=40)['counts']['total'], 0)

    def test_request_spanning_releases_cannot_qualify_for_the_new_release(self):
        ref = str(uuid.uuid4())
        self.send_request(ref=ref, missing=('reply', 'quality'))
        self.send_request(ref=ref, number=2, at=20, changes={'request_started_at_ms': self.t * 1000, 'release_sha': 'b' * 40})
        result = self.observe(release='b' * 40)
        self.assertEqual(result['counts']['success'], 0)
        self.assertEqual(result['coverage']['release_missing'], 1)
        self.assertIsNone(result['rate'])

    def test_one_known_wrong_fact_fails_even_when_another_claim_is_unknown(self):
        from contextlib import closing
        import json
        events = self.send_request(quality='unknown')
        facts = events[-1]['properties']['afterspan_audit']
        facts.update(checked_claims=2, mismatched_claims=1, unknown_claims=1)
        with closing(self.loop.connect()) as c, c:
            c.execute('UPDATE events SET properties=? WHERE project=? AND id=?', (json.dumps({'afterspan_audit': facts}), self.p['id'], events[-1]['id']))
        self.assertEqual(self.observe()['counts']['failure'], 1)

    def test_missing_stage_requirements_and_failed_parser_cannot_qualify(self):
        from contextlib import closing
        import json
        self.send_request(changes={'tool_required': None})
        self.send_request(at=30, changes={'confirmation_required': None})
        events = self.send_request(at=60)
        facts = events[0]['properties']['afterspan_audit']
        facts['reported_outcome'] = 'failed'
        with closing(self.loop.connect()) as c, c:
            c.execute('UPDATE events SET properties=? WHERE project=? AND id=?', (json.dumps({'afterspan_audit': facts}), self.p['id'], events[0]['id']))
        result = self.observe()
        self.assertEqual(result['counts'], {'total': 3, 'success': 0, 'failure': 0, 'unclassified': 3})
        self.assertIsNone(result['rate'])

    def test_quality_arriving_after_the_receipt_cutoff_cannot_backfill_a_pass(self):
        from contextlib import closing
        events = self.send_request()
        with closing(self.loop.connect()) as c, c:
            c.execute('UPDATE events SET received=? WHERE project=? AND id=?', (time.time() + 100, self.p['id'], events[-1]['id']))
        self.assertEqual(self.observe()['counts']['unclassified'], 1)

    def test_posthog_cannot_claim_the_native_request_contract(self):
        definition = copy.deepcopy(self.definition)
        definition['mapping'].update(kind='posthog', connection_id=str(uuid.uuid4()), connection_revision=1)
        self.assertEqual(self.call('PUT', '/proposals/' + str(uuid.uuid4()), definition).status_code, 422)
