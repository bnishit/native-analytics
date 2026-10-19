import json
import copy
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app
import test_workspaces
from loop_measurement import count_first_entry_outcomes
from product_loop import EVENT_CAP, ProductLoop, canonical, digest


CSRF = test_workspaces.CSRF


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def definition():
    return {'schema': 'afterspan-goal-v1', 'id': 'first-answer', 'question': 'Do workspaces get a checked answer within 24 hours?',
            'measurement': {'unit': 'workspace', 'entry': {'event': 'codex.question.started'},
                            'completion': {'event': 'codex.answer.ready'}, 'followup_seconds': 86400,
                            'entry_rule': 'first_in_entry_window', 'rubric': 'aggregate-answer-v1'},
            'mapping': {'source': 'native', 'environment': 'local', 'observation_id_property': 'observation_id'},
            'tracking': {'recipe': 'python-answer-v1', 'path': 'examples/loop_pilot.py', 'dependencies': ['examples/loop_pilot.py']}}


class FakeGitHub:
    def __init__(self):
        self.definition = definition()
        self.writes = []
        self.hook = None
        self.timeout = False
        self.remote = None
        self.healthy = False

    def load_goal(self, source):
        return {**source, 'definition': copy.deepcopy(self.definition),
                'dependencies': [{'path': 'examples/loop_pilot.py', 'blob_sha': 'd' * 40, 'content_sha256': 'e' * 64}],
                'workflows': {'.github/workflows/test.yml': 'f' * 40}}

    def prepare(self, bundle):
        if self.healthy:
            return {'status': 'healthy', 'reason': 'exact tracking already present'}
        return {'status': 'prepared', 'repository': bundle['repository'], 'base': bundle['base'], 'base_sha': 'b' * 40,
                'goal_path': bundle['path'], 'goal_sha256': bundle['content_sha256'],
                'files': [{'path': 'examples/loop_pilot.py', 'before_sha': 'c' * 40, 'content': 'tracking repair\n'}],
                'artifact_sha256': 'a' * 64, 'workflows': bundle['workflows']}

    def draft(self, plan, action_id, guard):
        guard()
        self.writes.append(('branch', action_id))
        self.remote = {'status': 'drafted', 'action_id': action_id, 'pr_number': 7,
                       'pr_url': 'https://github.com/example/loop/pull/7', 'head_sha': 'd' * 40,
                       'base_sha': plan['base_sha'], 'merged': False, 'artifact_sha256': plan['artifact_sha256']}
        if self.hook:
            self.hook()
        if self.timeout:
            raise TimeoutError('secret provider response must never escape')
        guard()
        self.writes.append(('pull_request', action_id))
        return copy.deepcopy(self.remote)

    def reconcile(self, plan, receipt):
        return copy.deepcopy(self.remote) if self.remote else {'status': 'prepared', 'action_id': plan['id']}

    def merge(self, plan, receipt, policy, guard):
        guard()
        if 'release-on-merge' not in policy['operations']:
            from fastapi import HTTPException
            raise HTTPException(403, 'release-on-merge authority required')
        self.writes.append(('merge', plan['id']))
        self.remote = {**receipt, 'status': 'merged', 'merged': True, 'merge_sha': 'e' * 40,
                       'release_status': 'release_pending'}
        return copy.deepcopy(self.remote)


class ProductLoopTests(unittest.TestCase):
    user = test_workspaces.WorkspaceTests.user
    token = test_workspaces.WorkspaceTests.token
    workspace = test_workspaces.WorkspaceTests.workspace
    project = test_workspaces.WorkspaceTests.project
    invite = test_workspaces.WorkspaceTests.invite
    join = test_workspaces.WorkspaceTests.join
    key = test_workspaces.WorkspaceTests.key

    def setUp(self):
        test_workspaces.WorkspaceTests.setUp(self)
        self.owner, self.owner_id = self.user('loop-owner@example.test')
        self.w = self.workspace(self.owner)
        self.p = self.project(self.owner, self.w)
        self.provider = FakeGitHub()
        self.loop_app = FastAPI()
        with closing(app.connect()) as c, c:
            ProductLoop.init_db(c)
        self.loop = ProductLoop(self.loop_app, app.connect, app.reader, app.editor, app.owner, app.RETENTION_DAYS, self.provider, store_events=app.store_events)
        self.api = self.loop_client(self.owner)
        self.t = time.time() - 72 * 3600
        self.n = 0
        self.source = {'repository': 'example/loop', 'base': 'loop-pilot', 'commit': 'a' * 40,
                       'path': '.afterspan/goals/first-answer.json', 'content_sha256': digest(self.provider.definition)}
        self.synced = self.call('PUT', '/goals/first-answer/sync', self.source).json()

    def test_prepare_retry_recovers_the_same_read_intent_after_a_crash(self):
        request_id=str(uuid.uuid4())
        with patch.object(self.provider,'prepare',side_effect=RuntimeError('read interrupted')):
            with self.assertRaises(RuntimeError):
                self.call('POST','/goals/first-answer/prepare',{'request_id':request_id})
        interrupted=self.call('GET','/goals/first-answer').json()['actions'][0]
        recovered=self.call('POST','/goals/first-answer/prepare',{'request_id':request_id})
        self.assertEqual(recovered.status_code,200,recovered.text)
        self.assertEqual(recovered.json()['id'],interrupted['id'])
        self.assertEqual(recovered.json()['status'],'prepared')
        self.assertEqual(self.provider.writes,[])

    def tearDown(self):
        test_workspaces.WorkspaceTests.tearDown(self)

    def connection_goal(self):
        self.provider.definition['measurement']['entry']['event'] = 'codex.connection.started'
        self.source['content_sha256'] = digest(self.provider.definition)
        response = self.call('PUT', '/goals/first-answer/sync', self.source)
        self.assertEqual(response.status_code, 200, response.text)

    def connect_codex(self):
        response = self.call('POST', '/goals/first-answer/connection', {})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def deliver_answer(self, connection, evidence, id=None, answer=None, client=None):
        return self.call('PUT', '/goals/first-answer/connections/' + connection['id'] + '/answers/' + (id or str(uuid.uuid4())),
                         {'evidence_id': evidence['id'], 'answer': answer or self.answer(evidence)}, client)

    def test_real_connection_clock_delivery_and_idempotent_retries(self):
        self.connection_goal()
        evidence = self.observe()
        with patch('product_loop.time.time', return_value=self.t):
            connection = self.connect_codex()
        answer_id = str(uuid.uuid4())
        with patch('product_loop.time.time', return_value=self.t + 3600):
            self.assertEqual(self.connect_codex(), connection)
            response = self.deliver_answer(connection, evidence, answer_id)
        self.assertEqual(response.status_code, 200, response.text)
        checked = response.json()
        self.assertEqual((checked['verdict'], checked['elapsed_seconds'], checked['within_deadline']), ('qualified', 3600, True))
        self.assertEqual(self.deliver_answer(connection, evidence, answer_id).json(), checked)
        self.assertEqual(self.deliver_answer(connection, evidence, answer_id, {**self.answer(evidence), 'mature': 1}).status_code, 409)
        with closing(app.connect()) as c:
            rows = c.execute('SELECT * FROM events WHERE project=? ORDER BY ts', (self.p['id'],)).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual({row['distinct_id'] for row in rows}, {self.w})
            self.assertEqual([row['ts'] for row in rows], [self.t, self.t + 3600])
            self.assertTrue(all(app.billing.seen(c, self.p['id'], row['id']) for row in rows))
            self.assertTrue(all('answer' not in json.loads(row['properties']) for row in rows))
        measured = self.observe(self.window(start=-1))
        self.assertEqual((measured['mature'], measured['qualified'], measured['rate']), (1, 1, 1))

    def test_recent_connection_is_waiting_even_after_checked_answer(self):
        self.connection_goal()
        evidence = self.observe()
        connection = self.connect_codex()
        self.assertEqual(self.deliver_answer(connection, evidence).json()['verdict'], 'qualified')
        now = time.time()
        measured = self.observe({'entry_start': stamp(now - 30), 'entry_end': stamp(now), 'observed_through': stamp(now), 'as_of': stamp(now)})
        self.assertEqual((measured['mature'], measured['waiting'], measured['rate']), (0, 1, None))
        view = self.call('GET', '/goals/first-answer').json()
        self.assertEqual(view['connection']['id'], connection['id'])
        self.assertEqual(view['latest_answer']['connection_id'], connection['id'])

    def test_wrong_answer_and_late_answer_do_not_create_qualified_conversion(self):
        self.connection_goal()
        evidence = self.observe()
        with patch('product_loop.time.time', return_value=self.t):
            connection = self.connect_codex()
        with patch('product_loop.time.time', return_value=self.t + 3600):
            failed = self.deliver_answer(connection, evidence, answer={**self.answer(evidence), 'mature': 7})
        self.assertEqual(failed.json()['verdict'], 'failed')
        with patch('product_loop.time.time', return_value=self.t + 25 * 3600):
            late = self.deliver_answer(connection, evidence)
        self.assertEqual((late.json()['verdict'], late.json()['within_deadline']), ('qualified', False))
        measured = self.observe(self.window(start=-1))
        self.assertEqual((measured['qualified'], measured['failed']), (0, 1))

    def test_connection_scope_and_contract_changes_are_rejected(self):
        self.connection_goal()
        evidence = self.observe()
        reader, _ = self.join(self.owner, self.w, 'connection-reader@example.test')
        reader_api = self.loop_client(reader)
        self.assertEqual(self.call('POST', '/goals/first-answer/connection', {}, reader_api).status_code, 403)
        connection = self.connect_codex()
        self.assertEqual(self.deliver_answer(connection, evidence, client=reader_api).status_code, 403)
        outsider, _ = self.user('connection-outsider@example.test')
        self.assertEqual(self.deliver_answer(connection, evidence, client=self.loop_client(outsider)).status_code, 403)
        self.provider.definition['measurement']['entry']['filters'] = [{'property': 'plan', 'op': 'eq', 'value': 'pro'}]
        self.source['content_sha256'] = digest(self.provider.definition)
        self.assertEqual(self.call('PUT', '/goals/first-answer/sync', self.source).status_code, 200)
        self.assertEqual(self.deliver_answer(connection, evidence).status_code, 409)
        self.assertEqual(self.call('POST', '/goals/first-answer/connection', {}).status_code, 409)

    def test_erasure_never_resurrects_a_connection_start_or_clock(self):
        self.connection_goal()
        evidence = self.observe()
        connection = self.connect_codex()
        erased = self.client.post('/v1/erase', headers={'Authorization': 'Bearer ' + self.p['write_key']}, json={'distinct_id': self.w})
        self.assertEqual(erased.status_code, 200)
        self.assertEqual(self.call('POST', '/goals/first-answer/connection', {}).status_code, 410)
        self.assertEqual(self.deliver_answer(connection, evidence).status_code, 410)
        view = self.call('GET', '/goals/first-answer').json()
        self.assertEqual(view['connection_status'], 'evidence_unavailable')
        self.assertEqual(view['connection']['id'], connection['id'])
        with closing(app.connect()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM events WHERE project=?', (self.p['id'],)).fetchone()[0], 0)

    def test_delivery_rechecks_service_environment_but_replay_is_immutable(self):
        self.connection_goal()
        evidence = self.observe()
        connection = self.connect_codex()
        id = str(uuid.uuid4())
        checked = self.deliver_answer(connection, evidence, id).json()
        with patch.dict('os.environ', {'AFTERSPAN_ENVIRONMENT': 'production'}):
            self.assertEqual(self.call('POST', '/goals/first-answer/connection', {}).status_code, 409)
            self.assertEqual(self.call('GET', '/goals/first-answer').json()['connection_status'], 'environment_changed')
            self.assertEqual(self.deliver_answer(connection, evidence).status_code, 409)
            self.assertEqual(self.deliver_answer(connection, evidence, id).json(), checked)

    def test_failed_ingestion_rolls_back_connection_and_answer_together(self):
        self.connection_goal()
        evidence = self.observe()
        with patch.object(self.loop, 'store_events', side_effect=ValueError('private storage detail')):
            response = self.call('POST', '/goals/first-answer/connection', {})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('private storage detail', response.text)
        self.assertIsNone(self.call('GET', '/goals/first-answer').json()['connection'])
        connection = self.connect_codex()
        with patch.object(self.loop, 'check_completion', side_effect=RuntimeError('checker interrupted')):
            with self.assertRaises(RuntimeError):
                self.deliver_answer(connection, evidence)
        with closing(app.connect()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM events WHERE project=?', (self.p['id'],)).fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM loop_answers WHERE project=?', (self.p['id'],)).fetchone()[0], 0)

    def loop_client(self, human):
        client = TestClient(self.loop_app)
        client.cookies.update(human.cookies)
        return client

    def call(self, method, path, body=None, client=None, project=None, query=''):
        return (client or self.api).request(method, '/api/loop' + path + '?project=' + (project or self.p['id']) + query,
                                           headers=CSRF, json=body)

    def send(self, name, identity='one', hour=0, properties=None, eid=None):
        self.n += 1
        event = {'id': eid or 'loop-' + str(self.n), 'name': name, 'distinct_id': identity,
                 'timestamp': stamp(self.t + hour * 3600), 'properties': {'environment': 'local', **(properties or {})}}
        response = self.client.post('/v1/events', headers={'Authorization': 'Bearer ' + self.p['write_key']}, json={'events': [event]})
        self.assertEqual(response.status_code, 200, response.text)
        return event['id']

    def window(self, start=0, end=48, through=48, as_of=None, release=None):
        return {'entry_start': stamp(self.t + start * 3600), 'entry_end': stamp(self.t + end * 3600),
                'observed_through': stamp(self.t + through * 3600), 'as_of': stamp(as_of or time.time()), 'release': release}

    def observe(self, window=None, id=None, client=None):
        response = self.call('PUT', '/goals/first-answer/observations/' + (id or str(uuid.uuid4())), window or self.window(), client)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def answer(self, evidence):
        return {key: evidence[key] for key in ['source', 'measurement_revision', 'mature', 'qualified', 'waiting',
                                              'unassessed', 'rate', 'limitations', 'next_action']}

    def assess(self, evidence, event_id, answer=None, id=None):
        return self.call('PUT', '/goals/first-answer/answers/' + (id or str(uuid.uuid4())),
                         {'completion_event_id': event_id, 'evidence_id': evidence['id'], 'answer': answer or self.answer(evidence)})

    def policy(self, state='active', operations=None, revision=0, client=None):
        payload = {'state': state, 'repository': self.source['repository'], 'base': self.source['base'],
                   'operations': operations or ['draft-pr'], 'paths': ['examples/loop_pilot.py'], 'environment': 'local',
                   'required_checks': [{'name': 'independent', 'app_id': 99, 'workflow_path': '.github/workflows/test.yml', 'workflow_sha': 'f' * 40}],
                   'expires_at': stamp(time.time() + 3600)}
        return self.call('PUT', '/goals/first-answer/policy', payload, client, query='&revision=' + str(revision))

    def prepare(self, id=None):
        response = self.call('POST', '/goals/first-answer/prepare', {'request_id': id or str(uuid.uuid4())})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def act(self, action, operation='draft-pr', client=None):
        return self.call('POST', '/actions/' + action['id'] + '/' + operation, {}, client)

    def test_openapi_has_typed_bodies_and_native_event_ids_are_bounded(self):
        schema = self.api.get('/openapi.json').json()
        body = schema['paths']['/api/loop/goals/{goal}/answers/{id}']['put']['requestBody']
        self.assertEqual(body['content']['application/json']['schema']['$ref'], '#/components/schemas/AnswerCheck')
        evidence = self.observe()
        for event_id in ['x' * 161, 'wrong id', {'raw': 'identity'}]:
            response = self.assess(evidence, event_id)
            self.assertEqual(response.status_code, 422, response.text)

    def test_goal_history_and_meaning_digests_are_immutable(self):
        self.assertEqual(self.synced['goal']['goal_revision'], 1)
        repeated = self.call('PUT', '/goals/first-answer/sync', self.source).json()
        self.assertEqual(len(repeated['history']), 1)
        self.provider.definition['question'] = 'Does the workspace receive its first checked answer?'
        self.source['commit'] = 'b' * 40
        self.source['content_sha256'] = digest(self.provider.definition)
        changed = self.call('PUT', '/goals/first-answer/sync', self.source).json()
        self.assertEqual(changed['goal']['goal_revision'], 2)
        self.assertEqual(changed['goal']['measurement_revision'], self.synced['goal']['measurement_revision'])
        self.assertNotEqual(changed['goal']['goal_digest'], self.synced['goal']['goal_digest'])
        self.assertEqual(changed['history'][0]['source']['definition']['question'], definition()['question'])
        self.assertNotIn('filters', changed['history'][0]['source']['definition']['measurement']['entry'])
        self.assertEqual(self.call('GET', '/goals').json()['goals'][0]['id'], 'first-answer')

    def test_repository_text_cannot_grant_or_expand_owner_authority(self):
        self.provider.definition['policy'] = {'state': 'active', 'operations': ['merge', 'release-on-merge']}
        self.source['content_sha256'] = digest(self.provider.definition)
        response = self.call('PUT', '/goals/first-answer/sync', self.source)
        self.assertEqual(response.status_code, 422, response.text)
        view = self.call('GET', '/goals/first-answer').json()
        self.assertEqual((view['goal']['goal_revision'], view['policy']), (1, None))
        policy = {'state': 'active', 'repository': self.source['repository'], 'base': self.source['base'],
                  'operations': ['draft-pr'], 'paths': ['app.py'], 'environment': 'local',
                  'required_checks': [], 'expires_at': stamp(time.time() + 3600)}
        self.assertEqual(self.call('PUT', '/goals/first-answer/policy', policy, query='&revision=0').status_code, 422)

    def test_first_entry_clock_maturity_partition_and_producer_flags(self):
        self.send('codex.question.started', 'late', 0)
        self.send('codex.question.started', 'late', 23)
        self.send('codex.answer.ready', 'late', 25, {'quality_passed': True, 'qualified': True})
        self.send('codex.question.started', 'waiting', 47)
        self.send('codex.answer.ready', 'waiting', 47.5, {'quality_passed': True})
        self.send('codex.question.started', 'unreviewed', 1)
        self.send('codex.answer.ready', 'unreviewed', 2, {'quality_passed': True})
        self.send('codex.question.started', None, 1)
        self.send('codex.question.started', 'internal', 1, {'internal': True})
        self.send('codex.question.started', 'other-env', 1, {'environment': 'production'})
        self.send('analytics.pipeline', 'one', 1)
        result = self.observe()
        self.assertEqual({key: result[key] for key in ['entrants', 'mature', 'waiting', 'qualified', 'failed', 'unassessed', 'timed_out', 'rate', 'confirmed_lower_bound']},
                         {'entrants': 3, 'mature': 2, 'waiting': 1, 'qualified': 0, 'failed': 0, 'unassessed': 1, 'timed_out': 1, 'rate': None, 'confirmed_lower_bound': 0.0})
        self.assertEqual(result['next_action'], 'review_answers')
        self.assertNotIn('distinct_id', canonical(result))
        with closing(app.connect()) as c:
            stored = c.execute('SELECT receipt FROM loop_observations').fetchone()[0]
        self.assertNotIn('unreviewed', stored)

    def test_independent_checker_uses_server_zero_null_and_exact_refs(self):
        evidence = self.observe()
        self.assertEqual((evidence['mature'], evidence['rate']), (0, None))
        self.send('codex.question.started', hour=0)
        event_id = self.send('codex.answer.ready', hour=1, properties={'observation_id': evidence['id'], 'qualified': True})
        good = self.assess(evidence, event_id)
        self.assertEqual(good.status_code, 200, good.text)
        self.assertEqual(good.json()['verdict'], 'qualified')
        self.assertEqual(good.json()['checker_version'], 'aggregate-answer-v1')
        answer = {**self.answer(evidence), 'mature': 1, 'rate': 0.0, 'limitations': ['private raw answer must not persist']}
        bad = self.assess(evidence, event_id, answer)
        self.assertEqual(bad.json()['verdict'], 'failed')
        self.assertEqual(bad.json()['mismatches'], ['mature', 'rate', 'limitations'])
        self.assertNotIn('private raw answer', bad.text)
        result = self.observe()
        self.assertEqual((result['mature'], result['unassessed'], result['qualified'], result['rate']), (1, 1, 0, None))
        self.assertEqual(self.assess(evidence, event_id, {**self.answer(evidence), 'verified': True}).status_code, 422)

    def test_pass_fail_and_timeout_are_distinct_and_ids_are_idempotent(self):
        evidence = self.observe()
        for who in ['passed', 'failed', 'timeout']:
            self.send('codex.question.started', who)
        passed = self.send('codex.answer.ready', 'passed', 1, {'observation_id': evidence['id']})
        failed = self.send('codex.answer.ready', 'failed', 1, {'observation_id': evidence['id']})
        self.send('codex.answer.failed', 'failed', 2, {'technical_failure': True})
        id = str(uuid.uuid4())
        self.assertEqual(self.assess(evidence, passed, id=id).json()['verdict'], 'qualified')
        self.assertEqual(self.assess(evidence, passed, id=id).json()['verdict'], 'qualified')
        self.assertEqual(self.assess(evidence, passed, {**self.answer(evidence), 'qualified': 1}, id).status_code, 409)
        self.assertEqual(self.assess(evidence, failed, {**self.answer(evidence), 'qualified': 1}).json()['verdict'], 'failed')
        result = self.observe()
        self.assertEqual({key: result[key] for key in ['mature', 'qualified', 'failed', 'unassessed', 'timed_out', 'technical_failures', 'rate']},
                         {'mature': 3, 'qualified': 1, 'failed': 1, 'unassessed': 0, 'timed_out': 1, 'technical_failures': 1, 'rate': 1 / 3})

    def test_cutoff_and_frozen_receipts_do_not_accept_future_reviews_or_delivery(self):
        evidence = self.observe()
        self.send('codex.question.started')
        event_id = self.send('codex.answer.ready', hour=1, properties={'observation_id': evidence['id']})
        cutoff = time.time()
        first = self.observe(self.window(as_of=cutoff))
        self.assertEqual(first['unassessed'], 1)
        self.assertEqual(self.assess(evidence, event_id).json()['verdict'], 'qualified')
        self.assertEqual(self.observe(self.window(as_of=cutoff))['unassessed'], 1)
        self.assertEqual(self.observe()['qualified'], 1)
        self.assertEqual(self.observe(self.window(as_of=cutoff), first['id']), first)
        self.send('codex.question.started', 'late-delivery', 0)
        old_cutoff = self.observe(self.window(as_of=cutoff))
        self.assertEqual(old_cutoff['entrants'], 1)
        changed = self.call('PUT', '/goals/first-answer/observations/' + first['id'], self.window(end=47))
        self.assertEqual(changed.status_code, 409)

    def test_completion_must_match_goal_environment_and_exact_evidence(self):
        evidence = self.observe()
        wrong_name = self.send('analytics.pipeline', properties={'observation_id': evidence['id']})
        wrong_environment = self.send('codex.answer.ready', properties={'observation_id': evidence['id'], 'environment': 'production'})
        wrong_evidence = self.send('codex.answer.ready', properties={'observation_id': str(uuid.uuid4())})
        for event_id in [wrong_name, wrong_environment, wrong_evidence, 'missing']:
            self.assertEqual(self.assess(evidence, event_id).status_code, 422)

    def test_every_timely_completion_needs_independent_review(self):
        evidence = self.observe()
        self.send('codex.question.started')
        first = self.send('codex.answer.ready', hour=1, properties={'observation_id': evidence['id']})
        second = self.send('codex.answer.ready', hour=2, properties={'observation_id': evidence['id'], 'quality_passed': True})
        self.assertEqual(self.assess(evidence, first).json()['verdict'], 'qualified')
        partial = self.observe()
        self.assertEqual((partial['qualified'], partial['unassessed'], partial['rate']), (0, 1, None))
        self.assertEqual(self.assess(evidence, second).json()['verdict'], 'qualified')
        complete = self.observe()
        self.assertEqual((complete['qualified'], complete['unassessed'], complete['rate']), (1, 0, 1.0))

    def test_retention_erasure_and_oversize_reads_are_visible(self):
        self.send('codex.question.started', 'erased')
        old = self.observe()
        erased = self.client.post('/v1/erase', headers={'Authorization': 'Bearer ' + self.p['write_key']}, json={'distinct_id': 'erased'})
        self.assertEqual(erased.status_code, 200)
        current = self.observe(old['window'])
        self.assertEqual((current['entrants'], current['coverage']['erasure'], current['coverage']['complete']), (0, True, False))
        self.assertEqual(self.observe(old['window'], old['id'])['entrants'], 1)
        retained = self.observe({**self.window(), 'entry_start': stamp(time.time() - 91 * 86400), 'entry_end': stamp(time.time() - 2 * 86400)})
        self.assertTrue(retained['coverage']['retention_gap'])
        event_ts = datetime.fromisoformat(self.window()['entry_start']).timestamp()
        with closing(app.connect()) as c, c:
            c.executemany('INSERT INTO events VALUES(?,?,?,?,?,?,?)',
                          [(self.p['id'], 'cap-' + str(i), 'codex.question.started', 'one', event_ts, time.time(), '{"environment":"local"}') for i in range(EVENT_CAP + 1)])
        response = self.call('PUT', '/goals/first-answer/observations/' + str(uuid.uuid4()), self.window())
        self.assertEqual(response.status_code, 422)
        self.assertIn('200,000', response.text)

    def test_roles_project_isolation_and_delegated_owner_cannot_grant_policy(self):
        reader, _ = self.join(self.owner, self.w, 'loop-reader@example.test')
        editor, _ = self.join(self.owner, self.w, 'loop-editor@example.test', 'editor')
        read_api, edit_api = self.loop_client(reader), self.loop_client(editor)
        self.assertEqual(self.observe(client=read_api)['entrants'], 0)
        self.assertEqual(self.call('POST', '/goals/first-answer/prepare', {'request_id': str(uuid.uuid4())}, read_api).status_code, 403)
        self.assertEqual(self.policy(client=edit_api).status_code, 403)
        self.assertEqual(self.policy(client=read_api).status_code, 403)
        self.assertEqual(self.call('POST', '/goals/first-answer/prepare', {'request_id': str(uuid.uuid4())}, edit_api).status_code, 200)
        key = self.key(self.owner, self.w, 'owner')
        delegated = TestClient(self.loop_app)
        delegated.headers.update({'Authorization': 'Bearer ' + key['key']})
        self.assertEqual(self.policy(client=delegated).status_code, 403)
        other, _ = self.user('loop-other@example.test')
        other_api = self.loop_client(other)
        self.assertEqual(self.call('GET', '/goals/first-answer', client=other_api).status_code, 403)
        self.assertEqual(self.call('GET', '/goals/first-answer', client=TestClient(self.loop_app)).status_code, 401)
        producer = TestClient(self.loop_app)
        producer.headers.update({'Authorization': 'Bearer ' + self.p['write_key']})
        self.assertEqual(self.call('GET', '/goals/first-answer', client=producer).status_code, 401)
        self.assertEqual(self.policy().status_code, 200)
        self.assertEqual(self.policy(revision=0).status_code, 409)
        outside = self.project(self.owner, self.workspace(self.owner, 'Other loop'))['id']
        action = self.prepare()
        self.assertEqual(self.call('POST', '/actions/' + action['id'] + '/draft-pr', {}, project=outside).status_code, 404)
        self.assertEqual(self.call('POST', '/goals/first-answer/compare', {'before': str(uuid.uuid4()), 'after': str(uuid.uuid4())}, project=outside).status_code, 404)

    def test_policy_pause_revocation_and_membership_are_rechecked_at_each_write(self):
        self.assertEqual(self.policy().status_code, 200)
        action = self.prepare()
        self.provider.hook = lambda: self.assertEqual(self.policy(state='paused', revision=1).status_code, 200)
        response = self.act(action)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.provider.writes, [('branch', action['id'])])
        self.assertEqual(self.call('GET', '/goals/first-answer').json()['actions'][0]['status'], 'reconcile_required')
        self.assertEqual(self.act(action).status_code, 403)
        self.assertEqual(self.policy(state='revoked', revision=2).json()['policy']['state'], 'revoked')

    def test_removed_editor_cannot_complete_second_provider_write(self):
        editor, eid = self.join(self.owner, self.w, 'loop-removed@example.test', 'editor')
        editor_api = self.loop_client(editor)
        self.assertEqual(self.policy().status_code, 200)
        action = self.prepare()
        def remove_editor():
            with closing(app.connect()) as c, c:
                c.execute('DELETE FROM memberships WHERE workspace=? AND user_id=?', (self.w, eid))
        self.provider.hook = remove_editor
        self.assertEqual(self.act(action, client=editor_api).status_code, 403)
        self.assertEqual(self.provider.writes, [('branch', action['id'])])

    def test_expired_policy_and_departed_granting_owner_block_provider_writes(self):
        second, _ = self.join(self.owner, self.w, 'loop-second-owner@example.test', 'owner')
        second_api = self.loop_client(second)
        self.assertEqual(self.policy().status_code, 200)
        action = self.prepare()
        future = time.time() + 7200
        with patch('product_loop.time.time', return_value=future):
            self.assertEqual(self.act(action).status_code, 403)
        removed = second.delete('/api/workspaces/' + self.w + '/members/' + self.owner_id, headers=CSRF)
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(self.act(action, client=second_api).status_code, 403)
        self.assertEqual(self.provider.writes, [])
        self.assertEqual(self.policy(client=second_api, revision=1).status_code, 200)
        self.assertEqual(self.act(action, client=second_api).status_code, 200)
        self.assertEqual(self.provider.writes, [('branch', action['id']), ('pull_request', action['id'])])

    def test_uncertain_writes_reconcile_before_retry_and_keep_action_id(self):
        self.assertEqual(self.policy().status_code, 200)
        request_id = str(uuid.uuid4())
        action = self.prepare(request_id)
        self.assertEqual(self.prepare(request_id)['id'], action['id'])
        self.provider.timeout = True
        uncertain = self.act(action)
        self.assertEqual(uncertain.status_code, 200, uncertain.text)
        self.assertEqual(uncertain.json()['status'], 'reconcile_required')
        self.assertNotIn('secret provider', uncertain.text)
        self.assertEqual(self.act(action).status_code, 409)
        with closing(app.connect()) as c, c:
            c.execute('INSERT INTO loop_operations VALUES(?,?,?,?,?,?)',
                      (self.p['id'], action['id'], 'reconcile', 'pending', None, time.time()))
        reconciled = self.act(action, 'reconcile').json()
        self.assertEqual((reconciled['id'], reconciled['receipt']['pr_number']), (action['id'], 7))
        self.assertEqual(self.act(action).json()['receipt']['pr_number'], 7)
        self.assertEqual(self.provider.writes, [('branch', action['id'])])

    def test_concurrent_action_intent_prevents_duplicate_provider_writes(self):
        self.assertEqual(self.policy().status_code, 200)
        action = self.prepare()
        entered, proceed = threading.Event(), threading.Event()
        def hold():
            entered.set()
            self.assertTrue(proceed.wait(5))
        self.provider.hook = hold
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.act, action)
            self.assertTrue(entered.wait(5))
            second = self.act(action)
            self.assertEqual(second.status_code, 409, second.text)
            self.assertEqual(self.act(action, 'reconcile').status_code, 409)
            with closing(app.connect()) as c:
                self.assertEqual(c.execute("SELECT status FROM loop_operations WHERE operation='draft-pr'").fetchone()[0], 'pending')
            proceed.set()
            self.assertEqual(first.result().status_code, 200)
        self.assertEqual(self.provider.writes, [('branch', action['id']), ('pull_request', action['id'])])
        self.assertEqual(self.act(action).status_code, 200)
        self.assertEqual(len(self.provider.writes), 2)

    def test_changed_goal_blocks_stale_action_and_healthy_path_has_no_patch(self):
        self.assertEqual(self.policy().status_code, 200)
        action = self.prepare()
        self.provider.definition['measurement']['entry']['event'] = 'codex.question.v2.started'
        self.source['content_sha256'] = digest(self.provider.definition)
        self.source['commit'] = 'c' * 40
        self.assertEqual(self.call('PUT', '/goals/first-answer/sync', self.source).status_code, 200)
        self.assertEqual(self.act(action).status_code, 409)
        self.assertEqual(self.provider.writes, [])
        self.assertEqual(self.call('POST', '/goals/first-answer/prepare', {'request_id': action['request_id']}).status_code, 409)
        new_action = self.prepare()
        self.assertEqual(self.act(new_action).status_code, 403)
        self.assertEqual(self.policy(revision=1).status_code, 200)
        self.provider.healthy = True
        healthy = self.prepare()
        self.assertEqual(healthy['status'], 'healthy')
        self.assertNotIn('files', healthy['plan'])
        self.assertEqual(self.call('GET', '/goals/first-answer').json()['next_action'], 'no_change')

    def test_repair_rejects_unsupported_completion_environment_and_filters(self):
        for change in ['completion', 'environment', 'filter']:
            self.provider.definition = definition()
            if change == 'completion':
                self.provider.definition['measurement']['completion']['event'] = 'invoice.paid'
            elif change == 'environment':
                self.provider.definition['mapping']['environment'] = 'production'
            else:
                self.provider.definition['measurement']['completion']['filters'] = [{'property': 'quality_passed', 'value': True}]
            self.source['content_sha256'] = digest(self.provider.definition)
            self.assertEqual(self.call('PUT', '/goals/first-answer/sync', self.source).status_code, 200)
            response = self.call('POST', '/goals/first-answer/prepare', {'request_id': str(uuid.uuid4())})
            self.assertEqual(response.status_code, 422, response.text)
            if change == 'environment':
                dossier = self.call('GET', '/goals/first-answer').json()
                self.assertFalse(dossier['tracking_repair_available'])
                self.assertEqual(dossier['stages']['tracking'], 'service_managed')
                self.assertEqual(dossier['next_action'], 'collect_evidence')
        self.assertEqual(self.provider.writes, [])

    def test_merge_and_release_remain_separate_and_release_authority_is_needed(self):
        self.assertEqual(self.policy(operations=['draft-pr', 'merge']).status_code, 200)
        action = self.prepare()
        self.assertEqual(self.act(action).status_code, 200)
        self.assertEqual(self.act(action, 'merge').status_code, 403)
        self.assertEqual(self.act(action, 'reconcile').status_code, 200)
        self.assertEqual(self.policy(operations=['draft-pr', 'merge', 'release-on-merge'], revision=1).status_code, 200)
        merged = self.act(action, 'merge')
        self.assertEqual(merged.status_code, 200, merged.text)
        view = self.call('GET', '/goals/first-answer').json()
        self.assertEqual((view['stages']['merge'], view['stages']['release'], view['next_action']), ('merged', 'release_pending', 'check_release'))

    def test_comparisons_reject_missing_denominators_coverage_meaning_and_sources(self):
        empty = self.observe(self.window(end=12, through=36))
        late = self.observe(self.window(start=24, end=48, through=48))
        unknown = self.call('POST', '/goals/first-answer/compare', {'before': empty['id'], 'after': late['id']}).json()
        self.assertEqual(unknown['status'], 'unknown')
        self.assertEqual(unknown['rate_difference'], None)
        self.assertIn('before_zero_denominator', unknown['reasons'])
        self.send('codex.question.started', 'early', 1)
        self.send('codex.question.started', 'later', 37)
        before = self.observe(self.window(end=12, through=36))
        after = self.observe(self.window(start=36, end=40, through=64))
        comparable = self.call('POST', '/goals/first-answer/compare', {'before': before['id'], 'after': after['id']}).json()
        self.assertEqual((comparable['status'], comparable['rate_difference'], comparable['post_release_comparison']), ('unknown', None, False))
        self.assertEqual(comparable['unbound_rate_difference'], 0.0)
        self.assertEqual(comparable['release_limitation'], 'no_verified_release_receipt')
        self.provider.definition['measurement']['completion']['event'] = 'codex.answer.v2.ready'
        self.source['content_sha256'] = digest(self.provider.definition)
        self.assertEqual(self.call('PUT', '/goals/first-answer/sync', self.source).status_code, 200)
        changed = self.observe(self.window(start=36, end=40, through=64))
        mismatch = self.call('POST', '/goals/first-answer/compare', {'before': before['id'], 'after': changed['id']}).json()
        self.assertEqual(mismatch['status'], 'unknown')
        self.assertIn('measurement_revision_changed', mismatch['reasons'])

    def test_external_attestation_cannot_claim_independent_quality_or_mix_native(self):
        native = self.observe(self.window(end=12, through=36))
        payload = {**self.window(start=24, end=36, through=60),
                   'measurement_revision': native['measurement_revision'], 'mapping_revision': native['mapping_revision'],
                   'source_url': 'https://app.example/report/first-answer', 'query_sha256': 'a' * 64,
                   'entrants': 3, 'mature': 3, 'waiting': 0, 'qualified': 2, 'failed': 0, 'unassessed': 0,
                   'timed_out': 1, 'technical_failures': 1, 'rate': 2 / 3}
        id = str(uuid.uuid4())
        response = self.call('PUT', '/goals/first-answer/external/' + id, payload)
        self.assertEqual(response.status_code, 200, response.text)
        external = response.json()
        self.assertEqual((external['source'], external['qualified'], external['unassessed'], external['rate']), ('externally_attested', 0, 2, None))
        self.assertEqual(external['attested_counts']['qualified'], 2)
        self.assertEqual(self.call('PUT', '/goals/first-answer/external/' + id, payload).json(), external)
        self.assertEqual(self.call('PUT', '/goals/first-answer/external/' + id, {**payload, 'verified': True}).status_code, 422)
        result = self.call('POST', '/goals/first-answer/compare', {'before': native['id'], 'after': id}).json()
        self.assertIn('source_changed', result['reasons'])
        self.assertEqual(result['status'], 'unknown')

    def test_external_contract_gap_has_no_invented_counts_and_is_not_comparable(self):
        payload = {'kind': 'contract_gap', 'source_url': 'https://us.posthog.com/project/39922',
                   'query_sha256': 'c' * 64, 'checked_at': stamp(time.time()),
                   'missing_events': ['codex.connection.started', 'codex.answer.ready']}
        id = str(uuid.uuid4())
        response = self.call('PUT', '/goals/first-answer/external/' + id, payload)
        self.assertEqual(response.status_code, 200, response.text)
        receipt = response.json()
        self.assertEqual((receipt['source'], receipt['entrants'], receipt['mature'], receipt['rate'], receipt['next_action']),
                         ('externally_attested', None, None, None, 'prepare_tracking'))
        self.assertEqual(receipt['missing_events'], ['codex.connection.started', 'codex.answer.ready'])
        self.assertEqual(receipt['coverage']['complete'], False)
        self.provider.definition['mapping']['environment'] = 'production'
        self.source['content_sha256'] = digest(self.provider.definition)
        self.assertEqual(self.call('PUT', '/goals/first-answer/sync', self.source).status_code, 200)
        production = self.call('PUT', '/goals/first-answer/external/' + str(uuid.uuid4()), payload)
        self.assertEqual(production.status_code, 200, production.text)
        dossier = self.call('GET', '/goals/first-answer').json()
        self.assertFalse(dossier['tracking_repair_available'])
        self.assertEqual(dossier['next_action'], 'collect_evidence')
        self.provider.definition['mapping']['environment'] = 'local'
        self.source['content_sha256'] = digest(self.provider.definition)
        self.assertEqual(self.call('PUT', '/goals/first-answer/sync', self.source).status_code, 200)
        self.assertEqual(self.call('PUT', '/goals/first-answer/external/' + id, payload).json(), receipt)
        native = self.observe()
        comparison = self.call('POST', '/goals/first-answer/compare', {'before': id, 'after': native['id']}).json()
        self.assertEqual(comparison['status'], 'unknown')
        self.assertIn('observation_window_unavailable', comparison['reasons'])
        self.assertIn('before_denominator_unavailable', comparison['reasons'])
        self.assertEqual(comparison['rate_difference'], None)

    def test_release_comparison_requires_exact_artifact_environment_and_separation(self):
        self.assertEqual(self.policy(operations=['draft-pr', 'merge', 'release-on-merge']).status_code, 200)
        action = self.prepare()
        self.assertEqual(self.act(action).status_code, 200)
        self.assertEqual(self.act(action, 'merge').status_code, 200)
        self.send('codex.question.started', 'before', 1, {'release': 'b' * 40})
        self.send('codex.question.started', 'after', 37, {'release': 'e' * 40})
        before = self.observe(self.window(end=12, through=30, release='b' * 40))
        after = self.observe(self.window(start=36, end=40, through=64, release='e' * 40))
        query = {'before': before['id'], 'after': after['id'], 'action_id': action['id']}
        pending = self.call('POST', '/goals/first-answer/compare', query).json()
        self.assertEqual((pending['status'], pending['post_release_comparison']), ('unknown', False))
        release = {'verified': True, 'commit': 'e' * 40, 'artifact_sha256': 'a' * 64,
                   'environment': 'local', 'released_at': stamp(self.t + 32 * 3600)}
        self.provider.remote.update(release_status='released', release=release)
        self.assertEqual(self.act(action, 'reconcile').status_code, 200)
        exact = self.call('POST', '/goals/first-answer/compare', query).json()
        self.assertEqual((exact['post_release_comparison'], exact['release_limitation']), (True, None))
        self.provider.remote['release']['artifact_sha256'] = 'b' * 64
        self.assertEqual(self.act(action, 'reconcile').status_code, 200)
        wrong = self.call('POST', '/goals/first-answer/compare', query).json()
        self.assertEqual((wrong['post_release_comparison'], wrong['release_limitation']), (False, 'no_verified_release_receipt'))
        self.assertEqual(self.call('GET', '/goals/first-answer').json()['stages']['release'], 'release_pending')

    def test_release_filter_cannot_reset_first_entry_clock_or_accept_wrong_version_completion(self):
        base, head = 'b' * 40, 'e' * 40
        self.send('codex.question.started', 'retry', 0, {'release': base})
        self.send('codex.question.started', 'retry', 23, {'release': head})
        self.send('codex.answer.ready', 'retry', 25, {'release': head, 'quality_passed': True})
        self.send('codex.question.started', 'wrong-completion', 1, {'release': head})
        self.send('codex.answer.ready', 'wrong-completion', 2, {'release': base})
        self.send('codex.question.started', 'missing-label', 1)
        result = self.observe(self.window(release=head))
        self.assertEqual({key: result[key] for key in ['observed_starters', 'entrants', 'mature', 'qualified', 'timed_out',
                                                     'release_excluded_starters', 'release_missing_starters',
                                                     'release_mismatched_completions', 'completion_events_observed']},
                         {'observed_starters': 3, 'entrants': 1, 'mature': 1, 'qualified': 0, 'timed_out': 1,
                          'release_excluded_starters': 1, 'release_missing_starters': 1,
                          'release_mismatched_completions': 1, 'completion_events_observed': 0})
        self.assertEqual((result['coverage']['complete'], result['coverage']['release_gap']), (False, True))

    def test_signal_needs_completion_and_exact_verified_release(self):
        evidence = self.observe()
        self.send('codex.question.started', 'one', 37, {'release': 'e' * 40})
        view = self.call('GET', '/goals/first-answer').json()
        self.assertEqual(view['stages']['signal'], 'not_observed')
        self.send('analytics.pipeline', 'one', 38, {'release': 'e' * 40})
        self.observe(self.window(start=36, end=40, through=64))
        self.assertEqual(self.call('GET', '/goals/first-answer').json()['stages']['signal'], 'not_observed')
        self.send('codex.answer.ready', 'one', 38, {'release': 'e' * 40, 'observation_id': evidence['id']})
        self.observe(self.window(start=36, end=40, through=64))
        self.assertEqual(self.call('GET', '/goals/first-answer').json()['stages']['signal'], 'completion_observed_unbound')
        self.assertEqual(self.policy(operations=['draft-pr', 'merge', 'release-on-merge']).status_code, 200)
        action = self.prepare()
        self.assertEqual(self.act(action).status_code, 200)
        self.assertEqual(self.act(action, 'merge').status_code, 200)
        self.provider.remote.update(release_status='released', release={'verified': True, 'commit': 'e' * 40,
                                   'artifact_sha256': 'a' * 64, 'environment': 'local', 'released_at': stamp(self.t + 32 * 3600).replace('+00:00', 'Z')})
        self.assertEqual(self.act(action, 'reconcile').status_code, 200)
        self.assertEqual(self.call('GET', '/goals/first-answer').json()['stages']['signal'], 'completion_observed_unbound')
        self.observe(self.window(start=36, end=40, through=64, release='e' * 40))
        self.assertEqual(self.call('GET', '/goals/first-answer').json()['stages']['signal'], 'verified_release_completion_observed')

    def test_pure_evaluator_strict_order_stable_ids_filters_and_boundary_deadline(self):
        measurement = definition()['measurement']
        measurement['entry']['filters'] = [{'property': 'tier', 'value': True}]
        def row(id, name, ts, **properties):
            return {'id': id, 'name': name, 'ts': ts, 'received': 1, 'distinct_id': 'workspace',
                    'properties': {'environment': 'local', **properties}}
        rows = [row('numeric', 'codex.question.started', 0, tier=1), row('start', 'codex.question.started', 1, tier=True),
                row('tie', 'codex.answer.ready', 1), row('exact', 'codex.answer.ready', 86401),
                row('exact', 'codex.answer.ready', 86401), row('late', 'codex.answer.ready', 86402)]
        result = count_first_entry_outcomes(rows, measurement, {'entry_start': 0, 'entry_end': 2, 'observed_through': 86401, 'as_of': 10},
                          {'exact': ['qualified']}, 'local')
        self.assertEqual((result['entrants'], result['qualified'], result['timed_out'], result['rate']), (1, 1, 0, 1.0))
