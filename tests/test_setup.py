from datetime import datetime,timezone
import time
import unittest

import app
import test_analytics


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_analytics.AnalyticsTests();self.fixture.setUp();self.client=self.fixture.client;self.admin=self.fixture.admin
    def tearDown(self):self.fixture.tearDown()
    def create(self,project='alpha',environment='local'):
        r=self.client.post('/api/setup-checks?project='+project,headers=self.admin,json={'environment':environment})
        self.assertEqual(r.status_code,200,r.text);return r.json()
    def status(self,project='alpha',environment='local',headers=None):
        r=self.client.get('/api/setup?project='+project+'&environment='+environment,headers=headers or self.admin)
        self.assertEqual(r.status_code,200,r.text);return r.json()

    def test_connection_needs_the_matching_project_environment_and_producer_receipt(self):
        check=self.create();waiting=self.status()['connection']
        self.assertEqual(waiting['state'],'waiting');self.assertEqual(waiting['event'],check['event'])
        self.assertEqual(self.fixture.ingest([check['event']],headers=self.admin).status_code,401)
        self.assertEqual(self.fixture.ingest([check['event']],headers=self.fixture.b).status_code,200)
        self.assertEqual(self.status()['connection']['state'],'waiting')
        wrong={**check['event'],'properties':{**check['event']['properties'],'environment':'production'}}
        wrong['id']='another-id'
        self.fixture.ingest([wrong]);self.assertEqual(self.status()['connection']['state'],'waiting')
        self.fixture.ingest([check['event']]);s=self.status()
        self.assertEqual(s['connection']['state'],'verified')
        self.assertIsNotNone(s['connection']['verified_at'])
        self.assertEqual(self.fixture.query()['total'],0)
        self.assertEqual(self.client.get('/api/overview?project=alpha',headers=self.admin).json()['totals']['total_events'],0)
        self.assertEqual(s['journeys']['state'],'not_seen')

    def test_expired_checks_do_not_accept_late_receipts_and_reads_are_scoped(self):
        check=self.create()
        with app.connect() as c:c.execute('UPDATE setup_checks SET expires=?',(time.time()-10,))
        self.fixture.ingest([check['event']]);self.assertEqual(self.status()['connection']['state'],'expired')
        key=self.client.post('/api/projects/alpha/read-keys',headers=self.admin,json={'label':'Reader'}).json()['read_key']
        reader={'Authorization':'Bearer '+key}
        self.assertEqual(self.status(headers=reader)['connection']['state'],'expired')
        self.assertEqual(self.client.get('/api/setup?project=beta',headers=reader).status_code,403)
        self.assertEqual(self.client.post('/api/setup-checks?project=alpha',headers=reader,json={}).status_code,401)

    def test_complete_journey_and_recovery_are_distinct_from_connection_verification(self):
        base=time.time()-100
        def event(number,state):return {'id':str(number)+state,'name':'summary.'+state,
            'timestamp':datetime.fromtimestamp(base+number*10+(0 if state=='started' else 1),timezone.utc).isoformat(),'journey':{
            'id':'task','workflow':'summary','environment':'local','subject_kind':'anonymous',
            'attempt_id':'attempt-'+str(number),'attempt_number':number,'state':state}}
        self.fixture.ingest([event(1,'failed')])
        self.assertEqual(self.status()['journeys']['state'],'not_seen')
        self.fixture.ingest([event(1,'started'),event(2,'started'),event(2,'succeeded')])
        s=self.status();self.assertEqual(s['connection']['state'],'not_started')
        self.assertEqual(s['journeys']['state'],'observed')
        self.assertEqual(s['retry_recovery']['state'],'observed')
        self.assertEqual(self.status(environment='production')['journeys']['state'],'not_seen')
