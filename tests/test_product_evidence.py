import json
import unittest
import uuid
from contextlib import closing
from datetime import datetime,timedelta,timezone
import app
import test_product as fixtures
from test_workspaces import CSRF
class ProductEvidenceTests(unittest.TestCase):
    user=fixtures.ProductTests.user;token=fixtures.ProductTests.token;workspace=fixtures.ProductTests.workspace;project=fixtures.ProductTests.project;join=fixtures.ProductTests.join;invite=fixtures.ProductTests.invite;key=fixtures.ProductTests.key;send=fixtures.ProductTests.send
    def setUp(self):fixtures.ProductTests.setUp(self)
    def tearDown(self):fixtures.ProductTests.tearDown(self)
    def post(self,path,body,method='POST'):
        r=self.owner.request(method,path+'?project='+self.p['id'],headers=CSRF,json=body);self.assertIn(r.status_code,[200,201],r.text);return r.json()
    def test_funnel_multivalue_filter_breakdown_and_open_windows(self):
        for i,value in enumerate(['milestone','birthday_wish','other']):
            self.send('d_modal_open',str(i),i,properties={'modal_type':value});self.send('setting_toggled',str(i),i+10,properties={'toggle_state':'off' if i==0 else 'on'})
        d={'steps':[{'event':'d_modal_open','filters':[{'property':'modal_type','op':'in','value':['milestone','birthday_wish']}]},{'event':'setting_toggled','filters':[{'property':'toggle_state','value':'off'}]}],'breakdown':'modal_type'}
        r=self.post('/api/funnels/analyze',{'definition':d});self.assertEqual(r['entrants'],2);self.assertEqual(r['converted'],1);self.assertEqual(r['open_window_nonconverters'],1)
        groups={r['value']:r for r in r['breakdown']};self.assertEqual(groups['milestone']['step_counts'],[1,1]);self.assertEqual(groups['birthday_wish']['step_counts'],[1,0])
        d['steps'][0]['filters'][0]['value']=[True];self.assertEqual(self.post('/api/funnels/analyze',{'definition':d})['entrants'],0)
    def test_retention_mature_denominators_exact_rolling_and_timezone(self):
        now=datetime.now(timezone.utc);monday=now.replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=now.weekday());start=monday-timedelta(weeks=3);self.t=start.timestamp()
        for identity,week in [('one',0),('two',0),('three',1),('four',2)]:self.send('print_barcode',identity,week*604800+100,properties={'platform':'web'})
        for identity,week in [('one',1),('two',2),('three',2)]:self.send('print_barcode',identity,week*604800+200,properties={'platform':'web'})
        self.send('print_barcode',None,200,properties={'platform':'web'})
        definition={'start':{'event':'print_barcode','filters':[{'property':'platform','value':'web'}]},'return_event':{'event':'print_barcode','filters':[{'property':'platform','value':'web'}]},'period':'week','periods':3,'method':'exact','timezone':'UTC'}
        query={'start':start.isoformat(),'end':monday.isoformat(),'definition':definition}
        r=self.post('/api/retention/analyze',query);self.assertEqual(r['started_identities'],4);self.assertEqual(r['overall'][1]['eligible'],3);self.assertEqual(r['overall'][1]['retained'],2);self.assertEqual(r['overall'][2]['eligible'],2)
        self.assertIsNone(r['cohorts'][-1]['cells'][1]['rate']);self.assertIsNone(r['cohorts'][-1]['cells'][1]['retained']);self.assertEqual(r['anonymous_events_excluded'],1)
        query['definition']['method']='on_or_after';rolling=self.post('/api/retention/analyze',query);self.assertEqual(rolling['overall'][1]['retained'],3)
        query['definition']['timezone']='Asia/Kolkata';local=self.post('/api/retention/analyze',query);self.assertTrue(local['cohorts'][0]['partial_entry_bucket']);self.assertEqual(local['timezone'],'Asia/Kolkata')
        saved=self.post('/api/retention',{'title':'Web barcode return use','definition':definition})
        r=self.owner.put('/api/retention/'+saved['id']+'?project='+self.p['id']+'&revision=1',headers=CSRF,json={'title':'Renamed','definition':definition});self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.owner.delete('/api/retention/'+saved['id']+'?project='+self.p['id']+'&revision=1',headers=CSRF).status_code,409)
        self.assertEqual(self.owner.delete('/api/retention/'+saved['id']+'?project='+self.p['id']+'&revision=2',headers=CSRF).status_code,200)
    def test_catalog_types_presence_and_explicit_meanings_without_values(self):
        self.send('setting_toggled','one',properties={'toggle_state':False,'nullable':None,'secret':'private-credential-sentinel','free_text':'private-customer-sentinel'})
        self.send('setting_toggled','two',properties={'toggle_state':'off'})
        self.post('/api/catalog/describe',{'event':'setting_toggled','property':'toggle_state','meaning':'The resulting setting; off is an opt-out, not an activation.','unit':'state'})
        r=self.post('/api/catalog',{'event':'setting_toggled'});fields={f['property']:f for f in r['properties']};self.assertEqual(fields['toggle_state']['types'],{'boolean':1,'string':1});self.assertEqual(fields['nullable']['null'],1);self.assertEqual(fields['nullable']['missing'],1);self.assertEqual(fields['secret']['redacted'],1);self.assertEqual(fields['toggle_state']['meaning_source'],'owner/editor')
        self.assertNotIn('private-customer-sentinel',json.dumps(r));self.assertNotIn('private-credential-sentinel',json.dumps(r));self.assertEqual(self.post('/api/catalog',{})['events'][0]['observations'],2)
    def test_historical_evidence_is_immutable_labeled_and_not_added_to_native_counts(self):
        start=datetime.now(timezone.utc)-timedelta(days=30);end=datetime.now(timezone.utc)-timedelta(seconds=5)
        evidence={'source_url':'https://us.posthog.com/project/39922','start':start.isoformat(),'end':end.isoformat(),'telemetry_schema':1,'observation_count':175,'selected_requests':60,'flagged_eligible':12,'flagged_agree':12,'requests_with_tool_report':6,'query_sha256':'a'*64}
        id=str(uuid.uuid4());url='/api/audit/evidence/'+id
        self.post(url,evidence,'PUT');self.post(url,evidence,'PUT')
        altered={**evidence,'flagged_agree':11};self.assertEqual(self.owner.put(url+'?project='+self.p['id'],headers=CSRF,json=altered).status_code,409)
        r=self.post('/api/audit/review',{});self.assertEqual(r['agreement']['denominator'],0);self.assertEqual(r['readiness'],'inconclusive');self.assertEqual(len(r['historical_evidence']),1);self.assertFalse(r['historical_evidence'][0]['combined_with_native'])
        fixed=r['query'];id2=str(uuid.uuid4());self.post('/api/audit/evidence/'+id2,evidence,'PUT');self.assertEqual(len(self.post('/api/audit/review',fixed)['historical_evidence']),1)
        bad={**evidence,'source_url':'https://us.posthog.com/project/39922?token=private'};self.assertEqual(self.owner.put('/api/audit/evidence/'+str(uuid.uuid4())+'?project='+self.p['id'],headers=CSRF,json=bad).status_code,422)
