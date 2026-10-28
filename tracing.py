"""Bounded, content-free workflow traces; observations are not business proof."""
from collections import defaultdict
from contextlib import closing
import hashlib
import hmac
import json
from typing import Literal
from fastapi import Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field,UUID4,model_validator
from product_analytics import Window

STEPS=('receive','queue','processing','availability','booking','provider_send','provider_delivery')
Environment=Literal['production','staging','development','test','local']
class SpanObservation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    schema_version:Literal['workflow-trace-v1']
    trace_id:UUID4
    attempt_id:UUID4
    attempt_number:int=Field(ge=1,le=10000,strict=True)
    span_id:UUID4
    parent_span_id:UUID4|None=None
    workflow:Literal['appointment_request','request_processing']
    environment:Environment
    release_sha:str|None=Field(default=None,pattern=r'^[a-f0-9]{40}$')
    step:Literal['receive','queue','processing','availability','booking','provider_send','provider_delivery']
    state:Literal['started','succeeded','failed','unknown']
    duration_ms:float|None=Field(default=None,ge=0,le=86400000,allow_inf_nan=False,strict=True)
    outcome_code:Literal['none','unavailable','rejected','timeout','cancelled','internal','uncertain']= 'none'
    receipt_count:int|None=Field(default=None,ge=1,le=20,strict=True)
    linked_trace_ids:list[UUID4]|None=Field(default=None,max_length=20)

    @model_validator(mode='after')
    def coherent(self):
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
    signatures={(r['facts']['state'],r['facts'].get('duration_ms'),r['facts']['outcome_code']) for r in ends}
    conflict=conflict or len(signatures)>1 or len({r['ts'] for r in starts})>1 or len({r['ts'] for r in ends})>1 or bool(starts and ends and min(r['ts'] for r in starts)>min(r['ts'] for r in ends))
    state='conflict' if conflict else ends[0]['facts']['state'] if ends else 'incomplete'
    parent=first.get('parent_span_id')
    return {'span':labels[first['span_id']],'parent':labels.get(parent),'parent_missing':parent is not None and parent not in labels,
            'step':first['step'],'attempt_number':first['attempt_number'],'state':state,'missing_start':not starts,
            'duration_ms':ends[0]['facts']['duration_ms'] if ends and not conflict else None,
            'outcome_code':ends[0]['facts']['outcome_code'] if ends and not conflict else 'uncertain',
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
    if len({r['facts']['workflow'] for r in rows})>1:issues.append('workflow_conflict')
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
    return {'trace':reference,'workflow':rows[0]['facts']['workflow'],'environment':rows[0]['facts']['environment'],
            'latest_attempt':latest,'observed_attempts':len(slots),'last_seen':max(r['ts'] for r in rows),'spans':spans,
            'completion':completion,'issues':sorted(set(issues)),'batch':batch,
            'reported_failures':[s['span'] for s in spans if s['state']=='failed'],'business_outcome':'not_established'}


class Tracing:
    def __init__(self,app,connect,reader,freeze,metadata):
        self.connect=connect;self.freeze=freeze;self.metadata=metadata
        @app.get('/api/traces/catalog',dependencies=[Depends(reader)])
        def catalog(project:str):
            return {'schema':SpanObservation.model_json_schema(),'steps':list(STEPS),
                    'meanings':{'receive':'Application receipt; not a queue or reply guarantee.',
                    'queue':'Queue handling; not patient delivery.','processing':'Application work; not booking or delivery proof.',
                    'availability':'Availability lookup completed; not a reservation.',
                    'booking':'Producer reports its booking operation completed; independent readback still required.',
                    'provider_send':'Provider accepted the send; not patient delivery.',
                    'provider_delivery':'Producer observed a delivery receipt; not proof of reading.'},
                    'limits':['Absent spans stay missing. Closed spans without a start stay incomplete evidence.',
                    'Only opaque UUIDs, enums and timings are accepted. No messages, patient identifiers or raw errors.',
                    'This is a bounded workflow contract, not an OpenTelemetry ingestion endpoint.']}
        @app.post('/api/traces/review',dependencies=[Depends(reader)])
        def review(project:str,req:TraceReview):return self.review(project,req)
        @app.post('/api/traces/detail',dependencies=[Depends(reader)])
        def detail(project:str,req:TraceDetail):
            report=self.review(project,TraceReview(**req.model_dump(exclude={'trace'})),req.trace)
            item=next((t for t in report['traces'] if hmac.compare_digest(t['trace'],req.trace)),None)
            if item is None:raise HTTPException(404,'trace is outside this review; reuse its exact query')
            return {**{k:v for k,v in report.items() if k!='traces'},'trace':item}

    def review(self,project,req,detail_reference=None):
        q=self.freeze(req)
        with closing(self.connect()) as c:
            rows=c.execute("SELECT ts,json_extract(properties,'$.afterspan_trace') AS facts FROM events WHERE project=? AND ts>=? AND ts<? AND received<=? AND json_type(properties,'$.afterspan_trace') IS NOT NULL ORDER BY ts,id LIMIT 20001",
                           (project,q.start.timestamp(),q.end.timestamp(),q.as_of.timestamp())).fetchall()
            key=c.execute("SELECT value FROM billing_settings WHERE key='receipt_key'").fetchone()[0]
        if len(rows)>20000:raise HTTPException(422,'more than 20,000 observations; shorten the window; no partial report returned')
        groups=defaultdict(list);invalid=0;other=0
        for row in rows:
            try:f=SpanObservation.model_validate(json.loads(row['facts'])).model_dump(mode='json')
            except (ValueError,TypeError):invalid+=1;continue
            if f['environment']!=q.environment:other+=1;continue
            groups[f['trace_id']].append({'ts':row['ts'],'facts':f})
        def reference_for(tid):
            return 'trace_'+hmac.new(key.encode(),(project+'\0'+q.environment+'\0'+tid).encode(),hashlib.sha256).hexdigest()[:32]
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
        return {**result,**({'linked_inputs':linked_inputs} if detail_reference else {}),'coverage':{'observations_scanned':len(rows),'invalid_observations':invalid,'other_environment_observations':other,
                                    'observed_traces':len(traces),'returned_traces':min(len(traces),q.limit),'has_more':len(traces)>q.limit},'traces':traces[:q.limit]}
