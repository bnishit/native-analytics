"""Owner-adopted, read-only provider release identity; never release authority."""
from contextlib import closing
from datetime import datetime
import json
import time
import uuid
from typing import Literal
from fastapi import BackgroundTasks, Depends, HTTPException, Request
from pydantic import Field, model_validator
from product_analytics import Model
from product_loop import digest
from loop_sources import railway_release

PROPOSAL='external-release-proposal'
FINAL='external-release-receipt'
LIMITS=['Provider source and deployment identity, not independent code review or CI attestation.',
        'Owner adoption records evidence; it grants no merge or deployment permission.',
        'Only the latest successful exact Railway deployment is supported; no historical deployment is inferred.']
class ReleaseProposal(Model):
    github_connection_id:uuid.UUID
    railway_connection_id:uuid.UUID
    pr_number:int=Field(gt=0,le=2147483647,strict=True)
    base:str=Field(default='main',pattern=r'^[A-Za-z0-9][A-Za-z0-9_./-]{0,150}$')
    base_sha:str|None=Field(default=None,pattern=r'^[a-f0-9]{40}$')
    head_sha:str|None=Field(default=None,pattern=r'^[a-f0-9]{40}$')
    commit:str|None=Field(default=None,pattern=r'^[a-f0-9]{40}$')
    environment:Literal['production','staging','development','test','local']
    merge_topology:Literal['single_parent','two_parent']|None=None
    @model_validator(mode='after')
    def pins_together(self):
        pins=(self.base_sha,self.head_sha,self.commit,self.merge_topology)
        if any(p is not None for p in pins) and not all(p is not None for p in pins):raise ValueError('provide all source pins or omit all of them')
        return self
class AdoptRelease(Model):
    proposal_id:uuid.UUID
    proposal_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')

class ReleaseEvidence:
    def __init__(self,app,loop,accounts,editor):
        self.repo=loop.onboarding;self.connect=loop.connect;self.accounts=accounts;self.editor=editor
        @app.get('/api/outcomes/release-sources',dependencies=[Depends(editor)])
        def sources(project:str):return {'connections':[x for x in self.repo.connections.list(project) if x['kind'] in {'github','railway'}]}
        @app.put('/api/outcomes/release-proposals/{id}',dependencies=[Depends(editor)])
        def prepare(project:str,id:uuid.UUID,req:ReleaseProposal,background:BackgroundTasks,request:Request):
            return self.prepare(project,str(id),req,background,lambda:editor(request,project))
        @app.get('/api/outcomes/release-proposals/{id}',dependencies=[Depends(editor)])
        def proposal(project:str,id:uuid.UUID):return self.proposal(project,str(id))
        @app.post('/api/outcomes/releases/{id}/adopt')
        def adopt(project:str,id:uuid.UUID,req:AdoptRelease,request:Request):return self.adopt(project,str(id),req,request)
        @app.get('/api/outcomes/releases/{id}',dependencies=[Depends(editor)])
        def receipt(project:str,id:uuid.UUID):return self.get(project,str(id),FINAL)

    def owner(self,request,project):
        user=self.accounts.session_user(request)
        self.accounts.project_role(request,project,'owner')
        return user

    def collision(self,c,project,id,kind,payload=None):
        if c.execute('SELECT 1 FROM loop_actions WHERE project=? AND id=?',(project,id)).fetchone():raise HTTPException(409,'release ID belongs to a legacy action')
        for table in ('loop_repo_receipts','loop_repo_jobs'):
            row=c.execute('SELECT kind,input_sha256 FROM '+table+' WHERE project=? AND id=?',(project,id)).fetchone()
            if row and (row['kind']!=kind or payload is not None and row['input_sha256']!=digest(payload)):
                raise HTTPException(409,'release ID belongs to another object or input')
        count=c.execute("SELECT count(*) FROM (SELECT id FROM loop_repo_receipts WHERE project=? AND kind IN (?,?) UNION SELECT id FROM loop_repo_jobs WHERE project=? AND kind IN (?,?))",(project,PROPOSAL,FINAL,project,PROPOSAL,FINAL)).fetchone()[0]
        exists=c.execute('SELECT 1 FROM loop_repo_receipts WHERE project=? AND id=? UNION SELECT 1 FROM loop_repo_jobs WHERE project=? AND id=?',(project,id,project,id)).fetchone()
        if count>=1000 and not exists:raise HTTPException(422,'release evidence object limit reached')

    def get(self,project,id,kind):
        with closing(self.connect()) as c:
            self.collision(c,project,id,kind)
            row=c.execute('SELECT receipt FROM loop_repo_receipts WHERE project=? AND id=? AND kind=?',(project,id,kind)).fetchone()
        if not row:raise HTTPException(404,'release evidence not found')
        result=json.loads(row['receipt']);field='proposal_sha256' if kind==PROPOSAL else 'receipt_sha256'
        if result.get('schema')!=kind+'-v1' or result.get('project')!=project or result.get('id')!=id or result.get(field)!=digest({k:v for k,v in result.items() if k!=field}):raise HTTPException(409,'release evidence integrity mismatch')
        return result

    def proposal(self,project,id):
        try:return self.get(project,id,PROPOSAL)
        except HTTPException as error:
            if error.status_code!=404:raise
        with closing(self.connect()) as c:
            self.collision(c,project,id,PROPOSAL)
            row=c.execute('SELECT kind FROM loop_repo_jobs WHERE project=? AND id=?',(project,id)).fetchone()
        if not row:raise HTTPException(404,'release proposal not found')
        return self.repo.job(project,id)

    def verify(self,project,payload):
        gh=self.repo.connections.resolve(project,payload['github_connection_id'],'github')
        rail=self.repo.connections.resolve(project,payload['railway_connection_id'],'railway')
        provider=self.repo.provider(project,{'connection_id':gh['id'],'connection_revision':gh['revision']})
        resolved=payload if payload['commit'] is not None else provider.external_release_pins(payload)
        source=provider.external_release_source(resolved)
        deployment=railway_release(rail,resolved['commit'],payload['environment'],guard=self.repo.source_guard(project,rail))
        if deployment.get('status')!='released' or deployment.get('verified') is not True:raise HTTPException(409,'exact successful provider deployment is unavailable')
        if datetime.fromisoformat(deployment['released_at'])<datetime.fromisoformat(source['merged_at']):raise HTTPException(409,'deployment predates the verified merge')
        guards=[self.repo.source_guard(project,x) for x in (gh,rail)]
        for guard in guards:guard()
        return {'source':source,'deployment':deployment,'connections':[{'id':x['id'],'kind':x['kind'],'revision':x['revision']} for x in (gh,rail)]},guards

    def prepare(self,project,id,req,background,authorize):
        payload=req.model_dump(mode='json');authorize()
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE');self.collision(c,project,id,PROPOSAL,payload)
        def execute():
            old=self.repo.replay(project,id,PROPOSAL,payload)
            if old:return self.get(project,id,PROPOSAL)
            evidence,guards=self.verify(project,payload)
            value={'schema':PROPOSAL+'-v1','id':id,'project':project,'status':'requires_owner_adoption','input':payload,'evidence':evidence,'limits':LIMITS}
            value['proposal_sha256']=digest(value)
            def guard(c):
                authorize()
                for check in guards:check()
                self.collision(c,project,id,PROPOSAL,payload)
            return self.repo.save(project,id,PROPOSAL,payload,value,guard=guard)
        return self.repo.queue(project,id,PROPOSAL,payload,background,execute,authorize)

    def adopt(self,project,id,req,request):
        user=self.owner(request,project);payload=req.model_dump(mode='json')
        if id==str(req.proposal_id):raise HTTPException(409,'adoption requires a different release ID')
        with self.repo.authorized(lambda:self.owner(request,project)):
            with closing(self.connect()) as c:
                self.collision(c,project,id,FINAL,payload)
                old=c.execute('SELECT 1 FROM loop_repo_receipts WHERE project=? AND id=?',(project,id)).fetchone()
            if old:return self.get(project,id,FINAL)
            proposal=self.get(project,str(req.proposal_id),PROPOSAL)
            if proposal['proposal_sha256']!=req.proposal_sha256:raise HTTPException(409,'adopt the exact release proposal hash')
            evidence,guards=self.verify(project,proposal['input'])
            if evidence!=proposal['evidence']:raise HTTPException(409,'provider evidence changed; prepare a new proposal')
            value={'schema':FINAL+'-v1','id':id,'project':project,'status':'adopted','proposal_id':str(req.proposal_id),'proposal_sha256':req.proposal_sha256,'evidence':evidence,'adopted_by':user['id'],'adopted_at':time.time(),'limits':LIMITS}
            value['receipt_sha256']=digest(value)
            def guard(c):
                self.owner(request,project)
                for check in guards:check()
                self.collision(c,project,id,FINAL,payload)
            return self.repo.save(project,id,FINAL,payload,value,guard=guard)

    def normalized(self,project,id):
        receipt=self.get(project,id,FINAL);e=receipt['evidence'];source=e['source']
        return {'action':id,'repository':source['repository'],**{k:e['deployment'][k] for k in ('provider','commit','environment','released_at','deployment_id','timestamp_basis')},'base_commit':source['base_sha'],'evidence_kind':FINAL+'-v1','approval_basis':'owner_adopted_provider_identity_only'}
