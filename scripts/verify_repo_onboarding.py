"""Verify repository onboarding on an isolated local preview with synthetic events.

Requires a private local mail directory and a caller-provided GitHub read credential.
No provider writes run. Tracker preparation is an explicit, single-file pilot.
"""
import argparse
import base64
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
import hashlib
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]


def private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle, indent=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog='Use a fresh private state directory per run. A pass requires an artifact or the explicit Tracker pilot.')
    for name in ['origin', 'repository', 'commit']:
        parser.add_argument('--' + name, required=True)
    for name in ['state', 'mail-dir', 'github-credential-file', 'report']:
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--checkout', type=Path, default=ROOT)
    parser.add_argument('--event', default='http.request')
    parser.add_argument('--dependency', default='src/lib/analytics/request.ts')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--artifact-file', type=Path, help='Bounded JSON {files} of source changes; the canonical adopted goal is added')
    group.add_argument('--prepare-tracker-pilot', action='store_true', help='Add release_sha to the known Tracker telemetry line only')
    args = parser.parse_args()
    origin = urllib.parse.urlsplit(args.origin)
    if (origin.scheme != 'http' or origin.hostname != '127.0.0.1' or origin.username or origin.password
            or origin.path or origin.query or origin.fragment):
        parser.error('use an isolated http://127.0.0.1:PORT origin')
    if not re.fullmatch(r'[0-9a-f]{40}', args.commit):
        parser.error('--commit must be an immutable lowercase 40-character SHA')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repository) or any(p in {'.', '..'} for p in args.repository.split('/')):
        parser.error('--repository must name an owner and repository')
    if (not re.fullmatch(r'[A-Za-z0-9_.$:@/-]{1,160}', args.event)
            or not re.fullmatch(r'[A-Za-z0-9_.][A-Za-z0-9_./-]{0,159}', args.dependency)
            or '..' in args.dependency.split('/') or '//' in args.dependency):
        parser.error('use a bounded event name and safe relative dependency')
    if args.prepare_tracker_pilot and (args.event, args.dependency) != ('http.request', 'src/lib/analytics/request.ts'):
        parser.error('the Tracker pilot supports only its known event and source file')
    for path, directory in [(args.mail_dir, True), (args.github_credential_file, False)]:
        if path.is_symlink() or not (path.is_dir() if directory else path.is_file()) or path.stat().st_mode & 0o077:
            parser.error('mail and credential paths must exist and be private')
    try:
        credential = json.loads(args.github_credential_file.read_text())['credential']
        if not isinstance(credential, str) or not credential or len(credential) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in credential):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        parser.error('provide private JSON containing a valid credential string')
    if not (args.checkout / 'cli/native_analytics.py').is_file():
        parser.error('--checkout must contain the official CLI')
    if args.state.exists() and (args.state.is_symlink() or not args.state.is_dir() or args.state.stat().st_mode & 0o077 or any(args.state.iterdir())):
        parser.error('--state must be a new or empty private directory; use a fresh directory per run')
    if args.report.exists() or args.report.is_symlink():
        parser.error('--report must be a new file')
    if args.artifact_file and (not args.artifact_file.is_file() or args.artifact_file.stat().st_size > 256 * 1024):
        parser.error('--artifact-file must be a bounded local JSON file')
    args.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    report = {'ok': False, 'environment': 'isolated local preview', 'data': 'synthetic local fixture, not customer outcomes',
              'repository': args.repository, 'commit': args.commit, 'checks': [], 'limits': [
                  'No GitHub, Slack, GrowthBook or deployment writes run.',
                  'Logged request outcomes do not prove customer benefit or a released change.']}
    cookies = CookieJar()
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *unused):
            return None
    owner = urllib.request.build_opener(NoRedirect, urllib.request.HTTPCookieProcessor(cookies))
    isolated = urllib.request.build_opener(NoRedirect)

    def request(path, body=None, method=None, key=None, anonymous=False, expected=(200, 201, 202)):
        headers = {'Content-Type': 'application/json', 'X-Native-CSRF': '1'}
        if key:
            headers['Authorization'] = 'Bearer ' + key
        req = urllib.request.Request(args.origin + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers=headers, method=method)
        try:
            with (isolated if key or anonymous else owner).open(req, timeout=90) as response:
                code, value = response.status, json.load(response)
        except urllib.error.HTTPError as error:
            code, value = error.code, None
        if code not in expected:
            raise AssertionError('Local API ' + (method or ('POST' if body is not None else 'GET')) + ' ' + path.split('?')[0] + ' returned HTTP ' + str(code))
        return value

    env = {k: v for k, v in os.environ.items() if not k.startswith(('ANALYTICS_', 'AFTERSPAN_', 'NATIVE_ANALYTICS_', 'GH_', 'GITHUB_'))}
    env['NATIVE_ANALYTICS_URL'] = args.origin

    def cli(*commands, read=False):
        current = dict(env)
        current.update({'AFTERSPAN_READ_KEY': read_key} if read else {'AFTERSPAN_WORKSPACE_KEY': editor_key})
        result = subprocess.run([sys.executable, str(args.checkout / 'cli/native_analytics.py'), *commands],
                                env=current, capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise AssertionError('Official CLI rejected ' + commands[0])
        return json.loads(result.stdout)

    def queued(path, body, method, job_id):
        result = request(path, body, method, key=editor_key)
        deadline, notice = time.monotonic() + 600, 0
        while result.get('status') == 'pending':
            job = request('/api/loop/jobs/' + job_id + suffix, key=editor_key)
            if job['status'] in {'failed', 'interrupted'}:
                raise AssertionError('Provider job failed; retry the same request ID from private run state')
            if job['status'] == 'complete':
                result = request(path, body, method, key=editor_key)
                break
            if time.monotonic() > deadline:
                raise AssertionError('Provider job exceeded the ten-minute bound')
            if time.monotonic() >= notice:
                print('Waiting for bounded repository verification.', flush=True)
                notice = time.monotonic() + 15
            time.sleep(2)
        return result

    try:
        config = request('/auth/config')
        assert config['delivery_mode'] == 'local' and config['email_sign_in'], 'Local mail delivery is required'
        request('/openapi.json')
        email = 'repo-qa-' + uuid.uuid4().hex + '@example.test'
        request('/auth/request-link', {'email': email})
        token = None
        for _ in range(50):
            for path in args.mail_dir.glob('*.eml'):
                message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
                if str(message['To']) != email:
                    continue
                for part in message.walk():
                    if part.get_content_type() != 'text/plain':
                        continue
                    for link in re.findall(r'http://[^\s<>]+', part.get_content()):
                        parsed = urllib.parse.urlsplit(link)
                        if (parsed.scheme, parsed.netloc) == (origin.scheme, origin.netloc):
                            token = urllib.parse.parse_qs(parsed.fragment).get('sign-in', [None])[0]
            if token:
                break
            time.sleep(.2)
        assert token, 'Matching local QA sign-in mail was not found'
        request('/auth/consume-link', {'token': token})
        private(args.state / 'owner.json', {'session': next(c.value for c in cookies if c.name == 'as_session'), 'cookie_name': 'as_session'})
        workspace = request('/api/workspaces', {'name': 'Repository onboarding QA synthetic'})['id']
        project = request('/api/projects?workspace=' + workspace, {'id': 'repo-qa-' + uuid.uuid4().hex[:12], 'name': 'Repository onboarding synthetic'})
        pid = project['id']
        editor_key = request('/api/workspaces/' + workspace + '/agent-keys', {'label': 'Local onboarding QA', 'role': 'editor', 'expires_days': 1})['key']
        read_key = request('/api/projects/' + pid + '/read-keys', {'label': 'Local onboarding QA', 'expires_days': 1, 'purpose': 'analytics'})['read_key']
        private(args.state / 'editor.json', {'key': editor_key, 'workspace': workspace})
        private(args.state / 'producer.json', {'write_key': project['write_key']})
        private(args.state / 'read.json', {'read_key': read_key, 'project': pid})
        private(args.state / 'connection.json', {'origin': args.origin, 'workspace': workspace, 'project': pid, 'key': editor_key,
                                               'root': str(args.checkout.resolve()), 'repository': args.repository, 'base': 'main', 'commit': args.commit, 'goal': 'qa-api-read-outcomes'})
        suffix = '?project=' + pid
        connection_id, scan_id = str(uuid.uuid4()), str(uuid.uuid4())
        private(args.state / 'run.json', {'scan_id': scan_id, 'connection_id': connection_id})
        request('/api/loop/sources/' + connection_id + suffix + '&revision=0', {'kind': 'github', 'config': {'repository': args.repository}, 'credential': credential}, 'PUT')
        assert cli('loop-sources', '--project', pid) == request('/api/loop/sources' + suffix, key=editor_key)
        scan_body = {'connection_id': connection_id, 'commit': args.commit}
        scan = queued('/api/loop/scans/' + scan_id + suffix, scan_body, 'PUT', scan_id)
        assert scan['commit'] == args.commit and scan['repository'] == args.repository
        assert cli('loop-scan-status', '--project', pid, '--id', scan_id) == scan
        assert request('/api/loop/scans/' + scan_id + suffix, scan_body, 'PUT', key=editor_key) == scan
        citations = [c for c in scan['findings']['events'] if c['name'] == args.event and c['path'] == args.dependency and c['sink'] == 'native']
        assert citations, 'Selected event needs a native citation in the real scan'
        report.update(source_files=scan['files'], citations=citations, scan_coverage=scan['coverage'])
        goal = 'qa-api-read-outcomes'
        definition = {'schema': 'afterspan-repo-goal-v2', 'id': goal, 'question': 'Do logged GET requests return successful responses?', 'scan_id': scan_id,
                      'measurement': {'kind': 'event-outcome-v1', 'unit': 'logged_event', 'event': args.event, 'filters': [{'property': 'method', 'op': 'eq', 'value': 'GET'}],
                                      'outcome_property': 'status', 'success_values': [200], 'failure_values': [500], 'min_sample_size': 2},
                      'mapping': {'kind': 'native', 'environment': 'local', 'environment_property': 'environment', 'release_property': 'release_sha'},
                      'tracking': {'kind': 'reviewed-artifact-v1', 'dependencies': [args.dependency]}}
        proposal = request('/api/loop/proposals/' + str(uuid.uuid4()) + suffix, definition, 'PUT', key=editor_key)
        prefix = '/api/loop/goals/' + goal
        request(prefix + '/adopt' + suffix, {'proposal_id': proposal['id'], 'proposal_sha256': proposal['proposal_sha256']}, 'POST')
        assert cli('loop-goals', '--project', pid, read=True) == request('/api/loop/goals' + suffix, key=read_key)
        report['checks'].append('real scan, same-ID scan replay, source citations, owner adoption, CLI sources/scan/goals parity')
        goal_file = {'path': '.afterspan/goals/' + goal + '.json', 'before_sha': None,
                     'content': json.dumps(definition, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n'}
        private(args.state / 'goal.json', definition)
        files = json.loads(args.artifact_file.read_text())['files'] if args.artifact_file else None
        if files is not None:
            assert isinstance(files, list) and 1 <= len(files) <= 7, 'Provide one to seven source changes'
            assert all(isinstance(f, dict) and f.get('path') != goal_file['path'] for f in files), 'The canonical goal is generated from adoption'
            files = [*files, goal_file]
        if args.prepare_tracker_pilot:
            fetched = subprocess.run(['gh', 'api', 'repos/' + args.repository + '/contents/' + args.dependency + '?ref=' + args.commit],
                                     env={**env, 'GH_TOKEN': credential}, capture_output=True, text=True, timeout=90)
            assert fetched.returncode == 0, 'GitHub source read failed'
            blob = json.loads(fetched.stdout)
            source = base64.b64decode(blob['content'], validate=False).decode()
            ref = next(f for f in scan['files'] if f['path'] == args.dependency)
            assert blob['sha'] == ref['blob_sha'] and hashlib.sha256(source.encode()).hexdigest() == ref['content_sha256']
            needle = 'deployment_id:process.env.VERCEL_DEPLOYMENT_ID || process.env.RAILWAY_DEPLOYMENT_ID,'
            assert source.count(needle) == 1 and 'release_sha:' not in source, 'Tracker pilot source shape is unsupported'
            changed = source.replace(needle, needle + 'release_sha:process.env.VERCEL_GIT_COMMIT_SHA || process.env.RAILWAY_GIT_COMMIT_SHA,')
            files = [{'path': args.dependency, 'before_sha': blob['sha'], 'content': changed}, goal_file]
            report['artifact_kind'] = 'explicit Tracker single-file local pilot; no general source patching'
        if files:
            artifact = args.state / 'artifact.json'
            private(artifact, {'files': files})
            request_id = str(uuid.uuid4())
            private(args.state / 'prepare.json', {'request_id': request_id})
            action = queued(prefix + '/prepare' + suffix, {'request_id': request_id, 'files': files}, 'POST', request_id)
            assert action['plan']['status'] == 'prepared'
            private(args.state / 'action.json', {'id': action['id'], 'request_id': request_id})
            assert cli('loop-prepare', '--project', pid, '--goal', goal, '--request-id', request_id, '--file', str(artifact)) == action
            report['checks'].append('prepared exact reviewed bytes and replayed same request through official CLI')
        start = datetime.now(timezone.utc) - timedelta(minutes=5)
        stamp = (start + timedelta(minutes=1)).isoformat()
        rows = [{'id': str(uuid.uuid4()), 'name': args.event, 'timestamp': stamp, 'properties': {'method': 'GET', 'status': status, 'environment': 'local', 'fixture': 'synthetic'}} for status in [200, 500]]
        request('/v1/events', {'events': rows}, key=project['write_key'])
        request('/v1/events', {'events': rows}, key=project['write_key'])
        def observe():
            now = datetime.now(timezone.utc).isoformat()
            body = {'entry_start': start.isoformat(), 'entry_end': now, 'observed_through': now, 'as_of': now}
            id = str(uuid.uuid4())
            window = args.state / ('window-' + id + '.json')
            private(window, body)
            receipt = request(prefix + '/observations/' + id + suffix, body, 'PUT', key=editor_key)
            assert cli('loop-observe', '--project', pid, '--goal', goal, '--id', id, '--file', str(window), read=True) == receipt
            return receipt
        enough = observe()
        assert enough['counts'] == {'total': 2, 'success': 1, 'failure': 1, 'unclassified': 0} and enough['rate'] == .5 and enough['verdict'] == 'observed'
        request('/v1/events', {'events': [{'id': str(uuid.uuid4()), 'name': args.event, 'timestamp': stamp, 'properties': {'method': 'GET', 'status': True, 'environment': 'local', 'fixture': 'synthetic'}}]}, key=project['write_key'])
        unknown = observe()
        assert unknown['counts'] == {'total': 3, 'success': 1, 'failure': 1, 'unclassified': 1} and unknown['rate'] is None and unknown['verdict'] == 'coverage_gap'
        request('/api/loop/scans/' + scan_id + suffix, anonymous=True, expected=(401,))
        other = request('/api/projects?workspace=' + workspace, {'id': 'repo-denial-' + uuid.uuid4().hex[:10], 'name': 'Isolation QA synthetic'})
        request('/api/loop/scans/' + scan_id + '?project=' + other['id'], key=read_key, expected=(403,))
        view = request(prefix + suffix, key=read_key)
        assert cli('loop-status', '--project', pid, '--goal', goal, read=True) == view
        assert view['stages']['release'] == 'not_observed'
        report.update(counts=enough['counts'], unknown_counts=unknown['counts'], stages=view['stages'],
                      ui_url=args.origin + '/app#' + urllib.parse.urlencode({'workspace': workspace, 'project': pid, 'view': 'loop'}))
        report['checks'].append('literal fixture counts, event deduplication, same-ID observation replay, CLI status/observations parity, anonymous and wrong-project denial')
        assert files, 'Goal adopted, but full local proof requires --artifact-file or --prepare-tracker-pilot'
        report['ok'] = True
    except Exception as error:
        report['failure'] = (str(error) or 'Local acceptance assertion failed') if isinstance(error, AssertionError) else 'Local verification failed (' + type(error).__name__ + ')'
    args.report.parent.mkdir(parents=True, exist_ok=True)
    private(args.report, report)
    print(json.dumps({'ok': report['ok'], 'report': str(args.report), **({'failure': report['failure']} if 'failure' in report else {})}))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
