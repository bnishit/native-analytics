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
    def test_check_execution_diagnostics_are_optional_searchable_and_not_proof(self):
        from tracing import WorkflowCheck,WorkflowEvidence
        from pydantic import ValidationError
        base={'name':'source_facts','version':1,'result':'unknown','required':True,'method':'source','phase':'before_delivery'}
        old=WorkflowCheck.model_validate(base).model_dump(mode='json')
        self.assertNotIn('execution',old);self.assertNotIn('reason_code',old)
        diagnostic={**base,'execution':'not_run','reason_code':'evidence_absent'}
        self.assertEqual(self.ingest(self.event(),self.event('succeeded',1,checks=[diagnostic])).status_code,200)
        row=self.review()['traces'][0]['checks'][0]
        self.assertEqual(row['execution'],'not_run');self.assertEqual(row['reason_code'],'evidence_absent');self.assertFalse(row['evidence_present'])
        for query in ['not_run','not run','evidence_absent','evidence absent']:
            response=self.client.post('/api/traces/search?project=alpha',headers=self.admin,json={'environment':'staging','search':query})
            self.assertEqual(response.status_code,200);self.assertEqual(response.json()['total'],1)
        for changes in [{'result':'passed'},{'result':'failed'},{'evidence_ref':str(uuid.uuid4())},{'execution':'completed'},{'execution':None},{'reason_code':'private arbitrary text'}]:
            with self.subTest(changes=changes),self.assertRaises(ValidationError):WorkflowCheck.model_validate({**diagnostic,**changes})
        self.assertNotIn('execution',WorkflowEvidence.model_fields);self.assertNotIn('reason_code',WorkflowEvidence.model_fields)

    def test_generic_checks_are_separate_from_step_success_and_hide_receipt_refs(self):
        receipt=str(uuid.uuid4())
        check={'name':'source_facts','version':1,'result':'failed','required':True,
               'method':'source','phase':'before_delivery','evidence_ref':receipt}
        self.assertEqual(self.ingest(self.event(),self.event('succeeded',1,checks=[check])).status_code,200)
        report=self.review();trace=report['traces'][0]
        self.assertEqual(trace['completion']['provider_send']['state'],'succeeded')
        self.assertEqual(trace['checks'][0]['result'],'failed')
        self.assertTrue(trace['checks'][0]['evidence_present'])
        self.assertEqual(trace['business_outcome'],'not_established')
        self.assertNotIn(receipt,json.dumps(report))
        response=self.client.post('/api/traces/open?project=alpha',headers=self.admin,
            json={'environment':'staging','reference':trace['trace']})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['trace']['checks'],trace['checks'])
        self.assertEqual(response.json()['trace']['status'],'failed')
        searched=self.client.post('/api/traces/search?project=alpha',headers=self.admin,
            json={'environment':'staging','search':'source_facts','status':'failed'}).json()
        self.assertEqual(searched['total'],1)
        self.ingest(self.event('succeeded',2,checks=[{**check,'result':'passed'}]))
        conflicted=self.review()['traces'][0]
        self.assertIn('span_conflict',conflicted['issues'])
        self.assertEqual(conflicted['checks'],[])

    def test_checks_are_bounded_terminal_only_and_not_free_text(self):
        check={'name':'schema_valid','version':1,'result':'unknown','required':True,
               'method':'code','phase':'after_delivery'}
        invalid=[self.event(checks=[check]),self.event('succeeded',checks=[check,check]),
                 self.event('succeeded',checks=[{**check,'required':'true'}]),
                 self.event('succeeded',checks=[{**check,'reason':'private output'}]),
                 self.event('succeeded',checks=[{**check,'evidence_ref':'raw-source-id'}])]
        for event in invalid:self.assertEqual(self.ingest(event).status_code,422)
        self.assertEqual(self.ingest(self.event(),self.event('succeeded',1,checks=[check])).status_code,200)
        self.assertEqual(self.review()['traces'][0]['checks'][0]['result'],'unknown')

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
        catalog=self.client.get('/api/traces/catalog?project=alpha',headers=headers).json()
        self.assertEqual({i['contract'] for i in catalog['integrations']},{'workflow-trace-v1','slack-request-v1'})
        slack=next(i for i in catalog['integrations'] if i['contract']=='slack-request-v1')
        self.assertIn('slack_read_request',slack['workflows'])
        self.assertEqual(slack['checks'][0]['evidence_version'],'source-check-v1')
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
