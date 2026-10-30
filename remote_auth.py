"""Project-scoped OAuth grants. Protocol validation is owned by the official MCP SDK."""
import json
import re
import secrets
import time
from contextlib import closing
from urllib.parse import urlsplit

from accounts import ROLES,SESSION_SECONDS,digest
from fastapi import HTTPException,Request,Response
from fastapi.responses import JSONResponse
from mcp.server.auth.provider import AccessToken,AuthorizationCode,AuthorizeError,RegistrationError,TokenError,construct_redirect_uri
from mcp.shared.auth import OAuthClientInformationFull,OAuthToken
from pydantic import BaseModel,ConfigDict,Field

SCOPES=['analytics:read','charts:write','audit:read']

class Decision(BaseModel):
    model_config=ConfigDict(extra='forbid')
    request_id:str=Field(min_length=32,max_length=100)
    project:str=Field(default='',max_length=160)
    approve:bool

class ProjectToken(AccessToken):
    project:str
    grant_id:str
    workspace:str

class OAuthProvider:
    def __init__(self,connect,accounts,origin):
        self.connect=connect;self.accounts=accounts;self.origin=origin;self.resource=origin+'/mcp'

    @staticmethod
    def init_db(c):
        c.executescript('''
        CREATE TABLE IF NOT EXISTS oauth_clients(id TEXT PRIMARY KEY,definition TEXT NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS oauth_pending(id TEXT PRIMARY KEY,client TEXT NOT NULL,params TEXT NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS oauth_grants(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES accounts(id),
          project TEXT NOT NULL REFERENCES projects(id),client TEXT NOT NULL,scopes TEXT NOT NULL,expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS oauth_codes(hash TEXT PRIMARY KEY,grant_id TEXT NOT NULL REFERENCES oauth_grants(id),params TEXT NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS oauth_tokens(hash TEXT PRIMARY KEY,grant_id TEXT NOT NULL REFERENCES oauth_grants(id),kind TEXT NOT NULL,scopes TEXT NOT NULL,expires REAL NOT NULL,used INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS oauth_grant_tokens ON oauth_tokens(grant_id);
        ''')

    async def get_client(self,client_id):
        with closing(self.connect()) as c:r=c.execute('SELECT definition FROM oauth_clients WHERE id=? AND expires>?',(client_id,time.time())).fetchone()
        return OAuthClientInformationFull.model_validate_json(r['definition']) if r else None

    async def register_client(self,client_info):
        if client_info.token_endpoint_auth_method!='none':raise RegistrationError('invalid_client_metadata','Public clients with PKCE and token_endpoint_auth_method=none are supported.')
        for uri in client_info.redirect_uris or []:
            u=urlsplit(str(uri))
            if u.fragment or u.username or u.password or not (u.scheme=='https' or u.scheme=='http' and u.hostname in {'127.0.0.1','localhost','::1'}):
                raise RegistrationError('invalid_redirect_uri','Use HTTPS or a loopback callback without fragments or credentials.')
        if len(client_info.model_dump_json())>16000:raise RegistrationError('invalid_client_metadata')
        if len(client_info.redirect_uris or [])>10 or len(client_info.client_name or '')>100:raise RegistrationError('invalid_client_metadata')
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM oauth_clients WHERE expires<=?',(time.time(),))
            if c.execute('SELECT count(*) FROM oauth_clients').fetchone()[0]>=1000:raise RegistrationError('invalid_client_metadata','Client registration capacity reached.')
            c.execute('INSERT INTO oauth_clients VALUES(?,?,?)',(client_info.client_id,client_info.model_dump_json(),time.time()+SESSION_SECONDS))

    async def authorize(self,client,params):
        if params.resource not in {None,self.resource}:raise AuthorizeError('invalid_request','Incorrect resource.')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}',params.code_challenge):raise AuthorizeError('invalid_request','S256 PKCE is required.')
        params.scopes=params.scopes or ['analytics:read']
        if set(params.scopes)-set(SCOPES):raise AuthorizeError('invalid_scope')
        params.resource=self.resource;rid=secrets.token_urlsafe(32)
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE');c.execute('DELETE FROM oauth_pending WHERE expires<=?',(time.time(),))
            if c.execute('SELECT count(*) FROM oauth_pending').fetchone()[0]>=500:raise AuthorizeError('temporarily_unavailable')
            c.execute('INSERT INTO oauth_pending VALUES(?,?,?,?)',(rid,client.client_id,params.model_dump_json(),time.time()+600))
        return self.origin+'/assets/mcp-connect.html#request='+rid

    def grant(self,c,gid):
        r=c.execute('SELECT g.*,m.role,p.workspace FROM oauth_grants g JOIN projects p ON p.id=g.project JOIN memberships m ON m.workspace=p.workspace AND m.user_id=g.user_id WHERE g.id=? AND g.expires>? AND g.revoked=0',(gid,time.time())).fetchone()
        return r

    def token(self,raw,kind):
        with closing(self.connect()) as c:
            t=c.execute('SELECT * FROM oauth_tokens WHERE hash=? AND kind=? AND expires>? AND used=0',(digest(raw),kind,time.time())).fetchone()
            g=self.grant(c,t['grant_id']) if t else None
        if not g:return None
        scopes=json.loads(t['scopes'])
        if ROLES[g['role']]<ROLES['editor']:scopes=[s for s in scopes if s!='charts:write']
        return ProjectToken(token=raw,client_id=g['client'],scopes=scopes,expires_at=int(t['expires']),resource=self.resource,subject=g['user_id'],project=g['project'],grant_id=g['id'],workspace=g['workspace'])

    async def load_access_token(self,token):return self.token(token,'access')

    async def load_authorization_code(self,client,authorization_code):
        with closing(self.connect()) as c:
            r=c.execute('SELECT * FROM oauth_codes WHERE hash=? AND expires>?',(digest(authorization_code),time.time())).fetchone()
            g=self.grant(c,r['grant_id']) if r else None
        if not g or g['client']!=client.client_id:return None
        p=json.loads(r['params'])
        return AuthorizationCode(code=authorization_code,client_id=g['client'],scopes=json.loads(g['scopes']),expires_at=r['expires'],subject=g['id'],**{k:p[k] for k in ['code_challenge','redirect_uri','redirect_uri_provided_explicitly','resource']})

    def issue(self,c,g,scopes):
        access='as_oauth_'+secrets.token_urlsafe(32);refresh='as_refresh_'+secrets.token_urlsafe(32);now=time.time()
        if ROLES[g['role']]<ROLES['editor']:scopes=[s for s in scopes if s!='charts:write']
        expiry=min(now+3600,g['expires']);refresh_expiry=g['expires']
        for raw,kind,end in [(access,'access',expiry),(refresh,'refresh',refresh_expiry)]:
            c.execute('INSERT INTO oauth_tokens VALUES(?,?,?,?,?,0)',(digest(raw),g['id'],kind,json.dumps(scopes),end))
        return OAuthToken(access_token=access,refresh_token=refresh,token_type='Bearer',expires_in=int(expiry-now),scope=' '.join(scopes))

    async def exchange_authorization_code(self,client,authorization_code):
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE')
            r=c.execute('SELECT * FROM oauth_codes WHERE hash=? AND expires>?',(digest(authorization_code.code),time.time())).fetchone()
            g=self.grant(c,r['grant_id']) if r else None
            if not g or g['client']!=client.client_id:raise TokenError('invalid_grant')
            c.execute('DELETE FROM oauth_codes WHERE hash=?',(digest(authorization_code.code),))
            return self.issue(c,g,json.loads(g['scopes']))

    async def load_refresh_token(self,client,refresh_token):
        # Keep used refresh tokens until grant expiry so a replay revokes its family.
        with closing(self.connect()) as c,c:
            r=c.execute('SELECT * FROM oauth_tokens WHERE hash=? AND kind=?',(digest(refresh_token),'refresh')).fetchone()
            g=self.grant(c,r['grant_id']) if r else None
            if g and g['client']==client.client_id and r['used']:
                c.execute('UPDATE oauth_grants SET revoked=1 WHERE id=?',(g['id'],));return None
        token=self.token(refresh_token,'refresh')
        return token if token and token.client_id==client.client_id else None

    async def exchange_refresh_token(self,client,refresh_token,scopes):
        replay=False;result=None
        with closing(self.connect()) as c,c:
            c.execute('BEGIN IMMEDIATE')
            r=c.execute('SELECT * FROM oauth_tokens WHERE hash=? AND kind=? AND expires>?',(digest(refresh_token.token),'refresh',time.time())).fetchone()
            g=self.grant(c,r['grant_id']) if r else None
            if not g or g['client']!=client.client_id:raise TokenError('invalid_grant')
            if r['used']:
                c.execute('UPDATE oauth_grants SET revoked=1 WHERE id=?',(g['id'],));replay=True
            else:
                if set(scopes)-set(json.loads(r['scopes'])):raise TokenError('invalid_scope')
                c.execute('UPDATE oauth_tokens SET used=1 WHERE grant_id=?',(g['id'],))
                result=self.issue(c,g,scopes)
        if replay:raise TokenError('invalid_grant')
        return result

    async def revoke_token(self,token):
        with closing(self.connect()) as c,c:c.execute('UPDATE oauth_grants SET revoked=1 WHERE id=? AND client=?',(token.grant_id,token.client_id))

    def project_access(self,request,project,role):
        token=self.token(request.headers.get('authorization','')[7:],'access')
        if not token:raise HTTPException(401,'agent access expired or revoked')
        path=request.url.path
        allowed={'/api/overview','/api/catalog','/api/query','/api/charts','/api/setup','/api/journeys','/api/funnels','/api/funnels/analyze','/api/segments','/api/segments/analyze','/api/retention','/api/retention/analyze'}
        audit=path in {'/api/audit/catalog','/api/audit/review','/api/audit/trace','/api/audit/evaluate','/api/audit/onboarding','/api/traces/catalog','/api/traces/review','/api/traces/detail'}
        chart=path=='/api/charts' or bool(re.fullmatch(r'/api/charts/[0-9a-f-]{36}',path))
        scope='audit:read' if audit else 'charts:write' if chart and request.method!='GET' else 'analytics:read'
        if chart and request.method not in {'GET','POST','PUT'}:raise HTTPException(403,'agent access cannot remove charts')
        if project!=token.project or scope not in token.scopes or not (path in allowed or audit or chart) or role=='owner' or role=='editor' and not chart:
            raise HTTPException(403,'agent scope does not allow this action')

    def install(self,app):
        @app.middleware('http')
        async def bound_oauth_requests(request,call_next):
            if request.url.path in {'/register','/authorize','/token','/revoke'}:
                now=time.time();ip=request.client.host if request.client else 'unknown'
                limit=20 if request.url.path=='/register' else 120
                with closing(self.connect()) as c,c:
                    c.execute('BEGIN IMMEDIATE')
                    for key,maximum in [('oauth:'+request.url.path+':'+digest(ip),limit),('oauth:global:'+request.url.path,limit*10)]:
                        row=c.execute('SELECT window,attempts FROM login_limits WHERE ip=?',(key,)).fetchone()
                        count=row['attempts'] if row and now-row['window']<60 else 0
                        if count>=maximum:return JSONResponse({'error':'temporarily_unavailable'},429,headers={'Retry-After':'60'})
                        c.execute('INSERT INTO login_limits VALUES(?,?,?) ON CONFLICT(ip) DO UPDATE SET window=excluded.window,attempts=excluded.attempts',(key,row['window'] if count else now,count+1))
            return await call_next(request)

        @app.post('/revoke')
        async def revoke(request:Request):
            form=await request.form();client_id=form.get('client_id');raw=form.get('token')
            if not isinstance(client_id,str) or not isinstance(raw,str) or len(raw)>256:
                return JSONResponse({'error':'invalid_request'},400)
            client=await self.get_client(client_id)
            if not client or client.token_endpoint_auth_method!='none':return JSONResponse({'error':'invalid_client'},401)
            token=self.token(raw,'access') or self.token(raw,'refresh')
            if token and token.client_id==client_id:await self.revoke_token(token)
            return Response(status_code=200,headers={'Cache-Control':'no-store'})

        @app.get('/auth/mcp/request')
        async def preview(request:Request,request_id:str):
            p=self.accounts.session_user(request)
            with closing(self.connect()) as c:
                r=c.execute('SELECT * FROM oauth_pending WHERE id=? AND expires>?',(request_id,time.time())).fetchone()
                if not r:raise HTTPException(400,'request expired or already decided')
                projects=[dict(x) for x in c.execute('SELECT p.id,p.name,m.role FROM projects p JOIN memberships m ON m.workspace=p.workspace WHERE m.user_id=? ORDER BY p.name',(p['id'],))]
            client=await self.get_client(r['client']);params=json.loads(r['params'])
            if not client:raise HTTPException(400,'client registration expired')
            return {'client':client.client_name or 'Unnamed client','redirect_uri':params['redirect_uri'],'scopes':params['scopes'],'projects':projects,'expires':r['expires']}

        @app.post('/auth/mcp/decision')
        def decide(req:Decision,request:Request):
            p=self.accounts.session_user(request)
            with closing(self.connect()) as c,c:
                c.execute('BEGIN IMMEDIATE')
                r=c.execute('SELECT * FROM oauth_pending WHERE id=? AND expires>?',(req.request_id,time.time())).fetchone()
                if not r:raise HTTPException(400,'request expired or already decided')
                params=json.loads(r['params']);redirect=params['redirect_uri']
                if req.approve:
                    minimum='editor' if 'charts:write' in params['scopes'] else 'reader'
                    row=c.execute('SELECT p.workspace,m.role FROM projects p JOIN memberships m ON m.workspace=p.workspace WHERE p.id=? AND m.user_id=?',(req.project,p['id'])).fetchone()
                    if not row or ROLES[row['role']]<ROLES[minimum]:raise HTTPException(403,'project role does not allow requested access')
                    if c.execute('SELECT count(*) FROM oauth_grants WHERE user_id=? AND revoked=0 AND expires>?',(p['id'],time.time())).fetchone()[0]>=30:raise HTTPException(422,'revoke an unused connection first')
                    gid=secrets.token_hex(16);code=secrets.token_urlsafe(32);expires=time.time()+SESSION_SECONDS
                    c.execute('UPDATE oauth_clients SET expires=MAX(expires,?) WHERE id=?',(expires,r['client']))
                    c.execute('INSERT INTO oauth_grants VALUES(?,?,?,?,?,?,0)',(gid,p['id'],req.project,r['client'],json.dumps(params['scopes']),expires))
                    c.execute('INSERT INTO oauth_codes VALUES(?,?,?,?)',(digest(code),gid,r['params'],time.time()+120))
                    self.accounts.audit(c,row['workspace'],p,'mcp.approved',gid,minimum)
                    result=construct_redirect_uri(redirect,code=code,state=params['state'])
                else:result=construct_redirect_uri(redirect,error='access_denied',state=params['state'])
                c.execute('DELETE FROM oauth_pending WHERE id=?',(req.request_id,))
            return {'redirect_uri':result}

        @app.get('/auth/mcp/connections')
        def connections(request:Request):
            p=self.accounts.session_user(request)
            with closing(self.connect()) as c:
                rows=c.execute('SELECT g.id,g.project,p.name,g.scopes,g.expires,c.definition FROM oauth_grants g JOIN projects p ON p.id=g.project JOIN oauth_clients c ON c.id=g.client WHERE g.user_id=? AND g.revoked=0 AND g.expires>?',(p['id'],time.time())).fetchall()
            return {'connections':[{'id':r['id'],'project':r['name'],'scopes':json.loads(r['scopes']),'expires':r['expires'],'client':json.loads(r['definition']).get('client_name') or 'Unnamed client'} for r in rows]}

        @app.delete('/auth/mcp/connections/{grant_id}')
        def disconnect(grant_id:str,request:Request):
            p=self.accounts.session_user(request)
            with closing(self.connect()) as c,c:c.execute('UPDATE oauth_grants SET revoked=1 WHERE id=? AND user_id=?',(grant_id,p['id']))
            return {'revoked':True}
