"""Browser approval for the CLI; credentials never enter terminal output."""
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
import urllib.parse
import webbrowser


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):return None


def login(origin,output,open_browser=True):
    path=Path(output)
    if path.exists():raise ValueError('private receipt already exists')
    # Reserve the destination before starting a grant that can only be collected once.
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    saved=False
    try:
        def post(route,body):
            request=urllib.request.Request(origin+route,data=json.dumps(body).encode(),
                headers={'Content-Type':'application/json','X-Native-CSRF':'1'},method='POST')
            with urllib.request.build_opener(NoRedirect).open(request,timeout=20) as response:return json.load(response)
        grant=post('/auth/cli/start',{})
        expected=origin+'/assets/cli-login.html'
        if grant['verification_uri']!=expected:raise ValueError('unexpected approval destination')
        url=expected+'#'+urllib.parse.urlencode({'code':grant['user_code']})
        print(json.dumps({'status':'approval_required','verification_url':url,'user_code':grant['user_code'],'expires_in':grant['expires_in']}),flush=True)
        if open_browser:webbrowser.open(url)
        deadline=time.monotonic()+min(grant['expires_in'],600)
        interval=max(3,grant['interval']);failures=0;uncertain=False
        while time.monotonic()+interval<deadline:
            time.sleep(interval)
            try:
                result=post('/auth/cli/poll',{'token':grant['device_code']})
            except (OSError,TimeoutError) as error:
                status=error.code if isinstance(error,urllib.error.HTTPError) else None
                retryable=status is None or status==429 or 500<=status<600
                uncertain=uncertain or status is None or status>=500
                failures+=1
                if not retryable or failures>=3:
                    print(json.dumps({'ok':False,'status':'restart_required','result':'unknown' if uncertain else 'rejected',
                        'message':'No session was saved. Start login-browser again; a lost completion response cannot be recovered.'}),flush=True)
                    return 1
                delay=error.headers.get('Retry-After','3') if isinstance(error,urllib.error.HTTPError) else '3'
                interval=max(3,int(delay)) if delay.isdigit() else 3
                print(json.dumps({'status':'retrying','result':'unknown' if uncertain else 'pending'}),flush=True)
                continue
            failures=0;interval=max(3,grant['interval'])
            if result['status']=='pending':continue
            if result['status']!='complete':raise ValueError('authorization denied')
            result.pop('status');result['origin']=origin
            with os.fdopen(fd,'w') as stream:
                fd=None;json.dump(result,stream);stream.write('\n');stream.flush();os.fsync(stream.fileno())
            saved=True
            print(json.dumps({'ok':True,'private_receipt':str(path),'expires':result['expires']}));return 0
        raise TimeoutError('authorization expired')
    finally:
        if fd is not None:os.close(fd)
        if not saved:path.unlink(missing_ok=True)
