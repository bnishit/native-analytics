"""Default-off, owner-scoped, redacted and expiring debug snapshots."""
from contextlib import closing
from datetime import datetime
import json
import os
import time
import uuid
from typing import Any,Literal
from cryptography.exceptions import InvalidTag
from fastapi import Depends,HTTPException,Request
from pydantic import Field,StrictBool,UUID4,field_validator,model_validator
from audit import Observation
from catalog_replay import CatalogReplay
from private_storage import storage_cipher
from regressions import Reviewed
from sdk.python.afterspan_experiments import redact_debug,canonical,digest
from trace_explorer import TraceOpen
from tracing import Environment,workflow_reference

class DebugPolicy(Reviewed):
    enabled:StrictBool
    contract:Literal['workflow-trace-v1','slack-request-v1']='workflow-trace-v1'
    workflow:str=Field(pattern=r'^[a-z][a-z0-9_.-]{0,63}$')
    environment:Environment
    window_seconds:int=Field(default=3600,ge=60,le=86400,strict=True)
    retention_seconds:int=Field(default=86400,ge=60,le=7*86400,strict=True)
    max_snapshots:int=Field(default=20,ge=1,le=100,strict=True)
    @model_validator(mode='after')
    def scope(self):
        if self.contract=='slack-request-v1' and self.workflow!='slack_read_request':raise ValueError('Slack capture supports only the catalog check workflow')
        return self

class DebugSnapshot(Reviewed):
    id:UUID4
    policy_revision:int=Field(ge=1,le=1000,strict=True)
    trace_id:UUID4
    attempt_id:UUID4
    span_id:UUID4
    workflow:str=Field(pattern=r'^[a-z][a-z0-9_.-]{0,63}$')
    environment:Environment
    contract:Literal['workflow-trace-v1','slack-request-v1']='workflow-trace-v1'
    kind:Literal['input','output','context','replay']
    content:CatalogReplay|dict[str,Any]
    @model_validator(mode='before')
    @classmethod
    def replay(cls,value):
        if isinstance(value,dict) and (value.get('kind')=='replay' or value.get('contract')=='slack-request-v1'):
            if value.get('contract')!='slack-request-v1' or value.get('kind')!='replay' or value.get('workflow')!='slack_read_request':raise ValueError('Slack capture accepts only typed catalog check fixtures')
            CatalogReplay.model_validate(value.get('content'))
        return value
    @field_validator('content')
    @classmethod
    def safe(cls,value):return redact_debug(value.model_dump(mode='json') if isinstance(value,CatalogReplay) else value)

class DebugReview(TraceOpen):snapshot_id:uuid.UUID|None=None

def aad(project,row):
    return canonical([project,row['id'],row['policy_revision'],row['trace_id'],row['attempt_id'],row['span_id'],row['workflow'],row['environment'],row['kind']]).encode()

class Debugging:
    @staticmethod
    def init_db(c):
        c.execute('''CREATE TABLE IF NOT EXISTS debug_policies(project TEXT NOT NULL REFERENCES projects(id),
          revision INTEGER NOT NULL,definition TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,captured_count INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(project,revision))''')
        c.execute('''CREATE TABLE IF NOT EXISTS debug_snapshots(project TEXT NOT NULL,id TEXT NOT NULL,
          policy_revision INTEGER NOT NULL,event_id TEXT NOT NULL,trace_id TEXT NOT NULL,attempt_id TEXT NOT NULL,
          span_id TEXT NOT NULL,workflow TEXT NOT NULL,environment TEXT NOT NULL,kind TEXT NOT NULL,
          payload BLOB NOT NULL,sha256 TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,
          PRIMARY KEY(project,id),FOREIGN KEY(project,event_id) REFERENCES events(project,id) ON DELETE CASCADE)''')
        c.execute('''CREATE INDEX IF NOT EXISTS trace_binding ON events(project,
          json_extract(properties,'$.afterspan_trace.trace_id'),json_extract(properties,'$.afterspan_trace.attempt_id'),
          json_extract(properties,'$.afterspan_trace.span_id')) WHERE name='trace.observed' ''')

    def __init__(self,app,connect,owner,editor,writer,explorer,audit,retention_days):
        self.connect=connect;self.explorer=explorer;self.audit=audit;self.retention_days=retention_days
        @app.get('/api/debug/policy',dependencies=[Depends(editor)])
        def policy(project:str):return self.policy(project)
        @app.put('/api/debug/policy',dependencies=[Depends(owner)])
        def save(project:str,revision:int,req:DebugPolicy,request:Request):return self.save(project,revision,req,request)
        @app.post('/v1/debug')
        def capture(req:DebugSnapshot,project=Depends(writer)):return self.capture(project,req)
        @app.post('/api/debug/review',dependencies=[Depends(editor)])
        def review(project:str,req:DebugReview,request:Request):return self.review(project,req,request)

    def policy(self,project):
        with closing(self.connect()) as c:
            row=c.execute('SELECT * FROM debug_policies WHERE project=? ORDER BY revision DESC LIMIT 1',(project,)).fetchone()
            count=c.execute('SELECT COUNT(*) FROM debug_snapshots WHERE project=? AND expires>?',(project,time.time())).fetchone()[0]
        return {'revision':row['revision'] if row else 0,'definition':json.loads(row['definition']) if row else None,
                'expires':row['expires'] if row else None,'active':bool(row and json.loads(row['definition'])['enabled'] and row['expires']>time.time()),
                'stored_snapshots':count,'captured_count':row['captured_count'] if row else 0,'limits':['Content is off by default. Only explicitly reviewed selected snapshots are supported.',
                  'Common-pattern redaction cannot recognize every private fact. Review before capture.',
                  'Changing policy purges prior snapshots. Capture-window expiry stops new captures; retained snapshots expire separately.']}

    def save(self,project,revision,req,request):
        if revision<0:raise HTTPException(422,'revision must be nonnegative')
        payload=req.model_dump_json();now=time.time()
        if req.enabled:storage_cipher()
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute('SELECT 1 FROM projects WHERE id=?',(project,)).fetchone():raise HTTPException(404,'project not found')
            row=c.execute('SELECT * FROM debug_policies WHERE project=? ORDER BY revision DESC LIMIT 1',(project,)).fetchone()
            current=row['revision'] if row else 0
            if row and current==revision+1 and DebugPolicy.model_validate(json.loads(row['definition']))==req:return self.policy(project)
            if revision!=current:raise HTTPException(409,'debug policy changed; read its current revision')
            if current>=1000:raise HTTPException(422,'debug policy revision capacity reached')
            c.execute('INSERT INTO debug_policies VALUES(?,?,?,?,?,0)',(project,current+1,payload,now,now+req.window_seconds if req.enabled else now))
            c.execute('DELETE FROM debug_snapshots WHERE project=?',(project,))
            self.audit(c,request,project,'project.debug_enabled' if req.enabled else 'project.debug_disabled')
        return self.policy(project)

    def capture(self,project,req):
        body=req.model_dump(mode='json');body.pop('contract');content=body.pop('content');fingerprint=digest([body,content]);now=time.time()
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE')
            policy=c.execute('SELECT * FROM debug_policies WHERE project=? ORDER BY revision DESC LIMIT 1',(project,)).fetchone()
            if not policy or not json.loads(policy['definition'])['enabled'] or policy['expires']<=now:raise HTTPException(403,'debug capture is off or its capture window expired')
            definition=json.loads(policy['definition'])
            if req.policy_revision!=policy['revision']:raise HTTPException(409,'debug policy revision changed')
            if req.contract!=definition.get('contract','workflow-trace-v1'):raise HTTPException(403,'snapshot contract is outside the approved capture scope')
            if req.workflow!=definition['workflow'] or req.environment!=definition['environment']:raise HTTPException(403,'snapshot is outside the approved workflow/environment')
            if req.kind=='replay' and any(value and value['environment']!=req.environment for value in [content['request'],content['candidate']]):raise HTTPException(409,'replay environment does not match its approved source scope')
            old=c.execute('SELECT sha256,expires FROM debug_snapshots WHERE project=? AND id=?',(project,str(req.id))).fetchone()
            if old:
                if old['expires']<=now:raise HTTPException(410,'snapshot expired; retry cannot renew retention')
                if old['sha256']!=fingerprint:raise HTTPException(409,'snapshot ID already contains different reviewed content')
                return {'id':str(req.id),'accepted':False,'duplicate':True,'expires':old['expires']}
            if policy['captured_count']>=definition['max_snapshots']:raise HTTPException(429,'debug snapshot budget reached; owner review is required for another policy')
            if req.contract=='slack-request-v1':
                source=c.execute('''SELECT id,ts,properties FROM events WHERE project=?
                  AND json_extract(properties,'$.afterspan_audit.schema_version')='slack-request-v1'
                  AND json_extract(properties,'$.afterspan_audit.request_ref')=?
                  AND json_extract(properties,'$.afterspan_audit.attempt_ref')=?
                  AND json_extract(properties,'$.afterspan_audit.span_ref')=?
                  AND json_extract(properties,'$.afterspan_audit.stage')='trace'
                  AND json_extract(properties,'$.afterspan_audit.trace_step')='quality'
                  AND json_extract(properties,'$.afterspan_audit.span_state') IN ('succeeded','failed')
                  AND json_extract(properties,'$.afterspan_audit.environment')=?
                  AND json_extract(properties,'$.afterspan_audit.request_kind')='read' ORDER BY received,id LIMIT 1''',
                  (project,str(req.trace_id),str(req.attempt_id),str(req.span_id),req.environment)).fetchone()
                if source:
                    facts=Observation.model_validate(json.loads(source['properties'])['afterspan_audit'])
                    if content['release_sha'] is not None and content['release_sha']!=facts.release_sha:raise HTTPException(409,'replay release does not match its retained source span')
            else:source=c.execute('''SELECT id,ts FROM events WHERE project=? AND name='trace.observed'
              AND json_extract(properties,'$.afterspan_trace.trace_id')=?
              AND json_extract(properties,'$.afterspan_trace.attempt_id')=?
              AND json_extract(properties,'$.afterspan_trace.span_id')=?
              AND json_extract(properties,'$.afterspan_trace.workflow')=?
              AND json_extract(properties,'$.afterspan_trace.environment')=? ORDER BY received,id LIMIT 1''',
              (project,str(req.trace_id),str(req.attempt_id),str(req.span_id),req.workflow,req.environment)).fetchone()
            if not source:raise HTTPException(409,'source span is not retained; flush metadata before submitting this snapshot')
            expires=min(now+definition['retention_seconds'],source['ts']+self.retention_days*86400)
            if expires<=now:raise HTTPException(410,'source span is outside retention')
            nonce=os.urandom(12);encrypted=nonce+storage_cipher().encrypt(nonce,canonical(content).encode(),aad(project,body))
            c.execute('INSERT INTO debug_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
              (project,str(req.id),req.policy_revision,source['id'],str(req.trace_id),str(req.attempt_id),str(req.span_id),req.workflow,req.environment,req.kind,encrypted,fingerprint,now,expires))
            c.execute('UPDATE debug_policies SET captured_count=captured_count+1 WHERE project=? AND revision=?',(project,req.policy_revision))
        return {'id':str(req.id),'accepted':True,'duplicate':False,'expires':expires}

    def review(self,project,req,request):
        query=req.model_dump(exclude={'snapshot_id'})
        opened=self.explorer.search(project,TraceOpen(**query),req.reference);q=opened['query'];now=time.time()
        with closing(self.connect()) as c,c:
            policy=c.execute('SELECT * FROM debug_policies WHERE project=? ORDER BY revision DESC LIMIT 1',(project,)).fetchone()
            if not policy or not json.loads(policy['definition'])['enabled']:raise HTTPException(403,'debug snapshots are off or revoked')
            key=c.execute("SELECT value FROM billing_settings WHERE key='receipt_key'").fetchone()[0]
            rows=c.execute('''SELECT d.*,e.ts FROM debug_snapshots d JOIN events e ON e.project=d.project AND e.id=d.event_id
              WHERE d.project=? AND d.policy_revision=? AND d.created<=? AND e.received<=? AND e.ts>=? AND e.ts<?''',
              (project,policy['revision'],datetime.fromisoformat(q['as_of'].replace('Z','+00:00')).timestamp(),
               datetime.fromisoformat(q['as_of'].replace('Z','+00:00')).timestamp(),
               datetime.fromisoformat(q['start'].replace('Z','+00:00')).timestamp(),
               datetime.fromisoformat(q['end'].replace('Z','+00:00')).timestamp())).fetchall()
            reference=lambda r:self.explorer.audit.ref(key,project,'request',r['trace_id']) if r['kind']=='replay' else workflow_reference(project,r['environment'],r['trace_id'],key)
            rows=[r for r in rows if r['environment']==req.environment and reference(r)==req.reference]
            if req.snapshot_id:
                row=next((r for r in rows if r['id']==str(req.snapshot_id)),None)
                if row is None:raise HTTPException(404,'snapshot not found in this frozen trace scope')
                if row['expires']<=now:raise HTTPException(410,'snapshot expired')
                content=self.decode(project,row)
                self.audit(c,request,project,'project.debug_snapshot_read')
                return {'id':row['id'],'kind':row['kind'],'created':row['created'],'expires':row['expires'],'content':content,'query':q,
                        **({'replay':CatalogReplay.model_validate(content).availability()} if row['kind']=='replay' else {}),
                        'limits':['Selected producer content, not correctness evidence. Redaction may miss private facts.','No raw trace/attempt/span IDs are returned.']}
        snapshots=[]
        for row in rows:
            if row['expires']<=now:continue
            item={k:row[k] for k in ['id','kind','created','expires']}
            if row['kind']=='replay':item['replay']=CatalogReplay.model_validate(self.decode(project,row)).availability()
            snapshots.append(item)
        return {'snapshots':snapshots,**({'replay':{'scope':'catalog_check','status':'blocked','reasons':['not_retained_in_window'],'full_request_replay':False,'replayed':False}} if req.reference.startswith('req_') and not snapshots else {}),
                'query':q,'limits':['Only retained snapshots bound to this exact trace/window are listed. Content needs a separate explicit read.',
                  'Absent fixtures may be uncaptured, expired, purged or beyond this cutoff. They cannot be reconstructed.']}

    def decode(self,project,row):
        try:return json.loads(storage_cipher().decrypt(row['payload'][:12],row['payload'][12:],aad(project,row)))
        except (InvalidTag,ValueError,TypeError):raise HTTPException(503,'snapshot cannot be decrypted; check private storage') from None
