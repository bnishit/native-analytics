"""Run the exact public draft-PR pilot locally through the real Python collector."""
import argparse
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import urllib.parse
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--state',type=Path,required=True);parser.add_argument('--report',type=Path,required=True);args=parser.parse_args()
    connection=json.loads((args.state/'connection.json').read_text());action=json.loads((args.state/'action.json').read_text());owner=json.loads((args.state/'owner.json').read_text());producer=json.loads((args.state/'producer.json').read_text());state=json.loads((args.state/'state.json').read_text())
    origin=urllib.parse.urlsplit(connection['origin'])
    assert origin.scheme=='http' and origin.hostname=='127.0.0.1' and not origin.path
    receipt=action['reconciled']['receipt'];plan=action['prepared']['plan']
    assert re.fullmatch(r'[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+',connection['repository'])
    assert re.fullmatch(r'[0-9a-f]{40}',receipt['head_sha'])
    source_url='https://raw.githubusercontent.com/'+connection['repository']+'/'+receipt['head_sha']+'/examples/loop_pilot.py'
    with urllib.request.urlopen(source_url,timeout=20) as response:source=response.read(20001)
    assert len(source)<=20000 and source.decode()==plan['files'][0]['content'],'PR source does not match the verified plan'
    scope={};exec(compile(source,source_url,'exec'),scope)
    spec=importlib.util.spec_from_file_location('pilot_collector',ROOT/'sdk/python/native_analytics.py');sdk=importlib.util.module_from_spec(spec);spec.loader.exec_module(sdk)
    collector=sdk.Collector(connection['origin'],producer['write_key'],args.state/('sdk-'+uuid.uuid4().hex+'.db'))
    subject='sdk-pilot-'+uuid.uuid4().hex;answer={'source':'native','mature':0,'rate':None};observation_id=state['zero']['id']
    assert scope['answer_question'](collector,subject,observation_id,answer,receipt['head_sha'])==answer
    with closing(collector.db()) as c:payload=[json.loads(row[0]) for row in c.execute('SELECT payload FROM outbox ORDER BY created')]
    assert [row['name'] for row in payload]==['codex.connection.started','codex.answer.ready'],payload
    assert collector.flush()==2 and collector.status()['queued']==0
    req=urllib.request.Request(connection['origin']+'/v1/events',data=json.dumps({'events':payload}).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+producer['write_key']})
    with urllib.request.urlopen(req,timeout=20) as response:duplicate=json.load(response)
    assert duplicate['accepted']==0 and duplicate['duplicates']==2,duplicate
    query=urllib.parse.urlencode({'project':connection['project'],'distinct_id':subject})
    req=urllib.request.Request(connection['origin']+'/api/events?'+query,headers={'Cookie':'as_session='+owner['session']})
    with urllib.request.urlopen(req,timeout=20) as response:stored=json.load(response)['events']
    assert {row['id'] for row in stored}=={row['id'] for row in payload}
    assert all(row['properties']['release']==receipt['head_sha'] and row['properties']['observation_id']==observation_id for row in stored)
    assert all(not any(key in row['properties'] for key in ['answer','question','password','token']) for row in stored)
    report={'environment':'local','source':'exact public draft PR head','pr_url':receipt['pr_url'],'head_sha':receipt['head_sha'],'source_sha256':hashlib.sha256(source).hexdigest(),'collector_events':len(stored),'names':sorted(row['name'] for row in stored),'duplicate_receipt':duplicate,'queue_drained':True,'limits':['Local execution of the reviewed pilot is not a production deployment.','A producer release label is not provider release proof.','Collection does not establish subjective answer usefulness.']}
    args.report.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'ok':True,'events':len(stored),'report':str(args.report)}))

if __name__=='__main__':main()
