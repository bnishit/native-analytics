"""One evidence view and bounded, observe-only follow-up over existing release checks."""
from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
import sqlite3
import time
import uuid
from typing import Literal
from fastapi import Depends, HTTPException, Request
from pydantic import Field, model_validator
from accounts import ROLES, digest as credential_digest
from outcomes import OutcomeBinding, OutcomeGoal
from product_analytics import Model
from regressions import CaseRef, compare_runs
from release_outcomes import FrozenWindow, ReleaseComparison
from trace_explorer import TraceOpen

FOLLOWUP_SCOPES={'audit:read','experiments:write','followups:write'}

class ImprovementQuery(Model):
    outcome_goal:OutcomeBinding|None=None
    source:TraceOpen|None=None
    dataset:CaseRef|None=None
    baseline_run:uuid.UUID|None=None
    candidate_run:uuid.UUID|None=None
    release_action:uuid.UUID|None=None

class FollowupDefinition(Model):
    outcome_goal:OutcomeBinding
    baseline_release_action:uuid.UUID
    release_action:uuid.UUID
    baseline_run:uuid.UUID
    candidate_run:uuid.UUID
    before:FrozenWindow
    window_seconds:int=Field(default=86400,ge=60,le=86400,strict=True)
    interval_seconds:int=Field(default=3600,ge=60,le=86400,strict=True)
    max_checks:int=Field(default=24,ge=1,le=24,strict=True)
    @model_validator(mode='after')
    def bounded(self):
        if self.interval_seconds>self.window_seconds:raise ValueError('check interval exceeds the follow-up window')
        return self

class FollowupControl(Model):
    operation:Literal['pause','resume','cancel']
    revision:int=Field(ge=1,strict=True)

def stamp(value):return datetime.fromtimestamp(value,timezone.utc)

class Improvements:
    @staticmethod
    def init_db(c):
        c.execute('''CREATE TABLE IF NOT EXISTS outcome_followups(project TEXT NOT NULL REFERENCES projects(id),
          id TEXT NOT NULL,definition TEXT NOT NULL,authority TEXT NOT NULL,created REAL NOT NULL,deadline REAL NOT NULL,
          next_due REAL NOT NULL,checks INTEGER NOT NULL DEFAULT 0,pending REAL,state TEXT NOT NULL,
          revision INTEGER NOT NULL DEFAULT 1,result TEXT,signal TEXT,notification TEXT,
          PRIMARY KEY(project,id))''')

    def __init__(self,app,connect,editor,outcomes,regressions,releases,explorer,accounts,oauth):
        self.connect=connect;self.outcomes=outcomes;self.regressions=regressions;self.releases=releases
        self.explorer=explorer;self.accounts=accounts;self.oauth=oauth
        @app.post('/api/improvements/status',dependencies=[Depends(editor)])
        def status(project:str,req:ImprovementQuery):return self.status(project,req)
        @app.put('/api/outcomes/followups/{id}',dependencies=[Depends(editor)])
        def save(project:str,id:uuid.UUID,req:FollowupDefinition,request:Request):
            return self.save(project,str(id),req,self.authority_for(request,project))
        @app.get('/api/outcomes/followups',dependencies=[Depends(editor)])
        def following(project:str):return self.list(project)
        @app.get('/api/outcomes/followups/{id}',dependencies=[Depends(editor)])
        def read(project:str,id:uuid.UUID):return self.read(project,str(id))
        @app.post('/api/outcomes/followups/{id}/control',dependencies=[Depends(editor)])
        def control(project:str,id:uuid.UUID,req:FollowupControl,request:Request):
            return self.control(project,str(id),req,self.authority_for(request,project))

    def goal(self,project,binding):
        saved=self.outcomes.get(project,str(binding.id),binding.version)
        if saved['sha256']!=binding.sha256:raise HTTPException(409,'outcome goal binding changed')
        return OutcomeGoal.model_validate(saved['definition'])

    def status(self,project,req):
        stages=[];reasons=[]
        def stage(name,state,action):
            stages.append({'stage':name,'state':state,'next_action':action})
        if req.outcome_goal is None:
            stage('goal','missing','select_outcome_goal')
        else:
            goal=self.goal(project,req.outcome_goal);stage('goal','pinned',None)
            if req.source:
                if any(getattr(req.source,k) is None for k in ['start','end','as_of']):raise HTTPException(422,'failure selection requires a frozen query')
                try:opened=self.explorer.search(project,req.source,req.source.reference)['trace']
                except HTTPException as exc:
                    if exc.status_code not in {404,410}:raise
                    return {'stages':stages+[{'stage':'failure','state':'blocked','next_action':'resolve_missing_failure_evidence'}],
                        'next_action':'resolve_missing_failure_evidence','reasons':['failure_not_retained_in_window'],
                        'original_request_replay':False,'reproduction_scope':'unavailable','deployment_authorized':False}
                if opened['environment']!=goal.environment or opened['contract']!=goal.contract:raise HTTPException(409,'selected failure does not match the goal scope')
                if opened['status'] not in {'failed','gaps'}:raise HTTPException(422,'select a failed or incomplete request')
            dataset=None
            if req.dataset:
                dataset=self.regressions.get(project,'dataset',str(req.dataset.id),req.dataset.version)
                if dataset['definition'].get('outcome_goal')!=req.outcome_goal.model_dump(mode='json'):raise HTTPException(409,'dataset must pin this exact goal')
                if req.source:
                    cases=[self.regressions.get(project,'case',str(r['id']),r['version']) for r in dataset['definition']['cases']]
                    if not any(c['definition'].get('source') and TraceOpen.model_validate(c['definition']['source'])==req.source for c in cases):
                        raise HTTPException(409,'dataset has no case linked to the selected frozen failure')
            stage('case','reviewed' if dataset else 'missing','review_regression_case' if not dataset else None)
            baseline=self.regressions.run_report(project,str(req.baseline_run)) if req.baseline_run else None
            candidate=self.regressions.run_report(project,str(req.candidate_run)) if req.candidate_run else None
            for run in [baseline,candidate]:
                if run and (not dataset or run['dataset_sha256']!=dataset['sha256'] or run['run']['definition']['dataset']!=str(req.dataset.id)):
                    raise HTTPException(409,'run does not use the selected immutable dataset')
            reproduced=bool(baseline and baseline['complete'] and baseline['counts']['failed']>0 and not baseline['counts']['error'])
            stage('baseline','reproduced_reviewed_case' if reproduced else 'blocked' if baseline else 'missing','run_failing_baseline' if not reproduced else None)
            comparison=compare_runs(baseline,candidate) if baseline and candidate else None
            repaired=bool(reproduced and comparison and comparison['eligible_for_review'] and not candidate['counts']['error'] and comparison['changes']['improved']>0)
            stage('candidate','eligible_for_independent_review' if repaired else 'blocked' if candidate else 'missing','repair_and_run_candidate' if not repaired else None)
            release=self.releases.resolve(project,str(req.release_action),goal.environment) if req.release_action else None
            bound=False
            if req.release_action and not release:reasons.append('verified_release_receipt_missing')
            if release and baseline and candidate:
                bound=baseline['run']['definition']['release_sha']==release['base_commit'] and candidate['run']['definition']['release_sha']==release['commit']
                if not bound:reasons.append('experiment_release_mismatch')
                if max(baseline['run']['created'],candidate['run']['created'])>datetime.fromisoformat(release['released_at']).timestamp():
                    bound=False;reasons.append('experiments_did_not_precede_deployment')
            stage('release','verified_identity' if repaired and bound else 'blocked' if req.release_action else 'missing','prepare_verified_release' if not (repaired and bound) else None)
            if repaired and bound:stage('followup','not_checked','inspect_existing_followups')
        next_action=next((s['next_action'] for s in stages if s['next_action']),None)
        return {'stages':stages,'next_action':next_action,'reasons':reasons,'query':req.model_dump(mode='json'),
            'original_request_replay':False,'reproduction_scope':'reviewed_case','deployment_authorized':False,
            'limits':['Fixture checks do not replay original model decisions or Slack delivery.',
                'Experiment outputs are application-side submissions checked against reviewed facts, not execution attestations.',
                'Review eligibility and verified release identity do not prove independent review, CI, current runtime health or user benefit.',
                'Use the coding host for code/tests and existing reviewed release authority. No code executes here.',
                'Follow-up jobs are not read by this status call. Discover existing jobs with separate followups:write before creating another window.']}

    def authority_for(self,request,project):
        raw=request.headers.get('authorization','').removeprefix('Bearer ')
        if raw.startswith('as_oauth_'):
            token=self.oauth.token(raw,'access')
            if not token or token.project!=project or not FOLLOWUP_SCOPES<=set(token.scopes):raise HTTPException(403,'follow-up requires separate followups:write, audit and experiment consent')
            return {'kind':'oauth','ref':token.grant_id}
        principal=self.accounts.project_role(request,project,'editor')
        if principal['key_id']:return {'kind':'workspace','ref':principal['key_id']}
        return {'kind':'session','ref':credential_digest(request.cookies.get('as_session',''))}

    def authority_valid(self,project,authority):
        now=time.time()
        with closing(self.connect()) as c:
            if authority['kind']=='oauth':
                grant=self.oauth.grant(c,authority['ref'])
                if not grant or grant['project']!=project or ROLES[grant['role']]<ROLES['editor'] or not FOLLOWUP_SCOPES<=set(json.loads(grant['scopes'])):return False
                tokens=c.execute('SELECT scopes FROM oauth_tokens WHERE grant_id=? AND used=0 AND expires>?',(authority['ref'],now)).fetchall()
                return any(FOLLOWUP_SCOPES<=set(json.loads(t['scopes'])) for t in tokens)
            base='JOIN memberships m ON m.user_id=a.user_id JOIN projects p ON p.workspace=m.workspace'
            if authority['kind']=='session':
                row=c.execute('SELECT m.role FROM account_sessions a '+base+' WHERE a.key_hash=? AND a.expires>? AND p.id=?',(authority['ref'],now,project)).fetchone()
                return bool(row and ROLES[row['role']]>=ROLES['editor'])
            if authority['kind']=='workspace':
                row=c.execute('SELECT m.role,a.role AS key_role FROM workspace_keys a '+base+' AND a.workspace=p.workspace WHERE a.id=? AND a.expires>? AND p.id=?',(authority['ref'],now,project)).fetchone()
                return bool(row and min(ROLES[row['role']],ROLES[row['key_role']])>=ROLES['editor'])
        return False

    def validate_followup(self,project,req):
        goal=self.goal(project,req.outcome_goal)
        if goal.traffic_kind!='user':raise HTTPException(422,'follow-up requires a normal-user goal')
        if req.before.end+timedelta(seconds=goal.deadline_seconds)>req.before.as_of:raise HTTPException(422,'baseline follow-up window is not mature')
        if req.window_seconds<=goal.deadline_seconds:raise HTTPException(422,'follow-up must include the goal maturity deadline')
        baseline=self.regressions.run_report(project,str(req.baseline_run));candidate=self.regressions.run_report(project,str(req.candidate_run))
        query=ImprovementQuery(outcome_goal=req.outcome_goal,dataset=CaseRef(id=candidate['run']['definition']['dataset'],version=candidate['run']['definition']['dataset_version']),
            baseline_run=req.baseline_run,candidate_run=req.candidate_run,release_action=req.release_action)
        status=self.status(project,query)
        if not any(s['stage']=='release' and s['state']=='verified_identity' for s in status['stages']):raise HTTPException(409,'reproduced baseline, improved candidate and predeployment release binding required')
        release=self.releases.resolve(project,str(req.release_action),goal.environment)
        old=self.releases.resolve(project,str(req.baseline_release_action),goal.environment)
        if not old or old['commit']!=release['base_commit']:raise HTTPException(409,'baseline provider release is not bound')
        if req.before.start<datetime.fromisoformat(old['released_at']) or req.before.end+timedelta(seconds=goal.deadline_seconds)>datetime.fromisoformat(release['released_at']):
            raise HTTPException(409,'baseline window must be mature before the candidate deployment')
        if datetime.fromisoformat(release['released_at']).timestamp()>time.time():raise HTTPException(409,'release is in the future')

    def save(self,project,id,req,authority):
        payload=req.model_dump_json();now=time.time()
        with closing(self.connect()) as c:
            old=c.execute('SELECT definition FROM outcome_followups WHERE project=? AND id=?',(project,id)).fetchone()
        if old:
            if old['definition']!=payload:raise HTTPException(409,'follow-up ID already has a different definition')
            return self.read(project,id)
        self.validate_followup(project,req)
        if not self.authority_valid(project,authority):raise HTTPException(403,'follow-up authority expired or was revoked')
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT COUNT(*) FROM outcome_followups WHERE project=?',(project,)).fetchone()[0]>=100:raise HTTPException(429,'follow-up history capacity reached')
            if c.execute("SELECT COUNT(*) FROM outcome_followups WHERE project=? AND state IN ('watching','checking','paused')",(project,)).fetchone()[0]>=3:
                raise HTTPException(429,'at most three active follow-ups per project')
            if c.execute("SELECT COUNT(*) FROM outcome_followups WHERE state IN ('watching','checking','paused')").fetchone()[0]>=20:
                raise HTTPException(429,'follow-up worker capacity reached')
            try:c.execute('INSERT INTO outcome_followups(project,id,definition,authority,created,deadline,next_due,state) VALUES(?,?,?,?,?,?,?,?)',
                (project,id,payload,json.dumps(authority),now,now+req.window_seconds,now+req.interval_seconds,'watching'))
            except sqlite3.IntegrityError:
                old=c.execute('SELECT definition FROM outcome_followups WHERE project=? AND id=?',(project,id)).fetchone()
                if not old or old['definition']!=payload:raise
        return self.read(project,id)

    def list(self,project):
        with closing(self.connect()) as c:
            rows=c.execute("SELECT id,created,deadline,next_due,checks,state,revision FROM outcome_followups WHERE project=? ORDER BY (state IN ('watching','checking','paused')) DESC,created DESC,id DESC LIMIT 21",(project,)).fetchall()
        return {'followups':[dict(row) for row in rows[:20]],'has_more':len(rows)>20,'limit':20,
                'next_action':'read_selected_followup',
                'limits':['At most twenty project follow-ups: active first, then most recent. A truncated list cannot establish that an older job is absent.',
                    'Metadata only; select an existing ID for its definition and result. Listing never creates or renews a window.']}

    def read(self,project,id):
        with closing(self.connect()) as c:r=c.execute('SELECT * FROM outcome_followups WHERE project=? AND id=?',(project,id)).fetchone()
        if not r:raise HTTPException(404,'follow-up not found in this project')
        return {k:r[k] for k in ['id','created','deadline','next_due','checks','state','revision']} | {
            'evidence_window_closed':time.time()>=r['deadline'],'worker_overdue':r['state'] in {'watching','checking'} and time.time()>r['next_due']+60,
            'definition':json.loads(r['definition']),'result':json.loads(r['result']) if r['result'] else None,
            'notification':json.loads(r['notification']) if r['notification'] else None,'next_action':'read_followup',
            'limits':['Observe-only: no code, model/provider call, Slack send, merge or deploy.',
                'At most 24 checks over a fixed 24-hour evidence window. Delayed recovery uses the original cutoff; pause/retry never renew either budget.',
                'Worker timing is best effort. Restart resumes a claimed frozen check; retention/erasure can still remove evidence.',
                'Notifications are stored status changes for clients to read, not external message delivery.']}

    def control(self,project,id,req,authority):
        if not self.authority_valid(project,authority):raise HTTPException(403,'follow-up authority expired or was revoked')
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE')
            r=c.execute('SELECT * FROM outcome_followups WHERE project=? AND id=?',(project,id)).fetchone()
            if not r:raise HTTPException(404,'follow-up not found')
            if req.revision!=r['revision']:raise HTTPException(409,'follow-up changed; read current revision')
            allowed={'pause':{'watching','checking'},'resume':{'paused'},'cancel':{'watching','checking','paused'}}
            if r['state'] not in allowed[req.operation]:raise HTTPException(409,'follow-up cannot perform that transition')
            if req.operation=='resume' and (time.time()>=r['deadline'] or r['checks']>=FollowupDefinition.model_validate_json(r['definition']).max_checks):
                raise HTTPException(410,'follow-up ended; resume cannot renew it')
            state={'pause':'paused','resume':'watching','cancel':'cancelled'}[req.operation]
            c.execute('UPDATE outcome_followups SET state=?,revision=revision+1 WHERE project=? AND id=?',(state,project,id))
        return self.read(project,id)

    def work(self):
        now=time.time()
        with closing(self.connect()) as c:
            rows=c.execute("SELECT * FROM outcome_followups WHERE state='checking' OR state='watching' AND (next_due<=? OR deadline<=?) OR state='paused' AND deadline<=? ORDER BY next_due LIMIT 2",(now,now,now)).fetchall()
        for row in rows:self.check(row,now)

    def check(self,row,now):
        project,id=row['project'],row['id'];req=FollowupDefinition.model_validate_json(row['definition'])
        if not self.authority_valid(project,json.loads(row['authority'])):
            with closing(self.connect()) as c,c:c.execute("UPDATE outcome_followups SET state='revoked',revision=revision+1,notification=? WHERE project=? AND id=? AND state IN ('watching','checking','paused')",(json.dumps({'kind':'authorization_revoked'}),project,id))
            return
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE')
            fresh=c.execute('SELECT * FROM outcome_followups WHERE project=? AND id=?',(project,id)).fetchone()
            if not fresh or fresh['state'] not in {'watching','checking','paused'}:return
            if fresh['state']=='paused' and now<fresh['deadline']:return
            if fresh['state']=='paused' or fresh['checks']>=req.max_checks and fresh['pending'] is None:
                c.execute("UPDATE outcome_followups SET state='expired',revision=revision+1,notification=? WHERE project=? AND id=?",(json.dumps({'kind':'followup_ended'}),project,id));return
            as_of=fresh['pending'] if fresh['pending'] is not None else min(now,fresh['deadline'])
            if fresh['pending'] is not None and fresh['state']!='checking':
                c.execute("UPDATE outcome_followups SET state='checking',revision=revision+1 WHERE project=? AND id=?",(project,id))
            if fresh['pending'] is None:
                if now<fresh['next_due'] and now<fresh['deadline']:return
                c.execute("UPDATE outcome_followups SET state='checking',pending=?,checks=checks+1,revision=revision+1 WHERE project=? AND id=?",(as_of,project,id))
        result={'status':'unknown','reasons':['followup_window_not_mature']}
        try:
            goal=self.goal(project,req.outcome_goal)
            end=as_of-goal.deadline_seconds
            if end>row['created']:
                report=self.releases.compare(project,ReleaseComparison(**req.model_dump(exclude={'window_seconds','interval_seconds','max_checks'}),
                    after=FrozenWindow(start=stamp(row['created']),end=stamp(end),as_of=stamp(as_of))))
                result={k:report[k] for k in ['status','reasons','satisfied_fraction_difference','observational','deployment_authorized']}
                result['windows']={label:{k:report[label][k] for k in ['counts','mature_denominator','satisfied_fraction','query']} for label in ['before','after'] if label in report}
        except (HTTPException,ValueError,sqlite3.Error):result={'status':'unknown','reasons':['retained_evidence_unavailable']}
        if not self.authority_valid(project,json.loads(row['authority'])):return
        signature=json.dumps([result['status'],result['reasons'],result.get('satisfied_fraction_difference')],sort_keys=True)
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE')
            fresh=c.execute('SELECT * FROM outcome_followups WHERE project=? AND id=?',(project,id)).fetchone()
            if not fresh or fresh['state']!='checking' or fresh['pending']!=as_of:return
            ended=as_of>=fresh['deadline'] or fresh['checks']>=req.max_checks
            result['observed_through']=stamp(as_of).isoformat()
            result['end_reason']=('window_ended' if as_of>=fresh['deadline'] else 'check_budget_exhausted') if ended else None
            state='completed' if ended else 'watching'
            notification={'kind':'comparison_ready' if result['status']=='comparable' else 'followup_inconclusive' if ended else 'evidence_attention'} if signature!=fresh['signal'] or ended else None
            if notification:notification.update(at=stamp(as_of).isoformat(),sequence=fresh['checks'])
            c.execute('UPDATE outcome_followups SET state=?,pending=NULL,next_due=?,result=?,signal=?,notification=?,revision=revision+1 WHERE project=? AND id=?',
                (state,min(as_of+req.interval_seconds,fresh['deadline']),json.dumps(result),signature,json.dumps(notification) if notification else fresh['notification'],project,id))
