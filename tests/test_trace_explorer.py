import json
import unittest
import uuid
from test_tracing import TracingTests
from test_audit import AuditTests
from test_workspaces import CSRF
from trace_explorer import finish_trace

class ExplorerTests(unittest.TestCase):
    def setUp(self):
        self.f=TracingTests();self.f.setUp();self.client=self.f.client;self.admin=self.f.admin
    def tearDown(self):self.f.tearDown()
    def search(self,**query):
        r=self.client.post('/api/traces/search?project=alpha',headers=self.admin,json={'environment':'staging',**query})
        self.assertEqual(r.status_code,200,r.text);return r.json()
    def test_generic_product_search_pagination_and_frozen_detail(self):
        ids=[]
        for i in range(3):
            self.f.tid=str(uuid.uuid4());ids.append(self.f.tid)
            self.assertEqual(self.f.ingest(self.f.event(at=i*2,workflow='document_export',step='storage'),self.f.event('succeeded',i*2+1,workflow='document_export',step='storage')).status_code,200)
        r=self.search(search='storage',limit=1)
        self.assertEqual(r['total'],3);self.assertTrue(r['has_more'])
        second=self.search(**{**r['query'],'offset':1})
        self.assertNotEqual(second['traces'][0]['reference'],r['traces'][0]['reference'])
        opened=self.client.post('/api/traces/open?project=alpha',headers=self.admin,json={**r['query'],'reference':second['traces'][0]['reference']})
        self.assertEqual(opened.status_code,200,opened.text)
        row=opened.json()['trace'];self.assertEqual(row['name'],'document_export');self.assertEqual(row['status'],'observed')
        self.assertEqual(row['business_outcome'],'not_independently_verified');self.assertEqual(row['unobserved_categories'],[])
        self.f.tid=str(uuid.uuid4());self.f.ingest(self.f.event(workflow='document_export',step='storage'))
        self.assertEqual(self.search(**r['query'])['total'],3)
        for tid in ids:self.assertNotIn(tid,json.dumps(opened.json()))
        self.assertEqual(self.search(environment='production')['total'],0)
        self.assertEqual(self.search(search='nonexistent')['total'],0)
    def test_unclosed_child_is_a_gap_even_when_summary_steps_finished(self):
        row=finish_trace({'issues':[],'spans':[{'state':'unclosed','parent_missing':False,'end_ms':None},{'state':'succeeded','parent_missing':False,'end_ms':100}],
                          'evidence':[{'state':'observed'},{'state':'succeeded'}]})
        self.assertTrue(row['has_gaps']);self.assertEqual(row['status'],'gaps')

    def test_explicit_sessions_group_only_linked_workflow_traces(self):
        session=str(uuid.uuid4());ids=[]
        for i in range(2):
            self.f.tid=str(uuid.uuid4());self.f.aid=str(uuid.uuid4());self.f.sid=str(uuid.uuid4());ids.append(self.f.tid)
            self.assertEqual(self.f.ingest(self.f.event(at=i*3,workflow='document_export',step='processing',session_id=session),
                self.f.event('succeeded',i*3+1,workflow='document_export',step='processing',session_id=session)).status_code,200)
        self.f.tid=str(uuid.uuid4());self.f.aid=str(uuid.uuid4());self.f.sid=str(uuid.uuid4())
        self.assertEqual(self.f.ingest(self.f.event(workflow='document_export',step='processing')).status_code,200)
        path='/api/traces/sessions/search?project=alpha'
        sessions=self.client.post(path,headers=self.admin,json={'environment':'staging'});self.assertEqual(sessions.status_code,200,sessions.text)
        result=sessions.json();self.assertEqual(result['total'],1);self.assertEqual(result['sessions'][0]['trace_count'],2)
        opened=self.client.post('/api/traces/sessions/open?project=alpha',headers=self.admin,json={**result['query'],'session_id':session})
        self.assertEqual(opened.status_code,200,opened.text);self.assertEqual(len(opened.json()['traces']),2)
        searched=self.client.post('/api/traces/search?project=alpha',headers=self.admin,json={'environment':'staging','session_id':session}).json()
        self.assertEqual(searched['total'],2)
        self.assertNotIn('private message',json.dumps(result));self.assertTrue(all(i not in json.dumps(opened.json()) for i in ids))

    def test_session_id_is_opaque_uuid_only_and_not_a_slack_join(self):
        for invalid in ['patient-123','not-a-uuid','00000000-0000-0000-0000-000000000000']:
            response=self.client.post('/api/traces/search?project=alpha',headers=self.admin,json={'session_id':invalid})
            self.assertEqual(response.status_code,422)
        response=self.client.post('/api/traces/search?project=alpha',headers=self.admin,json={'contract':'slack-request-v1','session_id':str(uuid.uuid4())})
        self.assertEqual(response.status_code,422)

    def test_missing_start_failure_and_denied_reads(self):
        self.f.ingest(self.f.event('failed',1,workflow='checkout',step='http',outcome_code='timeout'))
        r=self.search(status='gaps');self.assertEqual(r['total'],0)
        r=self.search(status='failed');self.assertEqual(r['total'],1);self.assertTrue(r['traces'][0]['has_gaps'])
        self.assertIsNone(r['traces'][0]['duration_ms'])
        self.assertEqual(self.search(min_duration_ms=1)['total'],0)
        self.assertEqual(self.client.post('/api/traces/search?project=alpha',json={}).status_code,401)
        key=self.client.post('/api/projects/alpha/read-keys',headers=self.admin,json={'label':'audit','purpose':'audit'}).json()['read_key']
        h={'Authorization':'Bearer '+key}
        self.assertEqual(self.client.post('/api/traces/search?project=alpha',headers=h,json={}).status_code,200)
        self.assertEqual(self.client.post('/api/traces/search?project=beta',headers=h,json={}).status_code,403)
        self.assertEqual(self.client.post('/api/traces/open?project=alpha',headers=h,json={'reference':'trace_'+'a'*32}).status_code,404)
        for query in [{'search':'x'*101},{'limit':61},{'offset':-1},{'environment':'anything'},{'min_duration_ms':-1}]:
            self.assertEqual(self.client.post('/api/traces/search?project=alpha',headers=h,json=query).status_code,422)

class SlackExplorerTests(unittest.TestCase):
    def setUp(self):self.f=AuditTests();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def test_slack_explicit_not_run_search_and_projection_keep_old_checks_unspecified(self):
        for explicit in (False,True):
            ref,attempt=self.f.chain();span=str(uuid.uuid4())
            for offset,state in enumerate(('started','succeeded')):self.f.observe(self.f.trace_facts(ref,attempt,span,'quality',state),4+offset)
            facts={'schema_version':'slack-request-v1','stage':'quality','environment':'production','request_ref':ref,'attempt_ref':attempt,'attempt_number':1,'quality_span_ref':span,
                   'check_version':'growthbook-catalog-v1','checked_claims':0,'mismatched_claims':0,'unknown_claims':0,'reported_outcome':'unknown','answer_disposition':'withheld','check_phase':'before_reply'}
            if explicit:facts.update(check_execution='not_run',check_reason_code='evidence_binding_rejected')
            self.f.observe(facts,6)
        path='/api/traces/search?project='+self.f.p['id']
        all_rows=self.f.owner.post(path,headers=CSRF,json={'environment':'production'}).json()['traces']
        self.assertEqual(len(all_rows),2)
        old=next(r for r in all_rows if 'check_execution' not in r['answer_check'])
        self.assertNotIn('execution',old['checks'][0]);self.assertNotIn('reason_code',old['checks'][0])
        for needle in ['not_run','not run','evidence_binding_rejected','evidence binding rejected']:
            response=self.f.owner.post(path,headers=CSRF,json={'environment':'production','search':needle})
            self.assertEqual(response.status_code,200,response.text);report=response.json();self.assertEqual(report['total'],1)
            check=report['traces'][0]['checks'][0];self.assertEqual(check['result'],'unknown');self.assertEqual(check['execution'],'not_run');self.assertFalse(check['evidence_present'])
            self.assertEqual(report['traces'][0]['answer_check']['check_reason_code'],'evidence_binding_rejected')
            opened=self.f.owner.post('/api/traces/open?project='+self.f.p['id'],headers=CSRF,json={**report['query'],'reference':report['traces'][0]['reference']})
            self.assertEqual(opened.status_code,200);self.assertEqual(opened.json()['trace']['checks'][0],check)

    def test_filter_before_pagination_and_no_private_payloads(self):
        production,_=self.f.chain()
        self.f.chain(environment='staging')
        path='/api/traces/search?project='+self.f.p['id']
        r=self.f.owner.post(path,headers=CSRF,json={'environment':'production','limit':1}).json()
        self.assertEqual(r['total'],1);self.assertEqual(r['traces'][0]['environment'],'production')
        self.assertTrue(r['traces'][0]['has_gaps']);self.assertIsNone(r['traces'][0]['duration_ms'])
        for private in [production,'private chat sentinel','private-feature-sentinel','a'*64]:self.assertNotIn(private,json.dumps(r))
        detail=self.f.owner.post('/api/traces/open?project='+self.f.p['id'],headers=CSRF,json={**r['query'],'reference':r['traces'][0]['reference']})
        self.assertEqual(detail.status_code,200);self.assertEqual(detail.json()['trace']['spans'],[])

    def test_target_environment_survives_early_production_parser_timing(self):
        ref,attempt=self.f.chain(environment='staging')
        self.f.observe(self.f.trace_facts(ref,attempt,str(uuid.uuid4()),'parser','started'),10)
        r=self.f.owner.post('/api/traces/search?project='+self.f.p['id'],headers=CSRF,json={'environment':'staging'}).json()
        self.assertEqual(r['total'],1)
        self.assertIn('timing_environment_mixed',r['traces'][0]['issues'])

    def test_terminal_only_slack_span_has_unknown_start(self):
        ref,attempt=self.f.chain()
        self.f.observe(self.f.trace_facts(ref,attempt,str(uuid.uuid4()),'model','succeeded'),10)
        r=self.f.owner.post('/api/traces/search?project='+self.f.p['id'],headers=CSRF,json={}).json()
        d=self.f.owner.post('/api/traces/open?project='+self.f.p['id'],headers=CSRF,json={**r['query'],'reference':r['traces'][0]['reference']}).json()['trace']
        self.assertIsNone(d['spans'][0]['start_ms']);self.assertTrue(d['has_gaps'])
