"""Ingest installed-package payloads into the real local test service."""
import json
from pathlib import Path
import sys
repo, artifacts = map(Path, sys.argv[1:])
sys.path.insert(0, str(repo))
sys.path.insert(0, str(repo / 'tests'))
from test_analytics import AnalyticsTests
fixture = AnalyticsTests()
fixture.setUp()
try:
    for name in ['python-events.json', 'node-events.json', 'langchain-events.json', 'anthropic-events.json']:
        events = json.loads((artifacts / name).read_text())
        assert all(event['distinct_id']=='account_'+'a'*64 and not event.get('attributes') for event in events)
        response = fixture.ingest(events)
        assert response.status_code == 200, response.text
        assert response.json()['accepted'] == len(events), response.text
    traces = fixture.client.post('/api/traces/review?project=alpha', headers=fixture.admin, json={'environment': 'test'}).json()['traces']
    assert len(traces) == 13, traces
    framework=[t for t in traces if t['workflow']=='langchain_interop']
    assert len(framework)==3
    assert sorted(len(t['spans']) for t in framework)==[3,6,6]
    assert sum(any(s['state']=='failed' for s in t['spans']) for t in framework)==1
    assert all(t['business_outcome']=='not_established' and not t['checks'] for t in framework)
    provider_fixtures=[t for t in traces if t['workflow'].startswith('anthropic_fixture_')]
    assert len(provider_fixtures)==6
    for trace in provider_fixtures:
        case=trace['workflow'].removeprefix('anthropic_fixture_');expected='succeeded' if case in {'complete','reasoning_only'} else 'failed'
        assert len(trace['spans'])==2 and not trace['issues'] and not trace['checks']
        assert all(s['state']==expected and not s['parent_missing'] and not s['missing_start'] and s['milestone'] is None for s in trace['spans'])
        model=next(s for s in trace['spans'] if s['step']=='model');usage=model['model_usage']
        assert usage['provider']=='anthropic_fixture' and usage['model']=='fixture/messages' and usage['cost_usd_decimal'] is None
        assert trace['business_outcome']=='not_established'
        if case=='complete':assert usage['input_count']==20 and usage['output_count']==5 and usage['cache_read_count']==10 and 0<=usage['first_output_ms']<=model['duration_ms']
        if case=='reasoning_only':assert usage['input_count'] is None and usage['cache_read_count'] is None and usage['first_output_ms'] is None and usage['output_count']==3
    streamed={trace['workflow']:trace for trace in traces if trace['workflow'].startswith('package_stream_')}
    assert set(streamed)=={'package_stream_complete','package_stream_cancel'}
    for name,trace in streamed.items():
        expected='failed' if name.endswith('cancel') else 'succeeded'
        assert all(span['state']==expected for span in trace['spans']), trace
        if name.endswith('cancel'):assert all(span['outcome_code']=='cancelled' for span in trace['spans']),trace
        assert trace['business_outcome']=='not_established'
        model=next(span for span in trace['spans'] if span['step']=='model')
        assert 0<=model['model_usage']['first_output_ms']<=model['duration_ms']
        assert all(span['milestone'] is None and span['disposition'] is None for span in trace['spans'])
    traces=[trace for trace in traces if trace['workflow']=='package_probe']
    assert len(traces)==2
    assert all(trace['completion']['response']['state'] == 'succeeded' for trace in traces)
    assert all(trace['checks'][0]['evidence_status'] == 'resolved' for trace in traces)
    assert all(trace['checks'][0]['execution'] == 'completed' for trace in traces)
    assert all(trace['checks'][1]['execution'] == 'not_run' and trace['checks'][1]['reason_code'] == 'evidence_absent' and trace['checks'][1]['result'] == 'unknown' and not trace['checks'][1]['evidence_present'] for trace in traces)
    for trace in traces:
        usage={span['model_usage']['model']:span['model_usage'] for span in trace['spans'] if span.get('model_usage')}
        assert usage['fixture/responses']['input_count']==10 and usage['fixture/responses']['reasoning_count']==2, usage
        assert usage['fixture/messages']['input_count']==20 and usage['fixture/messages']['output_count']==5, usage
        assert usage['fixture/messages']['cache_read_count']==10, usage
        assert all(usage[key]['first_output_ms'] is not None and usage[key]['cost_usd_decimal'] is None for key in ['fixture/responses','fixture/messages']), usage
    normalized=[span['model_usage'] for trace in traces for span in trace['spans'] if (span.get('model_usage') or {}).get('model')=='fixture/ai-sdk']
    assert len(normalized)==1
    assert all(normalized[0][key]==value for key,value in {'input_count':20,'output_count':7,'cache_read_count':10,'reasoning_count':2,'usage_source':'provider_reported','cost_usd_decimal':None,'first_output_ms':None}.items())
    assert all('account_' not in json.dumps(trace) for trace in traces+list(streamed.values()))
    erased=fixture.client.post('/v1/erase',headers=fixture.a,json={'distinct_id':'account_'+'a'*64})
    assert erased.status_code==200 and erased.json()['erased_events']>0
    assert fixture.client.post('/api/traces/review?project=alpha',headers=fixture.admin,json={'environment':'test'}).json()['traces']==[]
    print(json.dumps({'ingested_languages': 2, 'traces': len(traces)+len(streamed)+len(framework)+len(provider_fixtures), 'framework_traces':len(framework), 'provider_fixture_traces':len(provider_fixtures), 'stream_lifecycles': len(streamed), 'subject_erasure': 'passed', 'normalized_ai_sdk_usage': 'passed'}))
finally:
    fixture.tearDown()
