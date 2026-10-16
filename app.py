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

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

BASE = Path(__file__).parent
DB_PATH = Path(os.getenv('ANALYTICS_DB', '/data/analytics.db'))
RETENTION_DAYS = max(1, min(3650, int(os.getenv('RETENTION_DAYS', '90'))))
SENSITIVE = re.compile(r'password|passwd|secret|token|authorization|cookie|otp|api.?key|request.?body|response.?body', re.I)
IDENTIFIER = r'^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,159}$'
PROPERTY = r'^[A-Za-z_][A-Za-z0-9_]{0,63}$'
app = FastAPI(title='Native Analytics', version='1.0.0', docs_url=None, redoc_url=None, openapi_url=None)
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
        CREATE TABLE IF NOT EXISTS people(project TEXT NOT NULL REFERENCES projects(id),distinct_id TEXT NOT NULL,
          attributes TEXT NOT NULL,first_seen REAL NOT NULL,last_seen REAL NOT NULL,PRIMARY KEY(project,distinct_id));
        CREATE TABLE IF NOT EXISTS charts(id TEXT PRIMARY KEY,project TEXT NOT NULL REFERENCES projects(id),title TEXT NOT NULL,
          definition TEXT NOT NULL,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS login_limits(ip TEXT PRIMARY KEY,window REAL NOT NULL,attempts INTEGER NOT NULL);
        ''')
    prune()


def prune():
    cutoff = time.time() - RETENTION_DAYS * 86400
    with closing(connect()) as c, c:
        c.execute('DELETE FROM events WHERE ts < ?', (cutoff,))
        c.execute('DELETE FROM people WHERE last_seen < ?', (cutoff,))
        c.execute('DELETE FROM login_limits WHERE window < ?', (time.time()-3600,))


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


@app.on_event('shutdown')
def shutdown():
    _prune_stop.set()


def redact(value, depth=0):
    if depth > 5:
        raise ValueError('properties exceed five nested levels')
    if isinstance(value, dict):
        if len(value) > 64:
            raise ValueError('maximum 64 properties per object')
        return {k: '[redacted]' if SENSITIVE.search(k) else redact(v,depth+1) for k,v in value.items() if len(k)<=100}
    if isinstance(value,list):
        if len(value)>100:raise ValueError('maximum 100 array entries')
        return [redact(v,depth+1) for v in value]
    if isinstance(value,str):
        # Remove recognizable bearer/API credentials even when placed in innocent fields.
        return re.sub(r'(?i)\bBearer\s+[^\s]+|\b(?:mbb_inv_|na_write_|na_admin_|sk-)[A-Za-z0-9_-]+','[redacted]',value[:2000])
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
    properties: dict[str,Any] = Field(default_factory=dict)
    attributes: dict[str,Any] = Field(default_factory=dict)

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


class Query(Model):
    days: int = Field(default=7,ge=1,le=90)
    event: str | None = Field(default=None,max_length=160)
    distinct_id: str | None = Field(default=None,max_length=160)
    property: str | None = Field(default=None,pattern=PROPERTY)
    value: str | None = Field(default=None,max_length=2000)
    metric: Literal['count','users','sum','average','p95'] = 'count'
    measure: str | None = Field(default=None,pattern=PROPERTY)
    breakdown: str | None = Field(default=None,pattern=PROPERTY)
    interval: Literal['hour','day'] = 'day'


class Chart(Model):
    title: str = Field(min_length=1,max_length=100)
    query: Query


def credential(request):
    header=request.headers.get('authorization','')
    return header[7:] if header.startswith('Bearer ') else ''


def session(value):
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
def dashboard():return FileResponse(BASE/'static/index.html')


@app.get('/app.js')
def javascript():return FileResponse(BASE/'static/app.js',media_type='application/javascript')


@app.get('/style.css')
def stylesheet():return FileResponse(BASE/'static/style.css',media_type='text/css')


@app.post('/auth/login')
async def login(request:Request,response:Response):
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


@app.post('/auth/logout',dependencies=[Depends(admin)])
def logout(response:Response):
    response.delete_cookie('na_session')
    return {'ok':True}


@app.get('/api/projects',dependencies=[Depends(admin)])
def projects():
    with closing(connect()) as c:
        rows=c.execute('''SELECT p.id,p.name,p.created,(SELECT count(*) FROM events e WHERE e.project=p.id) AS stored_events,
                         (SELECT max(received) FROM events e WHERE e.project=p.id) AS last_ingest FROM projects p ORDER BY p.created''').fetchall()
    return {'projects':[dict(r) for r in rows],'retention_days':RETENTION_DAYS}


@app.post('/api/projects',dependencies=[Depends(admin)])
def create_project(req:Project):
    key='na_write_'+secrets.token_urlsafe(32)
    try:
        with closing(connect()) as c,c:c.execute('INSERT INTO projects VALUES(?,?,?,?)',(req.id,req.name,hashlib.sha256(key.encode()).hexdigest(),time.time()))
    except sqlite3.IntegrityError:raise HTTPException(409,'project already exists') from None
    return {'id':req.id,'name':req.name,'write_key':key,'note':'Write-only key; shown once. Keep it server-side.'}


def identify_person(c,project,distinct_id,attributes,now):
    if not distinct_id:return
    old=c.execute('SELECT attributes FROM people WHERE project=? AND distinct_id=?',(project,distinct_id)).fetchone()
    attrs=json.loads(old['attributes']) if old else {}
    attrs.update(attributes)
    attrs=Event.safe_json(attrs)
    c.execute('''INSERT INTO people VALUES(?,?,?,?,?) ON CONFLICT(project,distinct_id)
               DO UPDATE SET attributes=excluded.attributes,last_seen=excluded.last_seen''',
              (project,distinct_id,json.dumps(attrs),now,now))


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
            for event,ts in records:
                changed=c.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)',
                    (project,event.id,event.name,event.distinct_id,ts,now,json.dumps(event.properties))).rowcount
                if changed:
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


def where(project,q):
    sql='project=? AND ts>=?';params=[project,time.time()-q.days*86400]
    for column,value in [('name',q.event),('distinct_id',q.distinct_id)]:
        if value is not None:sql+=' AND '+column+'=?';params.append(value)
    if q.property and q.value is not None:
        sql+=' AND CAST(json_extract(properties,?) AS TEXT)=?';params.extend(['$.'+q.property,q.value])
    return sql,params


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
    clause,params=where(project,q)
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
    start=int((time.time()-q.days*86400)//step)*step;end=int(time.time()//step)*step
    series=[{'ts':b,'value':aggregate(buckets.get(b,[]),q)} for b in range(start,end+1,step)]
    breakdown=sorted([{'label':k,'value':aggregate(v,q)} for k,v in groups.items()],key=lambda x:x['value'] if x['value'] is not None else -1,reverse=True)[:20]
    return {'total':aggregate(rows,q),'matched_events':len(rows),'series':series,'breakdown':breakdown,'query':q.model_dump(),'timezone':'UTC'}


@app.post('/api/query',dependencies=[Depends(admin)])
def query(req:Query,project:str):
    project_exists(project)
    if req.metric not in {'count','users'} and not req.measure:raise HTTPException(422,'numeric metric needs measure')
    return query_data(project,req)


@app.get('/api/overview',dependencies=[Depends(admin)])
def overview(project:str,days:int=7):
    project_exists(project)
    q=Query(days=days);clause,params=where(project,q)
    with closing(connect()) as c:
        totals=dict(c.execute('''SELECT count(*) AS total_events,count(DISTINCT distinct_id) AS active_users,
          sum(CASE WHEN name='http.request' THEN 1 ELSE 0 END) AS requests,
          sum(CASE WHEN name='http.request' AND json_extract(properties,'$.status')>=400 THEN 1 ELSE 0 END) AS errors,
          sum(CASE WHEN name='http.request' AND json_extract(properties,'$.status')>=500 THEN 1 ELSE 0 END) AS server_errors,
          count(DISTINCT CASE WHEN name='http.request' THEN json_extract(properties,'$.ip') END) AS unique_ips
          FROM events WHERE '''+clause,params).fetchone())
        latency=[r[0] for r in c.execute("SELECT json_extract(properties,'$.duration_ms') FROM events WHERE "+clause+" AND name='http.request' AND json_type(properties,'$.duration_ms') IN ('integer','real')",params)]
        names=[dict(r) for r in c.execute('SELECT name,count(*) AS count FROM events WHERE '+clause+' GROUP BY name ORDER BY count DESC LIMIT 30',params)]
        latest=c.execute('SELECT max(received) FROM events WHERE project=?',(project,)).fetchone()[0]
        pulse=c.execute("SELECT properties,received FROM events WHERE project=? AND name='analytics.pipeline' ORDER BY received DESC LIMIT 1",(project,)).fetchone()
    totals.update(p95_ms=percentile(latency),average_ms=sum(latency)/len(latency) if latency else None,last_ingest=latest,
                  error_rate=100*(totals['errors'] or 0)/(totals['requests'] or 1))
    series=query_data(project,Query(days=days,event='http.request',interval='hour' if days==1 else 'day',breakdown='operation'))
    return {'totals':totals,'traffic':series,'event_names':names,'pipeline':{**json.loads(pulse['properties']),'received':pulse['received']} if pulse else None,'retention_days':RETENTION_DAYS}


@app.get('/api/events',dependencies=[Depends(admin)])
def events(project:str,days:int=7,event:str|None=None,distinct_id:str|None=None,property:str|None=None,value:str|None=None,
           status:int|None=None,ip:str|None=None,operation:str|None=None,before:float|None=None,before_id:str|None=None):
    project_exists(project)
    q=Query(days=days,event=event,distinct_id=distinct_id,property=property,value=value)
    clause,params=where(project,q)
    for key,val in [('status',status),('ip',ip),('operation',operation)]:
        if val is not None:clause+=' AND json_extract(properties,?)=?';params.extend(['$.'+key,val])
    if before is not None:
        clause+=' AND (ts<? OR (ts=? AND id<?))';params.extend([before,before,before_id or ''])
    with closing(connect()) as c:rows=c.execute('SELECT * FROM events WHERE '+clause+' ORDER BY ts DESC,id DESC LIMIT 101',params).fetchall()
    items=[{**dict(r),'properties':json.loads(r['properties'])} for r in rows[:100]]
    return {'events':items,'has_more':len(rows)>100,'next_cursor':{'before':items[-1]['ts'],'before_id':items[-1]['id']} if len(rows)>100 else None}


@app.get('/api/people',dependencies=[Depends(admin)])
def people(project:str,search:str='',offset:int=0):
    project_exists(project)
    if offset<0 or offset>100000:raise HTTPException(422,'invalid offset')
    with closing(connect()) as c:
        rows=c.execute('SELECT * FROM people WHERE project=? AND (distinct_id LIKE ? OR attributes LIKE ?) ORDER BY last_seen DESC LIMIT 101 OFFSET ?',
                       (project,'%'+search[:160]+'%','%'+search[:160]+'%',offset)).fetchall()
    return {'people':[{**dict(r),'attributes':json.loads(r['attributes'])} for r in rows[:100]],'has_more':len(rows)>100}


@app.patch('/api/people',dependencies=[Depends(admin)])
def tag_person(req:Identify,project:str):
    project_exists(project)
    with closing(connect()) as c,c:identify_person(c,project,req.distinct_id,req.attributes,time.time())
    return {'ok':True}


@app.get('/api/charts',dependencies=[Depends(admin)])
def charts(project:str):
    project_exists(project)
    with closing(connect()) as c:rows=c.execute('SELECT * FROM charts WHERE project=? ORDER BY created',(project,)).fetchall()
    return {'charts':[{**dict(r),'definition':json.loads(r['definition'])} for r in rows]}


@app.post('/api/charts',dependencies=[Depends(admin)])
def save_chart(req:Chart,project:str):
    project_exists(project)
    if req.query.metric not in {'count','users'} and not req.query.measure:raise HTTPException(422,'numeric metric needs measure')
    with closing(connect()) as c,c:
        if c.execute('SELECT count(*) FROM charts WHERE project=?',(project,)).fetchone()[0]>=30:raise HTTPException(422,'maximum 30 saved charts per project')
        cid=str(uuid.uuid4());c.execute('INSERT INTO charts VALUES(?,?,?,?,?)',(cid,project,req.title,req.query.model_dump_json(),time.time()))
    return {'id':cid}


@app.delete('/api/charts/{chart_id}',dependencies=[Depends(admin)])
def delete_chart(chart_id:str,project:str):
    with closing(connect()) as c,c:c.execute('DELETE FROM charts WHERE id=? AND project=?',(chart_id,project))
    return {'ok':True}


@app.post('/api/maintenance',dependencies=[Depends(admin)])
def maintenance():
    prune()
    return {'ok':True,'retention_days':RETENTION_DAYS}
