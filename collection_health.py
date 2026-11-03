"""Bounded source expectations and collection observations, never a lossless-ledger claim."""
from contextlib import closing
import json
import math
import sqlite3
import time
from typing import Literal
from fastapi import Depends,HTTPException
from pydantic import Field,StrictBool,StrictInt,UUID4,model_validator
from product_analytics import Model,Window
from tracing import Environment

Count=StrictInt
MAX_COUNT=9007199254740991
COUNTERS=('dropped','rejected','delivery_failures','evaluator_failures')
ReportingMode=Literal['continuous','on_demand']
class CollectionPulse(Model):
    schema_version:Literal['collection-health-v1']
    source:str=Field(pattern=r'^[a-z][a-z0-9_.-]{0,63}$')
    instance_id:UUID4
    environment:Environment
    workflow:str|None=Field(default=None,pattern=r'^[a-z][a-z0-9_.-]{0,63}$')
    sequence:Count=Field(ge=0,le=MAX_COUNT)
    observed_at_ms:Count=Field(ge=0,le=4102444800000)
    process_started_at_ms:Count=Field(ge=0,le=4102444800000)
    interval_seconds:Count=Field(ge=10,le=3600)
    delivery_mode:Literal['durable','memory','external']
    reporting_mode:ReportingMode='continuous'
    collector_state:Literal['running','stopped']|None=None
    queued:Count=Field(ge=0,le=MAX_COUNT)
    capacity:Count=Field(ge=1,le=MAX_COUNT)
    oldest_queued_at_ms:Count|None=Field(default=None,ge=0,le=4102444800000)
    last_delivery_at_ms:Count|None=Field(default=None,ge=0,le=4102444800000)
    dropped:Count=Field(ge=0,le=MAX_COUNT)
    rejected:Count=Field(ge=0,le=MAX_COUNT)
    delivery_failures:Count=Field(ge=0,le=MAX_COUNT)
    evaluator_failures:Count|None=Field(default=None,ge=0,le=MAX_COUNT)
    error_code:Literal['queue_full','enqueue_failed','delivery_failed','invalid_event','evaluator_failed','unknown']|None=None

    @model_validator(mode='after')
    def clocks(self):
        if self.process_started_at_ms>self.observed_at_ms:raise ValueError('process starts after pulse')
        if any(v is not None and v>self.observed_at_ms for v in [self.oldest_queued_at_ms,self.last_delivery_at_ms]):raise ValueError('collector clock exceeds pulse')
        if (self.oldest_queued_at_ms is not None)!=(self.queued>0):raise ValueError('queued events require their oldest timestamp')
        if self.queued>self.capacity:raise ValueError('queue exceeds capacity')
        if self.collector_state=='stopped' and (self.queued or self.error_code is not None):raise ValueError('stopped collector requires an empty queue and no current error')
        return self

class SourceExpectation(Model):
    source:str=Field(pattern=r'^[a-z][a-z0-9_.-]{0,63}$')
    environment:Environment
    workflow:str|None=Field(default=None,pattern=r'^[a-z][a-z0-9_.-]{0,63}$')
    interval_seconds:Count=Field(ge=10,le=3600)
    reporting_mode:ReportingMode='continuous'
    enabled:StrictBool=True

class HealthQuery(Window):
    environment:Environment
    workflow:str|None=Field(default=None,pattern=r'^[a-z][a-z0-9_.-]{0,63}$')

def pipeline_summary(properties):
    """Keep the aggregate overview compatible without exporting typed source metadata."""
    typed=properties.get('afterspan_health');value=typed if isinstance(typed,dict) else properties
    result={k:value[k] for k in ['queued','dropped','rejected','delivery_failures','evaluator_failures','capacity'] if type(value.get(k)) is int and value[k]>=0}
    if typed is not None:
        result['observed_at']=value['observed_at_ms']/1000
        result['last_delivery']=value['last_delivery_at_ms']/1000 if value.get('last_delivery_at_ms') is not None else None
    return result

class CollectionHealth:
    def __init__(self,app,connect,editor,audit_reader,freeze,evidence_metadata,retention_days):
        self.connect=connect;self.freeze=freeze;self.metadata=evidence_metadata;self.retention_days=retention_days
        @app.get('/api/collection/sources',dependencies=[Depends(audit_reader)])
        def sources(project:str):return self.sources(project)
        @app.put('/api/collection/sources',dependencies=[Depends(editor)])
        def save(project:str,req:SourceExpectation,revision:int=0):return self.save(project,req,revision)
        @app.post('/api/collection/health',dependencies=[Depends(audit_reader)])
        def health(project:str,req:HealthQuery):return self.report(project,req)

    @staticmethod
    def init_db(c):
        c.executescript('''CREATE TABLE IF NOT EXISTS collection_sources(project TEXT NOT NULL REFERENCES projects(id),source TEXT NOT NULL,
          environment TEXT NOT NULL,revision INTEGER NOT NULL,definition TEXT NOT NULL,updated REAL NOT NULL,PRIMARY KEY(project,source,environment));
          CREATE TABLE IF NOT EXISTS collection_ingestion(project TEXT NOT NULL REFERENCES projects(id),transport TEXT NOT NULL,bucket INTEGER NOT NULL,
          accepted_requests INTEGER NOT NULL,rejected_requests INTEGER NOT NULL,rejected_spans INTEGER NOT NULL,last_status INTEGER NOT NULL,
          PRIMARY KEY(project,transport,bucket));''')

    def record_ingestion(self,project,transport,status,rejected_spans=0):
        # Health failure cannot turn a committed telemetry write into a reported business failure.
        try:
            with closing(self.connect()) as c,c:
                c.execute('''INSERT INTO collection_ingestion VALUES(?,?,?,?,?,?,?) ON CONFLICT(project,transport,bucket) DO UPDATE SET
                  accepted_requests=accepted_requests+excluded.accepted_requests,rejected_requests=rejected_requests+excluded.rejected_requests,
                  rejected_spans=rejected_spans+excluded.rejected_spans,last_status=excluded.last_status''',
                  (project,transport,int(time.time()//60)*60,int(status<400),int(status>=400),rejected_spans,status))
        except sqlite3.Error:pass

    def sources(self,project):
        with closing(self.connect()) as c:rows=c.execute('SELECT * FROM collection_sources WHERE project=? ORDER BY source,environment',(project,)).fetchall()
        return {'sources':[{'revision':r['revision'],'updated':r['updated'],'definition':json.loads(r['definition'])} for r in rows],
                'limits':['Current editor-declared source expectations; not a historical deployment inventory or proof that every source is declared.']}

    def save(self,project,req,revision):
        if revision<0:raise HTTPException(422,'revision cannot be negative')
        definition=json.dumps(req.model_dump(mode='json'),sort_keys=True,separators=(',',':'))
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM collection_sources WHERE project=? AND source=? AND environment=?',(project,req.source,req.environment)).fetchone()
            old_definition=json.dumps({'reporting_mode':'continuous',**json.loads(old['definition'])},sort_keys=True,separators=(',',':')) if old else None
            if old and old['revision']==revision+1 and old_definition==definition:return {'revision':old['revision'],'definition':req.model_dump(mode='json')}
            if (old['revision'] if old else 0)!=revision:raise HTTPException(409,'source expectations changed; read current revision')
            if not old and c.execute('SELECT count(*) FROM collection_sources WHERE project=?',(project,)).fetchone()[0]>=100:raise HTTPException(422,'at most 100 expected sources per project')
            c.execute('INSERT OR REPLACE INTO collection_sources VALUES(?,?,?,?,?,?)',(project,req.source,req.environment,revision+1,definition,time.time()))
        return {'revision':revision+1,'definition':req.model_dump(mode='json')}

    def report(self,project,req):
        q=self.freeze(req);start=q.start.timestamp();cutoff=q.as_of.timestamp();end=q.end.timestamp()
        if end>cutoff or cutoff-start>90*86400:raise HTTPException(422,'collection window must end by cutoff and fit within 90 days')
        expected=[r for r in self.sources(project)['sources'] if r['definition']['environment']==q.environment and (q.workflow is None or r['definition']['workflow'] in [None,q.workflow])]
        with closing(self.connect()) as c:
            rows=c.execute("SELECT properties,ts,received FROM events WHERE project=? AND name='analytics.pipeline' AND ts>=? AND ts<=? AND received<=? ORDER BY ts LIMIT 20001",(project,start-10800,end,cutoff)).fetchall()
            counters=[dict(r) for r in c.execute('''SELECT transport,sum(accepted_requests) accepted_requests,sum(rejected_requests) rejected_requests,
                sum(rejected_spans) rejected_spans FROM collection_ingestion WHERE project=? AND bucket>=? AND bucket+60<=? GROUP BY transport''',(project,math.floor(start/60)*60,min(end,cutoff))).fetchall()]
        if len(rows)>20000:raise HTTPException(422,'collection review exceeds 20000 pulses; narrow the window')
        grouped={};legacy=0
        for r in rows:
            facts=json.loads(r['properties']).get('afterspan_health')
            if not facts:
                if r['ts']>=start:legacy+=1
                continue
            if facts['environment']!=q.environment or q.workflow is not None and facts.get('workflow') not in [None,q.workflow]:continue
            grouped.setdefault(facts['source'],[]).append(facts)
        if len(grouped)>100 or sum(len({p['instance_id'] for p in values}) for values in grouped.values())>200:raise HTTPException(422,'collection review exceeds 100 sources or 200 instances; narrow the window')
        declared={r['definition']['source']:r for r in expected};items=[];overall=[]
        if not any(r['definition']['enabled'] for r in expected):overall.append('no_enabled_source_expectations')
        for name in sorted(set(declared)|set(grouped)):
            exp=declared.get(name);pulses=grouped.get(name,[]);reasons=[]
            if exp is None:reasons.append('source_not_declared')
            elif not exp['definition']['enabled']:reasons.append('source_expectation_disabled')
            if not pulses:reasons.append('pulse_missing')
            interval=exp['definition']['interval_seconds'] if exp else pulses[-1]['interval_seconds']
            allowance=interval*3
            mode=exp['definition'].get('reporting_mode','continuous') if exp else pulses[-1].get('reporting_mode','continuous')
            if any(p.get('reporting_mode','continuous')!=mode for p in pulses):reasons.append('source_mode_mismatch')
            instances={}
            for p in pulses:instances.setdefault(p['instance_id'],[]).append(p)
            observations=[]
            for instance,values in instances.items():
                latest=values[-1];in_window=[p for p in values if p['observed_at_ms']/1000>=start]
                baseline=next((p for p in reversed(values) if p['observed_at_ms']/1000<=start),None)
                scoped=([baseline] if baseline and baseline not in in_window else [])+in_window
                issues=[];stops=[p for p in values if p.get('collector_state')=='stopped']
                conflict=bool(stops) and (len(stops)>1 or stops[0] is not latest or any(p is not stops[0] and p['sequence']>=stops[0]['sequence'] for p in values))
                lifecycle='conflicting' if conflict else 'reported_stopped' if stops else 'not_established'
                if conflict:issues.append('collector_lifecycle_conflict')
                for field in COUNTERS:
                    numbers=[p[field] for p in (values if mode=='on_demand' else scoped) if p[field] is not None]
                    if any(a>b for a,b in zip(numbers,numbers[1:])):issues.append('counter_regressed')
                if mode=='on_demand':
                    first=values[0]
                    if any(p.get('reporting_mode','continuous')!='on_demand' for p in values):issues.append('collector_mode_conflict')
                    if any(p['process_started_at_ms']!=first['process_started_at_ms'] for p in values):issues.append('collector_process_clock_conflict')
                    if any(b['sequence']!=a['sequence']+1 for a,b in zip(values,values[1:])):issues.append('invocation_sequence_gap')
                    valid_start=first['sequence']==1 and first.get('collector_state')=='running' and first['queued']==0 and first.get('reporting_mode','continuous')=='on_demand'
                    if not valid_start:issues.append('invocation_start_missing')
                    if first['observed_at_ms']-first['process_started_at_ms']>allowance*1000 or any(b['observed_at_ms']-a['observed_at_ms']>allowance*1000 for a,b in zip(values,values[1:])):issues.append('invocation_pulse_gap')
                    if lifecycle!='reported_stopped':issues.append('invocation_stop_missing')
                    if lifecycle=='reported_stopped' and latest['observed_at_ms']/1000<start and not issues:continue
                    if baseline is None and valid_start and first['process_started_at_ms']/1000>=start and all(first[k] in (0,None) for k in COUNTERS):baseline=first
                if any(p['evaluator_failures'] is None for p in scoped):issues.append('evaluator_health_unknown')
                if lifecycle!='reported_stopped' and latest['observed_at_ms']/1000<end-allowance:issues.append('pulse_stale')
                if latest['error_code']:issues.append('collector_reports_error')
                if latest['evaluator_failures'] is None:issues.append('evaluator_health_unknown')
                if latest['queued'] and latest['oldest_queued_at_ms']/1000<end-allowance:issues.append('queue_delayed')
                if latest['queued']>=latest['capacity']:issues.append('queue_full')
                if any(a['sequence']>=b['sequence'] for a,b in zip(scoped,scoped[1:])):issues.append('pulse_sequence_conflict')
                changes={k:latest[k]-baseline[k] if baseline is not None and in_window and all(p[k] is not None for p in scoped) and 'counter_regressed' not in issues else None for k in COUNTERS}
                if any(changes[k] is not None and changes[k]>0 for k in ['dropped','rejected']):issues.append('collector_reports_window_loss')
                elif any(changes[k] is None and latest[k]>0 for k in ['dropped','rejected']):issues.append('collector_loss_window_unknown')
                if changes['evaluator_failures'] is not None and changes['evaluator_failures']>0:issues.append('collector_reports_evaluator_failures')
                elif changes['evaluator_failures'] is None and latest['evaluator_failures']:issues.append('evaluator_failure_window_unknown')
                observations.append({'instance_id':instance,'latest':latest,'lifecycle':lifecycle,'issues':sorted(set(issues)),'window_counter_changes':changes})
                reasons+=issues
            # Coverage uses every declared source pulse. It does not prove that undeclared workers emitted anything.
            if mode=='continuous':
                times=sorted({p['observed_at_ms']/1000 for p in pulses})
                before=[t for t in times if t<=start];during=[t for t in times if start<t<=end]
                coverage=([max(before)] if before else [])+during
                if not coverage or coverage[0]>start or start-coverage[0]>allowance or end-coverage[-1]>allowance or any(b-a>allowance for a,b in zip(coverage,coverage[1:])):reasons.append('pulse_coverage_gap')
            elif not any(p['observed_at_ms']/1000>=start for p in pulses):reasons.append('no_invocations_observed')
            if exp and exp['updated']>start:reasons.append('expectation_changed_after_window_start')
            reasons=sorted(set(reasons));items.append({'source':name,'expectation':exp,'status':'attention' if reasons else 'observed','reasons':reasons,'instances':observations})
        if any(r['rejected_requests'] or r['rejected_spans'] for r in counters):overall.append('project_backend_reported_rejections')
        if legacy:overall.append('untyped_pipeline_reports')
        if any(i['reasons'] for i in items):overall.append('source_evidence_incomplete')
        if start<time.time()-self.retention_days*86400:overall.append('retention_gap')
        return {'status':'attention' if overall else 'observed','reasons':overall,'sources':items,'backend':counters,'backend_scope':'project_all_environments_and_workflows','legacy_pulses':legacy,
            'collection_completeness':'not_established',**self.metadata(project,q,'collection-health-v1'),
            'limits':['Source status is producer-reported; undeclared sources and pre-instrumentation requests may be absent. No pulse proves a complete task ledger.',
                'Expectations are current editor configuration, not an immutable historical inventory. Changes after the window start prevent clean coverage.',
                'Backend counters are project-wide across all environments/workflows; failures cannot be assigned to the selected task population. They cover authenticated requests in closed minute buckets overlapping this window. They count requests and OTLP rejected spans, not missing business tasks. Unattributed authentication/body/size failures and health-write failures may be absent.',
                'Loss counters are cumulative for the reported collector instance; full-window changes require a baseline at/before entry and a later in-window report. Historic nonzero totals alone do not establish new loss. Process exits can lose unsent memory queues without reporting a final pulse.',
                'Only an explicit empty-queue stop report ends instance freshness checks. This is producer-reported, not independent proof of process exit. Later activity conflicts with that stop within this bounded evidence. Historical instances without a stop remain unknown; current source coverage and counter checks still apply. A current source can have unreported workers.',
                'On-demand mode checks each observed invocation from explicit sequence-one start through drained stop; idle gaps between invocations are not continuous coverage. No observed invocation remains unavailable, not healthy idle. A lost start and entirely unreported invocation can be absent. Known evaluator failure counters do not prove any check ran or passed.']}
