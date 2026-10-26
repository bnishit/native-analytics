import base64
import copy
import json
import os
import time
import unittest
import uuid
from contextlib import closing
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from fastapi import HTTPException
from pydantic import SecretStr

import app
from loop_onboarding import canonical
import test_workspaces
CSRF = test_workspaces.CSRF


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class RepoProvider:
    def __init__(self):
        self.snapshot = {'repository': 'owner/application', 'repository_id': 123, 'commit': 'a' * 40,
                         'base': 'main', 'tree_sha': 'b' * 40, 'scanner_version': 'event-literals-v1',
                         'files': [{'path': 'src/analytics.ts', 'blob_sha': 'c' * 40, 'content_sha256': 'd' * 64}],
                         'findings': {'frameworks': ['next'], 'events': [
                             {'name': 'http.request', 'path': 'src/analytics.ts', 'line': 4, 'sink': 'native'}]},
                         'coverage': {'complete': False, 'reasons': ['file_count_limit']}, 'workflows': {}}

    def _head(self, base):
        return {'commit': {'sha': 'a' * 40}}

    def scan(self, commit, base):
        return copy.deepcopy(self.snapshot | {'commit': commit, 'base': base})

    def prepare_artifact(self, scan, definition, files):
        return {'kind': 'reviewed-artifact-v1', 'repository': scan['repository'], 'repository_id': 123,
                'base': scan['base'], 'base_sha': scan['commit'], 'goal_path': '.afterspan/goals/' + definition['id'] + '.json',
                'goal_sha256': 'e' * 64, 'files': files, 'workflows': {}, 'artifact_sha256': 'f' * 64,
                'scan': scan, 'definition': definition, 'line_counts': {'added': 2, 'deleted': 0}}

    def reconcile(self, plan, receipt):
        return receipt


class OnboardingTests(unittest.TestCase):
    user = test_workspaces.WorkspaceTests.user
    token = test_workspaces.WorkspaceTests.token
    workspace = test_workspaces.WorkspaceTests.workspace
    project = test_workspaces.WorkspaceTests.project
    key = test_workspaces.WorkspaceTests.key

    def setUp(self):
        test_workspaces.WorkspaceTests.setUp(self)
        self.env_crypto = patch.dict(os.environ, {'AFTERSPAN_CONNECTION_KEY': base64.urlsafe_b64encode(b'x' * 32).decode()})
        self.env_crypto.start()
        self.owner, self.owner_id = self.user('repo-owner@example.test')
        self.w = self.workspace(self.owner)
        self.p = self.project(self.owner, self.w)
        self.loop = app.product_loop
        self.provider = RepoProvider()
        self.provider_patch = patch.object(self.loop.onboarding, 'provider', return_value=self.provider)
        self.provider_patch.start()
        self.connection = self.loop.onboarding.connections.put(self.p['id'], str(uuid.uuid4()), 0, 'github',
                                                               {'repository': 'owner/application'}, SecretStr('private-provider-value'))
        self.scan_id = str(uuid.uuid4())
        self.scan = self.call('PUT', '/scans/' + self.scan_id, {'connection_id': self.connection['id']}).json()
        self.definition = {'schema': 'afterspan-repo-goal-v2', 'id': 'api-read-outcomes',
                           'question': 'Do the logged public API reads return successful responses?', 'scan_id': self.scan_id,
                           'measurement': {'kind': 'event-outcome-v1', 'unit': 'logged_event', 'event': 'http.request',
                                           'filters': [{'property': 'method', 'op': 'eq', 'value': 'GET'}],
                                           'outcome_property': 'status', 'success_values': [200], 'failure_values': [500],
                                           'min_sample_size': 2},
                           'mapping': {'kind': 'native', 'environment': 'production', 'environment_property': 'environment',
                                       'release_property': 'release_sha'},
                           'tracking': {'kind': 'reviewed-artifact-v1', 'dependencies': ['src/analytics.ts']}}
        self.adopt()

    def tearDown(self):
        self.provider_patch.stop()
        self.env_crypto.stop()
        test_workspaces.WorkspaceTests.tearDown(self)

    def call(self, method, path, body=None, client=None, project=None, query=''):
        target = '/api/loop' + path + '?project=' + (project or self.p['id']) + query
        response = (client or self.owner).request(method, target, json=body, headers=CSRF)
        if response.status_code == 202:
            job = (client or self.owner).get(response.json()['status_path'] + '?project=' + (project or self.p['id']))
            self.assertEqual(job.status_code, 200, job.text)
            self.assertEqual(job.json()['status'], 'complete', job.text)
            response = (client or self.owner).request(method, target, json=body, headers=CSRF)
        return response

    def adopt(self):
        proposal = self.call('PUT', '/proposals/' + str(uuid.uuid4()), self.definition)
        self.assertEqual(proposal.status_code, 200, proposal.text)
        data = proposal.json()
        result = self.call('POST', '/goals/api-read-outcomes/adopt',
                           {'proposal_id': data['id'], 'proposal_sha256': data['proposal_sha256']})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def send(self, status=200, environment='production', release=None):
        properties = {'status': status, 'method': 'GET', 'environment': environment}
        if release:
            properties['release_sha'] = release
        response = self.client.post('/v1/events', headers={'Authorization': 'Bearer ' + self.p['write_key']},
                                    json={'events': [{'id': str(uuid.uuid4()), 'name': 'http.request',
                                                      'timestamp': stamp(time.time() - 60), 'properties': properties}]})
        self.assertEqual(response.status_code, 200, response.text)

    def observe(self, id=None, release=None):
        now = time.time()
        window = {'entry_start': stamp(now - 3600), 'entry_end': stamp(now), 'observed_through': stamp(now),
                  'as_of': stamp(now), 'release': release}
        response = self.call('PUT', '/goals/api-read-outcomes/observations/' + (id or str(uuid.uuid4())), window)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), window

    def test_owner_scan_proposal_adoption_and_same_id_replay(self):
        self.assertEqual(self.scan['commit'], 'a' * 40)
        self.assertEqual(self.call('PUT', '/scans/' + self.scan_id, {'connection_id': self.connection['id']}).json(), self.scan)
        changed = self.call('PUT', '/scans/' + self.scan_id, {'connection_id': self.connection['id'], 'commit': 'e' * 40})
        self.assertEqual(changed.status_code, 409)
        status = self.call('GET', '/goals/api-read-outcomes').json()
        self.assertEqual(status['stages']['release'], 'not_observed')
        self.assertEqual(status['dependency_status'], 'scan_bound_proposal')
        self.assertEqual(self.adopt()['goal']['goal_revision'], 1)

    def merged_proposal(self):
        scan = self.scan | {'connection_id': self.connection['id'], 'connection_revision': 1}
        plan = self.provider.prepare_artifact(scan, self.definition, [{'path': 'src/analytics.ts'}]) | {'status': 'prepared'}
        merged = {'adoption': 'merged-pr-v1', 'merged': True, 'status': 'merged',
                  'pr_number': 156, 'head_sha': 'b' * 40, 'merge_sha': 'c' * 40,
                  'artifact_sha256': plan['artifact_sha256']}
        value = {'definition': self.definition, 'scan': scan, 'plan': plan, 'merged_receipt': merged}
        first = patch.object(self.provider, 'merged_proposal', return_value=value, create=True)
        second = patch.object(self.provider, 'verify_adopted', return_value=merged, create=True)
        first.start(); second.start()
        self.addCleanup(first.stop); self.addCleanup(second.stop)
        id = str(uuid.uuid4())
        payload = {'connection_id': self.connection['id'], 'goal': self.definition['id'], 'pr_number': 156,
                   'base_sha': 'a' * 40, 'head_sha': 'b' * 40, 'merge_sha': 'c' * 40, 'goal_sha256': 'd' * 64}
        proposed = self.call('PUT', '/merged-artifacts/' + id, payload)
        self.assertEqual(proposed.status_code, 200, proposed.text)
        self.assertEqual(self.call('PUT', '/merged-artifacts/' + id, payload).json(), proposed.json())
        self.assertEqual(self.call('PUT', '/merged-artifacts/' + id, payload | {'pr_number': 157}).status_code, 409)
        return proposed.json()

    def test_merged_adoption_requires_owner_and_cannot_create_or_merge_a_pr(self):
        proposed = self.merged_proposal()
        editor = TestClient(app.app)
        editor.headers['Authorization'] = 'Bearer ' + self.key(self.owner, self.w, 'editor')['key']
        path = '/goals/api-read-outcomes/adopt'
        adopt = {'proposal_id': proposed['id'], 'proposal_sha256': proposed['proposal_sha256']}
        self.assertEqual(self.call('POST', path, adopt, editor).status_code, 403)
        self.assertEqual(self.call('POST', path, adopt | {'proposal_sha256': '0' * 64}).status_code, 409)
        owned = self.call('POST', path, adopt)
        self.assertEqual(owned.status_code, 200, owned.text)
        self.assertEqual(owned.json()['adopted_action_id'], proposed['id'])
        self.assertEqual(owned.json()['dependency_status'], 'verified_merged_artifact')
        self.assertEqual(owned.json()['actions'][0]['plan']['measurement_revision'], owned.json()['goal']['measurement_revision'])
        self.assertEqual(self.call('POST', path, adopt).json(), owned.json())
        for operation in ['draft-pr', 'merge']:
            response = self.call('POST', '/actions/' + proposed['id'] + '/' + operation, {}, editor)
            self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.call('POST', '/actions/' + proposed['id'] + '/reconcile', {}, editor).status_code, 200)

    def test_connection_rotation_invalidates_a_pending_merged_adoption(self):
        proposed = self.merged_proposal()
        self.loop.onboarding.connections.put(self.p['id'], self.connection['id'], 1, 'github',
                                            {'repository': 'owner/application'}, SecretStr('replacement-credential'))
        self.provider_patch.stop()
        response = self.call('POST', '/goals/api-read-outcomes/adopt',
                             {'proposal_id': proposed['id'], 'proposal_sha256': proposed['proposal_sha256']})
        self.assertEqual(response.status_code, 409)
        with closing(self.loop.connect()) as c:
            self.assertIsNone(c.execute('SELECT id FROM loop_actions WHERE id=?', (proposed['id'],)).fetchone())

    def test_connection_rotation_during_final_provider_read_cannot_save_adoption(self):
        proposed = self.merged_proposal()
        self.provider_patch.stop()
        onboarding = self.loop.onboarding

        def checked_provider(project, binding):
            type(onboarding).provider(onboarding, project, binding)
            return self.provider

        def rotate_during_response(*args):
            onboarding.connections.put(self.p['id'], self.connection['id'], 1, 'github',
                                       {'repository': 'owner/application'}, SecretStr('replacement-credential'))
            return proposed['merged_receipt']

        self.provider.verify_adopted.side_effect = rotate_during_response
        with patch.object(onboarding, 'provider', side_effect=checked_provider):
            response = self.call('POST', '/goals/api-read-outcomes/adopt',
                                 {'proposal_id': proposed['id'], 'proposal_sha256': proposed['proposal_sha256']})
        self.assertEqual(response.status_code, 409)
        with closing(self.loop.connect()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM loop_goals').fetchone()[0], 1)
            self.assertIsNone(c.execute('SELECT id FROM loop_actions WHERE id=?', (proposed['id'],)).fetchone())

    def test_native_logged_counts_preserve_unknown_and_small_samples(self):
        empty, _ = self.observe()
        self.assertEqual((empty['verdict'], empty['counts']['total'], empty['rate']), ('insufficient_evidence', 0, None))
        self.send()
        small, _ = self.observe()
        self.assertEqual((small['verdict'], small['counts']['success'], small['rate']), ('insufficient_evidence', 1, None))
        self.send(500)
        enough, window = self.observe()
        self.assertEqual(enough['counts'], {'total': 2, 'success': 1, 'failure': 1, 'unclassified': 0})
        self.assertEqual((enough['verdict'], enough['rate']), ('observed', 0.5))
        self.assertEqual(self.call('PUT', '/goals/api-read-outcomes/observations/' + enough['id'], window).json(), enough)
        self.send(True)
        unknown, _ = self.observe()
        self.assertEqual((unknown['counts']['unclassified'], unknown['rate'], unknown['verdict']), (1, None, 'coverage_gap'))

    def test_malformed_scope_labels_remain_coverage_gaps(self):
        self.send(release='a' * 40)
        self.send(release='a' * 40)
        self.send(environment='staging', release='b' * 40)
        for value in ['', '   ', True, 42, {}, []]:
            self.send(environment=value)
        result, _ = self.observe()
        self.assertEqual(result['counts']['total'], 2)
        self.assertEqual(result['coverage']['environment_missing'], 6)
        self.assertEqual((result['rate'], result['verdict']), (None, 'coverage_gap'))
        for value in ['   ', True, 42, {}, []]:
            self.send(release=value)
        result, _ = self.observe(release='a' * 40)
        self.assertEqual(result['counts']['total'], 2)
        self.assertEqual(result['coverage']['release_missing'], 5)
        self.assertEqual((result['rate'], result['verdict']), (None, 'coverage_gap'))

    def test_release_labels_and_environments_cannot_be_guessed(self):
        self.send(environment='staging', release='a' * 40)
        self.send()
        filtered, _ = self.observe(release='a' * 40)
        self.assertEqual((filtered['counts']['total'], filtered['coverage']['release_missing'], filtered['rate']), (0, 1, None))
        self.assertEqual(filtered['release_bound'], False)
        self.send(environment=None)
        unscoped, _ = self.observe()
        self.assertEqual(unscoped['coverage']['environment_missing'], 1)
        self.assertEqual(unscoped['verdict'], 'coverage_gap')

    def test_connection_and_goal_authority_stay_with_owner(self):
        editor = TestClient(app.app)
        key = self.key(self.owner, self.w, 'editor')
        editor.headers['Authorization'] = 'Bearer ' + key['key']
        proposed = self.call('PUT', '/proposals/' + str(uuid.uuid4()), self.definition, editor).json()
        denied = self.call('POST', '/goals/api-read-outcomes/adopt',
                           {'proposal_id': proposed['id'], 'proposal_sha256': proposed['proposal_sha256']}, editor)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.call('GET', '/sources', client=editor).status_code, 200)
        self.assertNotIn('private-provider-value', self.call('GET', '/sources').text)
        self.assertEqual(self.call('GET', '/scans/' + self.scan_id, client=TestClient(app.app)).status_code, 401)
        other = self.project(self.owner, self.w, 'other-app')
        self.assertEqual(self.call('GET', '/scans/' + self.scan_id, project=other['id']).status_code, 404)

    def test_native_answer_and_unscanned_source_paths_are_not_repository_goals(self):
        self.assertEqual(self.call('POST', '/goals/api-read-outcomes/connection', {}).status_code, 422)
        definition = copy.deepcopy(self.definition)
        definition['tracking']['dependencies'] = ['.github/workflows/ci.yml']
        self.assertEqual(self.call('PUT', '/proposals/' + str(uuid.uuid4()), definition).status_code, 422)
        definition = copy.deepcopy(self.definition)
        definition['measurement']['success_values'] = [200, 500]
        self.assertEqual(self.call('PUT', '/proposals/' + str(uuid.uuid4()), definition).status_code, 422)

    def test_prepare_retry_and_artifact_bound_policy(self):
        files = [{'path': 'src/analytics.ts', 'before_sha': 'c' * 40, 'content': 'release tracking\n'},
                 {'path': '.afterspan/goals/api-read-outcomes.json', 'before_sha': None, 'content': canonical(self.definition) + '\n'}]
        request = {'request_id': str(uuid.uuid4()), 'files': files}
        result = self.call('POST', '/goals/api-read-outcomes/prepare', request)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.call('POST', '/goals/api-read-outcomes/prepare', request).json(), result.json())
        request['files'][0]['content'] = 'different artifact'
        self.assertEqual(self.call('POST', '/goals/api-read-outcomes/prepare', request).status_code, 409)
        plan = result.json()['plan']
        policy = {'state': 'active', 'repository': 'owner/application', 'base': 'main', 'operations': ['draft-pr'],
                  'paths': [file['path'] for file in files], 'artifact_sha256': plan['artifact_sha256'],
                  'environment': 'production', 'required_checks': [], 'expires_at': stamp(time.time() + 600)}
        approved = self.call('PUT', '/goals/api-read-outcomes/policy', policy, query='&revision=0')
        self.assertEqual(approved.status_code, 200, approved.text)
        policy['artifact_sha256'] = 'a' * 64
        self.assertEqual(self.call('PUT', '/goals/api-read-outcomes/policy', policy, query='&revision=1').status_code, 422)
        policy['artifact_sha256'] = plan['artifact_sha256']
        policy['max_added_lines'] = 1
        self.assertEqual(self.call('PUT', '/goals/api-read-outcomes/policy', policy, query='&revision=1').status_code, 200)
        self.assertEqual(self.call('POST', '/actions/' + result.json()['id'] + '/draft-pr', {}).status_code, 403)

    def test_failed_scan_is_unknown_and_retries_the_same_job(self):
        id = str(uuid.uuid4())
        path = '/api/loop/scans/' + id + '?project=' + self.p['id']
        payload = {'connection_id': self.connection['id']}
        with patch.object(self.provider, 'scan', side_effect=HTTPException(503, 'provider unavailable')):
            queued = self.owner.put(path, headers=CSRF, json=payload)
        self.assertEqual((queued.status_code, queued.json()['status']), (202, 'pending'))
        failed = self.call('GET', '/jobs/' + id).json()
        self.assertEqual((failed['status'], failed['next_action']), ('failed', 'retry_same_request'))
        self.assertNotIn('private-provider-value', canonical(failed))
        retried = self.owner.put(path, headers=CSRF, json=payload)
        self.assertEqual(retried.status_code, 202)
        complete = self.call('GET', '/jobs/' + id).json()
        self.assertEqual((complete['status'], complete['result']['commit']), ('complete', 'a' * 40))
        other = self.project(self.owner, self.w, 'isolated-app')
        self.assertEqual(self.call('GET', '/jobs/' + id, project=other['id']).status_code, 404)

    def test_revoked_agent_cannot_start_its_queued_provider_read(self):
        editor = TestClient(app.app)
        key = self.key(self.owner, self.w, 'editor')
        editor.headers['Authorization'] = 'Bearer ' + key['key']
        captured = []
        id = str(uuid.uuid4())
        with patch('loop_onboarding.BackgroundTasks.add_task', side_effect=lambda function, *args, **kwargs: captured.append((function, args, kwargs))):
            queued = editor.put('/api/loop/scans/' + id + '?project=' + self.p['id'], headers=CSRF,
                                json={'connection_id': self.connection['id']})
        self.assertEqual(queued.status_code, 202)
        self.assertEqual(len(captured), 1)
        revoked = self.owner.delete('/api/workspaces/' + self.w + '/agent-keys/' + key['id'], headers=CSRF)
        self.assertEqual(revoked.status_code, 200)
        with patch.object(self.provider, 'scan') as provider_read:
            function, args, kwargs = captured[0]
            function(*args, **kwargs)
            self.assertEqual(provider_read.call_count, 0)
        self.assertEqual(self.call('GET', '/jobs/' + id).json()['status'], 'failed')

    def test_native_sync_cannot_replace_an_owner_adopted_repo_goal(self):
        import test_product_loop
        definition = copy.deepcopy(self.definition)
        definition['id'] = 'first-answer'
        proposal = self.call('PUT', '/proposals/' + str(uuid.uuid4()), definition).json()
        adopted = self.call('POST', '/goals/first-answer/adopt',
                            {'proposal_id': proposal['id'], 'proposal_sha256': proposal['proposal_sha256']})
        self.assertEqual(adopted.status_code, 200)
        provider = test_product_loop.FakeGitHub()
        source = {'repository': 'example/loop', 'base': 'main', 'commit': 'a' * 40,
                  'path': '.afterspan/goals/first-answer.json', 'content_sha256': 'b' * 64}
        with patch.object(self.loop, 'provider', return_value=provider):
            rejected = self.call('PUT', '/goals/first-answer/sync', source)
        self.assertEqual(rejected.status_code, 409)
        current = self.call('GET', '/goals/first-answer').json()['goal']
        self.assertEqual((current['goal_revision'], current['definition']['schema']), (1, 'afterspan-repo-goal-v2'))

    def test_source_guard_rejects_revoked_provider_snapshots(self):
        from loop_sources import posthog_observe, railway_release
        for kind, config in [('posthog', {'region': 'us', 'project_id': 42}),
                             ('railway', {'project_id': str(uuid.uuid4()), 'environment_id': str(uuid.uuid4()), 'service_id': str(uuid.uuid4())})]:
            connection = self.loop.onboarding.connections.put(self.p['id'], str(uuid.uuid4()), 0, kind, config, SecretStr('private-key'))
            snapshot = self.loop.onboarding.connections.resolve(self.p['id'], connection['id'], kind)
            guard = self.loop.onboarding.source_guard(self.p['id'], snapshot)
            self.loop.onboarding.connections.revoke(self.p['id'], connection['id'], 1)
            with patch('loop_sources._request') as network:
                with self.assertRaises(HTTPException) as error:
                    if kind == 'railway':
                        railway_release(snapshot, 'a' * 40, 'production', guard=guard)
                    else:
                        definition = copy.deepcopy(self.definition)
                        definition['mapping'] |= {'kind': 'posthog', 'connection_id': connection['id'], 'connection_revision': 1}
                        now = time.time()
                        posthog_observe(snapshot, definition, {'entry_start': stamp(now - 3600), 'entry_end': stamp(now), 'as_of': stamp(now)}, guard=guard)
                self.assertEqual(error.exception.status_code, 409)
                self.assertEqual(network.call_count, 0)

    def test_release_route_reads_exact_merged_artifact(self):
        files = [{'path': 'src/analytics.ts', 'before_sha': 'c' * 40, 'content': 'release tracking\n'},
                 {'path': '.afterspan/goals/api-read-outcomes.json', 'before_sha': None, 'content': canonical(self.definition) + '\n'}]
        action = self.call('POST', '/goals/api-read-outcomes/prepare', {'request_id': str(uuid.uuid4()), 'files': files}).json()
        receipt = {'status': 'merged', 'merged': True, 'merge_sha': 'e' * 40, 'artifact_sha256': action['plan']['artifact_sha256']}
        with closing(app.connect()) as c, c:
            c.execute('UPDATE loop_actions SET receipt=? WHERE project=? AND id=?', (canonical(receipt), self.p['id'], action['id']))
        connection = self.loop.onboarding.connections.put(self.p['id'], str(uuid.uuid4()), 0, 'railway',
            {'project_id': str(uuid.uuid4()), 'environment_id': str(uuid.uuid4()), 'service_id': str(uuid.uuid4())}, SecretStr('private-release-key'))
        proof = {'provider': 'railway', 'status': 'released', 'verified': True, 'commit': 'e' * 40,
                 'environment': 'production', 'deployment_id': str(uuid.uuid4()), 'released_at': stamp(time.time() - 300)}
        with patch('loop_sources.railway_release', return_value=proof):
            checked = self.call('POST', '/actions/' + action['id'] + '/release', {'connection_id': connection['id']})
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(checked.json()['receipt']['release']['commit'], 'e' * 40)
        self.assertEqual(self.call('GET', '/goals/api-read-outcomes').json()['stages']['release'], 'released')
        descendant = 'd' * 40
        binding = {'commit': descendant, 'merge_commit': 'e' * 40, 'scope': 'unchanged_artifact_and_declared_dependencies',
                   'files': [{'path': f['path']} for f in action['plan']['files']]}
        with patch.object(self.provider, 'artifact_release_binding', return_value=binding, create=True) as artifact:
            with patch('loop_sources.railway_release', return_value=proof | {'commit': descendant}) as railway:
                checked = self.call('POST', '/actions/' + action['id'] + '/release',
                                    {'connection_id': connection['id'], 'commit': descendant})
                self.assertEqual(checked.status_code, 200, checked.text)
                self.assertEqual(railway.call_args.args[1], descendant)
                self.assertEqual(artifact.call_args.args[1:], ('e' * 40, descendant))
                self.assertEqual(checked.json()['receipt']['release']['artifact_binding'], binding)
                view = self.call('GET', '/goals/api-read-outcomes').json()
                self.assertEqual(view['stages']['release'], 'released')
                evidence = {k: view['goal'][k] for k in ['goal_revision', 'measurement_revision', 'mapping_revision', 'goal_digest']}
                evidence['environment'] = 'production'
                for missing in ['artifact_binding', 'commit', 'environment']:
                    tampered = copy.deepcopy(checked.json())
                    tampered['receipt']['release'].pop(missing)
                    self.assertIsNone(self.loop.onboarding.verify_release(tampered, evidence))
                tampered = copy.deepcopy(checked.json())
                tampered['receipt']['release']['artifact_binding']['files'] = []
                self.assertIsNone(self.loop.onboarding.verify_release(tampered, evidence))
        with patch.object(self.provider, 'artifact_release_binding', side_effect=HTTPException(409, 'changed bytes'), create=True):
            with patch('loop_sources.railway_release') as railway:
                changed = self.call('POST', '/actions/' + action['id'] + '/release',
                                    {'connection_id': connection['id'], 'commit': descendant})
                self.assertEqual(changed.status_code, 409)
                railway.assert_not_called()
