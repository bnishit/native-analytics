import contextlib
import io
import json
from pathlib import Path
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app
import test_workspaces
CSRF=test_workspaces.CSRF
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'cli'))
import browser_login
import vercel_setup


class CliLoginTests(unittest.TestCase):
    setUp=test_workspaces.WorkspaceTests.setUp
    tearDown=test_workspaces.WorkspaceTests.tearDown
    token=test_workspaces.WorkspaceTests.token
    user=test_workspaces.WorkspaceTests.user
    workspace=test_workspaces.WorkspaceTests.workspace
    project=test_workspaces.WorkspaceTests.project
    key=test_workspaces.WorkspaceTests.key
    def start(self):
        response=self.client.post('/auth/cli/start',headers=CSRF,json={})
        self.assertEqual(response.status_code,201,response.text)
        self.assertEqual(response.headers['cache-control'],'no-store')
        return response.json()

    def poll(self,grant):return self.client.post('/auth/cli/poll',headers=CSRF,json={'token':grant['device_code']})

    def approve(self,owner,grant,approve=True):
        return owner.post('/auth/cli/approve',headers=CSRF,json={'code':grant['user_code'],'approve':approve})

    def test_grant_requires_browser_then_creates_workspaces_and_projects(self):
        grant=self.start()
        self.assertEqual(self.approve(self.client,grant).status_code,401)
        owner,_=self.user('cli@example.test')
        self.assertEqual(self.approve(owner,grant).status_code,200)
        result=self.poll(grant);self.assertEqual(result.status_code,200,result.text)
        receipt=result.json();self.assertEqual(receipt['status'],'complete')
        self.client.cookies.set('as_session',receipt['session'])
        wid=self.workspace(self.client);project=self.project(self.client,wid)
        self.assertIn('write_key',project)
        self.assertEqual(self.poll(grant).status_code,400)
        self.assertEqual(self.approve(owner,grant).status_code,400)
        owner.post('/auth/logout-all',headers=CSRF,json={})
        self.assertEqual(self.client.get('/auth/me').status_code,401)

    def test_pending_throttle_denial_expiry_and_source_revocation(self):
        grant=self.start();self.assertEqual(self.poll(grant).json()['status'],'pending')
        self.assertEqual(self.poll(grant).status_code,429)
        owner,_=self.user('cli@example.test')
        denied=self.start();self.approve(owner,denied,False)
        self.assertEqual(self.poll(denied).json()['status'],'denied')
        revoked=self.start();self.approve(owner,revoked)
        owner.post('/auth/logout-all',headers=CSRF,json={})
        self.assertEqual(self.poll(revoked).json()['status'],'denied')
        with app.connect() as c:c.execute('UPDATE cli_authorizations SET expires=?',(time.time()-1,))
        self.assertEqual(self.poll(grant).status_code,400)

    def test_no_csrf_or_foreign_origin_and_workspace_key_cannot_approve(self):
        self.assertEqual(self.client.post('/auth/cli/start',json={}).status_code,403)
        self.assertEqual(self.client.post('/auth/cli/start',json={},headers={**CSRF,'Origin':'https://evil.example'}).status_code,403)
        grant=self.start();owner,_=self.user('cli@example.test');wid=self.workspace(owner)
        key=self.key(owner,wid,'owner')
        self.assertEqual(self.client.post('/auth/cli/approve',headers={**CSRF,'Authorization':'Bearer '+key['key']},json={'code':grant['user_code'],'approve':True}).status_code,403)

    def test_cli_poll_saves_private_receipt_without_printing_credentials(self):
        grant=self.start();owner,_=self.user('cli@example.test');self.approve(owner,grant)
        receipt=self.poll(grant).json();path=Path(self.fixture.temp.name)/'session.json'
        class Reply:
            def __init__(self,value):self.data=io.BytesIO(json.dumps(value).encode())
            def __enter__(self):return self.data
            def __exit__(self,*args):pass
        output=io.StringIO()
        with patch('browser_login.urllib.request.build_opener') as opener,patch('browser_login.time.sleep'),contextlib.redirect_stdout(output):
            opener.return_value.open.side_effect=[Reply(grant),Reply(receipt)]
            self.assertEqual(browser_login.login('http://127.0.0.1:8769',path,False),0)
        self.assertNotIn(grant['device_code'],output.getvalue());self.assertNotIn(receipt['session'],output.getvalue())
        self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(json.loads(path.read_text())['session'],receipt['session'])

    def test_vercel_transfer_pins_destination_and_keeps_key_off_arguments(self):
        root=Path(self.fixture.temp.name);(root/'.vercel').mkdir()
        (root/'.vercel/project.json').write_text(json.dumps({'orgId':'team_test','projectId':'prj_test'}))
        args=SimpleNamespace(cwd=str(root),vercel_project='prj_test',vercel_team='team_test',environment='preview')
        receipt={'write_key':'na_write_synthetic'}
        with patch('vercel_setup.subprocess.run') as run:
            run.return_value.returncode=0
            result=vercel_setup.install(args,receipt)
            self.assertTrue(result['ok']);self.assertNotIn(receipt['write_key'],json.dumps(result))
            self.assertNotIn(receipt['write_key'],str(run.call_args.args))
            self.assertNotIn('--yes',run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs['input'],receipt['write_key'])
            args.vercel_project='prj_other'
            with self.assertRaises(ValueError):vercel_setup.install(args,receipt)
            self.assertEqual(run.call_count,1)

    def test_unknown_device_and_public_code_cannot_collect_session(self):
        grant=self.start();owner,_=self.user('cli@example.test');self.approve(owner,grant)
        self.assertEqual(self.poll({'device_code':'as_device_'+'x'*40}).status_code,400)
        self.assertEqual(self.poll({'device_code':'as_device_'+grant['user_code']}).status_code,400)
        self.assertEqual(self.poll(grant).json()['status'],'complete')

    def test_failed_cli_login_removes_reserved_file(self):
        path=Path(self.fixture.temp.name)/'failed-session.json'
        with patch('browser_login.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=OSError('offline')
            with self.assertRaises(OSError):browser_login.login('http://127.0.0.1:8769',path,False)
        self.assertFalse(path.exists())

    def test_cli_session_is_bound_to_its_service(self):
        import runpy
        cli=runpy.run_path(str(Path(__file__).resolve().parents[1]/'cli/native_analytics.py'))
        path=Path(self.fixture.temp.name)/'bound-session.json'
        path.write_text(json.dumps({'session':'as_session_synthetic','origin':'https://original.example'}));path.chmod(0o600)
        with patch.dict('os.environ',{'NATIVE_ANALYTICS_URL':'https://different.example','AFTERSPAN_SESSION_FILE':str(path)}):
            with self.assertRaisesRegex(ValueError,'another service'):cli['main'](['me'])

    def test_vercel_failure_never_exposes_provider_output(self):
        import subprocess
        root=Path(self.fixture.temp.name);(root/'.vercel').mkdir()
        (root/'.vercel/project.json').write_text(json.dumps({'orgId':'team_test','projectId':'prj_test'}))
        args=SimpleNamespace(cwd=str(root),vercel_project='prj_test',vercel_team='team_test',environment='preview')
        with patch('vercel_setup.subprocess.run') as run:
            run.return_value=SimpleNamespace(returncode=1,stdout='na_write_synthetic',stderr='na_write_synthetic')
            result=vercel_setup.install(args,{'write_key':'na_write_synthetic'})
            self.assertFalse(result['ok']);self.assertNotIn('na_write_synthetic',json.dumps(result))
            run.side_effect=subprocess.TimeoutExpired('vercel',60)
            self.assertEqual(vercel_setup.install(args,{'write_key':'na_write_synthetic'})['status'],'unknown')

    def test_invalid_approval_attempts_are_rate_limited(self):
        owner,_=self.user('cli@example.test')
        for _ in range(19):
            self.assertEqual(self.approve(owner,{'user_code':'000000000000'}).status_code,400)
        self.assertEqual(self.approve(owner,{'user_code':'000000000000'}).status_code,429)

    def test_poll_recovers_from_throttle_and_service_outage(self):
        import urllib.error
        path=Path(self.fixture.temp.name)/'retry-session.json'
        grant={'verification_uri':'https://example.test/assets/cli-login.html','user_code':'123456ABCDEF','device_code':'as_device_synthetic','expires_in':600,'interval':3}
        receipt={'status':'complete','session':'as_session_synthetic','cookie_name':'as_session','expires':time.time()+100}
        output=io.StringIO()
        with patch('browser_login.urllib.request.build_opener') as opener,patch('browser_login.time.sleep') as sleep,contextlib.redirect_stdout(output):
            opener.return_value.open.side_effect=[io.BytesIO(json.dumps(grant).encode()),
                urllib.error.HTTPError('https://example.test',429,'limited',{'Retry-After':'8'},None),
                urllib.error.HTTPError('https://example.test',503,'unavailable',{},None),
                io.BytesIO(json.dumps(receipt).encode())]
            self.assertEqual(browser_login.login('https://example.test',path,False),0)
        self.assertEqual([call.args[0] for call in sleep.call_args_list],[3,8,3])
        self.assertEqual(json.loads(path.read_text())['session'],receipt['session'])
        self.assertNotIn(receipt['session'],output.getvalue());self.assertNotIn(grant['device_code'],output.getvalue())

    def test_lost_completion_response_never_claims_success(self):
        import urllib.error
        path=Path(self.fixture.temp.name)/'lost-session.json'
        grant={'verification_uri':'https://example.test/assets/cli-login.html','user_code':'123456ABCDEF','device_code':'as_device_synthetic','expires_in':600,'interval':3}
        output=io.StringIO()
        with patch('browser_login.urllib.request.build_opener') as opener,patch('browser_login.time.sleep'),contextlib.redirect_stdout(output):
            opener.return_value.open.side_effect=[io.BytesIO(json.dumps(grant).encode()),
                urllib.error.URLError('lost response'),urllib.error.HTTPError('https://example.test',400,'already collected',{},None)]
            self.assertEqual(browser_login.login('https://example.test',path,False),1)
        self.assertFalse(path.exists())
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1])['result'],'unknown')
