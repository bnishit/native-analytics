"""Small, project-isolated analytics. One process, one persistent SQLite volume."""
import base64
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from accounts import Accounts
from billing import Billing
from audit import Audit,Observation
from retention import Retention
from product_loop import ProductLoop
from catalog import Catalog
from product_analytics import Window,PropertyFilter,ProductAnalytics

from journeys import JourneyContext,project_journeys,summarize

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

BASE = Path(__file__).parent
DB_PATH = Path(os.getenv('ANALYTICS_DB', '/data/analytics.db'))
RETENTION_DAYS = max(1, min(3650, int(os.getenv('RETENTION_DAYS', '90'))))
SENSITIVE = re.compile(r'password|passwd|secret|token|authorization|cookie|otp|api.?key|request.?body|response.?body', re.I)
IDENTIFIER = r'^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,159}$'
PROPERTY = r'^[A-Za-z_][A-Za-z0-9_]{0,63}$'
app = FastAPI(title='Afterspan', version='0.1.0', docs_url=None, redoc_url=None)
_limits = {}
_limit_lock = threading.Lock()
_prune_stop = threading.Event()


@app.exception_handler(RequestValidationError)
@app.exception_handler(ValidationError)
async def invalid_input(request,exc):
    return JSONResponse({'detail':'invalid input','fields':[list(e['loc']) for e in exc.errors()]},422)


def connect():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA busy_timeout=10000')
    c.execute('PRAGMA foreign_keys=ON')
    return c


accounts=Accounts(connect)
billing=Billing(connect,accounts)
accounts.billing=billing


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as c, c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,key_hash TEXT UNIQUE NOT NULL,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS events(project TEXT NOT NULL REFERENCES projects(id),id TEXT NOT NULL,name TEXT NOT NULL,
          distinct_id TEXT,ts REAL NOT NULL,received REAL NOT NULL,properties TEXT NOT NULL,PRIMARY KEY(project,id));
        CREATE INDEX IF NOT EXISTS event_time ON events(project,ts);
        CREATE INDEX IF NOT EXISTS event_name ON events(project,name,ts);
        CREATE INDEX IF NOT EXISTS event_person ON events(project,distinct_id,ts);
        CREATE TABLE IF NOT EXISTS journey_events(project TEXT NOT NULL,event_id TEXT NOT NULL,journey_id TEXT NOT NULL,
          workflow TEXT NOT NULL,environment TEXT NOT NULL,subject_kind TEXT NOT NULL,attempt_id TEXT NOT NULL,
          attempt_number INTEGER NOT NULL,state TEXT NOT NULL,release TEXT,PRIMARY KEY(project,event_id),
          FOREIGN KEY(project,event_id) REFERENCES events(project,id) ON DELETE CASCADE);
        CREATE INDEX IF NOT EXISTS journey_lookup ON journey_events(project,environment,workflow,journey_id);
        CREATE TABLE IF NOT EXISTS erased_journeys(project TEXT NOT NULL,environment TEXT NOT NULL,workflow TEXT NOT NULL,
          journey_id TEXT NOT NULL,erased REAL NOT NULL,PRIMARY KEY(project,environment,workflow,journey_id),
          FOREIGN KEY(project) REFERENCES projects(id));
        CREATE TABLE IF NOT EXISTS setup_checks(id TEXT PRIMARY KEY,project TEXT NOT NULL REFERENCES projects(id),
          environment TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS setup_project ON setup_checks(project,environment,created);
        CREATE TABLE IF NOT EXISTS people(project TEXT NOT NULL REFERENCES projects(id),distinct_id TEXT NOT NULL,
          attributes TEXT NOT NULL,first_seen REAL NOT NULL,last_seen REAL NOT NULL,PRIMARY KEY(project,distinct_id));
        CREATE TABLE IF NOT EXISTS charts(id TEXT PRIMARY KEY,project TEXT NOT NULL REFERENCES projects(id),title TEXT NOT NULL,
          definition TEXT NOT NULL,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS read_keys(id TEXT PRIMARY KEY,project TEXT NOT NULL REFERENCES projects(id),
          label TEXT NOT NULL,key_hash TEXT UNIQUE NOT NULL,created REAL NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS login_limits(ip TEXT PRIMARY KEY,window REAL NOT NULL,attempts INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS erased_identities(project TEXT NOT NULL REFERENCES projects(id),distinct_id TEXT NOT NULL,
          erased REAL NOT NULL,PRIMARY KEY(project,distinct_id));
        CREATE TABLE IF NOT EXISTS relay_projects(project TEXT PRIMARY KEY REFERENCES projects(id),events TEXT NOT NULL,enabled INTEGER NOT NULL);
        ''')
        accounts.init_db(c)
        billing.init_db(c)
        ProductAnalytics.init_db(c)
        Catalog.init_db(c)
        Audit.init_db(c)
        ProductLoop.init_db(c)
        if 'origins' not in {r['name'] for r in c.execute('PRAGMA table_info(relay_projects)')}:
            c.execute("ALTER TABLE relay_projects ADD COLUMN origins TEXT NOT NULL DEFAULT '[]'")
    prune()


def prune():
    cutoff = time.time() - RETENTION_DAYS * 86400
    with closing(connect()) as c, c:
        c.execute('DELETE FROM events WHERE ts < ?', (cutoff,))
        c.execute('DELETE FROM people WHERE last_seen < ?', (cutoff,))
        c.execute('DELETE FROM login_limits WHERE window < ?', (time.time()-3600,))
        c.execute('DELETE FROM setup_checks WHERE created < ?', (cutoff,))
        c.execute('DELETE FROM sign_in_links WHERE expires<=?',(time.time(),))
        c.execute('DELETE FROM account_sessions WHERE expires<=?',(time.time(),))
        c.execute('DELETE FROM account_mail WHERE expires<?',(time.time()-86400,))
        c.execute('DELETE FROM usage_receipts WHERE received<?',(time.time()-400*86400,))


@app.on_event('startup')
def startup():
    for key in ['ANALYTICS_ADMIN_TOKEN','ANALYTICS_PASSWORD','ANALYTICS_SESSION_SECRET']:
        if len(os.getenv(key,'')) < 24:
            raise RuntimeError(key+' must contain at least 24 characters')
    init_db()
    _prune_stop.clear()
    def cleanup():
        while not _prune_stop.wait(3600):
            try:prune()
            except sqlite3.Error:pass
    threading.Thread(target=cleanup,name='retention',daemon=True).start()
    def deliver_mail():
        while not _prune_stop.is_set():
            try:accounts.deliver_pending();billing.work()
            except sqlite3.Error:pass
            if _prune_stop.wait(2):break
    threading.Thread(target=deliver_mail,name='account-mail',daemon=True).start()


@app.on_event('shutdown')
def shutdown():
    _prune_stop.set()


def redact(value, depth=0):
    if depth > 5:
        raise ValueError('properties exceed five nested levels')
    if isinstance(value, dict):
        if len(value) > 64:
            raise ValueError('maximum 64 properties per object')
        return {k: '[redacted]' if SENSITIVE.search(k) and not (
            re.fullmatch(r'(?:ai_)?(?:input|output|total|cached|reasoning|prompt|completion)_tokens',k)
            and isinstance(v,(int,float)) and not isinstance(v,bool)) else redact(v,depth+1)
            for k,v in value.items() if len(k)<=100}
    if isinstance(value,list):
        if len(value)>100:raise ValueError('maximum 100 array entries')
        return [redact(v,depth+1) for v in value]
    if isinstance(value,str):
        # Remove recognizable bearer/API credentials even when placed in innocent fields.
        return re.sub(r'(?i)\bBearer\s+[^\s]+|\b(?:mbb_inv_|na_write_|na_admin_|as_read_|as_workspace_|as_session_|as_login_|as_invite_|sk-)[A-Za-z0-9_-]+','[redacted]',value[:2000])
    if value is None or isinstance(value,(bool,int,float)):
        if isinstance(value,float) and not math.isfinite(value):raise ValueError('numbers must be finite')
        return value
    raise ValueError('properties must be JSON values')


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Event(Model):
    id: str = Field(default_factory=lambda:str(uuid.uuid4()), pattern=IDENTIFIER)
    name: str = Field(pattern=IDENTIFIER)
    distinct_id: str | None = Field(default=None, pattern=IDENTIFIER)
    timestamp: datetime | None = None
    journey: JourneyContext | None = None
    properties: dict[str,Any] = Field(default_factory=dict)
    attributes: dict[str,Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def subject_matches(self):
        if 'afterspan_audit' in self.properties:
            self.properties['afterspan_audit']=Observation.model_validate(self.properties['afterspan_audit']).model_dump(mode='json',exclude_unset=True)
        if self.journey and self.journey.subject_kind=='anonymous' and self.distinct_id is not None:
            raise ValueError('anonymous journeys cannot include an identity')
        return self

    @field_validator('timestamp')
    @classmethod
    def utc_timestamp(cls,value):
        return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value

    @field_validator('properties','attributes')
    @classmethod
    def safe_json(cls,value):
        value=redact(value)
        if len(json.dumps(value).encode())>16000:raise ValueError('properties exceed 16 KB')
        return value


class Batch(Model):
    events: list[Event] = Field(min_length=1,max_length=100)


class Identify(Model):
    distinct_id: str = Field(pattern=IDENTIFIER)
    attributes: dict[str,Any] = Field(default_factory=dict)

    @field_validator('attributes')
    @classmethod
    def safe_json(cls,value):return Event.safe_json(value)


class Project(Model):
    id: str = Field(pattern=r'^[a-z][a-z0-9-]{1,49}$')
    name: str = Field(min_length=1,max_length=100)


class Query(Window):
    event: str | None = Field(default=None,max_length=160)
    distinct_id: str | None = Field(default=None,max_length=160)
    property: str | None = Field(default=None,pattern=PROPERTY)
    value: str | None = Field(default=None,max_length=2000)
    filters:list[PropertyFilter]=Field(default_factory=list,max_length=10)
    metric: Literal['count','users','sum','average','p95'] = 'count'
    measure: str | None = Field(default=None,pattern=PROPERTY)
    breakdown: str | None = Field(default=None,pattern=PROPERTY)
    interval: Literal['hour','day'] = 'day'


class JourneyQuery(Window):
    workflow:str|None=Field(default=None,pattern=r'^[a-z][a-z0-9_.-]{0,79}$')
    environment:Literal['production','staging','development','test','local']|None=None


def freeze(q):
    now=time.time()
    cutoff=q.as_of.timestamp() if q.as_of else now
    end=q.end.timestamp() if q.end else cutoff
    start=q.start.timestamp() if q.start else end-q.days*86400
    if cutoff>now+1 or end>cutoff+300:raise HTTPException(422,'window or snapshot is in the future')
    return q.model_copy(update={k:datetime.fromtimestamp(v,timezone.utc) for k,v in [('start',start),('end',end),('as_of',cutoff)]})


def evidence_metadata(project,q,definition):
    query=q.model_dump(mode='json')
    fingerprint=hashlib.sha256(json.dumps({'project':project,'definition':definition,'query':query},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'query':query,'definition_version':definition,'fingerprint':fingerprint,
            'snapshot':{k:query[k] for k in ['start','end','as_of']},
            'limits':['Retention and authorized erasure can remove records from a repeated read.','Events delivered after as_of are excluded, even if their event time falls inside the window.']}


class Chart(Model):
    title: str = Field(min_length=1,max_length=100)
    query: Query


def credential(request):
    header=request.headers.get('authorization','')
    return header[7:] if header.startswith('Bearer ') else ''


def session(value):
    if os.environ.get('AFTERSPAN_LEGACY_LOGIN')!='true':return False
    try:
        payload,signature=value.split('.')
        expected=hmac.new(os.environ['ANALYTICS_SESSION_SECRET'].encode(),payload.encode(),hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature,expected) and time.time()<int(payload)
    except (ValueError,KeyError):return False


def admin(request: Request):
    supplied=credential(request)
    expected=os.getenv('ANALYTICS_ADMIN_TOKEN','')
    if (expected and hmac.compare_digest(supplied.encode(),expected.encode())) or session(request.cookies.get('na_session','')):
        if request.method not in {'GET','HEAD','OPTIONS'} and not supplied:
            if request.headers.get('x-native-csrf')!='1':raise HTTPException(403,'missing CSRF header')
            origin=request.headers.get('origin')
            if origin and origin.rstrip('/')!=os.getenv('ANALYTICS_PUBLIC_URL',str(request.base_url)).rstrip('/'):raise HTTPException(403,'invalid origin')
        return
    raise HTTPException(401,'dashboard sign-in required')


def operator(request):
    token=credential(request);expected=os.getenv('ANALYTICS_ADMIN_TOKEN','')
    return bool(expected and hmac.compare_digest(token.encode(),expected.encode()))


def project_access(request,project,role):
    if operator(request):return
    if session(request.cookies.get('na_session','')):
        admin(request)
        with closing(connect()) as c:row=c.execute('SELECT workspace FROM projects WHERE id=?',(project,)).fetchone()
        if row and row['workspace']=='legacy':return
        raise HTTPException(403,'existing-owner login only accesses existing projects')
    accounts.project_role(request,project,role)


def owner(request:Request,project:str):project_access(request,project,'owner')
def editor(request:Request,project:str):project_access(request,project,'editor')
def analyst(request:Request,project:str):project_access(request,project,'reader')


def read_scope(request,audit=False):
    token=credential(request)
    if not token.startswith('as_read_'):return None
    with closing(connect()) as c:
        row=c.execute('SELECT k.project,k.purpose FROM read_keys k JOIN projects p ON p.id=k.project WHERE k.key_hash=? AND k.expires>? AND (k.issuer IS NULL OR EXISTS(SELECT 1 FROM memberships m WHERE m.workspace=p.workspace AND m.user_id=k.issuer))',
                      (hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
    if not row:raise HTTPException(401,'read key expired or revoked')
    if row['purpose']=='audit' and not audit:raise HTTPException(403,'audit-only key cannot read general analytics')
    return row['project']


def audit_reader(request:Request,project:str):
    scope=read_scope(request,audit=True)
    if scope is None:project_access(request,project,'reader')
    elif scope!=project:raise HTTPException(403,'audit key belongs to another project')


def reader(request:Request,project:str):
    scope=read_scope(request)
    if scope is not None:
        if scope!=project:raise HTTPException(403,'read key belongs to another project')
    else:project_access(request,project,'reader')


def project_exists(project):
    with closing(connect()) as c:
        if not c.execute('SELECT 1 FROM projects WHERE id=?',(project,)).fetchone():raise HTTPException(404,'project not found')


def writer(request: Request):
    token=credential(request)
    with closing(connect()) as c:
        row=c.execute('SELECT id FROM projects WHERE key_hash=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    if not row:raise HTTPException(401,'invalid project write key')
    with _limit_lock:
        now=time.monotonic();start,count=_limits.get(row['id'],(now,0))
        if now-start>=60:start,count=now,0
        if count>=600:raise HTTPException(429,'600 ingestion requests per project per minute',headers={'Retry-After':'60'})
        _limits[row['id']]=(start,count+1)
    return row['id']


@app.middleware('http')
async def headers_and_size(request,call_next):
    # Count actual bytes too; Content-Length is not trusted (chunked uploads exist).
    if request.method in {'POST','PATCH','PUT'}:
        body=bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>2_000_000:return JSONResponse({'detail':'request exceeds 2 MB'},413)
        sent=False
        async def receive():
            nonlocal sent
            if not sent:
                sent=True
                return {'type':'http.request','body':bytes(body),'more_body':False}
            return {'type':'http.disconnect'}
        request._receive=receive
        request._body=bytes(body)
    response=await call_next(request)
    response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
                             'X-Robots-Tag':'noindex, nofollow','X-Frame-Options':'DENY',
                             'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
    return response


@app.get('/healthz')
def health():
    with closing(connect()) as c:c.execute('SELECT 1').fetchone()
    return {'ok':True}


@app.get('/')
def landing():return FileResponse(BASE/'static/landing.html')


@app.get('/app',include_in_schema=False)
def dashboard():return FileResponse(BASE/'static/index.html')


@app.get('/brand',include_in_schema=False)
def brand():return FileResponse(BASE/'static/brand.html')


@app.get('/app.js')
def javascript():return FileResponse(BASE/'static/app.js',media_type='application/javascript')


@app.get('/style.css')
def stylesheet():return FileResponse(BASE/'static/style.css',media_type='text/css')


@app.get('/llms.txt',include_in_schema=False)
def agent_guide():return FileResponse(BASE/'AGENTS.md',media_type='text/plain')


@app.get('/docs',include_in_schema=False)
def api_docs():return FileResponse(BASE/'static/api-docs.html',media_type='text/html')


@app.post('/auth/login')
async def login(request:Request,response:Response):
    if os.environ.get('AFTERSPAN_LEGACY_LOGIN')!='true':raise HTTPException(404,'use account email sign-in')
    try:data=await request.json()
    except ValueError:raise HTTPException(422,'provide a JSON password object') from None
    password=data.get('password','') if isinstance(data,dict) else ''
    # Proxy-normalized peer + a fixed global bucket prevents spoofed IPs bypassing the limit.
    ip=request.client.host if request.client else 'unknown'
    now=time.time()
    with closing(connect()) as c,c:
        for key in {ip,'__global__'}:
            row=c.execute('SELECT * FROM login_limits WHERE ip=?',(key,)).fetchone()
            count=row['attempts'] if row and now-row['window']<900 else 0
            if count >= (100 if key=='__global__' else 10):raise HTTPException(429,'too many login attempts; wait 15 minutes')
            c.execute('INSERT INTO login_limits VALUES(?,?,1) ON CONFLICT(ip) DO UPDATE SET window=?,attempts=?',
                      (key,now,row['window'] if row and count else now,count+1))
    if not isinstance(password,str) or not hmac.compare_digest(password.encode(),os.environ['ANALYTICS_PASSWORD'].encode()):
        raise HTTPException(401,'incorrect password')
    payload=str(int(now+43200))
    signature=hmac.new(os.environ['ANALYTICS_SESSION_SECRET'].encode(),payload.encode(),hashlib.sha256).hexdigest()
    response.set_cookie('na_session',payload+'.'+signature,httponly=True,secure=os.getenv('COOKIE_SECURE','true')=='true',samesite='strict',max_age=43200)
    return {'ok':True}


@app.post('/auth/logout')
def logout(request:Request,response:Response):
    accounts.csrf(request)
    with closing(connect()) as c,c:c.execute('DELETE FROM account_sessions WHERE key_hash=?',(hashlib.sha256(request.cookies.get('as_session','').encode()).hexdigest(),))
    response.delete_cookie('as_session');response.delete_cookie('na_session')
    return {'ok':True}


@app.get('/api/projects')
def projects(request:Request,workspace:str|None=None):
    scope=read_scope(request)
    if scope:
        if workspace:
            with closing(connect()) as c:record=c.execute('SELECT workspace FROM projects WHERE id=?',(scope,)).fetchone()
            if not record or record['workspace']!=workspace:raise HTTPException(403,'workspace access denied')
        clause='p.id=?';params=[scope]
    elif operator(request):clause='p.workspace=?' if workspace else '1=1';params=[workspace] if workspace else []
    elif session(request.cookies.get('na_session','')):
        admin(request);clause="p.workspace='legacy'";params=[]
        if workspace and workspace!='legacy':raise HTTPException(403,'workspace access denied')
    else:
        principal=accounts.principal(request)
        visible=[w['id'] for w in accounts.workspaces(principal)]
        if workspace:
            accounts.workspace_role(request,workspace);visible=[workspace]
        clause='p.workspace IN ('+','.join('?' for _ in visible)+')' if visible else '0=1';params=visible
    with closing(connect()) as c:
        rows=c.execute('SELECT p.id,p.slug,p.workspace,p.name,p.created,(SELECT count(*) FROM events e WHERE e.project=p.id) AS stored_events,(SELECT max(received) FROM events e WHERE e.project=p.id) AS last_ingest FROM projects p WHERE '+clause+' ORDER BY p.created',params).fetchall()
    return {'projects':[dict(r) for r in rows],'retention_days':RETENTION_DAYS}


@app.post('/api/projects')
def create_project(req:Project,request:Request,workspace:str|None=None):
    if operator(request):workspace=workspace or 'legacy';principal=None
    elif session(request.cookies.get('na_session','')):
        admin(request)
        if workspace and workspace!='legacy':raise HTTPException(403,'workspace access denied')
        workspace='legacy';principal=None
    else:
        p=accounts.principal(request);workspace=workspace or p['workspace']
        if not workspace:raise HTTPException(422,'select a workspace')
        principal=accounts.workspace_role(request,workspace,'editor')
    key='na_write_'+secrets.token_urlsafe(32);project_id=req.id if workspace=='legacy' else 'p_'+uuid.uuid4().hex
    try:
        with closing(connect()) as c,c:
            if not c.execute('SELECT 1 FROM workspaces WHERE id=?',(workspace,)).fetchone():raise HTTPException(404,'workspace not found')
            c.execute('BEGIN IMMEDIATE')
            if principal:accounts.workspace_role(request,workspace,'editor',c)
            billing.capacity(c,workspace,'projects')
            c.execute('INSERT INTO projects(id,name,key_hash,created,workspace,slug) VALUES(?,?,?,?,?,?)',(project_id,req.name,hashlib.sha256(key.encode()).hexdigest(),time.time(),workspace,req.id))
            if principal:accounts.audit(c,workspace,principal,'project.created',project_id)
    except sqlite3.IntegrityError:raise HTTPException(409,'project ID already exists in this workspace') from None
    return {'id':project_id,'slug':req.id,'workspace':workspace,'name':req.name,'write_key':key,'note':'Write-only key; shown once. Keep it server-side.'}


def audit_project(c,request,project,action):
    actor={'id':'operator'} if operator(request) else {'id':'existing-owner'} if session(request.cookies.get('na_session','')) else accounts.principal(request)
    workspace=c.execute('SELECT workspace FROM projects WHERE id=?',(project,)).fetchone()['workspace']
    accounts.audit(c,workspace,actor,action,project)


@app.post('/api/projects/{project}/rotate-key',dependencies=[Depends(owner)])
def rotate_key(project:str,request:Request):
    project_exists(project)
    key='na_write_'+secrets.token_urlsafe(32)
    with closing(connect()) as c,c:
        c.execute('UPDATE projects SET key_hash=? WHERE id=?',(hashlib.sha256(key.encode()).hexdigest(),project))
        audit_project(c,request,project,'project.producer_key_rotated')
    return {'id':project,'write_key':key,'note':'Old producer key is revoked. Update the producer with this key.'}


class ReadGrant(Model):
    purpose:Literal['analytics','audit']='analytics'
    label: str = Field(min_length=1,max_length=80)
    expires_days: int = Field(default=30,ge=1,le=90)


@app.post('/api/projects/{project}/read-keys',dependencies=[Depends(owner)])
def create_read_key(project:str,req:ReadGrant,request:Request):
    project_exists(project)
    token='as_read_'+secrets.token_urlsafe(32);now=time.time();kid=str(uuid.uuid4());expires=now+req.expires_days*86400
    issuer=None if operator(request) or session(request.cookies.get('na_session','')) else accounts.principal(request)['id']
    with closing(connect()) as c,c:
        if c.execute('SELECT count(*) FROM read_keys WHERE project=? AND expires>?',(project,now)).fetchone()[0]>=20:
            raise HTTPException(422,'maximum 20 active read keys per project')
        c.execute('INSERT INTO read_keys(id,project,label,key_hash,created,expires,issuer,purpose) VALUES(?,?,?,?,?,?,?,?)',(kid,project,req.label,hashlib.sha256(token.encode()).hexdigest(),now,expires,issuer,req.purpose))
        audit_project(c,request,project,'project.read_key_created')
    return {'id':kid,'project':project,'label':req.label,'purpose':req.purpose,'read_key':token,'expires':expires,
            'scope':'aggregates:read','note':'Shown once. No raw records, writes, or access to other projects.'}


@app.get('/api/projects/{project}/read-keys',dependencies=[Depends(owner)])
def read_keys(project:str):
    project_exists(project)
    with closing(connect()) as c:
        rows=c.execute('SELECT id,label,created,expires,issuer,purpose FROM read_keys WHERE project=? ORDER BY created DESC',(project,)).fetchall()
    return {'keys':[dict(r) for r in rows]}


@app.delete('/api/projects/{project}/read-keys/{key_id}',dependencies=[Depends(owner)])
def revoke_read_key(project:str,key_id:str,request:Request):
    project_exists(project)
    with closing(connect()) as c,c:
        removed=c.execute('DELETE FROM read_keys WHERE project=? AND id=?',(project,key_id)).rowcount
        if removed:audit_project(c,request,project,'project.read_key_revoked')
    return {'revoked':bool(removed)}



def identify_person(c,project,distinct_id,attributes,now):
    if not distinct_id:return
    if c.execute('SELECT 1 FROM erased_identities WHERE project=? AND distinct_id=?',(project,distinct_id)).fetchone():return
    old=c.execute('SELECT attributes FROM people WHERE project=? AND distinct_id=?',(project,distinct_id)).fetchone()
    attrs=json.loads(old['attributes']) if old else {}
    attrs.update(attributes)
    attrs=Event.safe_json(attrs)
    c.execute('''INSERT INTO people VALUES(?,?,?,?,?) ON CONFLICT(project,distinct_id)
               DO UPDATE SET attributes=excluded.attributes,last_seen=excluded.last_seen''',
              (project,distinct_id,json.dumps(attrs),now,now))


def record_journey(c,project,event):
    j=event.journey
    if j is None:return
    scope=(project,j.environment,j.workflow,j.id)
    clause='j.project=? AND j.environment=? AND j.workflow=? AND j.journey_id=?'
    first=c.execute('SELECT j.subject_kind,e.distinct_id FROM journey_events j JOIN events e ON e.project=j.project AND e.id=j.event_id WHERE '+clause+' LIMIT 1',scope).fetchone()
    if first and (first['subject_kind']!=j.subject_kind or first['distinct_id']!=event.distinct_id):
        raise HTTPException(409,'journey subject cannot change; use a new journey ID')
    old=c.execute('SELECT j.attempt_id,j.attempt_number,j.release FROM journey_events j WHERE '+clause+' AND (attempt_id=? OR attempt_number=?) LIMIT 1',(*scope,j.attempt_id,j.attempt_number)).fetchone()
    if old:
        if old['attempt_id']!=j.attempt_id or old['attempt_number']!=j.attempt_number:
            raise HTTPException(409,'attempt ID and number must identify the same attempt')
        if old['release']!=j.release:raise HTTPException(409,'release cannot change within one attempt')
    c.execute('INSERT INTO journey_events VALUES(?,?,?,?,?,?,?,?,?,?)',
        (project,event.id,j.id,j.workflow,j.environment,j.subject_kind,j.attempt_id,j.attempt_number,j.state,j.release))


@app.post('/v1/events')
def ingest(req:Batch,project=Depends(writer)):
    now=time.time();accepted=0
    records=[]
    for event in req.events:
        ts=event.timestamp.timestamp() if event.timestamp else now
        if ts<now-RETENTION_DAYS*86400 or ts>now+300:raise HTTPException(422,'timestamp outside retention or more than five minutes in future')
        records.append((event,ts))
    try:
        with closing(connect()) as c,c:
            c.execute('BEGIN IMMEDIATE')
            for event,ts in records:
                if billing.seen(c,project,event.id):continue
                if event.distinct_id and c.execute('SELECT 1 FROM erased_identities WHERE project=? AND distinct_id=?',(project,event.distinct_id)).fetchone():continue
                if event.journey and c.execute('SELECT 1 FROM erased_journeys WHERE project=? AND environment=? AND workflow=? AND journey_id=?',(project,event.journey.environment,event.journey.workflow,event.journey.id)).fetchone():continue
                changed=c.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)',
                    (project,event.id,event.name,event.distinct_id,ts,now,json.dumps(event.properties))).rowcount
                if changed:
                    record_journey(c,project,event)
                    billing.record(c,project,event.id,len(json.dumps(event.model_dump(mode="json"),separators=(",",":"),ensure_ascii=False).encode()),event.name in {"analytics.pipeline","afterspan.setup.checked"},now)
                    accepted+=1
                    identify_person(c,project,event.distinct_id,event.attributes,now)
    except ValueError as exc:raise HTTPException(422,str(exc)) from None
    return {'accepted':accepted,'duplicates':len(req.events)-accepted}


@app.post('/v1/identify')
def identify(req:Identify,project=Depends(writer)):
    try:
        with closing(connect()) as c,c:identify_person(c,project,req.distinct_id,req.attributes,time.time())
    except ValueError as exc:raise HTTPException(422,str(exc)) from None
    return {'ok':True}


class Erase(Model):
    distinct_id: str = Field(pattern=IDENTIFIER)


@app.post('/v1/erase')
def erase_identity(req:Erase,project=Depends(writer)):
    with closing(connect()) as c,c:
        c.execute('INSERT OR IGNORE INTO erased_journeys SELECT j.project,j.environment,j.workflow,j.journey_id,? FROM journey_events j JOIN events e ON e.project=j.project AND e.id=j.event_id WHERE e.project=? AND e.distinct_id=?',(time.time(),project,req.distinct_id))
        removed=c.execute('DELETE FROM events WHERE project=? AND distinct_id=?',(project,req.distinct_id)).rowcount
        c.execute('DELETE FROM people WHERE project=? AND distinct_id=?',(project,req.distinct_id))
        c.execute('INSERT OR IGNORE INTO erased_identities VALUES(?,?,?)',(project,req.distinct_id,time.time()))
    return {'ok':True,'erased_events':removed,'stale_identity_blocked':True}


class RelayConfig(Model):
    enabled: bool
    events: list[str] = Field(min_length=1,max_length=30)
    origins: list[str] = Field(default_factory=list,max_length=10)

    @field_validator('origins')
    @classmethod
    def browser_origins(cls,values):
        for value in values:
            url=urlsplit(value)
            if url.scheme!='https' or not url.netloc or url.path or url.query or url.fragment or url.username or url.password:
                raise ValueError('origins must be exact HTTPS origins')
        return values

    @field_validator('events')
    @classmethod
    def event_names(cls,values):
        if any(not re.fullmatch(r'[a-z][a-z0-9_.]{0,60}',v) for v in values):raise ValueError('invalid event name')
        return values


class RelayEvent(Model):
    id: uuid.UUID
    install_id: uuid.UUID
    consent: Literal[True]
    event: str = Field(pattern=r'^[a-z][a-z0-9_.]{0,60}$')
    version: str = Field(default='unknown',pattern=r'^[A-Za-z0-9_.-]{1,40}$')
    feature: Literal['app','process','ports','disk','memory','energy','settings','web'] = 'app'
    placement: Literal['nav','hero','install'] | None = None
    outcome: Literal['started','success','failure','cancelled'] = 'success'
    count: int = Field(default=0,ge=0,le=100000)
    duration_ms: float = Field(default=0,ge=0,le=3600000,allow_inf_nan=False)


@app.post('/api/projects/{project}/relay',dependencies=[Depends(owner)])
def configure_relay(project:str,req:RelayConfig,request:Request):
    project_exists(project)
    with closing(connect()) as c,c:
        c.execute('INSERT INTO relay_projects(project,events,enabled,origins) VALUES(?,?,?,?) ON CONFLICT(project) DO UPDATE SET events=excluded.events,enabled=excluded.enabled,origins=excluded.origins',
                  (project,json.dumps(req.events),int(req.enabled),json.dumps(req.origins)))
        audit_project(c,request,project,'project.relay_enabled' if req.enabled else 'project.relay_disabled')
    return {'ok':True,'enabled':req.enabled}


@app.middleware('http')
async def relay_browser_origin(request:Request,call_next):
    origin=request.headers.get('origin')
    path=request.url.path
    if not origin or not path.startswith('/v1/relay/'):
        return await call_next(request)
    project=path.removeprefix('/v1/relay/')
    with closing(connect()) as c:
        setting=c.execute('SELECT enabled,origins FROM relay_projects WHERE project=?',(project,)).fetchone()
    if not setting or not setting['enabled'] or origin not in json.loads(setting['origins']):
        return JSONResponse({'detail':'browser origin not enabled'},403)
    if request.method=='OPTIONS':
        requested={v.strip().lower() for v in request.headers.get('access-control-request-headers','').split(',') if v.strip()}
        if request.headers.get('access-control-request-method')!='POST' or requested-{'content-type'}:
            return JSONResponse({'detail':'unsupported preflight'},400)
        response=Response(status_code=204,headers={'Access-Control-Allow-Methods':'POST','Access-Control-Allow-Headers':'Content-Type','Access-Control-Max-Age':'600'})
    else:
        response=await call_next(request)
    response.headers['Access-Control-Allow-Origin']=origin
    response.headers['Vary']='Origin'
    return response


@app.post('/v1/relay/{project}')
def public_relay(project:str,req:RelayEvent,request:Request):
    with closing(connect()) as c:
        setting=c.execute('SELECT * FROM relay_projects WHERE project=?',(project,)).fetchone()
    if not setting or not setting['enabled'] or req.event not in json.loads(setting['events']):raise HTTPException(403,'relay event not enabled')
    candidate=request.headers.get('x-real-ip','') if os.getenv('RAILWAY_ENVIRONMENT_ID') else request.client.host if request.client else ''
    try:ip=str(ipaddress.ip_address(candidate))
    except ValueError:ip=None
    bucket='relay:'+project+':'+(ip or 'unknown');now=time.monotonic()
    with _limit_lock:
        if len(_limits)>20000:
            for k in list(_limits):
                if now-_limits[k][0]>60:del _limits[k]
            if len(_limits)>20000:raise HTTPException(429,'relay capacity reached')
        start,count=_limits.get(bucket,(now,0))
        if now-start>=60:start,count=now,0
        if count>=60:raise HTTPException(429,'relay rate limit',headers={'Retry-After':'60'})
        _limits[bucket]=(start,count+1)
    # Fixed schema: no caller-defined text, properties, URLs, process names or paths.
    identity='install:'+str(req.install_id)
    properties={'source':project,'feature':req.feature,'outcome':req.outcome,'count':req.count,'duration_ms':req.duration_ms,
                'version':req.version,'placement':req.placement,'client_reported':True,'consent':True,'ip':ip}
    with closing(connect()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        if billing.seen(c,project,str(req.id)) or c.execute('SELECT 1 FROM erased_identities WHERE project=? AND distinct_id=?',(project,identity)).fetchone():return {'accepted':0,'duplicates':1}
        changed=c.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)',(project,str(req.id),req.event,identity,time.time(),time.time(),json.dumps(properties))).rowcount
        if changed:
            billing.record(c,project,str(req.id),len(json.dumps(req.model_dump(mode='json'),separators=(',',':'),ensure_ascii=False).encode()),False,time.time())
            identify_person(c,project,identity,{'source':project,'platform':'web' if req.feature=='web' else 'macOS','version':req.version},time.time())
    return {'accepted':changed,'duplicates':1-changed}


def where(project,q):
    q=freeze(q) if q.start is None else q
    sql='project=? AND ts>=? AND ts<? AND received<=?';params=[project,q.start.timestamp(),q.end.timestamp(),q.as_of.timestamp()]
    for column,value in [('name',getattr(q,'event',None)),('distinct_id',getattr(q,'distinct_id',None))]:
        if value is not None:sql+=' AND '+column+'=?';params.append(value)
    if getattr(q,'property',None) and q.value is not None:
        sql+=' AND CAST(json_extract(properties,?) AS TEXT)=?';params.extend(['$.'+q.property,q.value])
    for f in getattr(q,'filters',[]):
        path='$.'+f.property
        if f.value is None:sql+=" AND json_type(properties,?)='null'";params.append(path)
        elif isinstance(f.value,bool):sql+=' AND json_type(properties,?)=?';params.extend([path,'true' if f.value else 'false'])
        elif isinstance(f.value,(int,float)):
            sql+=" AND json_type(properties,?) IN ('integer','real') AND json_extract(properties,?)=?";params.extend([path,path,f.value])
        else:sql+=" AND json_type(properties,?)='text' AND json_extract(properties,?)=?";params.extend([path,path,f.value])
    return sql,params


def product_only(clause):
    return clause+" AND name NOT IN ('analytics.pipeline','afterspan.setup.checked')"


def percentile(values):
    return sorted(values)[max(0,math.ceil(len(values)*.95)-1)] if values else None


def aggregate(rows,q):
    if q.metric=='count':return len(rows)
    if q.metric=='users':return len({r['distinct_id'] for r in rows if r['distinct_id']})
    values=[r['properties'].get(q.measure) for r in rows]
    values=[v for v in values if isinstance(v,(int,float)) and not isinstance(v,bool)]
    if q.metric=='sum':return sum(values)
    if q.metric=='average':return sum(values)/len(values) if values else None
    return percentile(values)


def query_data(project,q):
    q=freeze(q)
    clause,params=where(project,q)
    if q.event is None:clause=product_only(clause)
    with closing(connect()) as c:
        count=c.execute('SELECT count(*) FROM events WHERE '+clause,params).fetchone()[0]
        if count>200000:raise HTTPException(422,'query exceeds 200,000 events; shorten the time window')
        rows=[{**dict(r),'properties':json.loads(r['properties'])} for r in c.execute('SELECT name,distinct_id,ts,properties FROM events WHERE '+clause,params)]
    buckets={};groups={}
    step=3600 if q.interval=='hour' else 86400
    for row in rows:
        bucket=int(row['ts']//step)*step;buckets.setdefault(bucket,[]).append(row)
        if q.breakdown:
            value=row['properties'].get(q.breakdown,'(unset)');label=json.dumps(value) if isinstance(value,(dict,list)) else str(value)
            groups.setdefault(label,[]).append(row)
    start=int(q.start.timestamp()//step)*step;end=int((q.end.timestamp()-0.000001)//step)*step
    series=[{'ts':b,'value':aggregate(buckets.get(b,[]),q)} for b in range(start,end+1,step)]
    breakdown=sorted([{'label':k,'value':aggregate(v,q)} for k,v in groups.items()],key=lambda x:x['value'] if x['value'] is not None else -1,reverse=True)[:20]
    return {'total':aggregate(rows,q),'matched_events':len(rows),'series':series,'breakdown':breakdown,'timezone':'UTC',**evidence_metadata(project,q,'events-v2')}


@app.post('/api/query',dependencies=[Depends(reader)])
def query(req:Query,project:str):
    project_exists(project)
    if req.metric not in {'count','users'} and not req.measure:raise HTTPException(422,'numeric metric needs measure')
    return query_data(project,req)


@app.get('/api/overview',dependencies=[Depends(reader)])
def overview(project:str,days:int=7):
    project_exists(project)
    q=freeze(Query(days=days));clause,params=where(project,q);clause=product_only(clause)
    with closing(connect()) as c:
        totals=dict(c.execute('''SELECT count(*) AS total_events,count(DISTINCT distinct_id) AS active_users,
          sum(CASE WHEN name='http.request' THEN 1 ELSE 0 END) AS requests,
          sum(CASE WHEN name='http.request' AND json_extract(properties,'$.status')>=400 THEN 1 ELSE 0 END) AS errors,
          sum(CASE WHEN name='http.request' AND json_extract(properties,'$.status')>=500 THEN 1 ELSE 0 END) AS server_errors,
          sum(CASE WHEN json_extract(properties,'$.outcome') IN ('failure','failed','error') OR json_extract(properties,'$.is_error')=1 OR name LIKE '%.failed' THEN 1 ELSE 0 END) AS event_errors,
          count(DISTINCT CASE WHEN name='http.request' THEN json_extract(properties,'$.ip') END) AS unique_ips
          FROM events WHERE '''+clause,params).fetchone())
        latency=[r[0] for r in c.execute("SELECT json_extract(properties,'$.duration_ms') FROM events WHERE "+clause+" AND name='http.request' AND json_type(properties,'$.duration_ms') IN ('integer','real')",params)]
        names=[dict(r) for r in c.execute('SELECT name,count(*) AS count FROM events WHERE '+clause+' GROUP BY name ORDER BY count DESC LIMIT 30',params)]
        latest=c.execute('SELECT max(received) FROM events WHERE project=?',(project,)).fetchone()[0]
        pulse=c.execute("SELECT properties,received FROM events WHERE project=? AND name='analytics.pipeline' ORDER BY received DESC LIMIT 1",(project,)).fetchone()
    totals.update(p95_ms=percentile(latency),average_ms=sum(latency)/len(latency) if latency else None,last_ingest=latest,
                  error_rate=100*(totals['errors'] or 0)/(totals['requests'] or 1))
    series=query_data(project,Query(**q.model_dump(include={'days','start','end','as_of'}),event='http.request' if totals['requests'] else None,interval='hour' if days==1 else 'day',breakdown='operation' if totals['requests'] else None))
    if not totals['requests']:series['breakdown']=[{'label':n['name'],'value':n['count']} for n in names[:20]]
    return {'totals':totals,'traffic':series,'event_names':names,'pipeline':{**json.loads(pulse['properties']),'received':pulse['received']} if pulse else None,'retention_days':RETENTION_DAYS,'mode':'requests' if totals['requests'] else 'events'}



def journey_report(project,req):
    project_exists(project)
    q=freeze(req)
    clause="j.project=? AND e.ts<? AND e.received<=? AND e.name NOT IN ('analytics.pipeline','afterspan.setup.checked')"
    params=[project,q.end.timestamp(),q.as_of.timestamp()]
    join=' FROM journey_events j JOIN events e ON e.project=j.project AND e.id=j.event_id'
    with closing(connect()) as c:
        options=[dict(row) for row in c.execute('SELECT DISTINCT j.workflow,j.environment'+join+' WHERE '+clause+' ORDER BY j.workflow,j.environment LIMIT 200',params)]
        for key in ['workflow','environment']:
            if getattr(q,key) is not None:clause+=' AND j.'+key+'=?';params.append(getattr(q,key))
        if c.execute('SELECT count(*)'+join+' WHERE '+clause,params).fetchone()[0]>200000:
            raise HTTPException(422,'journey history exceeds 200,000 events; narrow workflow or environment')
        rows=[dict(row) for row in c.execute('SELECT j.*,e.distinct_id,e.ts'+join+' WHERE '+clause,params)]
        unlinked=c.execute("SELECT count(*) FROM events e LEFT JOIN journey_events j ON e.project=j.project AND e.id=j.event_id WHERE e.project=? AND e.ts>=? AND e.ts<? AND e.received<=? AND e.name NOT IN ('analytics.pipeline','afterspan.setup.checked') AND j.event_id IS NULL",
            (project,q.start.timestamp(),q.end.timestamp(),q.as_of.timestamp())).fetchone()[0]
    items=project_journeys(rows,q.start.timestamp())
    report={'totals':summarize(items),'coverage':{'linked_events_in_window':sum(row['ts']>=q.start.timestamp() for row in rows),
        'unlinked_project_events_in_window':unlinked,'unlinked_scope':'Project window; these events cannot be assigned to the selected workflow/environment.',
        'history':'Retained history through window end, received by as_of. Missing starts and attempt numbers are reported.'},
        'available_scopes':options,'unit':'Journeys with recorded activity in the event-time window; not all eligible users.',
        **evidence_metadata(project,q,'journeys-v1')}
    report['limits']+=['Outcomes are producer-reported attempt states, not independent quality checks.',
        'Failed means the latest known attempt failed by window end; a later retry may recover.',
        'Recovered journeys are included in succeeded. Identity kinds are producer asserted.']
    return report,items



class SetupCheck(Model):
    environment:Literal['production','staging','development','test','local']='production'


def setup_event(check_id,environment):
    return {'id':'setup:'+check_id,'name':'afterspan.setup.checked',
            'properties':{'setup_check_id':check_id,'environment':environment,'source':'afterspan.connection_check'}}


@app.post('/api/setup-checks',dependencies=[Depends(editor)])
def create_setup_check(req:SetupCheck,project:str):
    project_exists(project)
    now=time.time();check_id=str(uuid.uuid4())
    with closing(connect()) as c,c:
        if c.execute('SELECT count(*) FROM setup_checks WHERE project=? AND expires>?',(project,now)).fetchone()[0]>=10:
            raise HTTPException(429,'ten setup checks are already active; use a current check or wait')
        c.execute('INSERT INTO setup_checks VALUES(?,?,?,?,?)',(check_id,project,req.environment,now,now+900))
    return {'id':check_id,'environment':req.environment,'expires':now+900,
        'event':setup_event(check_id,req.environment),
        'next':'Send this exact event through your producer using its existing write key. It is excluded from default product and journey totals.'}


@app.get('/api/setup',dependencies=[Depends(reader)])
def setup_status(project:str,environment:Literal['production','staging','development','test','local']='production'):
    project_exists(project)
    with closing(connect()) as c:
        check=c.execute('SELECT * FROM setup_checks WHERE project=? AND environment=? ORDER BY created DESC LIMIT 1',(project,environment)).fetchone()
        event=None
        if check:
            event=c.execute("SELECT received FROM events WHERE project=? AND id=? AND name='afterspan.setup.checked' AND json_extract(properties,'$.setup_check_id')=? AND json_extract(properties,'$.environment')=? AND received>=? AND received<=? AND ts>=? AND ts<=?",
                (project,'setup:'+check['id'],check['id'],environment,check['created'],check['expires'],check['created']-300,check['expires'])).fetchone()
        last=c.execute('SELECT max(received) FROM events WHERE project=?',(project,)).fetchone()[0]
        name=c.execute('SELECT name FROM projects WHERE id=?',(project,)).fetchone()[0]
    report,items=journey_report(project,JourneyQuery(environment=environment))
    t=report['totals']
    complete=any(j['outcome'] in {'succeeded','failed','cancelled'} and not j['missing_starts'] and not j['missing_attempts'] and not j['conflicting'] for j in items)
    state='not_started' if check is None else 'verified' if event else 'expired' if time.time()>check['expires'] else 'waiting'
    return {'project':project,'name':name,'environment':environment,'last_received_at':last,
        'connection':{'state':state,'check_id':check['id'] if check else None,'expires':check['expires'] if check else None,'verified_at':event['received'] if event else None,
                      'event':setup_event(check['id'],environment) if state=='waiting' else None},
        'journeys':{'state':'observed' if complete else 'not_seen','totals':t,'snapshot':report['snapshot']},
        'retry_recovery':{'state':'observed' if t['recovered_journeys'] else 'not_seen'},
        'limits':['The connection receipt proves only that its matching check event arrived in this project and environment.',
                  'Journey states describe retained producer reports, not independently verified user success.',
                  'No app code, recording, survey, customer action or deployment is changed by a setup check.']}


@app.post('/api/journeys',dependencies=[Depends(reader)])
def journey_summary(req:JourneyQuery,project:str):
    return journey_report(project,req)[0]


@app.post('/api/journeys/list',dependencies=[Depends(analyst)])
def journey_list(req:JourneyQuery,project:str,offset:int=0):
    if offset<0 or offset>100000:raise HTTPException(422,'invalid offset')
    report,items=journey_report(project,req)
    return {**report,'journeys':items[offset:offset+50],'has_more':len(items)>offset+50,'next_offset':offset+50 if len(items)>offset+50 else None}


@app.get('/api/events',dependencies=[Depends(analyst)])
def events(project:str,days:int=7,event:str|None=None,distinct_id:str|None=None,property:str|None=None,value:str|None=None,
           status:int|None=None,ip:str|None=None,operation:str|None=None,before:float|None=None,before_id:str|None=None):
    project_exists(project)
    q=freeze(Query(days=days,event=event,distinct_id=distinct_id,property=property,value=value))
    clause,params=where(project,q)
    for key,val in [('status',status),('ip',ip),('operation',operation)]:
        if val is not None:clause+=' AND json_extract(properties,?)=?';params.extend(['$.'+key,val])
    if before is not None:
        clause+=' AND (ts<? OR (ts=? AND id<?))';params.extend([before,before,before_id or ''])
    with closing(connect()) as c:rows=c.execute('SELECT * FROM events WHERE '+clause+' ORDER BY ts DESC,id DESC LIMIT 101',params).fetchall()
    items=[{**dict(r),'properties':json.loads(r['properties'])} for r in rows[:100]]
    return {'events':items,'has_more':len(rows)>100,'next_cursor':{'before':items[-1]['ts'],'before_id':items[-1]['id']} if len(rows)>100 else None}


@app.get('/api/people',dependencies=[Depends(analyst)])
def people(project:str,search:str='',offset:int=0):
    project_exists(project)
    if offset<0 or offset>100000:raise HTTPException(422,'invalid offset')
    with closing(connect()) as c:
        rows=c.execute('SELECT * FROM people WHERE project=? AND (distinct_id LIKE ? OR attributes LIKE ?) ORDER BY last_seen DESC LIMIT 101 OFFSET ?',
                       (project,'%'+search[:160]+'%','%'+search[:160]+'%',offset)).fetchall()
    return {'people':[{**dict(r),'attributes':json.loads(r['attributes'])} for r in rows[:100]],'has_more':len(rows)>100}


@app.patch('/api/people',dependencies=[Depends(editor)])
def tag_person(req:Identify,project:str):
    project_exists(project)
    with closing(connect()) as c,c:identify_person(c,project,req.distinct_id,req.attributes,time.time())
    return {'ok':True}


@app.get('/api/charts',dependencies=[Depends(reader)])
def charts(project:str):
    project_exists(project)
    with closing(connect()) as c:rows=c.execute('SELECT * FROM charts WHERE project=? ORDER BY created',(project,)).fetchall()
    return {'charts':[{**dict(r),'definition':json.loads(r['definition'])} for r in rows]}


@app.post('/api/charts',dependencies=[Depends(editor)])
def save_chart(req:Chart,project:str):
    project_exists(project)
    if req.query.metric not in {'count','users'} and not req.query.measure:raise HTTPException(422,'numeric metric needs measure')
    with closing(connect()) as c,c:
        if c.execute('SELECT count(*) FROM charts WHERE project=?',(project,)).fetchone()[0]>=30:raise HTTPException(422,'maximum 30 saved charts per project')
        cid=str(uuid.uuid4());c.execute('INSERT INTO charts VALUES(?,?,?,?,?)',(cid,project,req.title,req.query.model_dump_json(),time.time()))
    return {'id':cid}


@app.delete('/api/charts/{chart_id}',dependencies=[Depends(editor)])
def delete_chart(chart_id:str,project:str):
    with closing(connect()) as c,c:c.execute('DELETE FROM charts WHERE id=? AND project=?',(chart_id,project))
    return {'ok':True}


@app.post('/api/maintenance',dependencies=[Depends(admin)])
def maintenance(request:Request):
    if not operator(request):raise HTTPException(403,'operator token required')
    prune()
    return {'ok':True,'retention_days':RETENTION_DAYS}

class ExistingOwner(Model):
    account_id:str=Field(pattern=r'^u_[a-f0-9]{32}$')


@app.post('/api/operator/assign-existing')
def assign_existing(req:ExistingOwner,request:Request):
    if not operator(request):raise HTTPException(401,'operator token required')
    with closing(connect()) as c,c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT 1 FROM accounts WHERE id=?',(req.account_id,)).fetchone():raise HTTPException(404,'verified account not found')
        if c.execute("SELECT 1 FROM memberships WHERE workspace='legacy'").fetchone():raise HTTPException(409,'existing workspace already has an owner; use its team controls')
        c.execute("INSERT INTO memberships VALUES('legacy',?,'owner',?)",(req.account_id,time.time()))
        accounts.audit(c,'legacy',{'id':'operator'},'workspace.existing_assigned',req.account_id,'owner')
    return {'workspace':'legacy','owner':req.account_id}


accounts.install(app)
billing.install(app)

@app.get('/pricing')
def pricing_page():return FileResponse(BASE/'static/pricing.html')

@app.get('/billing-test')
def local_billing_page():
    billing.local()
    return FileResponse(BASE/'static/billing-test.html')

app.mount('/assets',StaticFiles(directory=BASE/'static'),name='assets')


products=ProductAnalytics(app,connect,reader,editor,freeze,evidence_metadata)
Retention(app,products,reader,editor)
Catalog(app,products,reader,editor)

Audit(app,connect,audit_reader,freeze,evidence_metadata,editor)
product_loop=ProductLoop(app,connect,reader,editor,owner,RETENTION_DAYS)


@app.get('/.well-known/afterspan')
def capabilities():
    return {'product':'Afterspan','license':'MIT','schema':'/openapi.json','agent_guide':'/llms.txt',
      'analytics':{'catalog':'/api/catalog','funnels':'/api/funnels/analyze','retention':'/api/retention/analyze','unit':'explicit opaque identity; not automatically a person'},
      'product_loop':{'goals':'/api/loop/goals','contract':'committed goals, scoped tracking PRs, independent answer checks and mature observational outcomes','local_mcp':'agent/loop_server.py','production_release_proof':'separate from PR and merge'},
      'audit':{'catalog':'/api/audit/catalog','review':'/api/audit/review','trace':'/api/audit/trace','contract':'properties.afterspan_audit / slack-request-v1','scope':'project audit-purpose read key','historical_evidence':'/api/audit/evidence/{UUIDv4}','max_requests':60,'raw_data':False,'overall_readiness_claim':False},
      'connection':{'transport':'local stdio MCP','bootstrap':'An owner uses connect-audit to issue a private, expiring audit-only connection receipt. No operator token fallback.','launcher':'agent/run_audit.py PRIVATE_RECEIPT','remote_oauth':False}}
