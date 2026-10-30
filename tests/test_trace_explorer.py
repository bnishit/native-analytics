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
