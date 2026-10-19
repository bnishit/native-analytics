import hashlib
import json
import os
import re
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit

from fastapi.testclient import TestClient
import app
import test_analytics

CSRF={'X-Native-CSRF':'1'}

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_analytics.AnalyticsTests();self.fixture.setUp()
        self.env=patch.dict(os.environ,{'ANALYTICS_PUBLIC_URL':'http://127.0.0.1:8769','AFTERSPAN_MAIL_MODE':'local','AFTERSPAN_MAIL_DIR':str(Path(self.fixture.temp.name)/'mail'),'AFTERSPAN_LEGACY_LOGIN':'false'})
        self.env.start();self.client=TestClient(app.app)
    def tearDown(self):self.env.stop();self.fixture.tearDown()
    def token(self,email,kind='sign-in'):
        with closing(app.connect()) as c:rows=c.execute('SELECT body FROM account_mail WHERE recipient=? AND body IS NOT NULL ORDER BY created DESC',(email,)).fetchall()
        link=next(re.search(r'http://[^\s]+',row['body']).group() for row in rows if kind+'=' in row['body'])
        return parse_qs(urlsplit(link).fragment)[kind][0]
    def user(self,email):
        client=TestClient(app.app)
        response=client.post('/auth/request-link',headers=CSRF,json={'email':email});self.assertEqual(response.status_code,202,response.text)
        token=self.token(email);response=client.post('/auth/consume-link',headers=CSRF,json={'token':token});self.assertEqual(response.status_code,200,response.text)
        return client,response.json()['user']['id']
    def workspace(self,client,name='My apps'):
        r=client.post('/api/workspaces',headers=CSRF,json={'name':name});self.assertEqual(r.status_code,201,r.text);return r.json()['id']
    def project(self,client,workspace,slug='my-app'):
        r=client.post('/api/projects?workspace='+workspace,headers=CSRF,json={'id':slug,'name':'My app'});self.assertEqual(r.status_code,200,r.text);return r.json()
    def invite(self,client,workspace,email,role='reader'):
        r=client.post('/api/workspaces/'+workspace+'/invites',headers=CSRF,json={'email':email,'role':role});self.assertEqual(r.status_code,202,r.text);return r.json(),self.token(email,'invite')
    def join(self,owner,workspace,email,role='reader'):
        invite,token=self.invite(owner,workspace,email,role);member,user=self.user(email)
        r=member.post('/api/invites/accept',headers=CSRF,json={'token':token});self.assertEqual(r.status_code,200,r.text)
        return member,user
    def key(self,client,workspace,role='reader'):
        r=client.post('/api/workspaces/'+workspace+'/agent-keys',headers=CSRF,json={'label':'Codex','role':role});self.assertEqual(r.status_code,201,r.text);return r.json()

    def test_verified_link_required_single_use_expiry_and_revocable_session(self):
        r=self.client.post('/auth/request-link',headers=CSRF,json={'email':' Alice@Example.Test '});self.assertEqual(r.status_code,202)
        self.assertNotIn('token',r.text)
        with closing(app.connect()) as c:self.assertEqual(c.execute('SELECT count(*) FROM accounts').fetchone()[0],0)
        token=self.token('alice@example.test')
        preview=self.client.post('/auth/link-preview',headers=CSRF,json={'token':token});self.assertEqual(preview.json()['email'],'alice@example.test')
        self.assertEqual(self.client.get('/auth/consume-link?token='+token).status_code,405)
        self.assertEqual(self.client.post('/auth/consume-link',headers=CSRF,json={'token':token}).status_code,200)
        self.assertEqual(self.client.post('/auth/consume-link',headers=CSRF,json={'token':token}).status_code,400)
        session=self.client.cookies.get('as_session');other=TestClient(app.app);other.cookies.set('as_session',session)
        self.assertEqual(other.get('/auth/me').status_code,200)
        self.assertEqual(self.client.post('/auth/logout',headers=CSRF).status_code,200)
        self.assertEqual(other.get('/auth/me').status_code,401)
        self.client.post('/auth/request-link',headers=CSRF,json={'email':'expired@example.test'});token=self.token('expired@example.test')
        with closing(app.connect()) as c,c:c.execute('UPDATE sign_in_links SET expires=?',(time.time()-1,))
        self.assertEqual(self.client.post('/auth/consume-link',headers=CSRF,json={'token':token}).status_code,400)

    def test_email_limits_csrf_mail_configuration_and_no_shared_customer_login(self):
        self.assertEqual(self.client.post('/auth/login',json={'password':test_analytics.PASSWORD}).status_code,404)
        self.assertEqual(self.client.post('/auth/request-link',json={'email':'a@example.test'}).status_code,403)
        self.assertEqual(self.client.post('/auth/request-link',headers={**CSRF,'Origin':'https://elsewhere.test'},json={'email':'a@example.test'}).status_code,403)
        with patch.dict(os.environ,{'AFTERSPAN_MAIL_MODE':'disabled'}):self.assertEqual(self.client.post('/auth/request-link',headers=CSRF,json={'email':'a@example.test'}).status_code,503)
        with patch.dict(os.environ,{'ANALYTICS_PUBLIC_URL':'https://afterspan.example.test'}):self.assertEqual(self.client.post('/auth/request-link',headers=CSRF,json={'email':'a@example.test'}).status_code,503)
        for _ in range(3):self.assertEqual(self.client.post('/auth/request-link',headers=CSRF,json={'email':'a@example.test'}).status_code,202)
        self.assertEqual(self.client.post('/auth/request-link',headers=CSRF,json={'email':'a@example.test'}).status_code,429)

    def test_workspace_isolation_in_all_project_routes_and_same_project_slug(self):
        a,_=self.user('a@example.test');b,_=self.user('b@example.test');wa=self.workspace(a);wb=self.workspace(b)
        pa=self.project(a,wa);pb=self.project(b,wb);self.assertNotEqual(pa['id'],pb['id'])
        self.assertEqual(self.client.get('/api/projects').status_code,401)
        self.assertEqual([p['id'] for p in a.get('/api/projects').json()['projects']],[pa['id']])
        self.assertEqual(a.get('/api/projects?workspace='+wb).status_code,403)
        p=pb['id']
        for route in ['overview','events','people','charts','setup']:
            self.assertEqual(a.get('/api/'+route+'?project='+p).status_code,403,route)
        for route,body in [('query',{}),('journeys',{}),('journeys/list',{}),('setup-checks',{}),('charts',{'title':'No','query':{}})]:
            self.assertEqual(a.post('/api/'+route+'?project='+p,headers=CSRF,json=body).status_code,403,route)
        for route,body in [('rotate-key',{}),('read-keys',{'label':'No'}),('relay',{'enabled':True,'events':['app.opened']})]:
            self.assertEqual(a.post('/api/projects/'+p+'/'+route,headers=CSRF,json=body).status_code,403)
        self.assertEqual(a.patch('/api/people?project='+p,headers=CSRF,json={'distinct_id':'x','attributes':{}}).status_code,403)
        self.assertEqual(a.delete('/api/charts/unknown?project='+p,headers=CSRF).status_code,403)
        self.assertEqual(a.delete('/api/projects/'+p+'/read-keys/unknown',headers=CSRF).status_code,403)
        self.assertEqual(a.post('/api/projects?workspace='+wb,headers=CSRF,json={'id':'other','name':'No'}).status_code,403)
        self.assertEqual(a.get('/api/overview?project=alpha').status_code,403)
        self.assertEqual(a.post('/api/maintenance',headers=CSRF).status_code,401)

    def test_invites_are_email_bound_single_use_revocable_and_expiring(self):
        owner,_=self.user('owner@example.test');w=self.workspace(owner)
        receipt,token=self.invite(owner,w,'member@example.test','editor')
        self.assertNotIn(token,json.dumps(receipt))
        wrong,_=self.user('wrong@example.test')
        self.assertEqual(wrong.post('/api/invites/preview',headers=CSRF,json={'token':token}).status_code,400)
        member,_=self.user('member@example.test')
        self.assertEqual(member.get('/api/workspaces').json()['workspaces'],[])
        self.assertEqual(member.post('/api/invites/preview',headers=CSRF,json={'token':token}).json()['role'],'editor')
        self.assertEqual(member.post('/api/invites/accept',headers=CSRF,json={'token':token}).status_code,200)
        self.assertEqual(member.post('/api/invites/accept',headers=CSRF,json={'token':token}).status_code,400)
        for email,expired in [('revoked@example.test',False),('expired@example.test',True)]:
            receipt,token=self.invite(owner,w,email)
            if expired:
                with closing(app.connect()) as c,c:c.execute('UPDATE workspace_invites SET expires=? WHERE id=?',(time.time()-1,receipt['id']))
            else:self.assertEqual(owner.delete('/api/workspaces/'+w+'/invites/'+receipt['id'],headers=CSRF).status_code,200)
            person,_=self.user(email);self.assertEqual(person.post('/api/invites/accept',headers=CSRF,json={'token':token}).status_code,400)

    def test_editor_reader_permissions_and_no_self_promotion(self):
        owner,_=self.user('owner@example.test');w=self.workspace(owner);p=self.project(owner,w)['id']
        editor,eid=self.join(owner,w,'editor@example.test','editor');reader,rid=self.join(owner,w,'reader@example.test')
        for client in [reader,editor]:
            self.assertEqual(client.get('/api/overview?project='+p).status_code,200)
            self.assertEqual(client.post('/api/journeys/list?project='+p,headers=CSRF,json={}).status_code,200)
            self.assertEqual(client.post('/api/workspaces/'+w+'/invites',headers=CSRF,json={'email':'no@example.test'}).status_code,403)
            self.assertEqual(client.patch('/api/workspaces/'+w+'/members/'+rid,headers=CSRF,json={'role':'owner'}).status_code,403)
            self.assertEqual(client.post('/api/projects/'+p+'/read-keys',headers=CSRF,json={'label':'No'}).status_code,403)
        self.assertEqual(reader.post('/api/charts?project='+p,headers=CSRF,json={'title':'No','query':{}}).status_code,403)
        self.assertEqual(reader.post('/api/setup-checks?project='+p,headers=CSRF,json={}).status_code,403)
        self.assertEqual(editor.post('/api/charts?project='+p,headers=CSRF,json={'title':'Yes','query':{}}).status_code,200)
        self.assertEqual(editor.post('/api/setup-checks?project='+p,headers=CSRF,json={}).status_code,200)
        self.project(editor,w,'editor-app')

    def test_last_owner_guard_and_removed_members_lose_access_on_next_request(self):
        owner,oid=self.user('owner@example.test');w=self.workspace(owner);p=self.project(owner,w)['id']
        url='/api/workspaces/'+w+'/members/'+oid
        self.assertEqual(owner.patch(url,headers=CSRF,json={'role':'reader'}).status_code,409)
        self.assertEqual(owner.delete(url,headers=CSRF).status_code,409)
        second,sid=self.join(owner,w,'second@example.test','owner')
        key=self.key(owner,w,'owner');headers={'Authorization':'Bearer '+key['key']}
        invite,token=self.invite(owner,w,'late@example.test','owner')
        self.assertEqual(second.delete(url,headers=CSRF).status_code,200)
        self.assertEqual(owner.get('/api/overview?project='+p).status_code,403)
        self.assertEqual(self.client.get('/api/overview?project='+p,headers=headers).status_code,401)
        late,_=self.user('late@example.test');self.assertEqual(late.post('/api/invites/accept',headers=CSRF,json={'token':token}).status_code,400)
        invite,_=self.invite(second,w,'owner@example.test','reader');token=self.token('owner@example.test','invite')
        self.assertEqual(owner.post('/api/invites/accept',headers=CSRF,json={'token':token}).status_code,200)
        self.assertEqual(self.client.get('/api/overview?project='+p,headers=headers).status_code,401)

    def test_workspace_key_scope_role_ceiling_expiry_and_revocation(self):
        owner,oid=self.user('owner@example.test');w=self.workspace(owner);other=self.workspace(owner,'Other');p=self.project(owner,w)['id'];outside=self.project(owner,other)['id']
        second,sid=self.join(owner,w,'second@example.test','owner')
        key=self.key(owner,w,'owner');headers={'Authorization':'Bearer '+key['key']}
        self.assertEqual(self.client.get('/api/projects',headers=headers).json()['projects'][0]['id'],p)
        self.assertEqual(self.client.get('/api/overview?project='+outside,headers=headers).status_code,403)
        self.assertEqual(self.client.post('/api/workspaces',headers=headers,json={'name':'No'}).status_code,403)
        self.assertEqual(self.client.post('/api/workspaces/'+w+'/agent-keys',headers=headers,json={'label':'No'}).status_code,403)
        self.assertEqual(self.client.post('/api/projects?workspace='+w,headers=headers,json={'id':'agent-app','name':'Agent app'}).status_code,200)
        self.assertEqual(second.patch('/api/workspaces/'+w+'/members/'+oid,headers=CSRF,json={'role':'reader'}).status_code,200)
        self.assertEqual(self.client.post('/api/setup-checks?project='+p,headers=headers,json={}).status_code,403)
        self.assertEqual(self.client.get('/api/overview?project='+p,headers=headers).status_code,200)
        self.assertEqual(second.delete('/api/workspaces/'+w+'/agent-keys/'+key['id'],headers=CSRF).status_code,200)
        self.assertEqual(self.client.get('/api/overview?project='+p,headers=headers).status_code,401)
        key=self.key(second,w);headers={'Authorization':'Bearer '+key['key']}
        with closing(app.connect()) as c,c:c.execute('UPDATE workspace_keys SET expires=? WHERE id=?',(time.time()-1,key['id']))
        self.assertEqual(self.client.get('/api/overview?project='+p,headers=headers).status_code,401)

    def test_hashes_hidden_audit_scope_and_all_session_revocation(self):
        owner,oid=self.user('owner@example.test');w=self.workspace(owner);key=self.key(owner,w,'editor')
        with closing(app.connect()) as c:
            row=c.execute('SELECT key_hash FROM workspace_keys WHERE id=?',(key['id'],)).fetchone()
            self.assertEqual(row['key_hash'],hashlib.sha256(key['key'].encode()).hexdigest())
        listing=owner.get('/api/workspaces/'+w+'/agent-keys');self.assertNotIn(key['key'],listing.text)
        entries=owner.get('/api/workspaces/'+w+'/audit').json()['entries'];self.assertIn('agent_key.created',[r['action'] for r in entries])
        other,_=self.user('other@example.test');self.assertEqual(other.get('/api/workspaces/'+w+'/audit').status_code,403)
        self.assertEqual(owner.post('/auth/logout-all',headers=CSRF).status_code,200)
        self.assertEqual(owner.get('/auth/me').status_code,401)
        self.assertEqual(self.client.get('/auth/me',headers={'Authorization':'Bearer '+key['key']}).status_code,401)

    def test_local_mail_delivery_receipts_do_not_expose_links_and_failures_retry(self):
        owner,_=self.user('owner@example.test');w=self.workspace(owner);receipt,token=self.invite(owner,w,'member@example.test')
        app.accounts.deliver_pending()
        paths=list(Path(os.environ['AFTERSPAN_MAIL_DIR']).glob('*.eml'));self.assertEqual(len(paths),2)
        self.assertTrue(all(p.stat().st_mode&0o777==0o600 for p in paths))
        with closing(app.connect()) as c:self.assertEqual(c.execute('SELECT count(*) FROM account_mail WHERE body IS NOT NULL').fetchone()[0],0)
        status=owner.get('/api/workspaces/'+w+'/mail').json();self.assertEqual(status['messages'][0]['state'],'sent');self.assertNotIn(token,json.dumps(status))
        self.invite(owner,w,'another@example.test')
        with patch('accounts.Path.mkdir',side_effect=OSError('disk unavailable')):app.accounts.deliver_pending()
        with closing(app.connect()) as c:row=c.execute("SELECT state,attempts,body FROM account_mail WHERE recipient='another@example.test'").fetchone()
        self.assertEqual(row['state'],'queued');self.assertEqual(row['attempts'],1);self.assertIsNotNone(row['body'])

    def test_existing_projects_migrate_without_becoming_first_signup_property(self):
        with closing(app.connect()) as c:
            old=c.execute("SELECT workspace,slug FROM projects WHERE id='alpha'").fetchone();self.assertEqual(tuple(old),('legacy','alpha'))
        user,_=self.user('first@example.test');self.assertEqual(user.get('/api/projects').json()['projects'],[])
        app.init_db()
        self.assertEqual(self.fixture.query()['total'],0)

    def test_resending_replaces_old_link_and_mail_is_workspace_scoped(self):
        owner,_=self.user('owner@example.test');w=self.workspace(owner);other=self.workspace(owner,'Other')
        receipt,old=self.invite(owner,w,'member@example.test')
        response=owner.post('/api/workspaces/'+w+'/invites/'+receipt['id']+'/resend',headers=CSRF,json={})
        self.assertEqual(response.status_code,202,response.text);new=self.token('member@example.test','invite');self.assertNotEqual(old,new)
        person,_=self.user('member@example.test')
        self.assertEqual(person.post('/api/invites/accept',headers=CSRF,json={'token':old}).status_code,400)
        self.assertEqual(person.post('/api/invites/accept',headers=CSRF,json={'token':new}).status_code,200)
        self.assertEqual(owner.get('/api/workspaces/'+other+'/mail').json()['messages'],[])

    def test_existing_owner_assignment_requires_operator_and_is_one_time(self):
        owner,oid=self.user('owner@example.test')
        body={'account_id':oid}
        self.assertEqual(owner.post('/api/operator/assign-existing',headers=CSRF,json=body).status_code,401)
        result=self.client.post('/api/operator/assign-existing',headers=self.fixture.admin,json=body)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(owner.get('/api/projects?workspace=legacy').status_code,200)
        self.assertEqual(self.client.post('/api/operator/assign-existing',headers=self.fixture.admin,json=body).status_code,409)

    def test_two_owner_demotions_cannot_remove_all_owners(self):
        from concurrent.futures import ThreadPoolExecutor
        owner,oid=self.user('owner@example.test');w=self.workspace(owner)
        second,sid=self.join(owner,w,'second@example.test','owner')
        def demote(args):
            client,target=args
            return client.patch('/api/workspaces/'+w+'/members/'+target,headers=CSRF,json={'role':'reader'}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(demote,[(owner,oid),(second,sid)]))
        self.assertEqual(sorted(results),[200,409])
        self.assertEqual(sum(m['role']=='owner' for m in owner.get('/api/workspaces/'+w+'/members').json()['members']),1)

    def test_agentmail_scrubs_only_after_a_provider_receipt(self):
        self.client.post('/auth/request-link',headers=CSRF,json={'email':'api@example.test'})
        settings={'AFTERSPAN_MAIL_MODE':'agentmail','AFTERSPAN_AGENTMAIL_API_KEY':'private-test-key','AFTERSPAN_MAIL_FROM':'afterspan@agentmail.to'}
        with patch.dict(os.environ,settings),patch('accounts.http.client.HTTPSConnection') as https:
            response=https.return_value.getresponse.return_value;response.status=200;response.read.return_value=b'{"message_id":"accepted"}'
            app.accounts.deliver_pending()
            https.assert_called_once_with('api.agentmail.to',timeout=10)
            call=https.return_value.request.call_args
            self.assertEqual(call.args,('POST','/v0/inboxes/afterspan%40agentmail.to/messages/send'))
            payload=json.loads(call.kwargs['body']);self.assertEqual(payload['to'],['api@example.test'])
            self.assertIn('#sign-in=',payload['text'])
            self.assertEqual(call.kwargs['headers']['Authorization'],'Bearer private-test-key')
            self.assertNotIn('api_key',payload)
            https.return_value.close.assert_called_once()
        with closing(app.connect()) as c:row=c.execute("SELECT state,body FROM account_mail WHERE recipient='api@example.test'").fetchone()
        self.assertEqual(row['state'],'sent');self.assertIsNone(row['body'])

    def test_agentmail_uncertain_send_retries_the_same_provider_operation(self):
        self.client.post('/auth/request-link',headers=CSRF,json={'email':'retry-api@example.test'})
        settings={'AFTERSPAN_MAIL_MODE':'agentmail','AFTERSPAN_AGENTMAIL_API_KEY':'private-test-key','AFTERSPAN_MAIL_FROM':'afterspan@agentmail.to'}
        with patch.dict(os.environ,settings),patch('accounts.http.client.HTTPSConnection') as https:
            response=https.return_value.getresponse.return_value;response.status=200
            response.read.side_effect=[TimeoutError(),b'{"message_id":"accepted"}']
            app.accounts.deliver_pending()
            with closing(app.connect()) as c,c:
                row=c.execute("SELECT * FROM account_mail WHERE recipient='retry-api@example.test'").fetchone()
                self.assertEqual(row['attempts'],1);self.assertEqual(row['state'],'queued');self.assertIsNotNone(row['body'])
                c.execute('UPDATE account_mail SET next_attempt=0 WHERE id=?',(row['id'],))
            app.accounts.deliver_pending()
            first,second=https.return_value.request.call_args_list
            self.assertEqual(first,second)
            self.assertEqual(first.kwargs['headers']['Idempotency-Key'],'afterspan-mail-'+row['id'])
        with closing(app.connect()) as c:row=c.execute('SELECT state,body FROM account_mail WHERE id=?',(row['id'],)).fetchone()
        self.assertEqual(row['state'],'sent');self.assertIsNone(row['body'])

    def test_agentmail_rejection_and_invalid_receipts_keep_recoverable_mail(self):
        cases=[(302,b'{}'),(401,b'{}'),(429,b'{}'),(500,b'{}'),(200,b'not json'),(200,b'{}'),(200,b'{"message_id":false}'),(200,b'x'*8193)]
        for index,(status,raw) in enumerate(cases):
            with self.subTest(status=status,case=index):
                email=f'rejected-{index}@example.test'
                self.client.post('/auth/request-link',headers=CSRF,json={'email':email})
                with patch.dict(os.environ,{'AFTERSPAN_MAIL_MODE':'agentmail','AFTERSPAN_AGENTMAIL_API_KEY':'private-test-key','AFTERSPAN_MAIL_FROM':'afterspan@agentmail.to'}),patch('accounts.http.client.HTTPSConnection') as https:
                    response=https.return_value.getresponse.return_value;response.status=status;response.read.return_value=raw
                    app.accounts.deliver_pending();https.return_value.close.assert_called_once()
                with closing(app.connect()) as c:row=c.execute('SELECT state,body,attempts FROM account_mail WHERE recipient=?',(email,)).fetchone()
                self.assertEqual(row['state'],'queued');self.assertEqual(row['attempts'],1);self.assertIsNotNone(row['body'])

    def test_agentmail_signup_requires_sender_and_private_credential(self):
        for sender,key in [('afterspan@agentmail.to',''),('invalid','private-test-key')]:
            with self.subTest(sender=sender),patch.dict(os.environ,{'AFTERSPAN_MAIL_MODE':'agentmail','AFTERSPAN_AGENTMAIL_API_KEY':key,'AFTERSPAN_MAIL_FROM':sender}):
                self.assertFalse(self.client.get('/auth/config').json()['email_sign_in'])
                self.assertEqual(self.client.post('/auth/request-link',headers=CSRF,json={'email':'blocked@example.test'}).status_code,503)

    def test_smtp_uses_tls_and_scrubs_sent_mail_body(self):
        self.client.post('/auth/request-link',headers=CSRF,json={'email':'smtp@example.test'})
        with patch.dict(os.environ,{'AFTERSPAN_MAIL_MODE':'smtp','AFTERSPAN_SMTP_HOST':'smtp.example.test','AFTERSPAN_MAIL_FROM':'hello@example.test'}),patch('accounts.smtplib.SMTP') as smtp:
            app.accounts.deliver_pending();client=smtp.return_value.__enter__.return_value
            smtp.return_value.starttls.assert_called_once();smtp.return_value.send_message.assert_called_once()
        with closing(app.connect()) as c:row=c.execute("SELECT state,body FROM account_mail WHERE recipient='smtp@example.test'").fetchone()
        self.assertEqual(row['state'],'sent');self.assertIsNone(row['body'])

    def test_populated_pre_workspace_database_migrates_without_losing_events(self):
        import sqlite3
        path=Path(self.fixture.temp.name)/'old.db'
        with sqlite3.connect(path) as c:
            c.executescript("CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,key_hash TEXT UNIQUE NOT NULL,created REAL NOT NULL); CREATE TABLE events(project TEXT NOT NULL REFERENCES projects(id),id TEXT NOT NULL,name TEXT NOT NULL,distinct_id TEXT,ts REAL NOT NULL,received REAL NOT NULL,properties TEXT NOT NULL,PRIMARY KEY(project,id));")
            c.execute('INSERT INTO projects VALUES(?,?,?,?)',('old-app','Old app','hash',time.time()))
            c.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?)',('old-app','one','saved',None,time.time(),time.time(),'{}'))
        with patch.object(app,'DB_PATH',path):
            app.init_db();app.init_db()
            with closing(app.connect()) as c:
                self.assertEqual(c.execute('SELECT workspace FROM projects').fetchone()[0],'legacy')
                self.assertEqual(c.execute('SELECT count(*) FROM events').fetchone()[0],1)
                self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])
                with self.assertRaises(sqlite3.IntegrityError):c.execute("INSERT INTO projects(id,name,key_hash,created) VALUES('no-workspace','No','another-hash',0)")

    def test_reader_agent_reports_effective_role_and_project_read_keys_die_with_removed_issuer(self):
        owner,oid=self.user('owner@example.test');w=self.workspace(owner);p=self.project(owner,w)['id']
        second,_=self.join(owner,w,'second@example.test','owner')
        key=self.key(owner,w,'reader');headers={'Authorization':'Bearer '+key['key']}
        self.assertEqual(self.client.get('/api/workspaces',headers=headers).json()['workspaces'][0]['role'],'reader')
        read=owner.post('/api/projects/'+p+'/read-keys',headers=CSRF,json={'label':'Analytics agent'}).json()
        aggregate={'Authorization':'Bearer '+read['read_key']}
        self.assertEqual(self.client.get('/api/overview?project='+p,headers=aggregate).status_code,200)
        self.assertEqual(second.delete('/api/workspaces/'+w+'/members/'+oid,headers=CSRF).status_code,200)
        self.assertEqual(self.client.get('/api/overview?project='+p,headers=aggregate).status_code,401)

    def test_secure_session_cookie_and_expired_session_are_enforced(self):
        self.client.post('/auth/request-link',headers=CSRF,json={'email':'cookie@example.test'});token=self.token('cookie@example.test')
        with patch.dict(os.environ,{'COOKIE_SECURE':'true'}):
            self.assertEqual(self.client.post('/auth/consume-link',headers=CSRF,json={'token':token}).status_code,503)
        with patch.dict(os.environ,{'COOKIE_SECURE':'true','ANALYTICS_PUBLIC_URL':'https://afterspan.example.test'}):
            response=self.client.post('/auth/consume-link',headers=CSRF,json={'token':token})
        cookie=response.headers.get_list('set-cookie')[0]
        self.assertIn('HttpOnly',cookie);self.assertIn('Secure',cookie);self.assertIn('SameSite=strict',cookie)
        person,_=self.user('session@example.test')
        with closing(app.connect()) as c,c:c.execute('UPDATE account_sessions SET expires=?',(time.time()-1,))
        self.assertEqual(person.get('/auth/me').status_code,401)

    def test_workspace_rename_is_owner_only_and_cannot_cross_workspaces(self):
        owner,_=self.user('owner@example.test');w=self.workspace(owner)
        reader,_=self.join(owner,w,'reader@example.test')
        outsider,_=self.user('outsider@example.test')
        for client in [reader,outsider]:self.assertEqual(client.patch('/api/workspaces/'+w,headers=CSRF,json={'name':'No'}).status_code,403)
        self.assertEqual(owner.patch('/api/workspaces/'+w,headers=CSRF,json={'name':'Renamed studio'}).status_code,200)
        self.assertEqual(reader.get('/api/workspaces').json()['workspaces'][0]['name'],'Renamed studio')
