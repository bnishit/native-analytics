import time
import unittest
from datetime import datetime,timezone

import app
import test_analytics


def iso(value):return datetime.fromtimestamp(value,timezone.utc).isoformat()


class JourneyTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_analytics.AnalyticsTests();self.fixture.setUp()
        self.client=self.fixture.client;self.admin=self.fixture.admin;self.base=time.time()-300

    def tearDown(self):self.fixture.tearDown()

    def event(self,jid='save-one',number=1,state='started',at=0,**changes):
        ctx={'id':jid,'workflow':'summary','environment':'local','attempt_id':jid+'-attempt-'+str(number),
             'attempt_number':number,'state':state,'subject_kind':'workspace','release':'v1'}
        ctx.update(changes.pop('context',{}))
        return {'id':jid+'-'+str(number)+'-'+state+'-'+str(at),'name':'summary.'+state,
            'distinct_id':'workspace-opaque','timestamp':iso(self.base+at),'journey':ctx,**changes}

    def ingest(self,*events,headers=None):return self.client.post('/v1/events',headers=headers or self.fixture.a,json={'events':list(events)})

    def report(self,body=None,listed=False,headers=None,project='alpha'):
        result=self.client.post('/api/journeys'+('/list' if listed else '')+'?project='+project,headers=headers or self.admin,json=body or {})
        self.assertEqual(result.status_code,200,result.text)
        return result.json()

    def test_retry_is_one_recovered_journey_and_delivery_order_does_not_choose_outcome(self):
        events=[self.event(state='started'),self.event(state='failed',at=10),self.event(number=2,at=20),self.event(number=2,state='succeeded',at=30)]
        self.assertEqual(self.ingest(*reversed(events)).status_code,200)
        self.assertEqual(self.ingest(events[-1]).json()['duplicates'],1)
        report=self.report(listed=True);t=report['totals']
        self.assertEqual((t['observed_journeys'],t['attempts'],t['retry_attempts'],t['succeeded'],t['failed'],t['recovered_journeys']),(1,2,1,1,0,1))
        self.assertEqual([a['state'] for a in report['journeys'][0]['attempts']],['failed','succeeded'])

    def test_pending_retry_reopens_the_journey_and_failed_is_latest_known_attempt(self):
        self.ingest(self.event(state='started'),self.event(state='failed',at=10),self.event(number=2,at=20))
        self.assertEqual(self.report()['totals']['in_progress'],1)
        self.ingest(self.event(number=2,state='failed',at=30))
        self.assertEqual(self.report()['totals']['failed'],1)
        self.assertEqual(self.report()['totals']['recovered_journeys'],0)

    def test_conflicting_terminal_states_are_unknown_and_missing_history_is_visible(self):
        self.ingest(self.event(),self.event(state='succeeded',at=10),self.event(state='failed',at=10),
            self.event('partial',number=3,state='succeeded',at=20))
        report=self.report();self.assertEqual(report['totals']['unknown'],1)
        self.assertEqual(report['totals']['conflicting_journeys'],1)
        self.assertEqual(report['totals']['attempts_missing_start'],1)
        self.assertEqual(report['totals']['missing_attempts'],2)
        self.assertEqual(report['totals']['retry_attempts'],0)

    def test_shared_ids_stay_separate_by_project_environment_and_workflow(self):
        self.ingest(self.event(state='succeeded'))
        self.ingest(self.event(state='failed',at=1,context={'environment':'production'}))
        self.ingest(self.event(state='cancelled',at=2,context={'workflow':'ask'}))
        self.ingest(self.event(state='failed',at=3),headers=self.fixture.b)
        self.assertEqual(self.report()['totals']['observed_journeys'],3)
        self.assertEqual(self.report({'workflow':'summary','environment':'local'})['totals']['succeeded'],1)
        self.assertEqual(self.report(project='beta')['totals']['failed'],1)

    def test_context_conflicts_rollback_the_entire_batch_and_do_not_rewrite_identity(self):
        self.ingest(self.event())
        good=self.event('another',state='started',at=2)
        bad=self.event(state='succeeded',at=3,distinct_id='a-different-workspace')
        self.assertEqual(self.ingest(good,bad).status_code,409)
        self.assertEqual(self.report()['totals']['observed_journeys'],1)
        self.assertEqual(self.ingest(self.event(number=2,at=4,context={'attempt_id':'save-one-attempt-1'})).status_code,409)
        self.assertEqual(self.ingest(self.event(state='succeeded',at=5,context={'release':'v2'})).status_code,409)

    def test_old_events_remain_unlinked_and_anonymous_units_are_explicit(self):
        self.ingest({'id':'old','name':'summary.completed'},self.event('anonymous',distinct_id=None,context={'subject_kind':'anonymous'}))
        report=self.report()
        self.assertEqual(report['coverage']['unlinked_project_events_in_window'],1)
        self.assertEqual(report['totals']['observed_journeys'],1)
        self.assertEqual(report['totals']['identified_subjects'],{})
        self.assertEqual(self.ingest(self.event('invalid',context={'subject_kind':'anonymous'})).status_code,422)

    def test_history_before_window_preserves_recovery_but_does_not_create_a_new_cohort(self):
        self.ingest(self.event(),self.event(state='failed',at=10),self.event(number=2,at=20),self.event(number=2,state='succeeded',at=30))
        report=self.report({'start':iso(self.base+25),'end':iso(self.base+35)})
        self.assertEqual(report['totals']['recovered_journeys'],1)
        self.assertEqual(report['coverage']['linked_events_in_window'],1)
        self.assertEqual(report['totals']['attempts'],2)

    def test_snapshot_excludes_late_delivery_and_replays_the_same_fingerprint(self):
        self.ingest(self.event(),self.event(state='failed',at=10))
        first=self.report();self.ingest(self.event(number=2,state='succeeded',at=20))
        frozen=self.report(first['query']);fresh=self.report()
        self.assertEqual(frozen['totals'],first['totals'])
        self.assertEqual(frozen['fingerprint'],first['fingerprint'])
        self.assertEqual(fresh['totals']['recovered_journeys'],1)

    def test_erasure_cascades_links_and_blocks_late_identity_free_resurrection(self):
        self.ingest(self.event(),self.event(state='succeeded',at=10))
        receipt=self.client.post('/v1/erase',headers=self.fixture.a,json={'distinct_id':'workspace-opaque'})
        self.assertEqual(receipt.status_code,200)
        self.assertEqual(self.report()['totals']['observed_journeys'],0)
        late=self.event(state='succeeded',at=20,distinct_id=None,context={'subject_kind':'anonymous'})
        self.assertEqual(self.ingest(late).json()['accepted'],0)
        self.assertEqual(self.report()['totals']['observed_journeys'],0)

    def test_scoped_reader_gets_aggregate_evidence_without_journey_or_subject_ids(self):
        self.ingest(self.event())
        token=self.client.post('/api/projects/alpha/read-keys',headers=self.admin,json={'label':'Agent'}).json()['read_key']
        key={'Authorization':'Bearer '+token}
        report=self.report(headers=key)
        self.assertNotIn('save-one',str(report));self.assertNotIn('workspace-opaque',str(report))
        self.assertEqual(self.client.post('/api/journeys/list?project=alpha',headers=key,json={}).status_code,401)
        self.assertEqual(self.client.post('/api/journeys?project=beta',headers=key,json={}).status_code,403)


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_analytics.AnalyticsTests();self.fixture.setUp();self.client=self.fixture.client;self.admin=self.fixture.admin
    def tearDown(self):self.fixture.tearDown()
    def query(self,body):return self.client.post('/api/query?project=alpha',headers=self.admin,json=body)

    def test_combined_filters_preserve_json_types_and_explicit_null(self):
        values=[{'env':'production','ok':True,'cost':0,'tag':None},{'env':'production','ok':False,'cost':0},{'env':'test','ok':True,'cost':1}]
        self.fixture.ingest([{'id':str(i),'name':'saved','properties':p} for i,p in enumerate(values)])
        report=self.query({'filters':[{'property':'env','value':'production'},{'property':'ok','value':True}]}).json()
        self.assertEqual(report['total'],1)
        self.assertEqual(self.query({'filters':[{'property':'ok','value':0}]}).json()['total'],0)
        self.assertEqual(self.query({'filters':[{'property':'tag','value':None}]}).json()['total'],1)

    def test_absolute_window_boundaries_and_frozen_late_events(self):
        now=time.time()-100
        self.fixture.ingest([{'id':str(i),'name':'saved','timestamp':iso(now+i)} for i in range(3)])
        first=self.query({'start':iso(now),'end':iso(now+2)}).json()
        self.assertEqual(first['total'],2)
        self.fixture.ingest([{'id':'late','name':'saved','timestamp':iso(now+1)}])
        again=self.query(first['query']).json()
        self.assertEqual(again['total'],2);self.assertEqual(first['fingerprint'],again['fingerprint'])
        self.assertEqual(self.query({'start':iso(now),'end':iso(now+2)}).json()['total'],3)

    def test_bad_windows_filters_and_non_finite_numbers_are_rejected(self):
        now=time.time()
        for body in [{'start':iso(now-10)},{'start':iso(now),'end':iso(now-1)},
            {'start':'2026-10-01T00:00:00','end':'2026-10-02T00:00:00'},
            {'start':iso(now-91*86400),'end':iso(now)},
            {'filters':[{'property':'x); DROP TABLE events;','value':1}]},
            {'as_of':iso(now+1000)}]:
            self.assertEqual(self.query(body).status_code,422,body)
