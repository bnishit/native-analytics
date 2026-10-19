"""Provision isolated Stripe test prices/portal; save private configuration for dev.py."""
import argparse
import json
import os
from pathlib import Path
import sys
import tomllib
import stripe

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from payments import CATALOG_VERSION,PLANS,StripeProvider

def save(path,value):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:json.dump(value,f,indent=2)
    path.chmod(0o600)

def provision(cli_config,output):
    profile=tomllib.loads(cli_config.read_text())['default'];key=profile['test_mode_api_key']
    if not key.startswith(('sk_test_','rk_test_','rkcs_test_')):raise ValueError('Only a Stripe test key is accepted')
    client=stripe.StripeClient(key,max_network_retries=2,http_client=stripe.RequestsClient(timeout=10))
    account=profile['account_id'];values=json.loads(output.read_text()) if output.exists() else {}
    if values and values.get('account_id')!=account:raise ValueError('Use a separate config for another Stripe account')
    scope='afterspan:'+account+':'+CATALOG_VERSION
    values.update(account_id=account,expires_at=profile.get('sandbox_expires_at'),AFTERSPAN_BILLING_MODE='stripe_test',AFTERSPAN_STRIPE_SECRET_KEY=key)
    values.setdefault('products',{})
    for name in ['builder','team']:
        env='AFTERSPAN_STRIPE_PRICE_'+name.upper()
        if not values['products'].get(name):
            product=client.v1.products.create({'name':'Afterspan '+PLANS[name]['name'],'metadata':{'afterspan_catalog':CATALOG_VERSION,'purpose':'isolated_test'}},options={'idempotency_key':scope+':product:'+name})
            if product['livemode']:raise ValueError('Unexpected live product')
            values['products'][name]=product['id'];values.pop(env,None);save(output,values)
        if not values.get(env):
            price=client.v1.prices.create({'product':values['products'][name],'currency':'usd','unit_amount':PLANS[name]['monthly_cents'],'recurring':{'interval':'month'},'nickname':PLANS[name]['name'],'metadata':{'afterspan_plan':name,'afterspan_catalog':CATALOG_VERSION}},options={'idempotency_key':scope+':monthly-price:'+name})
            if price['livemode']:raise ValueError('Unexpected live price')
            values[env]=price['id'];save(output,values)
    if not values.get('AFTERSPAN_STRIPE_PORTAL_CONFIGURATION'):
        config=client.v1.billing_portal.configurations.create({'business_profile':{'headline':'Afterspan — test workspace billing'},'features':{
            'invoice_history':{'enabled':True},'payment_method_update':{'enabled':True},
            'customer_update':{'enabled':True,'allowed_updates':['address','name']},
            'subscription_cancel':{'enabled':True,'mode':'at_period_end','proration_behavior':'none'},
            'subscription_update':{'enabled':True,'default_allowed_updates':['price'],'products':[{'product':values['products']['team'],'prices':[values['AFTERSPAN_STRIPE_PRICE_TEAM']]}],'proration_behavior':'always_invoice','schedule_at_period_end':{'conditions':[{'type':'decreasing_item_amount'}]}}}},options={'idempotency_key':scope+':upgrade-portal'})
        if config['livemode']:raise ValueError('Unexpected live configuration')
        values['AFTERSPAN_STRIPE_PORTAL_CONFIGURATION']=config['id'];save(output,values)
    os.environ.update({k:value for k,value in values.items() if k.startswith('AFTERSPAN_') and isinstance(value,str)})
    StripeProvider('stripe_test').portal_configuration();save(output,values)
    print(json.dumps({'mode':'stripe_test','account':account,'expires_at':values.get('expires_at'),'catalog':CATALOG_VERSION,'plans':['builder','team'],'portal_allows_only_upgrades':True,'private_config':str(output),'webhook_ready':bool(values.get('AFTERSPAN_STRIPE_WEBHOOK_SECRET'))}))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--cli-config',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    try:provision(a.cli_config,a.output)
    except Exception as e:print(json.dumps({'ok':False,'error':type(e).__name__,'message':'Test provisioning failed; no keys are printed. Check test account access and the private configuration.'}));sys.exit(1)
