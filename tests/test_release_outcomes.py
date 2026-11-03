"""Synthetic release receipts exercise binding; external provider proof is separate."""
from contextlib import closing
from datetime import datetime,timezone
import json
import time
import unittest
import uuid
from unittest.mock import patch
import app
import test_outcomes
from product_loop import verified_release,released_artifact

def stamp(value):return datetime.fromtimestamp(value,timezone.utc).isoformat()

class ReleaseOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.now=time.time();self.f=test_outcomes.OutcomeTests()
        with patch('time.time',return_value=self.now-1000):
            self.f.setUp();self.client=self.f.client;self.auth=self.f.admin
            self.goal={**self.f.goal,'traffic_kind':'user','min_sample_size':1}
            self.f.save(1,self.goal).raise_for_status()
        self.binding=self.binding_for(2)
        self.pulse_instance=str(uuid.uuid4())
        with patch('time.time',return_value=self.now-1000):
            self.client.put('/api/collection/sources?project=alpha',headers=self.auth,json={'source':'export_worker','environment':'staging','workflow':'document_export','interval_seconds':60}).raise_for_status()
        for i,seconds in enumerate([670,610,550,490,370,310,250,190,130]):self.health_pulse(self.now-seconds,i)

        self.case_id=str(uuid.uuid4())
        with patch('time.time',return_value=self.now-800):
            case={'title':'Checked source list','privacy_reviewed':True,'input':{'request':'list experiments in production'},'expected':{'count':2}}
            self.put('cases',self.case_id,case).raise_for_status();self.make_runs()
        self.baseline_action=self.action('0'*40,'a'*40,self.now-700)
        self.candidate_action=self.action('a'*40,'c'*40,self.now-400)
        self.task(self.now-620,'a'*40,'failed','user',self.now-600)
        self.task(self.now-320,'c'*40,'passed','user',self.now-300)
        self.query={'outcome_goal':self.binding,'release_action':self.candidate_action,'baseline_release_action':self.baseline_action,
            'baseline_run':self.baseline_run,'candidate_run':self.candidate_run,
            'before':{'start':stamp(self.now-650),'end':stamp(self.now-550),'as_of':stamp(self.now-500)},
            'after':{'start':stamp(self.now-350),'end':stamp(self.now-250),'as_of':stamp(self.now-150)}}
    def health_pulse(self,at,sequence,**changes):
        pulse={'schema_version':'collection-health-v1','source':'export_worker','environment':'staging','workflow':'document_export','instance_id':self.pulse_instance,'sequence':sequence,
               'observed_at_ms':int(at*1000),'process_started_at_ms':int((self.now-1100)*1000),'interval_seconds':60,'delivery_mode':'durable','queued':0,'capacity':100,
               'dropped':0,'rejected':0,'delivery_failures':0,'evaluator_failures':0,**changes}
        with patch('time.time',return_value=at+1):
            response=self.f.fixture.ingest({'id':str(uuid.uuid4()),'name':'analytics.pipeline','timestamp':stamp(pulse['observed_at_ms']/1000),'properties':{'afterspan_health':pulse}})
            response.raise_for_status()
    def test_collection_loss_or_missing_evidence_blocks_a_clean_release_comparison(self):
        self.health_pulse(self.now-170,10,dropped=1)
        r=self.compare().json();self.assertEqual(r['status'],'unknown');self.assertIn('after_collection_evidence_incomplete',r['reasons'])
        self.assertEqual(r['after']['collection_health']['status'],'attention');self.assertIsNone(r['satisfied_fraction_difference'])
    def tearDown(self):self.f.tearDown()
    def binding_for(self,version):
        saved=self.client.get(f'/api/outcomes/goals/{self.f.id}?project=alpha&version={version}',headers=self.auth).json()
        return {k:saved[k] for k in ['id','version','sha256']}
    def put(self,kind,id,body,revision=0):
        return self.client.put(f'/api/experiments/{kind}/{id}?project=alpha&revision={revision}',headers=self.auth,json=body)
    def make_runs(self,goal=None,candidate_output=None):
        self.dataset_id=str(uuid.uuid4())
        dataset={'title':'Fixed source checks','cases':[{'id':self.case_id,'version':1}],'outcome_goal':goal or self.binding}
        self.put('datasets',self.dataset_id,dataset).raise_for_status()
        ids=[]
        for variant,release,output in [('baseline','a'*40,{'count':0}),('candidate','c'*40,candidate_output or {'count':2})]:
            id=str(uuid.uuid4());ids.append(id)
            body={'dataset':self.dataset_id,'dataset_version':1,'variant':variant,'release_sha':release,
                'evaluator_runner_sha256':'e'*64,'privacy_reviewed':True,'results':[{'id':self.case_id,'version':1,'output':output,'duration_ms':10}]}
            self.put('runs',id,body).raise_for_status()
        self.baseline_run,self.candidate_run=ids
    def task(self,at,release,result,traffic,received):
        self.f.fixture.tid=str(uuid.uuid4());self.f.fixture.aid=str(uuid.uuid4());self.f.fixture.base=at
        with patch('time.time',return_value=received):self.f.task(self.f.check(result),release_sha=release,traffic_kind=traffic)
    def action(self,base,commit,released):
        id=str(uuid.uuid4());artifact='f'*64
        plan={'kind':'reviewed-artifact-v1','status':'prepared','repository':'owner/application','base_sha':base,'artifact_sha256':artifact,
              'measurement_revision':'d'*64,'mapping_revision':'e'*64,'files':[{'path':'src/analytics.ts'}]}
        receipt={'merged':True,'release_status':'released','merge_sha':commit,'artifact_sha256':artifact,
                 'release':{'provider':'railway','verified':True,'commit':commit,'artifact_sha256':artifact,'environment':'staging',
                            'released_at':stamp(released),'deployment_id':str(uuid.uuid4()),'timestamp_basis':'provider_status_updated_at'}}
        with closing(app.connect()) as c,c:
            c.execute('INSERT INTO loop_actions VALUES(?,?,?,?,?,?,?,?,?)',('alpha','fixture-reviewed-goal',id,str(uuid.uuid4()),1,json.dumps(plan),json.dumps(receipt),'merged',released-50))
        return id
    def compare(self,changes=None,project='alpha',headers=None):
        return self.client.post('/api/outcomes/releases/compare?project='+project,headers=headers or self.auth,json={**self.query,**(changes or {})})
    def change_receipt(self,id,update):
        with closing(app.connect()) as c,c:
            row=c.execute('SELECT receipt FROM loop_actions WHERE id=?',(id,)).fetchone();receipt=json.loads(row[0]);update(receipt)
            c.execute('UPDATE loop_actions SET receipt=? WHERE id=?',(json.dumps(receipt),id))
    def test_fixed_experiment_verified_releases_and_mature_user_outcomes_connect(self):
        self.task(self.now-315,'c'*40,'failed','synthetic',self.now-290)
        response=self.compare();self.assertEqual(response.status_code,200,response.text);r=response.json()
        self.assertEqual(r['status'],'comparable');self.assertEqual(r['reasons'],[])
        self.assertEqual(r['satisfied_fraction_difference'],1);self.assertFalse(r['deployment_authorized']);self.assertTrue(r['observational'])
        self.assertEqual(r['before']['counts']['failed'],1);self.assertEqual(r['after']['counts']['satisfied'],1)
        self.assertEqual(r['after']['mature_denominator'],1);self.assertEqual(r['after']['coverage']['traffic_excluded'],1)
        self.assertNotIn('results',r['experiment']);self.assertNotIn('content',r['experiment'])
        self.assertEqual(self.compare().json()['fingerprint'],r['fingerprint'])
    def test_returned_comparison_criteria_are_the_pinned_goal_not_its_latest_revision(self):
        self.f.save(2,{**self.goal,'min_sample_size':99}).raise_for_status()
        report=self.compare().json()
        self.assertEqual(report['goal_definition'],self.goal)
        self.assertEqual(report['goal_definition']['min_sample_size'],1)
        self.assertEqual(report['outcome_goal'],self.binding)
        self.assertEqual(report['status'],'comparable')
        self.assertFalse(report['deployment_authorized'])

    def test_adopted_legacy_proposal_identity_remains_comparable_and_listed(self):
        with closing(app.connect()) as c,c:
            for id in (self.baseline_action,self.candidate_action):
                c.execute('INSERT INTO loop_repo_receipts VALUES(?,?,?,?,?,?)',
                          ('alpha',id,'proposal','0'*64,json.dumps({'adopted_action_id':id}),self.now-900))
        response=self.compare();self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['status'],'comparable')
        response=self.client.get('/api/outcomes/releases?project=alpha&environment=staging',headers=self.auth)
        self.assertEqual(response.status_code,200,response.text)
        releases=response.json()['releases']
        self.assertEqual({r['action'] for r in releases},{self.baseline_action,self.candidate_action})
        self.assertTrue(all(r['evidence_kind']=='legacy_tracking_artifact' for r in releases))

    def test_external_kind_collision_never_falls_back_to_legacy_release(self):
        for kind in ('external-release-proposal','external-release-receipt'):
            with self.subTest(kind=kind):
                with closing(app.connect()) as c,c:
                    c.execute('INSERT OR REPLACE INTO loop_repo_receipts VALUES(?,?,?,?,?,?)',
                              ('alpha',self.candidate_action,kind,'0'*64,'{}',self.now-900))
                self.assertEqual(self.compare().status_code,409)
                self.assertEqual(self.client.get('/api/outcomes/releases?project=alpha&environment=staging',headers=self.auth).status_code,409)

    def test_both_provider_receipts_are_required_and_artifact_binding_cannot_change(self):
        self.change_receipt(self.baseline_action,lambda r:r.update(release_status='release_pending'))
        r=self.compare().json();self.assertEqual(r['status'],'unknown');self.assertIn('baseline_verified_release_receipt_missing',r['reasons']);self.assertIsNone(r['satisfied_fraction_difference'])
        self.change_receipt(self.candidate_action,lambda r:r['release'].update(artifact_sha256='b'*64))
        r=self.compare().json();self.assertIn('verified_release_receipt_missing',r['reasons']);self.assertNotIn('after',r)
    def test_branch_head_experiment_or_late_experiment_cannot_be_predeployment_proof(self):
        with closing(app.connect()) as c,c:
            row=c.execute("SELECT definition FROM regression_objects WHERE kind='run' AND id=?",(self.candidate_run,)).fetchone()
            body=json.loads(row[0]);body['release_sha']='b'*40
        new=str(uuid.uuid4());self.put('runs',new,body).raise_for_status()
        r=self.compare({'candidate_run':new}).json();self.assertIn('candidate_experiment_commit_unbound',r['reasons']);self.assertIn('experiment_did_not_precede_verified_deployment',r['reasons'])
    def test_missing_traffic_labels_prevent_a_clean_user_comparison(self):
        self.task(self.now-310,'c'*40,'passed','unknown',self.now-280)
        r=self.compare().json();self.assertIn('after_evidence_incomplete',r['reasons']);self.assertEqual(r['after']['coverage']['traffic_unknown'],1)
        self.assertIsNone(r['after']['satisfied_fraction'])
    def test_goal_hash_and_dataset_goal_binding_are_not_caller_assertions(self):
        r=self.compare({'outcome_goal':{**self.binding,'sha256':'0'*64}});self.assertEqual(r.status_code,409)
        with patch('time.time',return_value=self.now-900):self.f.save(2,{**self.goal,'min_sample_size':2}).raise_for_status()
        third=self.binding_for(3);r=self.compare({'outcome_goal':third});self.assertEqual(r.status_code,409)
        dataset={'title':'Foreign binding','cases':[{'id':self.case_id,'version':1}],'outcome_goal':{**self.binding,'sha256':'0'*64}}
        self.assertEqual(self.put('datasets',str(uuid.uuid4()),dataset).status_code,409)
    def test_sample_threshold_and_user_population_come_from_the_pinned_goal(self):
        for version,changes,reason in [(3,{'min_sample_size':2},'after_sample_below_goal_minimum'),(4,{'traffic_kind':'all'},'normal_user_population_not_selected')]:
            with patch('time.time',return_value=self.now-900):self.f.save(version-1,{**self.goal,**changes}).raise_for_status()
            binding=self.binding_for(version)
            with patch('time.time',return_value=self.now-800):self.make_runs(binding)
            r=self.compare({'outcome_goal':binding,'baseline_run':self.baseline_run,'candidate_run':self.candidate_run}).json()
            self.assertEqual(r['status'],'unknown');self.assertIn(reason,r['reasons'])
    def test_overlap_unmature_followup_and_future_windows_do_not_pass(self):
        after={**self.query['after'],'start':stamp(self.now-410)}
        r=self.compare({'after':after}).json();self.assertIn('after_entries_precede_release',r['reasons'])
        before={**self.query['before'],'end':stamp(self.now-410),'as_of':stamp(self.now-350)}
        r=self.compare({'before':before}).json();self.assertIn('baseline_followup_overlaps_release',r['reasons'])
        after={**self.query['after'],'as_of':stamp(self.now-240)}
        r=self.compare({'after':after}).json();self.assertIn('after_followup_window_not_mature',r['reasons'])
        after={**self.query['after'],'as_of':stamp(self.now+100)}
        self.assertEqual(self.compare({'after':after}).status_code,422)
    def test_shared_release_refactor_preserves_existing_measurement_binding(self):
        with closing(app.connect()) as c:action=app.product_loop.action(c,'alpha',self.candidate_action)
        evidence={'measurement_revision':'d'*64,'mapping_revision':'e'*64,'environment':'staging'}
        self.assertIsNotNone(verified_release(action,evidence));self.assertIsNotNone(released_artifact(action,'staging'))
        self.assertIsNone(verified_release(action,{**evidence,'measurement_revision':'c'*64}))
        self.assertIsNone(verified_release(action,{**evidence,'mapping_revision':'c'*64}))
        self.assertIsNone(released_artifact(action,'production'))
    def test_scoped_listing_and_access_do_not_export_fixture_bodies(self):
        listed=self.client.get('/api/outcomes/releases?project=alpha&environment=staging&limit=1',headers=self.auth).json()
        self.assertEqual(len(listed['releases']),1);self.assertTrue(listed['has_more']);self.assertNotIn('plan',listed['releases'][0])
        second=self.client.get('/api/outcomes/releases?project=alpha&environment=staging&limit=1&offset=1',headers=self.auth).json()
        self.assertEqual(second['releases'][0]['action'],self.baseline_action)
        self.assertEqual(self.compare(project='beta').status_code,404)
        self.assertEqual(self.client.post('/api/outcomes/releases/compare?project=alpha',json=self.query).status_code,401)
        self.assertEqual(self.compare(headers=self.f.fixture.fixture.a).status_code,401)
