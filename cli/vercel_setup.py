"""Transfer a project producer key through stdin, never command arguments or output."""
import json
import os
from pathlib import Path
import re
import subprocess


def install(args,receipt):
    key=receipt.get('write_key')
    if not isinstance(key,str) or not re.fullmatch(r'na_write_[A-Za-z0-9_-]+',key):raise ValueError('producer key receipt required')
    root=Path(args.cwd).resolve()
    link=json.loads((root/'.vercel/project.json').read_text())
    if link.get('projectId')!=args.vercel_project or link.get('orgId')!=args.vercel_team:
        raise ValueError('linked Vercel destination does not match the requested project and team')
    environment={**os.environ,'VERCEL_PROJECT_ID':args.vercel_project,'VERCEL_ORG_ID':args.vercel_team}
    command=['vercel','env','add','NATIVE_ANALYTICS_WRITE_KEY',args.environment,
             '--sensitive','--cwd',str(root),'--scope',args.vercel_team]
    try:
        result=subprocess.run(command,input=key,text=True,capture_output=True,timeout=60,env=environment)
    except subprocess.TimeoutExpired:
        return {'ok':False,'status':'unknown','message':'Vercel timed out. Inspect the target before retrying; it may have accepted the key.'}
    if result.returncode:
        return {'ok':False,'status':'not_confirmed','message':'Vercel did not confirm installation. Check CLI login, destination access and whether the variable already exists. Provider output is withheld to protect secrets.'}
    return {'ok':True,'status':'provider_accepted','project':args.vercel_project,'team':args.vercel_team,
            'environment':args.environment,'variable':'NATIVE_ANALYTICS_WRITE_KEY',
            'note':'Redeploy the producer and verify a stored setup event. This is not delivery proof.'}
