import time
import uuid
from datetime import datetime,timezone
import unittest
import test_tracing

class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_tracing.TracingTests();self.fixture.setUp()
        self.client=self.fixture.client;self.admin=self.fixture.admin;self.id=str(uuid.uuid4())
        self.goal={'contract':'workflow-trace-v1','title':'Checked export','workflow':'document_export','environment':'staging','deadline_seconds':30,'traffic_kind':'all','min_sample_size':25,
            'checks':[{'name':'source_facts','version':1,'method':'source','phase':'before_delivery','evidence_required':True}],
            'milestones':['completion','delivery']}
        self.save()
    def tearDown(self):self.fixture.tearDown()
    def save(self,revision=0,goal=None):
        return self.client.put('/api/outcomes/goals/'+self.id+'?project=alpha&revision='+str(revision),headers=self.admin,json=goal or self.goal)
    def check(self,result='passed',**changes):
        return {'name':'source_facts','version':1,'result':result,'required':True,'method':'source','phase':'before_delivery','evidence_ref':str(uuid.uuid4()),**changes}
    def task(self,check=None,attempt=1,disposition='delivered',clock=None,receipts=True,receipt_changes=None,**changes):
        f=self.fixture
        clock=clock if clock is not None else int(f.base*1000)
        common={'workflow':'document_export','request_started_at_ms':clock,'attempt_number':attempt,**changes}
        events=[]
        for step,at,milestone,checks in [('processing',1,'completion',[check or self.check()]),('response',3,'delivery',None)]:
            span=str(uuid.uuid4())
            for checked in checks or []:
                if receipts and checked.get('evidence_ref'):
                    evidence={k:checked[k] for k in ['name','version','result','method','phase']}
                    evidence.update(schema_version='workflow-evidence-v1',receipt_id=checked['evidence_ref'],trace_id=f.tid,attempt_id=f.aid,
                        span_id=span,workflow=common['workflow'],environment=changes.get('environment','staging'),release_sha=changes.get('release_sha'),evidence_digest='a'*64)
                    evidence.update(receipt_changes or {})
                    events.append({'id':str(uuid.uuid4()),'name':'evidence.observed','timestamp':datetime.fromtimestamp(f.base+at+.5,timezone.utc).isoformat(),'properties':{'afterspan_evidence':evidence}})
            events.append(f.event(at=at,span_id=span,step=step,**common))
            events.append(f.event('succeeded',at+1,span_id=span,step=step,milestone=milestone,
                **({'checks':checks} if checks else {'disposition':disposition}),**common))
        r=f.ingest(*events);self.assertEqual(r.status_code,200,r.text)
    def evaluate(self,**query):
        r=self.client.post('/api/outcomes/evaluate?project=alpha',headers=self.admin,json={'goal_id':self.id,'version':1,**query})
        self.assertEqual(r.status_code,200,r.text);return r.json()
    def test_missing_wrong_task_wrong_attempt_and_wrong_check_receipts_do_not_qualify(self):
        self.task(receipts=False)
        self.assertEqual(self.evaluate()['counts']['unclassified'],1)
        for changes in [{'trace_id':str(uuid.uuid4())},{'attempt_id':str(uuid.uuid4())},{'version':2},{'workflow':'other_workflow'},{'result':'failed'}]:
            self.fixture.tid=str(uuid.uuid4());self.fixture.aid=str(uuid.uuid4())
            self.task(receipt_changes=changes)
        r=self.evaluate();self.assertEqual(r['counts']['satisfied'],0);self.assertEqual(r['counts']['unclassified'],6)
    def test_resolved_evidence_has_no_raw_receipt_reference_or_digest_in_reader_result(self):
        import json
        check=self.check();self.task(check)
        r=self.evaluate();self.assertEqual(r['counts']['satisfied'],1)
        opened=self.client.post('/api/traces/open?project=alpha',headers=self.admin,json={**r['trace_query'],'reference':r['tasks'][0]['trace']}).json()
        self.assertEqual(opened['trace']['checks'][0]['evidence_status'],'resolved')
        self.assertNotIn(check['evidence_ref'],json.dumps(opened));self.assertNotIn('a'*64,json.dumps(opened))
        aggregate=self.client.post('/api/query?project=alpha',headers=self.admin,json={'event':'evidence.observed','breakdown':'afterspan_evidence'}).json()
        self.assertEqual(aggregate['total'],0)
        self.assertNotIn('a'*64,json.dumps(aggregate))
    def test_other_project_or_environment_cannot_supply_a_receipt(self):
        import app,json
        self.task(receipts=False)
        with app.connect() as c:row=c.execute("SELECT ts,properties FROM events WHERE name='trace.observed' AND json_type(properties,'$.afterspan_trace.checks')='array'").fetchone()
        facts=json.loads(row['properties'])['afterspan_trace'];check=facts['checks'][0]
        evidence={k:check[k] for k in ['name','version','result','method','phase']}
        evidence.update(schema_version='workflow-evidence-v1',receipt_id=check['evidence_ref'],trace_id=facts['trace_id'],attempt_id=facts['attempt_id'],span_id=facts['span_id'],workflow=facts['workflow'],environment=facts['environment'],evidence_digest='a'*64)
        event={'id':str(uuid.uuid4()),'name':'evidence.observed','timestamp':datetime.fromtimestamp(row['ts']-.1,timezone.utc).isoformat(),'properties':{'afterspan_evidence':evidence}}
        self.assertEqual(self.fixture.ingest(event,headers=self.fixture.fixture.b).status_code,200)
        self.assertEqual(self.evaluate()['counts']['unclassified'],1)
        event['id']=str(uuid.uuid4());event['properties']['afterspan_evidence']['environment']='production'
        self.assertEqual(self.fixture.ingest(event).status_code,200)
        self.assertEqual(self.evaluate()['counts']['unclassified'],1)

    def test_receipt_is_immutable_on_same_event_retry_and_rejects_raw_content(self):
        import app,json
        self.task()
        with app.connect() as c:row=c.execute("SELECT id,ts,properties FROM events WHERE name='evidence.observed'").fetchone()
        event={'id':row['id'],'name':'evidence.observed','timestamp':datetime.fromtimestamp(row['ts'],timezone.utc).isoformat(),'properties':json.loads(row['properties'])}
        event['properties']['afterspan_evidence']['evidence_digest']='b'*64
        self.assertEqual(self.fixture.ingest(event).json()['duplicates'],1)
        self.assertEqual(self.evaluate()['counts']['satisfied'],1)
        event['id']=str(uuid.uuid4());event['properties']['afterspan_evidence']['prompt']='must never be retained'
        self.assertEqual(self.fixture.ingest(event).status_code,422)

    def test_receipt_conflicts_and_late_arrival_respect_frozen_window(self):
        import app,json
        check=self.check();self.task(check);frozen=self.evaluate()
        with app.connect() as c:
            row=c.execute("SELECT ts,properties FROM events WHERE name='evidence.observed'").fetchone()
        evidence=json.loads(row['properties'])['afterspan_evidence'];evidence['evidence_digest']='b'*64
        event={'id':str(uuid.uuid4()),'name':'evidence.observed','timestamp':datetime.fromtimestamp(row['ts'],timezone.utc).isoformat(),'properties':{'afterspan_evidence':evidence}}
        self.assertEqual(self.fixture.ingest(event).status_code,200)
        self.assertEqual(self.evaluate(**frozen['query']),frozen)
        self.assertEqual(self.evaluate()['counts']['unclassified'],1)

    def test_goal_versions_are_immutable_and_retries_idempotent(self):
        self.assertEqual(self.save().json()['version'],1)
        second={**self.goal,'deadline_seconds':60}
        self.assertEqual(self.save(1,second).json()['version'],2)
        self.assertEqual(self.save(0,second).status_code,409)
        self.assertEqual(self.client.get('/api/outcomes/goals/'+self.id+'?project=alpha&version=1',headers=self.admin).json()['definition'],self.goal)
        self.assertEqual(self.client.get('/api/outcomes/goals/'+self.id+'?project=beta',headers=self.admin).status_code,404)
    def test_qualified_success_requires_goal_checks_and_milestones(self):
        self.task();r=self.evaluate()
        self.assertEqual(r['counts']['satisfied'],1);self.assertEqual(r['mature_denominator'],1)
        self.assertEqual(r['satisfied_fraction'],1)
        opened=self.client.post('/api/traces/open?project=alpha',headers=self.admin,json={**r['trace_query'],'reference':r['tasks'][0]['trace']}).json()['trace']
        self.assertEqual(opened['clock'],'request start')
        self.assertAlmostEqual(opened['spans'][0]['start_ms'],1000,delta=1)
        self.assertEqual(self.evaluate(**r['query']),r)
        self.fixture.tid=str(uuid.uuid4());self.task(self.check('unknown'))
        r=self.evaluate();self.assertEqual(r['counts']['unclassified'],1);self.assertEqual(r['satisfied_fraction'],.5)
        self.fixture.tid=str(uuid.uuid4());self.task(self.check('failed'))
        r=self.evaluate();self.assertEqual(r['counts']['failed'],1);self.assertEqual(r['mature_denominator'],3)
    def test_partial_delivery_stays_unclassified_even_when_all_declared_facts_pass(self):
        self.task(disposition='partial')
        result=self.evaluate()
        self.assertEqual(result['counts']['satisfied'],0)
        self.assertEqual(result['counts']['unclassified'],1)
        self.assertEqual(result['mature_denominator'],1)
        self.assertIn('partial_answer_not_completed',result['tasks'][0]['reasons'])

    def test_withheld_and_missing_evidence_never_pass(self):
        self.task(disposition='withheld');self.assertEqual(self.evaluate()['counts']['withheld'],1)
        self.fixture.tid=str(uuid.uuid4());self.task(self.check(evidence_ref=None))
        r=self.evaluate();self.assertEqual(r['counts']['unclassified'],1)
        self.assertEqual(r['satisfied_fraction'],0)
    def test_retries_do_not_restart_clock_and_only_latest_attempt_qualifies(self):
        self.task(self.check('failed'))
        self.fixture.aid=str(uuid.uuid4());self.task(attempt=2)
        r=self.evaluate();self.assertEqual(r['counts']['satisfied'],1);self.assertEqual(r['tasks'][0]['attempts'],2)
        self.assertEqual(r['tasks'][0]['started_at_ms'],int(self.fixture.base*1000))
    def test_waiting_is_not_a_mature_success(self):
        self.fixture.base=time.time()-6
        self.task();r=self.evaluate()
        self.assertEqual(r['counts']['waiting'],1);self.assertEqual(r['mature_denominator'],0);self.assertIsNone(r['satisfied_fraction'])
    def test_wrong_check_version_and_late_success_fail_closed(self):
        self.task(self.check(version=2));self.assertEqual(self.evaluate()['counts']['unclassified'],1)
        self.fixture.tid=str(uuid.uuid4());self.task(clock=int((self.fixture.base-40)*1000))
        self.assertEqual(self.evaluate()['counts']['failed'],1)
    def test_goal_required_check_cannot_be_omitted_or_downgraded_by_producer(self):
        self.task(self.check(name='unrelated_check',required=False))
        r=self.evaluate();self.assertEqual(r['counts']['unclassified'],1)
        self.assertIn('check_missing_or_conflicting:source_facts',r['tasks'][0]['reasons'])
        self.fixture.tid=str(uuid.uuid4());self.task(self.check('failed',required=False))
        self.assertEqual(self.evaluate()['counts']['failed'],1)
    def test_missing_clock_and_release_do_not_qualify_clean_fraction(self):
        self.task(request_started_at_ms=None)
        r=self.evaluate();self.assertEqual(r['coverage']['original_clock_missing'],1);self.assertIsNone(r['satisfied_fraction'])
        r=self.evaluate(release_sha='a'*40);self.assertEqual(r['coverage']['release_uncertain'],1);self.assertIsNone(r['satisfied_fraction'])
    def test_declared_after_delivery_check_cannot_run_before_delivery(self):
        goal={**self.goal,'checks':[{**self.goal['checks'][0],'phase':'after_delivery'}]}
        self.save(1,goal);self.task(self.check(phase='after_delivery'))
        r=self.evaluate(version=2);self.assertEqual(r['counts']['unclassified'],1)
        self.assertIn('check_before_delivery:source_facts',r['tasks'][0]['reasons'])

    def test_read_purpose_keys_are_isolated_and_edit_requires_editor(self):
        key=self.client.post('/api/projects/alpha/read-keys',headers=self.admin,json={'label':'outcomes','purpose':'audit'}).json()['read_key']
        auth={'Authorization':'Bearer '+key}
        self.assertEqual(self.client.get('/api/outcomes/goals?project=alpha',headers=auth).status_code,200)
        self.assertEqual(self.client.post('/api/outcomes/evaluate?project=beta',headers=auth,json={'goal_id':self.id,'version':1}).status_code,403)
        self.assertEqual(self.client.put('/api/outcomes/goals/'+self.id+'?project=alpha&revision=1',headers=auth,json=self.goal).status_code,401)

    def test_write_key_and_unauthenticated_cannot_read_outcomes(self):
        query={'goal_id':self.id,'version':1}
        self.assertEqual(self.client.post('/api/outcomes/evaluate?project=alpha',json=query).status_code,401)
        self.assertEqual(self.client.post('/api/outcomes/evaluate?project=alpha',headers=self.fixture.fixture.a,json=query).status_code,401)
        self.assertEqual(self.client.get('/api/outcomes/goals?project=alpha',headers=self.fixture.fixture.a).status_code,401)

    def test_user_population_excludes_known_checks_and_unknown_labels_block_fraction(self):
        self.save(1,{**self.goal,'traffic_kind':'user','min_sample_size':1}).raise_for_status()
        self.task(traffic_kind='synthetic')
        self.fixture.tid=str(uuid.uuid4());self.fixture.aid=str(uuid.uuid4());self.task(traffic_kind='user')
        r=self.evaluate(version=2);self.assertEqual(r['counts']['satisfied'],1);self.assertEqual(r['coverage']['traffic_excluded'],1);self.assertEqual(r['satisfied_fraction'],1)
        self.fixture.tid=str(uuid.uuid4());self.fixture.aid=str(uuid.uuid4());self.task()
        r=self.evaluate(version=2);self.assertEqual(r['coverage']['traffic_unknown'],1);self.assertIsNone(r['satisfied_fraction'])
    def test_known_original_clock_outside_entry_window_does_not_pollute_traffic_coverage(self):
        self.save(1,{**self.goal,'traffic_kind':'user'}).raise_for_status();self.task()
        self.fixture.tid=str(uuid.uuid4());self.fixture.aid=str(uuid.uuid4());self.task(traffic_kind='user',clock=int((self.fixture.base+1)*1000))
        r=self.evaluate(version=2,start=datetime.fromtimestamp(self.fixture.base+.5,timezone.utc).isoformat(),end=datetime.fromtimestamp(self.fixture.base+10,timezone.utc).isoformat())
        self.assertEqual(r['counts']['satisfied'],1);self.assertEqual(r['coverage']['traffic_unknown'],0);self.assertEqual(r['satisfied_fraction'],1)
    def test_ninety_day_entry_window_respects_actual_receipt_cutoff(self):
        self.task();r=self.evaluate(days=90);self.assertEqual(r['counts']['satisfied'],1)
    def test_model_cost_per_satisfied_task_includes_failed_task_cost_and_unknown_coverage(self):
        def cost(amount):
            sid=str(uuid.uuid4());base={'workflow':'document_export','request_started_at_ms':int(self.fixture.base*1000),'step':'model','span_id':sid}
            usage={'provider':'synthetic','model':'example-v1','cost_microusd':amount,'cost_source':'provider_reported'} if amount is not None else None
            end=self.fixture.event('succeeded',.7,**base,**({'model_usage':usage} if usage else {}))
            self.fixture.ingest(self.fixture.event(at=.5,**base),end).raise_for_status()
        self.task();cost(20)
        self.fixture.tid=str(uuid.uuid4());self.fixture.aid=str(uuid.uuid4());self.task(self.check('failed'));cost(30)
        r=self.evaluate();self.assertEqual(r['model_cost']['measured_microusd'],50);self.assertEqual(r['model_cost']['per_satisfied_task_microusd'],50)
        self.assertEqual(r['counts']['satisfied'],1);self.assertEqual(r['counts']['failed'],1)
        self.fixture.tid=str(uuid.uuid4());self.fixture.aid=str(uuid.uuid4());self.task();cost(None)
        r=self.evaluate();self.assertEqual(r['model_cost']['cost_unknown_spans'],1);self.assertIsNone(r['model_cost']['per_satisfied_task_microusd'])
