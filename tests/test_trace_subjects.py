"""Native subject privacy, scope consistency and account-erasure replay boundaries."""
from contextlib import closing
import json,time,unittest,uuid
import test_analytics
from afterspan_tracing import WorkflowTracer
from native_analytics import Collector

SUBJECT='account_'+'a'*64
OTHER='account_'+'b'*64
class TraceSubjectTests(unittest.TestCase):
    def setUp(self):
        self.f=test_analytics.AnalyticsTests();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def event(self,subject=SUBJECT,trace=None):
        return {'id':str(uuid.uuid4()),'name':'trace.observed','distinct_id':subject,'properties':{'afterspan_trace':{'schema_version':'workflow-trace-v1','trace_id':trace or str(uuid.uuid4()),'attempt_id':str(uuid.uuid4()),'attempt_number':1,'span_id':str(uuid.uuid4()),'workflow':'subject_probe','environment':'test','step':'processing','state':'started'}}}
    def test_sdk_subject_binds_trace_and_evidence_without_extra_attributes(self):
        collector=Collector('https://analytics.example','synthetic-key',self.f.temp.name+'/outbox.db')
        tracer=WorkflowTracer(collector,subject_id=SUBJECT)
        with tracer.task('subject_probe','test',traffic_kind='synthetic'):
            with tracer.span('processing',milestone='completion') as span:span.check('scope','passed',method='code',evidence_digest='a'*64,execution='completed')
        with collector.db() as c:events=[json.loads(r[0]) for r in c.execute('SELECT payload FROM outbox ORDER BY created')]
        self.assertTrue(all(e['distinct_id']==SUBJECT and not e.get('attributes') and len(e['properties'])==1 for e in events))
        response=self.f.ingest(events);self.assertEqual(response.status_code,200,response.text)
        report=self.f.client.post('/api/traces/review?project=alpha',headers=self.f.admin,json={'environment':'test'}).json()
        self.assertEqual(report['traces'][0]['checks'][0]['evidence_status'],'resolved');self.assertNotIn(SUBJECT,json.dumps(report))
        with closing(test_analytics.analytics.connect()) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM workflow_trace_subjects').fetchone()[0],1)
    def test_raw_identity_and_native_attributes_remain_rejected(self):
        for value in ['person@example.com','1234567890',str(uuid.uuid4()),'account_'+'a'*63,SUBJECT+'\n','A_'+'a'*64]:
            self.assertEqual(self.f.ingest([self.event(value)]).status_code,422,value)
        event=self.event();event['attributes']={'application':'fixture'};self.assertEqual(self.f.ingest([event]).status_code,422)
        for value in ['person@example.com',SUBJECT+'\n']:
            with self.assertRaises(ValueError):WorkflowTracer(Collector('https://analytics.example','synthetic-key',self.f.temp.name+'/invalid.db'),subject_id=value)
    def test_a_trace_cannot_change_subject_or_retroactively_gain_one(self):
        for initial,next_subject in [(SUBJECT,OTHER),(SUBJECT,None),(None,SUBJECT)]:
            event=self.event(initial);self.assertEqual(self.f.ingest([event]).status_code,200)
            changed=self.event(next_subject,event['properties']['afterspan_trace']['trace_id'])
            response=self.f.ingest([changed]);self.assertEqual(response.status_code,409,response.text)
    def test_erasure_blocks_same_identity_and_same_trace_even_after_identity_is_removed(self):
        event=self.event();trace=event['properties']['afterspan_trace']['trace_id']
        self.assertEqual(self.f.ingest([event]).status_code,200)
        # The same opaque value and trace in another project must stay independent.
        self.assertEqual(self.f.ingest([event],self.f.b).status_code,200)
        erased=self.f.client.post('/v1/erase',headers=self.f.a,json={'distinct_id':SUBJECT});self.assertEqual(erased.status_code,200);self.assertEqual(erased.json()['erased_events'],1)
        for subject in [SUBJECT,None,OTHER]:
            replay=self.event(subject,trace);r=self.f.ingest([replay]);self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['accepted'],0)
        r=self.f.ingest([self.event(SUBJECT)]);self.assertEqual(r.json()['accepted'],0)
        self.assertEqual(self.f.ingest([self.event(OTHER)]).json()['accepted'],1)
        with closing(test_analytics.analytics.connect()) as c:
            row=c.execute('SELECT * FROM workflow_trace_subjects WHERE project=? AND trace_id=?',('alpha',trace)).fetchone();self.assertIsNotNone(row['erased']);self.assertIsNone(row['subject_id'])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM events WHERE project='beta'").fetchone()[0],1)
    def test_conflicting_subjects_roll_back_the_entire_batch(self):
        event=self.event();trace=event['properties']['afterspan_trace']['trace_id']
        self.assertEqual(self.f.ingest([event,self.event(OTHER,trace)]).status_code,409)
        with closing(test_analytics.analytics.connect()) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM events').fetchone()[0],0);self.assertEqual(c.execute('SELECT COUNT(*) FROM workflow_trace_subjects').fetchone()[0],0)
    def test_migration_preserves_anonymous_binding_and_prune_keeps_erasure_tombstones(self):
        event=self.event(None);trace=event['properties']['afterspan_trace']['trace_id'];self.assertEqual(self.f.ingest([event]).status_code,200)
        with closing(test_analytics.analytics.connect()) as c,c:c.execute('DROP TABLE workflow_trace_subjects')
        test_analytics.analytics.init_db()
        self.assertEqual(self.f.ingest([self.event(SUBJECT,trace)]).status_code,409)
        erased=self.event();erased_trace=erased['properties']['afterspan_trace']['trace_id'];self.f.ingest([erased]);self.f.client.post('/v1/erase',headers=self.f.a,json={'distinct_id':SUBJECT})
        with closing(test_analytics.analytics.connect()) as c,c:
            c.execute('UPDATE workflow_trace_subjects SET last_seen=?',(time.time()-1000*86400,));c.execute('DELETE FROM events')
        test_analytics.analytics.prune()
        with closing(test_analytics.analytics.connect()) as c:
            self.assertIsNone(c.execute('SELECT 1 FROM workflow_trace_subjects WHERE trace_id=?',(trace,)).fetchone());self.assertIsNotNone(c.execute('SELECT 1 FROM workflow_trace_subjects WHERE trace_id=?',(erased_trace,)).fetchone())
    def test_erased_subject_cannot_tombstone_another_subjects_active_trace(self):
        self.f.client.post('/v1/erase',headers=self.f.a,json={'distinct_id':SUBJECT})
        event=self.event(OTHER);trace=event['properties']['afterspan_trace']['trace_id'];self.assertEqual(self.f.ingest([event]).status_code,200)
        self.assertEqual(self.f.ingest([self.event(SUBJECT,trace)]).status_code,409)
        self.assertEqual(self.f.ingest([self.event(OTHER,trace)]).json()['accepted'],1)
    def test_account_erase_cascades_selected_debug_content_without_refunding_capture_budget(self):
        import base64,os
        from unittest.mock import patch
        event=self.event();facts=event['properties']['afterspan_trace'];facts['step']='model'
        self.assertEqual(self.f.ingest([event]).status_code,200)
        policy={'privacy_reviewed':True,'enabled':True,'workflow':'subject_probe','environment':'test','window_seconds':60,'retention_seconds':120,'max_snapshots':1}
        with patch.dict(os.environ,{'AFTERSPAN_CONNECTION_KEY':base64.urlsafe_b64encode(b'k'*32).decode()}):
            self.assertEqual(self.f.client.put('/api/debug/policy?project=alpha&revision=0',headers=self.f.admin,json=policy).status_code,200)
            body={k:facts[k] for k in ['trace_id','attempt_id','span_id','workflow','environment']};body.update(id=str(uuid.uuid4()),policy_revision=1,privacy_reviewed=True,kind='context',content={'answer':'synthetic public fixture'})
            response=self.f.client.post('/v1/debug',headers=self.f.a,json=body);self.assertEqual(response.status_code,200,response.text)
            self.f.client.post('/v1/erase',headers=self.f.a,json={'distinct_id':SUBJECT}).raise_for_status()
            with closing(test_analytics.analytics.connect()) as c:self.assertEqual(c.execute('SELECT count(*) FROM debug_snapshots').fetchone()[0],0)
            current=self.f.client.get('/api/debug/policy?project=alpha',headers=self.f.admin).json();self.assertEqual(current['captured_count'],1);self.assertEqual(current['stored_snapshots'],0)
