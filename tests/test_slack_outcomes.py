"""One immutable Slack stream can satisfy generic goals only through exact observed bindings."""
from contextlib import closing
import json
import time
import unittest
import uuid
import app
import test_audit
from test_workspaces import CSRF

class SlackOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.f=test_audit.AuditTests();self.f.setUp();self.f.t=int(time.time())-3600
        self.offset=0;self.goal_id=str(uuid.uuid4());self.request=str(uuid.uuid4());self.attempt=str(uuid.uuid4());self.quality_span=str(uuid.uuid4());self.reply_span=str(uuid.uuid4());self.total=str(uuid.uuid4());self.delivery=str(uuid.uuid4())
        self.common={'request_ref':self.request,'attempt_ref':self.attempt,'attempt_number':1,'release_ref':self.f.release,'release_sha':'c'*40,
            'environment':'production','request_kind':'read','command_kind':'list_experiments','traffic_kind':'user',
            'comparison_eligible':False,'confirmation_required':False,'tool_required':True,'reply_required':True,'request_started_at_ms':int(self.f.t*1000)}
        self.goal={'contract':'slack-request-v1','title':'Checked Slack answer','workflow':'slack_read_request','environment':'production','traffic_kind':'user','deadline_seconds':10,
            'checks':[{'name':'source_facts','version':1,'method':'source','phase':'before_delivery','evidence_required':True}],'milestones':['completion','delivery']}
        self.f.owner.put('/api/outcomes/goals/'+self.goal_id+'?project='+self.f.p['id']+'&revision=0',headers=CSRF,json=self.goal).raise_for_status()
    def tearDown(self):self.f.tearDown()
    def emit(self,stage,at,**facts):
        return self.f.observe({'schema_version':'slack-request-v1','stage':stage,**self.common,**facts},at+self.offset)
    def span(self,ref,step,start,end,parent=None,**terminal):
        common={'span_ref':ref,'trace_step':step,**({'parent_span_ref':parent} if parent else {}),**({'completion_kind':'processing'} if step=='total' else {})}
        self.emit('trace',start,**common,span_state='started',reported_outcome='unknown')
        self.emit('trace',end,**common,span_state='succeeded',reported_outcome='succeeded',duration_ms=float((end-start)*1000),**terminal)
    def fixture(self,proof=True,quality_changes=None,reply_changes=None,extra_delivery=False,model=False):
        self.span(self.total,'total',0,3)
        self.emit('parser',.1,reported_outcome='succeeded');self.emit('planner',.2,reported_outcome='succeeded')
        self.emit('tool',.3,reported_outcome='succeeded',tool_environments=['production'])
        self.span(self.quality_span,'quality',1,1.6,self.total)
        quality={'reported_outcome':'succeeded','check_version':'source-facts-v1','checked_claims':1,'mismatched_claims':0,'unknown_claims':0,
            'answer_disposition':'verified','check_phase':'before_reply','quality_span_ref':self.quality_span,
            **({'check_evidence_digest':'a'*64,'check_evidence_version':'source-check-v1'} if proof else {}),**(quality_changes or {})}
        self.emit('quality',1.5,**quality)
        self.span(self.reply_span,'reply',2,2.3,self.total);self.span(self.delivery,'slack_delivery',2.1,2.2,self.reply_span)
        self.emit('reply',2.25,**{'reported_outcome':'succeeded','reply_span_ref':self.reply_span,**(reply_changes or {})})
        if extra_delivery:self.span(str(uuid.uuid4()),'slack_delivery',2.11,2.21,self.reply_span)
        if model:self.span(str(uuid.uuid4()),'model',.4,.5,self.total,model_usage={'provider':'example','model':'model-v1','cost_usd_decimal':'0.000000037125','cost_source':'provider_reported'})
    def evaluate(self,**query):
        response=self.f.owner.post('/api/outcomes/evaluate?project='+self.f.p['id'],headers=CSRF,json={'goal_id':self.goal_id,'version':1,**query})
        self.assertEqual(response.status_code,200,response.text);return response.json()
    def test_exact_quality_and_final_delivery_bindings_qualify_one_existing_request(self):
        self.fixture(model=True);r=self.evaluate();self.assertEqual(r['counts']['satisfied'],1,r);self.assertEqual(r['mature_denominator'],1)
        self.assertEqual(r['trace_query']['contract'],'slack-request-v1');self.assertEqual(r['model_cost']['measured_usd'],'0.000000037125')
        opened=self.f.owner.post('/api/traces/open?project='+self.f.p['id'],headers=CSRF,json={**r['trace_query'],'reference':r['tasks'][0]['trace']})
        self.assertEqual(opened.status_code,200,opened.text);trace=opened.json()['trace'];self.assertEqual(trace['traffic_kind'],'user')
        self.assertEqual(trace['checks'][0]['evidence_status'],'resolved');self.assertEqual(trace['checks'][0]['result'],'passed')
        for raw in [self.request,self.attempt,self.quality_span,self.reply_span,'a'*64]:self.assertNotIn(raw,json.dumps(opened.json()))
        with closing(app.connect()) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE name='trace.observed'").fetchone()[0],0)
    def test_old_counts_without_new_bound_proof_stay_unclassified(self):
        self.fixture(proof=False);r=self.evaluate();self.assertEqual(r['counts']['unclassified'],1);self.assertEqual(r['counts']['satisfied'],0)
        self.assertIn('check_evidence_missing:source_facts',r['tasks'][0]['reasons'])
    def test_wrong_quality_pointer_and_wrong_verifier_cannot_borrow_other_steps(self):
        self.fixture(quality_changes={'quality_span_ref':self.reply_span});r=self.evaluate();self.assertEqual(r['counts']['unclassified'],1)
        self.assertIn('check_evidence_missing:source_facts',r['tasks'][0]['reasons'])
    def test_multiple_sends_under_final_reply_are_ambiguous(self):
        self.fixture(extra_delivery=True);r=self.evaluate();self.assertEqual(r['counts']['unclassified'],1)
        self.assertIn('final_delivery_missing_or_ambiguous',r['tasks'][0]['reasons'])
    def test_interim_accepted_send_does_not_replace_an_unbound_final_reply(self):
        self.fixture(reply_changes={'reply_span_ref':str(uuid.uuid4())});self.span(str(uuid.uuid4()),'slack_delivery',.6,.7,self.total)
        r=self.evaluate();self.assertEqual(r['counts']['unclassified'],1);self.assertIn('final_reply_operation_unbound',r['tasks'][0]['reasons'])
    def test_source_mismatch_or_unknown_stays_failed_or_withheld(self):
        self.fixture(quality_changes={'reported_outcome':'failed','mismatched_claims':1,'answer_disposition':'withheld'})
        r=self.evaluate();self.assertEqual(r['counts']['failed'],1);self.assertEqual(r['counts']['satisfied'],0)
    def test_synthetic_and_unknown_classifications_do_not_become_user_tasks(self):
        self.common['traffic_kind']='synthetic';self.fixture();r=self.evaluate();self.assertEqual(r['mature_denominator'],0);self.assertEqual(r['coverage']['traffic_excluded'],1)
    def test_historical_missing_kind_cannot_establish_user_population(self):
        del self.common['traffic_kind'];self.fixture();r=self.evaluate();self.assertEqual(r['coverage']['traffic_unknown'],1);self.assertIsNone(r['satisfied_fraction'])
    def test_retry_keeps_clock_and_uses_only_the_latest_attempt_quality(self):
        self.fixture(quality_changes={'reported_outcome':'failed','mismatched_claims':1,'answer_disposition':'withheld'})
        self.common.update(attempt_ref=str(uuid.uuid4()),attempt_number=2);self.quality_span=str(uuid.uuid4());self.reply_span=str(uuid.uuid4());self.total=str(uuid.uuid4());self.delivery=str(uuid.uuid4())
        self.offset=4;self.fixture();r=self.evaluate();self.assertEqual(r['mature_denominator'],1);self.assertEqual(r['counts']['satisfied'],1);self.assertEqual(r['tasks'][0]['attempts'],2)
    def test_trace_only_retry_cannot_reuse_prior_attempt_source_and_delivery(self):
        self.fixture()
        self.common.update(attempt_ref=str(uuid.uuid4()),attempt_number=2)
        self.span(str(uuid.uuid4()),'model',2.5,2.7)
        r=self.evaluate()
        self.assertEqual(r['counts']['satisfied'],0)
        self.assertEqual(r['counts']['unclassified'],1)
        self.assertEqual(r['tasks'][0]['attempts'],2)
        self.assertIn('required_stage_unobserved:parser',r['tasks'][0]['reasons'])

    def test_trace_only_conflicting_attempt_identity_cannot_qualify(self):
        self.fixture()
        self.common.update(attempt_ref=str(uuid.uuid4()))
        self.span(str(uuid.uuid4()),'model',.4,.5)
        r=self.evaluate()
        self.assertEqual(r['counts']['satisfied'],0)
        self.assertIn('attempt_slot_conflict',r['tasks'][0]['reasons'])

    def test_wrong_release_binding_cannot_qualify(self):
        self.fixture(quality_changes={'release_sha':'d'*40});r=self.evaluate();self.assertEqual(r['counts']['unclassified'],1)
        self.assertIsNone(self.evaluate(release_sha='c'*40)['satisfied_fraction'])

    def test_a_different_checker_version_does_not_satisfy_the_goal(self):
        self.fixture(quality_changes={'check_version':'growthbook-catalog-v1'});r=self.evaluate()
        self.assertEqual(r['counts']['unclassified'],1);self.assertIn('check_missing_or_conflicting:source_facts',r['tasks'][0]['reasons'])
    def test_catalog_scope_version_is_explicit_and_cannot_relabel_historical_checks(self):
        self.goal['checks']=[{'name':'growthbook_catalog','version':2,'method':'source','phase':'before_delivery','evidence_required':True}]
        self.goal_id=str(uuid.uuid4())
        self.f.owner.put('/api/outcomes/goals/'+self.goal_id+'?project='+self.f.p['id']+'&revision=0',headers=CSRF,json=self.goal).raise_for_status()
        self.fixture(quality_changes={'check_version':'growthbook-catalog-v2','checked_claims':16})
        r=self.evaluate();self.assertEqual(r['counts']['satisfied'],1,r)
        trace=self.f.owner.post('/api/traces/open?project='+self.f.p['id'],headers=CSRF,json={**r['trace_query'],'reference':r['tasks'][0]['trace']}).json()['trace']
        self.assertEqual(trace['checks'][0]['version'],2)
        with closing(app.connect()) as c,c:
            c.execute("UPDATE events SET properties=json_set(properties,'$.afterspan_audit.check_version','growthbook-catalog-v1','$.afterspan_audit.checked_claims',1) WHERE project=? AND json_extract(properties,'$.afterspan_audit.stage')='quality'",(self.f.p['id'],))
        self.assertEqual(self.evaluate()['counts']['satisfied'],0)

    def test_partial_catalog_delivery_never_satisfies_a_complete_scope_goal(self):
        self.goal['checks']=[{'name':'growthbook_catalog','version':2,'method':'source','phase':'before_delivery','evidence_required':True}]
        self.goal_id=str(uuid.uuid4())
        self.f.owner.put('/api/outcomes/goals/'+self.goal_id+'?project='+self.f.p['id']+'&revision=0',headers=CSRF,json=self.goal).raise_for_status()
        self.fixture(quality_changes={'check_version':'growthbook-catalog-v2','checked_claims':15,'unknown_claims':1,'reported_outcome':'unknown','answer_disposition':'partial'})
        r=self.evaluate();self.assertEqual(r['counts']['satisfied'],0,r);self.assertEqual(r['counts']['unclassified'],1,r)
        self.assertIn('partial_answer_not_completed',r['tasks'][0]['reasons'])

    def test_retry_does_not_restart_the_original_task_deadline(self):
        self.offset=11;self.fixture();r=self.evaluate();self.assertEqual(r['counts']['failed'],1)
        self.assertIn('check_late:source_facts',r['tasks'][0]['reasons'])
    def test_parent_cycle_cannot_qualify_even_when_checks_and_sends_close(self):
        self.fixture()
        with closing(app.connect()) as c,c:
            for span,parent in [(self.quality_span,self.reply_span),(self.reply_span,self.quality_span)]:
                c.execute("UPDATE events SET properties=json_set(properties,'$.afterspan_audit.parent_span_ref',?) WHERE project=? AND json_extract(properties,'$.afterspan_audit.span_ref')=?",(parent,self.f.p['id'],span))
        r=self.evaluate();self.assertEqual(r['counts']['unclassified'],1);self.assertIn('parent_cycle',r['tasks'][0]['reasons'])
