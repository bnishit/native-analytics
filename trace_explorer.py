"""One trace browser over explicit contracts; never join unrelated event streams."""
from typing import Literal
from fastapi import Depends, HTTPException
from pydantic import Field
from product_analytics import Window
from audit import ReviewQuery, request_groups
from tracing import Environment, TraceReview

class TraceSearch(Window):
    environment: Environment = 'production'
    contract: Literal['all','slack-request-v1','workflow-trace-v1'] = 'all'
    search: str = Field(default='', max_length=100)
    status: Literal['all','failed','gaps','observed'] = 'all'
    min_duration_ms: float = Field(default=0, ge=0, le=86400000, allow_inf_nan=False)
    offset: int = Field(default=0, ge=0, le=200000)
    limit: int = Field(default=20, ge=1, le=60)

class TraceOpen(TraceSearch):
    reference: str = Field(pattern=r'^(req|trace)_[a-f0-9]{32}$')


def slack_trace(audit, project, key, group, q):
    row = audit.chain(project, key, group, q)
    timing = row['trace']
    spans = [{'id':str(s['operation']), 'parent':str(s['parent_operation']) if s['parent_operation'] else None,
              'step':s['step'], 'attempt':s['attempt_number'], 'state':s['state'],
              'start_ms':None if s['state']=='missing_start' else s['at_ms_from_request_start'], 'duration_ms':s['duration_ms'],
              'end_ms':s['finished_at_ms_from_request_start'], 'parent_missing':s['parent_missing']}
             for s in timing.get('spans',[])]
    environments = {r['facts']['environment'] for r in group}
    evidence = [{'name':s['stage'], 'state':s['state'], 'outcome':s['reported_outcome']} for s in row['stages']]
    evidence += [{'name':name, 'state':value['state'], 'outcome':'producer_reported'} for name,value in timing.get('completion',{}).items()]
    return {'reference':row['request'], 'contract':'slack-request-v1',
            'name':row['command_kind'] or row['request_kind'],
            'environment':row['environment'] if row['environment']!='unknown' else next(iter(environments)) if len(environments)==1 else 'conflict',
            'release':row['release_sha'], 'started_at':min(r['ts'] for r in group),
            'last_seen':max(r['ts'] for r in group), 'attempts':row['business_attempts_observed'],
            'spans':spans, 'evidence':evidence, 'issues':sorted(set(row['issues']+timing.get('issues',[])+(['timing_environment_mixed'] if len(environments)>1 else []))),
            'unobserved_categories':timing.get('missing_steps',[]), 'links':[],
            'clock':'request start' if timing.get('spans') else 'unavailable',
            'business_outcome':'not_independently_verified'}


def workflow_trace(row):
    origin=min(s['first_seen'] for s in row['spans'])
    spans=[{'id':s['span'],'parent':s['parent'],'step':s['step'],'attempt':s['attempt_number'],
            'state':s['state'],'start_ms':round((s['first_seen']-origin)*1000,3) if not s['missing_start'] else None,
            'duration_ms':s['duration_ms'],'end_ms':round((s['last_seen']-origin)*1000,3) if s['state'] in {'succeeded','failed'} and not s['missing_start'] else None,
            'parent_missing':s['parent_missing']} for s in row['spans']]
    # Absent domain-specific steps are not requirements for other products.
    evidence=[{'name':s['step'],'state':s['state'],'outcome':s['outcome_code']} for s in row['spans']]
    return {'reference':row['trace'],'contract':'workflow-trace-v1','name':row['workflow'],
            'environment':row['environment'],'release':row['release_sha'],'started_at':origin,'last_seen':row['last_seen'],
            'attempts':row['observed_attempts'],'spans':spans,'evidence':evidence,'issues':row['issues'],
            'unobserved_categories':[], 'links':row['batch']['inputs'] if row['batch'] else [],
            'clock':'first observed event; earlier work may be absent', 'business_outcome':'not_independently_verified'}


def finish_trace(row):
    gaps={'missing','unknown','conflict','unclosed','incomplete','missing_start'}
    row['has_gaps']=bool(row['issues']) or not row['spans'] or any(e['state'] in gaps for e in row['evidence']) or any(s['parent_missing'] or s['state'] in gaps for s in row['spans'])
    row['status']='failed' if any(s['state']=='failed' for s in row['spans']) else 'gaps' if row['has_gaps'] else 'observed'
    ends=[s['end_ms'] for s in row['spans'] if s['end_ms'] is not None]
    row['duration_ms']=max(ends) if ends else None
    return row

class TraceExplorer:
    def __init__(self,app,audit,workflow,reader,freeze,metadata):
        self.audit=audit;self.workflow=workflow;self.freeze=freeze;self.metadata=metadata
        @app.post('/api/traces/search',dependencies=[Depends(reader)])
        def search(project:str,req:TraceSearch):return self.search(project,req)
        @app.post('/api/traces/open',dependencies=[Depends(reader)])
        def detail(project:str,req:TraceOpen):
            return self.search(project,TraceSearch(**req.model_dump(exclude={'reference'})),req.reference)

    def search(self,project,req,reference=None):
        q=self.freeze(req);window={k:getattr(q,k) for k in ('days','start','end','as_of')}
        rows=[];coverage={}
        if q.contract in {'all','slack-request-v1'}:
            aq=ReviewQuery(**window,expected_environment=q.environment)
            _,observations,coverage,key=self.audit.load(project,aq)
            groups,uncorrelated=request_groups(observations)
            coverage['uncorrelated_observations']=dict(uncorrelated)
            rows += [slack_trace(self.audit,project,key,g,aq) for g in groups.values()]
        if q.contract in {'all','workflow-trace-v1'}:
            report=self.workflow.review(project,TraceReview(**window,environment=q.environment),all_traces=True)
            rows += [workflow_trace(r) for r in report['traces']]
            coverage['workflow']=report['coverage']
        coverage['conflicting_environment_requests']=sum(r['environment']=='conflict' for r in rows)
        coverage['other_environment_requests']=sum(r['environment'] not in {q.environment,'conflict'} for r in rows)
        rows=[finish_trace(r) for r in rows if r['environment']==q.environment]
        needle=q.search.strip().lower()
        rows=[r for r in rows if (not needle or needle in ' '.join([r['reference'],r['name'],r['release'] or '',*(s['step'] for s in r['spans'])]).lower())
              and (q.status=='all' or r['status']==q.status)
              and (q.min_duration_ms==0 or r['duration_ms'] is not None and r['duration_ms']>=q.min_duration_ms)]
        rows.sort(key=lambda r:(-r['last_seen'],r['reference']))
        result=self.metadata(project,q,'trace-explorer-v1')
        result['limits'] += ['Only retained, explicitly linked observations in the frozen window are searchable. No message text is stored or searched.',
                            'Duration is the latest observed finish relative to the labelled clock, not a guarantee of complete end-to-end coverage.',
                            'Observed means timing reports exist, not business success. Missing evidence does not prove work was skipped.',
                            'Overlapping spans must not be summed. Provider acceptance does not prove delivery, reading or answer correctness.']
        result.update(coverage=coverage,total=len(rows),has_more=q.offset+q.limit<len(rows))
        if reference:
            row=next((r for r in rows if r['reference']==reference),None)
            if row is None:raise HTTPException(404,'Trace not retained in this exact project, window and filter. Refresh the search; do not infer an outcome.')
            return {**result,'trace':row}
        return {**result,'traces':[{k:v for k,v in r.items() if k not in {'spans','evidence','links'}} for r in rows[q.offset:q.offset+q.limit]]}
