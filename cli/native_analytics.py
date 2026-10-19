#!/usr/bin/env python3
import argparse
from dataclasses import dataclass
from http.cookies import SimpleCookie
import json
import os
import re
import sys
import uuid
from typing import Literal
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def private_json(path):
    source=Path(path)
    if not source.is_file() or source.stat().st_mode&0o077:raise ValueError('use a private file')
    value=json.loads(source.read_text())
    if not isinstance(value,dict):raise ValueError('provide a JSON object')
    return value


@dataclass(frozen=True)
class SessionReceipt:
    session:str
    cookie_name:Literal['as_session','na_session']='as_session'

    @classmethod
    def read(cls,path):
        value=private_json(path);name=value.get('cookie_name','as_session');token=value.get('session')
        if name not in ['as_session','na_session'] or not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+',token):raise ValueError('invalid session receipt')
        return cls(token,'na_session' if name=='na_session' else 'as_session')


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='command',required=True)
    assign=sub.add_parser('assign-existing');assign.add_argument('--user',required=True);assign.add_argument('--confirm',required=True)
    for name in ['health','schema','workspaces','me']:sub.add_parser(name)
    sub.add_parser('projects').add_argument('--workspace')
    login=sub.add_parser('login');login.add_argument('--email',required=True);login.add_argument('--plan',choices=['free','builder','team'])
    complete=sub.add_parser('complete-login');complete.add_argument('--file',required=True);complete.add_argument('--output',required=True)
    existing=sub.add_parser('login-existing');existing.add_argument('--file',required=True);existing.add_argument('--output',required=True)
    sub.add_parser('create-workspace').add_argument('--name',required=True)
    rename=sub.add_parser('rename-workspace');rename.add_argument('--workspace',required=True);rename.add_argument('--name',required=True)
    for name in ['members','invites','invite','change-role','remove-member','agent-keys','create-agent-key','revoke-agent-key','revoke-invite','resend-invite','mail','audit']:
        command=sub.add_parser(name);command.add_argument('--workspace',required=True)
        if name=='invite':command.add_argument('--file',required=True)
        if name in ['change-role','remove-member']:command.add_argument('--user',required=True)
        if name=='remove-member':command.add_argument('--confirm',required=True)
        if name in ['change-role','create-agent-key']:command.add_argument('--role',choices=['owner','editor','reader'],default='reader')
        if name=='create-agent-key':
            command.add_argument('--label',required=True);command.add_argument('--days',type=int,default=30);command.add_argument('--output',required=True)
        if name in ['revoke-agent-key','revoke-invite','resend-invite']:command.add_argument('--id',required=True)
    for name in ['usage','plans','billing','billing-invoices','billing-quote','billing-checkout','billing-portal','billing-refresh','billing-close-checkout','billing-downgrade']:
        command=sub.add_parser(name);command.add_argument('--workspace',required=True)
        if name=='billing-downgrade':command.add_argument('--confirm-plan',required=True,choices=['builder']);command.add_argument('--request-id',required=True)
        if name=='billing-portal':command.add_argument('--action',choices=['manage','cancel','change_plan','payment_method'],default='manage')
        if name=='usage':command.add_argument('--month')
        if name in ['billing-quote','billing-checkout']:command.add_argument('--plan',required=True,choices=['free','builder','team'] if name=='billing-quote' else ['builder','team'])
        if name in ['billing-checkout','billing-portal']:command.add_argument('--request-id')
    sub.add_parser('accept-invite').add_argument('--file',required=True)
    create=sub.add_parser('create-project');create.add_argument('--workspace');create.add_argument('--id',required=True);create.add_argument('--name',required=True);create.add_argument('--output',required=True)
    for name in ['send','identify']:
        sub.add_parser(name).add_argument('--file',required=True)
    for name in ['overview','events','people','charts','query','journeys','setup','check-setup','save-chart','delete-chart','tag','rotate-key']:
        s=sub.add_parser(name);s.add_argument('--project',required=True)
        if name in ['overview','events']:s.add_argument('--days',type=int,default=7)
        if name in ['setup','check-setup']:s.add_argument('--environment',choices=['production','staging','development','test','local'],default='production')
        if name=='events':
            for option in ['event','distinct-id','property','value','status','ip','operation','before','before-id']:s.add_argument('--'+option)
        if name=='people':s.add_argument('--search',default='');s.add_argument('--offset',type=int,default=0)
        if name in ['query','journeys','save-chart','tag']:s.add_argument('--file',required=True)
        if name=='delete-chart':s.add_argument('--id',required=True)
        if name=='rotate-key':s.add_argument('--confirm',required=True);s.add_argument('--output',required=True)
    for kind in ['funnels','segments','retention']:
        sub.add_parser(kind).add_argument('--project',required=True)
        for action in ['analyze','save','update','delete']:
            command=sub.add_parser(action+'-'+(kind[:-1] if kind!='retention' else kind));command.add_argument('--project',required=True)
            if action!='delete':command.add_argument('--file',required=True)
            if action in ['update','delete']:
                command.add_argument('--id',required=True);command.add_argument('--revision',required=True,type=int)
    evidence=sub.add_parser('attach-audit-evidence');evidence.add_argument('--project',required=True);evidence.add_argument('--id',required=True);evidence.add_argument('--file',required=True)
    for name in ['audit-catalog','audit-review','audit-trace','catalog','describe-property']:
        command=sub.add_parser(name);command.add_argument('--project',required=True);command.add_argument('--file',required=True)
    connect=sub.add_parser('connect-audit');connect.add_argument('--project',required=True);connect.add_argument('--label',default='Codex request audit');connect.add_argument('--days',type=int,default=7);connect.add_argument('--output',required=True)
    key=sub.add_parser('create-read-key');key.add_argument('--purpose',choices=['analytics','audit'],default='analytics');key.add_argument('--project',required=True);key.add_argument('--label',required=True);key.add_argument('--days',type=int,default=30);key.add_argument('--output',required=True)
    key=sub.add_parser('read-keys');key.add_argument('--project',required=True)
    key=sub.add_parser('revoke-read-key');key.add_argument('--project',required=True);key.add_argument('--id',required=True)
    for name in ['loop-inspect','loop-sync']:
        command=sub.add_parser(name);command.add_argument('--repo',default='.');command.add_argument('--goal',default='first-answer');command.add_argument('--repository');command.add_argument('--base',default='main')
        if name=='loop-sync':command.add_argument('--project',required=True)
    for name in ['loop-goals','loop-status','loop-policy','loop-prepare','loop-action','loop-observe','loop-assess','loop-external','loop-compare','loop-connect','loop-deliver']:
        command=sub.add_parser(name);command.add_argument('--project',required=True)
        if name not in ['loop-goals','loop-action']:command.add_argument('--goal',default='first-answer')
        if name in ['loop-policy','loop-observe','loop-assess','loop-external','loop-compare','loop-deliver']:command.add_argument('--file',required=True)
        if name=='loop-policy':command.add_argument('--revision',type=int,required=True)
        if name in ['loop-observe','loop-assess','loop-external','loop-action','loop-deliver']:command.add_argument('--id',required=True,type=uuid.UUID)
        if name=='loop-prepare':command.add_argument('--request-id',required=True,type=uuid.UUID)
        if name=='loop-deliver':command.add_argument('--connection',required=True,type=uuid.UUID)
        if name=='loop-action':command.add_argument('--operation',required=True,choices=['draft-pr','reconcile','merge'])
    return p


def main(argv=None):
    args=parser().parse_args(argv);command=args.command
    if command in ['loop-inspect','loop-sync']:
        from loop_repo import inspect
        inspected=inspect(args.repo,args.goal,args.repository,args.base)
        if command=='loop-inspect':
            print(json.dumps(inspected,indent=2));return 0
        if inspected['draft']:raise ValueError('commit the changed goal and dependencies before sync')
    origin=os.environ.get('NATIVE_ANALYTICS_URL','').rstrip('/')
    parsed=urllib.parse.urlsplit(origin)
    if not (parsed.scheme=='https' and parsed.hostname or parsed.scheme=='http' and parsed.hostname=='127.0.0.1') or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:raise ValueError('set NATIVE_ANALYTICS_URL to your HTTPS origin')
    path={'health':'/healthz','schema':'/openapi.json','projects':'/api/projects','create-project':'/api/projects','send':'/v1/events','identify':'/v1/identify',
          'overview':'/api/overview','events':'/api/events','people':'/api/people','charts':'/api/charts','query':'/api/query','journeys':'/api/journeys','setup':'/api/setup','check-setup':'/api/setup-checks','me':'/auth/me','workspaces':'/api/workspaces','create-workspace':'/api/workspaces','login':'/auth/request-link','complete-login':'/auth/consume-link','login-existing':'/auth/login','accept-invite':'/api/invites/accept','save-chart':'/api/charts','tag':'/api/people'}.get(command)
    body=None;method='GET';params={}
    if hasattr(args,'project'):params['project']=args.project
    if hasattr(args,'days'):params['days']=args.days
    if command.startswith('loop-'):
        path='/api/loop/goals'
        if command not in ['loop-goals','loop-action']:path+='/'+urllib.parse.quote(args.goal,safe='')
        if command=='loop-sync':path+='/sync';body=inspected['source'];method='PUT'
        if command=='loop-policy':path+='/policy';params['revision']=args.revision;method='PUT'
        if command=='loop-prepare':path+='/prepare';body={'request_id':str(args.request_id)};method='POST'
        if command=='loop-action':path='/api/loop/actions/'+str(args.id)+'/'+args.operation;body={};method='POST'
        if command in ['loop-observe','loop-assess','loop-external']:
            path+='/'+{'loop-observe':'observations','loop-assess':'answers','loop-external':'external'}[command]+'/'+str(args.id);method='PUT'
        if command=='loop-compare':path+='/compare';method='POST'
        if command=='loop-connect':path+='/connection';body={};method='POST'
        if command=='loop-deliver':path+='/connections/'+str(args.connection)+'/answers/'+str(args.id);method='PUT'
        if hasattr(args,'file'):body=json.loads(Path(args.file).read_text())
    if command in ['projects','create-project'] and (getattr(args,'workspace',None) or os.environ.get('AFTERSPAN_WORKSPACE')):params['workspace']=getattr(args,'workspace',None) or os.environ['AFTERSPAN_WORKSPACE']
    if command in ['usage','plans','billing','billing-invoices','billing-quote','billing-checkout','billing-portal','billing-refresh','billing-close-checkout','billing-downgrade']:
        path='/api/workspaces/'+urllib.parse.quote(args.workspace,safe='')+'/'+{'billing-invoices':'billing/invoices','billing-quote':'billing/quote','billing-checkout':'billing/checkout','billing-close-checkout':'billing/checkout','billing-portal':'billing/portal','billing-refresh':'billing/refresh','billing-downgrade':'billing/downgrade'}.get(command,command)
        if command=='usage' and args.month:params['month']=args.month
        if command=='billing-quote':params['plan']=args.plan
        if command in ['billing-checkout','billing-portal','billing-refresh']:
            body={};method='POST'
            if command=='billing-checkout':body['plan']=args.plan
            if getattr(args,'request_id',None):body['request_id']=args.request_id
        if command=='billing-portal':body['action']=args.action
        if command=='billing-downgrade':body={'confirm_plan':args.confirm_plan,'request_id':args.request_id};method='POST'
        if command=='billing-close-checkout':method='DELETE'
    for kind in ['funnels','segments','retention']:
        if command==kind:path='/api/'+kind
        for action in ['analyze','save','update','delete']:
            if command==action+'-'+(kind[:-1] if kind!='retention' else kind):
                path='/api/'+kind
                if action=='analyze':path+='/analyze'
                if action in ['update','delete']:
                    path+='/'+urllib.parse.quote(args.id,safe='');params['revision']=args.revision
                if action!='delete':body=json.loads(Path(args.file).read_text())
                method={'analyze':'POST','save':'POST','update':'PUT','delete':'DELETE'}[action]
    if command in ['audit-catalog','audit-review','audit-trace','catalog','describe-property']:
        path='/api/catalog'+('/describe' if command=='describe-property' else '') if command in ['catalog','describe-property'] else '/api/audit/'+command.split('-')[1];body=json.loads(Path(args.file).read_text());method='POST'
    if command=='attach-audit-evidence':
        path='/api/audit/evidence/'+urllib.parse.quote(args.id,safe='');body=json.loads(Path(args.file).read_text());method='PUT'
    if command=='assign-existing':
        if args.confirm!='existing-projects':raise ValueError('confirm existing-projects')
        path='/api/operator/assign-existing';body={'account_id':args.user};method='POST'
    if command=='login':body={'email':args.email,**({'plan':args.plan} if args.plan else {})};method='POST'
    if command=='login-existing':
        password=private_json(args.file).get('password')
        if not isinstance(password,str) or not password:raise ValueError('provide a private password object')
        body={'password':password};method='POST'
    if command in ['complete-login','accept-invite']:body=json.loads(Path(args.file).read_text());method='POST'
    if command=='create-workspace':body={'name':args.name};method='POST'
    if command=='rename-workspace':path='/api/workspaces/'+urllib.parse.quote(args.workspace,safe='');body={'name':args.name};method='PATCH'
    if command in ['members','invites','invite','change-role','remove-member','agent-keys','create-agent-key','revoke-agent-key','revoke-invite','resend-invite','mail','audit']:
        path='/api/workspaces/'+urllib.parse.quote(args.workspace,safe='')+'/'+{'invite':'invites','change-role':'members','remove-member':'members','create-agent-key':'agent-keys','revoke-agent-key':'agent-keys','revoke-invite':'invites','resend-invite':'invites'}.get(command,command);params={}
        if command=='invite':body=json.loads(Path(args.file).read_text());method='POST'
        if command=='create-agent-key':body={'label':args.label,'role':args.role,'expires_days':args.days};method='POST'
        if command=='change-role':path+='/'+urllib.parse.quote(args.user,safe='');body={'role':args.role};method='PATCH'
        if command=='remove-member':
            if args.confirm!=args.user:raise ValueError('--confirm must equal the member ID')
            path+='/'+urllib.parse.quote(args.user,safe='');method='DELETE'
        if command=='resend-invite':path+='/'+urllib.parse.quote(args.id,safe='')+'/resend';body={};method='POST'
        if command in ['revoke-agent-key','revoke-invite']:path+='/'+urllib.parse.quote(args.id,safe='');method='DELETE'
    if command=='create-project':body={'id':args.id,'name':args.name};method='POST'
    if command in ['send','identify','query','journeys','save-chart','tag']:
        body=json.loads(Path(args.file).read_text());method='PATCH' if command=='tag' else 'POST'
    if command=='setup':params['environment']=args.environment
    if command=='check-setup':method='POST';body={'environment':args.environment}
    if command=='delete-chart':path='/api/charts/'+urllib.parse.quote(args.id,safe='');method='DELETE'
    if command=='rotate-key':
        if args.confirm!=args.project:raise ValueError('--confirm must equal the project being rotated')
        path='/api/projects/'+urllib.parse.quote(args.project,safe='')+'/rotate-key';method='POST';body={}
    if command in ['create-read-key','read-keys','revoke-read-key','connect-audit']:
        params={};path='/api/projects/'+urllib.parse.quote(args.project,safe='')+'/read-keys'
        if command in ['create-read-key','connect-audit']:method='POST';body={'label':args.label,'expires_days':args.days,'purpose':getattr(args,'purpose','audit')}
        if command=='revoke-read-key':method='DELETE';path+='/'+urllib.parse.quote(args.id,safe='')
    for field in ['event' ,'distinct_id','property','value','status','ip','operation','before','before_id','search','offset']:
        if hasattr(args,field) and getattr(args,field) is not None:params[field]=getattr(args,field)
    key_name='NATIVE_ANALYTICS_WRITE_KEY' if command in ['send','identify'] else 'ANALYTICS_ADMIN_TOKEN'
    key=os.environ.get(key_name)
    if key_name!='NATIVE_ANALYTICS_WRITE_KEY' and command!='assign-existing':key=os.environ.get('AFTERSPAN_WORKSPACE_KEY') or key
    if command in ['projects','overview','query','journeys','setup','charts','funnels','segments','retention','analyze-funnel','analyze-segment','analyze-retention','catalog','audit-catalog','audit-review','audit-trace','loop-goals','loop-status','loop-observe','loop-compare']:
        key=os.environ.get('AFTERSPAN_READ_KEY') or key
    public=command in ['health','schema','login','complete-login','login-existing']
    session_file=os.environ.get('AFTERSPAN_SESSION_FILE')
    receipt=SessionReceipt.read(session_file) if session_file and not public and command not in ['send','identify','assign-existing'] else None
    if receipt:key=None
    if command in ['create-agent-key','create-workspace','accept-invite']:key=None
    if not public and not key and not receipt:raise ValueError('set a scoped key or AFTERSPAN_SESSION_FILE')
    if hasattr(args,'output') and Path(args.output).exists():raise ValueError('private receipt already exists')
    headers={'Content-Type':'application/json','X-Native-CSRF':'1'}
    if receipt:headers['Cookie']=receipt.cookie_name+'='+receipt.session
    if key and not public:headers['Authorization']='Bearer '+key
    request=urllib.request.Request(origin+path+('?' + urllib.parse.urlencode(params) if params else ''),
                                   data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args):return None
    try:
        with urllib.request.build_opener(NoRedirect).open(request,timeout=60 if command.startswith('loop-') else 20) as r:
            result=json.load(r)
            if command in ['complete-login','login-existing']:
                name='na_session' if command=='login-existing' else 'as_session'
                cookies=SimpleCookie();cookies.load(r.headers.get('Set-Cookie',''))
                if name not in cookies:raise ValueError('no session receipt')
                result={**({'user':result['user']} if command=='complete-login' else {}),'session':cookies[name].value,'cookie_name':name}
    except urllib.error.HTTPError as exc:
        # Do not echo a remote error payload that might contain a credential or input.
        print(json.dumps({'ok':False,'http_status':exc.code,'error':'analytics request rejected'}));return 1
    if command=='connect-audit':
        result.update(origin=origin,connection={'command':sys.executable,'args':[str(Path(__file__).resolve().parents[1]/'agent/run_audit.py'),str(Path(args.output).resolve())]})
    if hasattr(args,'output'):
        fd=os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(json.dumps(result,indent=2)+'\n')
        result={'ok':True,'private_receipt':args.output,'id':result.get('id'),**({'connection':result['connection']} if command=='connect-audit' else {})}
    print(json.dumps(result,indent=2));return 0


if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,OSError,TimeoutError) as exc:
        print(json.dumps({'ok':False,'error':type(exc).__name__,'message':'Check arguments, environment, local files and network.'}),file=sys.stderr);sys.exit(2)
