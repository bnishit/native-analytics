"""Exercise the first loop on an isolated loopback preview with synthetic workspaces."""
import argparse
from datetime import datetime,timedelta,timezone
from email import policy as email_policy
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

ROOT=Path(__file__).resolve().parents[1]

def private(path,value):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as handle:json.dump(value,handle,indent=2)
    path.chmod(0o600)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin',default='http://127.0.0.1:8786')
    parser.add_argument('--base',default='afterspan/loop-pilot-base-20261019')
    parser.add_argument('--repository',default='bnishit/native-analytics')
    parser.add_argument('--state',type=Path,default=ROOT/'data/loop-proof')
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--open-pilot-pr',action='store_true')
    parser.add_argument('--external-gap',type=Path)
    args=parser.parse_args();origin=urllib.parse.urlsplit(args.origin)
    if origin.scheme!='http' or origin.hostname!='127.0.0.1' or origin.path or origin.query or origin.fragment:parser.error('use an isolated loopback preview')
    cookies=CookieJar();opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    def request(path,body=None,method=None,key=None,expected=(200,201,202)):
        headers={'Content-Type':'application/json','X-Native-CSRF':'1'}
        if key:headers['Authorization']='Bearer '+key
        req=urllib.request.Request(args.origin+path,data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
        try:
            with opener.open(req,timeout=90) as response:
                value=json.load(response)
                if response.status not in expected:raise AssertionError('Unexpected HTTP status')
                return value
        except urllib.error.HTTPError as error:
            raise AssertionError(f'{path} returned HTTP {error.code}: '+error.read(2000).decode()) from None
    config=request('/auth/config');assert config['delivery_mode']=='local' and config['email_sign_in'],config
    email='loop-proof-'+uuid.uuid4().hex[:10]+'@example.test'
    request('/auth/request-link',{'email':email})
    token=None
    for _ in range(30):
        for path in (ROOT/'data/mail').glob('*.eml'):
            message=BytesParser(policy=email_policy.default).parsebytes(path.read_bytes())
            if message['To']==email:
                body=message.get_content();link=re.search(r'http://[^\s]+',body).group()
                token=urllib.parse.parse_qs(urllib.parse.urlsplit(link).fragment)['sign-in'][0];break
        if token:break
        time.sleep(.2)
    assert token,'Local sign-in email missing'
    request('/auth/consume-link',{'token':token})
    session=next(cookie.value for cookie in cookies if cookie.name=='as_session')
    private(args.state/'owner.json',{'session':session,'cookie_name':'as_session'})
    workspace=request('/api/workspaces',{'name':'First loop proof · synthetic'})['id']
    project=request('/api/projects?workspace='+workspace,{'id':'loop-'+uuid.uuid4().hex[:10],'name':'First useful answer · synthetic'})
    editor=request('/api/workspaces/'+workspace+'/agent-keys',{'label':'First loop proof Codex','role':'editor','expires_days':1})
    private(args.state/'connection.json',{'origin':args.origin,'workspace':workspace,'project':project['id'],'key':editor['key'],'root':str(ROOT),'repository':args.repository,'base':args.base})
    private(args.state/'producer.json',{'write_key':project['write_key']})
    env={k:v for k,v in os.environ.items() if not k.startswith(('ANALYTICS_','AFTERSPAN_','NATIVE_ANALYTICS_'))}
    env.update(NATIVE_ANALYTICS_URL=args.origin,AFTERSPAN_SESSION_FILE=str(args.state/'owner.json'))
    def cli(*commands):
        result=subprocess.run([sys.executable,str(ROOT/'cli/native_analytics.py'),*commands],env=env,text=True,capture_output=True,timeout=120)
        if result.returncode:raise AssertionError('CLI failed: '+result.stdout+' '+result.stderr)
        return json.loads(result.stdout)
    dossier=cli('loop-sync','--project',project['id'],'--repo',str(ROOT),'--repository',args.repository,'--base',args.base)
    assert dossier['goal']['id']=='first-answer'
    suffix='?project='+project['id'];prefix='/api/loop/goals/first-answer'
    now=datetime.now(timezone.utc)-timedelta(seconds=2)
    iso=lambda value:value.isoformat()
    def observe(start,end,through):
        oid=str(uuid.uuid4());body={'entry_start':iso(start),'entry_end':iso(end),'observed_through':iso(through),'as_of':iso(datetime.now(timezone.utc))}
        path=args.state/('window-'+oid+'.json');private(path,body)
        return cli('loop-observe','--project',project['id'],'--id',oid,'--file',str(path))
    zero=observe(now-timedelta(days=20),now-timedelta(days=19),now-timedelta(days=18))
    assert (zero['mature'],zero['qualified'],zero['waiting'],zero['rate'])==(0,0,0,None),zero
    expected={key:zero[key] for key in ['source','measurement_revision','mature','qualified','waiting','unassessed','rate','limitations','next_action']}
    rows=[];completion_ids={}
    def event(name,subject,at):
        eid='loop-proof-'+uuid.uuid4().hex
        rows.append({'id':eid,'name':name,'distinct_id':subject,'timestamp':iso(at),'properties':{'environment':'local','observation_id':zero['id'],'fixture':'synthetic'}})
        return eid
    before_start=now-timedelta(days=8);after_start=now-timedelta(days=4)
    for period,start in [('before',before_start),('after',after_start)]:
        for number in range(4):
            subject=period+'-'+str(number);entry=start+timedelta(hours=1)
            event('codex.connection.started',subject,entry)
            if number in [0,1,2]:completion_ids[subject]=event('codex.answer.ready',subject,entry+timedelta(hours=25 if period=='before' and number==1 else 1))
    event('codex.connection.started','before-1',before_start+timedelta(hours=24))
    event('codex.connection.started','waiting',now-timedelta(hours=1))
    request('/v1/events',{'events':rows},key=project['write_key'])
    verdicts=[]
    for subject,eid in completion_ids.items():
        answer=dict(expected)
        should_pass=subject in ['after-0','after-1']
        if not should_pass:answer['qualified']=1
        result=request(prefix+'/answers/'+str(uuid.uuid4())+suffix,{'completion_event_id':eid,'evidence_id':zero['id'],'answer':answer},'PUT')
        verdicts.append(result)
    before=observe(before_start,before_start+timedelta(days=1),before_start+timedelta(days=2))
    after=observe(after_start,now,datetime.now(timezone.utc))
    assert (before['mature'],before['qualified'],before['failed'],before['timed_out'],before['rate'])==(4,0,2,2,0),before
    assert (after['mature'],after['qualified'],after['failed'],after['timed_out'],after['waiting'],after['rate'])==(4,2,1,1,1,.5),after
    comparison=request(prefix+'/compare'+suffix,{'before':before['id'],'after':after['id']})
    assert comparison['status']=='unknown',comparison
    policy={'state':'active','repository':args.repository,'base':args.base,'operations':['draft-pr'],'paths':['examples/loop_pilot.py'],'environment':'local','required_checks':[],'expires_at':iso(datetime.now(timezone.utc)+timedelta(days=1))}
    policy_path=args.state/'policy.json';private(policy_path,policy)
    cli('loop-policy','--project',project['id'],'--revision','0','--file',str(policy_path))
    action=cli('loop-prepare','--project',project['id'],'--request-id',str(uuid.uuid4()))
    receipt=None
    if args.open_pilot_pr:
        receipt=cli('loop-action','--project',project['id'],'--id',action['id'],'--operation','draft-pr')
        repeated=cli('loop-action','--project',project['id'],'--id',action['id'],'--operation','reconcile')
        private(args.state/'action.json',{'prepared':action,'first':receipt,'reconciled':repeated})
    if args.external_gap:
        gap=json.loads(args.external_gap.read_text());query=json.dumps(gap['query'],sort_keys=True,separators=(',',':'))
        body={'kind':'contract_gap','source_url':gap['source_url'],'query_sha256':hashlib.sha256(query.encode()).hexdigest(),'checked_at':gap['taxonomy_snapshot_at'],'missing_events':gap['missing_events']}
        external=request(prefix+'/external/'+str(uuid.uuid4())+suffix,body,'PUT')
        assert external['mature'] is None and external['qualified'] is None,external
    view=cli('loop-status','--project',project['id'])
    private(args.state/'state.json',{'project':project['id'],'workspace':workspace,'zero':zero,'before':before,'after':after,'action_id':action['id']})
    report={'environment':'isolated local preview','data':'synthetic fixture; no production user outcomes','source_commit':subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip(),'goal_source':dossier['goal']['source'],'zero':zero,'before':before,'after':after,'comparison':comparison,'action':receipt,'stages':view['stages'],'ui_url':args.origin+'/app#'+urllib.parse.urlencode({'workspace':workspace,'project':project['id'],'view':'loop','days':7}),'private_state':str(args.state),'assertions':['committed goal verified by GitHub','zero denominator unavailable','first-entry retry never resets24h clock','independent checker rejects wrong structured facts','4 mature before with0 qualified','4 mature after with2 qualified;1 waiting','comparison lacks release evidence; no causal claim']}
    args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'ok':True,'report':str(args.report),'ui_url':report['ui_url'],'action_id':action['id']}))

if __name__=='__main__':main()
