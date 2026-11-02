# Afterspan Python SDK

Metadata-only tracing, model observations and a durable SQLite collector. Python 3.12+; no runtime dependencies. This distribution includes all four companion modules.

```python
from native_analytics import Collector
from afterspan_tracing import WorkflowTracer
from afterspan_model import ModelObservation
from afterspan_experiments import prepare_run

collector = Collector(origin, write_key, private_outbox_path)
tracer = WorkflowTracer(collector)
with tracer.task('document_export', 'test'):
    with tracer.span('storage'):
        export_document()
collector.flush()
```

Use your own authorized origin and project write key. The SQLite outbox needs a private writable volume. A local persistence failure remains a collection gap; a returned function is not proof of answer correctness or user value. No provider connection or content capture is automatic. See the repository's `docs/tracing-sdk.md` for checks, propagation, delivery limits and installation evidence.
