"""Email accounts, workspace boundaries, delegated keys and transactional membership rules."""
import hashlib
import http.client
import json
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import threading
import time
import uuid
from contextlib import closing
from email.message import EmailMessage
from pathlib import Path
from typing import Literal
from urllib.parse import quote,urlencode,urlsplit

from fastapi import Depends,HTTPException,Request,Response
from pydantic import BaseModel,ConfigDict,Field,field_validator

ROLES={'reader':1,'editor':2,'owner':3}
Role=Literal['reader','editor','owner']

def digest(value):return hashlib.sha256(value.encode()).hexdigest()
def uid(prefix):return prefix+uuid.uuid4().hex

class Model(BaseModel):
    model_config=ConfigDict(extra='forbid')
class EmailInput(Model):
    email:str=Field(max_length=254)
    @field_validator('email')
    @classmethod
    def email_address(cls,value):
        value=value.strip().lower()
        if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}",value):
            raise ValueError('provide an email address')
        return value
class SignIn(EmailInput):
    plan:Literal['free','builder','team']|None=None
    invite:str|None=Field(default=None,max_length=160)
class Token(Model):
    token:str=Field(min_length=20,max_length=160)
class DeviceApproval(Model):
    code:str=Field(pattern=r'^[A-F0-9]{12}$')
    approve:bool

class WorkspaceInput(Model):
    name:str=Field(min_length=1,max_length=80)
    @field_validator('name')
    @classmethod
    def name_value(cls,value):
        value=value.strip()
        if not value or any(ord(c)<32 for c in value):raise ValueError('provide a name without control characters')
        return value
class InviteInput(EmailInput):
    role:Role='reader'
class RoleInput(Model):
    role:Role
class AgentGrant(RoleInput):
    role:Role='reader'
    label:str=Field(min_length=1,max_length=80)
    expires_days:int=Field(default=30,ge=1,le=90)

class Accounts:
    def __init__(self,connect):
        self.connect=connect;self.delivery_lock=threading.Lock()

    def init_db(self,c):
        c.executescript('''
        CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY,name TEXT NOT NULL,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS memberships(workspace TEXT NOT NULL REFERENCES workspaces(id),
          user_id TEXT NOT NULL REFERENCES accounts(id),role TEXT NOT NULL CHECK(role IN ('reader','editor','owner')),
          created REAL NOT NULL,PRIMARY KEY(workspace,user_id));
        CREATE INDEX IF NOT EXISTS membership_user ON memberships(user_id,workspace);
        CREATE TABLE IF NOT EXISTS account_sessions(key_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES accounts(id),
          created REAL NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS cli_authorizations(device_hash TEXT PRIMARY KEY,code_hash TEXT UNIQUE NOT NULL,
          created REAL NOT NULL,expires REAL NOT NULL,last_poll REAL NOT NULL DEFAULT 0,
          source_session TEXT,state TEXT NOT NULL DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS sign_in_links(key_hash TEXT PRIMARY KEY,email TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS workspace_invites(id TEXT PRIMARY KEY,workspace TEXT NOT NULL REFERENCES workspaces(id),
          email TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('reader','editor','owner')),key_hash TEXT UNIQUE NOT NULL,
          created REAL NOT NULL,expires REAL NOT NULL,used REAL,revoked REAL,invited_by TEXT NOT NULL REFERENCES accounts(id));
        CREATE INDEX IF NOT EXISTS invite_workspace ON workspace_invites(workspace,created);
        CREATE TABLE IF NOT EXISTS workspace_keys(id TEXT PRIMARY KEY,workspace TEXT NOT NULL REFERENCES workspaces(id),
          user_id TEXT NOT NULL REFERENCES accounts(id),label TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('reader','editor','owner')),
          key_hash TEXT UNIQUE NOT NULL,created REAL NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS access_audit(id TEXT PRIMARY KEY,workspace TEXT NOT NULL REFERENCES workspaces(id),
          actor TEXT NOT NULL,action TEXT NOT NULL,target TEXT NOT NULL,role TEXT,created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS audit_workspace ON access_audit(workspace,created);
        CREATE TABLE IF NOT EXISTS account_mail(id TEXT PRIMARY KEY,recipient TEXT NOT NULL,subject TEXT NOT NULL,body TEXT,
          created REAL NOT NULL,expires REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL,
          state TEXT NOT NULL DEFAULT 'queued',workspace TEXT REFERENCES workspaces(id),invite_id TEXT REFERENCES workspace_invites(id));
        ''')
        c.execute("INSERT OR IGNORE INTO workspaces VALUES('legacy','Existing projects',?)",(time.time(),))
        columns={row['name'] for row in c.execute('PRAGMA table_info(projects)')}
        if 'workspace' not in columns:c.execute("ALTER TABLE projects ADD COLUMN workspace TEXT REFERENCES workspaces(id)")
        c.execute("UPDATE projects SET workspace='legacy' WHERE workspace IS NULL")
        c.executescript('''CREATE TRIGGER IF NOT EXISTS project_workspace_insert BEFORE INSERT ON projects WHEN NEW.workspace IS NULL BEGIN SELECT RAISE(ABORT,'workspace required'); END;
        CREATE TRIGGER IF NOT EXISTS project_workspace_update BEFORE UPDATE OF workspace ON projects WHEN NEW.workspace IS NULL BEGIN SELECT RAISE(ABORT,'workspace required'); END;''')
        if 'slug' not in columns:
            c.execute('ALTER TABLE projects ADD COLUMN slug TEXT')
            c.execute('UPDATE projects SET slug=id WHERE slug IS NULL')
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS workspace_project_slug ON projects(workspace,slug)')
        if 'purpose' not in {row['name'] for row in c.execute('PRAGMA table_info(read_keys)')}:c.execute("ALTER TABLE read_keys ADD COLUMN purpose TEXT NOT NULL DEFAULT 'analytics'")
        if 'issuer' not in {row['name'] for row in c.execute('PRAGMA table_info(read_keys)')}:c.execute('ALTER TABLE read_keys ADD COLUMN issuer TEXT REFERENCES accounts(id)')

    def csrf(self,request):
        origin=request.headers.get('origin')
        if origin and origin.rstrip('/')!=os.environ.get('ANALYTICS_PUBLIC_URL','').rstrip('/'):
            raise HTTPException(403,'invalid origin')
        if request.headers.get('x-native-csrf')!='1':raise HTTPException(403,'missing CSRF header')

    def principal(self,request):
        header=request.headers.get('authorization','')
        token=header[7:] if header.startswith('Bearer ') else ''
        if token:
            if not token.startswith('as_workspace_'):raise HTTPException(401,'account sign-in or workspace key required')
            with closing(self.connect()) as c:
                row=c.execute('SELECT k.id,k.user_id,k.workspace,k.role,k.expires,a.email,m.role AS current_role FROM workspace_keys k JOIN accounts a ON a.id=k.user_id JOIN memberships m ON m.user_id=k.user_id AND m.workspace=k.workspace WHERE k.key_hash=? AND k.expires>?',(digest(token),time.time())).fetchone()
            if not row:raise HTTPException(401,'workspace key expired, revoked or its issuer is no longer a member')
            return {'id':row['user_id'],'email':row['email'],'workspace':row['workspace'],'role':min((row['role'],row['current_role']),key=ROLES.get),'key_id':row['id']}
        token=request.cookies.get('as_session','')
        with closing(self.connect()) as c:
            row=c.execute('SELECT a.id,a.email FROM account_sessions s JOIN accounts a ON a.id=s.user_id WHERE s.key_hash=? AND s.expires>?',(digest(token),time.time())).fetchone() if token else None
        if not row:raise HTTPException(401,'sign in to your account')
        if request.method not in {'GET','HEAD','OPTIONS'}:self.csrf(request)
        return {**dict(row),'workspace':None,'role':None,'key_id':None}

    def session_user(self,request):
        p=self.principal(request)
        if p['key_id']:raise HTTPException(403,'this account action requires a browser session')
        return p

    def workspace_role(self,request,workspace,minimum='reader',connection=None):
        p=self.principal(request)
        if p['workspace'] is not None:
            role=p['role'] if p['workspace']==workspace else None
        else:
            if connection is None:
                with closing(self.connect()) as c:row=c.execute('SELECT role FROM memberships WHERE workspace=? AND user_id=?',(workspace,p['id'])).fetchone()
            else:row=connection.execute('SELECT role FROM memberships WHERE workspace=? AND user_id=?',(workspace,p['id'])).fetchone()
            role=row['role'] if row else None
        if role is None:raise HTTPException(403,'workspace access denied')
        if ROLES[role]<ROLES[minimum]:raise HTTPException(403,minimum+' role required')
        return {**p,'role':role}

    def project_role(self,request,project,minimum='reader'):
        p=self.principal(request)
        with closing(self.connect()) as c:
            row=c.execute('SELECT workspace FROM projects WHERE id=?',(project,)).fetchone()
        if not row:raise HTTPException(403,'project access denied')
        return self.workspace_role(request,row['workspace'],minimum)

    def workspaces(self,p):
        with closing(self.connect()) as c:
            rows=c.execute('SELECT w.*,m.role FROM workspaces w JOIN memberships m ON m.workspace=w.id WHERE m.user_id=? ORDER BY w.created,w.id',(p['id'],)).fetchall()
        return [{**dict(row),'role':p['role'] if p['workspace'] is not None else row['role']} for row in rows if p['workspace'] is None or row['id']==p['workspace']]

    def audit(self,c,workspace,p,action,target,role=None):
        c.execute('INSERT INTO access_audit VALUES(?,?,?,?,?,?,?)',(uid('a_'),workspace,p.get('key_id') or p['id'],action,target,role,time.time()))

    def origin(self):
        origin=os.environ.get('ANALYTICS_PUBLIC_URL','').rstrip('/');parsed=urlsplit(origin)
        local=parsed.scheme=='http' and parsed.hostname=='127.0.0.1' and os.environ.get('COOKIE_SECURE')=='false'
        if not (parsed.scheme=='https' and parsed.hostname or local) or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise HTTPException(503,'configure the service public origin')
        if not local and os.environ.get('COOKIE_SECURE','true')!='true':raise HTTPException(503,'hosted account sessions require secure cookies')
        return origin,local

    def mail_ready(self):
        _,local=self.origin();mode=os.environ.get('AFTERSPAN_MAIL_MODE','disabled')
        if mode=='local' and local and os.environ.get('AFTERSPAN_MAIL_DIR'):return
        if mode=='agentmail' and os.environ.get('AFTERSPAN_AGENTMAIL_API_KEY') and os.environ.get('AFTERSPAN_MAIL_FROM'):
            try:EmailInput(email=os.environ['AFTERSPAN_MAIL_FROM'])
            except ValueError:raise HTTPException(503,'configure a valid AgentMail inbox sender') from None
            return
        if mode=='smtp' and os.environ.get('AFTERSPAN_SMTP_HOST') and os.environ.get('AFTERSPAN_MAIL_FROM'):
            try:
                EmailInput(email=os.environ['AFTERSPAN_MAIL_FROM']);port=int(os.environ.get('AFTERSPAN_SMTP_PORT','587'))
                if not 1<=port<=65535:raise ValueError('invalid port')
            except ValueError:raise HTTPException(503,'configure a valid mail sender and SMTP port') from None
            return
        raise HTTPException(503,'email delivery is not configured; contact the service operator')

    def queue_mail(self,c,recipient,subject,body,expires,workspace=None,invite_id=None):
        self.mail_ready()
        if c.execute("SELECT count(*) FROM account_mail WHERE state IN ('queued','sending')").fetchone()[0]>=200:
            raise HTTPException(429,'email queue is busy; try later')
        mid=uid('mail_');now=time.time()
        c.execute('INSERT INTO account_mail(id,recipient,subject,body,created,expires,next_attempt,workspace,invite_id) VALUES(?,?,?,?,?,?,?,?,?)',(mid,recipient,subject,body,now,expires,now,workspace,invite_id))
        return mid

    def deliver_pending(self):
        if not self.delivery_lock.acquire(blocking=False):return
        try:
            with closing(self.connect()) as c,c:
                c.execute("UPDATE account_mail SET state='expired',body=NULL WHERE expires<=? AND body IS NOT NULL",(time.time(),))
                rows=c.execute("SELECT * FROM account_mail WHERE state='queued' AND next_attempt<=? AND expires>? ORDER BY created LIMIT 5",(time.time(),time.time())).fetchall()
            for row in rows:
                try:
                    self.mail_ready();message=EmailMessage();message['To']=row['recipient'];message['Subject']=row['subject'];message['From']=os.environ.get('AFTERSPAN_MAIL_FROM','afterspan@localhost.test');message.set_content(row['body'])
                    if os.environ.get('AFTERSPAN_MAIL_MODE')=='local':
                        directory=Path(os.environ['AFTERSPAN_MAIL_DIR']);directory.mkdir(mode=0o700,parents=True,exist_ok=True)
                        path=directory/(row['id']+'.eml')
                        with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w') as handle:handle.write(message.as_string())
                    elif os.environ.get('AFTERSPAN_MAIL_MODE')=='agentmail':
                        client=http.client.HTTPSConnection('api.agentmail.to',timeout=10)
                        try:
                            payload=json.dumps({'to':[row['recipient']],'subject':row['subject'],'text':row['body']}).encode()
                            client.request('POST','/v0/inboxes/'+quote(message['From'],safe='')+'/messages/send',body=payload,
                                           headers={'Authorization':'Bearer '+os.environ['AFTERSPAN_AGENTMAIL_API_KEY'],
                                                    'Content-Type':'application/json','Idempotency-Key':'afterspan-mail-'+row['id']})
                            response=client.getresponse();raw=response.read(8193)
                            if not 200<=response.status<300 or len(raw)>8192:
                                raise RuntimeError('AgentMail did not accept the message')
                            receipt=json.loads(raw)
                            if not isinstance(receipt,dict) or not isinstance(receipt.get('message_id'),str) or not receipt['message_id']:
                                raise RuntimeError('AgentMail returned no message receipt')
                        finally:client.close()
                    else:
                        context=ssl.create_default_context();port=int(os.environ.get('AFTERSPAN_SMTP_PORT','587'));host=os.environ['AFTERSPAN_SMTP_HOST']
                        client=smtplib.SMTP_SSL(host,port,timeout=10,context=context) if port==465 else smtplib.SMTP(host,port,timeout=10)
                        with client:
                            if port!=465:client.starttls(context=context)
                            if os.environ.get('AFTERSPAN_SMTP_USER'):client.login(os.environ['AFTERSPAN_SMTP_USER'],os.environ['AFTERSPAN_SMTP_PASSWORD'])
                            client.send_message(message)
                    with closing(self.connect()) as c,c:c.execute("UPDATE account_mail SET state='sent',body=NULL WHERE id=?",(row['id'],))
                except Exception:
                    with closing(self.connect()) as c,c:c.execute("UPDATE account_mail SET attempts=attempts+1,next_attempt=?,state=CASE WHEN attempts>=4 THEN 'failed' ELSE 'queued' END WHERE id=?",(time.time()+min(300,5*2**row['attempts']),row['id']))
        finally:self.delivery_lock.release()

    def rate(self,c,request,email):
        now=time.time();ip=request.client.host if request.client else 'unknown'
        for key,limit in [('mail:email:'+digest(email),3),('mail:ip:'+digest(ip),20),('mail:global',200)]:
            row=c.execute('SELECT window,attempts FROM login_limits WHERE ip=?',(key,)).fetchone();window=row['window'] if row and now-row['window']<900 else now;count=row['attempts'] if window!=now else 0
            if count>=limit:raise HTTPException(429,'too many link requests; wait 15 minutes',headers={'Retry-After':'900'})
            c.execute('INSERT INTO login_limits VALUES(?,?,?) ON CONFLICT(ip) DO UPDATE SET window=excluded.window,attempts=excluded.attempts',(key,window,count+1))

    def create_session(self,c,user_id,now,expires):
        token='as_session_'+secrets.token_urlsafe(32)
        c.execute('DELETE FROM account_sessions WHERE expires<=?',(now,))
        # Keep at most ten active sessions per account, including terminal sessions.
        c.execute('DELETE FROM account_sessions WHERE key_hash IN (SELECT key_hash FROM account_sessions WHERE user_id=? ORDER BY created DESC LIMIT -1 OFFSET 9)',(user_id,))
        c.execute('INSERT INTO account_sessions VALUES(?,?,?,?)',(digest(token),user_id,now,expires))
        return token

    def install(self,app):
        @app.post('/auth/cli/start',status_code=201)
        def cli_start(request:Request,response:Response):
            self.csrf(request);origin,_=self.origin();now=time.time()
            device='as_device_'+secrets.token_urlsafe(32);code=secrets.token_hex(6).upper()
            with closing(self.connect()) as c,c:
                c.execute('BEGIN IMMEDIATE');self.rate(c,request,'cli:'+secrets.token_hex(16))
                c.execute('DELETE FROM cli_authorizations WHERE expires<=?',(now,))
                c.execute('INSERT INTO cli_authorizations(device_hash,code_hash,created,expires) VALUES(?,?,?,?)',
                          (digest(device),digest(code),now,now+600))
            response.headers['Cache-Control']='no-store'
            return {'device_code':device,'user_code':code,'verification_uri':origin+'/assets/cli-login.html',
                    'expires_in':600,'interval':3}

        @app.post('/auth/cli/approve')
        def cli_approve(req:DeviceApproval,request:Request):
            self.session_user(request);now=time.time()
            with closing(self.connect()) as c,c:
                c.execute('BEGIN IMMEDIATE');self.rate(c,request,'cli-approval:'+secrets.token_hex(16))
            with closing(self.connect()) as c,c:
                c.execute('BEGIN IMMEDIATE')
                row=c.execute('SELECT state FROM cli_authorizations WHERE code_hash=? AND expires>?',
                              (digest(req.code),now)).fetchone()
                if not row or row['state']!='pending':raise HTTPException(400,'request expired or already decided')
                c.execute('UPDATE cli_authorizations SET source_session=?,state=? WHERE code_hash=?',
                          (digest(request.cookies['as_session']),'approved' if req.approve else 'denied',digest(req.code)))
            return {'ok':True,'status':'approved' if req.approve else 'denied'}

        @app.post('/auth/cli/poll')
        def cli_poll(req:Token,request:Request,response:Response):
            self.csrf(request);self.origin();now=time.time();result=None
            with closing(self.connect()) as c,c:
                c.execute('BEGIN IMMEDIATE')
                row=c.execute('SELECT * FROM cli_authorizations WHERE device_hash=? AND expires>?',
                              (digest(req.token),now)).fetchone()
                if not row:raise HTTPException(400,'request expired or invalid')
                if now-row['last_poll']<3:
                    raise HTTPException(429,'poll no more than once every 3 seconds',headers={'Retry-After':'3'})
                c.execute('UPDATE cli_authorizations SET last_poll=? WHERE device_hash=?',(now,digest(req.token)))
                if row['state']=='approved':
                    source=c.execute('SELECT user_id,expires FROM account_sessions WHERE key_hash=? AND expires>?',
                                     (row['source_session'],now)).fetchone()
                    if source:
                        token=self.create_session(c,source['user_id'],now,source['expires'])
                        result={'status':'complete','session':token,'cookie_name':'as_session','expires':source['expires']}
                    else:result={'status':'denied'}
                    c.execute('DELETE FROM cli_authorizations WHERE device_hash=?',(digest(req.token),))
                else:result={'status':row['state']}
            response.headers['Cache-Control']='no-store'
            return result

        @app.get('/auth/config')
        def auth_config():
            try:self.mail_ready();ready=True
            except HTTPException:ready=False
            return {'email_sign_in':ready,'delivery_mode':os.environ.get('AFTERSPAN_MAIL_MODE','disabled'),'legacy_login':os.environ.get('AFTERSPAN_LEGACY_LOGIN')=='true'}

        @app.post('/auth/request-link',status_code=202)
        def request_link(req:SignIn,request:Request):
            self.csrf(request);self.mail_ready();now=time.time();token='as_login_'+secrets.token_urlsafe(32)
            with closing(self.connect()) as c,c:
                c.execute('BEGIN IMMEDIATE');self.rate(c,request,req.email)
                if req.invite:
                    invite=c.execute('SELECT email FROM workspace_invites WHERE key_hash=? AND used IS NULL AND revoked IS NULL AND expires>?',(digest(req.invite),now)).fetchone()
                    if not invite or invite['email']!=req.email:raise HTTPException(400,'invite is invalid, expired or belongs to a different email')
                c.execute('INSERT INTO sign_in_links VALUES(?,?,?,?)',(digest(token),req.email,now,now+900))
                link=self.origin()[0]+'/app#'+urlencode({'sign-in':token,**({'invite':req.invite} if req.invite else {}),**({'plan':req.plan} if req.plan else {})})
                self.queue_mail(c,req.email,'Sign in to Afterspan','Use this one-time link within 15 minutes:\n\n'+link+'\n\nIf you did not request this, ignore this email.',now+900)
            return {'status':'queued','message':'A sign-in link has been queued for this address. It expires in 15 minutes.'}

        @app.post('/auth/link-preview')
        def link_preview(req:Token,request:Request):
            self.csrf(request)
            with closing(self.connect()) as c:row=c.execute('SELECT email,expires FROM sign_in_links WHERE key_hash=? AND expires>?',(digest(req.token),time.time())).fetchone()
            if not row:raise HTTPException(400,'sign-in link is invalid, expired or already used')
            return dict(row)

        @app.post('/auth/consume-link')
        def consume(req:Token,request:Request,response:Response):
            self.csrf(request);self.origin();now=time.time()
            with closing(self.connect()) as c,c:
                c.execute('BEGIN IMMEDIATE')
                link=c.execute('SELECT email FROM sign_in_links WHERE key_hash=? AND expires>?',(digest(req.token),now)).fetchone()
                if not link:raise HTTPException(400,'sign-in link is invalid, expired or already used')
                c.execute('DELETE FROM sign_in_links WHERE key_hash=?',(digest(req.token),))
                user=c.execute('SELECT id FROM accounts WHERE email=?',(link['email'],)).fetchone();user_id=user['id'] if user else uid('u_')
                if not user:c.execute('INSERT INTO accounts VALUES(?,?,?)',(user_id,link['email'],now))
                session_token=self.create_session(c,user_id,now,now+43200)
            response.set_cookie('as_session',session_token,httponly=True,secure=os.environ.get('COOKIE_SECURE','true')=='true',samesite='strict',max_age=43200)
            response.delete_cookie('na_session')
            return {'ok':True,'user':{'id':user_id,'email':link['email']}}

        @app.get('/auth/me')
        def me(request:Request):
            p=self.principal(request)
            return {'user':{'id':p['id'],'email':p['email']},'workspaces':self.workspaces(p),'agent':p['key_id'] is not None}

        @app.post('/auth/logout-all')
        def logout_all(request:Request,response:Response):
            p=self.session_user(request)
            with closing(self.connect()) as c,c:
                c.execute('DELETE FROM account_sessions WHERE user_id=?',(p['id'],))
                c.execute('DELETE FROM workspace_keys WHERE user_id=?',(p['id'],))
                c.execute('DELETE FROM read_keys WHERE issuer=?',(p['id'],))
            response.delete_cookie('as_session')
            return {'ok':True,'note':'All browser sessions and workspace keys issued by this account were revoked.'}

        @app.get('/api/workspaces')
        def list_workspaces(request:Request):return {'workspaces':self.workspaces(self.principal(request))}

        @app.post('/api/workspaces',status_code=201)
        def create_workspace(req:WorkspaceInput,request:Request):
            p=self.session_user(request);wid=uid('w_');now=time.time()
            with closing(self.connect()) as c,c:
                c.execute('BEGIN IMMEDIATE')
                if c.execute('SELECT count(*) FROM memberships WHERE user_id=?',(p['id'],)).fetchone()[0]>=20:raise HTTPException(422,'maximum 20 workspaces per account')
                c.execute('INSERT INTO workspaces VALUES(?,?,?)',(wid,req.name,now));self.billing.ensure(c,wid);c.execute("INSERT INTO memberships VALUES(?,?,'owner',?)",(wid,p['id'],now));self.audit(c,wid,p,'workspace.created',wid)
            return {'id':wid,'name':req.name,'role':'owner','created':now}

        @app.patch('/api/workspaces/{workspace}')
        def rename_workspace(workspace:str,req:WorkspaceInput,request:Request):
            with closing(self.connect()) as c,c:
                c.execute('BEGIN IMMEDIATE');p=self.workspace_role(request,workspace,'owner',c)
                c.execute('UPDATE workspaces SET name=? WHERE id=?',(req.name,workspace));self.audit(c,workspace,p,'workspace.renamed',workspace)
            return {'id':workspace,'name':req.name}

        @app.get('/api/workspaces/{workspace}/members')
        def members(workspace:str,request:Request):
            self.workspace_role(request,workspace)
            with closing(self.connect()) as c:rows=c.execute('SELECT a.id,a.email,m.role,m.created FROM memberships m JOIN accounts a ON a.id=m.user_id WHERE m.workspace=? ORDER BY m.created,a.id',(workspace,)).fetchall()
            return {'members':[dict(r) for r in rows]}

        @app.patch('/api/workspaces/{workspace}/members/{user_id}')
        def change_member(workspace:str,user_id:str,req:RoleInput,request:Request):
            return self.mutate_member(request,workspace,user_id,req.role)

        @app.delete('/api/workspaces/{workspace}/members/{user_id}')
        def remove_member(workspace:str,user_id:str,request:Request):return self.mutate_member(request,workspace,user_id,None)

        @app.get('/api/workspaces/{workspace}/invites')
        def invites(workspace:str,request:Request):
            self.workspace_role(request,workspace,'owner')
            with closing(self.connect()) as c:rows=c.execute('SELECT id,email,role,created,expires,used,revoked FROM workspace_invites WHERE workspace=? ORDER BY created DESC LIMIT 100',(workspace,)).fetchall()
            return {'invites':[dict(row) for row in rows]}

        @app.post('/api/workspaces/{workspace}/invites',status_code=202)
        def invite(workspace:str,req:InviteInput,request:Request):
            self.mail_ready();now=time.time();iid=uid('i_');token='as_invite_'+secrets.token_urlsafe(32)
            with closing(self.connect()) as c,c:
                c.execute('BEGIN IMMEDIATE');p=self.workspace_role(request,workspace,'owner',c)
                if c.execute('SELECT 1 FROM memberships m JOIN accounts a ON a.id=m.user_id WHERE m.workspace=? AND a.email=?',(workspace,req.email)).fetchone():raise HTTPException(409,'this account is already a member')
                if c.execute('SELECT 1 FROM workspace_invites WHERE workspace=? AND email=? AND used IS NULL AND revoked IS NULL AND expires>?',(workspace,req.email,now)).fetchone():raise HTTPException(409,'a pending invite already exists; revoke it before replacing it')
                if c.execute('SELECT count(*) FROM workspace_invites WHERE workspace=? AND created>?',(workspace,now-3600)).fetchone()[0]>=100:raise HTTPException(429,'workspace invite limit reached; try later')
                if c.execute('SELECT count(*) FROM workspace_invites WHERE workspace=? AND used IS NULL AND revoked IS NULL AND expires>?',(workspace,now)).fetchone()[0]>=50:raise HTTPException(422,'maximum 50 pending invites')
                self.billing.capacity(c,workspace,'seats')
                name=c.execute('SELECT name FROM workspaces WHERE id=?',(workspace,)).fetchone()['name']
                c.execute('INSERT INTO workspace_invites VALUES(?,?,?,?,?,?,?,?,?,?)',(iid,workspace,req.email,req.role,digest(token),now,now+7*86400,None,None,p['id']))
                link=self.origin()[0]+'/app#'+urlencode({'invite':token})
                mid=self.queue_mail(c,req.email,'Your Afterspan workspace invite',f'You are invited to {name} as {req.role}.\n\nSign in with {req.email}, then accept this invite within 7 days:\n\n'+link,now+7*86400,workspace,iid)
                self.audit(c,workspace,p,'invite.created',iid,req.role)
            return {'id':iid,'email':req.email,'role':req.role,'expires':now+7*86400,'delivery':'queued','mail_id':mid}

        @app.post('/api/workspaces/{workspace}/invites/{invite_id}/resend',status_code=202)
        def resend(workspace:str,invite_id:str,request:Request):
            self.mail_ready();now=time.time();token='as_invite_'+secrets.token_urlsafe(32)
            with closing(self.connect()) as c,c:
                c.execute('BEGIN IMMEDIATE');p=self.workspace_role(request,workspace,'owner',c)
                invite=c.execute('SELECT i.email,i.role,w.name FROM workspace_invites i JOIN workspaces w ON w.id=i.workspace WHERE i.id=? AND i.workspace=? AND i.used IS NULL AND i.revoked IS NULL',(invite_id,workspace)).fetchone()
                if not invite:raise HTTPException(404,'pending invite not found')
                if c.execute('SELECT count(*) FROM workspace_invites WHERE workspace=? AND id!=? AND used IS NULL AND revoked IS NULL AND expires>?',(workspace,invite_id,now)).fetchone()[0]>=50:raise HTTPException(422,'maximum 50 pending invites')
                if c.execute('SELECT 1 FROM workspace_invites WHERE workspace=? AND email=? AND id!=? AND used IS NULL AND revoked IS NULL AND expires>?',(workspace,invite['email'],invite_id,now)).fetchone():raise HTTPException(409,'a newer pending invite already exists')
                if c.execute('SELECT count(*) FROM account_mail WHERE workspace=? AND created>?',(workspace,now-3600)).fetchone()[0]>=100:raise HTTPException(429,'workspace email limit reached; try later')
                self.billing.capacity(c,workspace,'seats',exclude_invite=invite_id)
                c.execute('UPDATE workspace_invites SET key_hash=?,expires=?,invited_by=? WHERE id=?',(digest(token),now+7*86400,p['id'],invite_id))
                c.execute("UPDATE account_mail SET state='cancelled',body=NULL WHERE invite_id=? AND state IN ('queued','failed')",(invite_id,))
                link=self.origin()[0]+'/app#'+urlencode({'invite':token})
                self.queue_mail(c,invite['email'],'Your Afterspan workspace invite',f"Your new invite to {invite['name']} as {invite['role']} is below. Earlier invite links no longer work. Sign in with {invite['email']}.\n\n"+link,now+7*86400,workspace,invite_id)
                self.audit(c,workspace,p,'invite.resent',invite_id,invite['role'])
            return {'id':invite_id,'delivery':'queued','expires':now+7*86400}

        @app.delete('/api/workspaces/{workspace}/invites/{invite_id}')
        def revoke_invite(workspace:str,invite_id:str,request:Request):
            with closing(self.connect()) as c,c:
                c.execute('BEGIN IMMEDIATE');p=self.workspace_role(request,workspace,'owner',c)
                changed=c.execute('UPDATE workspace_invites SET revoked=? WHERE id=? AND workspace=? AND used IS NULL AND revoked IS NULL',(time.time(),invite_id,workspace)).rowcount
                if not changed:raise HTTPException(404,'pending invite not found')
                c.execute("UPDATE account_mail SET state='cancelled',body=NULL WHERE invite_id=? AND state='queued'",(invite_id,))
                self.audit(c,workspace,p,'invite.revoked',invite_id)
            return {'revoked':True}

        @app.post('/api/invites/preview')
        def preview(req:Token,request:Request):
            p=self.session_user(request)
            with closing(self.connect()) as c:invite=self.valid_invite(c,req.token,p)
            return {k:invite[k] for k in ['workspace','name','email','role','expires']}

        @app.post('/api/invites/accept')
        def accept(req:Token,request:Request):
            p=self.session_user(request);now=time.time()
            with closing(self.connect()) as c,c:
                c.execute('BEGIN IMMEDIATE');invite=self.valid_invite(c,req.token,p)
                if c.execute('SELECT count(*) FROM memberships WHERE user_id=?',(p['id'],)).fetchone()[0]>=20:raise HTTPException(422,'maximum 20 workspaces per account')
                if not c.execute('SELECT 1 FROM memberships WHERE workspace=? AND user_id=?',(invite['workspace'],p['id'])).fetchone():self.billing.capacity(c,invite['workspace'],'seats',exclude_invite=invite['id'])
                c.execute('INSERT OR IGNORE INTO memberships VALUES(?,?,?,?)',(invite['workspace'],p['id'],invite['role'],now))
                c.execute('UPDATE workspace_invites SET used=? WHERE id=?',(now,invite['id']));self.audit(c,invite['workspace'],p,'invite.accepted',invite['id'],invite['role'])
            return {'workspace':invite['workspace'],'joined':True}

        @app.post('/api/workspaces/{workspace}/agent-keys',status_code=201)
        def create_key(workspace:str,req:AgentGrant,request:Request):
            p=self.session_user(request);token='as_workspace_'+secrets.token_urlsafe(32);kid=uid('k_');now=time.time()
            with closing(self.connect()) as c,c:
                c.execute('BEGIN IMMEDIATE');p=self.workspace_role(request,workspace,'owner',c)
                if c.execute('SELECT count(*) FROM workspace_keys WHERE workspace=? AND expires>?',(workspace,now)).fetchone()[0]>=20:raise HTTPException(422,'maximum 20 active workspace agent keys')
                c.execute('INSERT INTO workspace_keys VALUES(?,?,?,?,?,?,?,?)',(kid,workspace,p['id'],req.label,req.role,digest(token),now,now+req.expires_days*86400));self.audit(c,workspace,p,'agent_key.created',kid,req.role)
            return {'id':kid,'workspace':workspace,'label':req.label,'role':req.role,'key':token,'expires':now+req.expires_days*86400,'note':'Shown once. Effective role never exceeds the issuer membership. Keys cannot issue further keys or sign in as an account.'}

        @app.get('/api/workspaces/{workspace}/agent-keys')
        def keys(workspace:str,request:Request):
            self.workspace_role(request,workspace,'owner')
            with closing(self.connect()) as c:rows=c.execute('SELECT k.id,k.user_id,k.label,k.role,k.created,k.expires,m.role AS issuer_role FROM workspace_keys k LEFT JOIN memberships m ON m.workspace=k.workspace AND m.user_id=k.user_id WHERE k.workspace=? ORDER BY k.created DESC',(workspace,)).fetchall()
            return {'keys':[{**dict(r),'effective_role':min((r['role'],r['issuer_role']),key=ROLES.get) if r['issuer_role'] and r['expires']>time.time() else None} for r in rows]}

        @app.delete('/api/workspaces/{workspace}/agent-keys/{key_id}')
        def revoke_key(workspace:str,key_id:str,request:Request):
            with closing(self.connect()) as c,c:
                c.execute('BEGIN IMMEDIATE');p=self.workspace_role(request,workspace,'owner',c)
                changed=c.execute('DELETE FROM workspace_keys WHERE workspace=? AND id=?',(workspace,key_id)).rowcount
                if changed:self.audit(c,workspace,p,'agent_key.revoked',key_id)
            return {'revoked':bool(changed)}

        @app.get('/api/workspaces/{workspace}/audit')
        def audit_log(workspace:str,request:Request):
            self.workspace_role(request,workspace,'owner')
            with closing(self.connect()) as c:rows=c.execute("""SELECT l.*,COALESCE(a.email,p.name,w.name,i.email,k.label,CASE WHEN l.action LIKE 'agent_key.%' THEN 'Agent key' ELSE 'Former workspace member' END) AS target_label,COALESCE(actor.email,actor_key.label,CASE WHEN l.actor='operator' THEN 'Service operator' ELSE 'Former workspace agent' END) AS actor_label FROM access_audit l LEFT JOIN accounts a ON a.id=l.target LEFT JOIN projects p ON p.id=l.target AND p.workspace=l.workspace LEFT JOIN workspaces w ON w.id=l.target LEFT JOIN workspace_invites i ON i.id=l.target AND i.workspace=l.workspace LEFT JOIN workspace_keys k ON k.id=l.target AND k.workspace=l.workspace LEFT JOIN accounts actor ON actor.id=l.actor LEFT JOIN workspace_keys actor_key ON actor_key.id=l.actor AND actor_key.workspace=l.workspace WHERE l.workspace=? ORDER BY l.created DESC,l.id DESC LIMIT 100""",(workspace,)).fetchall()
            return {'entries':[dict(row) for row in rows]}

        @app.get('/api/workspaces/{workspace}/mail')
        def mail_status(workspace:str,request:Request):
            self.workspace_role(request,workspace,'owner')
            with closing(self.connect()) as c:rows=c.execute('SELECT id,invite_id,recipient,state,attempts,created,expires FROM account_mail WHERE workspace=? ORDER BY created DESC LIMIT 100',(workspace,)).fetchall()
            return {'messages':[dict(row) for row in rows],'delivery_mode':os.environ.get('AFTERSPAN_MAIL_MODE','disabled')}

    def valid_invite(self,c,token,p):
        invite=c.execute('SELECT i.*,w.name FROM workspace_invites i JOIN workspaces w ON w.id=i.workspace WHERE i.key_hash=? AND i.used IS NULL AND i.revoked IS NULL AND i.expires>?',(digest(token),time.time())).fetchone()
        if not invite or invite['email']!=p['email']:raise HTTPException(400,'invite is invalid, expired, used or belongs to another account')
        return invite

    def mutate_member(self,request,workspace,user_id,role):
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE');p=self.workspace_role(request,workspace,'owner',c)
            member=c.execute('SELECT role FROM memberships WHERE workspace=? AND user_id=?',(workspace,user_id)).fetchone()
            if not member:raise HTTPException(404,'member not found')
            if member['role']=='owner' and role!='owner' and c.execute("SELECT count(*) FROM memberships WHERE workspace=? AND role='owner'",(workspace,)).fetchone()[0]<=1:raise HTTPException(409,'keep at least one owner; promote another member first')
            if role is None:
                c.execute('DELETE FROM memberships WHERE workspace=? AND user_id=?',(workspace,user_id))
                c.execute('UPDATE oauth_grants SET revoked=1 WHERE user_id=? AND project IN (SELECT id FROM projects WHERE workspace=?)',(user_id,workspace))
                c.execute('DELETE FROM workspace_keys WHERE workspace=? AND user_id=?',(workspace,user_id))
                c.execute('DELETE FROM read_keys WHERE issuer=? AND project IN (SELECT id FROM projects WHERE workspace=?)',(user_id,workspace))
            else:c.execute('UPDATE memberships SET role=? WHERE workspace=? AND user_id=?',(role,workspace,user_id))
            if role!='owner':
                c.execute('UPDATE workspace_invites SET revoked=? WHERE workspace=? AND invited_by=? AND used IS NULL AND revoked IS NULL',(time.time(),workspace,user_id))
                c.execute("UPDATE account_mail SET state='cancelled',body=NULL WHERE invite_id IN (SELECT id FROM workspace_invites WHERE workspace=? AND invited_by=? AND revoked IS NOT NULL) AND state='queued'",(workspace,user_id))
            self.audit(c,workspace,p,'member.removed' if role is None else 'member.role_changed',user_id,role)
        return {'ok':True}
