import hashlib
import hmac
import json
import os
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import MagicMock,patch
from fastapi import HTTPException
from fastapi.testclient import TestClient
import app
from billing import month_window
from payments import PLANS,StripeProvider,safe_url
import test_workspaces
CSRF=test_workspaces.CSRF

class BillingTests(unittest.TestCase):
    token=test_workspaces.WorkspaceTests.token;user=test_workspaces.WorkspaceTests.user;workspace=test_workspaces.WorkspaceTests.workspace;project=test_workspaces.WorkspaceTests.project;invite=test_workspaces.WorkspaceTests.invite;join=test_workspaces.WorkspaceTests.join;key=test_workspaces.WorkspaceTests.key
    def setUp(self):
        test_workspaces.WorkspaceTests.setUp(self)
        self.billing_env=patch.dict(os.environ,{'AFTERSPAN_EDITION':'hosted','AFTERSPAN_BILLING_MODE':'local','AFTERSPAN_LOCAL_WEBHOOK_SECRET':'local-test-secret-not-real'});self.billing_env.start()
        self.owner,_=self.user('billing-owner@example.test');self.w=self.workspace(self.owner);self.p=self.project(self.owner,self.w);self.base='/api/workspaces/'+self.w
    def tearDown(self):self.billing_env.stop();test_workspaces.WorkspaceTests.tearDown(self)
    def post(self,path,body=None,client=None):return (client or self.owner).post(self.base+path,headers=CSRF,json=body or {})
    def send(self,*events):return self.client.post('/v1/events',headers={'Authorization':'Bearer '+self.p['write_key']},json={'events':list(events)})
    def event(self,id=None,**kw):return {'id':id or uuid.uuid4().hex,'name':'task.completed',**kw}
    def usage(self):return self.owner.get(self.base+'/usage').json()
    def checkout(self,plan='builder',request_id=None):
        r=self.post('/billing/checkout',{'plan':plan,'request_id':request_id or str(uuid.uuid4())});self.assertEqual(r.status_code,200,r.text);return r.json()
    def pay(self,plan='builder'):
        op=self.checkout(plan);r=self.post('/billing/simulate',{'action':'pay','operation':op['operation']});self.assertEqual(r.status_code,200,r.text);return r.json()
    def signed(self,customer,id=None,stamp=None,live=False):
        body=json.dumps({'id':id or 'evt_'+uuid.uuid4().hex,'livemode':live,'type':'invoice.paid','data':{'object':{'customer':customer}}}).encode();stamp=str(stamp or int(time.time()));signature='t='+stamp+',v1='+hmac.new(b'local-test-secret-not-real',stamp.encode()+b'.'+body,hashlib.sha256).hexdigest();return body,signature
    def test_receipts_survive_raw_event_erasure_and_restart(self):
        e=self.event('stable',distinct_id='person');self.assertEqual(self.send(e).json()['accepted'],1);self.assertEqual(self.send(e).json()['duplicates'],1)
        with closing(app.connect()) as c,c:c.execute('DELETE FROM events WHERE project=?',(self.p['id'],))
        app.init_db();self.assertEqual(self.send(e).json()['accepted'],0);self.assertEqual(self.usage()['totals']['events'],1)
        with closing(app.connect()) as c:self.assertNotIn('stable',c.execute('SELECT event_hash FROM usage_receipts WHERE project=?',(self.p['id'],)).fetchone()[0])
    def test_atomic_quota_duplicate_ack_and_upgrade_keeps_usage(self):
        for offset in range(0,10000,100):self.assertEqual(self.send(*[self.event(str(i)) for i in range(offset,offset+100)]).status_code,200)
        self.assertEqual(self.usage()['remaining_events'],0)
        self.assertEqual(self.send(self.event('0')).json()['duplicates'],1)
        r=self.send(self.event('new'));self.assertEqual(r.status_code,429);self.assertGreater(int(r.headers['Retry-After']),0)
        self.pay();self.assertEqual(self.usage()['totals']['events'],10000);self.assertEqual(self.send(self.event('new')).status_code,200)
        self.assertEqual(self.usage()['totals']['events'],10001)
    def test_failed_batch_and_bytes_roll_back(self):
        with closing(app.connect()) as c,c:c.execute('INSERT INTO usage_months VALUES(?,?,?,?,?)',(self.w,month_window()[0],9999,0,0))
        r=self.send(self.event('batch-a'),self.event('batch-b'));self.assertEqual(r.status_code,429);self.assertEqual(self.usage()['totals']['events'],9999)
        with closing(app.connect()) as c,c:
            self.assertEqual(c.execute('SELECT count(*) FROM events WHERE project=?',(self.p['id'],)).fetchone()[0],0)
            c.execute('UPDATE usage_months SET events=0,bytes=? WHERE workspace=?',(PLANS['free']['bytes'],self.w))
        self.assertEqual(self.send(self.event('bytes')).status_code,429)
    def test_last_slot_concurrency(self):
        with closing(app.connect()) as c,c:c.execute('INSERT INTO usage_months VALUES(?,?,?,?,?)',(self.w,month_window()[0],9999,0,0))
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda i:self.send(self.event('race'+str(i))).status_code,range(2)))
        self.assertEqual(sorted(results),[200,429]);self.assertEqual(self.usage()['totals']['events'],10000)
    def test_internal_telemetry_bytes_and_month_boundary(self):
        self.send(self.event('health',name='analytics.pipeline'),self.event('check',name='afterspan.setup.checked'),self.event('failed',properties={'outcome':'failed'}))
        u=self.usage();self.assertEqual(u['totals']['events'],1);self.assertEqual(u['totals']['system_events'],2);self.assertGreater(u['totals']['bytes'],0)
        self.assertEqual(month_window(1767225599)[0],'2025-12');self.assertEqual(month_window(1767225600)[0],'2026-01')
        self.assertEqual(self.owner.get(self.base+'/usage?month=2099-01').status_code,422)
    def test_project_and_reserved_seat_limits_release(self):
        self.project(self.owner,self.w,'second');self.assertEqual(self.owner.post('/api/projects?workspace='+self.w,headers=CSRF,json={'id':'third','name':'Third'}).status_code,409)
        inv,_=self.invite(self.owner,self.w,'one@example.test');self.invite(self.owner,self.w,'two@example.test')
        self.assertEqual(self.post('/invites',{'email':'three@example.test','role':'reader'}).status_code,409)
        self.assertEqual(self.post('/invites/'+inv['id']+'/resend').status_code,202)
        self.owner.delete(self.base+'/invites/'+inv['id'],headers=CSRF);self.assertEqual(self.post('/invites',{'email':'three@example.test','role':'reader'}).status_code,202)
        self.assertEqual(self.usage()['resources']['seats'],3)
    def test_roles_keys_and_cross_workspace_privacy(self):
        reader,_=self.join(self.owner,self.w,'reader@example.test');other,_=self.user('other@example.test')
        for path in ['/billing','/billing/invoices','/billing/quote?plan=team']:
            self.assertEqual(reader.get(self.base+path).status_code,403);self.assertEqual(other.get(self.base+path).status_code,403)
        self.assertEqual(reader.get(self.base+'/usage').status_code,200);self.assertEqual(self.post('/billing/checkout',{'plan':'builder'},reader).status_code,403)
        key=self.key(self.owner,self.w,'owner');c=TestClient(app.app);headers={'Authorization':'Bearer '+key['key']}
        self.assertEqual(c.get(self.base+'/usage',headers=headers).status_code,200);self.assertEqual(c.get(self.base+'/billing',headers=headers).status_code,200)
    def test_checkout_idempotence_fail_and_return_url_not_receipt(self):
        op=self.checkout();self.assertEqual(self.checkout()['operation'],op['operation']);self.assertEqual(self.usage()['current_plan'],'free')
        self.assertEqual(self.post('/billing/checkout',{'plan':'team'}).status_code,409)
        r=self.post('/billing/simulate',{'action':'fail','operation':op['operation']});self.assertEqual(r.status_code,200);self.assertEqual(self.usage()['current_plan'],'free')
        self.assertEqual(self.owner.get('/app?paid=true#view=billing').status_code,200);self.assertEqual(self.usage()['current_plan'],'free')
        self.assertEqual(self.owner.delete(self.base+'/billing/checkout',headers=CSRF).status_code,200)
        self.assertEqual(self.post('/billing/simulate',{'action':'pay','operation':op['operation']}).status_code,409)
    def test_paid_cancel_past_due_expiry_and_data_kept(self):
        self.pay('team');self.assertEqual(self.usage()['current_plan'],'team');self.send(self.event())
        self.post('/billing/simulate',{'action':'cancel_at_period_end'});self.assertEqual(self.usage()['current_plan'],'team')
        self.assertTrue(self.owner.get(self.base+'/billing').json()['cancel_at_period_end'])
        self.post('/billing/simulate',{'action':'renew_fail'});self.assertEqual(self.usage()['current_plan'],'team')
        self.post('/billing/simulate',{'action':'expire'});self.assertEqual(self.usage()['current_plan'],'free');self.assertEqual(self.usage()['totals']['events'],1)
        invoices=self.owner.get(self.base+'/billing/invoices').json()['invoices'];self.assertEqual(len(invoices),2);self.assertTrue(all(i['test'] for i in invoices))
    def test_bad_stale_tampered_wrong_mode_duplicate_webhooks(self):
        self.pay()
        with closing(app.connect()) as c:customer=c.execute('SELECT customer FROM billing_accounts WHERE workspace=?',(self.w,)).fetchone()[0]
        body,sig=self.signed(customer)
        def send(b=body,s=sig):return self.client.post('/api/billing/webhook',content=b,headers={'stripe-signature':s})
        self.assertEqual(send(s='bad').status_code,400);self.assertEqual(send(b=body+b' ').status_code,400)
        stale,signature=self.signed(customer,stamp=int(time.time()-400));self.assertEqual(send(stale,signature).status_code,400)
        live,signature=self.signed(customer,live=True);self.assertEqual(send(live,signature).status_code,400)
        self.assertEqual(send().status_code,200);self.assertTrue(send().json()['duplicate']);app.billing.work();self.assertEqual(self.usage()['current_plan'],'builder')
        self.post('/billing/simulate',{'action':'expire'});old,signature=self.signed(customer);send(old,signature);app.billing.work();self.assertEqual(self.usage()['current_plan'],'free')
    def test_refresh_failure_preserves_paid_term_and_queue_retries(self):
        self.pay()
        with patch.object(StripeProvider,'snapshot',side_effect=RuntimeError('private-secret')):
            with patch.dict(os.environ,{'AFTERSPAN_BILLING_MODE':'stripe_test'}):
                with closing(app.connect()) as c,c:c.execute("UPDATE billing_accounts SET provider_mode='stripe_test' WHERE workspace=?",(self.w,))
                r=self.post('/billing/refresh');self.assertEqual(r.status_code,502);self.assertNotIn('private-secret',r.text);self.assertEqual(self.usage()['current_plan'],'builder')
    def test_local_billing_host_guard_and_disabled_collects(self):
        with patch.dict(os.environ,{'ANALYTICS_PUBLIC_URL':'https://afterspan.example.test','COOKIE_SECURE':'true'}):self.assertEqual(self.post('/billing/checkout',{'plan':'builder'}).status_code,404)
        with patch.dict(os.environ,{'AFTERSPAN_BILLING_MODE':'disabled'}):
            self.assertEqual(self.post('/billing/checkout',{'plan':'builder'}).status_code,503);self.assertEqual(self.send(self.event()).status_code,200)
        with self.assertRaises(HTTPException):StripeProvider('stripe_live')
    def test_retained_events_seed_only_no_backdated_usage(self):
        self.send(self.event('old'))
        with closing(app.connect()) as c,c:
            c.execute('DELETE FROM usage_receipts');c.execute('DELETE FROM usage_months');c.execute('DELETE FROM usage_days');c.execute("DELETE FROM billing_settings WHERE key='ledger_seeded'")
        app.init_db();self.assertEqual(self.usage()['totals']['events'],0);self.assertEqual(self.send(self.event('old')).json()['duplicates'],1)

    def test_invite_accept_reserves_once_and_expired_invite_releases(self):
        invite,token=self.invite(self.owner,self.w,'join@example.test');self.invite(self.owner,self.w,'reserved@example.test')
        member,_=self.user('join@example.test');r=member.post('/api/invites/accept',headers=CSRF,json={'token':token});self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.usage()['resources']['seats'],3)
        with closing(app.connect()) as c,c:c.execute('UPDATE workspace_invites SET expires=? WHERE workspace=? AND used IS NULL',(time.time()-1,self.w))
        self.assertEqual(self.usage()['resources']['seats'],2);self.assertEqual(self.post('/invites',{'email':'new@example.test','role':'reader'}).status_code,202)
    def test_downgrade_keeps_projects_and_schedules_then_applies(self):
        self.pay('team');self.project(self.owner,self.w,'second');self.project(self.owner,self.w,'third')
        self.post('/billing/simulate',{'action':'change_builder'});self.assertEqual(self.usage()['current_plan'],'team')
        self.post('/billing/simulate',{'action':'renew'});self.assertEqual(self.usage()['current_plan'],'builder')
        self.post('/billing/simulate',{'action':'expire'});self.assertEqual(self.usage()['resources']['projects'],3)
        self.assertEqual(self.owner.post('/api/projects?workspace='+self.w,headers=CSRF,json={'id':'fourth','name':'Fourth'}).status_code,409)
        self.assertEqual(len(self.owner.get('/api/projects?workspace='+self.w).json()['projects']),3)
    def test_queued_webhook_retries_after_restart_and_leaks_no_payload(self):
        self.pay()
        with closing(app.connect()) as c:customer=c.execute('SELECT customer FROM billing_accounts WHERE workspace=?',(self.w,)).fetchone()[0]
        body,sig=self.signed(customer);self.client.post('/api/billing/webhook',content=body,headers={'stripe-signature':sig});app.init_db()
        with patch.object(app.billing,'refresh',side_effect=RuntimeError('offline')):app.billing.work()
        with closing(app.connect()) as c,c:
            job=c.execute("SELECT * FROM billing_webhooks WHERE state='queued'").fetchone();self.assertEqual(job['attempts'],1);self.assertNotIn('data',job.keys());c.execute('UPDATE billing_webhooks SET next_attempt=0')
        app.billing.work()
        with closing(app.connect()) as c:self.assertEqual(c.execute("SELECT count(*) FROM billing_webhooks WHERE state='queued'").fetchone()[0],0)
    def test_creating_checkout_timeout_resumes_same_provider_operation(self):
        op=self.checkout()
        with closing(app.connect()) as c,c:c.execute("UPDATE billing_operations SET state='creating',url=NULL,provider_id=NULL,expires=NULL WHERE id=?",(op['operation'],))
        self.assertEqual(self.checkout()['operation'],op['operation'])
        self.assertEqual(self.owner.get(self.base+'/billing/quote?plan=team').json()['monthly_cents'],7900)
    def test_paid_expiry_by_clock_not_webhook_and_read_key_denial(self):
        self.pay()
        with closing(app.connect()) as c,c:c.execute('UPDATE billing_accounts SET paid_until=? WHERE workspace=?',(time.time()-1,self.w))
        self.assertEqual(self.usage()['current_plan'],'free')
        r=self.owner.post('/api/projects/'+self.p['id']+'/read-keys',headers=CSRF,json={'label':'reader'});self.assertEqual(r.status_code,200,r.text)
        c=TestClient(app.app);self.assertEqual(c.get(self.base+'/usage',headers={'Authorization':'Bearer '+r.json()['read_key']}).status_code,401)

    def test_signup_keeps_plan_in_one_time_email_and_public_catalog_has_no_keys(self):
        r=self.client.post('/auth/request-link',headers=CSRF,json={'email':'plan@example.test','plan':'builder'});self.assertEqual(r.status_code,202)
        with closing(app.connect()) as c:body=c.execute('SELECT body FROM account_mail WHERE recipient=?',('plan@example.test',)).fetchone()[0]
        self.assertIn('plan=builder',body)
        self.assertEqual(self.client.post('/auth/request-link',headers=CSRF,json={'email':'bad@example.test','plan':'enterprise'}).status_code,422)
        r=self.client.get('/api/plans');self.assertEqual(r.status_code,200);self.assertNotIn('SECRET',r.text);self.assertNotIn('whsec_',r.text);self.assertEqual(self.client.get('/pricing').status_code,200)
    def test_owner_scheduled_downgrade_keeps_paid_plan_and_is_idempotent(self):
        self.pay('team');self.send(self.event());body={'request_id':str(uuid.uuid4()),'confirm_plan':'builder'}
        r=self.post('/billing/downgrade',body);self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['current_plan'],'team');self.assertEqual(r.json()['scheduled_plan'],'builder')
        self.assertEqual(self.post('/billing/downgrade',body).status_code,200);self.assertEqual(self.usage()['totals']['events'],1)
        reader,_=self.join(self.owner,self.w,'downgrade-reader@example.test');self.assertEqual(self.post('/billing/downgrade',body,reader).status_code,403)
        self.assertEqual(self.post('/billing/downgrade',{'confirm_plan':'team','request_id':str(uuid.uuid4())}).status_code,422)
    def test_downgrade_cannot_change_builder_or_a_cancelling_subscription(self):
        self.pay('builder');body={'request_id':str(uuid.uuid4()),'confirm_plan':'builder'}
        self.assertEqual(self.post('/billing/downgrade',body).status_code,409)
        self.post('/billing/simulate',{'action':'change_team'});self.post('/billing/simulate',{'action':'cancel_at_period_end'})
        self.assertEqual(self.post('/billing/downgrade',body).status_code,409)
    def test_local_portal_action_validation_and_no_simulator_in_real_mode(self):
        self.pay();r=self.post('/billing/portal',{'action':'cancel'});self.assertEqual(r.status_code,200,r.text);self.assertIn('action=cancel',r.json()['url'])
        self.assertEqual(self.post('/billing/portal',{'action':'immediate_cancel'}).status_code,422)
        with patch.dict(os.environ,{'AFTERSPAN_BILLING_MODE':'stripe_test'}):self.assertEqual(self.post('/billing/simulate',{'action':'pay'}).status_code,404)

class StripeContractTests(unittest.TestCase):
    def provider(self):
        env=patch.dict(os.environ,{'AFTERSPAN_STRIPE_SECRET_KEY':'sk_test_fixture','AFTERSPAN_STRIPE_PRICE_BUILDER':'price_builder','AFTERSPAN_STRIPE_PRICE_TEAM':'price_team','AFTERSPAN_STRIPE_PORTAL_CONFIGURATION':'bpc_fixture'});env.start();self.addCleanup(env.stop);p=StripeProvider('stripe_test');p.client=MagicMock();return p
    def test_price_verification_checkout_and_restricted_portal(self):
        p=self.provider();p.client.v1.prices.retrieve.side_effect=lambda pid:{'id':pid,'product':'prod_afterspan','active':True,'livemode':False,'currency':'usd','unit_amount':1900 if pid=='price_builder' else 7900,'recurring':{'interval':'month','interval_count':1,'usage_type':'licensed'}}
        p.client.v1.checkout.sessions.create.return_value={'id':'cs_test','url':'https://checkout.stripe.com/c/pay/test','expires_at':time.time()+3600}
        self.assertEqual(p.checkout('cus_test','w_test','instance','builder','op','https://afterspan.test')['id'],'cs_test')
        self.assertEqual(p.client.v1.checkout.sessions.create.call_args.kwargs['options']['idempotency_key'],'checkout:op')
        config={'active':True,'livemode':False,'features':{'subscription_cancel':{'enabled':True,'mode':'at_period_end'},'subscription_update':{'enabled':True,'default_allowed_updates':['price'],'products':[{'product':'prod_afterspan','prices':['price_team']}],'proration_behavior':'always_invoice','schedule_at_period_end':{'conditions':[{'type':'decreasing_item_amount'}]}}}}
        p.client.v1.billing_portal.configurations.retrieve.return_value=config;p.client.v1.billing_portal.sessions.create.return_value={'url':'https://billing.stripe.com/p/session/test'}
        self.assertTrue(p.portal('cus_test','https://afterspan.test','w_test','op').startswith('https://billing.stripe.com'))
        p.client.v1.billing_portal.configurations.retrieve.assert_called_with('bpc_fixture',{'expand':['features.subscription_update.products']})
        p.client.v1.subscriptions.retrieve.return_value={'customer':'cus_test','livemode':False}
        p.portal('cus_test','https://afterspan.test','w_test','cancel-op','cancel','sub_test')
        params=p.client.v1.billing_portal.sessions.create.call_args.args[0]
        self.assertEqual(params['flow_data']['subscription_cancel']['subscription'],'sub_test')
        self.assertIn('billing_return=portal',params['flow_data']['after_completion']['redirect']['return_url'])
        p.client.v1.subscriptions.retrieve.return_value={'customer':'other','livemode':False}
        with self.assertRaises(ValueError):p.portal('cus_test','https://afterspan.test','w_test','bad-op','cancel','sub_test')
        config['features']['subscription_update']['default_allowed_updates'].append('quantity')
        with self.assertRaises(ValueError):p.portal('cus_test','https://afterspan.test','w_test','op')
        with self.assertRaises(ValueError):safe_url('https://stripe.com.evil.test/pay')
    def test_subscription_requires_matching_paid_invoice_and_customer(self):
        p=self.provider();p.client.v1.customers.retrieve.return_value={'livemode':False,'metadata':{'afterspan_workspace':'w','afterspan_instance':'i'}}
        sub={'id':'sub_test','status':'active','items':{'data':[{'quantity':1,'price':{'id':'price_builder'},'current_period_start':100,'current_period_end':200}]},'latest_invoice':{'id':'in_test','status':'open','customer':'cus_test','lines':{'data':[{'price':{'id':'price_builder'},'period':{'end':200}}]}}}
        p.client.v1.subscriptions.list.return_value={'data':[sub],'has_more':False}
        self.assertFalse(p.snapshot('cus_test','w','i')['paid']);sub['latest_invoice']['status']='paid';self.assertTrue(p.snapshot('cus_test','w','i')['paid'])
        sub['latest_invoice']['lines']['data'][0]['period']['end']=150;self.assertFalse(p.snapshot('cus_test','w','i')['paid'])
        with self.assertRaises(ValueError):p.snapshot('cus_test','other','i')

    def test_real_downgrade_only_schedules_future_phase_without_proration(self):
        p=self.provider();p.verify_prices=lambda:None
        p.client.v1.subscriptions.retrieve.return_value={'customer':'cus_test','livemode':False,'status':'active','items':{'data':[{'price':{'id':'price_team'},'quantity':1,'current_period_start':100,'current_period_end':200}]}}
        p.client.v1.subscription_schedules.create.return_value={'id':'sub_sched_test'};p.client.v1.subscription_schedules.update.return_value={'id':'sub_sched_test'}
        self.assertEqual(p.downgrade('cus_test','sub_test','op')['effective_at'],200)
        args=p.client.v1.subscription_schedules.update.call_args.args[1];self.assertEqual(args['proration_behavior'],'none');self.assertEqual(args['phases'][0]['items'][0]['price'],'price_team');self.assertEqual(args['phases'][1]['start_date'],200);self.assertEqual(args['phases'][1]['items'][0]['price'],'price_builder')
        self.assertFalse(p.client.v1.subscriptions.update.called);self.assertFalse(p.client.v1.invoices.create.called)
        with self.assertRaises(ValueError):p.downgrade('another','sub_test','op')
