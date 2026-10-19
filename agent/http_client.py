"""Bound-origin HTTP transport shared by the two scoped stdio bridges."""
import json
import urllib.error
import urllib.parse
import urllib.request


def request(origin,key,path,body=None,method=None,**params):
    parsed=urllib.parse.urlsplit(origin)
    if not (parsed.scheme=='https' and parsed.hostname or parsed.scheme=='http' and parsed.hostname=='127.0.0.1') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        raise ValueError('Set NATIVE_ANALYTICS_URL to an HTTPS origin or local loopback origin.')
    query=urllib.parse.urlencode(params)
    req=urllib.request.Request(origin+path+('?' + query if query else ''),data=None if body is None else json.dumps(body).encode(),
        headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},method=method)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args):return None
    try:
        with urllib.request.build_opener(NoRedirect).open(req,timeout=15) as response:return json.load(response)
    except urllib.error.HTTPError as exc:
        raise ValueError('Afterspan rejected the action (HTTP '+str(exc.code)+'). Check scope, role, expiry and the current resource state.') from None
    except (urllib.error.URLError,TimeoutError):raise ValueError('Afterspan is unreachable. No result was inferred.') from None
