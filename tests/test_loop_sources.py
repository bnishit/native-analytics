import base64
import copy
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import SecretStr

from loop_sources import Connections, posthog_observe, railway_release, validate_connection


ID = 'ad3b09f1-3248-478d-979d-1811f41a1312'
PROJECT_ID = '43613440-a612-4c3d-ad67-17dab13cc69b'
ENVIRONMENT_ID = 'f7cf85b5-29b7-4ed0-a61d-cf0f9ab14f40'
SERVICE_ID = 'f3276bc6-953c-4f2e-9011-29c1f629c463'
DEPLOYMENT_ID = 'dd67ecaa-9ce8-429e-9e58-fd42972895a3'
COMMIT = 'a' * 40
TOKEN = 'private-source-credential'
CONFIG = {'region': 'us', 'project_id': 42}
RAILWAY_CONFIG = {'project_id': PROJECT_ID, 'environment_id': ENVIRONMENT_ID, 'service_id': SERVICE_ID}
WINDOW = {'entry_start': '2026-10-01T00:00:00Z', 'entry_end': '2026-10-02T00:00:00Z', 'as_of': '2026-10-03T00:00:00Z'}
DEFINITION = {
    'measurement': {'kind': 'event-outcome-v1', 'unit': 'logged_event', 'event': 'http.request',
                    'filters': [{'property': 'method', 'op': 'eq', 'value': 'GET'}],
                    'outcome_property': 'status', 'success_values': [200], 'failure_values': [500], 'min_sample_size': 25},
    'mapping': {'kind': 'posthog', 'environment': 'production', 'environment_property': 'environment',
                'release_property': 'release_sha', 'connection_id': ID, 'connection_revision': 1},
}
COLUMNS = ['total', 'success', 'failure', 'unclassified', 'universe_total', 'environment_missing', 'release_missing']


def connection(kind='posthog'):
    return {'id': ID, 'revision': 1, 'kind': kind, 'config': CONFIG if kind == 'posthog' else RAILWAY_CONFIG,
            'secret': SecretStr(TOKEN)}


def aggregate(values=None, **extra):
    return {'columns': COLUMNS, 'results': [values or [8, 5, 2, 1, 12, 2, 3]], **extra}


def deployment(**changes):
    return {'id': DEPLOYMENT_ID, 'projectId': PROJECT_ID, 'environmentId': ENVIRONMENT_ID, 'serviceId': SERVICE_ID,
            'status': 'SUCCESS', 'createdAt': '2026-10-01T00:00:00Z', 'statusUpdatedAt': '2026-10-01T00:01:00Z',
            'environment': {'id': ENVIRONMENT_ID, 'name': 'production'}, 'meta': {'commitHash': COMMIT}, **changes}


def latest_deployment(node):
    return {'data': {'serviceInstance': {'latestDeployment': node}}}


class Transport:
    def __init__(self, data, status=200):
        self.data, self.status = data, status
        self.requests = []
        self.closed = False

    def connection(self, host, timeout):
        owner = self

        class HTTP:
            sock = None

            def request(self, method, path, body=None, headers=None):
                owner.requests.append({'host': host, 'timeout': timeout, 'method': method, 'path': path,
                                       'body': json.loads(body) if body else None, 'headers': headers})
                if isinstance(owner.data, Exception):
                    raise owner.data

            def getresponse(self):
                class Response:
                    status = owner.status
                    remaining = owner.data if isinstance(owner.data, bytes) else json.dumps(owner.data).encode()

                    def read1(self, size):
                        chunk, self.remaining = self.remaining[:size], self.remaining[size:]
                        return chunk

                return Response()

            def close(self):
                owner.closed = True

        return HTTP()


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = os.path.join(self.temp.name, 'source.db')

        def connect():
            c = sqlite3.connect(self.path)
            c.row_factory = sqlite3.Row
            c.execute('PRAGMA foreign_keys=ON')
            return c

        self.connect = connect
        self.store = Connections(connect)
        with closing(connect()) as c, c:
            c.execute('CREATE TABLE projects(id TEXT PRIMARY KEY)')
            c.executemany('INSERT INTO projects VALUES(?)', [('clinic',), ('other',)])
            Connections.init_db(c)
        self.env = patch.dict(os.environ, {'AFTERSPAN_CONNECTION_KEY': base64.urlsafe_b64encode(b'k' * 32).decode()})
        self.env.start()
        self.addCleanup(self.env.stop)

    def put(self, revision=0, secret=TOKEN, **changes):
        return self.store.put('clinic', ID, revision, changes.get('kind', 'posthog'), changes.get('config', CONFIG), SecretStr(secret))

    def test_metadata_never_returns_credential_and_storage_is_ciphertext(self):
        public = self.put()
        self.assertEqual(public, {'id': ID, 'revision': 1, 'kind': 'posthog', 'status': 'configured', 'target': CONFIG})
        self.assertEqual(self.store.list('clinic'), [public])
        private = self.store.resolve('clinic', ID, 'posthog')
        self.assertEqual(private['secret'].get_secret_value(), TOKEN)
        self.assertNotIn(TOKEN, repr(private))
        with closing(self.connect()) as c:
            stored = c.execute('SELECT secret FROM loop_provider_connections').fetchone()[0]
        self.assertIsInstance(stored, bytes)
        self.assertNotIn(TOKEN.encode(), stored)

    def test_rotation_and_revocation_are_append_only_and_require_current_revision(self):
        self.put()
        self.put(1, 'rotated-credential')
        self.assertEqual(self.store.resolve('clinic', ID)['secret'].get_secret_value(), 'rotated-credential')
        for operation in [lambda: self.put(1), lambda: self.store.revoke('clinic', ID, 1)]:
            with self.assertRaises(HTTPException) as error:
                operation()
            self.assertEqual(error.exception.status_code, 409)
        revoked = self.store.revoke('clinic', ID, 2)
        self.assertEqual(revoked['revision'], 3)
        self.assertEqual(revoked['status'], 'revoked')
        with self.assertRaises(HTTPException) as error:
            self.store.resolve('clinic', ID)
        self.assertEqual(error.exception.status_code, 409)
        with closing(self.connect()) as c:
            rows = c.execute('SELECT revision,status FROM loop_provider_connections ORDER BY revision').fetchall()
        self.assertEqual([tuple(row) for row in rows], [(1, 'configured'), (2, 'configured'), (3, 'revoked')])

    def test_wrong_project_cannot_read_rotate_or_revoke_connection(self):
        self.put()
        self.assertEqual(self.store.list('other'), [])
        for operation in [lambda: self.store.resolve('other', ID), lambda: self.store.revoke('other', ID, 1)]:
            with self.assertRaises(HTTPException) as error:
                operation()
            self.assertEqual(error.exception.status_code, 404)
        with self.assertRaises(HTTPException) as error:
            self.store.put('other', ID, 1, 'posthog', CONFIG, SecretStr(TOKEN))
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.store.list('clinic')[0]['revision'], 1)

    def test_missing_or_invalid_key_fails_closed(self):
        self.put()
        for value in ['', 'wrong', base64.urlsafe_b64encode(b'short').decode()]:
            with patch.dict(os.environ, {'AFTERSPAN_CONNECTION_KEY': value}):
                for operation in [lambda: self.put(1), lambda: self.store.resolve('clinic', ID)]:
                    with self.assertRaises(HTTPException) as error:
                        operation()
                    self.assertEqual(error.exception.status_code, 503)
                    self.assertNotIn(TOKEN, error.exception.detail)

    def test_concurrent_rotation_accepts_one_expected_revision(self):
        self.put()

        def rotate(_):
            try:
                return self.put(1)['revision']
            except HTTPException as error:
                return error.status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(rotate, range(2)))
        self.assertEqual(sorted(results), [2, 409])
        self.assertEqual(self.store.list('clinic')[0]['revision'], 2)

    def test_ciphertext_is_bound_to_project_id_revision_and_kind(self):
        self.put()
        for field, value in [('project', 'other'), ('id', DEPLOYMENT_ID), ('revision', 2), ('kind', 'github')]:
            original = {'project': 'clinic', 'id': ID, 'revision': 1, 'kind': 'posthog'}[field]
            with self.subTest(field=field), closing(self.connect()) as c, c:
                c.execute(f'UPDATE loop_provider_connections SET {field}=?', (value,))
            with self.assertRaises(HTTPException) as error:
                self.store.resolve(value if field == 'project' else 'clinic', value if field == 'id' else ID)
            self.assertEqual(error.exception.status_code, 503)
            with closing(self.connect()) as c, c:
                c.execute(f'UPDATE loop_provider_connections SET {field}=?', (original,))

    def test_config_is_typed_and_cannot_select_arbitrary_hosts(self):
        for kind, config in [('posthog', {'region': 'https://evil.example', 'project_id': 42}),
                             ('posthog', {'region': 'us', 'project_id': True}),
                             ('posthog', {**CONFIG, 'host': 'evil.example'}),
                             ('railway', {**RAILWAY_CONFIG, 'service_id': 'not-a-uuid'}),
                             ('github', {'repository': '../secret'})]:
            with self.subTest(kind=kind, config=config), self.assertRaises(HTTPException) as error:
                self.put(kind=kind, config=config)
            self.assertEqual(error.exception.status_code, 422)


class PostHogTests(unittest.TestCase):
    def observe(self, data, definition=None, window=None, source=None, status=200):
        transport = Transport(data, status)
        with patch('loop_sources.http.client.HTTPSConnection', transport.connection):
            receipt = posthog_observe(source or connection(), definition or DEFINITION, window or WINDOW)
        return receipt, transport

    def test_returns_only_disjoint_aggregates_with_full_universe_coverage(self):
        receipt, transport = self.observe(aggregate(private_rows=[TOKEN], hogql=TOKEN))
        self.assertEqual(receipt['counts'], {'total': 8, 'success': 5, 'failure': 2, 'unclassified': 1})
        self.assertEqual(receipt['coverage'], {'source_total': 12, 'environment_missing': 2, 'release_missing': 3, 'status': 'unknown'})
        self.assertEqual(receipt['provenance'], 'provider_aggregate')
        self.assertIs(receipt['source_verified'], True)
        self.assertNotIn(TOKEN, json.dumps(receipt))
        request = transport.requests[0]
        self.assertEqual((request['host'], request['method'], request['path']), ('us.posthog.com', 'POST', '/api/projects/42/query/'))
        self.assertEqual(request['headers']['Authorization'], 'Bearer ' + TOKEN)
        self.assertTrue(transport.closed)
        query = request['body']['query']['query']
        self.assertIn('count() AS universe_total', query)
        self.assertIn("created_at <= toDateTime64('2026-10-03 00:00:00.000000', 6, 'UTC')", query)
        self.assertNotIn('environment', query.split(' FROM events WHERE ', 1)[1])

    def test_eu_and_exact_release_are_service_owned(self):
        source = connection()
        source['config'] = {'region': 'eu', 'project_id': 123}
        receipt, transport = self.observe(aggregate(), window={**WINDOW, 'release': COMMIT}, source=source)
        self.assertEqual(transport.requests[0]['host'], 'eu.posthog.com')
        self.assertEqual(receipt['release'], COMMIT)
        self.assertIn(COMMIT, transport.requests[0]['body']['query']['query'])

    def test_zero_is_only_returned_from_a_complete_zero_response(self):
        receipt, _ = self.observe(aggregate([0] * 7))
        self.assertEqual(receipt['counts'], {'total': 0, 'success': 0, 'failure': 0, 'unclassified': 0})
        self.assertEqual(receipt['coverage']['status'], 'complete')
        missing, _ = self.observe({'columns': COLUMNS})
        self.assertIsNone(missing['counts'])
        self.assertEqual(missing['reason'], 'malformed_counts')

    def test_malformed_counts_are_unknown(self):
        for values in [[True, 0, 0, 1, 1, 0, 0], ['8', 5, 2, 1, 12, 2, 3], [8, 5, 2, 2, 12, 2, 3],
                       [8, -1, 8, 1, 12, 2, 3], [8, 5, 2, 1, 7, 0, 0], [8, 5, 2, 1, 12, 5, 0],
                       [8, 5, 2, 1, 12, 2, 13], [8.0, 5, 2, 1, 12, 2, 3]]:
            with self.subTest(values=values):
                receipt, _ = self.observe(aggregate(values))
                self.assertIsNone(receipt['counts'])
                self.assertIs(receipt['source_verified'], False)
                self.assertEqual(receipt['reason'], 'malformed_counts')

    def test_partial_running_cached_truncated_or_wrong_timezone_are_unknown(self):
        for changes in [{'hasMore': True}, {'partial': True}, {'truncated': True}, {'status': 'running'},
                        {'is_cached': True}, {'timezone': 'Asia/Kolkata'}, {'query_status': {'complete': False}},
                        {'query_status': {'complete': True, 'partial': True}},
                        {'query_status': {'complete': True, 'error_message': TOKEN}}, {'errors': [TOKEN]}]:
            with self.subTest(changes=changes):
                receipt, _ = self.observe(aggregate(**changes))
                self.assertEqual(receipt['reason'], 'partial_response')
                self.assertIsNone(receipt['counts'])
                self.assertNotIn(TOKEN, json.dumps(receipt))

    def test_provider_errors_timeouts_redirects_and_large_responses_are_bounded(self):
        for data, status, reason in [(TimeoutError(TOKEN), 200, 'provider_timeout'),
                                     ({'error': TOKEN}, 403, 'provider_denied'),
                                     ({'location': 'https://evil.example'}, 302, 'provider_unavailable'),
                                     (b'x' * (128 * 1024 + 1), 200, 'response_too_large')]:
            with self.subTest(status=status, reason=reason):
                receipt, transport = self.observe(data, status=status)
                self.assertEqual(receipt['reason'], reason)
                self.assertIsNone(receipt['counts'])
                self.assertNotIn(TOKEN, json.dumps(receipt))
                self.assertEqual(len(transport.requests), 1)
                self.assertTrue(transport.closed)

    def test_definition_cannot_supply_sql_or_unsafe_property_names(self):
        for change in [{'query': 'SELECT * FROM events'}, {'outcome_property': 'status) FROM events'},
                       {'success_values': [True], 'failure_values': [True]}, {'unit': 'users'},
                       {'filters': [{'property': 'method', 'op': 'sql', 'value': 'GET'}]}]:
            definition = copy.deepcopy(DEFINITION)
            definition['measurement'].update(change)
            with self.subTest(change=change), self.assertRaises(HTTPException) as error:
                self.observe(aggregate(), definition=definition)
            self.assertEqual(error.exception.status_code, 422)

    def test_literal_values_cannot_escape_the_generated_query(self):
        definition = copy.deepcopy(DEFINITION)
        definition['measurement']['filters'][0]['value'] = "GET'); SELECT secret FROM credentials; --"
        receipt, transport = self.observe(aggregate(), definition=definition)
        query = transport.requests[0]['body']['query']['query']
        self.assertIn("GET\\'); SELECT secret FROM credentials; --", query)
        self.assertEqual(receipt['counts']['total'], 8)

    def test_stale_binding_and_invalid_windows_are_rejected_before_network(self):
        source = connection()
        source['revision'] = 2
        with self.assertRaises(HTTPException) as error:
            self.observe(aggregate(), source=source)
        self.assertEqual(error.exception.status_code, 409)
        for window in [{**WINDOW, 'as_of': WINDOW['entry_start']},
                       {**WINDOW, 'entry_start': '2026-10-01T00:00:00'},
                       {**WINDOW, 'entry_start': '2026-01-01T00:00:00Z'},
                       {**WINDOW, 'entry_end': WINDOW['entry_start']}]:
            with self.subTest(window=window), self.assertRaises(HTTPException) as error:
                self.observe(aggregate(), window=window)
            self.assertEqual(error.exception.status_code, 422)


class RailwayTests(unittest.TestCase):
    def release(self, data, environment='production'):
        transport = Transport(data)
        with patch('loop_sources.http.client.HTTPSConnection', transport.connection):
            receipt = railway_release(connection('railway'), COMMIT, environment)
        self.assertNotIn(TOKEN, json.dumps(receipt))
        self.assertEqual(transport.requests[0]['host'], 'backboard.railway.app')
        self.assertNotIn('mutation', transport.requests[0]['body']['query'])
        self.assertNotIn('logs', transport.requests[0]['body']['query'])
        self.assertNotIn('variables(', transport.requests[0]['body']['query'])
        return receipt, transport

    def test_exact_success_receipt_includes_provider_timestamp_and_target(self):
        receipt, transport = self.release(latest_deployment(deployment(meta={'commitHash': COMMIT, 'private': TOKEN})))
        self.assertEqual((receipt['status'], receipt['verified'], receipt['deployment_id']), ('released', True, DEPLOYMENT_ID))
        self.assertEqual(receipt['released_at'], '2026-10-01T00:01:00+00:00')
        self.assertEqual(transport.requests[0]['body']['variables'], {'environmentId': ENVIRONMENT_ID, 'serviceId': SERVICE_ID})

    def test_wrong_commit_environment_service_or_status_never_proves_release(self):
        for changes in [{'meta': {'commitHash': 'b' * 40}}, {'environmentId': PROJECT_ID},
                        {'serviceId': PROJECT_ID}, {'projectId': SERVICE_ID}, {'status': 'DEPLOYING'},
                        {'environment': {'id': ENVIRONMENT_ID, 'name': 'staging'}},
                        {'statusUpdatedAt': None}, {'statusUpdatedAt': '2026-10-01T00:01:00'}]:
            with self.subTest(changes=changes):
                receipt, _ = self.release(latest_deployment(deployment(**changes)))
                self.assertEqual(receipt['status'], 'pending')
                self.assertIs(receipt['verified'], False)
                self.assertNotIn('released_at', receipt)

    def test_only_latest_deployment_can_prove_the_requested_release(self):
        newer = deployment(createdAt='2026-10-02T00:00:00Z', statusUpdatedAt='2026-10-02T00:01:00Z', meta={'commitHash': 'b' * 40})
        data = latest_deployment(newer)
        data['historical_deployments'] = [deployment()]
        receipt, transport = self.release(data)
        self.assertEqual(receipt['status'], 'pending')
        self.assertEqual(receipt['reason'], 'active_deployment_commit_mismatch')
        self.assertIn('latestDeployment', transport.requests[0]['body']['query'])

    def test_provider_errors_and_missing_deployment_do_not_return_release(self):
        for data in [{'errors': [{'message': TOKEN}]}, latest_deployment(None), latest_deployment([deployment()])]:
            receipt, _ = self.release(data)
            self.assertEqual(receipt['status'], 'pending')
            self.assertIs(receipt['verified'], False)


class PermissionProbeTests(unittest.TestCase):
    def test_posthog_probe_is_a_zero_row_aggregate_read_and_does_not_claim_readonly_key(self):
        transport = Transport({'columns': ['total'], 'results': [[0]]})
        with patch('loop_sources.http.client.HTTPSConnection', transport.connection):
            result = validate_connection('posthog', CONFIG, SecretStr(TOKEN))
        self.assertEqual(result, {'kind': 'posthog', 'target': CONFIG, 'read_access': 'verified', 'credential_scope': 'unverified'})
        self.assertEqual(transport.requests[0]['body']['query']['query'], 'SELECT count() AS total FROM events WHERE 1 = 0 LIMIT 1')

    def test_failed_probe_never_echoes_provider_credential_or_body(self):
        transport = Transport({'detail': TOKEN}, 403)
        with patch('loop_sources.http.client.HTTPSConnection', transport.connection), self.assertRaises(HTTPException) as error:
            validate_connection('posthog', CONFIG, SecretStr(TOKEN))
        self.assertEqual(error.exception.status_code, 503)
        self.assertNotIn(TOKEN, error.exception.detail)
