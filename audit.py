"""An explicit, allowlisted request-review contract; never infer joins from raw properties."""
import hashlib
import hmac
import json
import math
import uuid
import time
from datetime import datetime,timezone
from urllib.parse import urlsplit
from collections import Counter,defaultdict
from contextlib import closing
from typing import Annotated,Literal
from fastapi import Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field,StrictBool,UUID4,model_validator
from product_analytics import Window

SCHEMA='slack-request-v1'
TRACE_STEPS=('preparation','parser','model','feature_list','feature_lookup','policy','mutation','readback','slack_delivery','webhook_ack','background','total')
STAGES=('parser','planner','shadow','confirmation','tool','reply','quality')
Digest=Annotated[str,Field(pattern=r'^[a-f0-9]{64}$')]
class Observation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    schema_version:Literal['slack-request-v1']
    stage:Literal['parser','planner','shadow','confirmation','tool','reply','quality','trace']
    environment:Literal['production','staging','development','test','local']
    request_ref:UUID4|Literal['[redacted]']|None=None
    attempt_ref:UUID4|Literal['[redacted]']|None=None
    attempt_number:int|None=Field(default=None,ge=1,le=10000,strict=True)
    release_ref:UUID4|Literal['[redacted]']|None=None
    comparison_version:Literal['canonical-plan-v1']|None=None
    request_kind:Literal['read','write','unknown']|None=None
    command_kind:Literal['list_experiments','list_users','check_user','list_exp_parameters','other']|None=None
    release_sha:Annotated[str,Field(pattern=r'^[a-f0-9]{40}$')]|None=None
    request_started_at_ms:int|None=Field(default=None,ge=0,le=2**53-1,strict=True)
    comparison_eligible:StrictBool|None=None
    independent_proposal:StrictBool|None=None
    confirmation_required:StrictBool|None=None
    tool_required:StrictBool|None=None
    reply_required:StrictBool|None=None
    context_digest:Digest|Literal['[redacted]']|None=None
    decision_digest:Digest|Literal['[redacted]']|None=None
    proposal_safe:StrictBool|None=None
    reported_outcome:Literal['succeeded','failed','clarification','cancelled','unknown']|None=None
    diagnostic_code:Literal['none','input_mismatch','decision_mismatch','unsafe_proposal','ambiguous_request','rejected','comparison_scope_unavailable','unknown']|None=None
    duration_ms:float|None=Field(default=None,ge=0,le=86400000,allow_inf_nan=False,strict=True)
    span_ref:UUID4|None=None
    parent_span_ref:UUID4|None=None
    trace_step:Literal['preparation','parser','model','feature_list','feature_lookup','policy','mutation','readback','slack_delivery','webhook_ack','background','total']|None=None
    span_state:Literal['started','succeeded','failed']|None=None
    completion_kind:Literal['processing','delivered']|None=None
    check_version:Literal['growthbook-catalog-v1']|None=None
    checked_claims:int|None=Field(default=None,ge=0,le=15,strict=True)
    mismatched_claims:int|None=Field(default=None,ge=0,le=15,strict=True)
    unknown_claims:int|None=Field(default=None,ge=0,le=15,strict=True)
    @model_validator(mode='after')
    def quality_counts(self):
        trace_fields=(self.span_ref,self.parent_span_ref,self.trace_step,self.span_state,self.completion_kind)
        if self.stage=='trace':
            if any(v is None or v=='[redacted]' for v in (self.request_ref,self.attempt_ref,self.attempt_number,self.request_started_at_ms,self.span_ref,self.trace_step,self.span_state)):
                raise ValueError('trace observations require opaque request, attempt, span and clock')
            if (self.span_state=='started')!=(self.duration_ms is None):raise ValueError('only terminal spans have a required duration')
            expected='unknown' if self.span_state=='started' else self.span_state
            if self.reported_outcome is not None and self.reported_outcome!=expected:raise ValueError('trace outcome must match span state')
            if self.parent_span_ref==self.span_ref:raise ValueError('span cannot parent itself')
            if self.completion_kind is not None and self.trace_step!='total':raise ValueError('completion kind belongs to total')
            if self.trace_step=='total' and self.completion_kind is None:raise ValueError('total requires completion kind')
        elif any(v is not None for v in trace_fields):raise ValueError('trace fields belong to the trace stage')
        counts=(self.checked_claims,self.mismatched_claims,self.unknown_claims)
        if self.stage!='quality':
            if self.check_version is not None or any(v is not None for v in counts):raise ValueError('quality fields belong to the quality stage')
        elif self.check_version is not None:
            if any(v is None for v in counts) or self.mismatched_claims>self.checked_claims or self.checked_claims+self.unknown_claims>15:raise ValueError('inconsistent quality counts')
            outcome='unknown' if self.unknown_claims or not self.checked_claims else 'failed' if self.mismatched_claims else 'succeeded'
            if self.reported_outcome!=outcome:raise ValueError('quality outcome must match counts')
        return self

MEANINGS={
 'span_ref':'Fresh random UUIDv4 per operation. Start and terminal events share this span; delivery retries reuse their event IDs. Never exported.',
 'parent_span_ref':'Optional opaque parent span, never exported. An absent parent stays explicitly unobserved.',
 'trace_step':'Allowlisted operation category. Repeated operations get separate spans; no feature, user or message values.',
 'span_state':'Started, succeeded or failed. A missing terminal is unclosed in the selected window, not proof of continued execution.',
 'completion_kind':'Total processing and provider-accepted delivery are distinct. Neither establishes human reading or independent business success.',
 'command_kind':'Bounded parsed read-command category. Missing in older producers; never infer it from reply text or a source check.',
 'release_sha':'Exact producer Git revision. Missing or conflicting revisions cannot qualify a release-bound request outcome.',
 'request_started_at_ms':'Verified incoming event time when valid, otherwise server handling start, Unix milliseconds. It survives business retries; message sends never restart this clock.',
 'check_version':'growthbook-catalog-v1: producer-side deterministic check of the exact rendered catalog answer against a separate uncached GrowthBook read. Not a second model vote or an independent Afterspan source fetch.',
 'checked_claims':'Number of displayed catalog rows with a matching source version checked for presence, enabled state and explicit Identity $in list-entry count. Not active users or SDK eligibility.',
 'mismatched_claims':'Checked rows which disagreed with the source. Nonzero forces failed; never averaged into a clean pass.',
 'unknown_claims':'Rows not checkable, including changed or missing source versions. Missing checks and unsupported answers remain unknown.',
 'schema_version':'Version of this producer diagnostic contract; it is not an inferred event schema.',
 'stage':'One completed comparison-stage observation per business attempt, or trace start/terminal observations. Reuse the event ID for delivery retries.',
 'environment':'Producer-reported environment. A mismatch with the expected environment is reported.',
 'request_ref':'Fresh random UUIDv4 per request, shared across stages/retries. Never a Slack/message/user ID. Returned only as a project-scoped opaque server reference.',
 'attempt_ref':'Fresh random UUIDv4 per business attempt; one attempt number maps to one reference. Missing/redacted refs are never joined.',
 'attempt_number':'Increasing business-attempt number. Delivery retry keeps the same event ID and number.',
 'release_ref':'Random release UUID shared by the producer stages. Exported as an opaque server reference; missing and mixed releases block comparison.',
 'comparison_version':'canonical-plan-v1: both stages use the same canonical input and decision representation. Historical comparisons need matching semantics and release coverage.',
 'request_kind':'Explicit read/write classification. Unknown is not read.',
 'comparison_eligible':'Explicit producer eligibility. Only read requests with true eligibility and comparable primary/shadow observations enter agreement.',
 'independent_proposal':'Producer asserts the shadow proposal was computed independently, without copying the route. Explicit true is required for a comparable pair; missing/false is unavailable, not independence inferred from a stage name.',
 'confirmation_required':'True requires confirmation evidence, false means not required, absent/null means unknown.',
 'tool_required':'True requires tool evidence, false means not required, absent/null means unknown.',
 'reply_required':'True means this attempt intended a user-visible reply. Missing/null in older producers stays unknown. Reply success means the messaging provider accepted the send, not that a person read it.',
 'context_digest':'Project-keyed HMAC-SHA256 over the approved canonical planner input. Never used to join requests; never exported. Different inputs are not comparable.',
 'decision_digest':'Project-keyed HMAC-SHA256 over the approved canonical decision. Never exported. Equal digests report agreement only with matching context/release/schema semantics.',
 'proposal_safe':'Producer-reported proposal check. False is an unsafe proposal. True is not proof of business safety.',
 'reported_outcome':'Producer-reported stage result. No independent business-state check is provided by this telemetry.',
 'diagnostic_code':'Approved bounded category. comparison_scope_unavailable means the authoritative planner inputs/plan are outside this comparison contract; no baseline is substituted. Free-text reasons and feature references are forbidden.',
 'duration_ms':'Producer-measured completed-stage duration, milliseconds. Quantiles use nearest rank: sorted[ceil(p*n)-1]. Missing values are excluded with a sample count.'}
class ReviewQuery(Window):
    limit:int=Field(default=60,ge=1,le=60,strict=True)
    expected_environment:Literal['production','staging','development','test','local']='production'
class TraceQuery(ReviewQuery):request:str=Field(pattern=r'^req_[a-f0-9]{32}$')

class ReplyTracking(BaseModel):
    model_config=ConfigDict(extra='forbid')
    recipe:Literal['tracker-read-reply-v1']
    path:Literal['src/app/api/slack/route.ts']
    dependencies:list[Literal['src/app/api/slack/route.ts','src/lib/analytics/request-audit.ts']]=Field(min_length=2,max_length=2)

class ReplyGoal(BaseModel):
    model_config=ConfigDict(extra='forbid')
    schema_version:Literal['afterspan-audit-goal-v1']=Field(alias='schema')
    id:Literal['read-reply-delivery']
    question:str=Field(min_length=1,max_length=2000)
    metric:Literal['read-reply-delivery-v1']
    environment:Literal['production','staging']
    tracking:ReplyTracking

class QualityTracking(BaseModel):
    model_config=ConfigDict(extra='forbid')
    recipe:Literal['source-answer-check-v1']
    dependencies:list[Annotated[str,Field(pattern=r'^[A-Za-z0-9_./-]{1,160}$')]]=Field(min_length=1,max_length=16)
    @model_validator(mode='after')
    def relative_dependencies(self):
        if any(p.startswith('/') or '..' in p.split('/') for p in self.dependencies):raise ValueError('use relative dependency paths')
        return self

class QualityGoal(BaseModel):
    model_config=ConfigDict(extra='forbid')
    schema_version:Literal['afterspan-audit-goal-v1']=Field(alias='schema')
    id:Literal['answer-quality']
    question:str=Field(min_length=1,max_length=2000)
    source_of_truth:str=Field(min_length=1,max_length=200)
    criteria:str=Field(min_length=1,max_length=1000)
    safe_test:str=Field(min_length=1,max_length=1000)
    metric:Literal['answer-quality-v1']
    environment:Literal['production','staging']
    verifier:Literal['growthbook-catalog-v1','unconfigured']
    tracking:QualityTracking

class GoalQuery(ReviewQuery):
    goal:ReplyGoal|QualityGoal
    release:str|None=Field(default=None,pattern=r'^rel_[a-f0-9]{32}$')
    @model_validator(mode='after')
    def same_environment(self):
        if self.expected_environment!=self.goal.environment:raise ValueError('goal and review environments must match')
        return self

class HistoricalEvidence(Window):
    source:Literal['posthog']='posthog'
    source_url:str=Field(max_length=200)
    start:datetime
    end:datetime
    telemetry_schema:int=Field(ge=1,le=1000,strict=True)
    observation_count:int=Field(ge=0,le=1000000000,strict=True)
    selected_requests:int=Field(ge=0,le=60,strict=True)
    flagged_eligible:int=Field(ge=0,le=60,strict=True)
    flagged_agree:int=Field(ge=0,le=60,strict=True)
    requests_with_tool_report:int=Field(ge=0,le=60,strict=True)
    query_sha256:Digest
    @model_validator(mode='after')
    def bounded(self):
        p=urlsplit(self.source_url)
        if p.scheme!='https' or p.hostname not in {'us.posthog.com','eu.posthog.com','app.posthog.com'} or p.username or p.password or p.query or p.fragment or not __import__('re').fullmatch(r'/project/[0-9]+',p.path):raise ValueError('use a plain PostHog project URL, no access links')
        if not 0<=self.flagged_agree<=self.flagged_eligible<=self.selected_requests<=self.observation_count or self.requests_with_tool_report>self.selected_requests:raise ValueError('inconsistent evidence counts')
        return self

def available(value):return value is not None and value!='[redacted]'
def nearest(values,p):return sorted(values)[max(0,math.ceil(p*len(values))-1)] if values else None

def agreed_fact(observations,field):
    values={json.dumps(r['facts'][field],sort_keys=True) for r in observations if field in r['facts'] and r['facts'][field] is not None}
    return (json.loads(next(iter(values))) if len(values)==1 else None,len(values)>1)

def request_groups(rows):
    groups=defaultdict(list);uncorrelated=Counter()
    for row in rows:
        value=row['facts'].get('request_ref')
        if not available(value):uncorrelated['missing' if 'request_ref' not in row['facts'] else 'null' if value is None else 'redacted']+=1
        else:groups[value].append(row)
    return groups,uncorrelated

class Audit:
    @staticmethod
    def init_db(c):c.execute('CREATE TABLE IF NOT EXISTS audit_evidence(project TEXT NOT NULL REFERENCES projects(id),id TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(project,id))')
    def __init__(self,app,connect,reader,freeze,metadata,editor):
        self.connect=connect;self.freeze=freeze;self.metadata=metadata
        @app.put('/api/audit/evidence/{id}',dependencies=[Depends(editor)])
        def evidence(project:str,id:UUID4,req:HistoricalEvidence):
            # An imported aggregate is an attestation, never an independent source read.
            self.freeze(req);payload=req.model_dump_json()
            with closing(self.connect()) as c,c:
                c.execute('BEGIN IMMEDIATE');row=c.execute('SELECT payload FROM audit_evidence WHERE project=? AND id=?',(project,str(id))).fetchone()
                if row and row['payload']!=payload:raise HTTPException(409,'evidence receipts are immutable; use a new ID for a new read')
                if not row:
                    if c.execute('SELECT count(*) FROM audit_evidence WHERE project=?',(project,)).fetchone()[0]>=100:raise HTTPException(422,'maximum 100 evidence receipts per project')
                    c.execute('INSERT INTO audit_evidence VALUES(?,?,?,?)',(project,str(id),payload,time.time()))
            return {'id':str(id),'stored':True,'verification':'agent_imported_aggregate','native_comparison_denominator_changed':False}
        @app.post('/api/audit/catalog',dependencies=[Depends(reader)])
        def catalog(project:str,req:ReviewQuery):return self.catalog(project,req)
        @app.post('/api/audit/review',dependencies=[Depends(reader)])
        def review(project:str,req:ReviewQuery):return self.review(project,req)
        @app.post('/api/audit/evaluate',dependencies=[Depends(reader)])
        def evaluate(project:str,req:GoalQuery):return self.evaluate(project,req)
        @app.post('/api/audit/onboarding',dependencies=[Depends(reader)])
        def onboarding(project:str,req:GoalQuery):
            if not isinstance(req.goal,QualityGoal):raise HTTPException(422,'provide an answer-quality goal')
            result=self.evaluate(project,req)
            observed=any(r['answer_check'] and r['answer_check']['check_version']==req.goal.verifier for r in result['requests'])
            return {'goal':result['goal'],'verifier_declared':req.goal.verifier!='unconfigured','source_check_observed':observed,
                    'status':'unconfigured' if req.goal.verifier=='unconfigured' else 'observed' if observed else 'waiting_for_check','observed_reads':result['denominator'],
                    'passed':result['passed'],'unknown':result['unknown'],'next_action':result['next_action'],
                    'contract':Observation.model_json_schema(),'source_access':'producer server only; never send source credentials or reply text to Afterspan',
                    'setup':['Declare the source of truth and exact claim scope in the repo goal.',
                             'Implement a separate source read and deterministic check bound to the exact sent answer.',
                             'Keep source changes, unavailable reads, unsupported claims and missing checks unknown.',
                             'Send bounded quality-stage counts with the same request, attempt, environment and release as delivery.',
                             'Run one owner-authorized safe test; inspect trace and evaluate the frozen release window.'],
                    'limits':result['limits'],'query':result['query']}
        @app.post('/api/audit/trace',dependencies=[Depends(reader)])
        def trace(project:str,req:TraceQuery):
            report=self.review(project,ReviewQuery(**req.model_dump(exclude={'request'})))
            row=next((r for r in report['requests'] if hmac.compare_digest(r['request'],req.request)),None)
            if row is None:raise HTTPException(404,'request is outside the selected review; reuse its exact query')
            return {'request':row,'query':report['query'],'snapshot':report['snapshot'],'fingerprint':report['fingerprint'],'limits':report['limits']}
    def history(self,project,cutoff):
        with closing(self.connect()) as c:
            rows=c.execute('SELECT id,payload,created FROM audit_evidence WHERE project=? AND created<=? ORDER BY created DESC,id LIMIT 101',(project,cutoff)).fetchall()
        return [{'id':r['id'],'source_query':json.loads(r['payload']),'imported_at':r['created'],'verification':'agent_imported_aggregate_not_independently_fetched','comparison_semantics':'producer_flags_v1','source_receipt_cutoff':'not_supplied','release_coverage':'not_supplied','readiness':'not_established','combined_with_native':False} for r in rows]
    def load(self,project,req):
        q=self.freeze(req)
        with closing(self.connect()) as c:
            c.execute('BEGIN')
            params=(project,q.start.timestamp(),q.end.timestamp(),q.as_of.timestamp())
            total=c.execute('SELECT count(*) FROM events WHERE project=? AND ts>=? AND ts<? AND received<=?',params).fetchone()[0]
            rows=c.execute("SELECT ts,json_extract(properties,'$.afterspan_audit') AS facts FROM events WHERE project=? AND ts>=? AND ts<? AND received<=? AND json_type(properties,'$.afterspan_audit') IS NOT NULL ORDER BY ts,id LIMIT 200001",params).fetchall()
            key=c.execute("SELECT value FROM billing_settings WHERE key='receipt_key'").fetchone()[0]
        if len(rows)>200000:raise HTTPException(422,'audit exceeds 200,000 observations; shorten the window; no partial report was returned')
        valid=[];invalid=0
        for row in rows:
            try:
                facts=json.loads(row['facts']);Observation.model_validate(facts)
                valid.append({'ts':row['ts'],'facts':facts})
            except (ValueError,TypeError):invalid+=1
        return q,valid,{'stored_events_in_window':total,'compatible_observations':len(valid),'invalid_observations':invalid,'events_without_contract':total-len(rows),'all_diagnostic_rows_scanned':True},key
    def coverage(self,rows,key,project):
        versions=Counter((r['facts']['schema_version'],self.ref(key,project,'release',r['facts'].get('release_ref')) if available(r['facts'].get('release_ref')) else 'unavailable') for r in rows)
        return [{'schema_version':s,'release':v,'observations':n} for (s,v),n in sorted(versions.items())]
    def ref(self,key,project,kind,value):return ('req_' if kind=='request' else 'rel_')+hmac.new(key.encode(),(project+'\0'+kind+'\0'+value).encode(),hashlib.sha256).hexdigest()[:32]
    def catalog(self,project,req):
        q,rows,coverage,key=self.load(project,req);schema=Observation.model_json_schema();fields=[]
        for name,definition in schema['properties'].items():
            counts=Counter('missing' if name not in r['facts'] else 'null' if r['facts'][name] is None else 'redacted' if r['facts'][name]=='[redacted]' else type(r['facts'][name]).__name__ for r in rows)
            enums=[]
            for alternative in definition.get('anyOf',[definition]):enums.extend(alternative.get('enum',[]))
            fields.append({'property':name,'meaning':MEANINGS[name],'schema':definition,'presence':len(rows)-counts['missing'],'missing':counts['missing'],'null':counts['null'],'redacted':counts['redacted'],'types':dict(counts),'safe_enum_values':[v for v in enums if v!='[redacted]']})
        return {'contract':SCHEMA,'namespace':'properties.afterspan_audit','fields':fields,'coverage':coverage,'versions':self.coverage(rows,key,project),'raw_value_examples':False,'policy':'Only this approved contract is exposed. Arbitrary event/property names, contents, identifiers and digests are not returned.',**self.metadata(project,q,'request-audit-v2')}
    def timeline(self,group):
        rows=[r for r in group if r['facts']['stage']=='trace']
        if not rows:return {'availability':'unavailable','spans':[],'missing_steps':list(TRACE_STEPS),'completion':{},'issues':[]}
        grouped=defaultdict(list)
        for row in rows:grouped[row['facts']['span_ref']].append(row)
        ordered=sorted(grouped,key=lambda ref:(min(r['ts'] for r in grouped[ref]),ref))
        labels={ref:i+1 for i,ref in enumerate(ordered)};spans=[];issues=[]
        clock,clock_conflict=agreed_fact(group,'request_started_at_ms')
        if clock_conflict:issues.append('request_start_clock_conflict')
        for ref in ordered:
            seen=grouped[ref];facts=[r['facts'] for r in seen];first=facts[0]
            starts=[r for r in seen if r['facts']['span_state']=='started'];ends=[r for r in seen if r['facts']['span_state']!='started']
            fields=('trace_step','attempt_ref','attempt_number','request_started_at_ms','parent_span_ref','completion_kind','environment','release_sha')
            conflict=any(len({json.dumps(f.get(k)) for f in facts})>1 for k in fields)
            conflict=conflict or len(starts)>1 or len(ends)>1 or bool(starts and ends and starts[0]['ts']>ends[0]['ts'])
            state='conflict' if conflict else 'unclosed' if not ends else 'missing_start' if not starts else ends[0]['facts']['span_state']
            parent=first.get('parent_span_ref');parent_missing=bool(parent and parent not in labels)
            if conflict:issues.append('conflicting_span_reports')
            if parent_missing:issues.append('parent_span_missing_in_window')
            if any(r['ts']*1000<first['request_started_at_ms'] for r in seen):issues.append('trace_before_request_start')
            spans.append({'operation':labels[ref],'parent_operation':labels.get(parent),'parent_missing':parent_missing,
                          'step':first['trace_step'],'attempt_number':first['attempt_number'],'state':state,
                          'at_ms_from_request_start':round((min(r['ts'] for r in seen)*1000)-first['request_started_at_ms'],3),
                          'duration_ms':ends[0]['facts']['duration_ms'] if len(ends)==1 and not conflict else None,
                          'finished_at_ms_from_request_start':round(ends[0]['ts']*1000-clock,3) if len(ends)==1 and not conflict and clock is not None and not clock_conflict else None,
                          'completion_kind':first.get('completion_kind')})
        completion={}
        for name,step,kind in [('webhook_ack','webhook_ack',None),('processing','total','processing'),('delivery','slack_delivery',None)]:
            selected=[s for s in spans if s['step']==step and s['completion_kind']==kind]
            # Retain every send; a completion summary cannot choose between multiple operations.
            latest=max((s['attempt_number'] for s in selected),default=None)
            selected=[s for s in selected if s['attempt_number']==latest]
            if name=='processing' and selected:
                phase=max(s['at_ms_from_request_start'] for s in selected)
                selected=[s for s in selected if s['at_ms_from_request_start']==phase]
            completion[name]={'state':selected[0]['state'] if len(selected)==1 else 'missing' if not selected else 'conflict',
                              'duration_ms':selected[0]['duration_ms'] if len(selected)==1 else None,
                              'finished_at_ms_from_request_start':selected[0]['finished_at_ms_from_request_start'] if len(selected)==1 else None,'attempt_number':latest}
            if name=='processing' and len(selected)==1 and selected[0]['state'] in {'succeeded','failed'}:
                end=selected[0]['finished_at_ms_from_request_start']
                later_work=end is None or any(r['facts']['attempt_number']>=latest and r['facts']['trace_step'] not in {'total','webhook_ack'}
                    and round(r['ts']*1000-clock,3)>end for r in rows)
                if later_work:completion[name].update(state='incomplete',duration_ms=None,finished_at_ms_from_request_start=None)
        return {'availability':'observed','spans':spans,'missing_steps':[step for step in TRACE_STEPS if not any(s['step']==step for s in spans)],
                'completion':completion,'issues':sorted(set(issues))}
    def chain(self,project,key,group,q):
        trace=self.timeline(group)
        # Timing evidence never changes comparison eligibility or the seven-stage audit.
        comparison_group=[r for r in group if r['facts']['stage']!='trace']
        facts=[r['facts'] for r in (comparison_group or group)];group=comparison_group or group;reference=self.ref(key,project,'request',facts[0]['request_ref']);environments={f['environment'] for f in facts}
        issues=[];slots=defaultdict(list);attempt_ids=defaultdict(set)
        for row in group:
            f=row['facts']
            if not available(f.get('attempt_ref')) or f.get('attempt_number') is None:issues.append('attempt_key_unavailable');continue
            slots[f['attempt_number']].append(row);attempt_ids[f['attempt_ref']].add(f['attempt_number'])
        if any(len(v)>1 for v in attempt_ids.values()):issues.append('attempt_reference_reused')
        if any(len({r['facts']['attempt_ref'] for r in v})>1 for v in slots.values()):issues.append('attempt_slot_conflict')
        latest=max(slots) if slots else None;observations=slots[latest] if slots else []
        releases={r['facts'].get('release_ref') for r in observations}
        if any(r['facts'].get('diagnostic_code')=='comparison_scope_unavailable' for r in observations):issues.append('comparison_scope_unavailable')
        if len(environments)>1:issues.append('environment_conflict')
        if any(env!=q.expected_environment for env in environments):issues.append('unexpected_environment')
        if len(releases)!=1 or not all(available(r) for r in releases):issues.append('release_unavailable_or_mixed')
        by_stage=defaultdict(list)
        for row in observations:
            if row['facts']['stage'] in STAGES:by_stage[row['facts']['stage']].append(row)
        if any(len(v)>1 for v in by_stage.values()):issues.append('duplicate_stage_observations')
        values={}
        for field in ['request_kind','command_kind','release_sha','request_started_at_ms','comparison_eligible','confirmation_required','tool_required','reply_required']:
            values[field],conflict=agreed_fact(observations,field)
            if conflict:issues.append(field+'_conflict')
        stages=[]
        for name in STAGES:
            required=True if name in ['parser','planner'] else values['comparison_eligible'] if name=='shadow' else None if name=='quality' else values[name+'_required']
            seen=by_stage.get(name,[])
            state='unknown' if not observations else 'observed' if len(seen)==1 else 'conflict' if len(seen)>1 else 'missing' if required is True else 'not_required' if required is False else 'unknown'
            if seen and required is False:issues.append('unexpected_'+name)
            stages.append({'stage':name,'required':required,'state':state,'observations':len(seen),
                'reported_outcome':seen[0]['facts'].get('reported_outcome','unknown') if len(seen)==1 else 'unknown',
                'proposal_safe':seen[0]['facts'].get('proposal_safe') if len(seen)==1 else None,
                'diagnostic_code':seen[0]['facts'].get('diagnostic_code','unknown') if len(seen)==1 else 'unknown',
                'duration_ms':seen[0]['facts'].get('duration_ms') if len(seen)==1 else None,'at_ms_from_first_observation':round((seen[0]['ts']-min(r['ts'] for r in group))*1000,3) if len(seen)==1 else None})
        parser=by_stage.get('parser',[]);planner=by_stage.get('planner',[]);shadow=by_stage.get('shadow',[])
        comparison='unavailable';declared=values['request_kind']=='read' and values['comparison_eligible'] is True
        if values['request_kind']=='write' and values['comparison_eligible'] is True:issues.append('write_marked_comparison_eligible')
        if values['comparison_eligible'] is False:comparison='not_eligible'
        elif declared and not issues and len(parser)==len(planner)==len(shadow)==1:
            p=planner[0]['facts'];s=shadow[0]['facts']
            if p.get('comparison_version')!=s.get('comparison_version') or not p.get('comparison_version'):issues.append('comparison_version_unavailable_or_mixed')
            elif s.get('independent_proposal') is not True:issues.append('independent_proposal_not_confirmed')
            elif not all(available(x.get(field)) for x in [p,s] for field in ['context_digest','decision_digest']):issues.append('comparison_digest_unavailable')
            elif p['context_digest']!=s['context_digest']:issues.append('planner_input_mismatch')
            else:comparison='agree' if p['decision_digest']==s['decision_digest'] else 'disagree'
        elif declared:issues.append('incomplete_or_conflicting_comparison')
        ordering=[by_stage[s][0]['ts'] for s in ['parser','planner','confirmation','tool','reply','quality'] if len(by_stage.get(s,[]))==1]
        if ordering!=sorted(ordering):issues.append('stage_time_conflict');comparison='unavailable'
        return {'request':reference,'environment':('conflict' if len(environments)!=1 else next(iter(environments))) if comparison_group else 'unknown',
            'environment_mismatch':bool(comparison_group) and any(env!=q.expected_environment for env in environments),'request_kind':values['request_kind'] or 'unknown',
            'command_kind':values['command_kind'],'release_sha':values['release_sha'],'request_started_at_ms':values['request_started_at_ms'],
            'business_attempts_observed':len(slots),'latest_attempt_number':latest,'missing_attempt_numbers':max(slots)-len(slots) if slots else None,
            'comparison_declared_eligible':declared,'comparison':comparison,'trace':trace,'stages':stages,'issues':sorted(set(issues)),
            'producer_reported_unsafe':any(f.get('proposal_safe') is False for f in facts),'business_state':'not_independently_verified',
            'release_coverage':[self.ref(key,project,'release',r) if available(r) else 'unavailable' for r in sorted(releases,key=str)],
            'answer_check':({field:by_stage['quality'][0]['facts'].get(field) for field in ['check_version','checked_claims','mismatched_claims','unknown_claims','reported_outcome']}
                            if len(by_stage.get('quality',[]))==1 else None)}
    def evaluate(self,project,req):
        report=self.review(project,ReviewQuery(**req.model_dump(exclude={'goal','release'})))
        reads=[r for r in report['requests'] if r['request_kind']=='read' and r['environment']==req.goal.environment
               and not r['environment_mismatch'] and (req.release is None or r['release_coverage']==[req.release])]
        outcomes=Counter()
        for row in reads:
            reply=next(s for s in row['stages'] if s['stage']=='reply')
            invalid=(any(issue!='comparison_scope_unavailable' for issue in row['issues'])
                     or any(s['required'] is True and s['state']!='observed' for s in row['stages']))
            outcome=reply['reported_outcome'] if reply['state']=='observed' and reply['required'] is True and not invalid else 'unknown'
            outcomes[outcome if outcome in {'succeeded','failed'} else 'unknown']+=1
        count=len(reads);coverage=report['coverage']
        uncertain=sum(r['request_kind']=='unknown' or r['environment']=='conflict'
                      or 'request_kind_conflict' in r['issues']
                      or req.release is not None and 'release_unavailable_or_mixed' in r['issues'] for r in report['requests'])
        complete=(coverage['selection_complete_for_correlated_window'] and not coverage['invalid_observations']
                  and not any(coverage['uncorrelated_observations'].values()) and not uncertain)
        if isinstance(req.goal,QualityGoal):
            checked=Counter()
            for row in reads:
                quality=row['answer_check'];reply=next(s for s in row['stages'] if s['stage']=='reply')
                valid=quality and req.goal.verifier!='unconfigured' and quality['check_version']==req.goal.verifier
                valid=valid and not any(i!='comparison_scope_unavailable' for i in row['issues']) and all(s['state']=='observed' for s in row['stages'] if s['required'] is True)
                valid=valid and reply['required'] is True and reply['reported_outcome']=='succeeded'
                outcome=quality['reported_outcome'] if valid else 'unknown'
                checked[outcome if outcome in {'succeeded','failed'} else 'unknown']+=1
            return {'goal':req.goal.model_dump(by_alias=True),'definition_verification':'caller_supplied_data_not_action_authority',
                    'metric':'answer-quality-v1','denominator':count,'passed':checked['succeeded'],'failed':checked['failed'],'unknown':checked['unknown'],
                    'rate':checked['succeeded']/count if count and complete and not checked['unknown'] else None,
                    'selection_complete':complete,'release':req.release,'release_bound':req.release is not None,
                    'verification':'producer_source_check_not_independently_fetched_by_afterspan',
                    'coverage':coverage,'query':report['query'],'snapshot':report['snapshot'],'fingerprint':report['fingerprint'],'requests':reads,
                    'next_action':'configure_source_check' if req.goal.verifier=='unconfigured' else 'collect_real_requests' if not count else 'inspect_unassessed_answers' if checked['unknown'] else 'inspect_wrong_answers' if checked['failed'] else 'keep_observing',
                    'limits':['Checks cover displayed catalog names, enabled state and explicit Identity $in list-entry counts only; other answer kinds remain unassessed.',
                              'Source checks run in the producer with a separate uncached read, not in the answering model. Afterspan validates bounded reports; it does not fetch GrowthBook or certify the producer.',
                              'Changed source versions, unavailable reads, missing checks and incomplete selection prevent a clean rate.',
                              'A passed source check is not full feature eligibility, human reading, customer benefit or causal improvement.']}
        return {'goal':req.goal.model_dump(by_alias=True),'definition_verification':'caller_supplied_data_not_action_authority',
                'metric':'read-reply-delivery-v1','denominator':count,'delivered':outcomes['succeeded'],
                'failed':outcomes['failed'],'unknown':outcomes['unknown'],
                'rate':outcomes['succeeded']/count if count and complete and not outcomes['unknown'] else None,
                'selection_complete':complete,'release':req.release,'release_bound':req.release is not None,
                'coverage':{**coverage,'uncertain_request_classifications':uncertain,'excluded_selected_requests':len(report['requests'])-count},
                'query':report['query'],'snapshot':report['snapshot'],'fingerprint':report['fingerprint'],
                'next_action':'collect_real_requests' if not count else 'repair_tracking' if outcomes['unknown'] else 'inspect_failed_replies' if outcomes['failed'] else 'keep_observing',
                'limits':['Only selected observed read request chains are counted; untracked messages are not a denominator.',
                          'A delivery report is not proof of human reading, answer correctness or business success.',
                          'Unknown or omitted requests keep the rate unavailable. Older producers have no reply evidence.',
                          'Before/after is observational. A PR or merge is not deployment evidence.'],
                'requests':reads}
    def repo_outcome(self,project,definition,window):
        from loop_onboarding import digest
        metric,mapping=definition['measurement'],definition['mapping']
        start,end,through=[datetime.fromisoformat(window[k]).timestamp() for k in ['entry_start','entry_end','observed_through']]
        try:
            query=ReviewQuery(start=window['entry_start'],end=window['observed_through'],as_of=window['as_of'],expected_environment=mapping['environment'])
        except ValueError:
            raise HTTPException(422,'request observation span must fit the audit window') from None
        q,rows,loaded,key=self.load(project,query)
        groups,uncorrelated=request_groups(rows)
        counts=dict.fromkeys(['total','success','failure','unclassified'],0)
        coverage={'source_total':len(groups),'environment_missing':0,'release_missing':0,'command_missing':0,
                  'start_missing':0,'conflicting_requests':0,'uncorrelated_observations':sum(uncorrelated.values()),
                  'invalid_observations':loaded['invalid_observations'],'waiting':0,'other_reads':0,'eligible_started':0,
                  'delivered':0,'source_passed':0,'late_replies':0,'status':'bounded_request_read'}
        for group in groups.values():
            row=self.chain(project,key,group,q)
            if row['environment']=='conflict':coverage['environment_missing']+=1;continue
            if row['environment']!=mapping['environment'] or row['request_kind']=='write':continue
            if row['request_kind']!='read' or row['command_kind'] is None:
                coverage['command_missing']+=1;continue
            if row['command_kind']!=metric['command_kind']:
                coverage['other_reads']+=1;continue
            entered,conflict=agreed_fact(group,'request_started_at_ms')
            if entered is None or conflict or any(r['ts']<entered/1000 for r in group):
                coverage['start_missing']+=1;continue
            entered/=1000
            if not start<=entered<end:continue
            if window.get('release'):
                release,conflict=agreed_fact(group,'release_sha')
                if not release or conflict or any(not r['facts'].get('release_sha') for r in group):
                    coverage['release_missing']+=1;continue
                if release!=window['release']:continue
            coverage['eligible_started']+=1
            if entered+metric['deadline_seconds']>through:
                coverage['waiting']+=1;continue
            counts['total']+=1
            issues=[i for i in row['issues'] if i!='comparison_scope_unavailable']
            reply=next(s for s in row['stages'] if s['stage']=='reply')
            quality=row['answer_check']
            usable=not issues and all(s['state']=='observed' for s in row['stages'] if s['required'] is True)
            usable=usable and all(next(s for s in row['stages'] if s['stage']==stage)['required'] in (True,False) for stage in ['confirmation','tool'])
            usable=usable and not any(s['stage'] in ['parser','planner','tool'] and s['reported_outcome']=='failed' for s in row['stages'])
            if issues:coverage['conflicting_requests']+=1
            reply_rows=[r for r in group if r['facts']['stage']=='reply' and r['facts'].get('attempt_number')==row['latest_attempt_number']]
            delivered=usable and reply['required'] is True and reply['state']=='observed' and reply['reported_outcome']=='succeeded'
            passed=bool(quality and quality['check_version']==metric['verifier'] and quality['reported_outcome']=='succeeded')
            late=bool(delivered and len(reply_rows)==1 and reply_rows[0]['ts']>entered+metric['deadline_seconds'])
            coverage['delivered']+=bool(delivered);coverage['source_passed']+=bool(delivered and passed);coverage['late_replies']+=late
            failed=usable and reply['required'] is True and reply['state']=='observed' and (reply['reported_outcome']=='failed'
                    or delivered and (late or quality and quality['check_version']==metric['verifier'] and (quality['reported_outcome']=='failed' or (quality.get('mismatched_claims') or 0)>0)))
            outcome='failure' if failed else 'success' if delivered and passed else 'unclassified'
            counts[outcome]+=1
        with closing(self.connect()) as c:
            erasure=c.execute('SELECT 1 FROM erased_identities WHERE project=? AND erased>=? LIMIT 1',(project,start)).fetchone()
        coverage.update(request_coverage_gap=any(coverage[k] for k in ['environment_missing','release_missing','command_missing','start_missing',
                         'conflicting_requests','uncorrelated_observations','invalid_observations']),erasure_observed=bool(erasure))
        result={'provenance':'native_request_audit_read','source_verified':True,'counts':counts,'coverage':coverage,
                'query_sha256':digest({'contract':metric['kind'],'definition':definition,'window':window}),
                'response_sha256':digest({'counts':counts,'coverage':coverage}),'fetched_at':datetime.now(timezone.utc).isoformat(),'window':window,
                'limitations':['Pass requires accepted Slack delivery within the deadline and a producer-side source check of displayed catalog facts.',
                               'Afterspan validates stored reports; it does not fetch GrowthBook or establish answer completeness or human usefulness.',
                               'Missing starts, unsupported command classification, conflicting evidence and missing source checks cannot qualify.',
                               'Requests still within their follow-up window are waiting, outside the mature denominator.']}
        return result

    def review(self,project,req):
        q,rows,coverage,key=self.load(project,req);history=self.history(project,q.as_of.timestamp());groups,uncorrelated=request_groups(rows)
        selected=sorted(groups.values(),key=lambda g:(-max(r['ts'] for r in g),g[0]['facts']['request_ref']))[:q.limit]
        requests=[self.chain(project,key,g,q) for g in selected]
        denominator=sum(r['comparison'] in ['agree','disagree'] for r in requests);numerator=sum(r['comparison']=='agree' for r in requests)
        stage_counts={stage:dict(Counter(next(s['state'] for s in r['stages'] if s['stage']==stage) for r in requests)) for stage in STAGES}
        latency={}
        latency_requests=[r for r in requests if r['environment']==q.expected_environment and not r['environment_mismatch']]
        for stage in STAGES:
            values=[s['duration_ms'] for r in latency_requests for s in r['stages'] if s['stage']==stage and s['state']=='observed' and s['duration_ms'] is not None]
            latency[stage]={'sample_count':len(values),'missing_or_conflicting_requests':len(latency_requests)-len(values),'p50_ms':nearest(values,.5),'p95_ms':nearest(values,.95),'method':'nearest_rank'}
        unsafe=sum(r['producer_reported_unsafe'] for r in requests)
        cohorts=defaultdict(lambda:{'numerator':0,'denominator':0,'selected_requests':0})
        for row in requests:
            cohort=cohorts['|'.join(row['release_coverage']) or 'unavailable'];cohort['selected_requests']+=1
            cohort['denominator']+=row['comparison'] in ['agree','disagree'];cohort['numerator']+=row['comparison']=='agree'
        return {'verdict':'needs_attention' if unsafe or any(r['comparison']=='disagree' or r['environment_mismatch'] or r['issues'] or any(s['state'] in {'missing','conflict'} for s in r['stages']) for r in requests) else 'inconclusive',
            'readiness':'inconclusive','business_safety':'not_established','semantic_correctness':'not_established','latency_scope':{'environment':q.expected_environment,'selected_requests':len(latency_requests),'excluded_environment_requests':len(requests)-len(latency_requests)},'agreement':{'numerator':numerator,'denominator':denominator,'rate':numerator/denominator if denominator else None,'unit':'one comparable latest business attempt per distinct request','small_sample':denominator<20,'sample_floor':20,'sample_floor_is_not_release_clearance':True},
            'denominators':{'observed_request_chains_in_window':len(groups),'selected_requests':len(requests),'declared_eligible_read_requests':sum(r['comparison_declared_eligible'] for r in requests),'independent_eligible_comparisons':denominator},
            'coverage':{**coverage,'uncorrelated_observations':dict(uncorrelated),'omitted_request_chains':max(0,len(groups)-len(requests)),'selection_complete_for_correlated_window':len(groups)<=q.limit,'stages_are_complete_only_for_observed_window':True},
            'stage_counts':stage_counts,'counts':{'disagreements':denominator-numerator,'unsafe_proposals_reported':unsafe,'requests_with_unknown_proposal_safety':sum(not any(s['proposal_safe'] is not None for s in r['stages']) for r in requests),'environment_mistakes':sum(r['environment_mismatch'] for r in requests),'planner_input_mismatches':sum('planner_input_mismatch' in r['issues'] for r in requests),'requests_with_unavailable_comparison_scope':sum('comparison_scope_unavailable' in r['issues'] for r in requests),'repeated_business_attempts':sum(max(0,r['business_attempts_observed']-1) for r in requests)},
            'historical_evidence':history,'comparison_cohorts':[{'release':k,**v} for k,v in sorted(cohorts.items())],'latency':latency,'versions':self.coverage(rows,key,project),'requests':requests,
            'meaning':['Stored event volume is not request volume. Events without the explicit contract are uncovered, not irrelevant.','Missing means a required stage was not observed in this evidence window. It does not prove the app skipped it.','Only explicit false means not required. Missing/null is unknown. Redacted keys and digests never join or compare.','Agreement requires a producer-confirmed independent shadow and matching canonical inputs, release and comparison semantics. Two agreeing proposals can both be wrong. No supervised ground truth or independent business-state check is provided. No sample size grants parsing accuracy, readiness or overall-safety clearance.','Versions are reported, never silently combined with historical stores. Prior same-version evidence remains historical until coverage and semantics are compared.'],
            **self.metadata(project,q,'request-audit-v2')}
