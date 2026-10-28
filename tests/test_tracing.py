import json
import time
import unittest
import uuid
from datetime import datetime,timezone
import test_analytics

class TracingTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_analytics.AnalyticsTests();self.fixture.setUp()
        self.client=self.fixture.client;self.admin=self.fixture.admin
        self.tid=str(uuid.uuid4());self.aid=str(uuid.uuid4());self.sid=str(uuid.uuid4());self.base=time.time()-120
    def tearDown(self):self.fixture.tearDown()
    def event(self,state='started',at=0,**changes):
        f={'schema_version':'workflow-trace-v1','trace_id':self.tid,'attempt_id':self.aid,'attempt_number':1,'span_id':self.sid,
           'workflow':'appointment_request','environment':'staging','step':'provider_send','state':state}
        if state!='started':f['duration_ms']=100
        f.update(changes)
        return {'id':str(uuid.uuid4()),'name':'trace.observed','timestamp':datetime.fromtimestamp(self.base+at,timezone.utc).isoformat(),'properties':{'afterspan_trace':f}}
    def ingest(self,*events,headers=None):return self.client.post('/v1/events',headers=headers or self.fixture.a,json={'events':list(events)})
    def review(self,**changes):
        r=self.client.post('/api/traces/review?project=alpha',headers=self.admin,json={'environment':'staging',**changes})
        self.assertEqual(r.status_code,200,r.text);return r.json()
    def test_acceptance_is_not_delivery_or_booking_and_retries_deduplicate(self):
        start=self.event();end=self.event('succeeded',1)
        self.assertEqual(self.ingest(end,start).status_code,200)
        self.assertEqual(self.ingest(end).json()['duplicates'],1)
        report=self.review();trace=report['traces'][0]
        self.assertEqual(trace['completion']['provider_send']['state'],'succeeded')
        self.assertEqual(trace['completion']['provider_delivery']['state'],'missing')
        self.assertEqual(trace['completion']['booking']['state'],'missing')
        self.assertEqual(trace['business_outcome'],'not_established')
        self.assertNotIn(self.tid,json.dumps(report));self.assertNotIn(self.aid,json.dumps(report))
        detail=self.client.post('/api/traces/detail?project=alpha',headers=self.admin,json={**report['query'],'trace':trace['trace']})
        self.assertEqual(detail.status_code,200);self.assertEqual(detail.json()['trace'],trace)
    def test_missing_start_conflicts_and_parent_cycles_stay_unknown(self):
        self.ingest(self.event('succeeded',1))
        self.assertIn('missing_start',self.review()['traces'][0]['issues'])
        self.ingest(self.event(),self.event('failed',2,outcome_code='timeout'))
        self.assertIn('span_conflict',self.review()['traces'][0]['issues'])
        second=str(uuid.uuid4());self.tid=str(uuid.uuid4());self.sid=str(uuid.uuid4())
        self.ingest(self.event(parent_span_id=second),self.event(span_id=second,parent_span_id=self.sid))
        self.assertTrue(any('parent_cycle' in t['issues'] for t in self.review()['traces']))
    def test_scope_isolation_and_no_raw_payloads(self):
        event=self.event();self.ingest(event)
        self.assertEqual(self.client.post('/api/traces/review?project=alpha',json={'environment':'staging'}).status_code,401)
        key=self.client.post('/api/projects/alpha/read-keys',headers=self.admin,json={'label':'trace audit','purpose':'audit'}).json()['read_key']
        headers={'Authorization':'Bearer '+key}
        self.assertEqual(self.client.post('/api/traces/review?project=alpha',headers=headers,json={'environment':'staging'}).status_code,200)
        self.assertEqual(self.client.get('/api/traces/catalog?project=beta',headers=headers).status_code,403)
        self.assertEqual(self.client.get('/api/events?project=alpha',headers=headers).status_code,401)
        bad=self.event();bad['properties']['afterspan_trace']['patient_phone']='private'
        self.assertEqual(self.ingest(bad).status_code,422)
        bad=self.event();bad['properties']['outside']='raw message'
        self.assertEqual(self.ingest(bad).status_code,422)
        bad=self.event();bad['distinct_id']='patient'
        self.assertEqual(self.ingest(bad).status_code,422)
        self.assertEqual(self.ingest(self.event(trace_id='patient-id')).status_code,422)
    def test_snapshot_environment_and_retention_boundaries(self):
        self.ingest(self.event(),self.event('succeeded',1))
        frozen=self.review();self.ingest(self.event('failed',2,outcome_code='timeout'))
        self.assertEqual(self.review(**frozen['query'])['traces'],frozen['traces'])
        self.assertEqual(self.review(environment='production')['traces'],[])
        later=datetime.fromtimestamp(self.base+.5,timezone.utc).isoformat()
        end=datetime.fromtimestamp(self.base+10,timezone.utc).isoformat()
        self.assertIn('missing_start',self.review(start=later,end=end)['traces'][0]['issues'])
    def test_attempt_and_release_conflicts_are_not_success(self):
        self.ingest(self.event(),self.event('succeeded',1,attempt_id=str(uuid.uuid4())))
        self.assertIn('attempt_identity_conflict',self.review()['traces'][0]['issues'])
        self.assertEqual(self.review()['traces'][0]['completion']['provider_send']['state'],'unknown')

    def test_three_receipts_link_to_one_booking_without_fanout(self):
        inputs=[str(uuid.uuid4()) for _ in range(3)]
        for tid in inputs:
            self.assertEqual(self.ingest(self.event(trace_id=tid,step='receive',span_id=str(uuid.uuid4()))).status_code,200)
        self.assertEqual(self.ingest(self.event(at=1,step='processing',receipt_count=3,linked_trace_ids=inputs),
            self.event('succeeded',2,step='processing'),
            self.event(at=3,step='booking',span_id=(booking:=str(uuid.uuid4()))),
            self.event('succeeded',4,step='booking',span_id=booking)).status_code,200)
        report=self.review(limit=1);batch=report['traces'][0]
        self.assertEqual(batch['batch']['receipt_count'],3)
        self.assertEqual(batch['completion']['booking'],{'state':'succeeded','observations':1})
        detail=self.client.post('/api/traces/detail?project=alpha',headers=self.admin,json={**report['query'],'trace':batch['trace']}).json()
        self.assertEqual(len(detail['linked_inputs']),3)
        self.assertTrue(all(t['completion']['booking']['observations']==0 for t in detail['linked_inputs']))
        self.assertTrue(all(tid not in json.dumps(detail) for tid in inputs+[self.tid]))

    def test_links_are_strict_bounded_and_frozen(self):
        links=[str(uuid.uuid4()) for _ in range(21)]
        invalid=[{'receipt_count':1}, {'linked_trace_ids':[]}, {'receipt_count':True,'linked_trace_ids':[]},
            {'receipt_count':21,'linked_trace_ids':links}, {'receipt_count':1,'linked_trace_ids':links[:2]},
            {'receipt_count':2,'linked_trace_ids':[links[0],links[0]]}, {'receipt_count':1,'linked_trace_ids':[self.tid]},
            {'receipt_count':1,'linked_trace_ids':['patient']}, {'receipt_count':1,'linked_trace_ids':[],'step':'booking'},
            {'receipt_count':1,'linked_trace_ids':[],'state':'succeeded'}]
        for changes in invalid:
            self.assertEqual(self.ingest(self.event(**{'step':'processing',**changes})).status_code,422,changes)
        self.assertEqual(self.ingest(self.event(step='processing',receipt_count=20,linked_trace_ids=links[:20])).status_code,200)
        self.ingest(self.event(step='processing',receipt_count=20,linked_trace_ids=links[1:]))
        trace=self.review()['traces'][0]
        self.assertIn('batch_membership_conflict',trace['issues']);self.assertIn('span_conflict',trace['issues'])
        self.assertIsNone(trace['batch'])

    def test_links_never_resolve_across_project_environment_or_snapshot(self):
        links=[str(uuid.uuid4()) for _ in range(3)]
        self.ingest(self.event(trace_id=links[0],step='receive'),headers=self.fixture.b)
        self.ingest(self.event(trace_id=links[1],step='receive',environment='production'))
        self.ingest(self.event(at=1,step='processing',receipt_count=4,linked_trace_ids=links))
        frozen=self.review();batch=frozen['traces'][0]
        self.assertEqual(batch['batch']['unlinked_receipts'],1)
        self.assertTrue(all(i['state']=='missing_in_window' for i in batch['batch']['inputs']))
        self.ingest(self.event(trace_id=links[2],step='receive'))
        detail=self.client.post('/api/traces/detail?project=alpha',headers=self.admin,json={**frozen['query'],'trace':batch['trace']}).json()
        self.assertEqual(detail['linked_inputs'],[])
        self.assertEqual(detail['trace'],batch)

    def test_maximum_parent_chain_is_bounded_and_larger_review_refuses_partial_results(self):
        from tracing import project_trace
        from contextlib import closing
        import app
        rows=[];parent=None
        for n in range(20000):
            sid=str(uuid.uuid4())
            facts=self.event(span_id=sid,parent_span_id=parent)['properties']['afterspan_trace']
            rows.append({'ts':self.base+n/1000,'facts':facts});parent=sid
        started=time.monotonic();result=project_trace(rows,'trace_test')
        self.assertEqual(len(result['spans']),20000)
        self.assertLess(time.monotonic()-started,5,'parent traversal must not revisit every ancestor for each span')
        with closing(app.connect()) as c,c:
            c.executemany('INSERT INTO events VALUES(?,?,?,?,?,?,?)',
                [('alpha','bounded-'+str(n),'trace.observed',None,self.base,time.time(),json.dumps({'afterspan_trace':rows[0]['facts']})) for n in range(20001)])
        r=self.client.post('/api/traces/review?project=alpha',headers=self.admin,json={'environment':'staging'})
        self.assertEqual(r.status_code,422);self.assertIn('no partial report',r.text)
