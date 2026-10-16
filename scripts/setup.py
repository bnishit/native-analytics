#!/usr/bin/env python3
"""Create a private local setup; optionally provision and deploy your own Railway service."""
import argparse,json,os,secrets,subprocess
from pathlib import Path
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--env-file',default='.env');p.add_argument('--railway',action='store_true');p.add_argument('--workspace');p.add_argument('--name',default='native-analytics')
a=p.parse_args()
if a.railway and not a.workspace:p.error('--workspace is required for Railway setup')
repo=Path(__file__).resolve().parents[1];path=Path(a.env_file).resolve()
if path.exists():p.error('env file exists; refusing to replace your keys')
values={'ANALYTICS_ADMIN_TOKEN':'na_admin_'+secrets.token_urlsafe(40),'ANALYTICS_PASSWORD':secrets.token_urlsafe(30),
        'ANALYTICS_SESSION_SECRET':secrets.token_urlsafe(40),'ANALYTICS_DB':str(repo/'data/analytics.db'),
        'RETENTION_DAYS':'90','COOKIE_SECURE':'false','NATIVE_ANALYTICS_URL':'http://127.0.0.1:8080'}
def run(args,input=None):
    result=subprocess.run(['railway',*args],cwd=repo,input=input,text=True,capture_output=True)
    if result.returncode:raise RuntimeError('Railway '+args[0]+' failed; inspect your Railway project. Your keys remain in the private env file.')
    return result.stdout
# Save before any cloud step so a partial setup never loses generated keys.
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:f.write('\n'.join(k+'='+v for k,v in values.items())+'\n')
if a.railway:
    project=json.loads(run(['init','--name',a.name,'--workspace',a.workspace,'--json']))
    service=json.loads(run(['add','--service',a.name,'--json']))
    run(['service','link',a.name]);run(['volume','add','--mount-path','/data','--json'])
    domain=json.loads(run(['domain','--service',a.name,'--port','8080','--json']))['domain']
    origin=domain if domain.startswith('https://') else 'https://'+domain
    values.update(ANALYTICS_DB='/data/analytics.db',COOKIE_SECURE='true',ANALYTICS_PUBLIC_URL=origin,NATIVE_ANALYTICS_URL=origin,RAILWAY_DEPLOYMENT_OVERLAP_SECONDS='0')
    for key,value in values.items():
        if key!='NATIVE_ANALYTICS_URL':run(['variable','set',key,'--stdin','--skip-deploys','--service',a.name],value)
    with path.open('w') as f:f.write('\n'.join(k+'='+v for k,v in values.items())+'\n')
    run(['up','--detach','--project',project['id'],'--service',service['id']])
    print(json.dumps({'project_id':project['id'],'url':origin,'private_env_file':str(path),'status':'deployment_submitted; verify /healthz before using'}))
else:print(json.dumps({'private_env_file':str(path),'next':'set -a; source .env; set +a; uvicorn app:app --host 127.0.0.1 --port 8080'}))
