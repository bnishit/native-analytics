"""Reviewed regression fixtures and deterministic, version-bound comparisons."""
from collections import Counter
from contextlib import closing
import json
import time
import uuid
from typing import Any,Literal
from fastapi import Depends,HTTPException
from pydantic import Field,StrictBool,field_validator,model_validator
from product_analytics import Model
from trace_explorer import TraceOpen
from outcomes import OutcomeBinding
from catalog_replay import CatalogReplay

from sdk.python.afterspan_experiments import reviewed_json,canonical,digest

class Reviewed(Model):
    privacy_reviewed:StrictBool
    @field_validator('privacy_reviewed')
    @classmethod
    def reviewed(cls,value):
        if not value:raise ValueError('review fixture content before saving')
        return value

class CaseDefinition(Reviewed):
    title:str=Field(min_length=1,max_length=100)
    input:dict[str,Any]
    expected:dict[str,Any]
    evaluator:Literal['json_equal','json_subset']='json_equal'
    evaluator_version:int=Field(default=1,ge=1,le=1,strict=True)
    source:TraceOpen|None=None
    @field_validator('title','input','expected')
    @classmethod
    def safe(cls,value):return reviewed_json(value)
    @model_validator(mode='after')
    def frozen(self):
        if self.input.get('schema_version')=='catalog-check-replay-v1':CatalogReplay.model_validate(self.input)
        if self.source and any(getattr(self.source,k) is None for k in ['start','end','as_of']):raise ValueError('source trace requires its exact frozen start/end/as_of query')
        if self.source:reviewed_json(self.source.search)
        if self.evaluator=='json_subset' and not self.expected:raise ValueError('subset checks require at least one expected fact')
        return self

class CaseRef(Model):
    id:uuid.UUID
    version:int=Field(ge=1,le=1000,strict=True)

class DatasetDefinition(Model):
    title:str=Field(min_length=1,max_length=100)
    cases:list[CaseRef]=Field(min_length=1,max_length=100)
    minimum_pass_rate:float=Field(default=1.0,gt=0,le=1,allow_inf_nan=False,strict=True)
    outcome_goal:OutcomeBinding|None=None
    @field_validator('title')
    @classmethod
    def safe(cls,value):return reviewed_json(value)
    @model_validator(mode='after')
    def unique(self):
        if len({c.id for c in self.cases})!=len(self.cases):raise ValueError('a case appears only once in a dataset')
        return self

class CaseResult(CaseRef):
    output:dict[str,Any]|None=None
    error_code:Literal['timeout','unavailable','internal','unsupported']|None=None
    duration_ms:float=Field(ge=0,le=86400000,strict=True,allow_inf_nan=False)
    @field_validator('output')
    @classmethod
    def safe(cls,value):return reviewed_json(value)
    @model_validator(mode='after')
    def one(self):
        if (self.output is None)==(self.error_code is None):raise ValueError('supply output or a fixed error code')
        return self

class ExperimentRun(Reviewed):
    dataset:uuid.UUID
    dataset_version:int=Field(ge=1,le=1000,strict=True)
    variant:str=Field(pattern=r'^[a-z][a-z0-9_.-]{0,63}$')
    release_sha:str=Field(pattern=r'^[a-f0-9]{40}$')
    evaluator_runner_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    configuration:dict[str,Any]=Field(default_factory=dict)
    results:list[CaseResult]=Field(max_length=100)
    @field_validator('configuration')
    @classmethod
    def safe(cls,value):return reviewed_json(value)
    @model_validator(mode='after')
    def unique(self):
        if len({c.id for c in self.results})!=len(self.results):raise ValueError('a case can appear only once in a run')
        return self

class Comparison(Model):
    baseline:uuid.UUID
    candidate:uuid.UUID

def matches(expected,output,subset=False):
    # Python equality alone treats true and 1 as equal. JSON facts do not.
    if type(expected) is not type(output):return False
    if isinstance(expected,dict):
        return (subset or set(expected)==set(output)) and all(k in output and matches(v,output[k],subset) for k,v in expected.items())
    if isinstance(expected,list):return len(expected)==len(output) and all(matches(a,b,subset) for a,b in zip(expected,output))
    return expected==output

def differences(expected,output,subset=False,path='$'):
    if matches(expected,output,subset):return []
    preview=lambda value:canonical(value)[:200]
    if type(expected) is type(output) and isinstance(expected,dict):
        rows=[]
        for key in sorted(set(expected)|set(output)):
            if subset and key not in expected:continue
            label=path+'.'+key
            if key not in expected or key not in output:rows.append({'path':label,'expected':preview(expected[key]) if key in expected else 'absent','observed':preview(output[key]) if key in output else 'absent'})
            else:rows+=differences(expected[key],output[key],subset,label)
        return rows
    if type(expected) is type(output) and isinstance(expected,list) and len(expected)==len(output):
        return [row for i,(a,b) in enumerate(zip(expected,output)) for row in differences(a,b,subset,path+'['+str(i)+']')]
    return [{'path':path,'expected':preview(expected),'observed':preview(output)}]

class Regressions:
    @staticmethod
    def init_db(c):
        c.execute('''CREATE TABLE IF NOT EXISTS regression_objects(project TEXT NOT NULL REFERENCES projects(id),
          kind TEXT NOT NULL,id TEXT NOT NULL,version INTEGER NOT NULL,definition TEXT NOT NULL,
          sha256 TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,PRIMARY KEY(project,kind,id,version))''')

    def __init__(self,app,connect,editor,explorer,retention_days,outcomes):
        self.connect=connect;self.explorer=explorer;self.retention_days=retention_days;self.outcomes=outcomes
        # Inputs/outputs require editor authority, separate from aggregate/audit access.
        @app.get('/api/experiments/cases',dependencies=[Depends(editor)])
        def cases(project:str):return self.list(project,'case')
        @app.get('/api/experiments/cases/{id}',dependencies=[Depends(editor)])
        def case(project:str,id:uuid.UUID,version:int=1):return self.get(project,'case',str(id),version)
        @app.put('/api/experiments/cases/{id}',dependencies=[Depends(editor)])
        def save_case(project:str,id:uuid.UUID,revision:int,req:CaseDefinition):return self.save_case(project,str(id),revision,req)
        @app.get('/api/experiments/datasets',dependencies=[Depends(editor)])
        def datasets(project:str):return self.list(project,'dataset')
        @app.get('/api/experiments/datasets/{id}',dependencies=[Depends(editor)])
        def dataset(project:str,id:uuid.UUID,version:int=1):return self.get(project,'dataset',str(id),version)
        @app.put('/api/experiments/datasets/{id}',dependencies=[Depends(editor)])
        def save_dataset(project:str,id:uuid.UUID,revision:int,req:DatasetDefinition):return self.save_dataset(project,str(id),revision,req)
        @app.get('/api/experiments/runs',dependencies=[Depends(editor)])
        def runs(project:str):return self.list(project,'run')
        @app.get('/api/experiments/runs/{id}',dependencies=[Depends(editor)])
        def run(project:str,id:uuid.UUID):return self.run_report(project,str(id))
        @app.put('/api/experiments/runs/{id}',dependencies=[Depends(editor)])
        def save_run(project:str,id:uuid.UUID,req:ExperimentRun):
            dataset=self.validate_run(project,req)
            self.save(project,'run',str(id),0,req.model_dump(mode='json'),dataset['expires'])
            return self.run_report(project,str(id))
        @app.post('/api/experiments/compare',dependencies=[Depends(editor)])
        def compare(project:str,req:Comparison):return self.compare(project,str(req.baseline),str(req.candidate))

    def get(self,project,kind,id,version=1):
        with closing(self.connect()) as c:r=c.execute('SELECT * FROM regression_objects WHERE project=? AND kind=? AND id=? AND version=?',(project,kind,id,version)).fetchone()
        if not r:raise HTTPException(404,'version not found in this project')
        if r['expires']<=time.time():raise HTTPException(410,'fixture or run expired; do not infer reproducibility')
        definition=json.loads(r['definition'])
        if digest(definition)!=r['sha256']:raise HTTPException(409,'stored definition does not match its pinned hash')
        replay=CatalogReplay.model_validate(definition['input']).availability() if kind=='case' and definition['input'].get('schema_version')=='catalog-check-replay-v1' else None
        return {**dict(r),'definition':definition,**({'replay':replay} if replay else {})}

    def list(self,project,kind):
        with closing(self.connect()) as c:
            rows=c.execute('''SELECT o.* FROM regression_objects o WHERE project=? AND kind=? AND expires>? AND version=
              (SELECT MAX(version) FROM regression_objects x WHERE x.project=o.project AND x.kind=o.kind AND x.id=o.id)
              ORDER BY created DESC,id LIMIT 100''',(project,kind,time.time())).fetchall()
            bindings={(r['id'],r['version']):{'outcome_goal':json.loads(r['binding']) if r['binding'] else None,'dataset_title':r['title']} for r in c.execute("SELECT id,version,json_extract(definition,'$.title') AS title,json_extract(definition,'$.outcome_goal') AS binding FROM regression_objects WHERE project=? AND kind='dataset' AND expires>?",(project,time.time()))} if kind=='run' else {}
        items=[]
        for row in rows:
            definition=json.loads(row['definition'])
            summary={k:v for k,v in definition.items() if k not in {'input','expected','results','configuration','cases'}}
            if kind=='case' and definition['input'].get('schema_version')=='catalog-check-replay-v1':summary['replay']=CatalogReplay.model_validate(definition['input']).availability()
            if kind=='dataset':summary['case_count']=len(definition['cases'])
            item={**dict(row),'definition':summary}
            if kind=='run':item.update(bindings.get((definition['dataset'],definition['dataset_version']),{'outcome_goal':None,'dataset_title':None}))
            items.append(item)
        return {'items':items,'content_included':False}

    def save(self,project,kind,id,revision,definition,expires=None):
        if revision<0:raise HTTPException(422,'revision must be nonnegative')
        payload=canonical(definition);now=time.time()
        if len(payload.encode())>256000:raise HTTPException(422,'regression definition exceeds 256 KB')
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute('SELECT 1 FROM projects WHERE id=?',(project,)).fetchone():raise HTTPException(404,'project not found')
            existing=c.execute('SELECT definition,expires FROM regression_objects WHERE project=? AND kind=? AND id=? AND version=?',(project,kind,id,revision+1)).fetchone()
            if existing and existing['definition']==payload:
                if existing['expires']<=now:raise HTTPException(410,'version expired; retry cannot renew retention')
            else:
                latest=c.execute('SELECT MAX(version) FROM regression_objects WHERE project=? AND kind=? AND id=?',(project,kind,id)).fetchone()[0] or 0
                if revision!=latest:raise HTTPException(409,'version changed; read the current definition')
                if c.execute('SELECT COUNT(*) FROM regression_objects WHERE project=? AND kind=?',(project,kind)).fetchone()[0]>=1000 or not latest and c.execute('SELECT COUNT(DISTINCT id) FROM regression_objects WHERE project=? AND kind=?',(project,kind)).fetchone()[0]>=100:raise HTTPException(422,'regression object capacity reached')
                c.execute('INSERT INTO regression_objects VALUES(?,?,?,?,?,?,?,?)',(project,kind,id,revision+1,payload,digest(definition),now,min(expires if expires is not None else float('inf'),now+self.retention_days*86400)))
        return self.get(project,kind,id,revision+1)

    def save_case(self,project,id,revision,req):
        definition=req.model_dump(mode='json')
        if req.source:
            opened=self.explorer.search(project,req.source,req.source.reference)
            trace=opened['trace']
            if trace['status'] not in {'failed','gaps'}:raise HTTPException(422,'choose a failed or incomplete trace for a regression source')
            # Preserve the exact query and safe diagnosis, never raw request content.
            definition['source']=dict(opened['query'],reference=trace['reference'])
        return self.save(project,'case',id,revision,definition)

    def save_dataset(self,project,id,revision,req):
        definition=req.model_dump(mode='json');expires=[]
        if req.outcome_goal:
            goal=self.outcomes.get(project,str(req.outcome_goal.id),req.outcome_goal.version)
            if goal['sha256']!=req.outcome_goal.sha256:raise HTTPException(409,'outcome goal bytes do not match their pinned hash')
        for ref in req.cases:
            case=self.get(project,'case',str(ref.id),ref.version);expires.append(case['expires'])
            next(r for r in definition['cases'] if r['id']==str(ref.id))['sha256']=case['sha256']
        return self.save(project,'dataset',id,revision,definition,min(expires))

    def validate_run(self,project,req):
        dataset=self.get(project,'dataset',str(req.dataset),req.dataset_version)
        expected={(c['id'],c['version']) for c in dataset['definition']['cases']}
        if any((str(r.id),r.version) not in expected for r in req.results):raise HTTPException(422,'run results must use the exact dataset case versions')
        return dataset

    def run_report(self,project,id):
        saved=self.get(project,'run',id);run=ExperimentRun.model_validate(saved['definition'])
        dataset=self.validate_run(project,run);results={(str(r.id),r.version):r for r in run.results};rows=[]
        for ref in dataset['definition']['cases']:
            case=self.get(project,'case',ref['id'],ref['version'])
            if case['sha256']!=ref['sha256']:raise HTTPException(409,'dataset case bytes do not match their pinned hash')
            result=results.get((ref['id'],ref['version']));state='missing';duration=None;diff=[]
            if result:
                duration=result.duration_ms
                state='error' if result.error_code else 'passed' if matches(case['definition']['expected'],result.output,case['definition']['evaluator']=='json_subset') else 'failed'
                if state=='failed':diff=differences(case['definition']['expected'],result.output,case['definition']['evaluator']=='json_subset')
            rows.append({'case':ref['id'],'version':ref['version'],'title':case['definition']['title'],'state':state,'duration_ms':duration,
                         'evaluator':case['definition']['evaluator'],'evaluator_version':case['definition']['evaluator_version'],
                         'differences':diff[:20],'difference_count':len(diff),'previews_may_be_truncated':True})
        counts=Counter(r['state'] for r in rows);complete=counts['missing']==0
        return {'run':saved,'dataset_sha256':dataset['sha256'],'dataset':dataset,'cases':rows,
                'counts':{k:counts[k] for k in ['passed','failed','error','missing']},'complete':complete,
                'pass_rate':counts['passed']/len(rows),'limits':['Fixed reviewed expected facts, not independent source verification or subjective answer quality.','Every pinned case remains in the denominator. Errors and missing cases cannot pass.','A declared release SHA is not deployment proof. Experiment results do not authorize a release.']}

    def compare(self,project,baseline,candidate):
        return compare_runs(self.run_report(project,baseline),self.run_report(project,candidate))

def compare_runs(old,new):
    baseline=old['run']['id'];candidate=new['run']['id']
    if baseline==candidate:raise HTTPException(422,'choose distinct baseline and candidate runs')
    a=old['run']['definition'];b=new['run']['definition']
    if old['dataset_sha256']!=new['dataset_sha256'] or a['dataset']!=b['dataset'] or a['dataset_version']!=b['dataset_version']:raise HTTPException(409,'compare the same immutable dataset version')
    if a['evaluator_runner_sha256']!=b['evaluator_runner_sha256']:raise HTTPException(409,'the evaluation runner changed; keep checks fixed for this comparison')
    previous={r['case']:r for r in old['cases']};rows=[]
    for result in new['cases']:
        before=previous[result['case']]['state'];after=result['state']
        change='unknown' if 'missing' in {before,after} else 'regressed' if before=='passed' and after!='passed' else 'improved' if before!='passed' and after=='passed' else 'unchanged'
        rows.append({**result,'baseline_state':before,'baseline_differences':previous[result['case']]['differences'],'baseline_difference_count':previous[result['case']]['difference_count'],'change':change})
    counts=Counter(r['change'] for r in rows)
    eligible=old['complete'] and new['complete'] and not counts['regressed'] and new['pass_rate']>=new['dataset']['definition']['minimum_pass_rate']
    return {'baseline':baseline,'candidate':candidate,'dataset_sha256':new['dataset_sha256'],'cases':rows,
            'changes':{k:counts[k] for k in ['improved','regressed','unchanged','unknown']},'baseline_pass_rate':old['pass_rate'],'candidate_pass_rate':new['pass_rate'],
            'eligible_for_review':eligible,'deployment_authorized':False,'limits':new['limits']+['This controlled fixture comparison does not establish production impact or causal improvement for real users.']}
