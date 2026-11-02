"""Reviewed fixtures and local application experiment runner. No network or server execution."""
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import time

_SECRET=re.compile(r'\b(?:Bearer\s+\S+|(?:na_write_|na_admin_|as_read_|as_workspace_|as_session_|as_device_|as_login_|as_invite_|ghp_|github_pat_|xoxb-|sk-)[A-Za-z0-9_-]+)',re.I)
_PII=re.compile(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|(?<!\w)(?:\+?\d[\s().-]*){10,15}(?!\w)',re.I)
_PRIVATE_KEY=re.compile(r'password|passwd|secret|authorization|cookie|credential|api.?key|email|phone|mobile|patient|address|(?:^|_)(?:key|token)(?:$|_)',re.I)

def reviewed_json(value):
    """Reject obvious secrets/PII rather than comparing two redacted values as equal."""
    def inspect(item,depth=0):
        if depth>5:raise ValueError('fixture exceeds five nested levels')
        if isinstance(item,dict):
            if len(item)>64:raise ValueError('fixture has too many fields')
            for key,v in item.items():
                if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}',key) or _PRIVATE_KEY.search(key):raise ValueError('fixture contains a private or unsupported field')
                inspect(v,depth+1)
        elif isinstance(item,list):
            if len(item)>100:raise ValueError('fixture list exceeds 100 items')
            for v in item:inspect(v,depth+1)
        elif isinstance(item,str):
            if len(item)>4000 or _SECRET.search(item) or _PII.search(item):raise ValueError('fixture contains sensitive or oversized text; curate a safe case')
        elif item is not None and type(item) not in {bool,int,float}:raise ValueError('fixture must be JSON')
        elif isinstance(item,float) and not math.isfinite(item):raise ValueError('fixture numbers must be finite')
    inspect(value)
    if len(json.dumps(value,ensure_ascii=False).encode())>16000:raise ValueError('fixture exceeds 16 KB')
    return value

def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()


def prepare_run(dataset,load_case,execute,*,variant,release_sha,configuration=None,privacy_reviewed=False):
    """Execute the application locally on exact pinned inputs; never pass expected outputs to it."""
    if privacy_reviewed is not True:raise ValueError('review fixture content before running')
    definition=dataset['definition'];refs=definition['cases']
    if not 1<=len(refs)<=100 or len({r['id'] for r in refs})!=len(refs):raise ValueError('invalid dataset cases')
    if digest(definition)!=dataset['sha256']:raise ValueError('dataset bytes changed')
    if not re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}',variant) or not re.fullmatch(r'[a-f0-9]{40}',release_sha):raise ValueError('invalid variant or release')
    if configuration is not None and not isinstance(configuration,dict):raise ValueError('configuration must be a JSON object')
    configuration=reviewed_json(configuration or {});results=[]
    for ref in refs:
        case=load_case(ref['id'],ref['version'])
        if case['project']!=dataset['project'] or case['id']!=ref['id'] or case['version']!=ref['version'] or digest(case['definition'])!=ref['sha256'] or case['sha256']!=ref['sha256']:raise ValueError('case does not match pinned dataset')
        if min(case['expires'],dataset['expires'])<=time.time():raise ValueError('fixture expired')
        fixture=copy.deepcopy(reviewed_json(case['definition']['input']));started=time.monotonic()
        try:
            output=execute(fixture)
        except Exception:
            result={'error_code':'internal'}
        else:
            try:
                if not isinstance(output,dict):raise ValueError('output must be a JSON object')
                result={'output':copy.deepcopy(reviewed_json(output))}
            except ValueError:result={'error_code':'unsupported'}
        duration=(time.monotonic()-started)*1000
        if duration>86400000:raise ValueError('case execution exceeded supported duration')
        results.append({'id':ref['id'],'version':ref['version'],'duration_ms':duration,**result})
    return {'dataset':dataset['id'],'dataset_version':dataset['version'],'variant':variant,'release_sha':release_sha,
            'evaluator_runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'configuration':configuration,'privacy_reviewed':True,'results':results}
