"""Forward signed Stripe test webhooks; keep signing credentials in private files."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib
from stripe_test import save

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--stripe-cli',type=Path,required=True);p.add_argument('--cli-config',type=Path,required=True);p.add_argument('--provider-config',type=Path,required=True);p.add_argument('--port',type=int,default=8770);a=p.parse_args()
    profile=tomllib.loads(a.cli_config.read_text())['default'];values=json.loads(a.provider_config.read_text())
    if values.get('AFTERSPAN_BILLING_MODE')!='stripe_test' or profile['account_id']!=values.get('account_id'):raise ValueError('matching test account required')
    if not 1024<=a.port<=65535:raise ValueError('use a loopback development port')
    events='checkout.session.completed,checkout.session.async_payment_succeeded,checkout.session.async_payment_failed,customer.subscription.created,customer.subscription.updated,customer.subscription.deleted,invoice.paid,invoice.payment_failed,invoice.voided,invoice.marked_uncollectible'
    command=[str(a.stripe_cli.resolve()),'--config',str(a.cli_config.resolve()),'listen','--events',events,'--forward-to',f'http://127.0.0.1:{a.port}/api/billing/webhook']
    log=a.provider_config.parent/'listener.log';fd=os.open(log,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    with os.fdopen(fd,'w') as private:
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,env={**os.environ,'STRIPE_NO_AUTO_UPDATE':'1'})
        try:
            for line in process.stdout:
                private.write(line);private.flush()
                match=re.search(r'whsec_[A-Za-z0-9]+',line)
                if match:
                    values=json.loads(a.provider_config.read_text());values['AFTERSPAN_STRIPE_WEBHOOK_SECRET']=match[0];save(a.provider_config,values)
                    print('Stripe test webhook forwarder ready. Start/restart the isolated test preview with this provider configuration.',flush=True)
                elif '-->' in line or '<--' in line:
                    code=re.search(r'\[(\d{3})\]',line)
                    print('Stripe test webhook forwarded'+(' · HTTP '+code[1] if code else ''),flush=True)
            return process.wait()
        finally:
            if process.poll() is None:process.terminate();process.wait(timeout=10)

if __name__=='__main__':
    try:sys.exit(main())
    except KeyboardInterrupt:pass
    except Exception as e:print('Test webhook forwarder stopped: '+type(e).__name__,file=sys.stderr);sys.exit(1)
