"""Workspace usage ledger, bounded plans and payment-confirmed entitlements."""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import uuid
from contextlib import closing
from datetime import datetime,timezone
from typing import Literal
from urllib.parse import urlencode

import stripe
from fastapi import HTTPException,Request
from pydantic import BaseModel,ConfigDict,Field
from payments import PLANS,CATALOG_VERSION,StripeProvider

class Model(BaseModel):model_config=ConfigDict(extra='forbid')
class Checkout(Model):
    plan:Literal['builder','team']
    request_id:uuid.UUID=Field(default_factory=uuid.uuid4)
class Operation(Model):
    request_id:uuid.UUID=Field(default_factory=uuid.uuid4)
    action:Literal['manage','cancel','change_plan','payment_method']='manage'
class Downgrade(Model):
    request_id:uuid.UUID
    confirm_plan:Literal['builder']
class Simulation(Model):
    action:Literal['pay','fail','cancel_at_period_end','resume','renew','renew_fail','expire','change_builder','change_team']
    operation:str|None=Field(default=None,max_length=100)

def month_window(now=None):
    d=datetime.fromtimestamp(time.time() if now is None else now,timezone.utc);start=datetime(d.year,d.month,1,tzinfo=timezone.utc)
    end=datetime(d.year+int(d.month==12),1 if d.month==12 else d.month+1,1,tzinfo=timezone.utc)
    return start.strftime('%Y-%m'),start.timestamp(),end.timestamp()
def iso(value):return datetime.fromtimestamp(value,timezone.utc).isoformat()

class Billing:
    def __init__(self,connect,accounts):self.connect=connect;self.accounts=accounts;self.lock=threading.RLock()
    def self_hosted(self):return os.environ.get('AFTERSPAN_EDITION','self_hosted')=='self_hosted'
    def mode(self):return 'disabled' if self.self_hosted() else os.environ.get('AFTERSPAN_BILLING_MODE','disabled')
    def local(self):
        _,local=self.accounts.origin()
        if self.mode()!='local' or not local:raise HTTPException(404,'local billing simulation is unavailable')
    def configured(self):
        mode=self.mode()
        if mode=='local':self.local();return
        if mode not in {'stripe_test','stripe_live'}:raise HTTPException(503,'billing provider is not configured')
        self.accounts.origin();StripeProvider(mode).price_ids()
        if not os.environ.get('AFTERSPAN_STRIPE_WEBHOOK_SECRET','').startswith('whsec_'):raise HTTPException(503,'configure the Stripe webhook secret')
    def init_db(self,c):
        c.executescript('''
        CREATE TABLE IF NOT EXISTS billing_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS billing_accounts(workspace TEXT PRIMARY KEY REFERENCES workspaces(id),started REAL NOT NULL,
          base_plan TEXT NOT NULL,provider_mode TEXT,customer TEXT UNIQUE,subscription TEXT,status TEXT NOT NULL DEFAULT 'none',
          paid_plan TEXT,paid_until REAL NOT NULL DEFAULT 0,cancel_at_end INTEGER NOT NULL DEFAULT 0,
          scheduled_plan TEXT,scheduled_at REAL,last_synced REAL,sync_error TEXT);
        CREATE TABLE IF NOT EXISTS usage_receipts(project TEXT NOT NULL REFERENCES projects(id),event_hash TEXT NOT NULL,
          workspace TEXT NOT NULL REFERENCES workspaces(id),received REAL NOT NULL,kind TEXT NOT NULL,PRIMARY KEY(project,event_hash));
        CREATE INDEX IF NOT EXISTS usage_receipt_retention ON usage_receipts(received);
        CREATE TABLE IF NOT EXISTS usage_months(workspace TEXT NOT NULL REFERENCES workspaces(id),month TEXT NOT NULL,
          events INTEGER NOT NULL DEFAULT 0,bytes INTEGER NOT NULL DEFAULT 0,system_events INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(workspace,month));
        CREATE TABLE IF NOT EXISTS usage_days(workspace TEXT NOT NULL,month TEXT NOT NULL,day TEXT NOT NULL,
          project TEXT NOT NULL REFERENCES projects(id),events INTEGER NOT NULL DEFAULT 0,bytes INTEGER NOT NULL DEFAULT 0,
          system_events INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(workspace,day,project));
        CREATE TABLE IF NOT EXISTS billing_operations(id TEXT PRIMARY KEY,workspace TEXT NOT NULL REFERENCES workspaces(id),
          kind TEXT NOT NULL,plan TEXT,request_id TEXT NOT NULL,created REAL NOT NULL,state TEXT NOT NULL,
          provider_id TEXT,url TEXT,expires REAL,UNIQUE(workspace,kind,request_id));
        CREATE INDEX IF NOT EXISTS billing_operation_workspace ON billing_operations(workspace,created);
        CREATE TABLE IF NOT EXISTS billing_webhooks(id TEXT PRIMARY KEY,mode TEXT NOT NULL,customer TEXT NOT NULL,
          event_type TEXT NOT NULL,received REAL NOT NULL,state TEXT NOT NULL DEFAULT 'queued',attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS billing_local(customer TEXT PRIMARY KEY,workspace TEXT UNIQUE NOT NULL REFERENCES workspaces(id),snapshot TEXT NOT NULL,invoices TEXT NOT NULL);
        ''')
        for key,value in [('instance',uuid.uuid4().hex),('receipt_key',secrets.token_hex(32))]:c.execute('INSERT OR IGNORE INTO billing_settings VALUES(?,?)',(key,value))
        for row in c.execute('SELECT id FROM workspaces').fetchall():self.ensure(c,row['id'])
        if not c.execute("SELECT 1 FROM billing_settings WHERE key='ledger_seeded'").fetchone():
            # Existing retained events establish deduplication only. Never backdate a bill.
            rows=c.execute('SELECT e.project,e.id,e.received,p.workspace FROM events e JOIN projects p ON p.id=e.project')
            while batch:=rows.fetchmany(500):
                c.executemany('INSERT OR IGNORE INTO usage_receipts VALUES(?,?,?,?,?)',[(e['project'],self.event_hash(c,e['project'],e['id']),e['workspace'],e['received'],'before_metering') for e in batch])
            c.execute("INSERT INTO billing_settings VALUES('ledger_seeded',?)",(str(time.time()),))
    def ensure(self,c,workspace):
        c.execute('INSERT OR IGNORE INTO billing_accounts(workspace,started,base_plan) VALUES(?,?,?)',(workspace,time.time(),'legacy' if workspace=='legacy' else 'free'))
    def event_hash(self,c,project,event_id):
        key=c.execute("SELECT value FROM billing_settings WHERE key='receipt_key'").fetchone()[0]
        return hmac.new(key.encode(),(project+'\0'+event_id).encode(),hashlib.sha256).hexdigest()
    def seen(self,c,project,event_id):return c.execute('SELECT 1 FROM usage_receipts WHERE project=? AND event_hash=?',(project,self.event_hash(c,project,event_id))).fetchone() is not None
    def effective(self,row,now=None):
        if self.self_hosted():return 'self_hosted'
        now=time.time() if now is None else now
        compatible=row['provider_mode']==self.mode() or self.mode()=='disabled' and row['provider_mode'] in {'stripe_test','stripe_live'}
        return row['paid_plan'] if compatible and row['paid_plan'] in PLANS and row['paid_until']>now and row['status'] not in {'canceled','unpaid','incomplete_expired'} else row['base_plan']
    def row(self,c,workspace):
        row=c.execute('SELECT * FROM billing_accounts WHERE workspace=?',(workspace,)).fetchone()
        if row is None:raise HTTPException(503,'workspace metering is not initialized')
        return row
    def counts(self,c,workspace,exclude_invite=None):
        projects=c.execute('SELECT count(*) FROM projects WHERE workspace=?',(workspace,)).fetchone()[0]
        seats=c.execute('SELECT count(*) FROM memberships WHERE workspace=?',(workspace,)).fetchone()[0]
        pending=c.execute('SELECT count(*) FROM workspace_invites WHERE workspace=? AND used IS NULL AND revoked IS NULL AND expires>? AND id!=?',(workspace,time.time(),exclude_invite or '')).fetchone()[0]
        return {'projects':projects,'members':seats,'reserved_invites':pending,'seats':seats+pending}
    def capacity(self,c,workspace,kind,exclude_invite=None):
        row=self.row(c,workspace);plan=PLANS[self.effective(row)];used=self.counts(c,workspace,exclude_invite)
        if plan[kind] is not None and used[kind]+1>plan[kind]:raise HTTPException(409,'workspace '+kind+' allowance reached; an owner can review Usage & billing')
    def record(self,c,project,event_id,size,system,now):
        workspace=c.execute('SELECT workspace FROM projects WHERE id=?',(project,)).fetchone()[0];row=self.row(c,workspace);plan=PLANS[self.effective(row,now)];month,start,end=month_window(now)
        current=c.execute('SELECT * FROM usage_months WHERE workspace=? AND month=?',(workspace,month)).fetchone();used=dict(current) if current else {'events':0,'bytes':0}
        if plan['events'] is not None and used['events']+int(not system)>plan['events'] or plan['bytes'] is not None and used['bytes']+size>plan['bytes']:
            raise HTTPException(429,'workspace collection allowance reached; ask an owner to review Usage & billing',headers={'Retry-After':str(max(1,int(end-now)))})
        day=datetime.fromtimestamp(now,timezone.utc).strftime('%Y-%m-%d')
        if system:
            n=c.execute('SELECT coalesce(sum(system_events),0) FROM usage_days WHERE workspace=? AND day=?',(workspace,day)).fetchone()[0]
            cap=max(1,self.counts(c,workspace)['projects'])*2000
            if n>=cap:raise HTTPException(429,'daily internal telemetry allowance reached',headers={'Retry-After':'3600'})
        c.execute('INSERT INTO usage_receipts VALUES(?,?,?,?,?)',(project,self.event_hash(c,project,event_id),workspace,now,'system' if system else 'counted'))
        c.execute('INSERT INTO usage_months VALUES(?,?,?,?,?) ON CONFLICT(workspace,month) DO UPDATE SET events=events+excluded.events,bytes=bytes+excluded.bytes,system_events=system_events+excluded.system_events',(workspace,month,int(not system),size,int(system)))
        c.execute('INSERT INTO usage_days VALUES(?,?,?,?,?,?,?) ON CONFLICT(workspace,day,project) DO UPDATE SET events=events+excluded.events,bytes=bytes+excluded.bytes,system_events=system_events+excluded.system_events',(workspace,month,day,project,int(not system),size,int(system)))
    def usage(self,workspace,month=None):
        current,start,end=month_window()
        if month:
            if not re.fullmatch(r'\d{4}-\d{2}',month):raise HTTPException(422,'use YYYY-MM')
            try:at=datetime.strptime(month,'%Y-%m').replace(tzinfo=timezone.utc).timestamp()
            except ValueError:raise HTTPException(422,'invalid month') from None
            if at>start or at<time.time()-400*86400:raise HTTPException(422,'month outside retained usage history')
            _,start,end=month_window(at)
        else:month=current
        with closing(self.connect()) as c:
            row=self.row(c,workspace);plan_name=self.effective(row);plan=PLANS[plan_name];totals=c.execute('SELECT events,bytes,system_events FROM usage_months WHERE workspace=? AND month=?',(workspace,month)).fetchone()
            counts=self.counts(c,workspace)
            projects=[dict(r) for r in c.execute('SELECT u.project,p.name,sum(u.events) AS events,sum(u.bytes) AS bytes,sum(u.system_events) AS system_events FROM usage_days u JOIN projects p ON p.id=u.project WHERE u.workspace=? AND u.month=? GROUP BY u.project,p.name ORDER BY events DESC,p.id',(workspace,month))]
            days=[dict(r) for r in c.execute('SELECT day,sum(events) AS events,sum(bytes) AS bytes,sum(system_events) AS system_events FROM usage_days WHERE workspace=? AND month=? GROUP BY day ORDER BY day',(workspace,month))]
        t=dict(totals) if totals else {'events':0,'bytes':0,'system_events':0}
        return {'workspace':workspace,'month':month,'start':iso(start),'end':iso(end),'as_of':iso(time.time()),'metering_started_at':iso(row['started']),'partial':row['started']>start,
            'edition':'self_hosted' if self.self_hosted() else 'hosted','commercial_limits':not self.self_hosted(),'current_plan':plan_name,'limits':plan,'totals':t,'resources':counts,'projects':projects,'days':days,
            'remaining_events':None if plan['events'] is None else max(0,plan['events']-t['events']),
            'warnings':[kind+' allowance reached' for kind in ['events','bytes'] if plan[kind] is not None and t[kind]>=plan[kind]],
            'definition_version':'usage-v1','definitions':{'events':'New accepted product/agent events, deduplicated by stable event ID. Failed business attempts count as events, not extra successful journeys.','bytes':'Compact UTF-8 JSON after validation, including internal telemetry; not network traffic or disk size.','system_events':'Collector heartbeats and setup checks are excluded from the event allowance, but count toward payload volume and a daily safety cap.','period':'UTC calendar month by receipt time, separate from the subscription renewal date. Upgrades do not reset usage.','history':'Metering starts at integration, without backdated charges. Aggregate usage survives analytics retention/erasure. Hashed delivery receipts are retained for 400 days.','overage':'No automatic overage charges. Limits reject new collection atomically; duplicates remain acknowledged.'}}
    def catalog(self):
        if self.self_hosted():return {'edition':'self_hosted','mode':'disabled','license':'MIT','version':CATALOG_VERSION,'draft':False,'plans':{'self_hosted':PLANS['self_hosted']},'overage_charges':False}
        return {'mode':self.mode(),'sandbox_expires_on':os.environ.get('AFTERSPAN_TEST_SANDBOX_EXPIRES') if self.mode()=='stripe_test' else None,'version':CATALOG_VERSION,'draft':self.mode()!='stripe_live','currency':'usd','plans':{k:v for k,v in PLANS.items() if k not in {'legacy','self_hosted'}},'usage_period':'UTC calendar month','overage_charges':False,'tax':'Prices exclude applicable tax; the hosted checkout shows the amount before payment.'}
    def status(self,workspace):
        with closing(self.connect()) as c:
            row=self.row(c,workspace);pending=c.execute("SELECT id,plan,state,url,expires FROM billing_operations WHERE workspace=? AND kind='checkout' AND state IN ('creating','open') ORDER BY created DESC LIMIT 1",(workspace,)).fetchone()
            events=c.execute("SELECT count(*) FROM billing_webhooks WHERE customer=? AND mode=? AND state!='processed'",(row['customer'],row['provider_mode'])).fetchone()[0]
        ready=True
        try:self.configured()
        except HTTPException:ready=False
        return {'workspace':workspace,'mode':self.mode(),'configured':ready,'catalog_version':CATALOG_VERSION,'current_plan':self.effective(row),'provider_status':row['status'],'has_subscription':bool(row['subscription']),'paid_until':row['paid_until'] or None,'cancel_at_period_end':bool(row['cancel_at_end']),'scheduled_plan':row['scheduled_plan'],'scheduled_at':row['scheduled_at'],'last_synced_at':row['last_synced'],'sync_error':row['sync_error'],'pending_webhooks':events,'pending_checkout':dict(pending) if pending else None,'human_payment_required':True,'no_automatic_overages':True}
    def quote(self,workspace,plan):
        usage=self.usage(workspace);target=PLANS[plan]
        return {'plan':plan,'catalog_version':CATALOG_VERSION,'monthly_cents':target['monthly_cents'],'currency':target['currency'],'limits':target,
            'current_usage':usage['totals'],'resources':usage['resources'],'over_limit':[k for k in ['events','bytes'] if usage['totals'][k]>(target[k] if target[k] is not None else float('inf'))]+[k for k in ['projects','seats'] if usage['resources'][k]>target[k]],
            'terms':['Usage stays in the current UTC calendar month; a plan change does not reset it.','Existing data and members are kept after a downgrade. New collection/resources can be blocked above the new limits.','Only a provider-confirmed paid subscription enables a paid plan. A return URL is not a payment receipt.','Checkout/portal displays the payment, proration and tax before the person confirms. No application endpoint charges a saved card.']}
    def profile(self,workspace):
        self.configured()
        with closing(self.connect()) as c:
            row=dict(self.row(c,workspace));instance=c.execute("SELECT value FROM billing_settings WHERE key='instance'").fetchone()[0]
        if row['provider_mode'] and row['provider_mode']!=self.mode():raise HTTPException(409,'billing profile belongs to another environment; an operator must migrate it')
        if row['customer']:return row,instance
        try:
            customer='cus_local_'+uuid.uuid4().hex if self.mode()=='local' else StripeProvider(self.mode()).customer(workspace,instance,'customer:'+instance+':'+workspace)
        except HTTPException:raise
        except Exception:raise HTTPException(502,'billing customer could not be confirmed; retry safely') from None
        with closing(self.connect()) as c,c:
            c.execute('UPDATE billing_accounts SET customer=?,provider_mode=? WHERE workspace=?',(customer,self.mode(),workspace))
            if self.mode()=='local':c.execute('INSERT INTO billing_local VALUES(?,?,?,?)',(customer,workspace,json.dumps({'status':'none','plan':None,'paid':False,'paid_until':0,'cancel_at_period_end':False}), '[]'))
            row=dict(self.row(c,workspace))
        return row,instance
    def checkout(self,workspace,req,p):
        with self.lock:
            row,instance=self.profile(workspace)
            self.refresh(workspace)
            with closing(self.connect()) as c,c:
                c.execute('BEGIN IMMEDIATE')
                row=self.row(c,workspace)
                if row['subscription'] and row['status'] not in {'canceled','incomplete_expired','none'}:raise HTTPException(409,'use the billing portal for an existing subscription')
                op=c.execute("SELECT * FROM billing_operations WHERE workspace=? AND kind='checkout' AND state IN ('creating','open') ORDER BY created DESC LIMIT 1",(workspace,)).fetchone()
                if op and op['state']=='open' and op['expires']<=time.time():
                    c.execute("UPDATE billing_operations SET state='expired' WHERE id=?",(op['id'],));op=None
                if op and op['plan']!=req.plan:raise HTTPException(409,'close the pending checkout before choosing another plan')
                if not op:
                    existing=c.execute("SELECT * FROM billing_operations WHERE workspace=? AND kind='checkout' AND request_id=?",(workspace,str(req.request_id))).fetchone()
                    if existing:raise HTTPException(409,'request already completed or expired; start a new request')
                    oid=uuid.uuid4().hex;c.execute('INSERT INTO billing_operations VALUES(?,?,?,?,?,?,?,?,?,?)',(oid,workspace,'checkout',req.plan,str(req.request_id),time.time(),'creating',None,None,None))
                    op=c.execute('SELECT * FROM billing_operations WHERE id=?',(oid,)).fetchone()
                if op['state']=='open':return {'operation':op['id'],'url':op['url'],'mode':self.mode(),'expires':op['expires']}
            origin=self.accounts.origin()[0]
            try:
                receipt={'id':'cs_local_'+op['id'],'url':origin+'/billing-test?'+urlencode({'workspace':workspace,'operation':op['id']}),'expires':time.time()+3600} if self.mode()=='local' else StripeProvider(self.mode()).checkout(row['customer'],workspace,instance,req.plan,op['id'],origin)
            except Exception as exc:
                if isinstance(exc,HTTPException):raise
                raise HTTPException(502,'checkout could not be confirmed; retry the same plan to resume safely') from None
            with closing(self.connect()) as c,c:
                c.execute("UPDATE billing_operations SET state='open',provider_id=?,url=?,expires=? WHERE id=?",(receipt['id'],receipt['url'],receipt['expires'],op['id']));self.accounts.audit(c,workspace,p,'billing.checkout_created',workspace,req.plan)
            return {'operation':op['id'],'url':receipt['url'],'expires':receipt['expires'],'mode':self.mode()}
    def close_checkout(self,workspace,p):
        with self.lock:
            with closing(self.connect()) as c:op=c.execute("SELECT * FROM billing_operations WHERE workspace=? AND kind='checkout' AND state IN ('creating','open') ORDER BY created DESC LIMIT 1",(workspace,)).fetchone()
            if not op:return {'closed':False}
            if op['state']=='creating':raise HTTPException(409,'resume the pending checkout before closing it')
            if self.mode()!='local':
                try:StripeProvider(self.mode()).expire(op['provider_id'])
                except Exception:raise HTTPException(409,'checkout may have completed; refresh billing before trying again') from None
            with closing(self.connect()) as c,c:c.execute("UPDATE billing_operations SET state='expired' WHERE id=?",(op['id'],));self.accounts.audit(c,workspace,p,'billing.checkout_closed',workspace)
            return {'closed':True}
    def portal(self,workspace,req,p):
        with self.lock:
            row,instance=self.profile(workspace);origin=self.accounts.origin()[0]
            if not row['subscription']:raise HTTPException(409,'complete a subscription before opening its billing portal')
            try:url=origin+'/billing-test?'+urlencode({'workspace':workspace,'portal':'1','action':req.action}) if self.mode()=='local' else StripeProvider(self.mode()).portal(row['customer'],origin,workspace,str(req.request_id),req.action,row['subscription'])
            except HTTPException:raise
            except Exception:raise HTTPException(502,'billing portal is unavailable; check its configured prices and policy') from None
            with closing(self.connect()) as c,c:self.accounts.audit(c,workspace,p,'billing.portal_opened',workspace)
            return {'url':url,'mode':self.mode(),'human_confirmation_required':True}
    def downgrade(self,workspace,req,p):
        with self.lock:
            self.refresh(workspace)
            with closing(self.connect()) as c,c:
                c.execute('BEGIN IMMEDIATE');row=dict(self.row(c,workspace))
                if self.effective(row)!='team' or row['status']!='active' or row['cancel_at_end']:raise HTTPException(409,'an active Team subscription without pending cancellation is required')
                if row['scheduled_plan']=='builder':return self.status(workspace)
                op=c.execute("SELECT * FROM billing_operations WHERE workspace=? AND kind='downgrade' AND state='creating' ORDER BY created LIMIT 1",(workspace,)).fetchone()
                if not op:
                    oid=uuid.uuid4().hex
                    if c.execute("SELECT 1 FROM billing_operations WHERE workspace=? AND kind='downgrade' AND request_id=?",(workspace,str(req.request_id))).fetchone():raise HTTPException(409,'request already used; use a new UUID')
                    c.execute('INSERT INTO billing_operations VALUES(?,?,?,?,?,?,?,?,?,?)',(oid,workspace,'downgrade','builder',str(req.request_id),time.time(),'creating',None,None,None))
                else:oid=op['id']
            try:
                if self.mode()=='local':self.simulate(workspace,Simulation(action='change_builder'),p)
                else:StripeProvider(self.mode()).downgrade(row['customer'],row['subscription'],oid)
            except Exception:raise HTTPException(502,'downgrade is not yet confirmed; retry safely or refresh before changing the subscription') from None
            with closing(self.connect()) as c,c:
                c.execute("UPDATE billing_operations SET state='complete' WHERE id=?",(oid,));self.accounts.audit(c,workspace,p,'billing.downgrade_scheduled',workspace,'builder')
            return self.refresh(workspace)

    def refresh(self,workspace):
        with self.lock:
            cutoff=time.time()
            with closing(self.connect()) as c:
                row=dict(self.row(c,workspace));instance=c.execute("SELECT value FROM billing_settings WHERE key='instance'").fetchone()[0]
                if not row['customer']:return self.status(workspace)
                if row['provider_mode']!=self.mode():raise HTTPException(409,'billing environment mismatch')
                local=c.execute('SELECT snapshot FROM billing_local WHERE customer=?',(row['customer'],)).fetchone() if self.mode()=='local' else None
            try:snapshot=json.loads(local['snapshot']) if local else StripeProvider(self.mode()).snapshot(row['customer'],workspace,instance)
            except Exception:
                with closing(self.connect()) as c,c:c.execute("UPDATE billing_accounts SET sync_error='Provider refresh failed. Retry or contact the service operator.' WHERE workspace=?",(workspace,))
                raise HTTPException(502,'provider state could not be verified; the last confirmed entitlement was preserved') from None
            with closing(self.connect()) as c,c:
                previous=self.row(c,workspace);until=previous['paid_until'];paid_plan=previous['paid_plan'];base=previous['base_plan']
                if snapshot['status'] in {'canceled','unpaid','incomplete_expired'}:until=0
                elif snapshot.get('paid') and snapshot.get('plan') in {'builder','team'}:
                    until=snapshot['paid_until'];paid_plan=snapshot['plan'];base='free'
                c.execute('UPDATE billing_accounts SET base_plan=?,subscription=?,status=?,paid_plan=?,paid_until=?,cancel_at_end=?,scheduled_plan=?,scheduled_at=?,last_synced=?,sync_error=NULL WHERE workspace=?',
                    (base,snapshot.get('subscription'),snapshot['status'],paid_plan,until,int(snapshot.get('cancel_at_period_end',False)),snapshot.get('scheduled_plan'),snapshot.get('scheduled_at'),time.time(),workspace))
                c.execute("UPDATE billing_webhooks SET state='processed' WHERE customer=? AND mode=? AND received<=?",(row['customer'],self.mode(),cutoff))
                if snapshot.get('subscription') and snapshot['status'] not in {'canceled','incomplete_expired'}:c.execute("UPDATE billing_operations SET state='complete' WHERE workspace=? AND kind='checkout' AND state IN ('open','creating')",(workspace,))
                if self.effective(previous)!=self.effective(self.row(c,workspace)):self.accounts.audit(c,workspace,{'id':'billing-provider'},'billing.entitlement_changed',workspace,self.effective(self.row(c,workspace)))
            return self.status(workspace)
    def invoices(self,workspace):
        with closing(self.connect()) as c:
            row=self.row(c,workspace)
            if not row['customer']:return {'invoices':[],'mode':self.mode()}
            if row['provider_mode']!=self.mode():raise HTTPException(409,'billing environment mismatch')
            local=c.execute('SELECT invoices FROM billing_local WHERE customer=?',(row['customer'],)).fetchone() if self.mode()=='local' else None
        try:items=json.loads(local['invoices']) if local else StripeProvider(self.mode()).invoices(row['customer'])
        except Exception:raise HTTPException(502,'invoice history is unavailable') from None
        return {'invoices':items,'mode':self.mode(),'limit':24}
    def webhook(self,body,signature):
        self.configured();secret=os.environ.get('AFTERSPAN_LOCAL_WEBHOOK_SECRET' if self.mode()=='local' else 'AFTERSPAN_STRIPE_WEBHOOK_SECRET','')
        if not secret:raise HTTPException(503,'webhook secret is not configured')
        try:
            event=stripe.Webhook.construct_event(body,signature,secret,tolerance=300)
            if event.get('livemode') is not (self.mode()=='stripe_live') or not re.fullmatch(r'evt_[A-Za-z0-9_]{1,200}',event['id']):raise ValueError('invalid mode or event')
            if event['type'] not in {'checkout.session.completed','checkout.session.async_payment_succeeded','checkout.session.async_payment_failed','customer.subscription.created','customer.subscription.updated','customer.subscription.deleted','invoice.paid','invoice.payment_failed','invoice.voided','invoice.marked_uncollectible'}:return {'received':True,'ignored':True}
            customer=event['data']['object']['customer']
            if not isinstance(customer,str) or not re.fullmatch(r'cus_[A-Za-z0-9_]{1,200}',customer):raise ValueError('invalid customer')
        except Exception:raise HTTPException(400,'webhook signature or payload is invalid') from None
        with closing(self.connect()) as c,c:
            if not c.execute('SELECT 1 FROM billing_accounts WHERE customer=? AND provider_mode=?',(customer,self.mode())).fetchone():return {'received':True,'ignored':True}
            if c.execute('SELECT 1 FROM billing_webhooks WHERE id=?',(event['id'],)).fetchone():return {'received':True,'duplicate':True}
            if c.execute("SELECT count(*) FROM billing_webhooks WHERE state='queued'").fetchone()[0]>=1000:raise HTTPException(503,'webhook queue is busy; retry delivery')
            changed=c.execute('INSERT OR IGNORE INTO billing_webhooks(id,mode,customer,event_type,received,next_attempt) VALUES(?,?,?,?,?,?)',(event['id'],self.mode(),customer,event['type'],time.time(),time.time())).rowcount
        return {'received':True,'duplicate':not bool(changed)}
    def work(self):
        with closing(self.connect()) as c:jobs=c.execute("SELECT b.workspace,w.id,w.attempts FROM billing_webhooks w JOIN billing_accounts b ON b.customer=w.customer AND b.provider_mode=w.mode WHERE w.state='queued' AND w.mode=? AND w.next_attempt<=? ORDER BY w.received LIMIT 3",(self.mode(),time.time())).fetchall()
        for job in jobs:
            try:self.refresh(job['workspace'])
            except Exception:
                with closing(self.connect()) as c,c:c.execute("UPDATE billing_webhooks SET attempts=attempts+1,next_attempt=?,state=CASE WHEN attempts>=9 THEN 'failed' ELSE 'queued' END WHERE id=? AND state='queued'",(time.time()+min(3600,5*2**job['attempts']),job['id']))
    def simulate(self,workspace,req,p):
        self.local()
        with self.lock:
            row,_=self.profile(workspace);now=time.time();operation=None
            with closing(self.connect()) as c,c:
                local=c.execute('SELECT * FROM billing_local WHERE workspace=?',(workspace,)).fetchone();snap=json.loads(local['snapshot']);invoices=json.loads(local['invoices'])
                if req.action in {'pay','fail'}:
                    operation=c.execute("SELECT * FROM billing_operations WHERE workspace=? AND id=? AND kind='checkout' AND state='open' AND expires>?",(workspace,req.operation or '',now)).fetchone()
                    if not operation:raise HTTPException(409,'test checkout is closed, expired or belongs to another workspace')
                    if req.action=='fail':return {'simulation':True,'outcome':'payment_failed','note':'No entitlement was granted; no real payment was attempted.'}
                    if snap.get('subscription') and snap['status'] not in {'canceled','none'}:raise HTTPException(409,'subscription already exists')
                    snap={'status':'active','subscription':'sub_local_'+uuid.uuid4().hex,'plan':operation['plan'],'paid':True,'paid_until':now+30*86400,'period_start':now,'period_end':now+30*86400,'cancel_at_period_end':False}
                else:
                    if not snap.get('subscription'):raise HTTPException(409,'no test subscription exists')
                    if req.action=='cancel_at_period_end':snap['cancel_at_period_end']=True
                    if req.action=='resume':snap['cancel_at_period_end']=False
                    if req.action=='expire':snap.update(status='canceled',paid=False,paid_until=0)
                    if req.action=='renew_fail':snap.update(status='past_due',paid=False)
                    if req.action=='renew':snap.update(plan=snap.get('scheduled_plan') or snap['plan'],scheduled_plan=None,scheduled_at=None,status='active',paid=True,paid_until=now+30*86400,period_start=now,period_end=now+30*86400)
                    if req.action=='change_team':snap.update(plan='team',status='active',paid=True,scheduled_plan=None,scheduled_at=None)
                    if req.action=='change_builder':snap.update(scheduled_plan='builder',scheduled_at=snap['paid_until'])
                if req.action in {'pay','renew','change_team'}:
                    plan=PLANS[snap['plan']];invoices.insert(0,{'id':'in_local_'+uuid.uuid4().hex,'number':'LOCAL TEST','status':'paid','currency':'usd','total':plan['monthly_cents'],'amount_paid':plan['monthly_cents'],'created':now,'url':None,'test':True})
                if req.action=='renew_fail':invoices.insert(0,{'id':'in_local_'+uuid.uuid4().hex,'number':'LOCAL TEST','status':'open','currency':'usd','total':PLANS[snap['plan']]['monthly_cents'],'amount_paid':0,'created':now,'url':None,'test':True})
                c.execute('UPDATE billing_local SET snapshot=?,invoices=? WHERE workspace=?',(json.dumps(snap),json.dumps(invoices[:24]),workspace));self.accounts.audit(c,workspace,p,'billing.local_simulation',workspace,req.action)
            event={'id':'evt_local_'+uuid.uuid4().hex,'object':'event','livemode':False,'type':'invoice.paid' if req.action in {'pay','renew','change_team'} else 'customer.subscription.updated','data':{'object':{'customer':row['customer']}}}
            body=json.dumps(event).encode();stamp=str(int(time.time()));secret=os.environ.get('AFTERSPAN_LOCAL_WEBHOOK_SECRET','')
            signature='t='+stamp+',v1='+hmac.new(secret.encode(),stamp.encode()+b'.'+body,hashlib.sha256).hexdigest()
            self.webhook(body,signature);self.work()
            return {'simulation':True,'billing':self.status(workspace),'note':'Local provider simulation only. No funds moved and no Stripe request was made.'}
    def install(self,app):
        @app.get('/api/plans')
        def public_plans():return self.catalog()
        @app.post('/api/workspaces/{workspace}/billing/downgrade')
        def downgrade(workspace:str,req:Downgrade,request:Request):return self.downgrade(workspace,req,self.accounts.workspace_role(request,workspace,'owner'))
        @app.get('/api/workspaces/{workspace}/usage')
        def usage(workspace:str,request:Request,month:str|None=None):self.accounts.workspace_role(request,workspace);return self.usage(workspace,month)
        @app.get('/api/workspaces/{workspace}/plans')
        def plans(workspace:str,request:Request):self.accounts.workspace_role(request,workspace);return self.catalog()
        @app.get('/api/workspaces/{workspace}/billing')
        def status(workspace:str,request:Request):self.accounts.workspace_role(request,workspace,'owner');return self.status(workspace)
        @app.get('/api/workspaces/{workspace}/billing/invoices')
        def invoices(workspace:str,request:Request):self.accounts.workspace_role(request,workspace,'owner');return self.invoices(workspace)
        @app.get('/api/workspaces/{workspace}/billing/quote')
        def quote(workspace:str,request:Request,plan:Literal['free','builder','team']):self.accounts.workspace_role(request,workspace,'owner');return self.quote(workspace,plan)
        @app.post('/api/workspaces/{workspace}/billing/checkout')
        def checkout(workspace:str,req:Checkout,request:Request):return self.checkout(workspace,req,self.accounts.workspace_role(request,workspace,'owner'))
        @app.delete('/api/workspaces/{workspace}/billing/checkout')
        def close(workspace:str,request:Request):return self.close_checkout(workspace,self.accounts.workspace_role(request,workspace,'owner'))
        @app.post('/api/workspaces/{workspace}/billing/portal')
        def portal(workspace:str,req:Operation,request:Request):return self.portal(workspace,req,self.accounts.workspace_role(request,workspace,'owner'))
        @app.post('/api/workspaces/{workspace}/billing/refresh')
        def refresh(workspace:str,request:Request):self.accounts.workspace_role(request,workspace,'owner');return self.refresh(workspace)
        @app.post('/api/workspaces/{workspace}/billing/simulate')
        def simulate(workspace:str,req:Simulation,request:Request):return self.simulate(workspace,req,self.accounts.workspace_role(request,workspace,'owner'))
        @app.post('/api/billing/webhook')
        async def webhook(request:Request):return self.webhook(await request.body(),request.headers.get('stripe-signature',''))
