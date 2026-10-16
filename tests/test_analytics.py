import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

import app as analytics
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'sdk/python'))
from native_analytics import Collector,RequestAnalytics,route_path,install

ADMIN='na_admin_'+('a'*40)
PASSWORD='p'+('b'*40)

class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'ANALYTICS_ADMIN_TOKEN':ADMIN,'ANALYTICS_PASSWORD':PASSWORD,'ANALYTICS_SESSION_SECRET':'s'*40,'COOKIE_SECURE':'false'})
        self.env.start();self.path=patch.object(analytics,'DB_PATH',Path(self.temp.name)/'a.db');self.path.start()
        analytics._limits.clear();analytics.init_db()
        self.client=TestClient(analytics.app)
        self.admin={'Authorization':'Bearer '+ADMIN}
        self.a=self.create('alpha');self.b=self.create('beta')

    def tearDown(self):
        self.path.stop();self.env.stop();self.temp.cleanup()

    def create(self,name):
        r=self.client.post('/api/projects',headers=self.admin,json={'id':name,'name':name})
        self.assertEqual(r.status_code,200,r.text)
        return {'Authorization':'Bearer '+r.json()['write_key']}

    def ingest(self,events,headers=None):return self.client.post('/v1/events',headers=headers or self.a,json={'events':events})

    def event(self,eid='event-1',**extra):return {'id':eid,'name':'http.request','distinct_id':'support:ankit','properties':{'status':200,'duration_ms':25,'operation':'mbb.invoice-link','ip':'203.0.113.1'},**extra}

    def query(self,project='alpha',**query):
        r=self.client.post('/api/query?project='+project,headers=self.admin,json=query)
        self.assertEqual(r.status_code,200,r.text)
        return r.json()

    def test_project_keys_cannot_read_admin_routes_or_cross_projects(self):
        self.ingest([self.event()]);self.ingest([self.event('event-2')],self.b)
        self.assertEqual(self.client.get('/api/projects',headers=self.a).status_code,401)
        self.assertEqual(self.client.get('/api/overview?project=alpha').status_code,401)
        self.assertEqual(self.ingest([self.event()],self.admin).status_code,401)
        self.assertEqual(self.query()['total'],1);self.assertEqual(self.query(project='beta')['total'],1)
        self.assertNotIn('write_key',self.client.get('/api/projects',headers=self.admin).text)

    def test_schema_and_agent_guide_are_public_and_key_rotation_revokes_old_producer(self):
        self.assertEqual(self.client.get('/openapi.json').status_code,200)
        self.assertEqual(self.client.get('/llms.txt').status_code,200)
        self.assertEqual(self.client.get('/api/projects').status_code,401)
        rotated=self.client.post('/api/projects/alpha/rotate-key',headers=self.admin).json()
        self.assertEqual(self.ingest([self.event()]).status_code,401)
        self.assertEqual(self.ingest([self.event()],{'Authorization':'Bearer '+rotated['write_key']}).status_code,200)

    def test_numeric_token_metrics_survive_while_credentials_are_redacted(self):
        self.ingest([self.event(properties={'ai_input_tokens':123,'output_tokens':7,'token':'credential'})])
        self.assertEqual(self.query(metric='sum',measure='ai_input_tokens')['total'],123)
        result=self.client.get('/api/events?project=alpha',headers=self.admin).text
        self.assertNotIn('credential',result)

    def test_event_id_deduplicates_and_does_not_reapply_traits(self):
        event=self.event(attributes={'plan':'starter','tags':['support']})
        self.assertEqual(self.ingest([event]).json(),{'accepted':1,'duplicates':0})
        event['attributes']['plan']='changed'
        self.assertEqual(self.ingest([event]).json(),{'accepted':0,'duplicates':1})
        people=self.client.get('/api/people?project=alpha',headers=self.admin).json()['people']
        self.assertEqual(people[0]['attributes']['plan'],'starter')

    def test_properties_redact_secrets_recursively_and_error_responses_hide_input(self):
        self.ingest([self.event(properties={'password':'private-password','nested':{'Authorization':'Bearer private-token'},'text':'Bearer abc123','safe':'yes'})])
        response=self.client.get('/api/events?project=alpha',headers=self.admin)
        self.assertNotIn('private-',response.text);self.assertNotIn('abc123',response.text)
        bad=self.ingest([self.event(name='INVALID SECRET !')])
        self.assertEqual(bad.status_code,422);self.assertNotIn('INVALID SECRET',bad.text)

    def test_count_sum_average_p95_breakdown_and_property_filters(self):
        self.ingest([self.event('e-'+str(i),properties={'duration_ms':i,'plan':'starter' if i<3 else 'pro'}) for i in range(1,5)])
        self.assertEqual(self.query(metric='sum',measure='duration_ms')['total'],10)
        self.assertEqual(self.query(metric='average',measure='duration_ms')['total'],2.5)
        self.assertEqual(self.query(metric='p95',measure='duration_ms')['total'],4)
        self.assertEqual(self.query(metric='users')['total'],1)
        self.assertEqual(self.query(property='plan',value='pro')['total'],2)
        self.assertEqual(len(self.query(breakdown='plan')['breakdown']),2)
        self.assertEqual(self.client.post('/api/query?project=alpha',headers=self.admin,json={'metric':'sum'}).status_code,422)

    def test_identify_merges_traits_and_owner_can_tag(self):
        self.client.post('/v1/identify',headers=self.a,json={'distinct_id':'user-1','attributes':{'plan':'pro'}})
        self.client.patch('/api/people?project=alpha',headers=self.admin,json={'distinct_id':'user-1','attributes':{'tags':['vip']}})
        p=self.client.get('/api/people?project=alpha&search=vip',headers=self.admin).json()['people'][0]
        self.assertEqual(p['attributes'],{'plan':'pro','tags':['vip']})
        self.assertEqual(self.client.get('/api/people?project=beta',headers=self.admin).json()['people'],[])

    def test_cookie_login_csrf_wrong_password_and_bruteforce_limits(self):
        self.assertEqual(self.client.post('/auth/login',json={'password':'wrong'}).status_code,401)
        self.assertEqual(self.client.post('/auth/login',json={'password':PASSWORD}).status_code,200)
        self.assertEqual(self.client.get('/api/projects').status_code,200)
        self.assertEqual(self.client.post('/api/projects',json={'id':'gamma','name':'Gamma'}).status_code,403)
        self.assertEqual(self.client.post('/api/projects',headers={'X-Native-CSRF':'1','Origin':'https://evil.example'},json={'id':'gamma','name':'Gamma'}).status_code,403)
        self.assertEqual(self.client.post('/auth/logout',headers={'X-Native-CSRF':'1'},json={}).status_code,200)
        for i in range(8):self.client.post('/auth/login',json={'password':'wrong'})
        self.assertEqual(self.client.post('/auth/login',json={'password':PASSWORD}).status_code,429)

    def test_saved_charts_are_project_isolated(self):
        chart=self.client.post('/api/charts?project=alpha',headers=self.admin,json={'title':'Invoices','query':{'event':'http.request','breakdown':'operation'}}).json()
        self.client.delete('/api/charts/'+chart['id']+'?project=beta',headers=self.admin)
        self.assertEqual(len(self.client.get('/api/charts?project=alpha',headers=self.admin).json()['charts']),1)
        self.assertEqual(self.client.get('/api/charts?project=beta',headers=self.admin).json()['charts'],[])

    def test_overview_requests_errors_latency_and_ips(self):
        self.ingest([self.event(),self.event('error',properties={'status':502,'duration_ms':500,'ip':'203.0.113.2'})])
        r=self.client.get('/api/overview?project=alpha',headers=self.admin)
        self.assertEqual(r.status_code,200,r.text)
        t=r.json()['totals'];self.assertEqual((t['requests'],t['errors'],t['p95_ms'],t['unique_ips']),(2,1,500,2))

    def test_retention_future_and_batch_rollback(self):
        self.assertEqual(self.ingest([self.event(timestamp='2099-01-01T00:00:00Z')]).status_code,422)
        with analytics.connect() as c:
            c.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?)',('alpha','old','test',None,0,0,'{}'));c.commit()
        analytics.prune();self.assertEqual(self.query()['total'],0)
        self.assertEqual(self.ingest([self.event(),self.event('bad',timestamp='2099-01-01T00:00:00Z')]).status_code,422)
        self.assertEqual(self.query()['total'],0)

    def test_validation_and_size_caps_and_sql_injection(self):
        self.assertEqual(self.ingest([self.event(properties={'big':'x'*3000000})]).status_code,413)
        self.assertEqual(self.client.post('/api/query?project=alpha',headers=self.admin,json={'breakdown':"x');DROP TABLE events;--"}).status_code,422)
        self.assertEqual(self.client.get('/api/overview?project=alpha&days=0',headers=self.admin).status_code,422)
        self.assertEqual(self.client.post('/v1/events',headers=self.a,json={'events':[]}).status_code,422)

    def test_ingestion_rate_limit_and_cursor_does_not_skip_equal_timestamps(self):
        now=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
        self.ingest([self.event('a-'+str(i).zfill(3),timestamp=now) for i in range(100)])
        self.ingest([self.event('z-last',timestamp=now)])
        first=self.client.get('/api/events?project=alpha',headers=self.admin).json()
        cursor=first['next_cursor'];second=self.client.get('/api/events?project=alpha&before='+str(cursor['before'])+'&before_id='+cursor['before_id'],headers=self.admin).json()
        self.assertEqual(len(first['events'])+len(second['events']),101)
        analytics._limits['alpha']=(time.monotonic(),600)
        self.assertEqual(self.ingest([self.event()]).status_code,429)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'queue.db'
        self.c=Collector('https://analytics.example','write-secret',self.path)
    def tearDown(self):self.temp.cleanup()

    def test_outage_retains_events_across_restart_and_dedup_receipt_clears(self):
        eid=self.c.track('test.event','user-1',{'value':5})
        with patch('urllib.request.OpenerDirector.open',side_effect=OSError('offline')):self.assertEqual(self.c.flush(),0)
        c=Collector('https://analytics.example','write-secret',self.path)
        self.assertEqual(c.status()['queued'],1)
        class Reply:
            status=200
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self):return b'{"accepted":0,"duplicates":1}'
        with patch('urllib.request.OpenerDirector.open',return_value=Reply()):self.assertEqual(c.flush(),1)
        self.assertEqual(c.status()['queued'],0)

    def test_queue_capacity_is_visible_and_does_not_break_caller(self):
        c=Collector('https://analytics.example','write-secret',self.path,max_queue=1)
        c.track('test.one');self.assertIsNone(c.track('test.two'));self.assertEqual(c.status()['dropped'],1)

    def test_asgi_logs_rejected_requests_and_redacts_payloads_and_ips(self):
        from fastapi import FastAPI,Request
        a=FastAPI()
        @a.post('/test/{id}')
        async def endpoint(id:str,request:Request):
            await request.json();return {'ok':True}
        a.add_middleware(RequestAnalytics,collector=self.c,credentials={'support:ankit':'secret'},trust_railway=False)
        client=TestClient(a)
        result=client.post('/test/customer-private-id?token=private-query',headers={'Authorization':'Bearer secret','X-Real-IP':'203.0.113.7','Cookie':'secret=private-cookie'},json={'password':'private-password','parameters':{'mobile':'9876543210'},'operation':'mbb.freeze','environment':'prod'})
        self.assertEqual(result.status_code,200)
        with self.c.db() as c:payload=c.execute('SELECT payload FROM outbox').fetchone()[0]
        for value in ['private-password','private-query','private-cookie','customer-private-id','9876543210','Bearer secret']:
            self.assertNotIn(value,payload)
        event=json.loads(payload);self.assertEqual(event['distinct_id'],'support:ankit');self.assertEqual(event['properties']['path'],'/test/{id}')
        self.assertNotEqual(event['properties']['ip'],'203.0.113.7')

    def test_unknown_paths_do_not_leak_customer_ids_and_safe_sdk_redacts(self):
        self.assertEqual(route_path({'path':'/support/v1/plans/secret-customer-id/execute'}),'/support/v1/plans/:redacted/execute')
        self.c.track('custom.event',properties={'token':'private-token','text':'Bearer abc123'})
        with self.c.db() as c:body=c.execute('SELECT payload FROM outbox').fetchone()[0]
        self.assertNotIn('private-token',body);self.assertNotIn('abc123',body)

    def test_enabled_installation_starts_and_stops_collector_with_fastapi(self):
        from fastapi import FastAPI
        a=FastAPI()
        @a.get('/healthz')
        def health():return {'ok':True}
        with patch.dict(os.environ,{'NATIVE_ANALYTICS_URL':'https://analytics.example','NATIVE_ANALYTICS_WRITE_KEY':'write-secret'}), \
             patch.object(Collector,'start') as start,patch.object(Collector,'close') as close:
            collector=install(a,self.path)
            with TestClient(a) as client:self.assertEqual(client.get('/healthz').status_code,200)
            start.assert_called_once();close.assert_called_once()
            self.assertEqual(collector.status()['queued'],1)

if __name__=='__main__':unittest.main()
