"""Connect fixed experiments to existing verified release receipts and mature outcomes."""
from contextlib import closing
from datetime import datetime,timedelta
import json
import logging
import time
import uuid
from fastapi import Depends,HTTPException,Query
from pydantic import Field,model_validator
from collection_health import HealthQuery
from outcomes import OutcomeBinding,OutcomeQuery,OutcomeGoal
from product_analytics import Model,Window

log=logging.getLogger(__name__)
from product_loop import released_artifact
from regressions import compare_runs
from sdk.python.afterspan_experiments import digest
from tracing import Environment

class FrozenWindow(Window):
    @model_validator(mode='after')
    def frozen(self):
        if any(getattr(self,k) is None for k in ['start','end','as_of']):raise ValueError('release measurement requires explicit frozen start/end/as_of')
        if self.end>self.as_of:raise ValueError('entry window must end by its receipt cutoff')
        if self.as_of.timestamp()>time.time()+1:raise ValueError('snapshot cannot be in the future')
        return self

class ReleaseComparison(Model):
    outcome_goal:OutcomeBinding
    release_action:uuid.UUID
    baseline_release_action:uuid.UUID
    baseline_run:uuid.UUID
    candidate_run:uuid.UUID
    before:FrozenWindow
    after:FrozenWindow

class ReleaseMonitorCreate(Model):
    id:uuid.UUID
    query:ReleaseComparison
    regression_threshold:float=Field(default=.05,gt=0,le=1)

def public_release(action,release):
    return {'action':action['id'],'repository':action['plan'].get('repository'),
            **{k:release.get(k) for k in ['provider','commit','base_commit','environment','released_at','deployment_id','timestamp_basis']}}

class ReleaseOutcomes:
    def __init__(self,app,connect,editor,outcomes,regressions,loop,collection,release_evidence=None):
        self.release_evidence=release_evidence
        self.connect=connect;self.outcomes=outcomes;self.regressions=regressions;self.loop=loop;self.collection=collection
        @app.get('/api/outcomes/releases',dependencies=[Depends(editor)])
        def releases(project:str,environment:Environment,limit:int=Query(20,ge=1,le=100),offset:int=Query(0,ge=0,le=10000)):
            return self.list(project,environment,limit,offset)
        @app.post('/api/outcomes/releases/compare',dependencies=[Depends(editor)])
        def compare(project:str,req:ReleaseComparison):return self.compare(project,req)
        @app.get('/api/outcomes/release-monitors',dependencies=[Depends(editor)])
        def monitors(project:str):return self.list_monitors(project)
        @app.post('/api/outcomes/release-monitors',dependencies=[Depends(editor)])
        def create_monitor(project:str,req:ReleaseMonitorCreate):return self.create_monitor(project,req)
        @app.post('/api/outcomes/release-monitors/{monitor_id}/evaluate',dependencies=[Depends(editor)])
        def evaluate_monitor(monitor_id:uuid.UUID,project:str):return self.evaluate_monitor(project,str(monitor_id))
        @app.get('/api/outcomes/release-monitors/{monitor_id}/alerts',dependencies=[Depends(editor)])
        def monitor_alerts(monitor_id:uuid.UUID,project:str,limit:int=Query(50,ge=1,le=100)):
            return self.monitor_alerts(project,str(monitor_id),limit)
        @app.delete('/api/outcomes/release-monitors/{monitor_id}',dependencies=[Depends(editor)])
        def disable_monitor(monitor_id:uuid.UUID,project:str,revision:int):
            return self.disable_monitor(project,str(monitor_id),revision)

    def list_monitors(self,project):
        with closing(self.connect()) as c:
            rows=c.execute('SELECT id,definition,state,revision,active,created,updated FROM release_monitors WHERE project=? ORDER BY active DESC,created DESC LIMIT 50',(project,)).fetchall()
        return {'monitors':[{'id':r['id'],'definition':json.loads(r['definition']),'state':r['state'],'revision':r['revision'],'active':bool(r['active']),'created':r['created'],'updated':r['updated']} for r in rows],
            'limits':['At most 50 active monitors service-wide. Checks run hourly. Unknown evidence never triggers an alert.']}

    def create_monitor(self,project,req):
        monitor_id=str(req.id);query=req.query.model_dump(mode='json')
        with closing(self.connect()) as c:
            existing=c.execute('SELECT project,definition,state,revision,created,active FROM release_monitors WHERE id=?',(monitor_id,)).fetchone()
        if existing:
            if existing['project']!=project:raise HTTPException(404,'release monitor not found')
            saved=json.loads(existing['definition'])
            if saved.get('query')!=query or saved.get('regression_threshold')!=req.regression_threshold:
                raise HTTPException(409,'monitor ID already exists with a different definition')
            return {'id':monitor_id,'state':existing['state'],'revision':existing['revision'],'active':bool(existing['active']),
                'created':existing['created'],'reused':True}
        preview=self.compare(project,req.query)
        blocked=[reason for reason in preview['reasons'] if not reason.startswith('after_')]
        if blocked:raise HTTPException(409,{'message':'release baseline is not ready for monitoring','reasons':blocked})
        now=time.time()
        definition={'query':query,'regression_threshold':req.regression_threshold,
            'outcome_title':preview.get('goal_definition',{}).get('title','Outcome goal')}
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE')
            existing=c.execute('SELECT project,definition,state,revision,created,active FROM release_monitors WHERE id=?',(monitor_id,)).fetchone()
            if existing:
                if existing['project']!=project:raise HTTPException(404,'release monitor not found')
                if json.loads(existing['definition'])!=definition:raise HTTPException(409,'monitor ID already exists with a different definition')
                return {'id':monitor_id,'state':existing['state'],'revision':existing['revision'],'active':bool(existing['active']),'created':existing['created'],'reused':True}
            if c.execute('SELECT count(*) FROM release_monitors WHERE active=1').fetchone()[0]>=50:
                raise HTTPException(429,'maximum active release monitors reached')
            c.execute("INSERT INTO release_monitors(id,project,definition,state,revision,active,created,updated) VALUES(?,?,?,'waiting',1,1,?,?)",
                (monitor_id,project,json.dumps(definition,separators=(',',':')),now,now))
        result=self.evaluate_monitor(project,monitor_id)
        return {'id':monitor_id,'state':result['state'],'revision':result['revision'],'created':now,
            'alert':result.get('alert'),'limits':['Regression episodes create one in-app alert; unchanged checks do not repeat it. Unknown comparisons do not alert.']}

    def monitor_query(self,project,definition):
        query=definition['query']
        saved=self.outcomes.get(project,query['outcome_goal']['id'],query['outcome_goal']['version'])
        goal=OutcomeGoal.model_validate(saved['definition'])
        now=datetime.now().astimezone();end=now-timedelta(seconds=goal.deadline_seconds)
        start=datetime.fromisoformat(query['after']['start'])
        if end<=start:return None
        start=max(start,end-timedelta(days=90))
        query={**query,'after':{'days':7,'start':start.isoformat(),'end':end.isoformat(),'as_of':now.isoformat()}}
        return ReleaseComparison.model_validate(query)

    @staticmethod
    def alert_evidence(report):
        def side(value):
            if not value:return None
            return {k:value.get(k) for k in ['counts','mature_denominator','satisfied_fraction','coverage']}
        return {'status':report['status'],'reasons':report['reasons'],'before':side(report.get('before')),
            'after':side(report.get('after')),'satisfied_fraction_difference':report.get('satisfied_fraction_difference'),
            'baseline_release':report.get('baseline_release'),'release':report.get('release'),
            'observational':True,'deployment_authorized':False}

    def evaluate_monitor(self,project,monitor_id):
        with closing(self.connect()) as c:
            row=c.execute('SELECT * FROM release_monitors WHERE id=? AND project=?',(monitor_id,project)).fetchone()
        if not row:raise HTTPException(404,'release monitor not found')
        if not row['active']:
            return {'id':monitor_id,'state':row['state'],'revision':row['revision'],'active':False}
        started=time.time()
        definition=json.loads(row['definition'])
        try:
            query=self.monitor_query(project,definition)
            report=self.compare(project,query) if query else {'status':'waiting','reasons':['after_window_not_mature']}
        except HTTPException:
            report={'status':'unknown','reasons':['pinned_release_evidence_unavailable']}
        threshold=definition['regression_threshold']
        comparable=report['status']=='comparable'
        regressed=comparable and report['satisfied_fraction_difference']<=-threshold
        alert=None
        if comparable:evidence=self.alert_evidence(report);fingerprint=digest(evidence)
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE')
            current=c.execute('SELECT state,episode,revision,active,updated FROM release_monitors WHERE id=? AND project=?',(monitor_id,project)).fetchone()
            if not current:raise HTTPException(404,'release monitor not found')
            if not current['active'] or current['updated']>started:
                row=current
            else:
                state=('regressed' if regressed else 'clear') if comparable else current['state'] if current['state']=='regressed' else 'unknown'
                kind='regression' if comparable and regressed and current['state']!='regressed' else 'recovered' if comparable and not regressed and current['state']=='regressed' else None
                episode=current['episode']+(1 if kind=='regression' else 0)
                if kind:
                    alert_id=str(uuid.uuid4())
                    c.execute('INSERT INTO release_monitor_alerts(id,monitor_id,project,episode,kind,evidence,fingerprint,created) VALUES(?,?,?,?,?,?,?,?)',
                        (alert_id,monitor_id,project,episode,kind,json.dumps(evidence,separators=(',',':')),fingerprint,started))
                    alert={'id':alert_id,'episode':episode,'kind':kind,'evidence':evidence,'created':started}
                changed=state!=current['state']
                c.execute('UPDATE release_monitors SET state=?,episode=?,revision=revision+?,updated=? WHERE id=? AND project=? AND active=1',
                    (state,episode,int(changed),started,monitor_id,project))
                row=c.execute('SELECT state,revision,episode,active FROM release_monitors WHERE id=? AND project=?',(monitor_id,project)).fetchone()
        return {'id':monitor_id,'state':row['state'],'revision':row['revision'],'episode':row['episode'],'active':bool(row['active']),
            'comparison_status':report['status'],'reasons':report['reasons'],'alert':alert}

    def evaluate_active(self):
        with closing(self.connect()) as c:
            rows=c.execute('SELECT project,id FROM release_monitors WHERE active=1 ORDER BY updated LIMIT 50').fetchall()
        for row in rows:
            try:self.evaluate_monitor(row['project'],row['id'])
            except Exception as exc:log.warning('release monitor evaluation failed (%s)',type(exc).__name__)

    def monitor_alerts(self,project,monitor_id,limit):
        with closing(self.connect()) as c:
            rows=c.execute('SELECT id,episode,kind,evidence,fingerprint,created FROM release_monitor_alerts WHERE project=? AND monitor_id=? ORDER BY created DESC LIMIT ?',
                (project,monitor_id,limit)).fetchall()
        return {'alerts':[{**dict(r),'evidence':json.loads(r['evidence'])} for r in rows]}

    def disable_monitor(self,project,monitor_id,revision):
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT active,revision,state FROM release_monitors WHERE id=? AND project=?',(monitor_id,project)).fetchone()
            if not row:raise HTTPException(404,'release monitor not found')
            if not row['active']:return {'id':monitor_id,'active':False,'revision':row['revision']}
            if revision!=row['revision']:raise HTTPException(409,'monitor changed since it was read; reload before stopping')
            c.execute('UPDATE release_monitors SET active=0,revision=revision+1,updated=? WHERE id=? AND project=? AND revision=?',
                (time.time(),monitor_id,project,revision))
        return {'id':monitor_id,'active':False,'revision':revision+1}

    def resolve(self,project,id,environment):
        with closing(self.connect()) as c:
            legacy=c.execute('SELECT 1 FROM loop_actions WHERE project=? AND id=?',(project,id)).fetchone()
            external=c.execute("SELECT kind FROM loop_repo_receipts WHERE project=? AND id=? AND kind IN ('external-release-proposal','external-release-receipt')",(project,id)).fetchone()
            if legacy and external:raise HTTPException(409,'ambiguous release evidence identity')
            if legacy:
                action=self.loop.action(c,project,id);release=released_artifact(action,environment)
                return public_release(action,release) | {'evidence_kind':'legacy_tracking_artifact','approval_basis':'existing_tracking_policy'} if release else None
        if external:
            if external['kind']!='external-release-receipt' or self.release_evidence is None:raise HTTPException(409,'object is not adopted release evidence')
            release=self.release_evidence.normalized(project,id)
            return release if release['environment']==environment else None
        raise HTTPException(404,'release evidence not found')

    def list(self,project,environment,limit,offset):
        with closing(self.connect()) as c:
            rows=c.execute("""SELECT * FROM (SELECT id, json_extract(receipt,'$.release.released_at') AS released_at FROM loop_actions
                WHERE project=? AND json_extract(receipt,'$.release_status')='released' AND json_extract(receipt,'$.release.environment')=?
                UNION ALL SELECT id,json_extract(receipt,'$.evidence.deployment.released_at') AS released_at FROM loop_repo_receipts
                WHERE project=? AND kind='external-release-receipt' AND json_extract(receipt,'$.evidence.deployment.environment')=?
                ) ORDER BY julianday(released_at) DESC,id LIMIT ? OFFSET ?""",(project,environment,project,environment,limit+1,offset)).fetchall()
        items=[]
        for row in rows[:limit]:
            release=self.resolve(project,row['id'],environment)
            if release:items.append(release)
        return {'releases':items,'has_more':len(rows)>limit,'offset':offset,'limit':limit,
                'limits':['Stored provider release evidence, not a fresh health check or deployment authority.',
                          'External evidence records owner-adopted provider identity, not independent code review or CI attestation.']}

    def compare(self,project,req):
        saved=self.outcomes.get(project,str(req.outcome_goal.id),req.outcome_goal.version)
        if saved['sha256']!=req.outcome_goal.sha256:raise HTTPException(409,'outcome goal bytes changed from the selected binding')
        goal=OutcomeGoal.model_validate(saved['definition'])
        baseline=self.regressions.run_report(project,str(req.baseline_run));candidate=self.regressions.run_report(project,str(req.candidate_run))
        experiment=compare_runs(baseline,candidate)
        binding=candidate['dataset']['definition'].get('outcome_goal')
        if binding!=req.outcome_goal.model_dump(mode='json'):raise HTTPException(409,'experiment dataset must pin this exact outcome goal version/hash')
        release=self.resolve(project,str(req.release_action),goal.environment);reasons=[]
        baseline_release=self.resolve(project,str(req.baseline_release_action),goal.environment)
        if not release:reasons.append('verified_release_receipt_missing')
        if not baseline_release:reasons.append('baseline_verified_release_receipt_missing')
        if not experiment['eligible_for_review']:reasons.append('experiment_does_not_meet_review_criteria')
        if goal.traffic_kind!='user':reasons.append('normal_user_population_not_selected')
        released=None
        if release:
            try:
                released=datetime.fromisoformat(release['released_at'])
                if released.tzinfo is None or released.timestamp()>time.time()+1:raise ValueError()
            except (ValueError,TypeError):reasons.append('release_timestamp_invalid');released=None
            if baseline['run']['definition']['release_sha']!=release['base_commit']:reasons.append('baseline_experiment_commit_unbound')
            if candidate['run']['definition']['release_sha']!=release['commit']:reasons.append('candidate_experiment_commit_unbound')
            if released:
                if max(baseline['run']['created'],candidate['run']['created'])>released.timestamp():reasons.append('experiment_did_not_precede_verified_deployment')
                if req.before.end+timedelta(seconds=goal.deadline_seconds)>released:reasons.append('baseline_followup_overlaps_release')
                if req.after.start<released:reasons.append('after_entries_precede_release')
            if baseline_release:
                if baseline_release['commit']!=release['base_commit']:reasons.append('baseline_provider_commit_unbound')
                try:
                    baseline_at=datetime.fromisoformat(baseline_release['released_at'])
                    if baseline_at.tzinfo is None:raise ValueError()
                    if req.before.start<baseline_at:reasons.append('baseline_entries_precede_verified_deployment')
                except (ValueError,TypeError):reasons.append('baseline_release_timestamp_invalid')
        if req.before.end+timedelta(seconds=goal.deadline_seconds)>req.after.start:reasons.append('outcome_windows_overlap')
        for label,window in [('before',req.before),('after',req.after)]:
            if window.end+timedelta(seconds=goal.deadline_seconds)>window.as_of:reasons.append(label+'_followup_window_not_mature')
        # A missing proof cannot supply a release filter; return unknown without inventing one.
        reports={}
        if release:
            for label,window,commit in [('before',req.before,release['base_commit']),('after',req.after,release['commit'])]:
                result=self.outcomes.evaluate(project,OutcomeQuery(goal_id=req.outcome_goal.id,version=req.outcome_goal.version,
                    start=window.start,end=window.end,as_of=window.as_of,release_sha=commit))
                if result['mature_denominator']<goal.min_sample_size:reasons.append(label+'_sample_below_goal_minimum')
                if result['counts']['waiting']:reasons.append(label+'_tasks_still_waiting')
                if result['counts']['unclassified'] or result['satisfied_fraction'] is None:reasons.append(label+'_evidence_incomplete')
                if window.start.timestamp()<time.time()-self.loop.retention_days*86400:reasons.append(label+'_retention_gap')
                reports[label]={k:result[k] for k in ['counts','mature_denominator','satisfied_fraction','coverage','model_cost','query','snapshot','tasks','total','has_more','trace_query']}
                health=self.collection.report(project,HealthQuery(start=window.start,end=window.as_of,as_of=window.as_of,environment=goal.environment,workflow=goal.workflow))
                reports[label]['collection_health']={k:health[k] for k in ['status','reasons','collection_completeness','query','fingerprint','limits']}
                reports[label]['collection_health'].update(backend=health['backend'],backend_scope=health['backend_scope'],sources=[{k:s[k] for k in ['source','status','reasons']} for s in health['sources']])
                if health['status']!='observed':reasons.append(label+'_collection_evidence_incomplete')
        reasons=sorted(set(reasons));comparable=not reasons
        result={'status':'comparable' if comparable else 'unknown','reasons':reasons,
            'outcome_goal':req.outcome_goal.model_dump(mode='json'),'goal_definition':saved['definition'],'query':req.model_dump(mode='json'),
            'release':release,
            'baseline_release':baseline_release,
            'experiment':{k:experiment[k] for k in ['baseline','candidate','dataset_sha256','changes','baseline_pass_rate','candidate_pass_rate','eligible_for_review']},
            **reports,'satisfied_fraction_difference':reports['after']['satisfied_fraction']-reports['before']['satisfied_fraction'] if comparable else None,
            'deployment_authorized':False,'observational':True,'collection_completeness':'not_established',
            'limits':['Experiment results check fixed reviewed facts, not all possible answers or independent source state.',
              'Comparability requires experiments before the provider-reported deployment; this does not prove when users first saw the commit.',
              'Only retained producer-labelled user task traces are measured, not all users or all tasks. No sample count grants production readiness.',
              'Before/after is observational and does not establish that the release caused a change.',
              'This read grants no merge or deployment permission. External receipts attest provider identity only, not code review or CI.']}
        result['fingerprint']=digest(result)
        return result
