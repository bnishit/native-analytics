"""Fresh read-only Codex + real remote MCP on an isolated local-mail preview only."""
import argparse
import base64
from email import policy
from email.parser import BytesParser
import hashlib
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from verify_repo_codex import completed_value

ROOT=Path(__file__).resolve().parents[1]

def private(path,value):
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as handle:
        json.dump(value,handle,indent=2)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin',default='http://127.0.0.1:8786')
    parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args();origin=urllib.parse.urlsplit(args.origin)
    if origin.scheme!='http' or origin.hostname!='127.0.0.1' or origin.username or origin.password or origin.path or origin.query or origin.fragment or '?' in args.origin or '#' in args.origin:
        parser.error('use an isolated numeric-loopback local-mail preview')
    if args.state.exists() or args.state.is_symlink() or args.report.exists() or args.report.is_symlink():parser.error('use new state and report paths')
    args.state.mkdir(mode=0o700,parents=True);args.report.parent.mkdir(parents=True,exist_ok=True)
    opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    def call(path,body=None,method=None,form=False):
        data=urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode() if body is not None else None
        req=urllib.request.Request(args.origin+path,data=data,method=method,headers={
            'Content-Type':'application/x-www-form-urlencoded' if form else 'application/json','X-Native-CSRF':'1'})
        with opener.open(req,timeout=30) as response:
            body=response.read()
            return json.loads(body) if body else {}
    config=call('/auth/config')
    assert config['delivery_mode']=='local' and config['email_sign_in'],'local mail is required'
    email='tracker-agent-'+uuid.uuid4().hex[:10]+'@example.test'
    call('/auth/request-link',{'email':email});token=None
    for _ in range(50):
        for path in (ROOT/'data/mail').glob('*.eml'):
            message=BytesParser(policy=policy.default).parsebytes(path.read_bytes())
            if message['To']==email:
                link=re.search(r'http://[^\s]+',message.get_content()).group()
                token=urllib.parse.parse_qs(urllib.parse.urlsplit(link).fragment)['sign-in'][0];break
        if token:break
        time.sleep(.2)
    assert token,'local approval mail was not delivered'
    call('/auth/consume-link',{'token':token})
    workspace=call('/api/workspaces',{'name':'Isolated Tracker agent verification'})
    project=call('/api/projects?workspace='+workspace['id'],{'id':'tracker-proof','name':'Tracker agent proof'})
    gid=str(uuid.uuid4())
    definition={'title':'Tracker catalog answer criteria','contract':'slack-request-v1','workflow':'slack_read_request',
        'environment':'production','traffic_kind':'user','min_sample_size':25,'deadline_seconds':30,
        'checks':[{'name':'growthbook_catalog','version':2,'method':'source','phase':'before_delivery','evidence_required':True}],
        'milestones':['completion','delivery']}
    goal=call('/api/outcomes/goals/'+gid+'?project='+project['id']+'&revision=0',definition,'PUT')
    binding={k:goal[k] for k in ['id','version','sha256']}
    scopes=['audit:read','experiments:write'];redirect='http://127.0.0.1:19191/callback'
    client=call('/register',{'client_name':'Isolated Tracker proof','redirect_uris':[redirect],
        'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'token_endpoint_auth_method':'none','scope':' '.join(scopes)})
    verifier=uuid.uuid4().hex+uuid.uuid4().hex
    params={'client_id':client['client_id'],'redirect_uri':redirect,'response_type':'code','state':'isolated-tracker-proof',
        'scope':' '.join(scopes),'code_challenge':base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='),
        'code_challenge_method':'S256','resource':args.origin+'/mcp'}
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*unused):return None
    try:urllib.request.build_opener(NoRedirect()).open(args.origin+'/authorize?'+urllib.parse.urlencode(params),timeout=30)
    except urllib.error.HTTPError as error:
        assert error.code==302,'authorization did not produce a consent request'
        pending=urllib.parse.parse_qs(urllib.parse.urlsplit(error.headers['Location']).fragment)['request'][0]
    approved=call('/auth/mcp/decision',{'request_id':pending,'project':project['id'],'approve':True})
    code=urllib.parse.parse_qs(urllib.parse.urlsplit(approved['redirect_uri']).query)['code'][0]
    credentials=call('/token',{'grant_type':'authorization_code','client_id':client['client_id'],'code':code,
        'code_verifier':verifier,'redirect_uri':redirect,'resource':args.origin+'/mcp'},form=True)
    exposed=['connection_status','get_workflow','outcome_goals','tracker_flow_status']
    report={'ok':False,'environment':'isolated local preview','exposed_tools':exposed,
        'limits':['Fresh direct-MCP Codex task, not plugin installation or public publication.',
            'Synthetic empty project: proves onboarding and a truthful missing-case next action, not a repaired or released user task.']}
    try:
        empty=args.state/'empty-client';empty.mkdir(mode=0o700)
        expected={'project':project['id'],'workflow':'improve-tracker','next_action':'review_regression_case',
            'original_request_replay':False,'deployment_authorized':False}
        schema={'type':'object','properties':{k:{'const':v} for k,v in expected.items()},'required':list(expected),'additionalProperties':False}
        schema_path=args.state/'schema.json';private(schema_path,schema)
        env={k:v for k,v in os.environ.items() if k in {'HOME','PATH','TMPDIR','USER','LOGNAME','SHELL','LANG'}}
        env['AFTERSPAN_AGENT_TOKEN']=credentials['access_token']
        argv=['codex','exec','--ignore-user-config','--strict-config','--ephemeral','--skip-git-repo-check','--sandbox','read-only','--json','-C',str(empty),
            '--output-schema',str(schema_path),'-c','web_search="disabled"',
            '-c','mcp_servers.afterspan.url='+json.dumps(args.origin+'/mcp'),
            '-c','mcp_servers.afterspan.bearer_token_env_var="AFTERSPAN_AGENT_TOKEN"',
            '-c','mcp_servers.afterspan.required=true','-c','mcp_servers.afterspan.enabled_tools='+json.dumps(exposed)]
        for feature in ['shell_tool','apps','browser_use','browser_use_external','computer_use','image_generation','plugins','multi_agent','memories','skill_search']:
            argv+=['--disable',feature]
        prompt='Use ONLY Afterspan MCP. Call connection_status, get_workflow(name="improve-tracker"), outcome_goals, then tracker_flow_status(query={"outcome_goal":'+json.dumps(binding)+'}). Inspect completed results. This project has no case or runs. Report the actual next action and full-request/deployment limits, without claiming reproduction, fix, release or user benefit. Do not use any other tools or change state. Return only the requested JSON.'
        transcript=args.state/'transcript.jsonl';stderr=args.state/'stderr.txt';answer=args.state/'answer.json'
        private(answer,{})
        with os.fdopen(os.open(transcript,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as out,os.fdopen(os.open(stderr,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as err:
            result=subprocess.run(argv+['-o',str(answer),'-'],input=prompt,text=True,env=env,stdout=out,stderr=err,timeout=300)
        assert result.returncode==0,'fresh Codex failed; private stderr retained'
        assert json.loads(answer.read_text())==expected,'agent answer differs from the supported next step'
        calls=[]
        for line in transcript.read_text().splitlines():
            event=json.loads(line);item=event.get('item',{})
            assert item.get('type') in {None,'reasoning','agent_message','mcp_tool_call'},'unexpected client capability'
            if item.get('type')!='mcp_tool_call' or event.get('type')!='item.completed':continue
            assert item.get('server')=='afterspan' and item.get('tool') in exposed,'unexpected tool'
            receipt=completed_value(item)
            if item['tool']=='tracker_flow_status':assert receipt['next_action']=='review_regression_case'
            calls.append(item['tool'])
        assert set(calls)==set(exposed),'all four completed tool receipts are required'
        report.update(ok=True,tool_sequence=calls,answer=expected)
    except (AssertionError,ValueError,subprocess.TimeoutExpired) as error:
        report['failure']=str(error) if isinstance(error,AssertionError) else type(error).__name__
    finally:
        call('/revoke',{'client_id':client['client_id'],'token':credentials['access_token'],'token_type_hint':'access_token'},form=True)
        try:
            request=urllib.request.Request(args.origin+'/api/outcomes/goals?project='+project['id'],headers={'Authorization':'Bearer '+credentials['access_token']})
            urllib.request.build_opener(urllib.request.HTTPHandler()).open(request,timeout=30)
        except urllib.error.HTTPError as error:
            assert error.code==401,'revoked temporary credential was not denied'
            report['temporary_grant_revoked']=True
        else:
            report['ok']=False;report['failure']='revoked temporary credential still reads protected data'
    private(args.report,report);print(json.dumps({'ok':report['ok'],'report':str(args.report)}))
    return 0 if report['ok'] else 1

if __name__=='__main__':sys.exit(main())
