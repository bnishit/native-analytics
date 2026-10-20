"""An explicit, allowlisted request-review contract; never infer joins from raw properties."""
import hashlib
import hmac
import json
import math
import uuid
import time
from datetime import datetime
from urllib.parse import urlsplit
from collections import Counter,defaultdict
from contextlib import closing
from typing import Annotated,Literal
from fastapi import Depends,HTTPException
from pydantic import BaseModel,ConfigDict,Field,StrictBool,UUID4,model_validator
from product_analytics import Window

SCHEMA='slack-request-v1'
STAGES=('parser','planner','shadow','confirmation','tool','reply')
Digest=Annotated[str,Field(pattern=r'^[a-f0-9]{64}$')]
class Observation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    schema_version:Literal['slack-request-v1']
    stage:Literal['parser','planner','shadow','confirmation','tool','reply']
    environment:Literal['production','staging','development','test','local']
    request_ref:UUID4|Literal['[redacted]']|None=None
    attempt_ref:UUID4|Literal['[redacted]']|None=None
    attempt_number:int|None=Field(default=None,ge=1,le=10000,strict=True)
    release_ref:UUID4|Literal['[redacted]']|None=None
    comparison_version:Literal['canonical-plan-v1']|None=None
    request_kind:Literal['read','write','unknown']|None=None
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

MEANINGS={
 'schema_version':'Version of this producer diagnostic contract; it is not an inferred event schema.',
 'stage':'One completed stage observation per business attempt. Reuse the event ID for delivery retries.',
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

class GoalQuery(ReviewQuery):
    goal:ReplyGoal
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
    def chain(self,project,key,group,q):
        facts=[r['facts'] for r in group];reference=self.ref(key,project,'request',facts[0]['request_ref']);environments={f['environment'] for f in facts}
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
        for row in observations:by_stage[row['facts']['stage']].append(row)
        if any(len(v)>1 for v in by_stage.values()):issues.append('duplicate_stage_observations')
        values={}
        for field in ['request_kind','comparison_eligible','confirmation_required','tool_required','reply_required']:
            values[field],conflict=agreed_fact(observations,field)
            if conflict:issues.append(field+'_conflict')
        stages=[]
        for name in STAGES:
            required=True if name in ['parser','planner'] else values['comparison_eligible'] if name=='shadow' else values[name+'_required']
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
        ordering=[by_stage[s][0]['ts'] for s in ['parser','planner','confirmation','tool','reply'] if len(by_stage.get(s,[]))==1]
        if ordering!=sorted(ordering):issues.append('stage_time_conflict');comparison='unavailable'
        return {'request':reference,'environment':'conflict' if len(environments)!=1 else next(iter(environments)),
            'environment_mismatch':any(env!=q.expected_environment for env in environments),'request_kind':values['request_kind'] or 'unknown',
            'business_attempts_observed':len(slots),'latest_attempt_number':latest,'missing_attempt_numbers':max(slots)-len(slots) if slots else None,
            'comparison_declared_eligible':declared,'comparison':comparison,'stages':stages,'issues':sorted(set(issues)),
            'producer_reported_unsafe':any(f.get('proposal_safe') is False for f in facts),'business_state':'not_independently_verified',
            'release_coverage':[self.ref(key,project,'release',r) if available(r) else 'unavailable' for r in sorted(releases,key=str)]}
    def evaluate(self,project,req):
        report=self.review(project,ReviewQuery(**req.model_dump(exclude={'goal','release'})))
        reads=[r for r in report['requests'] if r['request_kind']=='read' and r['environment']==req.goal.environment
               and not r['environment_mismatch'] and (req.release is None or r['release_coverage']==[req.release])]
        outcomes=Counter()
        for row in reads:
            reply=next(s for s in row['stages'] if s['stage']=='reply')
            invalid=any(issue!='comparison_scope_unavailable' for issue in row['issues'])
            outcome=reply['reported_outcome'] if reply['state']=='observed' and reply['required'] is True and not invalid else 'unknown'
            outcomes[outcome if outcome in {'succeeded','failed'} else 'unknown']+=1
        count=len(reads);complete=report['coverage']['selection_complete_for_correlated_window']
        return {'goal':req.goal.model_dump(by_alias=True),'definition_verification':'caller_supplied_data_not_action_authority',
                'metric':'read-reply-delivery-v1','denominator':count,'delivered':outcomes['succeeded'],
                'failed':outcomes['failed'],'unknown':outcomes['unknown'],
                'rate':outcomes['succeeded']/count if count and complete and not outcomes['unknown'] else None,
                'selection_complete':complete,'release':req.release,'release_bound':req.release is not None,
                'query':report['query'],'snapshot':report['snapshot'],'fingerprint':report['fingerprint'],
                'next_action':'collect_real_requests' if not count else 'repair_tracking' if outcomes['unknown'] else 'inspect_failed_replies' if outcomes['failed'] else 'keep_observing',
                'limits':['Only selected observed read request chains are counted; untracked messages are not a denominator.',
                          'A delivery report is not proof of human reading, answer correctness or business success.',
                          'Unknown or omitted requests keep the rate unavailable. Older producers have no reply evidence.',
                          'Before/after is observational. A PR or merge is not deployment evidence.'],
                'requests':reads}
    def review(self,project,req):
        q,rows,coverage,key=self.load(project,req);history=self.history(project,q.as_of.timestamp());groups=defaultdict(list);uncorrelated=Counter()
        for row in rows:
            value=row['facts'].get('request_ref')
            if not available(value):uncorrelated['missing' if 'request_ref' not in row['facts'] else 'null' if value is None else 'redacted']+=1
            else:groups[value].append(row)
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
