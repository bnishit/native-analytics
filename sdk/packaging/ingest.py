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
    for name in ['python-events.json', 'node-events.json']:
        events = json.loads((artifacts / name).read_text())
        response = fixture.ingest(events)
        assert response.status_code == 200, response.text
        assert response.json()['accepted'] == len(events), response.text
    traces = fixture.client.post('/api/traces/review?project=alpha', headers=fixture.admin, json={'environment': 'test'}).json()['traces']
    assert len(traces) == 2, traces
    assert all(trace['completion']['response']['state'] == 'succeeded' for trace in traces)
    assert all(trace['checks'][0]['evidence_status'] == 'resolved' for trace in traces)
    assert all(trace['checks'][0]['execution'] == 'completed' for trace in traces)
    assert all(trace['checks'][1]['execution'] == 'not_run' and trace['checks'][1]['reason_code'] == 'evidence_absent' and trace['checks'][1]['result'] == 'unknown' and not trace['checks'][1]['evidence_present'] for trace in traces)
    print(json.dumps({'ingested_languages': 2, 'traces': len(traces)}))
finally:
    fixture.tearDown()
