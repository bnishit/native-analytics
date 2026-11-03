import json
import time
import unittest
import uuid
from datetime import datetime,timezone
from unittest.mock import patch
from contextlib import closing
import app
import test_analytics
from collection_health import HealthQuery

def stamp(value):return datetime.fromtimestamp(value,timezone.utc).isoformat()

class CollectionHealthTests(unittest.TestCase):
    def setUp(self):
        self.f=test_analytics.AnalyticsTests();self.f.setUp();self.client=self.f.client;self.admin=self.f.admin
        self.now=time.time();self.instance=str(uuid.uuid4());self.source={'source':'export_worker','environment':'staging','workflow':'document_export','interval_seconds':60}
        self.query={'start':stamp(self.now-300),'end':stamp(self.now-60),'as_of':stamp(self.now),'environment':'staging','workflow':'document_export'}
    def tearDown(self):self.f.tearDown()
    def save(self,definition=None,revision=0,headers=None):
        return self.client.put('/api/collection/sources?project=alpha&revision='+str(revision),headers=headers or self.admin,json=definition or self.source)
    def pulse(self,at,sequence=1,**changes):
        at_ms=int(at*1000)
        facts={'schema_version':'collection-health-v1',**self.source,'instance_id':self.instance,'sequence':sequence,
            'observed_at_ms':at_ms,'process_started_at_ms':int((self.now-1000)*1000),'delivery_mode':'durable','queued':0,'capacity':100,
            'dropped':0,'rejected':0,'delivery_failures':0,'evaluator_failures':0,'error_code':None,**changes}
        return {'id':str(uuid.uuid4()),'name':'analytics.pipeline','timestamp':stamp(at_ms/1000),'properties':{'afterspan_health':facts}}
    def report(self,**changes):
        response=self.client.post('/api/collection/health?project=alpha',headers=self.admin,json={**self.query,'as_of':stamp(time.time()),**changes})
        self.assertEqual(response.status_code,200,response.text);return response.json()
    def drain(self,collector):
        import io
        class Transport:
            def open(inner,request,timeout):
                response=self.f.ingest(json.loads(request.data)['events']);self.assertEqual(response.status_code,200,response.text)
                body=io.BytesIO(response.content);body.status=200;return body
        with patch('urllib.request.build_opener',return_value=Transport()):return collector.flush()
    def configured(self,**changes):
        with patch('time.time',return_value=self.now-1000):self.save().raise_for_status()
        events=[self.pulse(self.now-v,i,**changes) for i,v in enumerate([310,250,190,130,70])]
        with patch('time.time',return_value=self.now-5):self.f.ingest(events).raise_for_status()
    def test_missing_and_unknown_sources_are_not_healthy_or_complete(self):
        r=self.report();self.assertIn('no_enabled_source_expectations',r['reasons']);self.assertEqual(r['collection_completeness'],'not_established')
        self.save().raise_for_status();r=self.report();self.assertIn('pulse_missing',r['sources'][0]['reasons'])
        self.f.ingest([self.pulse(self.now-100,source='undeclared')]).raise_for_status()
        r=self.report();self.assertIn('source_not_declared',next(s for s in r['sources'] if s['source']=='undeclared')['reasons'])
    def test_observed_source_preserves_limits_and_frozen_window(self):
        self.configured();r=self.report();self.assertEqual(r['status'],'observed',r);self.assertEqual(r['sources'][0]['status'],'observed')
        self.assertEqual(r['collection_completeness'],'not_established');self.assertEqual(r['sources'][0]['instances'][0]['window_counter_changes']['dropped'],0)
        repeated=self.report(**r['query']);self.assertEqual(repeated['fingerprint'],r['fingerprint'])
        aggregate=self.client.post('/api/query?project=alpha',headers=self.admin,json={'event':'analytics.pipeline'}).json();self.assertEqual(aggregate['total'],0)
    def test_stale_drops_rejections_delay_evaluator_unknown_and_counter_reset_are_explicit(self):
        self.configured();self.f.ingest([self.pulse(self.now-65,8,queued=100,oldest_queued_at_ms=int((self.now-800)*1000),dropped=3,rejected=1,evaluator_failures=None,error_code='queue_full')]).raise_for_status()
        r=self.report();issues=r['sources'][0]['reasons']
        for issue in ['queue_full','queue_delayed','collector_reports_window_loss','evaluator_health_unknown','collector_reports_error']:self.assertIn(issue,issues)
        self.f.ingest([self.pulse(self.now-62,9,dropped=1)]).raise_for_status()
        self.assertIn('counter_regressed',self.report()['sources'][0]['reasons'])
        with patch('time.time',return_value=self.now+250):
            self.assertIn('pulse_stale',self.report(end=stamp(self.now+250),as_of=stamp(self.now+250))['sources'][0]['reasons'])
    def test_native_backend_rejections_are_separate_from_producer_counters(self):
        self.configured()
        with patch('time.time',return_value=self.now-180):
            invalid=self.f.ingest([{'name':'invalid event'}]);self.assertEqual(invalid.status_code,422)
        r=self.report();native=next(x for x in r['backend'] if x['transport']=='native')
        self.assertEqual(native['rejected_requests'],1);self.assertIn('project_backend_reported_rejections',r['reasons'])
        self.assertEqual(r['sources'][0]['instances'][0]['latest']['rejected'],0)
    def test_sources_require_editor_revision_and_strict_metadata_contract(self):
        r=self.save();self.assertEqual(r.status_code,200);self.assertEqual(self.save().json()['revision'],1)
        self.assertEqual(self.save({**self.source,'interval_seconds':30}).status_code,409)
        self.assertEqual(self.save(headers=self.f.a).status_code,401)
        for change in [{'dropped':True},{'error_code':'Bearer SECRET'},{'queued':1},{'sequence':-1},{'observed_at_ms':int((self.now+1)*1000)},{'private_text':'do not retain'}]:
            self.assertEqual(self.f.ingest([self.pulse(self.now-100,**change)]).status_code,422,change)
        event=self.pulse(self.now-100);event['distinct_id']='someone';self.assertEqual(self.f.ingest([event]).status_code,422)
    def test_exact_start_pulse_is_one_baseline_not_a_sequence_conflict(self):
        self.configured()
        r=self.report(start=stamp(int((self.now-250)*1000)/1000))
        self.assertEqual(r['status'],'observed',r['reasons'])
        source=r['sources'][0]
        self.assertNotIn('pulse_sequence_conflict',source['reasons'])
        self.assertEqual(source['instances'][0]['window_counter_changes']['dropped'],0)

    def test_project_environment_workflow_and_time_scopes_do_not_mix(self):
        self.configured()
        self.assertEqual(self.report(environment='production')['sources'],[])
        self.assertEqual(self.report(workflow='other_work')['sources'],[])
        self.assertEqual(self.client.post('/api/collection/health?project=alpha',headers=self.f.a,json=self.query).status_code,401)
        read=self.client.post('/api/projects/alpha/read-keys',headers=self.admin,json={'label':'bounded reader','expires_days':1,'purpose':'audit'}).json()
        self.assertEqual(self.client.post('/api/collection/health?project=beta',headers={'Authorization':'Bearer '+read['read_key']},json=self.query).status_code,403)
        r=self.client.post('/api/collection/health?project=alpha',json=self.query);self.assertEqual(r.status_code,401)
    def test_replayed_event_does_not_duplicate_pulse_but_conflicting_sequence_is_visible(self):
        self.configured();event=self.pulse(self.now-65,10)
        self.f.ingest([event]).raise_for_status();r=self.f.ingest([event]).json();self.assertEqual(r['duplicates'],1)
        self.assertNotIn('pulse_sequence_conflict',self.report()['sources'][0]['reasons'])
        self.f.ingest([self.pulse(self.now-63,10)]).raise_for_status();self.assertIn('pulse_sequence_conflict',self.report()['sources'][0]['reasons'])
    def test_legacy_pulses_remain_unknown_and_changes_after_window_start_are_visible(self):
        self.configured();self.f.ingest([{'id':str(uuid.uuid4()),'name':'analytics.pipeline','timestamp':stamp(self.now-100),'properties':{'queued':0,'dropped':0,'last_error':'private provider body'}}]).raise_for_status()
        r=self.report();self.assertEqual(r['legacy_pulses'],1);self.assertNotIn('private provider body',json.dumps(r));self.assertIn('untyped_pipeline_reports',r['reasons'])
        self.save({**self.source,'interval_seconds':30},1).raise_for_status();self.assertIn('expectation_changed_after_window_start',self.report()['sources'][0]['reasons'])
    def test_python_pulse_uses_real_collector_counters_and_survives_restart(self):
        from sdk.python.native_analytics import Collector
        c=Collector('http://127.0.0.1:8789','fake-local-key',self.f.temp.name+'/collector.db',source='export_worker',environment='staging',workflow='document_export')
        c.record_evaluation(failed=True);c.pulse()
        with closing(c.db()) as db:event=json.loads(db.execute('SELECT payload FROM outbox').fetchone()[0])
        response=self.f.ingest([event]);self.assertEqual(response.status_code,200,response.text)
        facts=event['properties']['afterspan_health'];self.assertEqual(facts['evaluator_failures'],1);self.assertEqual(facts['delivery_mode'],'durable')
        c2=Collector('http://127.0.0.1:8789','fake-local-key',c.path,source='export_worker',environment='staging',workflow='document_export')
        self.assertEqual(c2.status()['queued'],1);self.assertEqual(c2.status()['evaluator_failures'],1);self.assertNotEqual(c2.instance_id,c.instance_id)
        with self.assertRaises(ValueError):c2.pulse(state='stopped')
        # Drain through the actual backend instead of clearing the persisted queue.
        self.assertEqual(self.drain(c2),1)
        c2.close();c2.pulse(state='stopped')
        with closing(c2.db()) as db:final=json.loads(db.execute('SELECT payload FROM outbox').fetchone()[0])
        self.assertEqual(final['properties']['afterspan_health']['collector_state'],'stopped')
        self.assertEqual(final['properties']['afterspan_health']['queued'],0)
        self.assertEqual(self.f.ingest([final]).status_code,200)

    def test_actual_node_pulse_roundtrips_through_shared_backend_contract(self):
        import subprocess
        from pathlib import Path
        code="""import {createAnalytics} from './sdk/typescript/native-analytics.ts';let events=[];const c=createAnalytics({url:'https://analytics.example',writeKey:'fake-local-key',health:{source:'export_worker',environment:'staging',workflow:'document_export'},fetch:async(_,o)=>{events=JSON.parse(o.body).events;return new Response(JSON.stringify({accepted:events.length,duplicates:0}))}});c.recordEvaluation(false);await c.pulse();console.log(JSON.stringify(events));"""
        result=subprocess.run(['node','--experimental-strip-types','--input-type=module','-e',code],cwd=Path(__file__).parents[1],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr);events=json.loads(result.stdout)
        response=self.f.ingest(events);self.assertEqual(response.status_code,200,response.text)
        pulse=events[0]['properties']['afterspan_health'];self.assertEqual(pulse['delivery_mode'],'memory');self.assertEqual(pulse['evaluator_failures'],0)

    def test_aggregate_overview_has_counts_not_typed_source_or_error_content(self):
        self.configured();self.f.ingest([self.pulse(self.now-50,10,queued=1,oldest_queued_at_ms=int((self.now-100)*1000),error_code='delivery_failed')]).raise_for_status()
        response=self.client.get('/api/overview?project=alpha',headers=self.admin);self.assertEqual(response.status_code,200)
        pipeline=response.json()['pipeline'];self.assertEqual(pipeline['queued'],1);self.assertLess(pipeline['observed_at'],time.time()-45)
        for key in ['afterspan_health','instance_id','source','workflow','error_code']:self.assertNotIn(key,pipeline)

    def test_stable_historical_loss_does_not_permanently_block_later_windows(self):
        self.configured(dropped=3,rejected=2,evaluator_failures=1)
        r=self.report();self.assertEqual(r['status'],'observed',r)
        state=r['sources'][0]['instances'][0];self.assertEqual(state['latest']['dropped'],3)
        self.assertEqual(state['window_counter_changes']['dropped'],0);self.assertEqual(state['window_counter_changes']['evaluator_failures'],0)

    def test_explicit_empty_stop_survives_a_healthy_worker_handover(self):
        self.configured()
        # A new worker covers the whole window while the old worker explicitly ends.
        replacement=str(uuid.uuid4())
        self.f.ingest([self.pulse(self.now-v,i,instance_id=replacement) for i,v in enumerate([310,250,190,130,70])]).raise_for_status()
        stopped=self.pulse(self.now-65,8,collector_state='stopped')
        response=self.f.ingest([stopped]);self.assertEqual(response.status_code,200,response.text)
        query={**self.query,'end':stamp(self.now+180),'as_of':stamp(self.now+185)}
        with patch('time.time',return_value=self.now+184):
            self.f.ingest([self.pulse(self.now+v,10+i,instance_id=replacement) for i,v in enumerate([0,60,120,180])]).raise_for_status()
        with patch('time.time',return_value=self.now+185):
            r=self.report(**query)
        self.assertEqual(r['status'],'observed',r)
        old=next(i for i in r['sources'][0]['instances'] if i['instance_id']==self.instance)
        self.assertEqual(old['lifecycle'],'reported_stopped');self.assertNotIn('pulse_stale',old['issues'])
        self.assertEqual(r['collection_completeness'],'not_established')
        # A silent old worker still has no stop evidence and stays stale.
        stopped['id']=str(uuid.uuid4());stopped['properties']['afterspan_health'].pop('collector_state')
        self.f.ingest([stopped]).raise_for_status()
        with patch('time.time',return_value=self.now+185):
            self.assertIn('pulse_stale',self.report(**query)['sources'][0]['reasons'])

    def test_stop_cannot_hide_pending_work_unknown_checks_or_later_activity(self):
        self.configured()
        for changes in [{'queued':1,'oldest_queued_at_ms':int((self.now-100)*1000)},{'error_code':'delivery_failed'}]:
            self.assertEqual(self.f.ingest([self.pulse(self.now-65,8,collector_state='stopped',**changes)]).status_code,422)
        self.f.ingest([self.pulse(self.now-65,8,collector_state='stopped',evaluator_failures=None)]).raise_for_status()
        r=self.report();self.assertIn('evaluator_health_unknown',r['sources'][0]['reasons'])
        # Replaying the exact terminal is deduplicated; a new report contradicts it.
        self.f.ingest([self.pulse(self.now-62,9)]).raise_for_status()
        r=self.report();self.assertIn('collector_lifecycle_conflict',r['sources'][0]['reasons'])
        self.assertEqual(r['sources'][0]['instances'][0]['lifecycle'],'conflicting')

    def test_retiring_the_only_worker_does_not_clear_source_coverage(self):
        self.configured();self.f.ingest([self.pulse(self.now-65,8,collector_state='stopped')]).raise_for_status()
        with patch('time.time',return_value=self.now+185):
            r=self.report(end=stamp(self.now+180),as_of=stamp(self.now+180))
        self.assertIn('pulse_coverage_gap',r['sources'][0]['reasons'])

    def test_stop_conflicts_with_higher_sequence_even_before_the_window_baseline(self):
        with patch('time.time',return_value=self.now-1000):self.save().raise_for_status()
        replacement=str(uuid.uuid4())
        events=[self.pulse(self.now-v,i,instance_id=replacement) for i,v in enumerate([310,250,190,130,70])]
        events+=[self.pulse(self.now-500,1),self.pulse(self.now-310,8,collector_state='stopped')]
        self.f.ingest(events).raise_for_status();self.assertEqual(self.report()['status'],'observed')
        # Sequence says this activity followed the stop, even though its clock regressed.
        self.f.ingest([self.pulse(self.now-320,9)]).raise_for_status()
        r=self.report();self.assertIn('collector_lifecycle_conflict',r['sources'][0]['reasons'])
        old=next(i for i in r['sources'][0]['instances'] if i['instance_id']==self.instance)
        self.assertEqual(old['lifecycle'],'conflicting');self.assertIn('pulse_stale',old['issues'])

    def demand_source(self):
        self.source['reporting_mode']='on_demand'
        self.query.update(start=stamp(self.now-1800),end=stamp(self.now-60))
        with patch('time.time',return_value=self.now-3000):
            response=self.save();self.assertEqual(response.status_code,200,response.text)

    def demand_run(self,age,*,instance=None,**end_changes):
        instance=instance or str(uuid.uuid4());started=int((self.now-age-1)*1000)
        return [self.pulse(self.now-age,1,instance_id=instance,process_started_at_ms=started,collector_state='running'),
                self.pulse(self.now-age+10,2,instance_id=instance,process_started_at_ms=started,collector_state='stopped',**end_changes)]

    def test_on_demand_closed_invocations_do_not_promise_idle_heartbeats(self):
        self.demand_source()
        self.assertIn('no_invocations_observed',self.report()['sources'][0]['reasons'])
        self.f.ingest(self.demand_run(1500)+self.demand_run(200)).raise_for_status()
        r=self.report();self.assertEqual(r['status'],'observed',r)
        self.assertEqual(len(r['sources'][0]['instances']),2)
        self.assertTrue(all(i['window_counter_changes']['dropped']==0 for i in r['sources'][0]['instances']))
        self.assertEqual(r['collection_completeness'],'not_established')
        control={**self.source,'source':'continuous_control','workflow':'continuous_export','reporting_mode':'continuous'}
        with patch('time.time',return_value=self.now-3000):self.save(control).raise_for_status()
        events=self.demand_run(1500)+self.demand_run(200)
        for event in events:event['properties']['afterspan_health'].update(source=control['source'],workflow=control['workflow'],reporting_mode='continuous')
        self.f.ingest(events).raise_for_status()
        self.assertIn('pulse_coverage_gap',self.report(workflow=control['workflow'])['sources'][0]['reasons'])

    def test_on_demand_unfinished_missing_start_and_active_gaps_stay_visible(self):
        self.demand_source();events=self.demand_run(1500)
        self.f.ingest([events[0]]).raise_for_status()
        self.assertIn('invocation_stop_missing',self.report()['sources'][0]['reasons'])
        events[1]['properties']['afterspan_health']['sequence']=3
        self.f.ingest([events[1]]).raise_for_status()
        self.assertIn('invocation_sequence_gap',self.report()['sources'][0]['reasons'])
        missing=self.demand_run(200)[1];self.f.ingest([missing]).raise_for_status()
        self.assertIn('invocation_start_missing',self.report()['sources'][0]['reasons'])
        long=self.demand_run(900);long[1]=self.pulse(self.now-200,2,instance_id=long[0]['properties']['afterspan_health']['instance_id'],process_started_at_ms=long[0]['properties']['afterspan_health']['process_started_at_ms'],collector_state='stopped')
        self.f.ingest(long).raise_for_status()
        self.assertIn('invocation_pulse_gap',self.report()['sources'][0]['reasons'])

    def test_on_demand_mode_start_clocks_loss_and_unknown_checks_are_not_inferred(self):
        self.demand_source();events=self.demand_run(1500)
        events[0]['properties']['afterspan_health']['evaluator_failures']=None
        events[1]['properties']['afterspan_health']['dropped']=2
        self.f.ingest(events).raise_for_status();r=self.report()
        self.assertIn('evaluator_health_unknown',r['sources'][0]['reasons'])
        self.assertIn('collector_reports_window_loss',r['sources'][0]['reasons'])
        changed=self.demand_run(200);changed[1]['properties']['afterspan_health']['process_started_at_ms']-=1000
        self.f.ingest(changed).raise_for_status()
        self.assertIn('collector_process_clock_conflict',self.report()['sources'][0]['reasons'])
        legacy=self.demand_run(300)
        for event in legacy:event['properties']['afterspan_health'].pop('reporting_mode')
        self.f.ingest(legacy).raise_for_status()
        self.assertIn('source_mode_mismatch',self.report()['sources'][0]['reasons'])

    def test_on_demand_pre_window_completed_invocations_are_not_current_activity(self):
        self.demand_source();old=self.demand_run(2000)
        self.f.ingest(old).raise_for_status();r=self.report()
        self.assertEqual(r['sources'][0]['instances'],[])
        self.assertIn('no_invocations_observed',r['sources'][0]['reasons'])
        # A later sequence with a regressed clock cannot hide in an excluded episode.
        facts=old[0]['properties']['afterspan_health']
        self.f.ingest([self.pulse(self.now-2010,3,instance_id=facts['instance_id'],process_started_at_ms=int((self.now-2020)*1000),collector_state='running')]).raise_for_status()
        r=self.report();self.assertIn('collector_lifecycle_conflict',r['sources'][0]['reasons'])
        self.assertEqual(len(r['sources'][0]['instances']),1)

    def test_legacy_source_idempotent_retry_preserves_stored_definition_bytes(self):
        self.save().raise_for_status()
        raw=json.dumps({**self.source,'enabled':True},sort_keys=True,separators=(',',':'))
        with closing(app.connect()) as c,c:c.execute('UPDATE collection_sources SET definition=?',(raw,))
        response=self.save();self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['revision'],1)
        with closing(app.connect()) as c:self.assertEqual(c.execute('SELECT definition FROM collection_sources').fetchone()[0],raw)

    def test_on_demand_does_not_invent_an_in_window_baseline_from_a_late_first_pulse(self):
        self.demand_source();self.query['start']=stamp(self.now-300)
        events=self.demand_run(200)
        for event in events:
            event['properties']['afterspan_health'].update(process_started_at_ms=int((self.now-350)*1000),dropped=3)
        self.f.ingest(events).raise_for_status();r=self.report()
        self.assertIsNone(r['sources'][0]['instances'][0]['window_counter_changes']['dropped'])
        self.assertIn('collector_loss_window_unknown',r['sources'][0]['reasons'])

    def test_on_demand_initial_failures_cannot_be_subtracted_into_clean_health(self):
        self.demand_source();events=self.demand_run(200)
        for event in events:event['properties']['afterspan_health'].update(dropped=3,rejected=2,delivery_failures=1,evaluator_failures=1)
        self.f.ingest(events).raise_for_status();r=self.report()
        self.assertEqual(r['status'],'attention')
        self.assertIn('collector_loss_window_unknown',r['sources'][0]['reasons'])
        self.assertIn('evaluator_failure_window_unknown',r['sources'][0]['reasons'])
        self.assertTrue(all(value is None for value in r['sources'][0]['instances'][0]['window_counter_changes'].values()))

    def test_on_demand_counter_contradictions_cannot_hide_in_completed_pre_window_work(self):
        self.demand_source();events=self.demand_run(2000)
        events[0]['properties']['afterspan_health']['dropped']=3
        self.f.ingest(events).raise_for_status();r=self.report()
        self.assertIn('counter_regressed',r['sources'][0]['reasons'])
        self.assertEqual(len(r['sources'][0]['instances']),1)

    def test_on_demand_end_and_receipt_cutoffs_do_not_pull_in_a_later_stop(self):
        self.demand_source();events=self.demand_run(200)
        with patch('time.time',return_value=self.now-199):self.f.ingest([events[0]]).raise_for_status()
        with patch('time.time',return_value=self.now-20):self.f.ingest([events[1]]).raise_for_status()
        self.assertIn('invocation_stop_missing',self.report(as_of=stamp(self.now-30))['sources'][0]['reasons'])
        self.assertIn('invocation_stop_missing',self.report(end=stamp(self.now-195))['sources'][0]['reasons'])
        self.assertEqual(self.report()['status'],'observed')

    def test_python_on_demand_tracking_is_explicit_and_never_rewrites_existing_unknowns(self):
        from sdk.python.native_analytics import Collector
        options={'source':'export_worker','environment':'staging','workflow':'document_export','reporting_mode':'on_demand'}
        c=Collector('http://127.0.0.1:8789','fake-local-key',self.f.temp.name+'/invocation.db',track_evaluations=True,**options)
        c.pulse()
        with closing(c.db()) as db:entry=json.loads(db.execute('SELECT payload FROM outbox').fetchone()[0])
        facts=entry['properties']['afterspan_health'];self.assertEqual(facts['reporting_mode'],'on_demand');self.assertEqual(facts['evaluator_failures'],0)
        self.assertEqual(self.drain(c),1);c.close();c.pulse(state='stopped');self.assertEqual(self.drain(c),1)
        legacy=Collector('http://127.0.0.1:8789','fake-local-key',self.f.temp.name+'/untracked.db',**options)
        self.assertIsNone(legacy.status()['evaluator_failures'])
        resumed=Collector('http://127.0.0.1:8789','fake-local-key',legacy.path,track_evaluations=True,**options)
        self.assertIsNone(resumed.status()['evaluator_failures'])
        resumed.record_evaluation(True)
        again=Collector('http://127.0.0.1:8789','fake-local-key',legacy.path,track_evaluations=True,**options)
        self.assertEqual(again.status()['evaluator_failures'],1)
