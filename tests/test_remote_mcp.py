import base64
import hashlib
import json
import logging
import time
import unittest
import uuid
from contextlib import closing
from urllib.parse import parse_qs,urlsplit

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
        r=self.client.post('/register',json={'client_name':'Fresh test agent','redirect_uris':['http://127.0.0.1:19191/callback'],'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'token_endpoint_auth_method':'none','scope':'analytics:read charts:write audit:read'})
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
        with closing(app.connect()) as c,c:app.store_events(c,p,[event])
        h={'Authorization':'Bearer '+t['access_token']}
        self.assertEqual(self.client.post('/api/traces/review',params={'project':p},headers=h,json={'environment':'staging'}).status_code,403)
        for query in [{},{'event':'trace.observed','breakdown':'afterspan_trace'},{'breakdown':'afterspan_trace'}]:
            r=self.client.post('/api/query',params={'project':p},headers=h,json=query)
            self.assertEqual(r.status_code,200,r.text);self.assertEqual(r.json()['total'],0)
            self.assertNotIn(trace_id,r.text)
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
        with closing(app.connect()) as c,c:c.execute("UPDATE memberships SET role='reader' WHERE workspace=? AND user_id=?",(w,uid))
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
        owner,uid,w,p,cid,t,*_=self.grant('analytics:read charts:write audit:read')
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
            self.assertIn('instructions',call('get_workflow',{'name':'build-dashboard'}))
            self.assertIn('events',call('event_catalog',{'query':{'days':30}}))
            self.assertIn('meanings',call('workflow_trace_catalog'))
            self.assertEqual(call('workflow_trace_review',{'query':{'environment':'staging'}})['traces'],[])
            trace_event=app.Event(name='trace.observed',properties={'afterspan_trace':{
                'schema_version':'workflow-trace-v1','trace_id':str(uuid.uuid4()),'attempt_id':str(uuid.uuid4()),
                'attempt_number':1,'span_id':str(uuid.uuid4()),'workflow':'appointment_request',
                'environment':'staging','step':'receive','state':'started'}})
            with closing(app.connect()) as c,c:app.store_events(c,p,[trace_event])
            reviewed=call('workflow_trace_review',{'query':{'environment':'staging'}})
            detail=call('workflow_trace_detail',{'trace':reviewed['traces'][0]['trace'],'query':reviewed['query']})
            self.assertEqual(detail['trace'],reviewed['traces'][0])
            self.assertEqual(detail['trace']['completion']['receive']['state'],'incomplete')
            chart={'chart_id':str(uuid.uuid4()),'title':'Contacts','query':{'event':'contact.clicked','days':30}}
            a=call('save_chart',chart);b=call('save_chart',chart);self.assertEqual(a['id'],b['id'])
            self.assertEqual(len(call('saved_charts')['charts']),1)
            changed=rpc('tools/call',{'name':'save_chart','arguments':{**chart,'title':'Changed'}})['result'];self.assertTrue(changed['isError'])
            self.assertEqual(len(call('saved_charts')['charts']),1)

if __name__=='__main__':unittest.main()
