"""Stripe-hosted payments. No application endpoint charges a saved payment method."""
import os
from urllib.parse import urlsplit
import stripe
from fastapi import HTTPException

PLANS={
 'self_hosted':{'name':'Open source · self-hosted','monthly_cents':0,'currency':'usd','events':None,'bytes':None,'projects':None,'seats':None},
 'free':{'name':'Free','monthly_cents':0,'currency':'usd','events':10000,'bytes':50_000_000,'projects':2,'seats':3},
 'builder':{'name':'Builder','monthly_cents':1900,'currency':'usd','events':100000,'bytes':1_000_000_000,'projects':10,'seats':10},
 'team':{'name':'Team','monthly_cents':7900,'currency':'usd','events':500000,'bytes':5_000_000_000,'projects':50,'seats':25},
 'legacy':{'name':'Existing workspace','monthly_cents':0,'currency':'usd','events':None,'bytes':None,'projects':100,'seats':100},
}
CATALOG_VERSION='2026-10-17-v1'

def safe_url(value,local=False):
    p=urlsplit(value or '')
    valid=p.scheme=='https' and p.hostname and (p.hostname=='stripe.com' or p.hostname.endswith('.stripe.com'))
    if local:valid=p.scheme=='http' and p.hostname=='127.0.0.1'
    if not valid or p.username or p.password:raise ValueError('invalid provider URL')
    return value

class StripeProvider:
    def __init__(self,mode):
        key=os.environ.get('AFTERSPAN_STRIPE_SECRET_KEY','');live=mode=='stripe_live'
        if not key.startswith(('sk_live_',) if live else ('sk_test_','rk_test_','rkcs_test_')) or live and os.environ.get('AFTERSPAN_ENABLE_LIVE_BILLING')!='true':raise HTTPException(503,'configure the matching Stripe key and explicit live billing setting')
        self.live=live;self.client=stripe.StripeClient(key,max_network_retries=2,http_client=stripe.RequestsClient(timeout=10))

    def price_ids(self):
        result={name:os.environ.get('AFTERSPAN_STRIPE_PRICE_'+name.upper(),'') for name in ['builder','team']}
        if not all(v.startswith('price_') for v in result.values()) or len(set(result.values()))!=2:raise HTTPException(503,'configure distinct Builder and Team Stripe price IDs')
        return result

    def verify_prices(self):
        ids=self.price_ids();products=[]
        for name,pid in ids.items():
            p=self.client.v1.prices.retrieve(pid);plan=PLANS[name];recurring=p.get('recurring') or {}
            if not p.get('active') or p.get('livemode')!=self.live or p.get('currency')!=plan['currency'] or p.get('unit_amount')!=plan['monthly_cents'] or recurring.get('interval')!='month' or recurring.get('interval_count')!=1 or recurring.get('usage_type')!='licensed':raise ValueError('Stripe price does not match the versioned catalog')
            products.append({'product':p['product'],'prices':[pid]})
        return products

    def customer(self,workspace,instance,key):
        return self.client.v1.customers.create({'metadata':{'afterspan_workspace':workspace,'afterspan_instance':instance}},options={'idempotency_key':key})['id']

    def checkout(self,customer,workspace,instance,plan,operation,origin):
        self.verify_prices()
        s=self.client.v1.checkout.sessions.create({'mode':'subscription','customer':customer,
            'line_items':[{'price':self.price_ids()[plan],'quantity':1}],
            'metadata':{'afterspan_workspace':workspace,'afterspan_instance':instance,'afterspan_operation':operation},
            'subscription_data':{'metadata':{'afterspan_workspace':workspace,'afterspan_instance':instance}},
            'success_url':origin+'/app?billing_return=checkout#workspace='+workspace+'&view=billing','cancel_url':origin+'/app?billing_return=cancelled#workspace='+workspace+'&view=billing',
            'customer_update':{'address':'auto'},'automatic_tax':{'enabled':os.environ.get('AFTERSPAN_STRIPE_AUTOMATIC_TAX')=='true'},'billing_address_collection':'required'},
            options={'idempotency_key':'checkout:'+operation})
        return {'id':s['id'],'url':safe_url(s['url']),'expires':s['expires_at']}

    def expire(self,session):
        result=self.client.v1.checkout.sessions.retrieve(session)
        if result['status']=='open':self.client.v1.checkout.sessions.expire(session)
        elif result['status']=='complete':raise ValueError('checkout already completed; refresh billing')

    def portal_configuration(self):
        allowed=self.verify_prices();config=os.environ.get('AFTERSPAN_STRIPE_PORTAL_CONFIGURATION','')
        if not config.startswith('bpc_'):raise HTTPException(503,'configure a restricted Stripe billing portal')
        c=self.client.v1.billing_portal.configurations.retrieve(config,{'expand':['features.subscription_update.products']});f=c.get('features') or {};cancel=f.get('subscription_cancel') or {};update=f.get('subscription_update') or {}
        configured={(p['product'],pid) for p in update.get('products',[]) for pid in p.get('prices',[])}
        expected={(p['product'],pid) for p in allowed for pid in p['prices'] if pid==self.price_ids()['team']}
        conditions=(update.get('schedule_at_period_end') or {}).get('conditions',[])
        if not c.get('active') or c.get('livemode')!=self.live or not cancel.get('enabled') or cancel.get('mode')!='at_period_end' or not update.get('enabled') or set(update.get('default_allowed_updates',[]))!={'price'} or configured!=expected or update.get('proration_behavior')!='always_invoice' or {condition.get('type') for condition in conditions}!={'decreasing_item_amount'}:
            raise ValueError('portal must restrict prices, invoice upgrades, and schedule downgrades/cancellation at period end')
        return config

    def portal(self,customer,origin,workspace,operation,action='manage',subscription=None):
        config=self.portal_configuration()
        back=origin+'/app?billing_return=portal#workspace='+workspace+'&view=billing'
        params={'customer':customer,'configuration':config,'return_url':back}
        if action!='manage':
            flow={'type':{'cancel':'subscription_cancel','change_plan':'subscription_update','payment_method':'payment_method_update'}[action],'after_completion':{'type':'redirect','redirect':{'return_url':back}}}
            if action in {'cancel','change_plan'}:
                s=self.client.v1.subscriptions.retrieve(subscription)
                if s.get('customer')!=customer or s.get('livemode')!=self.live:raise ValueError('subscription ownership mismatch')
                flow[flow['type']]={'subscription':subscription}
            params['flow_data']=flow
        s=self.client.v1.billing_portal.sessions.create(params,options={'idempotency_key':'portal:'+operation})
        return safe_url(s['url'])

    def downgrade(self,customer,subscription,operation):
        self.verify_prices();s=self.client.v1.subscriptions.retrieve(subscription);items=s['items']['data']
        if s.get('customer')!=customer or s.get('livemode')!=self.live or s['status']!='active' or s.get('cancel_at_period_end') or s.get('cancel_at') or s.get('pending_update') or s.get('discounts') or len(items)!=1 or items[0]['price']['id']!=self.price_ids()['team'] or items[0].get('quantity')!=1:raise ValueError('only an active plain Team subscription can schedule a Builder downgrade')
        schedule=self.client.v1.subscription_schedules.create({'from_subscription':subscription},options={'idempotency_key':'downgrade:create:'+operation})
        sid=schedule['id'];end=items[0]['current_period_end'];start=items[0]['current_period_start']
        result=self.client.v1.subscription_schedules.update(sid,{'end_behavior':'release','proration_behavior':'none','metadata':{'afterspan_operation':operation},'phases':[
            {'start_date':start,'end_date':end,'items':[{'price':self.price_ids()['team'],'quantity':1}],'proration_behavior':'none'},
            {'start_date':end,'duration':{'interval':'month','interval_count':1},'items':[{'price':self.price_ids()['builder'],'quantity':1}],'proration_behavior':'none'}]},options={'idempotency_key':'downgrade:update:'+operation})
        return {'id':result['id'],'effective_at':end}

    def snapshot(self,customer,workspace,instance):
        owner=self.client.v1.customers.retrieve(customer)
        if owner.get('deleted') or owner.get('livemode')!=self.live or owner.get('metadata',{}).get('afterspan_workspace')!=workspace or owner.get('metadata',{}).get('afterspan_instance')!=instance:raise ValueError('customer ownership mismatch')
        subs=self.client.v1.subscriptions.list({'customer':customer,'status':'all','limit':100,'expand':['data.latest_invoice']})
        active=[s for s in subs['data'] if s['status'] not in {'canceled','incomplete_expired'}]
        if subs.get('has_more') or len(active)>1:raise ValueError('multiple subscriptions require operator review')
        if not active:return {'status':'canceled' if subs['data'] else 'none','subscription':None,'plan':None,'paid':False,'paid_until':0,'cancel_at_period_end':False}
        s=active[0];items=s['items']['data'];ids=self.price_ids()
        if len(items)!=1 or items[0].get('quantity')!=1:raise ValueError('unsupported subscription items')
        item=items[0];plan=next((name for name,pid in ids.items() if item['price']['id']==pid),None)
        if not plan:raise ValueError('subscription price is outside the catalog')
        invoice=s.get('latest_invoice');invoice=self.client.v1.invoices.retrieve(invoice) if isinstance(invoice,str) else invoice
        paid=bool(invoice and invoice.get('status')=='paid' and invoice.get('customer')==customer)
        start=item.get('current_period_start',s.get('current_period_start'));end=item.get('current_period_end',s.get('current_period_end'))
        if not isinstance(start,(int,float)) or not isinstance(end,(int,float)) or end<=start:raise ValueError('missing subscription period')
        # Subscription state alone does not establish a paid entitlement.
        if paid:
            lines=(invoice.get('lines') or {}).get('data',[])
            matching=[line for line in lines if ((line.get('pricing') or {}).get('price_details') or {}).get('price')==ids[plan] or (line.get('price') or {}).get('id')==ids[plan]]
            if not matching or max((line.get('period') or {}).get('end',0) for line in matching)<end:paid=False
        future_plan=None;future_at=None
        if s.get('schedule'):
            schedule=self.client.v1.subscription_schedules.retrieve(s['schedule'] if isinstance(s['schedule'],str) else s['schedule']['id'])
            for phase in schedule.get('phases',[]):
                if phase['start_date']>=end and phase.get('items'):
                    price=phase['items'][0].get('price');pid=price if isinstance(price,str) else (price or {}).get('id')
                    future_plan=next((name for name,value in ids.items() if value==pid),None);future_at=phase['start_date'];break
        return {'status':s['status'],'subscription':s['id'],'plan':plan,'paid':paid,'paid_until':end if paid else 0,'period_start':start,'period_end':end,'cancel_at_period_end':bool(s.get('cancel_at_period_end') or s.get('cancel_at')),'scheduled_plan':future_plan,'scheduled_at':future_at,'invoice_id':invoice.get('id') if invoice else None}

    def invoices(self,customer):
        response=self.client.v1.invoices.list({'customer':customer,'limit':24})
        return [{'id':i['id'],'number':i.get('number'),'status':i['status'],'currency':i['currency'],'total':i['total'],'amount_paid':i['amount_paid'],'created':i['created'],'url':safe_url(i['hosted_invoice_url']) if i.get('hosted_invoice_url') else None,'test':not self.live} for i in response['data']]
