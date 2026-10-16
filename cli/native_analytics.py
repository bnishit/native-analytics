#!/usr/bin/env python3
"""JSON CLI for Native Analytics. Secrets come only from the environment."""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='command',required=True)
    for name in ['health','schema','projects']:sub.add_parser(name)
    create=sub.add_parser('create-project');create.add_argument('--id',required=True);create.add_argument('--name',required=True);create.add_argument('--output',required=True)
    for name in ['send','identify']:
        sub.add_parser(name).add_argument('--file',required=True)
    for name in ['overview','events','people','charts','query','save-chart','delete-chart','tag','rotate-key']:
        s=sub.add_parser(name);s.add_argument('--project',required=True)
        if name in ['overview','events']:s.add_argument('--days',type=int,default=7)
        if name=='events':
            for option in ['event','distinct-id','property','value','status','ip','operation','before','before-id']:s.add_argument('--'+option)
        if name=='people':s.add_argument('--search',default='');s.add_argument('--offset',type=int,default=0)
        if name in ['query','save-chart','tag']:s.add_argument('--file',required=True)
        if name=='delete-chart':s.add_argument('--id',required=True)
        if name=='rotate-key':s.add_argument('--confirm',required=True);s.add_argument('--output',required=True)
    return p


def main(argv=None):
    args=parser().parse_args(argv);command=args.command
    origin=os.environ.get('NATIVE_ANALYTICS_URL','').rstrip('/')
    if not origin.startswith('https://') and not origin.startswith('http://127.0.0.1:'):raise ValueError('set NATIVE_ANALYTICS_URL to your HTTPS origin')
    path={'health':'/healthz','schema':'/openapi.json','projects':'/api/projects','create-project':'/api/projects','send':'/v1/events','identify':'/v1/identify',
          'overview':'/api/overview','events':'/api/events','people':'/api/people','charts':'/api/charts','query':'/api/query','save-chart':'/api/charts','tag':'/api/people'}.get(command)
    body=None;method='GET';params={}
    if hasattr(args,'project'):params['project']=args.project
    if hasattr(args,'days'):params['days']=args.days
    if command=='create-project':body={'id':args.id,'name':args.name};method='POST'
    if command in ['send','identify','query','save-chart','tag']:
        body=json.loads(Path(args.file).read_text());method='PATCH' if command=='tag' else 'POST'
    if command=='delete-chart':path='/api/charts/'+urllib.parse.quote(args.id,safe='');method='DELETE'
    if command=='rotate-key':
        if args.confirm!=args.project:raise ValueError('--confirm must equal the project being rotated')
        path='/api/projects/'+urllib.parse.quote(args.project,safe='')+'/rotate-key';method='POST';body={}
    for field in ['event','distinct_id','property','value','status','ip','operation','before','before_id','search','offset']:
        if hasattr(args,field) and getattr(args,field) is not None:params[field]=getattr(args,field)
    key_name='NATIVE_ANALYTICS_WRITE_KEY' if command in ['send','identify'] else 'ANALYTICS_ADMIN_TOKEN'
    key=os.environ.get(key_name)
    if command not in ['health','schema'] and not key:raise ValueError('set '+key_name)
    headers={'Content-Type':'application/json'}
    if key and command not in ['health','schema']:headers['Authorization']='Bearer '+key
    request=urllib.request.Request(origin+path+('?' + urllib.parse.urlencode(params) if params else ''),
                                   data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args):return None
    try:
        with urllib.request.build_opener(NoRedirect).open(request,timeout=20) as r:result=json.load(r)
    except urllib.error.HTTPError as exc:
        # Do not echo a remote error payload that might contain a credential or input.
        print(json.dumps({'ok':False,'http_status':exc.code,'error':'analytics request rejected'}));return 1
    if hasattr(args,'output'):
        fd=os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(json.dumps(result,indent=2)+'\n')
        result={'ok':True,'private_receipt':args.output,'id':result.get('id')}
    print(json.dumps(result,indent=2));return 0


if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,OSError,TimeoutError) as exc:
        print(json.dumps({'ok':False,'error':type(exc).__name__,'message':'Check arguments, environment, local files and network.'}),file=sys.stderr);sys.exit(2)
