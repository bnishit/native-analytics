import importlib.util
import json
import os
import random
import sys
import time
import unittest
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
import app
from product_analytics import FunnelDefinition,ProductAnalytics
import test_workspaces

CSRF=test_workspaces.CSRF
class ProductTests(unittest.TestCase):
    user=test_workspaces.WorkspaceTests.user;token=test_workspaces.WorkspaceTests.token;workspace=test_workspaces.WorkspaceTests.workspace;project=test_workspaces.WorkspaceTests.project;join=test_workspaces.WorkspaceTests.join;invite=test_workspaces.WorkspaceTests.invite;key=test_workspaces.WorkspaceTests.key
    def setUp(self):
        test_workspaces.WorkspaceTests.setUp(self)
        self.edition=patch.dict(os.environ,{'AFTERSPAN_EDITION':'self_hosted'});self.edition.start()
        self.owner,_=self.user('product@example.test');self.w=self.workspace(self.owner);self.p=self.project(self.owner,self.w);self.t=time.time()-3600;self.n=0
    def tearDown(self):self.edition.stop();test_workspaces.WorkspaceTests.tearDown(self)
    def send(self,name,identity='one',at=0,properties=None,attributes=None):
        self.n+=1;e={'id':'event-'+str(self.n),'name':name,'distinct_id':identity,'timestamp':datetime.fromtimestamp(self.t+at,timezone.utc).isoformat(),'properties':properties or {},'attributes':attributes or {}}
        r=self.client.post('/v1/events',headers={'Authorization':'Bearer '+self.p['write_key']},json={'events':[e]});self.assertEqual(r.status_code,200,r.text);return e
    def request(self,kind,body,client=None,method='POST',suffix='/analyze'):
        return (client or self.owner).request(method,'/api/'+kind+suffix+'?project='+self.p['id'],headers=CSRF,json=body)
    def funnel(self,**definition):return {'definition':{'steps':[{'event':'start'},{'event':'middle'},{'event':'finish'}],**definition}}
    def test_order_window_retries_ties_anonymous_and_unique_units(self):
        for identity,events in {'one':[('start',0),('middle',10),('finish',20),('finish',30)],'two':[('middle',0),('start',10),('finish',20)],'three':[('start',0),('middle',0),('finish',1)],'four':[('start',0),('start',100),('middle',110),('finish',120)],'five':[('start',0),('middle',1),('finish',40)]}.items():
            for name,t in events:self.send(name,identity,t)
        for name,t in [('start',0),('middle',1),('finish',2)]:self.send(name,None,t)
        r=self.request('funnels',self.funnel(conversion_seconds=25));self.assertEqual(r.status_code,200,r.text);r=r.json()
        self.assertEqual([s['identities'] for s in r['steps']],[5,3,2]);self.assertEqual(r['anonymous_events_excluded'],3);self.assertEqual(r['conversion_rate'],.4);self.assertEqual(r['median_conversion_seconds'],20)
        self.assertNotIn('distinct_ids',r);self.assertEqual(self.request('funnels',r['query']).json()['fingerprint'],r['fingerprint'])
    def test_typed_filters_null_missing_and_current_traits(self):
        self.send('start','one',properties={'tier':True,'nullable':None},attributes={'plan':'builder','seats':2})
        self.send('start','two',properties={'tier':1},attributes={'plan':'free','seats':1})
        self.send('finish','one',1);self.send('finish','two',1)
        q=self.funnel(steps=[{'event':'start','filters':[{'property':'tier','value':True},{'property':'nullable','value':None}]},{'event':'finish'}])
        self.assertEqual(self.request('funnels',q).json()['converted'],1)
        rules=[{'kind':'trait','property':'seats','op':'gte','value':2},{'kind':'event','event':'start','min_count':1}]
        q={'definition':{'match':'all','rules':rules}};r=self.request('segments',q).json();self.assertEqual(r['matched_identities'],1)
        self.send('profile.updated','two',2,attributes={'seats':3})
        self.assertEqual(self.request('segments',r['query']).json()['matched_identities'],2)
        self.assertIn('current traits',r['traits'])
    def test_dynamic_zero_rules_and_any(self):
        self.send('start','one');self.send('finish','one',1);self.send('start','two');self.send('start','three');self.send('start','three',1)
        rules=[{'kind':'event','event':'finish','min_count':0,'max_count':0},{'kind':'event','event':'start','min_count':2}]
        allq={'definition':{'match':'all','rules':rules}}
        self.assertEqual(self.request('segments',allq).json()['matched_identities'],1)
        allq['definition']['match']='any';self.assertEqual(self.request('segments',allq).json()['matched_identities'],2)
        self.send('finish','three',2);allq['definition']['match']='all';self.assertEqual(self.request('segments',allq).json()['matched_identities'],0)
    def test_saved_segments_filter_funnels_and_optimistic_updates(self):
        self.send('start',attributes={'plan':'builder'});self.send('middle',at=1);self.send('finish',at=2)
        segment={'title':'Builders','definition':{'rules':[{'kind':'trait','property':'plan','value':'builder'}]}}
        r=self.request('segments',segment,suffix='');self.assertEqual(r.status_code,201,r.text);seg=r.json()
        q=self.funnel(segment_id=seg['id']);self.assertEqual(self.request('funnels',q).json()['converted'],1)
        saved=self.request('funnels',{'title':'Activation',**q},suffix='').json()
        path='/api/segments/'+seg['id']+'?project='+self.p['id']+'&revision=1'
        self.assertEqual(self.owner.delete(path,headers=CSRF).status_code,409)
        segment['title']='Paying builders';self.assertEqual(self.owner.put(path,headers=CSRF,json=segment).status_code,200)
        self.assertEqual(self.owner.put(path,headers=CSRF,json=segment).status_code,409)
        self.assertEqual(self.owner.delete('/api/funnels/'+saved['id']+'?project='+self.p['id']+'&revision=1',headers=CSRF).status_code,200)
        self.assertEqual(self.owner.delete(path.replace('revision=1','revision=2'),headers=CSRF).status_code,200)
        self.assertEqual(self.request('funnels',q).status_code,404)
    def test_workspace_reader_write_and_cross_project_isolation(self):
        segment={'title':'Started','definition':{'rules':[{'kind':'event','event':'start'}]}}
        saved=self.request('segments',segment,suffix='').json()
        reader,_=self.join(self.owner,self.w,'reader-product@example.test')
        self.assertEqual(self.request('segments',segment,reader,suffix='').status_code,403)
        self.assertEqual(self.request('segments',{'definition':segment['definition']},reader).status_code,200)
        otherw=self.workspace(self.owner,'Other');otherp=self.project(self.owner,otherw)
        query=self.funnel(segment_id=saved['id'])
        r=self.owner.post('/api/funnels/analyze?project='+otherp['id'],headers=CSRF,json=query);self.assertEqual(r.status_code,404)
        key=self.key(self.owner,self.w,'editor');agent=TestClient(app.app);agent.headers.update({'Authorization':'Bearer '+key['key']})
        self.assertEqual(agent.get('/api/segments?project='+otherp['id']).status_code,403)
        self.assertEqual(self.request('segments',segment,agent,suffix='').status_code,201)
        self.assertEqual(self.client.get('/api/funnels?project='+self.p['id']).status_code,401)
        self.assertEqual(self.client.get('/api/segments?project='+self.p['id'],headers={'Authorization':'Bearer '+self.p['write_key']}).status_code,401)
    def test_aggregate_read_key_parity_without_write_or_raw_access(self):
        self.send('start');self.send('middle',at=1);self.send('finish',at=2)
        r=self.owner.post('/api/projects/'+self.p['id']+'/read-keys',headers=CSRF,json={'label':'Agent','expires_days':1});self.assertEqual(r.status_code,200,r.text)
        c=TestClient(app.app);c.headers.update({'Authorization':'Bearer '+r.json()['read_key']})
        self.assertEqual(self.request('funnels',self.funnel(),c).json()['converted'],1)
        self.assertEqual(c.get('/api/people?project='+self.p['id']).status_code,401)
        self.assertEqual(self.request('funnels',{'title':'Not allowed',**self.funnel()},c,suffix='').status_code,401)
        self.assertEqual(c.get('/api/segments?project=alpha').status_code,403)
    def test_fixed_receipt_cutoff_and_erasure(self):
        self.send('start');self.send('middle',at=1);r=self.request('funnels',self.funnel()).json();self.assertEqual(r['converted'],0)
        self.send('finish',at=2);self.assertEqual(self.request('funnels',r['query']).json()['converted'],0);self.assertEqual(self.request('funnels',self.funnel()).json()['converted'],1)
        with closing(app.connect()) as c,c:c.execute('DELETE FROM events WHERE project=? AND distinct_id=?',(self.p['id'],'one'))
        self.assertEqual(self.request('funnels',self.funnel()).json()['entrants'],0)
    def test_validation_and_no_silent_truncation(self):
        for q in [self.funnel(steps=[{'event':'start'}]),self.funnel(conversion_seconds=0),self.funnel(steps=[{'event':'start','filters':[{'property':'x);DROP','value':1}]},{'event':'finish'}])]:self.assertEqual(self.request('funnels',q).status_code,422)
        for rules in [[{'kind':'event','event':'x','min_count':2,'max_count':1}],[{'kind':'trait','property':'a','op':'gte','value':'two'}]]:self.assertEqual(self.request('segments',{'definition':{'rules':rules}}).status_code,422)
        with closing(app.connect()) as c,c:
            c.executemany('INSERT INTO events VALUES(?,?,?,?,?,?,?)',[(self.p['id'],str(i),'start','one',self.t,time.time(),'{}') for i in range(200001)])
        self.assertEqual(self.request('funnels',self.funnel()).status_code,422)
    def test_self_hosted_has_no_commercial_gate_but_keeps_security(self):
        u=self.owner.get('/api/workspaces/'+self.w+'/usage').json();self.assertEqual(u['edition'],'self_hosted');self.assertTrue(all(u['limits'][k] is None for k in ['events','bytes','projects','seats']))
        for i in range(4):
            self.project(self.owner,self.w,'project-'+str(i))
            self.invite(self.owner,self.w,'seat-'+str(i)+'@example.test')
        with closing(app.connect()) as c,c:c.execute('INSERT INTO usage_months VALUES(?,?,?,?,?)',(self.w,__import__('billing').month_window()[0],10000,50000000,0))
        self.send('still.collecting');self.assertEqual(self.owner.get('/api/workspaces/'+self.w+'/usage').json()['totals']['events'],10001)
        self.assertEqual(self.owner.post('/api/workspaces/'+self.w+'/billing/checkout',headers=CSRF,json={'plan':'team'}).status_code,503)
        self.assertEqual(self.client.get('/api/plans').json()['license'],'MIT')
    def test_funnel_matches_exhaustive_paths(self):
        engine=object.__new__(ProductAnalytics);random.seed(71);definition=FunnelDefinition(steps=[{'event':x} for x in ['a','b','c']],conversion_seconds=5)
        for _ in range(100):
            rows=[{'name':random.choice('abc'),'ts':random.randrange(15),'distinct_id':'one','properties':{}} for _ in range(14)];rows.sort(key=lambda r:r['ts'])
            paths=[(r['ts'],) for r in rows if r['name']=='a'];counts=[int(bool(paths))]
            for name in ['b','c']:
                paths=[(*p,r['ts']) for p in paths for r in rows if r['name']==name and p[-1]<r['ts'] and r['ts']-p[0]<=5];counts.append(int(bool(paths)))
            result=engine.funnel(rows,{'one'},definition);self.assertEqual([s['identities'] for s in result['steps']],counts)
