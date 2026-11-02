import unittest
from release_evidence import ReleaseProposal

class ReleaseEvidenceTests(unittest.TestCase):
    def test_exact_identity_is_required(self):
        with self.assertRaises(ValueError):ReleaseProposal.model_validate({})

import base64
import copy
import json
import os
import time
import uuid
from contextlib import closing
from unittest.mock import patch
from fastapi import HTTPException
from pydantic import SecretStr
import app
import test_workspaces
from loop_github import GitHub
from release_evidence import PROPOSAL,FINAL

class SourceProofTests(unittest.TestCase):
    def setUp(self):
        self.pins={'base':'main','base_sha':'a'*40,'head_sha':'b'*40,'commit':'c'*40,'pr_number':192,'merge_topology':'single_parent'}
        self.pr={'number':192,'merged':True,'merge_commit_sha':'c'*40,'merged_at':'2026-01-01T00:00:00Z','changed_files':1,
                 'base':{'sha':'a'*40,'ref':'main','repo':{'id':1,'full_name':'org/app'}},'head':{'sha':'b'*40,'repo':{'id':1,'full_name':'org/app'}}}
        self.before={'file.ts':{'path':'file.ts','type':'blob','mode':'100644','sha':'d'*40}}
        self.after={'file.ts':{'path':'file.ts','type':'blob','mode':'100755','sha':'e'*40}}
        self.rows=[{'filename':'file.ts','status':'modified','sha':'e'*40,'patch':'private patch must not persist'}]
        self.g=GitHub('org/app','synthetic');self.calls=[]
        def request(method,path,**kw):
            self.assertEqual(method,'GET');self.calls.append(path)
            if path=='':return {'id':1,'full_name':'org/app','archived':False}
            if path=='/pulls/192':return copy.deepcopy(self.pr)
            if '/files?' in path:
                page=int(path.rsplit('=',1)[1]);return copy.deepcopy(self.rows[(page-1)*100:page*100])
            if '/git/commits/' in path:
                sha=path.rsplit('/',1)[1]
                return {'sha':sha,'parents':[{'sha':x} for x in (['a'*40,'b'*40] if self.pins['merge_topology']=='two_parent' else ['a'*40])], 'tree':{'sha':('f' if sha=='a'*40 else '1')*40}}
            if '/git/trees/' in path:
                before='/'+('f'*40) in path
                return {'sha':('f' if before else '1')*40,'truncated':False,'tree':copy.deepcopy(list((self.before if before else self.after).values()))}
            raise AssertionError(path)
        self.g._request=request
    def test_derived_pins_use_authoritative_parents_and_same_strict_verifier(self):
        request={k:v for k,v in self.pins.items() if k not in {'base_sha','head_sha','commit','merge_topology'}}
        pins=self.g.external_release_pins(request)
        self.assertEqual(pins,self.pins)
        self.assertEqual(self.g.external_release_source(pins)['commit'],'c'*40)
        self.pr['head']['sha']='d'*40
        with self.assertRaises(HTTPException):self.g.external_release_source(pins)
        self.pr['head']['sha']='b'*40;self.pr['base']['ref']='other'
        with self.assertRaises(HTTPException):self.g.external_release_source(pins)

    def test_exact_squash_and_two_parent_metadata_only_proof(self):
        for topology in ['single_parent','two_parent']:
            self.pins['merge_topology']=topology
            proof=self.g.external_release_source(self.pins)
            self.assertEqual(proof['changed_files'],1);self.assertEqual(proof['manifest'][0]['before']['mode'],'100644')
            self.assertNotIn('private patch',json.dumps(proof));self.assertTrue(any('page=2' in p for p in self.calls))
    def test_wrong_repository_hash_and_tree_omission_fail(self):
        self.pr['head']['repo']['id']=2
        with self.assertRaises(HTTPException):self.g.external_release_source(self.pins)
        self.pr['head']['repo']['id']=1
        self.after['hidden.ts']={**self.after['file.ts'],'path':'hidden.ts'}
        with self.assertRaises(HTTPException):self.g.external_release_source(self.pins)
        del self.after['hidden.ts'];self.rows[0]['sha']='f'*40
        with self.assertRaises(HTTPException):self.g.external_release_source(self.pins)
    def test_truncated_tree_and_missing_page_fail(self):
        original=self.g._request
        self.g._request=lambda method,path,**kw: ({'truncated':True,'tree':[]} if '/trees/' in path else original(method,path,**kw))
        with self.assertRaises(HTTPException):self.g.external_release_source(self.pins)
        self.g._request=original;self.pr['changed_files']=101
        with self.assertRaises(HTTPException):self.g.external_release_source(self.pins)
    def test_renames_additions_and_deletions_match_exact_tree(self):
        self.after={'renamed.ts':{**self.after['file.ts'],'path':'renamed.ts'}}
        self.rows=[{'filename':'renamed.ts','previous_filename':'file.ts','status':'renamed','sha':'e'*40}]
        proof=self.g.external_release_source(self.pins);self.assertEqual(len(proof['manifest']),2)
        self.rows=[{'filename':'file.ts','status':'removed','sha':'d'*40},{'filename':'renamed.ts','status':'added','sha':'e'*40}];self.pr['changed_files']=2
        self.assertEqual(self.g.external_release_source(self.pins)['changed_files'],2)
    def test_complete_two_hundred_file_pagination_and_overflow(self):
        self.before={};self.after={f'f{i}':{'path':f'f{i}','type':'blob','mode':'100644','sha':'e'*40} for i in range(200)}
        self.rows=[{'filename':p,'status':'added','sha':'e'*40} for p in self.after];self.pr['changed_files']=200
        self.assertEqual(self.g.external_release_source(self.pins)['changed_files'],200)
        self.assertTrue(any('page=3' in p for p in self.calls))
        self.pr['changed_files']=201
        with self.assertRaises(HTTPException):self.g.external_release_source(self.pins)

class ReleaseAPITests(unittest.TestCase):
    def setUp(self):
        self.f=test_workspaces.WorkspaceTests();self.f.setUp()
        self.crypto=patch.dict(os.environ,{'AFTERSPAN_CONNECTION_KEY':base64.urlsafe_b64encode(b'x'*32).decode()});self.crypto.start()
        self.owner,self.uid=self.f.user('release-owner@example.test');self.w=self.f.workspace(self.owner);self.p=self.f.project(self.owner,self.w)['id']
        self.repo=app.product_loop.onboarding
        self.gh=self.repo.connections.put(self.p,str(uuid.uuid4()),0,'github',{'repository':'org/app'},SecretStr('synthetic'))
        self.rail=self.repo.connections.put(self.p,str(uuid.uuid4()),0,'railway',{k:str(uuid.uuid4()) for k in ('project_id','service_id','environment_id')},SecretStr('synthetic'))
        self.payload={'github_connection_id':self.gh['id'],'railway_connection_id':self.rail['id'],'pr_number':192,'base':'main','base_sha':'a'*40,'head_sha':'b'*40,'commit':'c'*40,'environment':'production','merge_topology':'single_parent'}
        self.source={'repository':'org/app','repository_id':1,'base_sha':'a'*40,'commit':'c'*40,'merged_at':'2026-01-01T00:00:00+00:00','manifest':[]}
        self.deployment={'provider':'railway','verified':True,'status':'released','commit':'c'*40,'environment':'production','deployment_id':str(uuid.uuid4()),'released_at':'2026-01-02T00:00:00+00:00','timestamp_basis':'provider_status_updated_at'}
        self.gp=patch('loop_github.GitHub.external_release_source',return_value=self.source);self.gp.start()
        self.rp=patch('release_evidence.railway_release',return_value=self.deployment);self.rp.start()
        self.id=str(uuid.uuid4());self.final=str(uuid.uuid4())
    def tearDown(self):self.rp.stop();self.gp.stop();self.crypto.stop();self.f.tearDown()
    def call(self,method,path,body=None,client=None,headers=None):return (client or self.owner).request(method,'/api/outcomes/'+path+'?project='+self.p,json=body,headers=headers or test_workspaces.CSRF)
    def proposal(self):
        r=self.call('PUT','release-proposals/'+self.id,self.payload);self.assertEqual(r.status_code,202,r.text)
        result=self.call('GET','release-proposals/'+self.id);self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['status'],'requires_owner_adoption',result.text)
        return result.json()
    def adopt(self,proposal):return self.call('POST','releases/'+self.final+'/adopt',{'proposal_id':self.id,'proposal_sha256':proposal['proposal_sha256']})
    def test_pm_input_resolves_pins_and_reverifies_without_changing_request_identity(self):
        original=dict(self.payload)
        for field in ('base_sha','head_sha','commit','merge_topology'):del self.payload[field]
        with patch('loop_github.GitHub.external_release_pins',return_value=original) as derive:
            proposal=self.proposal()
            self.assertIsNone(proposal['input']['commit'])
            self.assertEqual(proposal['evidence']['source']['commit'],'c'*40)
            self.assertEqual(self.adopt(proposal).status_code,200)
            self.assertEqual(derive.call_count,2)
        self.payload['commit']='c'*40
        self.assertEqual(self.call('PUT','release-proposals/'+str(uuid.uuid4()),self.payload).status_code,422)
        self.payload=original
        self.assertEqual(self.call('PUT','release-proposals/'+self.id,self.payload).status_code,409)

    def test_prepare_adopt_read_idempotency_and_catalog(self):
        proposal=self.proposal();self.assertNotIn('synthetic',json.dumps(proposal))
        self.assertEqual(self.call('GET','releases/'+self.id).status_code,409)
        result=self.adopt(proposal);self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(self.adopt(proposal).json(),result.json());self.assertEqual(result.json()['adopted_by'],self.uid)
        catalog=self.owner.get('/api/outcomes/releases?project='+self.p+'&environment=production').json()
        self.assertEqual(catalog['releases'][0]['evidence_kind'],'external-release-receipt-v1')
        self.assertEqual(catalog['releases'][0]['base_commit'],'a'*40)
        sources=self.call('GET','release-sources').json();self.assertEqual(len(sources['connections']),2);self.assertNotIn('secret',json.dumps(sources))
    def test_changed_deployment_and_connection_cannot_be_adopted(self):
        p=self.proposal();self.deployment['deployment_id']=str(uuid.uuid4())
        self.assertEqual(self.adopt(p).status_code,409)
        self.repo.connections.revoke(self.p,self.gh['id'],1)
        self.assertEqual(self.adopt(p).status_code,409)
    def test_owner_session_not_operator_or_delegated_key(self):
        p=self.proposal();body={'proposal_id':self.id,'proposal_sha256':p['proposal_sha256']}
        key=self.f.key(self.owner,self.w,'owner')
        r=self.call('POST','releases/'+self.final+'/adopt',body,client=self.f.client,headers={'Authorization':'Bearer '+key['key'],**test_workspaces.CSRF})
        self.assertEqual(r.status_code,403,r.text)
        r=self.call('POST','releases/'+self.final+'/adopt',body,client=self.f.client,headers=self.f.fixture.admin)
        self.assertEqual(r.status_code,401,r.text)
    def test_final_role_revocation_blocks_receipt(self):
        p=self.proposal()
        def revoke(*a,**kw):
            with closing(app.connect()) as c,c:c.execute("UPDATE memberships SET role='reader' WHERE workspace=? AND user_id=?",(self.w,self.uid))
            return self.deployment
        with patch('release_evidence.railway_release',side_effect=revoke):self.assertEqual(self.adopt(p).status_code,403)
        with closing(app.connect()) as c:self.assertIsNone(c.execute('SELECT 1 FROM loop_repo_receipts WHERE project=? AND id=?',(self.p,self.final)).fetchone())
    def test_transaction_guard_rechecks_owner_after_provider_reads(self):
        p=self.proposal();original=self.repo.save
        def between(*args,**kwargs):
            with closing(app.connect()) as c,c:c.execute("UPDATE memberships SET role='reader' WHERE workspace=? AND user_id=?",(self.w,self.uid))
            return original(*args,**kwargs)
        with patch.object(self.repo,'save',side_effect=between):self.assertEqual(self.adopt(p).status_code,403)
        with closing(app.connect()) as c:self.assertIsNone(c.execute('SELECT 1 FROM loop_repo_receipts WHERE project=? AND id=?',(self.p,self.final)).fetchone())

    def test_collision_wrong_hash_and_other_job_never_adopt(self):
        p=self.proposal()
        self.assertEqual(self.call('POST','releases/'+self.id+'/adopt',{'proposal_id':self.id,'proposal_sha256':p['proposal_sha256']}).status_code,409)
        self.assertEqual(self.call('POST','releases/'+self.final+'/adopt',{'proposal_id':self.id,'proposal_sha256':'0'*64}).status_code,409)
        alien=str(uuid.uuid4())
        with closing(app.connect()) as c,c:c.execute('INSERT INTO loop_repo_jobs VALUES(?,?,?,?,?,?,?)',(self.p,alien,'scan','0'*64,'pending',None,time.time()))
        self.assertEqual(self.call('GET','release-proposals/'+alien).status_code,409)
        self.assertEqual(self.call('PUT','release-proposals/'+alien,self.payload).status_code,409)

class ExternalComparisonTests(unittest.TestCase):
    def test_external_identity_preserves_user_sample_and_goal_gates(self):
        import test_release_outcomes
        from product_loop import digest
        f=test_release_outcomes.ReleaseOutcomeTests();f.setUp()
        try:
            for field in ('baseline_release_action','release_action'):
                legacy=f.query[field]
                with closing(app.connect()) as c:
                    action=app.product_loop.action(c,'alpha',legacy)
                id=str(uuid.uuid4());release=action['receipt']['release']
                receipt={'schema':FINAL+'-v1','id':id,'project':'alpha','evidence':{'source':{'repository':'org/app','base_sha':action['plan']['base_sha']},'deployment':{**release,'provider':'railway','timestamp_basis':'provider_status_updated_at'}},'status':'adopted'}
                receipt['receipt_sha256']=digest(receipt)
                app.product_loop.onboarding.save('alpha',id,FINAL,{},receipt)
                f.query[field]=id
            result=f.client.post('/api/outcomes/releases/compare?project=alpha',headers=f.auth,json=f.query)
            self.assertEqual(result.status_code,200,result.text);self.assertEqual(result.json()['status'],'comparable')
            self.assertFalse(result.json()['deployment_authorized'])
            with closing(app.connect()) as c,c:c.execute("DELETE FROM events WHERE project='alpha'")
            result=f.client.post('/api/outcomes/releases/compare?project=alpha',headers=f.auth,json=f.query).json()
            self.assertEqual(result['status'],'unknown');self.assertIsNone(result['satisfied_fraction_difference'])
            self.assertIn('before_sample_below_goal_minimum',result['reasons']);self.assertIn('after_sample_below_goal_minimum',result['reasons'])
            self.assertEqual(result['before']['mature_denominator'],0)
        finally:f.tearDown()
