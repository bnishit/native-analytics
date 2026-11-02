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
from pydantic import Field,StrictBool,UUID4,field_validator
from private_storage import storage_cipher
from regressions import Reviewed
from sdk.python.afterspan_experiments import redact_debug,canonical,digest
from trace_explorer import TraceOpen
from tracing import Environment,workflow_reference

class DebugPolicy(Reviewed):
    enabled:StrictBool
    workflow:str=Field(pattern=r'^[a-z][a-z0-9_.-]{0,63}$')
    environment:Environment
    window_seconds:int=Field(default=3600,ge=60,le=86400,strict=True)
    retention_seconds:int=Field(default=86400,ge=60,le=7*86400,strict=True)
    max_snapshots:int=Field(default=20,ge=1,le=100,strict=True)

class DebugSnapshot(Reviewed):
    id:UUID4
    policy_revision:int=Field(ge=1,le=1000,strict=True)
    trace_id:UUID4
    attempt_id:UUID4
    span_id:UUID4
    workflow:str=Field(pattern=r'^[a-z][a-z0-9_.-]{0,63}$')
    environment:Environment
    kind:Literal['input','output','context']
    content:dict[str,Any]
    @field_validator('content')
    @classmethod
    def safe(cls,value):return redact_debug(value)

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
            if row and current==revision+1 and row['definition']==payload:return self.policy(project)
            if revision!=current:raise HTTPException(409,'debug policy changed; read its current revision')
            if current>=1000:raise HTTPException(422,'debug policy revision capacity reached')
            c.execute('INSERT INTO debug_policies VALUES(?,?,?,?,?,0)',(project,current+1,payload,now,now+req.window_seconds if req.enabled else now))
            c.execute('DELETE FROM debug_snapshots WHERE project=?',(project,))
            self.audit(c,request,project,'project.debug_enabled' if req.enabled else 'project.debug_disabled')
        return self.policy(project)

    def capture(self,project,req):
        body=req.model_dump(mode='json');content=body.pop('content');fingerprint=digest([body,content]);now=time.time()
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE')
            policy=c.execute('SELECT * FROM debug_policies WHERE project=? ORDER BY revision DESC LIMIT 1',(project,)).fetchone()
            if not policy or not json.loads(policy['definition'])['enabled'] or policy['expires']<=now:raise HTTPException(403,'debug capture is off or its capture window expired')
            definition=json.loads(policy['definition'])
            if req.policy_revision!=policy['revision']:raise HTTPException(409,'debug policy revision changed')
            if req.workflow!=definition['workflow'] or req.environment!=definition['environment']:raise HTTPException(403,'snapshot is outside the approved workflow/environment')
            old=c.execute('SELECT sha256,expires FROM debug_snapshots WHERE project=? AND id=?',(project,str(req.id))).fetchone()
            if old:
                if old['expires']<=now:raise HTTPException(410,'snapshot expired; retry cannot renew retention')
                if old['sha256']!=fingerprint:raise HTTPException(409,'snapshot ID already contains different reviewed content')
                return {'id':str(req.id),'accepted':False,'duplicate':True,'expires':old['expires']}
            if policy['captured_count']>=definition['max_snapshots']:raise HTTPException(429,'debug snapshot budget reached; owner review is required for another policy')
            source=c.execute('''SELECT id,ts FROM events WHERE project=? AND name='trace.observed'
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
        if not req.reference.startswith('trace_'):raise HTTPException(422,'debug snapshots currently bind to workflow spans')
        query=req.model_dump(exclude={'snapshot_id'})
        opened=self.explorer.search(project,TraceOpen(**query),req.reference);q=opened['query'];now=time.time()
        with closing(self.connect()) as c,c:
            policy=c.execute('SELECT * FROM debug_policies WHERE project=? ORDER BY revision DESC LIMIT 1',(project,)).fetchone()
            if not policy or not json.loads(policy['definition'])['enabled']:raise HTTPException(403,'debug snapshots are off or revoked')
            key=c.execute("SELECT value FROM billing_settings WHERE key='receipt_key'").fetchone()[0]
            rows=c.execute('''SELECT d.*,e.ts FROM debug_snapshots d JOIN events e ON e.project=d.project AND e.id=d.event_id
              WHERE d.project=? AND d.policy_revision=? AND d.created<=? AND e.ts>=? AND e.ts<?''',
              (project,policy['revision'],datetime.fromisoformat(q['as_of'].replace('Z','+00:00')).timestamp(),
               datetime.fromisoformat(q['start'].replace('Z','+00:00')).timestamp(),
               datetime.fromisoformat(q['end'].replace('Z','+00:00')).timestamp())).fetchall()
            rows=[r for r in rows if r['environment']==req.environment and workflow_reference(project,r['environment'],r['trace_id'],key)==req.reference]
            if req.snapshot_id:
                row=next((r for r in rows if r['id']==str(req.snapshot_id)),None)
                if row is None:raise HTTPException(404,'snapshot not found in this frozen trace scope')
                if row['expires']<=now:raise HTTPException(410,'snapshot expired')
                try:content=json.loads(storage_cipher().decrypt(row['payload'][:12],row['payload'][12:],aad(project,row)))
                except (InvalidTag,ValueError,TypeError):raise HTTPException(503,'snapshot cannot be decrypted; check private storage') from None
                self.audit(c,request,project,'project.debug_snapshot_read')
                return {'id':row['id'],'kind':row['kind'],'created':row['created'],'expires':row['expires'],'content':content,'query':q,
                        'limits':['Selected producer content, not correctness evidence. Redaction may miss private facts.','No raw trace/attempt/span IDs are returned.']}
        return {'snapshots':[{k:r[k] for k in ['id','kind','created','expires']} for r in rows if r['expires']>now],
                'query':q,'limits':['Only retained snapshots bound to this exact trace/window are listed. Content needs a separate explicit read.']}
