"""Run only in an isolated environment with the installed wheel."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
from unittest.mock import patch
from native_analytics import Collector
from afterspan_tracing import WorkflowTracer
from afterspan_model import ModelObservation
from afterspan_experiments import prepare_run

root = Path(sys.argv[1])
assert Path(importlib.metadata.distribution('afterspan-sdk').locate_file('afterspan_model.py')).is_relative_to(Path(sys.prefix))
collector = Collector('https://analytics.invalid', 'synthetic-package-key', root / 'outbox.db')
assert collector.max_queue == 100000
tracer = WorkflowTracer(collector)
with tracer.task('package_probe', 'test', traffic_kind='synthetic'):
    with tracer.span('model') as span:
        span.model_usage(provider='openai', model='fixture/model')
        span.observe_openai({'choices': [{'delta': {'content': 'private-content-sentinel'}}]})
        span.model_usage(input_count=4, output_count=2, usage_source='provider_reported', cost_usd_decimal='0.000000254', cost_source='provider_reported')
    with tracer.span('processing', milestone='completion') as span:
        result = 2 + 2
        assert result == 4
        span.check('arithmetic', 'passed', method='code', evidence_digest=hashlib.sha256(str(result).encode()).hexdigest())
    with tracer.span('response', milestone='delivery', disposition='delivered'):
        pass
reopened = Collector('https://analytics.invalid', 'synthetic-package-key', root / 'outbox.db')
assert reopened.status()['queued'] > 0
with reopened.db() as db:
    events = [json.loads(row[0]) for row in db.execute('SELECT payload FROM outbox ORDER BY created')]
assert 'private-content-sentinel' not in json.dumps(events)
assert any(e['properties'].get('afterspan_trace', {}).get('model_usage', {}).get('cost_usd_decimal') == '0.000000254' for e in events)
class Offline:
    def open(self, request, timeout):
        assert timeout == 5
        raise OSError('synthetic offline')
with patch('urllib.request.build_opener', return_value=Offline()):
    assert reopened.flush() == 0
assert reopened.status()['queued'] == len(events)
(root / 'python-events.json').write_text(json.dumps(events))
