"""Connect fixed experiments to existing verified release receipts and mature outcomes."""
from contextlib import closing
from datetime import datetime,timedelta
import time
import uuid
from fastapi import Depends,HTTPException,Query
from pydantic import model_validator
from collection_health import HealthQuery
from outcomes import OutcomeBinding,OutcomeQuery,OutcomeGoal
from product_analytics import Model,Window
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

def public_release(action,release):
    return {'action':action['id'],'repository':action['plan'].get('repository'),
            **{k:release.get(k) for k in ['provider','commit','base_commit','environment','released_at','deployment_id','timestamp_basis']}}

class ReleaseOutcomes:
    def __init__(self,app,connect,editor,outcomes,regressions,loop,collection):
        self.connect=connect;self.outcomes=outcomes;self.regressions=regressions;self.loop=loop;self.collection=collection
        @app.get('/api/outcomes/releases',dependencies=[Depends(editor)])
        def releases(project:str,environment:Environment,limit:int=Query(20,ge=1,le=100),offset:int=Query(0,ge=0,le=10000)):
            return self.list(project,environment,limit,offset)
        @app.post('/api/outcomes/releases/compare',dependencies=[Depends(editor)])
        def compare(project:str,req:ReleaseComparison):return self.compare(project,req)

    def list(self,project,environment,limit,offset):
        with closing(self.connect()) as c:
            rows=c.execute('''SELECT id FROM loop_actions WHERE project=? AND json_extract(receipt,'$.release_status')='released'
              AND json_extract(receipt,'$.release.environment')=? ORDER BY julianday(json_extract(receipt,'$.release.released_at')) DESC,id LIMIT ? OFFSET ?''',(project,environment,limit+1,offset)).fetchall()
            items=[]
            for row in rows[:limit]:
                action=self.loop.action(c,project,row['id']);release=released_artifact(action,environment)
                if release:items.append(public_release(action,release))
        return {'releases':items,'has_more':len(rows)>limit,'offset':offset,'limit':limit,
                'limits':['Stored provider-verified release receipts, not a fresh claim about the currently running deployment.','This list grants no provider, merge or deployment authority.']}

    def compare(self,project,req):
        saved=self.outcomes.get(project,str(req.outcome_goal.id),req.outcome_goal.version)
        if saved['sha256']!=req.outcome_goal.sha256:raise HTTPException(409,'outcome goal bytes changed from the selected binding')
        goal=OutcomeGoal.model_validate(saved['definition'])
        baseline=self.regressions.run_report(project,str(req.baseline_run));candidate=self.regressions.run_report(project,str(req.candidate_run))
        experiment=compare_runs(baseline,candidate)
        binding=candidate['dataset']['definition'].get('outcome_goal')
        if binding!=req.outcome_goal.model_dump(mode='json'):raise HTTPException(409,'experiment dataset must pin this exact outcome goal version/hash')
        with closing(self.connect()) as c:
            action=self.loop.action(c,project,str(req.release_action))
            baseline_action=self.loop.action(c,project,str(req.baseline_release_action))
        release=released_artifact(action,goal.environment);reasons=[]
        baseline_release=released_artifact(baseline_action,goal.environment)
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
            'outcome_goal':req.outcome_goal.model_dump(mode='json'),'query':req.model_dump(mode='json'),
            'release':public_release(action,release) if release else None,
            'baseline_release':public_release(baseline_action,baseline_release) if baseline_release else None,
            'experiment':{k:experiment[k] for k in ['baseline','candidate','dataset_sha256','changes','baseline_pass_rate','candidate_pass_rate','eligible_for_review']},
            **reports,'satisfied_fraction_difference':reports['after']['satisfied_fraction']-reports['before']['satisfied_fraction'] if comparable else None,
            'deployment_authorized':False,'observational':True,'collection_completeness':'not_established',
            'limits':['Experiment results check fixed reviewed facts, not all possible answers or independent source state.',
              'The experiment precedes this provider-reported verified deployment; this does not prove when users first saw the commit.',
              'Only retained producer-labelled user task traces are measured, not all users or all tasks. No sample count grants production readiness.',
              'Before/after is observational and does not establish that the release caused a change.',
              'This read reuses existing reviewed release evidence and grants no merge or deployment permission.']}
        result['fingerprint']=digest(result)
        return result
