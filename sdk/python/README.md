# Afterspan Python SDK

Metadata-only tracing, model observations and a durable SQLite collector. Python 3.12+; the base install has no runtime dependencies. Four core companion modules and one optional LangChain adapter are included. Install the `langchain` extra to use the adapter; importing the core never loads LangChain.

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

The optional `afterspan_langchain.AfterspanCallbackHandler` records nested framework execution without copying content. Model identity is allowlisted; reported usage needs a separate explicit opt-in. See `examples/langchain/README.md` for the per-request context manager, bounded diagnostics and synthetic recipe.

The repository also has `examples/anthropic-sdk-fixture`: an official Anthropic SDK recipe using a dummy key and a local server. It tests the existing Messages observer without a real provider call; Anthropic is not a base SDK dependency.
