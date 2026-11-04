import base64
import hashlib
import json
import logging
import os
from unittest.mock import patch
import time
import unittest
import uuid
from contextlib import closing
from urllib.parse import parse_qs,parse_qsl,urlsplit

from fastapi.testclient import TestClient
import app
import test_workspaces

CSRF=test_workspaces.CSRF
ORIGIN='http://127.0.0.1:8769'

class RemoteMCPTests(unittest.TestCase):
    setUp=test_workspaces.WorkspaceTests.setUp
    tearDown=test_workspaces.WorkspaceTests.tearDown
    token=test_workspaces.WorkspaceTests.token
    user=test_workspaces.WorkspaceTests.user
    workspace=test_workspaces.WorkspaceTests.workspace
    project=test_workspaces.WorkspaceTests.project
    invite=test_workspaces.WorkspaceTests.invite
    join=test_workspaces.WorkspaceTests.join

    def register(self):
        r=self.client.post('/register',json={'client_name':'Fresh test agent','redirect_uris':['http://127.0.0.1:19191/callback'],'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'token_endpoint_auth_method':'none','scope':'analytics:read charts:write audit:read goals:write experiments:write debug:read collection:write releases:write followups:write'})
        self.assertEqual(r.status_code,201,r.text);return r.json()['client_id']

    def begin(self,cid,scope='analytics:read charts:write',**overrides):
        verifier='v'*64
        data={'client_id':cid,'redirect_uri':'http://127.0.0.1:19191/callback','response_type':'code','state':'client-state','scope':scope,'code_challenge':base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='),'code_challenge_method':'S256','resource':ORIGIN+'/mcp',**overrides}
        r=self.client.get('/authorize',params=data,follow_redirects=False)
        self.assertEqual(r.status_code,302,r.text)
        return parse_qs(urlsplit(r.headers['location']).fragment)['request'][0],verifier

    def exchange(self,cid,code,verifier):
        return self.client.post('/token',data={'grant_type':'authorization_code','client_id':cid,'code':code,'code_verifier':verifier,'redirect_uri':'http://127.0.0.1:19191/callback','resource':ORIGIN+'/mcp'})

    def grant(self,scopes='analytics:read charts:write'):
        owner,uid=self.user('owner@example.test');workspace=self.workspace(owner);project=self.project(owner,workspace)['id']
        cid=self.register();rid,verifier=self.begin(cid,scopes)
        preview=owner.get('/auth/mcp/request',params={'request_id':rid});self.assertEqual(preview.status_code,200)
        self.assertEqual(preview.json()['scopes'],scopes.split())
        r=owner.post('/auth/mcp/decision',headers=CSRF,json={'request_id':rid,'project':project,'approve':True});self.assertEqual(r.status_code,200,r.text)
        q=parse_qs(urlsplit(r.json()['redirect_uri']).query);self.assertEqual(q['state'],['client-state'])
        code=q['code'][0];r=self.exchange(cid,code,verifier);self.assertEqual(r.status_code,200,r.text)
        return owner,uid,workspace,project,cid,r.json(),code,verifier

    def downgrade_member(self,owner,workspace,user):
        guardian,_=self.join(owner,workspace,'guardian@example.test','owner')
        response=guardian.patch('/api/workspaces/'+workspace+'/members/'+user,headers=CSRF,json={'role':'reader'})
        self.assertEqual(response.status_code,200,response.text)
        members=guardian.get('/api/workspaces/'+workspace+'/members').json()['members']
        self.assertEqual(next(m['role'] for m in members if m['id']==user),'reader')
        self.assertEqual(sum(m['role']=='owner' for m in members),1)
        return guardian

    def test_connection_lasts_six_months_but_access_tokens_stay_short_lived(self):
        owner,uid,w,p,cid,t,code,verifier=self.grant('audit:read')
        self.assertLessEqual(t['expires_in'],3600)
        with closing(app.connect()) as c:
            grant=c.execute('SELECT expires FROM oauth_grants WHERE client=?',(cid,)).fetchone()
            client=c.execute('SELECT expires FROM oauth_clients WHERE id=?',(cid,)).fetchone()
            self.assertGreater(grant['expires'],time.time()+179*86400)
            self.assertGreaterEqual(client['expires'],grant['expires'])

    def test_followups_need_new_consent_and_current_authority(self):
        from starlette.requests import Request
        owner,uid,w,p,cid,t,*_=self.grant('audit:read experiments:write')
        headers={'Authorization':'Bearer '+t['access_token']}
        self.assertEqual(self.client.post('/api/improvements/status?project='+p,headers=headers,json={}).json()['next_action'],'select_outcome_goal')
        path='/api/outcomes/followups/'+str(uuid.uuid4())+'?project='+p
        self.assertEqual(self.client.get(path,headers=headers).status_code,403)
        owner,uid,w,p,cid,t,*_=self.grant('audit:read experiments:write followups:write')
        request=Request({'type':'http','method':'PUT','path':'/api/outcomes/followups/'+str(uuid.uuid4()),
            'headers':[(b'authorization',('Bearer '+t['access_token']).encode())]})
        authority=app.improvements.authority_for(request,p)
        self.assertTrue(app.improvements.authority_valid(p,authority))
        self.assertFalse(app.improvements.authority_valid('alpha',authority))
        response=self.client.post('/token',data={'grant_type':'refresh_token','client_id':cid,'refresh_token':t['refresh_token'],
            'scope':'audit:read experiments:write'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertFalse(app.improvements.authority_valid(p,authority))
        self.assertEqual(self.client.get('/api/outcomes/followups/'+str(uuid.uuid4())+'?project='+p,
            headers={'Authorization':'Bearer '+response.json()['access_token']}).status_code,403)

    def test_followup_reconnect_listing_reuses_new_scope_and_project(self):
        owner,uid,w,p,cid,t,*_=self.grant('audit:read experiments:write')
        self.assertEqual(self.client.get('/api/outcomes/followups?project='+p,headers={'Authorization':'Bearer '+t['access_token']}).status_code,403)
        owner,uid,w,p,cid,t,*_=self.grant('audit:read experiments:write followups:write')
        headers={'Authorization':'Bearer '+t['access_token'],'Accept':'application/json, text/event-stream'}
        id=str(uuid.uuid4())
        with closing(app.connect()) as c,c:
            c.execute("INSERT INTO outcome_followups(project,id,definition,authority,created,deadline,next_due,state) VALUES(?,?,?,?,?,?,?,?)",
                (p,id,json.dumps({'private':'fixture-body-sentinel'}),json.dumps({'ref':'private-authority-sentinel'}),time.time(),time.time()+120,time.time()+60,'completed'))
        response=self.client.get('/api/outcomes/followups?project='+p,headers=headers)
        self.assertEqual(response.status_code,200,response.text);listed=response.json()
        self.assertEqual(listed['followups'][0]['id'],id);self.assertFalse(listed['has_more'])
        self.assertNotIn('sentinel',json.dumps(listed))
        selected=self.client.get('/api/outcomes/followups/'+id+'?project='+p,headers=headers)
        self.assertEqual(selected.status_code,200,selected.text)
        self.assertEqual(selected.json()['definition'],{'private':'fixture-body-sentinel'})
        self.assertEqual(self.client.get('/api/outcomes/followups?project=alpha',headers=headers).status_code,403)
        self.assertEqual(self.client.get('/api/outcomes/followups?project='+p).status_code,401)

    def test_followup_session_key_and_member_revocation_are_rechecked(self):
        from starlette.requests import Request
        owner,uid,w,p,cid,t,*_=self.grant('audit:read experiments:write followups:write')
        def request(headers):
            return Request({'type':'http','method':'PUT','path':'/api/outcomes/followups/'+str(uuid.uuid4()),
                'headers':[(k.encode(),v.encode()) for k,v in headers.items()]})
        session_authority=app.improvements.authority_for(request({'cookie':'as_session='+owner.cookies.get('as_session'),'x-native-csrf':'1'}),p)
        self.assertTrue(app.improvements.authority_valid(p,session_authority))
        key=test_workspaces.WorkspaceTests.key(self,owner,w,'editor')
        key_authority=app.improvements.authority_for(request({'authorization':'Bearer '+key['key']}),p)
        self.assertTrue(app.improvements.authority_valid(p,key_authority))
        self.assertEqual(owner.delete('/api/workspaces/'+w+'/agent-keys/'+key['id'],headers=CSRF).status_code,200)
        self.assertFalse(app.improvements.authority_valid(p,key_authority))
        oauth_authority=app.improvements.authority_for(request({'authorization':'Bearer '+t['access_token']}),p)
        self.downgrade_member(owner,w,uid)
        self.assertFalse(app.improvements.authority_valid(p,session_authority))
        self.assertFalse(app.improvements.authority_valid(p,oauth_authority))

    def test_real_protocol_requires_pkce_consent_and_scopes(self):
        metadata=self.client.get('/.well-known/oauth-authorization-server',headers={'Origin':'https://client.example'})
        self.assertEqual(metadata.status_code,200)
        self.assertEqual(metadata.headers['access-control-allow-origin'],'*')
        self.assertEqual(metadata.json()['token_endpoint_auth_methods_supported'],['none'])
        self.assertEqual(metadata.json()['revocation_endpoint_auth_methods_supported'],['none'])
        owner,uid,w,p,cid,t,code,verifier=self.grant('analytics:read')
        h={'Authorization':'Bearer '+t['access_token']}
        self.assertEqual(self.client.get('/api/charts',params={'project':p},headers=h).status_code,200)
        self.assertEqual(self.client.get('/api/charts?project=alpha',headers=h).status_code,403)
        self.assertEqual(self.client.get('/api/events',params={'project':p},headers=h).status_code,403)
        self.assertEqual(self.client.post('/api/charts',params={'project':p},headers=h,json={'title':'Denied','query':{}}).status_code,403)
        self.assertEqual(self.client.post('/api/audit/review',params={'project':p},headers=h,json={}).status_code,403)
        self.assertEqual(self.client.get('/api/traces/catalog',params={'project':p},headers=h).status_code,403)
        self.assertEqual(self.client.post('/api/traces/search',params={'project':p},headers=h,json={}).status_code,403)
        self.assertEqual(self.client.delete('/api/charts/'+str(uuid.uuid4()),params={'project':p},headers=h).status_code,403)
        self.assertEqual(self.exchange(cid,code,verifier).status_code,400)
        rid,v=self.begin(cid)
        self.assertEqual(self.client.post('/auth/mcp/decision',headers=CSRF,json={'request_id':rid,'project':p,'approve':True}).status_code,401)
        self.assertEqual(owner.post('/auth/mcp/decision',json={'request_id':rid,'project':p,'approve':True}).status_code,403)
        r=owner.post('/auth/mcp/decision',headers=CSRF,json={'request_id':rid,'project':p,'approve':True})
        nextcode=parse_qs(urlsplit(r.json()['redirect_uri']).query)['code'][0]
        self.assertEqual(self.exchange(cid,nextcode,'wrong'*12).status_code,400)
        self.assertEqual(self.exchange(cid,nextcode,v).status_code,200)

    def test_analytics_scope_cannot_read_trace_properties_through_product_queries(self):
        owner,uid,w,p,cid,t,*_=self.grant('analytics:read')
        trace_id=str(uuid.uuid4())
        event=app.Event(name='trace.observed',properties={'afterspan_trace':{
            'schema_version':'workflow-trace-v1','trace_id':trace_id,'attempt_id':str(uuid.uuid4()),
            'attempt_number':1,'span_id':str(uuid.uuid4()),'workflow':'appointment_request',
            'environment':'staging','step':'provider_send','state':'started'}})
        audit=app.Event(name='slack.audit.parser',properties={'afterspan_audit':{'schema_version':'slack-request-v1','stage':'parser','environment':'staging','request_ref':trace_id,'attempt_ref':str(uuid.uuid4()),'attempt_number':1,'context_digest':'a'*64}})
        with closing(app.connect()) as c,c:app.store_events(c,p,[event,audit])
        h={'Authorization':'Bearer '+t['access_token']}
        self.assertEqual(self.client.post('/api/traces/review',params={'project':p},headers=h,json={'environment':'staging'}).status_code,403)
        for query in [{'event':'slack.audit.parser','breakdown':'afterspan_audit'},{},{'event':'trace.observed','breakdown':'afterspan_trace'},{'breakdown':'afterspan_trace'},{'breakdown':'afterspan_audit'}]:
            r=self.client.post('/api/query',params={'project':p},headers=h,json=query)
            self.assertEqual(r.status_code,200,r.text);self.assertNotIn('a'*64,r.text);self.assertNotIn(trace_id,r.text)
            self.assertEqual(r.json()['total'],0)
        r=self.client.post('/api/catalog',params={'project':p},headers=h,json={'event':'trace.observed'})
        self.assertEqual(r.status_code,200,r.text);self.assertNotIn('afterspan_trace',r.text)
        r=self.client.get('/api/overview',params={'project':p},headers=h)
        self.assertEqual(r.status_code,200,r.text);self.assertNotIn('trace.observed',r.text)

    def test_refresh_rotation_replay_and_membership_changes(self):
        owner,uid,w,p,cid,t,*_=self.grant()
        def refresh(value):return self.client.post('/token',data={'grant_type':'refresh_token','client_id':cid,'refresh_token':value})
        r=refresh(t['refresh_token']);self.assertEqual(r.status_code,200,r.text);new=r.json()
        self.assertEqual(self.client.get('/api/charts',params={'project':p},headers={'Authorization':'Bearer '+t['access_token']}).status_code,401)
        self.assertEqual(refresh(t['refresh_token']).status_code,400)
        self.assertEqual(self.client.get('/api/charts',params={'project':p},headers={'Authorization':'Bearer '+new['access_token']}).status_code,401)
        rid,v=self.begin(cid);r=owner.post('/auth/mcp/decision',headers=CSRF,json={'request_id':rid,'project':p,'approve':True})
        code=parse_qs(urlsplit(r.json()['redirect_uri']).query)['code'][0];t=self.exchange(cid,code,v).json();h={'Authorization':'Bearer '+t['access_token']}
        self.downgrade_member(owner,w,uid)
        self.assertEqual(self.client.post('/api/charts',params={'project':p},headers=h,json={'title':'Denied','query':{}}).status_code,403)
        self.assertEqual(self.client.get('/api/charts',params={'project':p},headers=h).status_code,200)
        connections=owner.get('/auth/mcp/connections').json()['connections'];gid=connections[0]['id']
        self.assertEqual(owner.delete('/auth/mcp/connections/'+gid,headers=CSRF).status_code,200)
        self.assertEqual(self.client.get('/api/charts',params={'project':p},headers=h).status_code,401)

    def test_redirect_resource_and_denial(self):
        r=self.client.post('/register',json={'redirect_uris':['http://evil.example/callback'],'token_endpoint_auth_method':'none'})
        self.assertEqual(r.status_code,400,r.text)
        cid=self.register();rid,v=self.begin(cid)
        owner,uid=self.user('owner@example.test')
        r=owner.post('/auth/mcp/decision',headers=CSRF,json={'request_id':rid,'approve':False})
        self.assertEqual(parse_qs(urlsplit(r.json()['redirect_uri']).query)['error'],['access_denied'])
        self.assertEqual(owner.post('/auth/mcp/decision',headers=CSRF,json={'request_id':rid,'approve':False}).status_code,400)
        params={'client_id':cid,'redirect_uri':'http://127.0.0.1:19191/callback','response_type':'code','scope':'analytics:read','code_challenge':'v'*43,'code_challenge_method':'S256','resource':'https://other.example/mcp'}
        r=self.client.get('/authorize',params=params,follow_redirects=False)
        self.assertIn('invalid_request',r.headers.get('location',''))
        params['redirect_uri']='https://evil.example/callback'
        self.assertEqual(self.client.get('/authorize',params=params,follow_redirects=False).status_code,400)

    def test_expiry_wrong_client_and_revocation_endpoint(self):
        owner,uid,w,p,cid,t,code,verifier=self.grant()
        other=self.register()
        r=self.client.post('/token',data={'grant_type':'refresh_token','client_id':other,'refresh_token':t['refresh_token']})
        self.assertEqual(r.status_code,400)
        r=self.client.post('/revoke',data={'client_id':cid,'token':t['access_token'],'token_type_hint':'access_token'})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.client.get('/api/charts',params={'project':p},headers={'Authorization':'Bearer '+t['access_token']}).status_code,401)
        rid,v=self.begin(cid)
        with closing(app.connect()) as c,c:c.execute('UPDATE oauth_pending SET expires=0 WHERE id=?',(rid,))
        self.assertEqual(owner.post('/auth/mcp/decision',headers=CSRF,json={'request_id':rid,'project':p,'approve':True}).status_code,400)
        app.init_db()  # Safe migration/restart of the same persisted store.
        self.assertEqual(owner.get('/auth/mcp/connections').json()['connections'],[])

    def test_remote_transport_tools_and_idempotent_chart_storage(self):
        owner,uid,w,p,cid,t,*_=self.grant('analytics:read charts:write audit:read goals:write experiments:write debug:read collection:write followups:write')
        with TestClient(app.app,base_url=ORIGIN) as client:
            headers={'Authorization':'Bearer '+t['access_token'],'Accept':'application/json, text/event-stream'}
            def rpc(method,params=None):
                r=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':method,**({'params':params} if params else {})})
                self.assertEqual(r.status_code,200,r.text);return r.json()
            self.assertEqual(client.post('/mcp',json={}).status_code,401)
            meta=client.get('/.well-known/oauth-protected-resource/mcp').json();self.assertEqual(meta['resource'],ORIGIN+'/mcp')
            rpc('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'fresh','version':'1'}})
            names={x['name'] for x in rpc('tools/list')['result']['tools']}
            self.assertTrue({'save_chart','request_trace','get_workflow','connection_status'}<=names)
            def call(name,args={}):
                result=rpc('tools/call',{'name':name,'arguments':args})['result']
                self.assertFalse(result.get('isError'),result);return json.loads(result['content'][0]['text'])
            self.assertEqual(call('connection_status')['project'],p)
            self.assertEqual(call('outcome_followup')['followups'],[])
            self.assertEqual(call('collection_sources')['sources'],[])
            self.assertEqual(call('save_collection_source',{'definition':{'source':'worker','environment':'staging','interval_seconds':60},'revision':0})['revision'],1)
            health=call('collection_health',{'query':{'environment':'staging'}})
            self.assertEqual(health['collection_completeness'],'not_established');self.assertIn('pulse_missing',health['sources'][0]['reasons'])

            self.assertIn('instructions',call('get_workflow',{'name':'build-dashboard'}))
            self.assertIn('events',call('event_catalog',{'query':{'days':30}}))
            self.assertIn('meanings',call('workflow_trace_catalog'))
            self.assertEqual(call('workflow_trace_review',{'query':{'environment':'staging'}})['traces'],[])
            trace_event=app.Event(name='trace.observed',properties={'afterspan_trace':{
                'schema_version':'workflow-trace-v1','trace_id':str(uuid.uuid4()),'attempt_id':str(uuid.uuid4()),
                'attempt_number':1,'span_id':str(uuid.uuid4()),'workflow':'appointment_request',
                'environment':'staging','step':'receive','state':'started'}})
            child={**trace_event.properties['afterspan_trace'],'span_id':str(uuid.uuid4()),'parent_span_id':trace_event.properties['afterspan_trace']['span_id'],'step':'processing'}
            check={'name':'source_facts','version':1,'result':'unknown','required':True,'method':'source','phase':'before_delivery','execution':'not_run','reason_code':'evidence_absent'}
            check_start=app.Event(name='trace.observed',properties={'afterspan_trace':child})
            check_end=app.Event(name='trace.observed',properties={'afterspan_trace':{**child,'state':'succeeded','duration_ms':1.0,'checks':[check]}})
            with closing(app.connect()) as c,c:app.store_events(c,p,[trace_event,check_start,check_end])
            reviewed=call('workflow_trace_review',{'query':{'environment':'staging'}})
            detail=call('workflow_trace_detail',{'trace':reviewed['traces'][0]['trace'],'query':reviewed['query']})
            self.assertEqual(detail['trace'],reviewed['traces'][0])
            self.assertEqual(detail['trace']['completion']['receive']['state'],'incomplete')
            searched=call('search_traces',{'query':{'environment':'staging'}})
            opened=call('open_trace',{'reference':searched['traces'][0]['reference'],'query':searched['query']})
            self.assertTrue(opened['trace']['has_gaps'])
            self.assertEqual(opened['trace']['business_outcome'],'not_independently_verified')
            self.assertEqual(opened['trace']['checks'][0]['execution'],'not_run')
            self.assertEqual(opened['trace']['checks'][0]['reason_code'],'evidence_absent')
            diagnostic=call('search_traces',{'query':{'environment':'staging','search':'not run'}})
            self.assertEqual(diagnostic['total'],1)
            self.assertEqual(call('open_trace',{'reference':diagnostic['traces'][0]['reference'],'query':diagnostic['query']})['trace']['checks'],opened['trace']['checks'])
            self.assertFalse(call('debug_capture_policy')['active'])
            with patch.dict(os.environ,{'AFTERSPAN_CONNECTION_KEY':base64.urlsafe_b64encode(b'k'*32).decode()}):
                policy={'enabled':True,'workflow':'appointment_request','environment':'staging','privacy_reviewed':True}
                response=owner.put('/api/debug/policy?project='+p+'&revision=0',headers=CSRF,json=policy)
                self.assertEqual(response.status_code,200,response.text)
                from debugging import DebugSnapshot
                facts=trace_event.properties['afterspan_trace'];snapshot_id=str(uuid.uuid4())
                capture=DebugSnapshot(id=snapshot_id,policy_revision=1,privacy_reviewed=True,kind='context',content={'chosen':'public fixture'},**{k:facts[k] for k in ['trace_id','attempt_id','span_id','workflow','environment']})
                app.debugging.capture(p,capture)
                fresh=call('search_traces',{'query':{'environment':'staging'}})
                debug_query={**fresh['query'],'reference':fresh['traces'][0]['reference']}
                listed=call('review_debug_snapshots',{'query':debug_query})
                self.assertEqual(listed['snapshots'][0]['id'],snapshot_id);self.assertNotIn('content',listed['snapshots'][0])
                debugged=call('review_debug_snapshots',{'query':{**debug_query,'snapshot_id':snapshot_id}})
                self.assertEqual(debugged['content'],{'chosen':'public fixture'});self.assertNotIn(facts['trace_id'],json.dumps(debugged))
                self.assertEqual(client.put('/api/debug/policy?project='+p+'&revision=1',headers={'Authorization':'Bearer '+t['access_token']},json=policy).status_code,403)
                slack={'schema_version':'slack-request-v1','stage':'trace','request_ref':str(uuid.uuid4()),'attempt_ref':str(uuid.uuid4()),'span_ref':str(uuid.uuid4()),
                       'attempt_number':1,'request_started_at_ms':int(time.time()*1000),'trace_step':'quality','span_state':'succeeded','duration_ms':1.0,'environment':'staging','request_kind':'read'}
                with closing(app.connect()) as c,c:app.store_events(c,p,[app.Event(name='slack.audit.trace',properties={'afterspan_audit':slack})])
                response=owner.put('/api/debug/policy?project='+p+'&revision=1',headers=CSRF,json={**policy,'contract':'slack-request-v1','workflow':'slack_read_request'})
                self.assertEqual(response.status_code,200,response.text)
                content={'schema_version':'catalog-check-replay-v1','release_sha':None,'request':None,'candidate':None,'source':None,'blocked':[],
                         'quality':{'check_version':'growthbook-catalog-v2','reported_outcome':'failed','checked_claims':1,'mismatched_claims':1,'unknown_claims':0}}
                capture=DebugSnapshot(id=uuid.uuid4(),policy_revision=2,privacy_reviewed=True,contract='slack-request-v1',kind='replay',workflow='slack_read_request',environment='staging',
                    trace_id=slack['request_ref'],attempt_id=slack['attempt_ref'],span_id=slack['span_ref'],content=content)
                app.debugging.capture(p,capture)
                fresh=call('search_traces',{'query':{'environment':'staging','contract':'slack-request-v1'}})
                debug_query={**fresh['query'],'reference':fresh['traces'][0]['reference']}
                listed=call('review_debug_snapshots',{'query':debug_query})
                self.assertEqual(listed['snapshots'][0]['replay']['status'],'blocked');self.assertNotIn('content',listed['snapshots'][0])
                selected=call('review_debug_snapshots',{'query':{**debug_query,'snapshot_id':str(capture.id)}})
                self.assertEqual(selected['content'],content);self.assertFalse(selected['replay']['full_request_replay'])
                self.assertIn('release_unavailable',selected['replay']['reasons'])

            from outcomes import OutcomeGoal
            goal_id=str(uuid.uuid4())
            goal=OutcomeGoal(title='Checked workflow',workflow='appointment_request',environment='staging',deadline_seconds=30,
                checks=[{'name':'source_facts','version':1,'method':'source','phase':'before_delivery'}],milestones=['delivery'])
            args={'id':goal_id,'revision':0,'definition':goal.model_dump(mode='json')}
            self.assertEqual(call('save_outcome_goal',args)['version'],1)
            self.assertEqual(call('save_outcome_goal',args)['version'],1)
            self.assertEqual(call('outcome_goals')['goals'][0]['id'],goal_id)
            self.assertEqual(call('released_artifacts',{'environment':'staging'})['releases'],[])
            outcome=call('evaluate_outcomes',{'query':{'goal_id':goal_id,'version':1}})
            self.assertEqual(outcome['counts']['unclassified'],1)
            self.assertIsNone(outcome['satisfied_fraction'])

            case_id=str(uuid.uuid4());dataset_id=str(uuid.uuid4())
            case={'title':'Reviewed read request','privacy_reviewed':True,'input':{'request':'list experiments in production'},'expected':{'count':2}}
            saved=call('save_experiment_object',{'kind':'cases','id':case_id,'definition':case})
            self.assertEqual(saved['version'],1)
            self.assertEqual(call('save_experiment_object',{'kind':'cases','id':case_id,'definition':case})['sha256'],saved['sha256'])
            self.assertNotIn('input',call('experiment_objects',{'kind':'cases'})['items'][0]['definition'])
            dataset={'title':'Read requests','cases':[{'id':case_id,'version':1}]}
            call('save_experiment_object',{'kind':'datasets','id':dataset_id,'definition':dataset})
            ids=[]
            for variant,count in [('baseline',3),('candidate',2)]:
                run_id=str(uuid.uuid4());ids.append(run_id)
                definition={'dataset':dataset_id,'dataset_version':1,'variant':variant,'release_sha':'a'*40,'evaluator_runner_sha256':'b'*64,'privacy_reviewed':True,'results':[{'id':case_id,'version':1,'output':{'count':count},'duration_ms':10}]}
                call('save_experiment_object',{'kind':'runs','id':run_id,'definition':definition})
            compared=call('compare_experiments',{'baseline':ids[0],'candidate':ids[1]})
            self.assertTrue(compared['eligible_for_review']);self.assertFalse(compared['deployment_authorized'])

            batch_event=app.Event(name='trace.observed',properties={'afterspan_trace':{
                **trace_event.properties['afterspan_trace'],'trace_id':str(uuid.uuid4()),'span_id':str(uuid.uuid4()),
                'step':'processing','receipt_count':1,'linked_trace_ids':[trace_event.properties['afterspan_trace']['trace_id']]}})
            with closing(app.connect()) as c,c:app.store_events(c,p,[batch_event])
            reviewed=call('workflow_trace_review',{'query':{'environment':'staging','limit':1}})
            detail=call('workflow_trace_detail',{'trace':reviewed['traces'][0]['trace'],'query':reviewed['query']})
            self.assertEqual(detail['trace']['batch']['receipt_count'],1)
            self.assertEqual(len(detail['linked_inputs']),1)
            chart={'chart_id':str(uuid.uuid4()),'title':'Contacts','query':{'event':'contact.clicked','days':30}}
            a=call('save_chart',chart);b=call('save_chart',chart);self.assertEqual(a['id'],b['id'])
            self.assertEqual(len(call('saved_charts')['charts']),1)
            changed=rpc('tools/call',{'name':'save_chart','arguments':{**chart,'title':'Changed'}})['result'];self.assertTrue(changed['isError'])
            self.assertEqual(len(call('saved_charts')['charts']),1)
            self.downgrade_member(owner,w,uid)
            downgraded=rpc('tools/call',{'name':'save_outcome_goal','arguments':{**args,'revision':1}})['result']
            self.assertTrue(downgraded['isError'])
            downgraded=rpc('tools/call',{'name':'experiment_objects','arguments':{'kind':'cases'}})['result']
            self.assertTrue(downgraded['isError'])
            self.assertTrue(rpc('tools/call',{'name':'debug_capture_policy','arguments':{}})['result']['isError'])
            self.check_release_tools_use_real_protocol_and_keep_owner_adoption_separate(client)



    def test_release_measurement_needs_both_diagnostic_and_experiment_scopes(self):
        for scope in ['audit:read','experiments:write']:
            owner,uid,w,p,cid,t,*_=self.grant(scope)
            response=self.client.get('/api/outcomes/releases?project='+p+'&environment=staging',headers={'Authorization':'Bearer '+t['access_token']})
            self.assertEqual(response.status_code,403)
        owner,uid,w,p,cid,t,*_=self.grant('audit:read experiments:write')
        response=self.client.get('/api/outcomes/releases?project='+p+'&environment=staging',headers={'Authorization':'Bearer '+t['access_token']})
        self.assertEqual(response.status_code,200,response.text)

    def test_release_preparation_needs_new_consent_and_cannot_adopt(self):
        _,_,_,project,_,old,*_=self.grant('audit:read experiments:write')
        headers={'Authorization':'Bearer '+old['access_token']}
        self.assertEqual(self.client.get('/api/outcomes/release-sources?project='+project,headers=headers).status_code,403)
        self.assertEqual(self.client.put('/api/outcomes/release-proposals/'+str(uuid.uuid4())+'?project='+project,headers=headers,json={}).status_code,403)
        _,_,_,project,_,approved,*_=self.grant('releases:write')
        headers={'Authorization':'Bearer '+approved['access_token']}
        sources=self.client.get('/api/outcomes/release-sources?project='+project,headers=headers)
        self.assertEqual(sources.status_code,200,sources.text);self.assertEqual(sources.json(),{'connections':[]})
        request={'proposal_id':str(uuid.uuid4()),'proposal_sha256':'a'*64}
        denied=self.client.post('/api/outcomes/releases/'+str(uuid.uuid4())+'/adopt?project='+project,headers=headers,json=request)
        self.assertIn(denied.status_code,[401,403])

    def check_release_tools_use_real_protocol_and_keep_owner_adoption_separate(self,client):
        from pydantic import SecretStr
        from loop_github import GitHub
        from test_release_evidence import SourceProofTests
        owner,uid,workspace,project,_,token,*_=self.grant('releases:write')
        fixture=SourceProofTests();fixture.setUp()
        repo=app.product_loop.onboarding;target={key:str(uuid.uuid4()) for key in ('project_id','service_id','environment_id')}
        def github(client,method,path,**kwargs):
            if client.read_guard:client.read_guard()
            return fixture.g._request(method,path,**kwargs)
        def railway(host,method,path,secret,body):
            self.assertEqual((host,method,path),('backboard.railway.app','POST','/graphql/v2'))
            self.assertEqual(body['variables'],{'serviceId':target['service_id'],'environmentId':target['environment_id']})
            return {'data':{'serviceInstance':{'latestDeployment':{'id':str(uuid.UUID(int=1)),**{'projectId':target['project_id'],'serviceId':target['service_id'],'environmentId':target['environment_id']},'environment':{'id':target['environment_id'],'name':'production'},'status':'SUCCESS','createdAt':'2026-01-02T00:00:00Z','statusUpdatedAt':'2026-01-02T00:00:01Z','meta':{'commitHash':'c'*40}}}}}
        with patch.dict(os.environ,{'AFTERSPAN_CONNECTION_KEY':base64.urlsafe_b64encode(b'x'*32).decode()}),patch.object(GitHub,'_request',github),patch('loop_sources._request',side_effect=railway):
            gh=repo.connections.put(project,str(uuid.uuid4()),0,'github',{'repository':'org/app'},SecretStr('synthetic'))
            rail=repo.connections.put(project,str(uuid.uuid4()),0,'railway',target,SecretStr('synthetic'))
            headers={'Authorization':'Bearer '+token['access_token'],'Accept':'application/json, text/event-stream'}
            def rpc(method,params):
                response=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':method,'params':params})
                self.assertEqual(response.status_code,200,response.text);return response.json()['result']
            rpc('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'release-fixture','version':'1'}})
            names={tool['name'] for tool in rpc('tools/list',{})['tools']}
            self.assertTrue({'release_sources','prepare_release_evidence','release_proposal','release_evidence'}<=names)
            self.assertNotIn('adopt_release_evidence',names)
            def call(name,args):
                result=rpc('tools/call',{'name':name,'arguments':args});self.assertFalse(result.get('isError'),result)
                return json.loads(result['content'][0]['text'])
            self.assertEqual(len(call('release_sources',{})['connections']),2)
            proposal_id=str(uuid.uuid4());definition={'github_connection_id':gh['id'],'railway_connection_id':rail['id'],'pr_number':192,'environment':'production'}
            queued=call('prepare_release_evidence',{'id':proposal_id,'definition':definition});self.assertEqual(queued['status'],'pending')
            proposal=call('release_proposal',{'id':proposal_id});self.assertEqual(proposal['status'],'requires_owner_adoption')
            route=dict(parse_qsl(urlsplit(proposal['owner_review_url']).fragment));self.assertEqual(route['project'],project);self.assertEqual(route['release_proposal'],proposal_id)
            self.assertEqual(proposal['evidence']['source']['manifest'][0]['after']['mode'],'100755')
            receipt_id=str(uuid.uuid4());body={'proposal_id':proposal_id,'proposal_sha256':proposal['proposal_sha256']}
            denied=client.post('/api/outcomes/releases/'+receipt_id+'/adopt?project='+project,headers=headers,json=body);self.assertIn(denied.status_code,[401,403])
            adopted=owner.post('/api/outcomes/releases/'+receipt_id+'/adopt?project='+project,headers=CSRF,json=body);self.assertEqual(adopted.status_code,200,adopted.text)
            final=call('release_evidence',{'id':receipt_id});self.assertEqual(final['adopted_by'],uid);self.assertNotIn('private patch',json.dumps(final))
            self.downgrade_member(owner,workspace,uid)
            self.assertTrue(rpc('tools/call',{'name':'release_sources','arguments':{}})['isError'])

    def test_chart_write_scope_cannot_edit_outcome_goals(self):
        owner,uid,w,p,cid,t,*_=self.grant('audit:read charts:write')
        definition={'title':'Checked export','workflow':'document_export','environment':'production','deadline_seconds':60,
            'checks':[{'name':'source_facts','version':1,'method':'source','phase':'before_delivery'}],'milestones':['delivery']}
        response=self.client.put('/api/outcomes/goals/'+str(uuid.uuid4())+'?project='+p+'&revision=0',headers={'Authorization':'Bearer '+t['access_token']},json=definition)
        self.assertEqual(response.status_code,403)
        self.assertEqual(app.outcomes.list(p)['goals'],[])
        self.assertEqual(self.client.get('/api/debug/policy?project='+p,headers={'Authorization':'Bearer '+t['access_token']}).status_code,403)
        self.assertEqual(self.client.get('/api/experiments/cases?project='+p,headers={'Authorization':'Bearer '+t['access_token']}).status_code,403)


    def test_collection_writes_need_separate_scope_and_current_editor_membership(self):
        owner,uid,w,p,cid,t,*_=self.grant('audit:read')
        definition={'source':'worker','environment':'staging','interval_seconds':60}
        headers={'Authorization':'Bearer '+t['access_token']}
        self.assertEqual(self.client.get('/api/collection/sources?project='+p,headers=headers).status_code,200)
        self.assertEqual(self.client.put('/api/collection/sources?project='+p,headers=headers,json=definition).status_code,403)
        owner,uid,w,p,cid,t,*_=self.grant('audit:read collection:write');headers={'Authorization':'Bearer '+t['access_token']}
        self.assertEqual(self.client.put('/api/collection/sources?project='+p,headers=headers,json=definition).status_code,200)
        self.assertEqual(self.client.get('/api/collection/sources?project=other-project',headers=headers).status_code,403)
        guardian=self.downgrade_member(owner,w,uid)
        self.assertEqual(self.client.put('/api/collection/sources?project='+p+'&revision=1',headers=headers,json={**definition,'enabled':False}).status_code,403)
        self.assertEqual(self.client.get('/api/collection/sources?project='+p,headers=headers).status_code,200)
        refreshed=self.client.post('/token',data={'grant_type':'refresh_token','client_id':cid,'refresh_token':t['refresh_token']})
        self.assertEqual(refreshed.status_code,200,refreshed.text);fresh=refreshed.json()
        self.assertEqual(fresh['scope'],'audit:read')
        self.assertEqual(self.client.get('/api/collection/sources?project='+p,headers=headers).status_code,401)
        headers={'Authorization':'Bearer '+fresh['access_token']}
        self.assertEqual(self.client.get('/api/collection/sources?project='+p,headers=headers).status_code,200)
        member='/api/workspaces/'+w+'/members/'+uid
        self.assertEqual(guardian.patch(member,headers=CSRF,json={'role':'editor'}).status_code,200)
        self.assertEqual(self.client.put('/api/collection/sources?project='+p+'&revision=1',headers=headers,json={**definition,'enabled':False}).status_code,403)
        sources=guardian.get('/api/collection/sources?project='+p).json()['sources']
        self.assertEqual(sources[0]['revision'],1);self.assertTrue(sources[0]['definition']['enabled'])
        self.assertEqual(guardian.delete(member,headers=CSRF).status_code,200)
        self.assertEqual(self.client.get('/api/collection/sources?project='+p,headers=headers).status_code,401)
        self.assertEqual(self.client.post('/token',data={'grant_type':'refresh_token','client_id':cid,'refresh_token':fresh['refresh_token']}).status_code,400)

if __name__=='__main__':unittest.main()
