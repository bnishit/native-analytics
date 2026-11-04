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

class CatalogReplayTests(unittest.TestCase):
    enable=DebuggingTests.enable
    capture=DebuggingTests.capture
    read=DebuggingTests.read
    tearDown=DebuggingTests.tearDown

    def setUp(self):
        DebuggingTests.setUp(self)
        self.policy.update(contract='slack-request-v1',workflow='slack_read_request',environment='production')
        self.facts={'schema_version':'slack-request-v1','stage':'trace','request_ref':str(uuid.uuid4()),'attempt_ref':str(uuid.uuid4()),
                    'attempt_number':1,'request_started_at_ms':int(time.time()*1000),'span_ref':str(uuid.uuid4()),
                    'trace_step':'quality','span_state':'succeeded','duration_ms':1.0,'environment':'production',
                    'request_kind':'read','release_sha':'a'*40}
        self.event_id=str(uuid.uuid4())
        self.f.ingest([{'id':self.event_id,'name':'slack.audit.trace','properties':{'afterspan_audit':self.facts}}]).raise_for_status()
        request={'environment':'production','status':'enabled','limit':5,'intent':'bounded'}
        self.content={'schema_version':'catalog-check-replay-v1','release_sha':'a'*40,'request':request,
          'candidate':{'environment':'production','request':request,'coverage':{'checked':1,'complete':True},'matching':1,
                       'claims':[{'row':1,'enabled':False,'entries':0,'version':1}]},
          'source':{'coverage':{'checked':1,'complete':True},'rows':[{'row':1,'enabled':True,'archived':False,'entries':0,'version':1}]},
          'quality':{'check_version':'growthbook-catalog-v2','reported_outcome':'failed','checked_claims':1,'mismatched_claims':1,'unknown_claims':0},'blocked':[]}
        self.snapshot.update(contract='slack-request-v1',kind='replay',workflow='slack_read_request',environment='production',
          trace_id=self.facts['request_ref'],attempt_id=self.facts['attempt_ref'],span_id=self.facts['span_ref'],content=self.content)

    def query(self,project='alpha'):
        result=self.client.post('/api/traces/search?project='+project,headers=self.auth,json={'environment':'production','contract':'slack-request-v1'}).json()
        return {**result['query'],'reference':result['traces'][0]['reference']}

    def test_slack_fixture_reuses_encryption_selected_reads_and_exact_reference(self):
        self.enable().raise_for_status();self.capture().raise_for_status()
        listed=self.read().json();self.assertNotIn('content',listed['snapshots'][0]);self.assertEqual(listed['snapshots'][0]['replay']['status'],'available')
        opened=self.read(id=self.snapshot['id']);self.assertEqual(opened.status_code,200,opened.text)
        self.assertFalse(opened.json()['replay']['full_request_replay']);self.assertFalse(opened.json()['replay']['replayed'])
        self.assertEqual(opened.json()['content'],self.content)
        for private in [self.facts['request_ref'],self.facts['span_ref']]:self.assertNotIn(private,opened.text)
        with closing(test_analytics.analytics.connect()) as c:self.assertNotIn(b'catalog-check',c.execute('SELECT payload FROM debug_snapshots').fetchone()[0])
        self.assertEqual(self.read(id=self.snapshot['id'],project='beta').status_code,404)

    def test_native_capture_policy_does_not_grant_slack_capture(self):
        self.enable({**self.policy,'contract':'workflow-trace-v1'}).raise_for_status()
        self.assertEqual(self.capture().status_code,403)

    def test_only_failed_checks_can_consume_the_approved_capture_budget(self):
        self.enable().raise_for_status()
        for outcome in ['unknown','succeeded']:
            body={**self.snapshot,'id':str(uuid.uuid4()),'content':{**self.content,'quality':{**self.content['quality'],'reported_outcome':outcome}}}
            response=self.capture(body);self.assertEqual(response.status_code,403,response.text)
        self.assertEqual(self.client.get('/api/debug/policy?project=alpha',headers=self.auth).json()['captured_count'],0)
        self.capture().raise_for_status()
        self.assertEqual(self.client.get('/api/debug/policy?project=alpha',headers=self.auth).json()['captured_count'],1)

    def test_arbitrary_text_and_wrong_release_cannot_become_replay_content(self):
        self.enable().raise_for_status()
        for extra in [{'message':'private text'},{'password':'private secret'},{'source_url':'https://private.example'}]:
            response=self.capture({**self.snapshot,'content':{**self.content,**extra}})
            self.assertEqual(response.status_code,422,response.text);self.assertNotIn('private secret',response.text)
        self.assertEqual(self.capture({**self.snapshot,'content':{**self.content,'release_sha':'b'*40}}).status_code,409)
        self.assertEqual(self.capture({**self.snapshot,'content':{**self.content,'request':{**self.content['request'],'environment':'staging'}}}).status_code,409)
        self.assertEqual(self.capture({**self.snapshot,'kind':'context'}).status_code,422)

    def test_missing_source_and_release_are_explicitly_blocked(self):
        self.enable().raise_for_status()
        self.capture({**self.snapshot,'content':{**self.content,'source':None,'release_sha':None}}).raise_for_status()
        result=self.read(id=self.snapshot['id']).json()['replay']
        self.assertEqual(result['status'],'blocked');self.assertEqual(result['reasons'],['release_unavailable','source_unavailable'])

    def test_budget_retry_expiry_and_source_erasure_apply_to_slack(self):
        self.enable({**self.policy,'max_snapshots':1}).raise_for_status();first=self.capture().json()
        self.assertEqual(self.capture().json()['expires'],first['expires'])
        self.assertEqual(self.capture({**self.snapshot,'id':str(uuid.uuid4())}).status_code,429)
        with patch('debugging.time.time',return_value=time.time()+130):self.assertEqual(self.read(id=self.snapshot['id']).status_code,410)
        query=self.query()
        with closing(test_analytics.analytics.connect()) as c,c:c.execute('DELETE FROM events WHERE project=? AND id=?',('alpha',self.event_id))
        self.assertEqual(self.read(query).status_code,404)
        with closing(test_analytics.analytics.connect()) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM debug_snapshots').fetchone()[0],0)

    def test_absent_replay_and_revocation_never_look_runnable(self):
        self.enable().raise_for_status()
        result=self.read().json();self.assertEqual(result['replay']['status'],'blocked');self.assertIn('not_retained_in_window',result['replay']['reasons'])
        self.capture().raise_for_status();self.enable({**self.policy,'enabled':False},revision=1).raise_for_status()
        self.assertEqual(self.read(id=self.snapshot['id']).status_code,403)

    def test_reviewed_case_keeps_replay_limits_and_does_not_guess_expected_facts(self):
        content={**self.content,'source':None}
        case={'title':'Reviewed anonymous check','input':content,'expected':{'reported_outcome':'failed'},'privacy_reviewed':True}
        response=self.client.put('/api/experiments/cases/'+str(uuid.uuid4())+'?project=alpha&revision=0',headers=self.auth,json=case)
        self.assertEqual(response.status_code,200,response.text);self.assertEqual(response.json()['replay']['status'],'blocked')
        listed=self.client.get('/api/experiments/cases?project=alpha',headers=self.auth).json()['items'][0]['definition']
        self.assertEqual(listed['replay']['reasons'],['source_unavailable']);self.assertNotIn('input',listed)
        self.assertEqual(response.json()['definition']['expected'],case['expected'])

    def test_capture_does_not_leak_into_an_earlier_frozen_receipt(self):
        self.enable().raise_for_status();query=self.query();self.capture().raise_for_status()
        result=self.read(query).json();self.assertEqual(result['snapshots'],[])
        self.assertEqual(result['replay']['status'],'blocked');self.assertEqual(len(self.read().json()['snapshots']),1)

    def test_only_quality_read_spans_and_separately_authorized_readers_can_inspect(self):
        self.enable().raise_for_status()
        self.assertEqual(self.capture({**self.snapshot,'attempt_id':str(uuid.uuid4())}).status_code,409)
        self.capture().raise_for_status();query=self.query()
        for purpose in ['analytics','audit']:
            key=self.client.post('/api/projects/alpha/read-keys',headers=self.auth,json={'label':'replay denied','purpose':purpose}).json()['read_key']
            self.assertIn(self.read(query,self.snapshot['id'],headers={'Authorization':'Bearer '+key}).status_code,[401,403])
        self.assertEqual(self.read(query,self.snapshot['id'],headers=self.f.a).status_code,401)
