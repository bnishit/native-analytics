"""Run an isolated loopback preview, with local simulation or a real Stripe test sandbox."""
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import sys

ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--billing',choices=['off','local','stripe-test'],default='off')
p.add_argument('--stripe-config',type=Path)
p.add_argument('--port',type=int)
a=p.parse_args();port=a.port or (8770 if a.billing=='stripe-test' else 8769)
if not 1024<=port<=65535:p.error('use a development port between 1024 and 65535')
state=ROOT/'data'/('stripe-preview' if a.billing=='stripe-test' else '')
state.mkdir(mode=0o700,parents=True,exist_ok=True);private=state/'dev-access.json'
if private.exists():values=json.loads(private.read_text())
else:values={key:secrets.token_urlsafe(36) for key in ['ANALYTICS_ADMIN_TOKEN','ANALYTICS_PASSWORD','ANALYTICS_SESSION_SECRET']}
if 'AFTERSPAN_LOCAL_WEBHOOK_SECRET' not in values:values['AFTERSPAN_LOCAL_WEBHOOK_SECRET']=secrets.token_urlsafe(36)
if 'AFTERSPAN_CONNECTION_KEY' not in values:values['AFTERSPAN_CONNECTION_KEY']=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
fd=os.open(private,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
with os.fdopen(fd,'w') as handle:json.dump(values,handle)
private.chmod(0o600);os.environ.update(values)
os.environ['AFTERSPAN_ENABLE_LIVE_BILLING']='false'
if a.billing=='stripe-test':
    if not a.stripe_config:p.error('--stripe-config is required for real Stripe test mode')
    config=json.loads(a.stripe_config.read_text())
    allowed=['AFTERSPAN_STRIPE_SECRET_KEY','AFTERSPAN_STRIPE_PRICE_BUILDER','AFTERSPAN_STRIPE_PRICE_TEAM','AFTERSPAN_STRIPE_PORTAL_CONFIGURATION','AFTERSPAN_STRIPE_WEBHOOK_SECRET']
    if config.get('AFTERSPAN_BILLING_MODE')!='stripe_test' or not all(config.get(k) for k in allowed):p.error('finish test provisioning and start the webhook listener first')
    if not config['AFTERSPAN_STRIPE_SECRET_KEY'].startswith(('sk_test_','rk_test_','rkcs_test_')):p.error('a Stripe test key is required')
    os.environ['AFTERSPAN_TEST_SANDBOX_EXPIRES']=config.get('expires_at') or ''
    os.environ.update({k:config[k] for k in allowed});os.environ['AFTERSPAN_BILLING_MODE']='stripe_test'
else:os.environ['AFTERSPAN_BILLING_MODE']='local' if a.billing=='local' else 'disabled'
os.environ['AFTERSPAN_EDITION']='self_hosted' if a.billing=='off' else 'hosted'
os.environ.update(ANALYTICS_DB=str(state/'preview.db'),COOKIE_SECURE='false',AFTERSPAN_MAIL_MODE='local',AFTERSPAN_MAIL_DIR=str(state/'mail'),ANALYTICS_PUBLIC_URL=f'http://127.0.0.1:{port}')
sys.path.insert(0,str(ROOT))
import uvicorn
if __name__=='__main__':
    print(f'Afterspan {a.billing} preview: http://127.0.0.1:{port} — private local mail; live billing disabled',flush=True)
    uvicorn.run('app:app',host='127.0.0.1',port=port,log_level='warning')
