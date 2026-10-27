"""Browser approval for the CLI; credentials never enter terminal output."""
import json
import os
from pathlib import Path
import time
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
        while time.monotonic()<deadline:
            time.sleep(max(3,grant['interval']))
            result=post('/auth/cli/poll',{'token':grant['device_code']})
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
