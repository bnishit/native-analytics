"""Versioned outcome criteria over existing workflow traces, never inferred success."""
import json
import time
import uuid
from collections import Counter
from contextlib import closing
from datetime import timedelta
from decimal import Decimal,localcontext
from typing import Literal
from fastapi import Depends, HTTPException
from pydantic import Field, StrictBool, model_validator
from product_analytics import Model, Window
from tracing import Environment, TraceReview
from outcome_adapters import slack_outcomes
from sdk.python.afterspan_experiments import digest

class OutcomeBinding(Model):
    id:uuid.UUID
    version:int=Field(ge=1,le=1000,strict=True)
    sha256:str=Field(pattern=r'^[a-f0-9]{64}$')

class RequiredCheck(Model):
    name:str=Field(pattern=r'^[a-z][a-z0-9_.-]{0,63}$')
    version:int=Field(ge=1,le=1000000,strict=True)
    method:Literal['source','code','model','human']
    phase:Literal['before_delivery','after_delivery']
    evidence_required:StrictBool=True

class OutcomeGoal(Model):
    contract:Literal['workflow-trace-v1','slack-request-v1']='workflow-trace-v1'
    title:str=Field(min_length=1,max_length=100)
    workflow:str=Field(pattern=r'^[a-z][a-z0-9_.-]{0,63}$')
    environment:Environment
    traffic_kind:Literal['user','synthetic','internal','all']='all'
    min_sample_size:int=Field(default=25,ge=1,le=100000,strict=True)
    deadline_seconds:int=Field(ge=1,le=7*86400,strict=True)
    checks:list[RequiredCheck]=Field(min_length=1,max_length=20)
    milestones:list[Literal['completion','delivery','usefulness']]=Field(min_length=1,max_length=3)
    @model_validator(mode='after')
    def unique(self):
        if len({c.name for c in self.checks})!=len(self.checks) or len(set(self.milestones))!=len(self.milestones):
            raise ValueError('criteria must be unique')
        return self

class OutcomeQuery(Window):
    goal_id:uuid.UUID
    version:int=Field(ge=1,strict=True)
    release_sha:str|None=Field(default=None,pattern=r'^[a-f0-9]{40}$')
    offset:int=Field(default=0,ge=0,le=20000,strict=True)
    limit:int=Field(default=20,ge=1,le=60,strict=True)

def classify(trace,goal,as_of):
    entered=trace['request_started_at_ms']
    if entered is None:return 'unclassified',['original_clock_missing']
    deadline=entered/1000+goal.deadline_seconds
    if as_of<deadline:return 'waiting',[]
    if trace['issues']:return 'unclassified',trace['issues']
    spans=[s for s in trace['spans'] if s['attempt_number']==trace['latest_attempt']]
    reasons=[];failed=False;withheld=False
    delivery=[s for s in spans if s['milestone']=='delivery' and s['state']=='succeeded']
    for criterion in goal.checks:
        found=[(c,s) for s in spans for c in s['checks'] if c['name']==criterion.name]
        if len(found)!=1:reasons.append('check_missing_or_conflicting:'+criterion.name);continue
        check,span=found[0]
        if any(check[k]!=getattr(criterion,k) for k in ['version','method','phase']):
            reasons.append('check_definition_mismatch:'+criterion.name);continue
        if span['missing_start']:reasons.append('check_start_missing:'+criterion.name)
        if criterion.phase=='after_delivery' and delivery and any(span['last_seen']<s['last_seen'] for s in delivery):reasons.append('check_before_delivery:'+criterion.name)
        if check['result']=='failed':failed=True;reasons.append('check_failed:'+criterion.name)
        elif check['result']=='unknown':reasons.append('check_unknown:'+criterion.name)
        if criterion.evidence_required and check.get('evidence_status')!='resolved':reasons.append('check_evidence_missing:'+criterion.name)
        if span['last_seen']>deadline:failed=True;reasons.append('check_late:'+criterion.name)
        if criterion.phase=='before_delivery' and delivery and any(s['first_seen']<span['last_seen'] for s in delivery):
            reasons.append('check_after_delivery:'+criterion.name)
    for milestone in goal.milestones:
        found=[s for s in spans if s['milestone']==milestone]
        if len(found)!=1:reasons.append('milestone_missing_or_conflicting:'+milestone);continue
        span=found[0]
        if span['state']=='failed':failed=True;reasons.append('milestone_failed:'+milestone)
        elif span['state']!='succeeded' or span['missing_start']:reasons.append('milestone_unverified:'+milestone)
        if span['last_seen']>deadline:failed=True;reasons.append('milestone_late:'+milestone)
    for span in spans:
        if span['disposition']=='withheld':withheld=True
        if span['disposition']=='clarification':reasons.append('clarification_not_completed')
    return ('failed' if failed else 'withheld' if withheld else 'unclassified' if reasons else 'satisfied'),sorted(set(reasons))

def model_cost_summary(traces,satisfied,population_certain):
    spans=[s for trace in traces for s in trace['spans'] if s['step']=='model']
    known=[s['model_usage'] for s in spans if s.get('model_usage') and any(s['model_usage'].get(k) is not None for k in ['cost_microusd','cost_usd_decimal'])]
    measured=sum((Decimal(m['cost_usd_decimal']) if m.get('cost_usd_decimal') is not None else Decimal(m['cost_microusd'])/1000000 for m in known),Decimal(0))
    micros=measured*1000000;overflow=micros>2**53-1;integral=micros==micros.to_integral_value()
    complete=bool(spans) and len(known)==len(spans)
    def text(value):
        value=format(value,'f');return value.rstrip('0').rstrip('.') if '.' in value else value
    with localcontext() as context:
        context.prec=50
        ratio=measured/satisfied if satisfied and complete and population_certain else None
        rounded=ratio.quantize(Decimal('0.000000000000000001')) if ratio is not None else None
    return {'measured_usd':text(measured) if known else None,'measured_microusd':int(micros) if integral and not overflow else None,
            'model_spans':len(spans),'cost_known_spans':len(known),'cost_unknown_spans':len(spans)-len(known),'amount_outside_safe_range':overflow,
            'sources':dict(Counter(m['cost_source'] for m in known)),
            'per_satisfied_task_usd':text(rounded) if rounded is not None else None,
            'per_satisfied_task_rounded':ratio is not None and rounded!=ratio,
            'per_satisfied_task_below_display_precision':ratio is not None and ratio>0 and rounded==0,
            'per_satisfied_task_microusd':int(micros)/satisfied if ratio is not None and integral and not overflow else None,
            'scope':'Observed model spans in mature tasks, including failed/withheld tasks and all retry attempts. Waiting tasks excluded.',
            'limits':['Only reported model cost, not a complete application bill. Missing cost is unknown, not zero.',
                'USD totals use exact decimal strings; legacy integer micro-USD fields are unavailable for fractional micro-USD or unsafe integer totals. Per-satisfied-task USD ratios round to 18 decimal places and identify positive values below that display precision.',
                'Cost sources remain provider-reported or pinned price-table estimates.']}

class Outcomes:
    @staticmethod
    def init_db(c):
        c.execute('''CREATE TABLE IF NOT EXISTS outcome_goals(project TEXT NOT NULL REFERENCES projects(id),
          id TEXT NOT NULL,version INTEGER NOT NULL,definition TEXT NOT NULL,created REAL NOT NULL,
          PRIMARY KEY(project,id,version))''')

    def __init__(self,app,connect,reader,editor,audit_reader,freeze,metadata,tracing,audit):
        self.connect=connect;self.freeze=freeze;self.metadata=metadata;self.tracing=tracing;self.audit=audit
        @app.get('/api/outcomes/goals',dependencies=[Depends(audit_reader)])
        def goals(project:str):return self.list(project)
        @app.get('/api/outcomes/goals/{id}',dependencies=[Depends(audit_reader)])
        def goal(project:str,id:uuid.UUID,version:int=1):return self.get(project,str(id),version)
        @app.put('/api/outcomes/goals/{id}',dependencies=[Depends(editor)])
        def save(project:str,id:uuid.UUID,revision:int,req:OutcomeGoal):return self.save(project,str(id),revision,req)
        @app.post('/api/outcomes/evaluate',dependencies=[Depends(audit_reader)])
        def evaluate(project:str,req:OutcomeQuery):return self.evaluate(project,req)

    def get(self,project,id,version):
        with closing(self.connect()) as c:r=c.execute('SELECT * FROM outcome_goals WHERE project=? AND id=? AND version=?',(project,id,version)).fetchone()
        if not r:raise HTTPException(404,'goal version not found in this project')
        definition=json.loads(r['definition'])
        return {**dict(r),'definition':definition,'sha256':digest(definition)}

    def list(self,project):
        with closing(self.connect()) as c:rows=c.execute('''SELECT g.* FROM outcome_goals g WHERE project=? AND version=
            (SELECT MAX(version) FROM outcome_goals x WHERE x.project=g.project AND x.id=g.id) ORDER BY created DESC,id''',(project,)).fetchall()
        return {'goals':[{**dict(r),'definition':json.loads(r['definition']),'sha256':digest(json.loads(r['definition']))} for r in rows]}

    def save(self,project,id,revision,req):
        if revision<0:raise HTTPException(422,'revision must be nonnegative')
        payload=req.model_dump_json()
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE')
            existing=c.execute('SELECT definition FROM outcome_goals WHERE project=? AND id=? AND version=?',(project,id,revision+1)).fetchone()
            if existing and existing['definition']==payload:return self.get(project,id,revision+1)
            latest=c.execute('SELECT MAX(version) FROM outcome_goals WHERE project=? AND id=?',(project,id)).fetchone()[0] or 0
            if revision!=latest:raise HTTPException(409,'goal changed; read the current version')
            if not latest and c.execute('SELECT COUNT(DISTINCT id) FROM outcome_goals WHERE project=?',(project,)).fetchone()[0]>=100:
                raise HTTPException(422,'at most 100 goals per project')
            if latest>=1000:raise HTTPException(422,'goal version capacity reached')
            c.execute('INSERT INTO outcome_goals VALUES(?,?,?,?,?)',(project,id,revision+1,payload,time.time()))
        return self.get(project,id,revision+1)

    def evaluate(self,project,req):
        saved=self.get(project,str(req.goal_id),req.version);goal=OutcomeGoal.model_validate(saved['definition']);q=self.freeze(req)
        followup_end=min(q.end+timedelta(seconds=goal.deadline_seconds),q.as_of)
        if (followup_end-q.start).total_seconds()>90*86400:
            raise HTTPException(422,'entry window plus deadline must fit within 90 days')
        observation_query=TraceReview(start=q.start,end=followup_end,as_of=q.as_of,environment=goal.environment)
        report=slack_outcomes(self.audit,project,observation_query) if goal.contract=='slack-request-v1' else self.tracing.review(project,observation_query,all_traces=True)
        rows=[];mature_traces=[];missing=0;excluded=0;release_uncertain=0;traffic_excluded=0;traffic_unknown=0
        for trace in report['traces']:
            if trace['workflow']!=goal.workflow:continue
            entered=trace['request_started_at_ms']
            if entered is not None and not q.start.timestamp()<=entered/1000<q.end.timestamp():continue
            if goal.traffic_kind!='all' and trace.get('traffic_kind','unknown')!=goal.traffic_kind:
                traffic_excluded+=1;traffic_unknown+=trace.get('traffic_kind','unknown')=='unknown';continue
            if req.release_sha and trace['release_sha']!=req.release_sha:
                excluded+=1;release_uncertain+=trace['release_sha'] is None;continue
            if entered is None:missing+=1
            state,reasons=classify(trace,goal,q.as_of.timestamp())
            if state!='waiting':mature_traces.append(trace)
            rows.append({'trace':trace['trace'],'state':state,'reasons':reasons,'attempts':trace['observed_attempts'],
                         'started_at_ms':entered,'release_sha':trace['release_sha']})
        rows.sort(key=lambda r:((r['started_at_ms'] or 0),r['trace']),reverse=True)
        counts=Counter(r['state'] for r in rows);mature=sum(counts[k] for k in ['satisfied','failed','withheld','unclassified'])
        result=self.metadata(project,q,'workflow-outcome-v1' if goal.contract=='workflow-trace-v1' else 'slack-outcome-v1')
        result.update(goal=saved,query=req.model_dump(mode='json')|{k:result['query'][k] for k in ['days','start','end','as_of']},
            counts={k:counts[k] for k in ['satisfied','failed','withheld','unclassified','waiting']},mature_denominator=mature,
            satisfied_fraction=counts['satisfied']/mature if mature and not missing and not release_uncertain and not traffic_unknown and not report['coverage']['invalid_observations'] and not report['coverage']['invalid_evidence_receipts'] and not report['coverage'].get('population_unassigned',0) else None,
            coverage={**report['coverage'],'original_clock_missing':missing,'release_excluded':excluded,'release_uncertain':release_uncertain,'traffic_excluded':traffic_excluded,'traffic_unknown':traffic_unknown},
            trace_query=report['query'],total=len(rows),tasks=rows[q.offset:q.offset+q.limit],has_more=q.offset+q.limit<len(rows))
        result['model_cost']=model_cost_summary(mature_traces,counts['satisfied'],result['satisfied_fraction'] is not None)
        result['limits']+=['Outcomes apply the saved criteria to producer-reported observations, not an independent source read.',
            'One observed task trace is counted once across retries. All mature unclassified and withheld tasks remain in the denominator.',
            'Missing original clocks cannot establish membership; fraction stays unavailable. Unobserved tasks are not a complete task ledger.',
            'Release comparisons are observational, not causal. Retention, erasure and late telemetry can remove evidence.']
        return result
