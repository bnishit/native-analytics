"""Real stored experiments and releases drive progress; the worker never executes code."""
from contextlib import closing
import json
import sqlite3
import time
import unittest
import uuid
from unittest.mock import patch
import app
import test_release_outcomes
from improvement import FollowupDefinition

class ImprovementTests(unittest.TestCase):
    def setUp(self):
        self.f=test_release_outcomes.ReleaseOutcomeTests();self.f.setUp()
        self.client=self.f.client;self.auth=self.f.auth;self.id=str(uuid.uuid4())
        self.query={'outcome_goal':self.f.binding,'dataset':{'id':self.f.dataset_id,'version':1},
            'baseline_run':self.f.baseline_run,'candidate_run':self.f.candidate_run,'release_action':self.f.candidate_action}
        self.definition={k:v for k,v in self.f.query.items() if k!='after'}
        self.definition.update(window_seconds=120,interval_seconds=60,max_checks=2)
        self.authority=patch.object(app.improvements,'authority_for',return_value={'kind':'fixture','ref':'fixture'})
        self.valid=patch.object(app.improvements,'authority_valid',return_value=True)
        self.authority.start();self.valid.start()
    def tearDown(self):
        self.authority.stop();self.valid.stop();self.f.tearDown()
    def status(self,body=None,project='alpha',headers=None):
        return self.client.post('/api/improvements/status?project='+project,headers=self.auth if headers is None else headers,json=self.query if body is None else body)
    def save(self,body=None):
        return self.client.put('/api/outcomes/followups/'+self.id+'?project=alpha',headers=self.auth,json=body or self.definition)
    def read(self,project='alpha',headers=None):
        return self.client.get('/api/outcomes/followups/'+self.id+'?project='+project,headers=self.auth if headers is None else headers)
    def work(self,at):
        with patch('time.time',return_value=at):app.improvements.work()
    def test_next_action_is_derived_from_stored_results_and_exact_release(self):
        self.assertEqual(self.status({}).json()['next_action'],'select_outcome_goal')
        self.assertEqual(self.status({'outcome_goal':self.f.binding}).json()['next_action'],'review_regression_case')
        response=self.status();self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['next_action'],'inspect_existing_followups')
        self.assertEqual(response.json()['stages'][-1]['state'],'not_checked')
        saved=self.save();self.assertEqual(saved.status_code,200,saved.text)
        self.assertEqual(self.status().json()['stages'][-1]['state'],'not_checked')
        self.assertFalse(response.json()['original_request_replay']);self.assertFalse(response.json()['deployment_authorized'])
        self.assertNotIn('\"output\":',json.dumps(response.json()))
        forged=self.status({**self.query,'passed':True});self.assertEqual(forged.status_code,422)
        changed={**self.query,'outcome_goal':{**self.f.binding,'sha256':'0'*64}}
        self.assertEqual(self.status(changed).status_code,409)
        self.f.change_receipt(self.f.candidate_action,lambda r:r['release'].update(commit='f'*40))
        result=self.status().json();self.assertNotEqual(result['next_action'],'inspect_existing_followups')
        self.assertIn('verified_release_receipt_missing',result['reasons'])
    def test_postdeployment_runs_and_missing_cases_cannot_unlock_followup(self):
        with closing(app.connect()) as c,c:
            c.execute("UPDATE regression_objects SET created=? WHERE project='alpha' AND kind='run'",(time.time(),))
        r=self.status().json();self.assertIn('experiments_did_not_precede_deployment',r['reasons'])
        self.assertEqual(self.save().status_code,409)
    def test_resume_same_id_never_renews_limits_or_overwrites(self):
        r=self.save();self.assertEqual(r.status_code,200,r.text);first=r.json()
        with patch('time.time',return_value=first['created']+30):
            again=self.save().json();self.assertEqual(again['deadline'],first['deadline']);self.assertEqual(again['checks'],0)
        self.assertEqual(self.save({**self.definition,'max_checks':3}).status_code,409)
        self.assertEqual(self.save({**self.definition,'window_seconds':86401}).status_code,422)
        self.assertEqual(self.save({**self.definition,'max_checks':25}).status_code,422)
        text=json.dumps(first);self.assertNotIn('fixture',text);self.assertNotIn('authority',text)
    def test_worker_restarts_on_frozen_pending_check_and_budget_is_durable(self):
        first=self.save().json();at=first['created']+60
        with closing(app.connect()) as c,c:
            c.execute("UPDATE outcome_followups SET state='checking',pending=?,checks=1 WHERE project='alpha' AND id=?",(at,self.id))
        with patch.object(app.release_outcomes,'compare',wraps=app.release_outcomes.compare) as compare:
            self.work(at+10)
        self.assertAlmostEqual(compare.call_args[0][1].after.as_of.timestamp(),at,places=5)
        after=self.read().json();self.assertEqual(after['checks'],1);self.assertEqual(after['state'],'watching')
        self.work(first['deadline']+30)
        ended=self.read().json();self.assertEqual(ended['checks'],2);self.assertEqual(ended['state'],'completed')
        self.work(first['deadline']+100);self.assertEqual(self.read().json()['checks'],2)
        self.assertIsNone(ended['result']['satisfied_fraction_difference']);self.assertEqual(ended['result']['status'],'unknown')
    def test_pause_cancel_and_expiry_do_not_restart_a_clock(self):
        first=self.save().json();url='/api/outcomes/followups/'+self.id+'/control?project=alpha'
        r=self.client.post(url,headers=self.auth,json={'operation':'pause','revision':1})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['state'],'paused')
        self.work(first['created']+60);self.assertEqual(self.read().json()['checks'],0)
        self.assertEqual(self.client.post(url,headers=self.auth,json={'operation':'resume','revision':1}).status_code,409)
        self.work(first['deadline']+1);self.assertEqual(self.read().json()['state'],'expired')
        self.assertEqual(self.client.post(url,headers=self.auth,json={'operation':'resume','revision':3}).status_code,409)
    def test_paused_pending_check_resumes_once_without_spending_another_slot(self):
        first=self.save().json();at=first['created']+60
        with closing(app.connect()) as c,c:
            c.execute("UPDATE outcome_followups SET state='checking',pending=?,checks=1,revision=2 WHERE project='alpha' AND id=?",(at,self.id))
        url='/api/outcomes/followups/'+self.id+'/control?project=alpha'
        self.client.post(url,headers=self.auth,json={'operation':'pause','revision':2}).raise_for_status()
        self.client.post(url,headers=self.auth,json={'operation':'resume','revision':3}).raise_for_status()
        self.work(at+1)
        resumed=self.read().json();self.assertIsNotNone(resumed['result']);self.assertEqual(resumed['checks'],1)
        self.assertEqual(resumed['state'],'watching');self.assertEqual(resumed['deadline'],first['deadline'])
        self.work(first['deadline']+1);self.assertEqual(self.read().json()['checks'],2)
        self.assertEqual(self.read().json()['state'],'completed')

    def test_pause_between_worker_selection_and_claim_does_not_expire_early(self):
        first=self.save().json()
        with closing(app.connect()) as c:row=c.execute('SELECT * FROM outcome_followups WHERE id=?',(self.id,)).fetchone()
        url='/api/outcomes/followups/'+self.id+'/control?project=alpha'
        self.client.post(url,headers=self.auth,json={'operation':'pause','revision':1}).raise_for_status()
        with patch('time.time',return_value=first['created']+60):app.improvements.check(row,first['created']+60)
        result=self.read().json();self.assertEqual(result['state'],'paused');self.assertEqual(result['checks'],0)

    def test_authority_revocation_stops_checks_without_returning_authority(self):
        first=self.save().json()
        with patch.object(app.improvements,'authority_valid',return_value=False),patch.object(app.release_outcomes,'compare') as compare:
            self.work(first['created']+60);compare.assert_not_called()
        result=self.read().json();self.assertEqual(result['state'],'revoked');self.assertEqual(result['checks'],0)
        self.assertEqual(result['notification']['kind'],'authorization_revoked');self.assertNotIn('authority',result)
    def test_empty_user_window_never_claims_an_improvement(self):
        first=self.save().json();self.work(first['deadline'])
        r=self.read().json();self.assertEqual(r['state'],'completed');self.assertEqual(r['result']['status'],'unknown')
        self.assertIsNone(r['result']['satisfied_fraction_difference']);self.assertFalse(r['result']['deployment_authorized'])
        self.assertEqual(r['result']['windows']['after']['mature_denominator'],0)
    def test_aggregate_storage_failure_is_unknown_and_still_spends_bounded_checks(self):
        first=self.save().json()
        with patch.object(app.release_outcomes,'compare',side_effect=sqlite3.OperationalError('private storage detail')):
            self.work(first['created']+60);self.work(first['deadline']+1)
        r=self.read().json();self.assertEqual(r['state'],'completed');self.assertEqual(r['checks'],2)
        self.assertEqual(r['result']['reasons'],['retained_evidence_unavailable'])
        self.assertNotIn('private storage detail',json.dumps(r))

    def test_retention_removal_during_selection_or_evaluation_does_not_kill_worker(self):
        first=self.save().json()
        with closing(app.connect()) as c,c:
            row=c.execute('SELECT * FROM outcome_followups WHERE id=?',(self.id,)).fetchone()
            c.execute('DELETE FROM outcome_followups WHERE id=?',(self.id,))
        app.improvements.check(row,first['created']+60)
        self.id=str(uuid.uuid4());first=self.save().json()
        compare=app.release_outcomes.compare
        def prune_while_evaluating(*args):
            result=compare(*args)
            with closing(app.connect()) as c,c:c.execute('DELETE FROM outcome_followups WHERE id=?',(self.id,))
            return result
        with patch.object(app.release_outcomes,'compare',side_effect=prune_while_evaluating):self.work(first['created']+60)
        self.assertEqual(self.read().status_code,404)

    def test_reconnect_lists_existing_jobs_without_creating_or_renewing(self):
        first=self.save().json();path='/api/outcomes/followups?project=alpha'
        response=self.client.get(path,headers=self.auth);self.assertEqual(response.status_code,200,response.text)
        listed=response.json();self.assertEqual(listed['followups'][0]['id'],self.id);self.assertFalse(listed['has_more'])
        self.assertEqual(listed['limit'],20);self.assertNotIn('definition',listed['followups'][0])
        self.assertNotIn('authority',json.dumps(listed));self.assertNotIn('result',listed['followups'][0])
        self.assertEqual(self.read().json(),first)
        self.assertEqual(self.client.get(path).status_code,401)
        self.assertEqual(self.client.get('/api/outcomes/followups?project=beta',headers=self.auth).json()['followups'],[])
        with closing(app.connect()) as c,c:
            for i in range(21):
                c.execute("INSERT INTO outcome_followups SELECT project,?,definition,authority,created+?,deadline,next_due,checks,pending,'completed',revision,result,signal,notification FROM outcome_followups WHERE id=?",(str(uuid.uuid4()),i+1,self.id))
        page=self.client.get(path,headers=self.auth).json()
        self.assertEqual(len(page['followups']),20);self.assertTrue(page['has_more'])
        self.assertEqual(page['followups'][0]['id'],self.id)
        self.assertEqual(page['followups'][0]['state'],'watching')
        self.assertTrue(all(r['state']=='completed' for r in page['followups'][1:]))
        self.assertEqual([r['created'] for r in page['followups'][1:]],sorted([r['created'] for r in page['followups'][1:]],reverse=True))

    def test_cli_can_discover_followups_without_remembering_a_job_id(self):
        import os,subprocess,sys,threading
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        from pathlib import Path
        paths=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(inner):
                paths.append(inner.path);self.assertEqual(inner.headers['Authorization'],'Bearer as_workspace_fixture')
                inner.send_response(200);inner.send_header('Content-Type','application/json');inner.end_headers()
                inner.wfile.write(b'{"followups":[],"has_more":false,"limit":20}')
            def log_message(self,*unused):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            env={k:v for k,v in os.environ.items() if k in {'PATH','LANG'}}
            env.update(NATIVE_ANALYTICS_URL='http://127.0.0.1:'+str(server.server_port),AFTERSPAN_WORKSPACE_KEY='as_workspace_fixture')
            run=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'cli/native_analytics.py'),'followup','--project','alpha'],env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(run.returncode,0,run.stderr);self.assertEqual(json.loads(run.stdout)['followups'],[])
            self.assertEqual(paths,['/api/outcomes/followups?project=alpha'])
        finally:server.shutdown();server.server_close();thread.join()

    def test_scope_and_project_isolation(self):
        self.save().raise_for_status()
        self.assertEqual(self.read(headers={}).status_code,401)
        self.assertEqual(self.status(headers={}).status_code,401)
        self.assertEqual(self.read(project='beta').status_code,404)
        self.assertNotEqual(self.status(project='beta').status_code,200)

if __name__=='__main__':unittest.main()
