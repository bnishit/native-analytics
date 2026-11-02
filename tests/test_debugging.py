import base64
from contextlib import closing
import json
import os
import time
import unittest
import uuid
from unittest.mock import patch
import test_analytics
from sdk.python.afterspan_experiments import redact_debug

class DebuggingTests(unittest.TestCase):
    def setUp(self):
        self.f=test_analytics.AnalyticsTests();self.f.setUp();self.client=self.f.client;self.auth=self.f.admin
        self.crypto=patch.dict(os.environ,{'AFTERSPAN_CONNECTION_KEY':base64.urlsafe_b64encode(b'k'*32).decode()});self.crypto.start()
        self.policy={'privacy_reviewed':True,'enabled':True,'workflow':'document_export','environment':'test','window_seconds':60,'retention_seconds':120,'max_snapshots':2}
        self.facts={'schema_version':'workflow-trace-v1','trace_id':str(uuid.uuid4()),'attempt_id':str(uuid.uuid4()),'attempt_number':1,'span_id':str(uuid.uuid4()),'workflow':'document_export','environment':'test','step':'model','state':'started'}
        self.event_id=str(uuid.uuid4());self.f.ingest([{'id':self.event_id,'name':'trace.observed','properties':{'afterspan_trace':self.facts}}]).raise_for_status()
        self.snapshot={k:self.facts[k] for k in ['trace_id','attempt_id','span_id','workflow','environment']}
        self.snapshot.update(id=str(uuid.uuid4()),policy_revision=1,privacy_reviewed=True,kind='context',content={'answer':'selected public fact'})
    def tearDown(self):self.crypto.stop();self.f.tearDown()
    def enable(self,body=None,revision=0,project='alpha'):
        return self.client.put(f'/api/debug/policy?project={project}&revision={revision}',headers=self.auth,json=body or self.policy)
    def capture(self,body=None,headers=None):return self.client.post('/v1/debug',headers=headers or self.f.a,json=body or self.snapshot)
    def query(self,project='alpha'):
        r=self.client.post('/api/traces/search?project='+project,headers=self.auth,json={'environment':'test'}).json()
        return {**r['query'],'reference':r['traces'][0]['reference']}
    def read(self,query=None,id=None,project='alpha',headers=None):
        return self.client.post('/api/debug/review?project='+project,headers=headers or self.auth,json={**(query or self.query()),**({'snapshot_id':id} if id else {})})
    def test_default_off_and_missing_key_never_fall_back_to_plaintext(self):
        response=self.client.get('/api/debug/policy?project=alpha',headers=self.auth).json()
        self.assertFalse(response['active']);self.assertEqual(response['revision'],0)
        self.assertEqual(self.capture().status_code,403)
        with patch.dict(os.environ,{'AFTERSPAN_CONNECTION_KEY':''}):self.assertEqual(self.enable().status_code,503)
        with closing(test_analytics.analytics.connect()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM debug_snapshots').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM debug_policies').fetchone()[0],0)
    def test_explicit_capture_is_redacted_encrypted_and_scoped_to_exact_trace(self):
        self.assertEqual(self.enable().status_code,200)
        body={**self.snapshot,'content':{'prompt':'Contact private@example.com or +91 98765 43210. Bearer private-value','password':'private-value','metadata':{'cookie':'private-cookie','status':'public'}}}
        response=self.capture(body);self.assertEqual(response.status_code,200,response.text)
        query=self.query();listed=self.read(query).json();self.assertEqual(len(listed['snapshots']),1)
        self.assertNotIn('content',listed['snapshots'][0]);self.assertNotIn(self.facts['trace_id'],json.dumps(listed))
        opened=self.read(query,body['id']);self.assertEqual(opened.status_code,200,opened.text);content=opened.json()['content']
        self.assertEqual(content,{'prompt':'Contact [redacted] or [redacted]. [redacted]','metadata':{'status':'public'}})
        for value in ['private@example.com','98765','private-value','private-cookie',self.facts['trace_id']]:self.assertNotIn(value,opened.text)
        with closing(test_analytics.analytics.connect()) as c:
            blob=c.execute('SELECT payload FROM debug_snapshots').fetchone()[0]
            actions=[r[0] for r in c.execute('SELECT action FROM access_audit')]
        self.assertNotIn(b'Contact',blob);self.assertNotIn(b'public',blob);self.assertIn('project.debug_snapshot_read',actions)
        self.assertEqual(self.read(query,body['id'],project='beta').status_code,404)
    def test_retry_does_not_renew_snapshot_or_policy_and_changed_uuid_content_conflicts(self):
        original=self.enable().json();self.assertEqual(self.enable().json()['expires'],original['expires'])
        first=self.capture().json();same=self.capture().json();self.assertTrue(same['duplicate']);self.assertEqual(same['expires'],first['expires'])
        self.assertEqual(self.capture({**self.snapshot,'content':{'answer':'changed'}}).status_code,409)
        self.assertEqual(self.client.get('/api/debug/policy?project=alpha',headers=self.auth).json()['captured_count'],1)
        self.assertEqual(self.enable({**self.policy,'window_seconds':120}).status_code,409)
    def test_window_expiry_stops_new_capture_but_retained_review_is_available(self):
        self.enable().raise_for_status();self.capture().raise_for_status();now=time.time()
        with patch('debugging.time.time',return_value=now+70):
            self.assertEqual(self.capture({**self.snapshot,'id':str(uuid.uuid4())}).status_code,403)
            query=self.query();self.assertEqual(self.read(query,self.snapshot['id']).status_code,200)
        with patch('debugging.time.time',return_value=now+130):
            query=self.query();self.assertEqual(self.read(query,self.snapshot['id']).status_code,410)
            test_analytics.analytics.prune()
            with closing(test_analytics.analytics.connect()) as c:self.assertEqual(c.execute('SELECT count(*) FROM debug_snapshots').fetchone()[0],0)
    def test_policy_change_revokes_previous_snapshots_and_stale_producers(self):
        self.enable().raise_for_status();self.capture().raise_for_status()
        response=self.enable({**self.policy,'environment':'staging'},revision=1);self.assertEqual(response.status_code,200)
        self.assertEqual(self.capture().status_code,409)
        self.assertEqual(self.read(id=self.snapshot['id']).status_code,404)
        with closing(test_analytics.analytics.connect()) as c:self.assertEqual(c.execute('SELECT count(*) FROM debug_snapshots').fetchone()[0],0)
        self.assertEqual(self.enable({**self.policy,'enabled':False},revision=2).status_code,200)
        self.assertEqual(self.capture().status_code,403)
        self.assertEqual(self.read().status_code,403)
    def test_durable_budget_cannot_be_reset_by_expiry_or_cleanup(self):
        policy={**self.policy,'max_snapshots':1,'retention_seconds':60,'window_seconds':3600}
        self.enable(policy).raise_for_status();self.capture().raise_for_status();now=time.time()
        with patch('debugging.time.time',return_value=now+70):
            test_analytics.analytics.prune()
            self.assertEqual(self.capture({**self.snapshot,'id':str(uuid.uuid4())}).status_code,429)
            self.assertEqual(self.client.get('/api/debug/policy?project=alpha',headers=self.auth).json()['captured_count'],1)
    def test_unretained_or_wrong_scope_spans_and_false_review_flags_are_rejected(self):
        self.enable().raise_for_status()
        for change,status in [({'workflow':'other'},403),({'environment':'production'},403),({'span_id':str(uuid.uuid4())},409),({'privacy_reviewed':False},422),({'privacy_reviewed':'true'},422),({'privacy_reviewed':1},422),({'policy_revision':True},422)]:
            r=self.capture({**self.snapshot,**change});self.assertEqual(r.status_code,status,r.text)
        self.assertEqual(self.enable({**self.policy,'enabled':1},revision=1).status_code,422)
    def test_aggregate_audit_write_and_unauthenticated_credentials_cannot_read_content(self):
        self.enable().raise_for_status();self.capture().raise_for_status();query=self.query()
        for purpose in ['analytics','audit']:
            key=self.client.post('/api/projects/alpha/read-keys',headers=self.auth,json={'label':'debug denied','purpose':purpose}).json()['read_key']
            auth={'Authorization':'Bearer '+key}
            self.assertIn(self.read(query,self.snapshot['id'],headers=auth).status_code,[401,403])
            self.assertIn(self.client.put('/api/debug/policy?project=alpha&revision=1',headers=auth,json=self.policy).status_code,[401,403])
        self.assertEqual(self.read(query,self.snapshot['id'],headers=self.f.a).status_code,401)
        self.assertEqual(self.client.post('/api/debug/review?project=alpha',json=query).status_code,401)
        self.assertEqual(self.capture(headers=self.auth).status_code,401)
    def test_aead_binding_and_erasure_cascade_protect_stored_content(self):
        self.enable().raise_for_status();self.capture().raise_for_status()
        second={**self.snapshot,'id':str(uuid.uuid4()),'content':{'answer':'another selected public fact'}};self.capture(second).raise_for_status()
        with closing(test_analytics.analytics.connect()) as c,c:
            blob=c.execute('SELECT payload FROM debug_snapshots WHERE id=?',(second['id'],)).fetchone()[0]
            c.execute('UPDATE debug_snapshots SET payload=? WHERE id=?',(blob,self.snapshot['id']))
        self.assertEqual(self.read(id=self.snapshot['id']).status_code,503)
        with closing(test_analytics.analytics.connect()) as c,c:c.execute('DELETE FROM events WHERE project=? AND id=?',('alpha',self.event_id))
        with closing(test_analytics.analytics.connect()) as c:self.assertEqual(c.execute('SELECT count(*) FROM debug_snapshots').fetchone()[0],0)
    def test_client_redaction_is_idempotent_and_rejects_oversize_non_json_values(self):
        raw={'prompt':'email private@example.com','otp':'123456','session':'private-session','context':{'enabled':False,'count':0}}
        safe=redact_debug(raw);self.assertEqual(safe,redact_debug(safe));self.assertNotIn('private',json.dumps(safe))
        self.assertFalse(safe['context']['enabled']);self.assertEqual(safe['context']['count'],0)
        for value in [{'x':float('inf')},{'x':'a'*4001},{'x':[0]*101},{'x':object()}]:
            with self.assertRaises((ValueError,TypeError)):redact_debug(value)
