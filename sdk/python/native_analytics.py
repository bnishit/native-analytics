"""Dependency-free durable collector + ASGI request tracking. Copy into any Python service."""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.request
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

log=logging.getLogger('native_analytics')


def safe_properties(value,depth=0):
    if depth>5:raise ValueError('properties nested too deeply')
    if isinstance(value,dict):
        if len(value)>64:raise ValueError('too many properties')
        return {k:'[redacted]' if re.search(r'password|secret|token|authorization|cookie|otp|api.?key|request.?body|response.?body',k,re.I)
                else safe_properties(v,depth+1) for k,v in value.items() if isinstance(k,str) and len(k)<=100}
    if isinstance(value,list):
        if len(value)>100:raise ValueError('too many list items')
        return [safe_properties(v,depth+1) for v in value]
    if isinstance(value,str):return re.sub(r'(?i)\bBearer\s+[^\s]+|\b(?:mbb_inv_|na_write_|na_admin_|sk-)[A-Za-z0-9_-]+','[redacted]',value[:2000])
    return value


class Collector:
    def __init__(self,url,key,path,max_queue=100000):
        if not url.startswith('https://') and not url.startswith('http://127.0.0.1:'):
            raise ValueError('analytics ingestion requires HTTPS')
        self.url=url.rstrip('/')+'/v1/events';self.key=key;self.path=Path(path);self.max_queue=max_queue
        self.stop=threading.Event();self.thread=None;self.last_pulse=0
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.db()) as c,c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,payload TEXT NOT NULL,created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS delivery(id INTEGER PRIMARY KEY CHECK(id=1),last_delivery REAL,dropped INTEGER NOT NULL DEFAULT 0,last_error TEXT);
              INSERT OR IGNORE INTO delivery(id,dropped) VALUES(1,0);''')

    def db(self):
        return sqlite3.connect(self.path,timeout=1)

    def track(self,name,distinct_id=None,properties=None,attributes=None):
        eid=str(uuid.uuid4())
        try:
            for value in [name]+([distinct_id] if distinct_id is not None else []):
                if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,159}',value):
                    raise ValueError('invalid event or identity name')
            properties=safe_properties(properties or {});attributes=safe_properties(attributes or {})
            if not isinstance(properties,dict) or not isinstance(attributes,dict):raise ValueError('properties must be objects')
            if max(len(json.dumps(properties)),len(json.dumps(attributes)))>16000:raise ValueError('properties exceed 16 KB')
            event={'id':eid,'name':name,'distinct_id':distinct_id,'timestamp':datetime.now(timezone.utc).isoformat(),
                   'properties':properties,'attributes':attributes}
            with closing(self.db()) as c,c:
                if c.execute('SELECT count(*) FROM outbox').fetchone()[0]>=self.max_queue:
                    c.execute('UPDATE delivery SET dropped=dropped+1,last_error=? WHERE id=1',('queue capacity reached',))
                    log.error('analytics queue full; event dropped')
                    return None
                c.execute('INSERT INTO outbox VALUES(?,?,?)',(eid,json.dumps(event,allow_nan=False),time.time()))
            return eid
        except Exception:
            log.exception('analytics local enqueue failed; application request continues')
            return None

    def identify(self,distinct_id,attributes):
        return self.track('user.identified',distinct_id,attributes=attributes)

    def status(self):
        with closing(self.db()) as c:
            count,oldest=c.execute('SELECT count(*),min(created) FROM outbox').fetchone()
            last,dropped,error=c.execute('SELECT last_delivery,dropped,last_error FROM delivery WHERE id=1').fetchone()
        return {'queued':count,'oldest_queued_at':oldest,'last_delivery':last,'dropped':dropped,'last_error':error}

    def flush(self):
        with closing(self.db()) as c:rows=c.execute('SELECT id,payload FROM outbox ORDER BY created LIMIT 100').fetchall()
        if not rows:return 0
        body=json.dumps({'events':[json.loads(row[1]) for row in rows]}).encode()
        req=urllib.request.Request(self.url,data=body,headers={'Content-Type':'application/json','Authorization':'Bearer '+self.key})
        try:
            # Never redirect the write credential to another host.
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self,*args):return None
            with urllib.request.build_opener(NoRedirect).open(req,timeout=5) as r:
                result=json.load(r)
                if r.status!=200 or result.get('accepted',0)+result.get('duplicates',0)!=len(rows):
                    raise ValueError('incomplete ingestion receipt')
            with closing(self.db()) as c,c:
                c.executemany('DELETE FROM outbox WHERE id=?',[(r[0],) for r in rows])
                c.execute('UPDATE delivery SET last_delivery=?,last_error=NULL WHERE id=1',(time.time(),))
            return len(rows)
        except Exception as exc:
            # Do not log URLs, keys or provider bodies on network failure.
            with closing(self.db()) as c,c:c.execute('UPDATE delivery SET last_error=? WHERE id=1',(type(exc).__name__,))
            return 0

    def work(self):
        while not self.stop.is_set():
            try:
                if time.time()-self.last_pulse>=60:
                    self.track('analytics.pipeline',properties=self.status());self.last_pulse=time.time()
                delivered=self.flush()
            except Exception:
                delivered=0;log.exception('analytics worker failed; queued events retained')
            self.stop.wait(.2 if delivered else 3)

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.thread=threading.Thread(target=self.work,name='analytics-outbox',daemon=True);self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=6)


def ip_address(value):
    try:return str(ipaddress.ip_address(value.strip()))
    except (ValueError,AttributeError):return None


def origin_only(value):
    try:
        parts=urlsplit(value)
        return (parts.scheme+'://'+parts.hostname)[:250] if parts.scheme in {'http','https'} and parts.hostname else None
    except ValueError:return None


def route_path(scope):
    route=scope.get('route')
    if route and getattr(route,'path',None):return route.path
    # Rejected before route matching: keep only known non-customer path components.
    allowed={'support','v1','invoke','catalog','plans','execute','cancel','invoices','subscriptions','support-api','docs',
             'healthz','run','audit','commands','login','auth','start','complete','telemetry','summary','disputes','v1','analytics','status'}
    return '/'+ '/'.join(p if p in allowed else ':redacted' for p in scope.get('path','/').split('/') if p)[:250]


class RequestAnalytics:
    def __init__(self,app,collector,credentials=None,trust_railway=False,source='application'):
        self.app=app;self.collector=collector;self.credentials=credentials or {};self.trust_railway=trust_railway
        self.source=source

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        started=time.perf_counter();status=500;incoming=0;outgoing=0;body=bytearray();response_headers={};error=None
        request_id=str(uuid.uuid4())
        headers={k.decode('latin1').lower():v.decode('latin1') for k,v in scope.get('headers',[])}
        principal=None
        for label,secret in self.credentials.items():
            if secret and (hmac.compare_digest(headers.get('authorization','').encode(),('Bearer '+secret).encode()) or
                           hmac.compare_digest(headers.get('x-broker-key','').encode(),secret.encode())):
                principal=label;break
        async def capture_receive():
            nonlocal incoming
            message=await receive()
            if message['type']=='http.request':
                chunk=message.get('body',b'');incoming+=len(chunk)
                if len(body)<16384:body.extend(chunk[:16384-len(body)])
            return message
        async def capture_send(message):
            nonlocal status,outgoing,response_headers
            if message['type']=='http.response.start':
                status=message['status'];response_headers={k.decode('latin1').lower():v.decode('latin1') for k,v in message.get('headers',[])}
                if 'x-request-id' not in response_headers:
                    message={**message,'headers':[*message.get('headers',[]),(b'x-request-id',request_id.encode())]}
            elif message['type']=='http.response.body':outgoing+=len(message.get('body',b''))
            await send(message)
        try:
            await self.app(scope,capture_receive,capture_send)
        except BaseException as exc:
            error=type(exc).__name__;raise
        finally:
            peer=ip_address(scope.get('client',('unknown',0))[0]);ip=peer;source='asgi_peer'
            if self.trust_railway:
                forwarded=ip_address(headers.get('x-real-ip',''))
                if forwarded:ip,source=forwarded,'railway_x_real_ip'
            content={}
            if incoming<=16384 and 'application/json' in headers.get('content-type',''):
                try:
                    parsed=json.loads(body)
                    if isinstance(parsed,dict):content=parsed
                except (ValueError,UnicodeDecodeError):pass
            operation=scope.get('analytics.operation') or content.get('operation') or content.get('command')
            if not isinstance(operation,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_. -]{0,99}',operation):operation=None
            properties={'request_id':response_headers.get('x-request-id',request_id),'method':scope.get('method'),'path':route_path(scope),'status':status,'duration_ms':round((time.perf_counter()-started)*1000,2),
                        'ip':ip,'ip_source':source,'peer_ip':peer,'operation':operation,'environment':scope.get('analytics.environment') or (content.get('environment',content.get('env')) if content.get('environment',content.get('env')) in {'prod','staging'} else None),
                        'source':self.source,'deployment_id':os.getenv('RAILWAY_DEPLOYMENT_ID'),
                        'principal':principal or 'unauthenticated','authenticated_credential':bool(principal),'request_bytes_read':incoming,
                        'action_state':scope.get('analytics.action_state'),
                        'response_bytes':outgoing,'http_version':scope.get('http_version'),'scheme':scope.get('scheme'),
                        'user_agent':headers.get('user-agent','')[:500],'origin':origin_only(headers.get('origin','')),
                        'referrer_origin':origin_only(headers.get('referer','')),'content_type':headers.get('content-type','')[:100],
                        'edge':headers.get('x-railway-edge','')[:100] if self.trust_railway else None,
                        'platform_request_id':response_headers.get('x-request-id') or headers.get('x-railway-request-id','')[:100] if self.trust_railway else None,
                        'error_type':error,'is_healthcheck':scope.get('path')=='/healthz'}
            # No query values, arbitrary headers, cookies, request/response payloads, actor claims or customer IDs.
            await asyncio.to_thread(self.collector.track,'http.request',principal,properties,
                                    {'source':self.source,'kind':'integration'} if principal else {})


def install(app,db_path,credentials=None):
    url=os.getenv('NATIVE_ANALYTICS_URL');key=os.getenv('NATIVE_ANALYTICS_WRITE_KEY')
    if not url or not key:return None
    collector=Collector(url,key,Path(db_path).with_name('analytics-outbox.db'))
    app.add_middleware(RequestAnalytics,collector=collector,credentials=credentials,
                       trust_railway=os.getenv('NATIVE_ANALYTICS_TRUST_RAILWAY')=='true',
                       source=os.getenv('NATIVE_ANALYTICS_SOURCE','application'))
    app.add_event_handler('startup',collector.start)
    app.add_event_handler('shutdown',collector.close)
    return collector
