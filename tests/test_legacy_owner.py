import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import uvicorn
from fastapi.testclient import TestClient

import app
import test_workspaces

CSRF=test_workspaces.CSRF


class LegacyOwnerTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_workspaces.WorkspaceTests();self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.owner,_=self.fixture.user('customer@example.test')
        self.workspace=self.fixture.workspace(self.owner)
        self.customer=self.fixture.project(self.owner,self.workspace)['id']
        self.env=patch.dict(os.environ,{'AFTERSPAN_LEGACY_LOGIN':'true'})
        self.env.start();self.addCleanup(self.env.stop)
        self.root=Path(self.fixture.fixture.temp.name)
        self.sock=socket.socket();self.sock.bind(('127.0.0.1',0))
        self.origin='http://127.0.0.1:'+str(self.sock.getsockname()[1])
        self.server=uvicorn.Server(uvicorn.Config(app.app,lifespan='off',access_log=False,log_level='error'))
        self.thread=threading.Thread(target=self.server.run,kwargs={'sockets':[self.sock]},daemon=True)
        self.thread.start();self.addCleanup(self.stop_server)
        deadline=time.monotonic()+5
        while not self.server.started and self.thread.is_alive() and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(self.server.started,'local test server did not start')

    def stop_server(self):
        self.server.should_exit=True;self.thread.join(timeout=5);self.sock.close()

    def private(self,name,value):
        path=self.root/name
        path.write_text(json.dumps(value));path.chmod(0o600)
        return str(path)

    def cli(self,*args,session=None):
        env={k:v for k,v in os.environ.items() if k not in ['AFTERSPAN_SESSION_FILE','AFTERSPAN_WORKSPACE_KEY','AFTERSPAN_READ_KEY','AFTERSPAN_WORKSPACE']}
        env['NATIVE_ANALYTICS_URL']=self.origin
        if session:env['AFTERSPAN_SESSION_FILE']=session
        return subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'cli/native_analytics.py'),*args],env=env,capture_output=True,text=True,timeout=10)

    def login(self):
        from test_analytics import PASSWORD
        password=self.private('password.json',{'password':PASSWORD})
        receipt=str(self.root/'existing-session.json')
        result=self.cli('login-existing','--file',password,'--output',receipt)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertNotIn(PASSWORD,result.stdout+result.stderr)
        self.assertEqual(Path(receipt).stat().st_mode&0o777,0o600)
        data=json.loads(Path(receipt).read_text())
        self.assertEqual(data['cookie_name'],'na_session')
        self.assertNotIn('user',data)
        self.assertNotIn(data['session'],result.stdout+result.stderr)
        client=TestClient(app.app);client.cookies.set('na_session',data['session'])
        self.assertEqual(client.get('/auth/me').status_code,401)
        self.assertEqual(client.get('/api/projects?workspace='+self.workspace).status_code,403)
        self.assertEqual(client.post('/api/workspaces',headers=CSRF,json={'name':'Denied'}).status_code,401)
        return receipt,data

    def test_existing_login_to_audit_uses_owner_session_despite_inherited_operator(self):
        receipt,data=self.login()
        result=self.cli('projects','--workspace','legacy',session=receipt)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(sorted(p['id'] for p in json.loads(result.stdout)['projects']),['alpha','beta'])
        created=self.cli('create-project','--workspace','legacy','--id','owner-app','--name','Owner app','--output',str(self.root/'producer.json'),session=receipt)
        self.assertEqual(created.returncode,0,created.stdout+created.stderr)
        self.assertEqual(json.loads((self.root/'producer.json').read_text())['workspace'],'legacy')
        denied=self.cli('connect-audit','--project',self.customer,'--output',str(self.root/'denied.json'),session=receipt)
        self.assertEqual(denied.returncode,1,denied.stdout+denied.stderr)
        self.assertEqual(json.loads(denied.stdout)['http_status'],403)
        audit=str(self.root/'audit.json')
        result=self.cli('connect-audit','--project','alpha','--output',audit,session=receipt)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        grant=json.loads(Path(audit).read_text())
        self.assertEqual((grant['project'],grant['purpose']),('alpha','audit'))
        self.assertEqual(Path(audit).stat().st_mode&0o777,0o600)
        self.assertNotIn('session',grant);self.assertNotIn(data['session'],Path(audit).read_text())
        headers={'Authorization':'Bearer '+grant['read_key']}
        self.assertEqual(self.owner.post('/api/audit/catalog?project=alpha',headers=headers,json={}).status_code,200)
        self.assertEqual(self.owner.post('/api/audit/catalog?project='+self.customer,headers=headers,json={}).status_code,403)
        self.assertEqual(self.owner.get('/api/events?project=alpha',headers=headers).status_code,401)
        with patch.dict(os.environ,{'AFTERSPAN_LEGACY_LOGIN':'false'}):
            rejected=self.cli('projects','--workspace','legacy',session=receipt)
            self.assertEqual(json.loads(rejected.stdout)['http_status'],401)
            self.assertEqual(self.owner.post('/api/audit/catalog?project=alpha',headers=headers,json={}).status_code,200)
        client=TestClient(app.app);client.cookies.set('na_session',data['session'])
        self.assertEqual(client.post('/auth/logout',headers=CSRF).status_code,200)
        self.assertEqual(self.owner.post('/api/audit/catalog?project=alpha',headers=headers,json={}).status_code,200)
        revoked=self.cli('revoke-read-key','--project','alpha','--id',grant['id'],session=receipt)
        self.assertEqual(json.loads(revoked.stdout),{'revoked':True})
        self.assertEqual(self.owner.post('/api/audit/catalog?project=alpha',headers=headers,json={}).status_code,401)

    def test_disabled_login_and_private_password_file_are_enforced(self):
        from test_analytics import PASSWORD
        password=self.private('password.json',{'password':PASSWORD})
        output=str(self.root/'session.json')
        with patch.dict(os.environ,{'AFTERSPAN_LEGACY_LOGIN':'false'}):
            self.assertFalse(self.fixture.client.get('/auth/config').json()['legacy_login'])
            result=self.cli('login-existing','--file',password,'--output',output)
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        self.assertEqual(json.loads(result.stdout)['http_status'],404)
        self.assertFalse(Path(output).exists())
        Path(password).chmod(0o644)
        rejected=self.cli('login-existing','--file',password,'--output',output)
        self.assertEqual(rejected.returncode,2)
        self.assertFalse(Path(output).exists())

    def test_account_receipt_default_and_bad_cookie_name(self):
        receipt=self.private('account.json',{'session':self.owner.cookies.get('as_session')})
        result=self.cli('projects',session=receipt)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual([p['id'] for p in json.loads(result.stdout)['projects']],[self.customer])
        invalid=self.private('invalid.json',{'session':'private-sentinel','cookie_name':'Authorization'})
        result=self.cli('projects',session=invalid)
        self.assertEqual(result.returncode,2)
        self.assertNotIn('private-sentinel',result.stdout+result.stderr)

    def test_complete_login_keeps_account_cookie_and_separate_write_key(self):
        email='email-owner@example.test'
        self.fixture.client.post('/auth/request-link',headers=CSRF,json={'email':email})
        token=self.private('token.json',{'token':self.fixture.token(email)})
        receipt=str(self.root/'account-session.json')
        result=self.cli('complete-login','--file',token,'--output',receipt)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(json.loads(Path(receipt).read_text())['cookie_name'],'as_session')
        event=self.private('events.json',{'events':[self.fixture.fixture.event('from-cli')]})
        with patch.dict(os.environ,{'NATIVE_ANALYTICS_WRITE_KEY':self.fixture.fixture.a['Authorization'][7:]}):
            result=self.cli('send','--file',event,session=receipt)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(json.loads(result.stdout),{'accepted':1,'duplicates':0})
