"""Exercise optional integrations using the installed wheel, outside the checkout."""
import asyncio
from contextlib import closing
import inspect
import json
from pathlib import Path
import runpy
import sys
from native_analytics import Collector
from afterspan_tracing import WorkflowTracer

repo,root=map(Path,sys.argv[1:])
for name,recipe,observations in [('langchain','langchain',30),('anthropic','anthropic-sdk-fixture',24)]:
    collector=Collector('https://analytics.invalid','synthetic-package-key',root/(name+'.db'))
    result=runpy.run_path(str(repo/'examples'/recipe/'emit.py'))['exercise'](WorkflowTracer(collector,subject_id='account_'+'a'*64))
    if inspect.isawaitable(result):result=asyncio.run(result)
    if name=='langchain':assert len(result)==3 and sum(x['framework_failed'] for x in result)==1
    else:assert result['loopback_fixture_requests']==6 and result['native_provider_calls']==0
    with closing(collector.db()) as db:events=[json.loads(r[0]) for r in db.execute('SELECT payload FROM outbox ORDER BY created')]
    assert len(events)==observations
    for canary in ['DO_NOT_LEAK_','private','sk-ant-fixture-not-a-real-key']:assert canary not in json.dumps(events)
    (root/(name+'-events.json')).write_text(json.dumps(events))
