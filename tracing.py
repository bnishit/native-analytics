"""Bounded, content-free workflow traces; observations are not business proof."""
from collections import defaultdict
from contextlib import closing
from decimal import Decimal
import hashlib
import hmac
import json
from typing import Literal
from fastapi import Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field,UUID4,StrictBool,model_validator,field_validator,model_serializer
from product_analytics import Window

STEPS=('receive','queue','processing','model','tool','retrieval','storage','http','response','availability','booking','provider_send','provider_delivery')
Environment=Literal['production','staging','development','test','local']
TrafficKind=Literal['user','synthetic','internal','unknown']
def workflow_reference(project,environment,trace_id,key):
    return 'trace_'+hmac.new(key.encode(),(project+'\0'+environment+'\0'+trace_id).encode(),hashlib.sha256).hexdigest()[:32]
class CheckResult(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name:str=Field(pattern=r'^[a-z][a-z0-9_.-]{0,63}$')
    version:int=Field(ge=1,le=1000000,strict=True)
    result:Literal['passed','failed','unknown']
    method:Literal['source','code','model','human']
    phase:Literal['before_delivery','after_delivery']

CheckExecution=Literal['completed','not_run']
CheckReason=Literal['evidence_absent','evidence_binding_rejected']
def validate_check_execution(execution,reason,result,has_evidence):
    if reason is not None and execution!='not_run':raise ValueError('reason requires a check that did not run')
    if execution=='not_run' and (result!='unknown' or has_evidence):raise ValueError('a check that did not run must stay unknown and have no evidence')

class WorkflowCheck(CheckResult):
    required:StrictBool
    evidence_ref:UUID4|None=None
    execution:CheckExecution|None=None
    reason_code:CheckReason|None=None
    @model_validator(mode='after')
    def execution_consistent(self):
        validate_check_execution(self.execution,self.reason_code,self.result,self.evidence_ref is not None)
        return self
    @model_serializer(mode='wrap')
    def optional_diagnostics(self,handler):
        result=handler(self)
        for field in ('execution','reason_code'):
            if getattr(self,field) is None:result.pop(field,None)
        return result

class WorkflowEvidence(CheckResult):
    schema_version:Literal['workflow-evidence-v1']
    receipt_id:UUID4
    trace_id:UUID4
    attempt_id:UUID4
    span_id:UUID4
    workflow:str=Field(pattern=r'^[a-z][a-z0-9_.-]{0,63}$')
    environment:Environment
    release_sha:str|None=Field(default=None,pattern=r'^[a-f0-9]{40}$')
    evidence_digest:str=Field(pattern=r'^[a-f0-9]{64}$')

class ModelUsage(BaseModel):
    model_config=ConfigDict(extra='forbid')
    provider:str=Field(pattern=r'^[a-z][a-z0-9_.-]{0,63}$')
    model:str=Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}$')
    template_version:str|None=Field(default=None,pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
    unit:Literal['token']='token'
    input_count:int|None=Field(default=None,ge=0,le=1000000000,strict=True)
    output_count:int|None=Field(default=None,ge=0,le=1000000000,strict=True)
    cache_read_count:int|None=Field(default=None,ge=0,le=1000000000,strict=True)
    reasoning_count:int|None=Field(default=None,ge=0,le=1000000000,strict=True)
    usage_source:Literal['provider_reported','estimated']|None=None
    cost_microusd:int|None=Field(default=None,ge=0,le=1000000000000,strict=True)
    cost_usd_decimal:str|None=Field(default=None,pattern=r'^(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,12})?$')
    cost_source:Literal['provider_reported','price_table']|None=None
    price_version:str|None=Field(default=None,pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
    first_output_ms:float|None=Field(default=None,ge=0,le=86400000,strict=True,allow_inf_nan=False)

    @field_validator('cost_usd_decimal')
    @classmethod
    def decimal_cost(cls,value):
        if value is None:return None
        if Decimal(value)>Decimal('1000000'):raise ValueError('cost exceeds supported bound')
        return value.rstrip('0').rstrip('.') if '.' in value else value

    @model_validator(mode='after')
    def provenance(self):
        has_usage=any(getattr(self,k) is not None for k in ('input_count','output_count','cache_read_count','reasoning_count'))
        if has_usage!=(self.usage_source is not None):raise ValueError('usage requires its source')
        if self.cost_microusd is not None and self.cost_usd_decimal is not None:raise ValueError('use one exact cost representation')
        if (self.cost_microusd is not None or self.cost_usd_decimal is not None)!=(self.cost_source is not None):raise ValueError('cost requires its source')
        if (self.cost_source=='price_table')!=(self.price_version is not None):raise ValueError('price-table cost requires a pinned price version')
        if self.input_count is not None and self.cache_read_count is not None and self.cache_read_count>self.input_count:raise ValueError('cached units exceed input units')
        if self.output_count is not None and self.reasoning_count is not None and self.reasoning_count>self.output_count:raise ValueError('reasoning units exceed output units')
        return self

def resolve_evidence(check,facts,at,receipts):
    reference=check.get('evidence_ref')
    if reference is None:return 'not_referenced'
    rows=receipts.get(reference,[])
    if not rows:return 'missing'
    signatures={(json.dumps(r['facts'],sort_keys=True),r['ts']) for r in rows}
    if len(signatures)!=1:return 'conflict'
    receipt=rows[0];body=receipt['facts']
    if any(body[k]!=facts.get(k) for k in ['trace_id','attempt_id','span_id','workflow','environment','release_sha']):return 'mismatch'
    if any(body[k]!=check[k] for k in ['name','version','result','method','phase']):return 'mismatch'
    if receipt['ts']>at or facts.get('request_started_at_ms') is not None and receipt['ts']*1000<facts['request_started_at_ms']:return 'mismatch'
    return 'resolved'


class SpanObservation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    schema_version:Literal['workflow-trace-v1']
    trace_id:UUID4
    attempt_id:UUID4
    attempt_number:int=Field(ge=1,le=10000,strict=True)
    span_id:UUID4
    parent_span_id:UUID4|None=None
    workflow:str=Field(pattern=r'^[a-z][a-z0-9_.-]{0,63}$')
    environment:Environment
    traffic_kind:TrafficKind='unknown'
    release_sha:str|None=Field(default=None,pattern=r'^[a-f0-9]{40}$')
    step:Literal['receive','queue','processing','model','tool','retrieval','storage','http','response','availability','booking','provider_send','provider_delivery']
    state:Literal['started','succeeded','failed','unknown']
    duration_ms:float|None=Field(default=None,ge=0,le=86400000,allow_inf_nan=False,strict=True)
    outcome_code:Literal['none','unavailable','rejected','timeout','cancelled','internal','uncertain']= 'none'
    receipt_count:int|None=Field(default=None,ge=1,le=20,strict=True)
    linked_trace_ids:list[UUID4]|None=Field(default=None,max_length=20)

    request_started_at_ms:int|None=Field(default=None,ge=0,le=4102444800000,strict=True)
    milestone:Literal['completion','delivery','usefulness']|None=None
    disposition:Literal['delivered','withheld','clarification','partial']|None=None
    checks:list[WorkflowCheck]|None=Field(default=None,min_length=1,max_length=20)
    model_usage:ModelUsage|None=None

    @model_validator(mode='after')
    def coherent(self):
        if self.model_usage is not None:
            if self.step!='model' or self.state=='started':raise ValueError('model usage belongs to a terminal model step')
            if self.model_usage.first_output_ms is not None and (self.duration_ms is None or self.model_usage.first_output_ms>self.duration_ms):raise ValueError('first output must fall inside measured duration')
        if self.state=='started' and (self.milestone is not None or self.disposition is not None):raise ValueError('milestones and dispositions belong to terminal observations')
        if self.disposition is not None and self.step!='response':raise ValueError('disposition belongs to response')
        if self.checks is not None:
            if self.state=='started':raise ValueError('checks belong to a terminal observation')
            if len({c.name for c in self.checks})!=len(self.checks):raise ValueError('check names must be unique on one observation')
        if (self.receipt_count is None)!=(self.linked_trace_ids is None):raise ValueError('batch receipt count and links must occur together')
        if self.linked_trace_ids is not None:
            if self.step!='processing' or self.state!='started':raise ValueError('batch links belong only to processing start')
            if len(set(self.linked_trace_ids))!=len(self.linked_trace_ids) or self.trace_id in self.linked_trace_ids:
                raise ValueError('batch links must be unique and cannot include self')
            if len(self.linked_trace_ids)>self.receipt_count:raise ValueError('links cannot exceed receipt count')
        if self.parent_span_id==self.span_id:raise ValueError('span cannot parent itself')
        if self.state=='started' and (self.duration_ms is not None or self.outcome_code!='none'):
            raise ValueError('a start has no duration or outcome')
        if self.state=='succeeded' and self.outcome_code!='none':raise ValueError('success cannot carry a failure code')
        return self

class TraceReview(Window):
    environment:Environment
    limit:int=Field(default=20,ge=1,le=60)
class TraceDetail(TraceReview):
    trace:str=Field(pattern=r'^trace_[a-f0-9]{32}$')


def summarize_span(rows,labels):
    facts=[r['facts'] for r in rows];first=facts[0]
    immutable=('attempt_id','attempt_number','step','parent_span_id','release_sha')
    conflict=any(len({f.get(k) for f in facts})>1 for k in immutable)
    starts=[r for r in rows if r['facts']['state']=='started']
    conflict=conflict or len({(r['facts'].get('receipt_count'),tuple(r['facts'].get('linked_trace_ids') or [])) for r in starts})>1
    ends=[r for r in rows if r['facts']['state']!='started']
    signatures={(r['facts']['state'],r['facts'].get('duration_ms'),r['facts']['outcome_code'],json.dumps([r['facts'].get(k) for k in ['checks','milestone','disposition','model_usage']],sort_keys=True)) for r in ends}
    conflict=conflict or len(signatures)>1 or len({r['ts'] for r in starts})>1 or len({r['ts'] for r in ends})>1 or bool(starts and ends and min(r['ts'] for r in starts)>min(r['ts'] for r in ends))
    state='conflict' if conflict else ends[0]['facts']['state'] if ends else 'incomplete'
    parent=first.get('parent_span_id')
    return {'span':labels[first['span_id']],'parent':labels.get(parent),'parent_missing':parent is not None and parent not in labels,
            'step':first['step'],'attempt_number':first['attempt_number'],'state':state,'missing_start':not starts,
            'duration_ms':ends[0]['facts']['duration_ms'] if ends and not conflict else None,
            'outcome_code':ends[0]['facts']['outcome_code'] if ends and not conflict else 'uncertain',
            'milestone':ends[0]['facts'].get('milestone') if ends and not conflict else None,
            'disposition':ends[0]['facts'].get('disposition') if ends and not conflict else None,
            'model_usage':ends[0]['facts'].get('model_usage') if ends and not conflict else None,
            'checks':[{**{k:v for k,v in check.items() if k not in {'evidence_ref','_evidence_status'}},'evidence_present':check.get('evidence_ref') is not None,'evidence_status':check.get('_evidence_status','not_resolved')}
                      for check in ends[0]['facts'].get('checks') or []] if ends and not conflict else [],
            'first_seen':min(r['ts'] for r in rows),'last_seen':max(r['ts'] for r in rows)}


def project_trace(rows,reference,reference_for=None,available=None):
    groups=defaultdict(list);slots=defaultdict(set);attempts=defaultdict(set);releases=defaultdict(set)
    for r in rows:
        f=r['facts'];groups[f['span_id']].append(r);slots[f['attempt_number']].add(f['attempt_id']);attempts[f['attempt_id']].add(f['attempt_number'])
        releases[f['attempt_number']].add(f.get('release_sha'))
    labels={sid:'span_'+str(i+1) for i,sid in enumerate(sorted(groups,key=lambda sid:(min(r['ts'] for r in groups[sid]),sid)))}
    spans=[summarize_span(group,labels) for group in groups.values()]
    spans.sort(key=lambda s:(s['attempt_number'],s['first_seen'],s['span']))
    issues=[]
    memberships={(r['facts']['receipt_count'],tuple(r['facts']['linked_trace_ids'])) for r in rows if r['facts'].get('linked_trace_ids') is not None}
    batch=None
    if len(memberships)>1:issues.append('batch_membership_conflict')
    elif memberships:
        count,links=next(iter(memberships))
        batch={'receipt_count':count,'unlinked_receipts':count-len(links),'inputs':[
            {'trace':reference_for(tid),'state':'observed' if tid in available else 'missing_in_window'}
            for tid in links] if reference_for else []}
    if any(len(x)>1 for x in [*slots.values(),*attempts.values()]):issues.append('attempt_identity_conflict')
    clocks={r['facts'].get('request_started_at_ms') for r in rows}
    clock=next(iter(clocks)) if len(clocks)==1 else None
    if len(clocks)>1 or clock is not None and any(r['ts']*1000<clock for r in rows):issues.append('request_clock_conflict')
    if len({r['facts']['workflow'] for r in rows})>1:issues.append('workflow_conflict')
    traffic={r['facts'].get('traffic_kind','unknown') for r in rows}
    if len(traffic)>1:issues.append('traffic_kind_conflict')
    if any(len(values)>1 for values in releases.values()):issues.append('release_conflict')
    parents={sid:group[0]['facts'].get('parent_span_id') for sid,group in groups.items()}
    for sid,parent in parents.items():
        if parent in groups and groups[sid][0]['facts']['attempt_id']!=groups[parent][0]['facts']['attempt_id']:
            issues.append('parent_attempt_conflict')
    visited=set()
    for sid in parents:
        seen=set();node=sid
        while node in parents and node not in visited:
            if node in seen:issues.append('parent_cycle');break
            seen.add(node);node=parents[node]
        visited.update(seen)
    latest=max(slots);current=[s for s in spans if s['attempt_number']==latest]
    completion={}
    for step in STEPS:
        matching=[s for s in current if s['step']==step]
        completion[step]={'state':matching[0]['state'] if len(matching)==1 else 'missing' if not matching else 'multiple',
                          'observations':len(matching)}
    if any(s['state']=='conflict' for s in spans):issues.append('span_conflict')
    if any(s['parent_missing'] for s in spans):issues.append('missing_parent')
    if any(s['missing_start'] for s in spans):issues.append('missing_start')
    if max(slots)!=len(slots):issues.append('missing_attempts')
    if issues:
        for item in completion.values():item['state']='unknown'
    return {'trace':reference,'request_started_at_ms':clock,'workflow':rows[0]['facts']['workflow'],'environment':rows[0]['facts']['environment'],'traffic_kind':next(iter(traffic)) if len(traffic)==1 else 'unknown',
            'release_sha':next(iter({r['facts'].get('release_sha') for r in rows})) if len({r['facts'].get('release_sha') for r in rows})==1 else None,
            'latest_attempt':latest,'observed_attempts':len(slots),'last_seen':max(r['ts'] for r in rows),'spans':spans,
            'completion':completion,'issues':sorted(set(issues)),'batch':batch,
            'checks':[{**check,'span':s['span'],'attempt_number':s['attempt_number']} for s in current for check in s['checks']],
            'reported_failures':[s['span'] for s in spans if s['state']=='failed'],'business_outcome':'not_established'}


class Tracing:
    def __init__(self,app,connect,reader,freeze,metadata,integrations=None):
        self.connect=connect;self.freeze=freeze;self.metadata=metadata
        @app.get('/api/traces/catalog',dependencies=[Depends(reader)])
        def catalog(project:str):
            return {'schema':SpanObservation.model_json_schema(),'steps':list(STEPS),'integrations':integrations or [],
                    'meanings':{'receive':'Application receipt; not a queue or reply guarantee.',
                    'checks.execution':'Optional completed or not_run diagnostic, separate from the result. Missing stays unspecified; a closed operation does not prove its check ran.',
                    'checks.reason_code':'Optional bounded reason for not_run: evidence_absent or evidence_binding_rejected. This is producer-reported metadata, not source truth.',
                    'queue':'Queue handling; not end-user delivery.','processing':'Application work; not business outcome or delivery proof.',
                    'model':'Model call completed; not answer correctness.','tool':'Tool call completed; not verified business state.',
                    'retrieval':'Context lookup; not source correctness.','storage':'Storage operation; not independent durable-state verification.',
                    'http':'HTTP call; status alone is not business success.','response':'Application response prepared; not receipt or reading.',
                    'availability':'Availability lookup completed; not a reservation.',
                    'booking':'Producer reports its booking operation completed; independent readback still required.',
                    'provider_send':'Provider accepted the send; not final delivery.',
                    'provider_delivery':'Producer observed a delivery receipt; not proof of reading.'},
                    'limits':['Absent spans stay missing. Closed spans without a start stay incomplete evidence.',
                    'Workflow observations accept bounded identities, categories, timings, checks and model usage. No raw messages, private identifiers or errors.',
                    'This catalog describes native workflow observations; OTLP/HTTP uses /v1/traces with a separate metadata allowlist.']}
        @app.post('/api/traces/review',dependencies=[Depends(reader)])
        def review(project:str,req:TraceReview):return self.review(project,req)
        @app.post('/api/traces/detail',dependencies=[Depends(reader)])
        def detail(project:str,req:TraceDetail):
            report=self.review(project,TraceReview(**req.model_dump(exclude={'trace'})),req.trace)
            item=next((t for t in report['traces'] if hmac.compare_digest(t['trace'],req.trace)),None)
            if item is None:raise HTTPException(404,'trace is outside this review; reuse its exact query')
            return {**{k:v for k,v in report.items() if k!='traces'},'trace':item}

    def review(self,project,req,detail_reference=None,*,all_traces=False):
        q=self.freeze(req)
        with closing(self.connect()) as c:
            rows=c.execute("SELECT ts,json_extract(properties,'$.afterspan_trace') AS facts FROM events WHERE project=? AND ts>=? AND ts<? AND received<=? AND json_type(properties,'$.afterspan_trace') IS NOT NULL ORDER BY ts,id LIMIT 20001",
                           (project,q.start.timestamp(),q.end.timestamp(),q.as_of.timestamp())).fetchall()
            evidence_rows=c.execute("SELECT ts,json_extract(properties,'$.afterspan_evidence') AS facts FROM events WHERE project=? AND ts>=? AND ts<? AND received<=? AND json_type(properties,'$.afterspan_evidence') IS NOT NULL ORDER BY ts,id LIMIT 20001",
                (project,q.start.timestamp(),q.end.timestamp(),q.as_of.timestamp())).fetchall()
            key=c.execute("SELECT value FROM billing_settings WHERE key='receipt_key'").fetchone()[0]
        if len(rows)>20000:raise HTTPException(422,'more than 20,000 observations; shorten the window; no partial report returned')
        if len(evidence_rows)>20000:raise HTTPException(422,'more than 20,000 evidence receipts; shorten the window')
        receipts=defaultdict(list);invalid_evidence=0
        for row in evidence_rows:
            try:body=WorkflowEvidence.model_validate(json.loads(row['facts'])).model_dump(mode='json')
            except (ValueError,TypeError):invalid_evidence+=1;continue
            if body['environment']==q.environment:receipts[body['receipt_id']].append({'ts':row['ts'],'facts':body})
        groups=defaultdict(list);invalid=0;other=0
        for row in rows:
            try:f=SpanObservation.model_validate(json.loads(row['facts'])).model_dump(mode='json')
            except (ValueError,TypeError):invalid+=1;continue
            if f['environment']!=q.environment:other+=1;continue
            for check in f.get('checks') or []:check['_evidence_status']=resolve_evidence(check,f,row['ts'],receipts)
            groups[f['trace_id']].append({'ts':row['ts'],'facts':f})
        def reference_for(tid):
            return workflow_reference(project,q.environment,tid,key)
        traces=[]
        for tid,group in groups.items():
            traces.append(project_trace(group,reference_for(tid),reference_for,groups))
        traces.sort(key=lambda t:(-t['last_seen'],t['trace']))
        linked_inputs=[]
        if detail_reference:
            selected=next((t for t in traces[:q.limit] if hmac.compare_digest(t['trace'],detail_reference)),None)
            refs={i['trace'] for i in selected['batch']['inputs']} if selected and selected['batch'] else set()
            linked_inputs=[t for t in traces if t['trace'] in refs]
        result=self.metadata(project,q,'workflow-trace-v1')
        result['limits']+=['Only retained observations in this exact window are shown; earlier starts may be outside it.',
                           'Missing or conflicting evidence is unknown, never proof a step was skipped.',
                           'Provider acceptance, provider delivery and booking are separate producer observations. No patient reading or end-to-end business success is inferred.',
                           'Telemetry is best effort. This is not a complete ledger of patient activity.',
                           'Batch inputs are direct links within the same project, environment and frozen window. Missing links remain unknown. Booking and send spans belong once to the batch, never once per linked receipt.']
        return {**result,**({'linked_inputs':linked_inputs} if detail_reference else {}),'coverage':{'observations_scanned':len(rows),'invalid_observations':invalid,'invalid_evidence_receipts':invalid_evidence,'evidence_receipts_scanned':len(evidence_rows),'other_environment_observations':other,
                                    'observed_traces':len(traces),'returned_traces':min(len(traces),q.limit),'has_more':len(traces)>q.limit},'traces':traces if all_traces else traces[:q.limit]}
