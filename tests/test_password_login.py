import time
import unittest
from contextlib import closing
from fastapi.testclient import TestClient
import app
import test_workspaces
from accounts import SESSION_SECONDS,digest

CSRF=test_workspaces.CSRF
PASSWORD='A sufficiently long test passphrase'

class PasswordTests(unittest.TestCase):
    setUp=test_workspaces.WorkspaceTests.setUp
    tearDown=test_workspaces.WorkspaceTests.tearDown
    token=test_workspaces.WorkspaceTests.token
    user=test_workspaces.WorkspaceTests.user

    def setup_password(self,client,**extra):
        return client.post('/auth/password',headers=CSRF,json={'username':'test.owner','password':PASSWORD,**extra})

    def test_verified_account_username_email_login_long_session_and_logout(self):
        self.assertEqual(self.setup_password(self.client).status_code,401)
        owner,uid=self.user('owner@example.test');original=owner.cookies.get('as_session')
        response=self.setup_password(owner);self.assertEqual(response.status_code,200,response.text)
        self.assertIn('Max-Age='+str(SESSION_SECONDS),response.headers['set-cookie'])
        self.assertNotIn(PASSWORD,response.text)
        other=TestClient(app.app);other.cookies.set('as_session',original)
        self.assertEqual(other.get('/auth/me').status_code,401)
        with closing(app.connect()) as c:
            row=c.execute('SELECT * FROM account_passwords').fetchone();self.assertNotEqual(row['password_hash'],PASSWORD)
            session=c.execute('SELECT * FROM account_sessions').fetchone();self.assertAlmostEqual(session['expires']-session['created'],180*86400)
        for username in ['TEST.OWNER','owner@example.test']:
            login=TestClient(app.app)
            result=login.post('/auth/password-login',headers=CSRF,json={'username':username,'password':PASSWORD})
            self.assertEqual(result.status_code,200,result.text);self.assertEqual(login.get('/auth/me').json()['user']['id'],uid)
            login.post('/auth/logout',headers=CSRF);self.assertEqual(login.get('/auth/me').status_code,401)

    def test_csrf_wrong_password_unknown_and_persistent_rate_limit(self):
        owner,_=self.user('owner@example.test');self.setup_password(owner)
        body={'username':'test.owner','password':'wrong'}
        self.assertEqual(self.client.post('/auth/password-login',json=body).status_code,403)
        self.assertEqual(self.client.post('/auth/password-login',headers={**CSRF,'Origin':'https://evil.example'},json=body).status_code,403)
        wrong=self.client.post('/auth/password-login',headers=CSRF,json=body)
        unknown=self.client.post('/auth/password-login',headers=CSRF,json={**body,'username':'unknown'})
        self.assertEqual(wrong.status_code,401);self.assertEqual(wrong.json(),unknown.json())
        for _ in range(9):self.assertEqual(self.client.post('/auth/password-login',headers=CSRF,json=body).status_code,401)
        self.assertEqual(self.client.post('/auth/password-login',headers=CSRF,json=body).status_code,429)

    def test_password_change_reauthentication_uniqueness_and_recovery(self):
        owner,uid=self.user('owner@example.test');self.assertEqual(self.setup_password(owner).status_code,200)
        second,_=self.user('second@example.test');self.assertEqual(self.setup_password(second).status_code,409)
        self.assertEqual(self.setup_password(owner,password='short').status_code,422)
        with closing(app.connect()) as c,c:c.execute('UPDATE account_sessions SET auth_time=? WHERE user_id=?',(time.time()-1000,uid))
        self.assertEqual(self.setup_password(owner).status_code,403)
        self.assertEqual(self.setup_password(owner,current_password='incorrect').status_code,403)
        self.assertEqual(self.setup_password(owner,current_password=PASSWORD,password=PASSWORD+' changed').status_code,200)
        old=self.client.post('/auth/password-login',headers=CSRF,json={'username':'test.owner','password':PASSWORD});self.assertEqual(old.status_code,401)
        recovered,_=self.user('owner@example.test');self.assertEqual(self.setup_password(recovered).status_code,200)

    def test_terminal_inherits_auth_time_and_expiry(self):
        owner,uid=self.user('owner@example.test')
        with closing(app.connect()) as c,c:c.execute('UPDATE account_sessions SET auth_time=? WHERE user_id=?',(time.time()-1000,uid))
        start=self.client.post('/auth/cli/start',headers=CSRF,json={}).json()
        owner.post('/auth/cli/approve',headers=CSRF,json={'code':start['user_code'],'approve':True})
        result=self.client.post('/auth/cli/poll',headers=CSRF,json={'token':start['device_code']}).json()
        self.client.cookies.set('as_session',result['session'])
        self.assertEqual(self.setup_password(self.client).status_code,403)
        self.assertGreater(result['expires'],time.time()+179*86400)
