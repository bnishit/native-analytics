import json
import subprocess
import sys
import tempfile
from pathlib import Path
import time
import unittest
import uuid
from contextlib import closing
from fastapi.testclient import TestClient
import app
from audit import nearest
import test_product as fixtures
from test_workspaces import CSRF

class AuditTests(unittest.TestCase):
    user=fixtures.ProductTests.user;token=fixtures.ProductTests.token;workspace=fixtures.ProductTests.workspace;project=fixtures.ProductTests.project;join=fixtures.ProductTests.join;invite=fixtures.ProductTests.invite;key=fixtures.ProductTests.key;send=fixtures.ProductTests.send
    def setUp(self):fixtures.ProductTests.setUp(self);self.release=str(uuid.uuid4())
    def tearDown(self):fixtures.ProductTests.tearDown(self)
    def facts(self,stage,ref,attempt,**kw):
        return {'schema_version':'slack-request-v1','stage':stage,'environment':'production','request_ref':ref,'attempt_ref':attempt,'attempt_number':1,'release_ref':self.release,'request_kind':'read','comparison_eligible':True,'independent_proposal':True,'confirmation_required':False,'tool_required':False,'comparison_version':'canonical-plan-v1','context_digest':'a'*64,'decision_digest':'b'*64,'duration_ms':10.0,**kw}
    def observe(self,facts,at=0):return self.send('stage.observed',None,at,properties={'afterspan_audit':facts,'messagePreview':'private chat sentinel','featureId':'private-feature-sentinel','email':'private-person@example.test','ip':'203.0.113.99'})
    def chain(self,**kw):
        ref=str(uuid.uuid4());attempt=str(uuid.uuid4())
        for i,stage in enumerate(['parser','planner','shadow']):self.observe(self.facts(stage,ref,attempt,**kw),i)
        return ref,attempt
    def review(self,body=None,client=None,path='review'):
        r=(client or self.owner).post('/api/audit/'+path+'?project='+self.p['id'],headers=CSRF,json=body or {});self.assertEqual(r.status_code,200,r.text);return r.json()
    def trace_facts(self,ref,attempt,span,step,state,**kw):
        return {'schema_version':'slack-request-v1','stage':'trace','environment':'production',
                'request_ref':ref,'attempt_ref':attempt,'attempt_number':1,'request_started_at_ms':0,
                'span_ref':span,'trace_step':step,'span_state':state,
                **({} if state=='started' else {'duration_ms':10.0}),**kw}

    def test_trace_repeats_failures_missing_terminals_and_private_refs(self):
        ref,attempt=self.chain();spans=[str(uuid.uuid4()) for _ in range(6)]
        for number,(step,states,kind) in enumerate([
            ('feature_list',['started','succeeded'],None),('feature_list',['started','failed'],None),
            ('readback',['started'],None),('webhook_ack',['started','succeeded'],None),
            ('total',['started','succeeded'],'processing'),('slack_delivery',['started','failed'],None)]):
            for offset,state in enumerate(states):
                self.observe(self.trace_facts(ref,attempt,spans[number],step,state,
                             **({'completion_kind':kind} if kind else {})),10+number*2+offset)
        report=self.review();row=report['requests'][0];trace=row['trace']
        self.assertEqual(report['agreement']['denominator'],1)
        self.assertEqual([s['state'] for s in trace['spans']],['succeeded','failed','unclosed','succeeded','succeeded','failed'])
        self.assertEqual(trace['completion']['webhook_ack']['state'],'succeeded')
        self.assertEqual(trace['completion']['processing']['state'],'incomplete')
        self.assertEqual(trace['completion']['delivery']['state'],'failed')
        self.assertIn('policy',trace['missing_steps'])
        frozen=self.review({**report['query'],'request':row['request']},path='trace')
        for private in [ref,attempt,*spans,'private chat sentinel','a'*64,'b'*64]:self.assertNotIn(private,json.dumps(frozen))
        self.assertEqual(self.review(path='catalog')['contract'],'slack-request-v1')

    def test_trace_old_requests_missing_start_parent_and_business_retry(self):
        ref,attempt=self.chain();self.assertEqual(self.review()['requests'][0]['trace']['availability'],'unavailable')
        span,parent=str(uuid.uuid4()),str(uuid.uuid4())
        self.observe(self.trace_facts(ref,attempt,span,'mutation','failed',parent_span_ref=parent),10)
        next_attempt=str(uuid.uuid4());total=str(uuid.uuid4())
        for offset,state in enumerate(['started','succeeded']):
            self.observe(self.trace_facts(ref,next_attempt,total,'total',state,attempt_number=2,completion_kind='processing'),20+offset)
        trace=self.review()['requests'][0]['trace']
        self.assertEqual(trace['spans'][0]['state'],'missing_start');self.assertTrue(trace['spans'][0]['parent_missing'])
        self.assertEqual(trace['completion']['processing']['attempt_number'],2)
        self.assertEqual(trace['completion']['delivery']['state'],'missing')

    def test_trace_conflicts_do_not_change_comparison_or_authoritative_environment(self):
        ref,attempt=self.chain(environment='staging');span=str(uuid.uuid4())
        for offset,state in enumerate(['started','succeeded','failed']):
            self.observe(self.trace_facts(ref,attempt,span,'feature_lookup',state),10+offset)
        report=self.review({'expected_environment':'staging'});row=report['requests'][0]
        self.assertEqual(row['environment'],'staging');self.assertFalse(row['environment_mismatch'])
        self.assertEqual(report['agreement']['denominator'],1)
        self.assertEqual(row['trace']['spans'][0]['state'],'conflict')
        self.assertIsNone(row['trace']['spans'][0]['duration_ms'])
        self.assertEqual(self.client.post('/api/audit/trace?project='+self.p['id'],json={**report['query'],'request':row['request']}).status_code,401)

    def test_producer_shaped_spans_and_multiple_slack_sends(self):
        # Literal JSON shape matches the TypeScript producer: total processing, transport has no completion kind.
        ref,attempt=self.chain()
        payload=json.loads('{"schema_version":"slack-request-v1","stage":"trace","environment":"production","attempt_number":1,"request_started_at_ms":0,"trace_step":"total","span_state":"started","completion_kind":"processing"}')
        payload.update(request_ref=ref,attempt_ref=attempt,span_ref=str(uuid.uuid4()))
        self.observe(payload,10)
        self.observe({**payload,'span_state':'succeeded','duration_ms':20},11)
        for number in range(2):
            span=str(uuid.uuid4())
            for offset,state in enumerate(['started','succeeded']):self.observe(self.trace_facts(ref,attempt,span,'slack_delivery',state),20+number*2+offset)
        trace=self.review()['requests'][0]['trace']
        self.assertEqual(trace['completion']['processing']['state'],'incomplete')
        self.assertEqual(trace['completion']['delivery']['state'],'conflict')
        self.assertEqual(len([s for s in trace['spans'] if s['step']=='slack_delivery']),2)

    def test_trace_only_default_environment_does_not_enter_production_latency(self):
        self.observe(self.trace_facts(str(uuid.uuid4()),str(uuid.uuid4()),str(uuid.uuid4()),'webhook_ack','started'))
        report=self.review();self.assertEqual(report['requests'][0]['environment'],'unknown')
        self.assertEqual(report['latency_scope']['selected_requests'],0)
        self.assertEqual(report['agreement']['denominator'],0)

    def test_trace_rejects_contradictory_outcomes_without_requiring_historical_outcome(self):
        from audit import Observation
        for state,expected in [('started','unknown'),('succeeded','succeeded'),('failed','failed')]:
            facts=self.trace_facts(str(uuid.uuid4()),str(uuid.uuid4()),str(uuid.uuid4()),'slack_delivery',state)
            Observation.model_validate(facts)
            Observation.model_validate({**facts,'reported_outcome':expected})
            for outcome in {'unknown','succeeded','failed','clarification','cancelled'}-{expected}:
                with self.subTest(state=state,outcome=outcome),self.assertRaises(ValueError):
                    Observation.model_validate({**facts,'reported_outcome':outcome})
        response=self.client.post('/v1/events',headers={'Authorization':'Bearer '+self.p['write_key']},json={'events':[{'name':'trace','properties':{'afterspan_audit':{**facts,'span_state':'succeeded','reported_outcome':'failed'}}}]})
        self.assertEqual(response.status_code,422)

    def test_confirmation_processing_uses_latest_phase_and_never_old_completion(self):
        ref,attempt=self.chain();clock=int(self.t*1000)
        def span(step,start,end=None,**extra):
            identity=str(uuid.uuid4())
            self.observe(self.trace_facts(ref,attempt,identity,step,'started',request_started_at_ms=clock,**extra),start)
            if end is not None:self.observe(self.trace_facts(ref,attempt,identity,step,'succeeded',request_started_at_ms=clock,duration_ms=(end-start)*1000,**extra),end)
        span('total',.01,.33,completion_kind='processing')
        span('total',.50,.60,completion_kind='processing')
        span('mutation',.52,.535);span('readback',.54,.549)
        trace=self.review()['requests'][0]['trace']
        self.assertEqual(len([s for s in trace['spans'] if s['step']=='total']),2)
        self.assertEqual(trace['completion']['processing']['state'],'succeeded')
        self.assertAlmostEqual(trace['completion']['processing']['duration_ms'],100)
        self.assertAlmostEqual(trace['completion']['processing']['finished_at_ms_from_request_start'],600,delta=1)
        span('mutation',.70,.80)
        summary=self.review()['requests'][0]['trace']['completion']['processing']
        self.assertEqual(summary['state'],'incomplete');self.assertIsNone(summary['duration_ms'])
        span('total',.65,None,completion_kind='processing')
        self.assertEqual(self.review()['requests'][0]['trace']['completion']['processing']['state'],'unclosed')
        span('total',.65,.81,completion_kind='processing')
        self.assertEqual(self.review()['requests'][0]['trace']['completion']['processing']['state'],'conflict')

    def test_trace_strict_contract_rejects_private_fields_and_wrong_state_shapes(self):
        from audit import Observation
        facts=self.trace_facts(str(uuid.uuid4()),str(uuid.uuid4()),str(uuid.uuid4()),'policy','started')
        for change in [{'message':'private'}, {'user_id':'private'}, {'error':'private'}, {'span_ref':str(uuid.uuid1())},
                       {'request_ref':None},{'attempt_ref':'[redacted]'},{'trace_step':'private'}, {'span_state':'unknown'},
                       {'duration_ms':5.0},{'completion_kind':'processing'}, {'request_started_at_ms':True}]:
            with self.assertRaises(ValueError):Observation.model_validate({**facts,**change})
        terminal={**facts,'span_state':'failed'}
        with self.assertRaises(ValueError):Observation.model_validate(terminal)
        with self.assertRaises(ValueError):Observation.model_validate({**facts,'stage':'parser'})
        response=self.client.post('/v1/events',headers={'Authorization':'Bearer '+self.p['write_key']},json={'events':[{'name':'trace','properties':{'afterspan_audit':{**facts,'error':'private'}}}]})
        self.assertEqual(response.status_code,422)

    def test_misleading_183_events_two_writes_zero_comparisons(self):
        for i in range(177):self.send('http.request',None,i)
        for i in range(2):
            ref,attempt=str(uuid.uuid4()),str(uuid.uuid4())
            for j,stage in enumerate(['parser','planner','tool']):self.observe(self.facts(stage,ref,attempt,request_kind='write',comparison_eligible=False,confirmation_required=False,tool_required=True),200+i*10+j)
        r=self.review();self.assertEqual(r['coverage']['stored_events_in_window'],183);self.assertEqual(r['denominators']['selected_requests'],2)
        self.assertEqual(r['agreement']['denominator'],0);self.assertIsNone(r['agreement']['rate']);self.assertEqual(r['readiness'],'inconclusive');self.assertEqual(r['business_safety'],'not_established')
        self.assertEqual(r['stage_counts']['shadow']['not_required'],2);self.assertEqual(r['coverage']['events_without_contract'],177)
    def test_disagreement_unsafe_input_mismatch_and_required_unknown(self):
        self.chain()
        ref,attempt=str(uuid.uuid4()),str(uuid.uuid4())
        for i,stage in enumerate(['parser','planner','shadow']):self.observe(self.facts(stage,ref,attempt,decision_digest=('c'*64 if stage=='shadow' else 'b'*64),proposal_safe=False if stage=='shadow' else None,confirmation_required=True,tool_required=None),10+i)
        ref,attempt=str(uuid.uuid4()),str(uuid.uuid4())
        for i,stage in enumerate(['parser','planner','shadow']):self.observe(self.facts(stage,ref,attempt,context_digest='c'*64 if stage=='shadow' else 'a'*64),20+i)
        r=self.review();self.assertEqual(r['agreement']['denominator'],2);self.assertEqual(r['agreement']['numerator'],1);self.assertEqual(r['counts']['unsafe_proposals_reported'],1);self.assertEqual(r['counts']['planner_input_mismatches'],1)
        self.assertEqual(r['stage_counts']['confirmation']['missing'],1);self.assertEqual(r['stage_counts']['tool']['unknown'],1);self.assertEqual(r['verdict'],'needs_attention')
    def test_missing_null_redacted_never_join_and_catalog_preserves_false(self):
        ref,attempt=str(uuid.uuid4()),str(uuid.uuid4())
        a=self.facts('parser',ref,attempt);a.pop('request_ref');self.observe(a)
        self.observe(self.facts('parser',None,attempt),1);self.observe(self.facts('planner','[redacted]',attempt),2)
        self.chain(comparison_eligible=False,confirmation_required=False,proposal_safe=False)
        r=self.review();self.assertEqual(r['coverage']['uncorrelated_observations'],{'missing':1,'null':1,'redacted':1});self.assertEqual(r['denominators']['selected_requests'],1)
        catalog=self.review(path='catalog');fields={f['property']:f for f in catalog['fields']};self.assertEqual(fields['request_ref']['missing'],1);self.assertEqual(fields['request_ref']['null'],1);self.assertEqual(fields['request_ref']['redacted'],1);self.assertEqual(fields['proposal_safe']['types']['bool'],3)
        self.assertNotIn(ref,json.dumps(catalog));self.assertNotIn('private chat sentinel',json.dumps(catalog));self.assertFalse(catalog['raw_value_examples'])
    def test_retries_deliveries_ambiguous_stages_and_release_boundaries(self):
        ref,attempt=self.chain();e=self.observe(self.facts('tool',ref,attempt,tool_required=True),3)
        # A changed fact on only one stage conflicts; it cannot create a clean comparison.
        response=self.client.post('/v1/events',headers={'Authorization':'Bearer '+self.p['write_key']},json={'events':[e]});self.assertEqual(response.json()['duplicates'],1)
        self.assertEqual(self.review()['agreement']['denominator'],0)
        second=str(uuid.uuid4())
        for i,stage in enumerate(['parser','planner','shadow']):self.observe(self.facts(stage,ref,second,attempt_number=2),10+i)
        r=self.review();self.assertEqual(r['denominators']['selected_requests'],1);self.assertEqual(r['counts']['repeated_business_attempts'],1);self.assertEqual(r['agreement']['denominator'],1)
        self.observe(self.facts('planner',ref,second,attempt_number=2),20);self.assertEqual(self.review()['agreement']['denominator'],0)
        other,other_attempt=str(uuid.uuid4()),str(uuid.uuid4())
        for i,stage in enumerate(['parser','planner','shadow']):self.observe(self.facts(stage,other,other_attempt,release_ref=str(uuid.uuid4()) if stage=='shadow' else self.release),30+i)
        self.assertEqual(self.review()['agreement']['denominator'],0)
    def test_bounded_selection_across_100_event_page_and_frozen_trace_privacy(self):
        for i in range(65):
            ref,attempt=str(uuid.uuid4()),str(uuid.uuid4())
            for j,stage in enumerate(['parser','planner','shadow']):self.observe(self.facts(stage,ref,attempt,duration_ms=float(i)),i*10+j)
        r=self.review();self.assertEqual(r['denominators']['observed_request_chains_in_window'],65);self.assertEqual(r['denominators']['selected_requests'],60);self.assertEqual(r['agreement']['denominator'],60);self.assertEqual(r['coverage']['omitted_request_chains'],5)
        trace=self.review({**r['query'],'request':r['requests'][0]['request']},path='trace');self.assertEqual(trace['fingerprint'],r['fingerprint']);self.assertEqual(len(trace['request']['stages']),7)
        self.chain();again=self.review(r['query']);self.assertEqual(again['denominators'],r['denominators']);self.assertEqual(again['fingerprint'],r['fingerprint'])
        payload=json.dumps(r)
        for forbidden in ['private chat sentinel','private-feature-sentinel','private-person@example.test','203.0.113.99','a'*64,'b'*64,self.release]:self.assertNotIn(forbidden,payload)
        self.assertEqual(nearest(list(range(1,21)),.95),19);self.assertEqual(nearest([0,100],.5),0)
    def test_audit_key_cannot_query_general_properties_and_is_project_scoped(self):
        self.chain();r=self.owner.post('/api/projects/'+self.p['id']+'/read-keys',headers=CSRF,json={'label':'Audit only','purpose':'audit','expires_days':1});self.assertEqual(r.status_code,200,r.text)
        c=TestClient(app.app);c.headers.update({'Authorization':'Bearer '+r.json()['read_key']});self.assertEqual(self.review(client=c)['agreement']['denominator'],1)
        for path in ['/api/overview','/api/charts','/api/segments','/api/funnels','/api/setup']:self.assertEqual(c.get(path+'?project='+self.p['id']).status_code,403)
        self.assertEqual(c.post('/api/query?project='+self.p['id'],json={'breakdown':'email'}).status_code,403)
        self.assertEqual(c.get('/api/events?project='+self.p['id']).status_code,401)
        self.assertEqual(c.post('/api/audit/review?project=alpha',json={}).status_code,403)
        self.assertEqual(self.client.post('/api/audit/review?project='+self.p['id'],json={}).status_code,401)
    def test_contract_rejects_free_text_and_bad_types_and_marks_uncovered_history(self):
        facts=self.facts('parser',str(uuid.uuid4()),str(uuid.uuid4()))
        for field,value in [('comparison_eligible','false'),('request_ref','same-redacted-value'),('diagnostic_code','customer free text'),('duration_ms',True)]:
            bad={**facts,field:value};r=self.client.post('/v1/events',headers={'Authorization':'Bearer '+self.p['write_key']},json={'events':[{'name':'stage.observed','properties':{'afterspan_audit':bad}}]});self.assertEqual(r.status_code,422,r.text)
        self.send('slack_parser_outcome',None,properties={'messageHash':'[redacted]','finalAction':'add_users'});r=self.review();self.assertEqual(r['coverage']['events_without_contract'],1);self.assertEqual(r['denominators']['selected_requests'],0)
    def test_mixed_environment_and_missing_attempt_never_create_clean_agreement(self):
        ref,attempt=str(uuid.uuid4()),str(uuid.uuid4())
        for i,stage in enumerate(['parser','planner','shadow']):self.observe(self.facts(stage,ref,attempt,environment='staging' if stage=='shadow' else 'production'),i)
        self.assertEqual(self.review()['agreement']['denominator'],0);self.assertEqual(self.review()['counts']['environment_mistakes'],1);self.assertEqual(self.review()['latency_scope']['selected_requests'],0);self.assertEqual(self.review()['latency_scope']['excluded_environment_requests'],1)
        f=self.facts('parser',str(uuid.uuid4()),None);self.observe(f,10);r=self.review();request=next(x for x in r['requests'] if x['latest_attempt_number'] is None);self.assertTrue(all(s['state']=='unknown' for s in request['stages']))

    def test_a_shadow_stage_does_not_prove_independent_generation(self):
        self.chain(independent_proposal=False)
        r=self.review();self.assertEqual(r['agreement']['denominator'],0)
        self.assertIn('independent_proposal_not_confirmed',r['requests'][0]['issues'])
        self.chain(comparison_eligible=None,diagnostic_code='comparison_scope_unavailable')
        self.assertTrue(any('comparison_scope_unavailable' in x['issues'] for x in self.review()['requests']));self.assertEqual(self.review()['counts']['requests_with_unavailable_comparison_scope'],1)
        self.chain(independent_proposal=None)
        ref,attempt=str(uuid.uuid4()),str(uuid.uuid4())
        for i,stage in enumerate(['parser','planner','shadow']):
            facts=self.facts(stage,ref,attempt);facts.pop('independent_proposal');self.observe(facts,20+i)
        r=self.review();self.assertEqual(r['agreement']['denominator'],0);self.assertEqual(r['semantic_correctness'],'not_established')

    def test_audit_launcher_rejects_shared_or_wrong_purpose_receipts(self):
        launcher=Path(__file__).resolve().parents[1]/'agent/run_audit.py'
        with tempfile.TemporaryDirectory() as folder:
            receipt=Path(folder)/'audit.json';receipt.write_text('{"purpose":"audit"}');receipt.chmod(0o644)
            r=subprocess.run([sys.executable,str(launcher),str(receipt)],capture_output=True,text=True)
            self.assertNotEqual(r.returncode,0);self.assertIn('must be private',r.stderr)
            receipt.chmod(0o600);receipt.write_text('{"purpose":"analytics"}')
            r=subprocess.run([sys.executable,str(launcher),str(receipt)],capture_output=True,text=True)
            self.assertNotEqual(r.returncode,0);self.assertIn('audit-purpose',r.stderr)

    def goal(self):
        return {'schema':'afterspan-audit-goal-v1','id':'read-reply-delivery','question':'Did Slack accept the reply?',
                'metric':'read-reply-delivery-v1','environment':'production','tracking':{'recipe':'tracker-read-reply-v1',
                'path':'src/app/api/slack/route.ts','dependencies':['src/app/api/slack/route.ts','src/lib/analytics/request-audit.ts']}}

    def quality_goal(self,verifier='growthbook-catalog-v1'):
        return {'schema':'afterspan-audit-goal-v1','id':'answer-quality','question':'Do displayed catalog facts match the source?',
                'metric':'answer-quality-v1','environment':'production','verifier':verifier,'source_of_truth':'GrowthBook','criteria':'Displayed catalog facts match raw source rows','safe_test':'One approved private read',
                'tracking':{'recipe':'source-answer-check-v1','dependencies':['src/reply.ts']}}

    def quality_chain(self,outcome='succeeded',**counts):
        ref,attempt=str(uuid.uuid4()),str(uuid.uuid4())
        for i,stage in enumerate(['parser','planner','reply','quality']):
            extra={'check_version':'growthbook-catalog-v1','checked_claims':1,'mismatched_claims':int(outcome=='failed'),'unknown_claims':int(outcome=='unknown'),**counts} if stage=='quality' else {}
            self.observe(self.facts(stage,ref,attempt,comparison_eligible=None,diagnostic_code='comparison_scope_unavailable',
                         reply_required=True,reported_outcome=outcome if stage=='quality' else 'succeeded',**extra),i)
        return ref,attempt

    def test_source_check_is_distinct_from_delivery_and_preserves_missing_checks(self):
        self.quality_chain()
        query={'goal':self.quality_goal()};r=self.review(query,path='evaluate')
        self.assertEqual((r['passed'],r['unknown'],r['rate']),(1,0,1))
        self.assertEqual(r['verification'],'producer_source_check_not_independently_fetched_by_afterspan')
        self.assertEqual(self.review({'goal':self.goal()},path='evaluate')['delivered'],1)
        self.chain(comparison_eligible=False)
        older=self.review(query,path='evaluate');self.assertEqual(older['unknown'],1);self.assertIsNone(older['rate'])
        self.assertEqual(self.review({'goal':self.quality_goal('unconfigured')},path='evaluate')['passed'],0)

    def test_source_mismatch_and_changed_source_never_become_clean_pass(self):
        self.quality_chain('failed');bad=self.review({'goal':self.quality_goal()},path='evaluate')
        self.assertEqual((bad['passed'],bad['failed'],bad['rate']),(0,1,0))
        self.quality_chain('unknown');changed=self.review({'goal':self.quality_goal()},path='evaluate')
        self.assertEqual(changed['unknown'],1);self.assertIsNone(changed['rate'])
        capped=self.review({'goal':self.quality_goal(),'limit':1},path='evaluate');self.assertFalse(capped['selection_complete']);self.assertIsNone(capped['rate'])

    def test_quality_needs_delivery_and_conflicting_reports_never_pass(self):
        ref,attempt=self.quality_chain();self.observe(self.facts('reply',ref,attempt,reply_required=True,reported_outcome='failed'),10)
        result=self.review({'goal':self.quality_goal()},path='evaluate');self.assertEqual(result['unknown'],1);self.assertIsNone(result['rate'])
        query={'goal':self.quality_goal()};self.assertEqual(self.client.post('/api/audit/evaluate?project='+self.p['id'],json=query).status_code,401)
        query['goal']['tracking']['dependencies']=['../private'];self.assertEqual(self.owner.post('/api/audit/onboarding?project='+self.p['id'],headers=CSRF,json=query).status_code,422)

    def test_quality_contract_rejects_false_success_and_source_contents(self):
        from audit import Observation
        from pydantic import ValidationError
        facts=self.facts('quality',str(uuid.uuid4()),str(uuid.uuid4()),check_version='growthbook-catalog-v1',checked_claims=1,mismatched_claims=1,unknown_claims=0,reported_outcome='succeeded')
        with self.assertRaises(ValidationError):Observation.model_validate(facts)
        for extra in [{'checked_claims':True},{'source_response':'private contents'},{'checked_claims':16}]:
            with self.assertRaises(ValidationError):Observation.model_validate({**facts,**extra})

    def test_new_repo_setup_reports_unconfigured_and_observed_coverage(self):
        query={'goal':self.quality_goal('unconfigured')};setup=self.review(query,path='onboarding')
        self.assertFalse(setup['verifier_declared']);self.assertEqual(setup['next_action'],'configure_source_check');self.assertEqual(setup['observed_reads'],0)
        self.quality_chain();ready=self.review({'goal':self.quality_goal()},path='onboarding')
        self.assertTrue(ready['verifier_declared']);self.assertEqual(ready['passed'],1);self.assertIn('setup',ready)

    def test_reply_goal_empty_unknown_failed_delivered_and_release_scope(self):
        query={'goal':self.goal()}
        empty=self.review(query,path='evaluate');self.assertEqual(empty['denominator'],0);self.assertIsNone(empty['rate'])
        self.chain(comparison_eligible=False)
        older=self.review(query,path='evaluate');self.assertEqual(older['unknown'],1);self.assertIsNone(older['rate'])
        for n,outcome in enumerate(['succeeded','failed']):
            ref,attempt=str(uuid.uuid4()),str(uuid.uuid4())
            for i,stage in enumerate(['parser','planner','reply']):
                self.observe(self.facts(stage,ref,attempt,comparison_eligible=False,reply_required=True,
                             reported_outcome=outcome if stage=='reply' else 'succeeded'),20+n*10+i)
        result=self.review(query,path='evaluate')
        self.assertEqual((result['denominator'],result['delivered'],result['failed'],result['unknown']),(3,1,1,1));self.assertIsNone(result['rate'])
        release=self.review()['requests'][0]['release_coverage'][0]
        self.assertEqual(self.review({**query,'release':release},path='evaluate')['denominator'],3)
        self.assertEqual(self.review({**query,'release':'rel_'+'0'*32},path='evaluate')['denominator'],0)
        self.assertEqual(self.owner.post('/api/audit/evaluate?project='+self.p['id'],headers=CSRF,json={**query,'expected_environment':'staging'}).status_code,422)
        self.assertEqual(self.client.post('/api/audit/evaluate?project='+self.p['id'],json=query).status_code,401)

    def test_reply_goal_requires_complete_selection_and_preserves_false_and_conflict(self):
        for n in range(2):
            ref,attempt=str(uuid.uuid4()),str(uuid.uuid4())
            for i,stage in enumerate(['parser','planner','reply']):
                self.observe(self.facts(stage,ref,attempt,comparison_eligible=False,reply_required=True,reported_outcome='succeeded'),n*10+i)
        query={'goal':self.goal()}
        good=self.review(query,path='evaluate');self.assertEqual(good['rate'],1);self.assertEqual(good['denominator'],2)
        capped=self.review({**query,'limit':1},path='evaluate');self.assertFalse(capped['selection_complete']);self.assertIsNone(capped['rate'])
        self.observe(self.facts('reply',ref,attempt,comparison_eligible=False,reply_required=False,reported_outcome='succeeded'),30)
        conflict=self.review(query,path='evaluate');self.assertEqual(conflict['unknown'],1);self.assertIsNone(conflict['rate'])

    def test_reply_alone_and_uncorrelated_diagnostics_cannot_prove_complete_delivery(self):
        ref,attempt=str(uuid.uuid4()),str(uuid.uuid4())
        self.observe(self.facts('reply',ref,attempt,comparison_eligible=False,reply_required=True,reported_outcome='succeeded'),2)
        query={'goal':self.goal()}
        missing=self.review(query,path='evaluate');self.assertEqual(missing['unknown'],1);self.assertIsNone(missing['rate'])
        for i,stage in enumerate(['parser','planner']):
            self.observe(self.facts(stage,ref,attempt,comparison_eligible=False,reply_required=True,reported_outcome='succeeded'),i)
        self.assertEqual(self.review(query,path='evaluate')['rate'],1)
        self.observe(self.facts('parser',None,str(uuid.uuid4()),comparison_eligible=False),5)
        gap=self.review(query,path='evaluate');self.assertFalse(gap['selection_complete']);self.assertIsNone(gap['rate'])

    def test_ambiguous_request_kind_cannot_disappear_from_goal_coverage(self):
        ref,attempt=str(uuid.uuid4()),str(uuid.uuid4())
        for i,stage in enumerate(['parser','planner','reply']):
            self.observe(self.facts(stage,ref,attempt,comparison_eligible=False,reply_required=True,reported_outcome='succeeded'),i)
        self.assertEqual(self.review({'goal':self.goal()},path='evaluate')['rate'],1)
        other=str(uuid.uuid4());attempt=str(uuid.uuid4())
        self.observe(self.facts('parser',other,attempt,request_kind='read',comparison_eligible=False),10)
        self.observe(self.facts('planner',other,attempt,request_kind='write',comparison_eligible=False),11)
        result=self.review({'goal':self.goal()},path='evaluate')
        self.assertEqual(result['coverage']['uncertain_request_classifications'],1);self.assertFalse(result['selection_complete']);self.assertIsNone(result['rate'])
