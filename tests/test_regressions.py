import copy
from datetime import datetime,timezone
import json
import time
import unittest
import uuid
import test_analytics
from regressions import reviewed_json,matches

class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.f=test_analytics.AnalyticsTests();self.f.setUp();self.client=self.f.client;self.admin=self.f.admin
        self.case_id=str(uuid.uuid4());self.dataset_id=str(uuid.uuid4())
        self.case={'privacy_reviewed':True,'title':'Production list facts','input':{'request':'list experiments in production'},'expected':{'count':2,'complete':True}}
    def tearDown(self):self.f.tearDown()
    def put(self,kind,id,body,revision=0,project='alpha',headers=None):
        return self.client.put(f'/api/experiments/{kind}/{id}?project={project}&revision={revision}',headers=headers or self.admin,json=body)
    def get(self,kind,id,version=1,project='alpha'):
        return self.client.get(f'/api/experiments/{kind}/{id}?project={project}&version={version}',headers=self.admin)
    def setup_dataset(self,extra=False):
        r=self.put('cases',self.case_id,self.case);self.assertEqual(r.status_code,200,r.text)
        self.case_refs=[{'id':self.case_id,'version':1}]
        if extra:
            second=str(uuid.uuid4());r=self.put('cases',second,{**self.case,'title':'Empty list','expected':{'count':0,'complete':True}});self.assertEqual(r.status_code,200,r.text)
            self.case_refs.append({'id':second,'version':1})
        self.dataset={'title':'Read-only source facts','cases':self.case_refs}
        r=self.put('datasets',self.dataset_id,self.dataset);self.assertEqual(r.status_code,200,r.text)
        return r.json()
    def make_run(self,outputs,**changes):
        body={'dataset':self.dataset_id,'dataset_version':1,'variant':'baseline','release_sha':'a'*40,'evaluator_runner_sha256':'b'*64,'privacy_reviewed':True,
              'results':[{**ref,'output':output,'duration_ms':10} for ref,output in zip(self.case_refs,outputs)]}
        body.update(changes);id=str(uuid.uuid4());r=self.put('runs',id,body);self.assertEqual(r.status_code,200,r.text)
        return id,body,r.json()
    def compare(self,baseline,candidate):return self.client.post('/api/experiments/compare?project=alpha',headers=self.admin,json={'baseline':baseline,'candidate':candidate})
    def test_case_versions_are_immutable_and_dataset_pins_exact_bytes(self):
        original=self.setup_dataset();same=self.put('cases',self.case_id,self.case)
        self.assertEqual(same.json()['version'],1)
        changed=self.put('cases',self.case_id,{**self.case,'expected':{'count':3}})
        self.assertEqual(changed.status_code,409)
        second=self.put('cases',self.case_id,{**self.case,'expected':{'count':3}},revision=1)
        self.assertEqual(second.status_code,200)
        self.assertEqual(original['definition']['cases'][0]['sha256'],self.get('cases',self.case_id).json()['sha256'])
        id,body,report=self.make_run([{'count':2,'complete':True}])
        self.assertEqual(report['counts']['passed'],1)
        self.assertEqual(report['cases'][0]['version'],1)
        self.assertEqual(self.get('cases',self.case_id,2).json()['definition']['expected'],{'count':3})
    def test_mismatched_candidate_fails_and_improvement_remains_review_only(self):
        self.setup_dataset(extra=True)
        baseline,_,old=self.make_run([{'count':9,'complete':True},{'count':0,'complete':True}])
        candidate,_,new=self.make_run([{'count':2,'complete':True},{'count':0,'complete':True}],variant='candidate',release_sha='c'*40)
        r=self.compare(baseline,candidate);self.assertEqual(r.status_code,200,r.text);report=r.json()
        self.assertEqual(report['changes'],{'improved':1,'regressed':0,'unchanged':1,'unknown':0})
        self.assertTrue(report['eligible_for_review']);self.assertFalse(report['deployment_authorized'])
        self.assertEqual(report['baseline_pass_rate'],.5);self.assertEqual(report['candidate_pass_rate'],1)
        changed=next(c for c in report['cases'] if c['change']=='improved')
        self.assertEqual(changed['baseline_differences'],[{'path':'$.count','expected':'2','observed':'9'}])
        self.assertEqual(changed['differences'],[])
    def test_regressions_and_missing_results_cannot_clear_review(self):
        self.setup_dataset(extra=True)
        baseline,_,_=self.make_run([{'count':2,'complete':True},{'count':0,'complete':True}])
        candidate,_,_=self.make_run([{'count':2,'complete':True},{'count':99,'complete':True}],variant='candidate')
        r=self.compare(baseline,candidate).json();self.assertEqual(r['changes']['regressed'],1);self.assertFalse(r['eligible_for_review'])
        missing,_,report=self.make_run([{'count':2,'complete':True}],variant='incomplete')
        self.assertEqual(report['counts']['missing'],1);self.assertFalse(report['complete']);self.assertEqual(report['pass_rate'],.5)
        r=self.compare(baseline,missing).json();self.assertEqual(r['changes']['unknown'],1);self.assertFalse(r['eligible_for_review'])
    def test_run_identity_cannot_overwrite_and_versions_must_match(self):
        self.setup_dataset();id,body,report=self.make_run([{'count':2,'complete':True}])
        self.assertEqual(self.put('runs',id,body).json(),report)
        changed=copy.deepcopy(body);changed['results'][0]['output']['count']=3
        self.assertEqual(self.put('runs',id,changed).status_code,409)
        changed=copy.deepcopy(body);changed['results'][0]['version']=2
        self.assertEqual(self.put('runs',str(uuid.uuid4()),changed).status_code,422)
        candidate,_,_=self.make_run([{'count':2,'complete':True}],evaluator_runner_sha256='d'*64)
        self.assertEqual(self.compare(id,candidate).status_code,409)
        self.assertEqual(self.compare(id,id).status_code,422)
    def test_error_is_a_counted_result_and_typed_facts_are_not_coerced(self):
        self.setup_dataset();id,_,report=self.make_run([],results=[{**self.case_refs[0],'error_code':'timeout','duration_ms':20}])
        self.assertEqual(report['counts']['error'],1);self.assertTrue(report['complete']);self.assertEqual(report['pass_rate'],0)
        id,_,report=self.make_run([{'count':2,'complete':1}]);self.assertEqual(report['counts']['failed'],1)
        self.assertFalse(matches({'count':True},{'count':1}))
        self.assertTrue(matches({'count':2},{'count':2,'extra':False},True))
        self.assertFalse(matches({'count':2},{'count':2,'extra':False}))
    def test_sensitive_fixtures_are_rejected_without_reflecting_secrets(self):
        for unsafe in [{'email':'private@example.com'},{'request':'mail private@example.com'},{'request':'Bearer extremely-private-value'},{'request':'call +91 98765 43210'},{'password':'private-value'}]:
            response=self.put('cases',str(uuid.uuid4()),{**self.case,'input':unsafe})
            self.assertEqual(response.status_code,422,response.text);self.assertNotIn('private',response.text.lower())
        for reviewed in [False,1,'true']:
            response=self.put('cases',str(uuid.uuid4()),{**self.case,'privacy_reviewed':reviewed})
            self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.put('cases',str(uuid.uuid4()),{**self.case,'evaluator':'json_subset','expected':{}}).status_code,422)
        self.setup_dataset()
        id,body,_=self.make_run([{'count':2,'complete':True}]);body['results'][0]['output']={'request':'Bearer private-output'}
        response=self.put('runs',str(uuid.uuid4()),body);self.assertEqual(response.status_code,422)
        self.assertNotIn('private-output',response.text)
    def test_project_isolation_and_narrow_readers_cannot_get_fixtures(self):
        self.setup_dataset()
        self.assertEqual(self.get('cases',self.case_id,project='beta').status_code,404)
        self.assertEqual(self.put('cases',str(uuid.uuid4()),self.case,project='missing-project').status_code,404)
        self.assertEqual(self.put('datasets',str(uuid.uuid4()),self.dataset,project='beta').status_code,404)
        for headers in [{},self.f.a]:
            response=self.client.get('/api/experiments/cases?project=alpha',headers=headers)
            self.assertIn(response.status_code,[401,403])
        for purpose in ['audit','analytics']:
            key=self.client.post('/api/projects/alpha/read-keys',headers=self.admin,json={'label':'fixture denial','purpose':purpose})
            self.assertEqual(key.status_code,200,key.text)
            response=self.client.get('/api/experiments/cases?project=alpha',headers={'Authorization':'Bearer '+key.json()['read_key']})
            self.assertIn(response.status_code,[401,403])
    def test_expiry_cannot_be_renewed_by_retry_and_prune_removes_payload(self):
        self.setup_dataset();id,body,_=self.make_run([{'count':2,'complete':True}])
        with test_analytics.analytics.connect() as c,c:
            c.execute('UPDATE regression_objects SET expires=? WHERE kind=? AND id=?',(time.time()-1,'case',self.case_id))
        self.assertEqual(self.get('cases',self.case_id).status_code,410)
        self.assertEqual(self.put('cases',self.case_id,self.case).status_code,410)
        self.assertEqual(self.get('runs',id).status_code,410)
        test_analytics.analytics.prune()
        self.assertEqual(self.get('cases',self.case_id).status_code,404)
        with test_analytics.analytics.connect() as c:self.assertEqual(c.execute("SELECT COUNT(*) FROM regression_objects WHERE kind='case'").fetchone()[0],0)
    def test_failed_trace_is_bound_to_case_without_copying_request_content(self):
        tid=str(uuid.uuid4());sid=str(uuid.uuid4());at=time.time()-10
        facts={'schema_version':'workflow-trace-v1','trace_id':tid,'attempt_id':str(uuid.uuid4()),'attempt_number':1,'span_id':sid,'workflow':'document_export','environment':'test','step':'model','state':'failed','outcome_code':'internal','duration_ms':5}
        self.f.ingest([{'id':str(uuid.uuid4()),'name':'trace.observed','timestamp':datetime.fromtimestamp(at,timezone.utc).isoformat(),'properties':{'afterspan_trace':facts}}]).raise_for_status()
        search=self.client.post('/api/traces/search?project=alpha',headers=self.admin,json={'environment':'test'}).json()
        source={**search['query'],'reference':search['traces'][0]['reference']}
        response=self.put('cases',self.case_id,{**self.case,'source':source});self.assertEqual(response.status_code,200,response.text)
        saved=response.json();self.assertEqual(saved['definition']['source']['reference'],source['reference'])
        self.assertEqual(saved['definition']['source']['as_of'],source['as_of'])
        self.assertNotIn(tid,json.dumps(saved));self.assertNotIn(sid,json.dumps(saved))
        self.assertEqual(self.put('cases',self.case_id,{**self.case,'source':source}).json()['version'],1)
        self.assertEqual(self.put('cases',str(uuid.uuid4()),{**self.case,'source':source},project='beta').status_code,404)
    def test_local_runner_executes_real_callbacks_then_server_compares_fixed_cases(self):
        from sdk.python.afterspan_experiments import prepare_run
        dataset=self.setup_dataset(extra=True)
        def load(id,version):return self.get('cases',id,version).json()
        runs=[];seen=[]
        for variant in ['baseline','candidate']:
            def execute(input):
                seen.append(input)
                self.assertNotIn('expected',input)
                return {'count':2 if len(seen)%2 else 0,'complete':variant=='candidate'}
            body=prepare_run(dataset,load,execute,variant=variant,release_sha='a'*40,privacy_reviewed=True)
            id=str(uuid.uuid4());response=self.put('runs',id,body);self.assertEqual(response.status_code,200,response.text);runs.append(id)
        compared=self.compare(*runs).json();self.assertTrue(compared['eligible_for_review']);self.assertEqual(compared['changes']['improved'],2)
        self.assertEqual(len(seen),4)
        self.assertNotIn('complete',self.get('cases',self.case_id).json()['definition']['input'])
    def test_runner_blocks_unsafe_output_and_rejects_changed_pins_before_execution(self):
        from sdk.python.afterspan_experiments import prepare_run
        dataset=self.setup_dataset();seen=[]
        def load(id,version):return self.get('cases',id,version).json()
        def unsafe(input):seen.append('executed');return {'answer':'Bearer private-output'}
        body=prepare_run(dataset,load,unsafe,variant='candidate',release_sha='a'*40,privacy_reviewed=True)
        self.assertEqual(body['results'][0]['error_code'],'unsupported');self.assertNotIn('private-output',json.dumps(body))
        def broken(input):raise RuntimeError('private exception text')
        body=prepare_run(dataset,load,broken,variant='candidate',release_sha='a'*40,privacy_reviewed=True)
        self.assertEqual(body['results'][0]['error_code'],'internal');self.assertNotIn('private exception',json.dumps(body))
        dataset['definition']['cases'][0]['sha256']='f'*64
        with self.assertRaises(ValueError):prepare_run(dataset,load,unsafe,variant='candidate',release_sha='a'*40,privacy_reviewed=True)
        self.assertEqual(seen,['executed'])
        with self.assertRaises(ValueError):prepare_run(dataset,load,unsafe,variant='candidate',release_sha='a'*40)

    def test_duplicate_cases_bad_hash_and_versioned_dataset_comparison(self):
        self.setup_dataset()
        self.assertEqual(self.put('datasets',str(uuid.uuid4()),{**self.dataset,'cases':self.case_refs*2}).status_code,422)
        baseline,_,_=self.make_run([{'count':2,'complete':True}])
        second=self.put('datasets',self.dataset_id,{**self.dataset,'minimum_pass_rate':.9},revision=1)
        self.assertEqual(second.status_code,200)
        candidate,_,_=self.make_run([{'count':2,'complete':True}],dataset_version=2)
        self.assertEqual(self.compare(baseline,candidate).status_code,409)
        with test_analytics.analytics.connect() as c,c:c.execute("UPDATE regression_objects SET sha256=? WHERE kind='case'",('0'*64,))
        self.assertEqual(self.get('runs',baseline).status_code,409)
