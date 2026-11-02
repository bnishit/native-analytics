# Workflow SDKs

A developer wraps a task and its timed steps. Nested calls and concurrent async tasks keep separate context. The helpers emit the existing strict workflow contract, not user content. A completed function is not a correct answer or useful outcome: declare checks and goal milestones separately.

## Python

Put `sdk/python` on the application import path. The standard-library collector persists telemetry in its SQLite outbox before sending. Keep its database on a private writable volume; a failed local write remains a collection gap.

```python
from native_analytics import Collector
from afterspan_tracing import WorkflowTracer, inject, extract

collector = Collector(origin, write_key, private_outbox_path)
tracer = WorkflowTracer(collector)
with tracer.task('document_export', 'production', release_sha=release_sha):
    with tracer.span('storage', milestone='completion') as span:
        result = export_document()
        span.check('source_facts', 'passed', evidence_digest=source_digest)
    with tracer.span('response', milestone='delivery', disposition='delivered'):
        send_download_link(result)
collector.flush()
```

`source_digest` is a SHA-256 digest computed by the producer over its canonical evidence, never the source body. Only emit a passed check after actually checking the result. A resolved receipt proves matching producer evidence, not an independent Afterspan source fetch.

## Node

Import `WorkflowTracer` from `sdk/typescript/tracing-node.ts` and `createAnalytics` from the adjacent client. Node 24+ can execute these files with `--experimental-strip-types`; an application TypeScript build can compile them normally. The Node context helper uses `AsyncLocalStorage` and is server-only.

```typescript
const tracer = new WorkflowTracer(createAnalytics({url: origin, writeKey}));
await tracer.task({workflow:'document_export', environment:'production', releaseSha}, async () => {
  await tracer.span('storage', async span => {
    await exportDocument();
    span.check('source_facts', 'passed', {evidenceDigest: sourceDigest});
  }, {milestone:'completion'});
});
await tracer.flush(); // register with the serverless host's after/waitUntil hook
```

The Node client uses a bounded in-memory queue. It is best effort across process termination; it does not claim Python's durable outbox. Status exposes queued, dropped and rejected events, last delivery/error and the oldest queued clock. Throwing error callbacks cannot interrupt business work. Neither helper retries a business action after telemetry failure. Application exceptions are rethrown unchanged; their messages are never captured.

## HTTP and queue handoff

Inside a span, call `inject()` and send its W3C `traceparent` and bounded `baggage` as headers or queue metadata. At a trusted internal boundary, Python `extract(headers, trusted=True)` or Node `extract(headers, true)` validates that carrier, then pass the returned context to `task`. Public headers are untrusted by default. Do not forward arbitrary baggage or credentials. Invalid carriers return no context; the application chooses whether to start a separate task or reject its own request.

Keep the same context through a continuation. For an actual business retry use Python `context.retry()` or Node `retryContext(context)`: it retains the original request clock and trace ID, creates a new attempt ID and increments the attempt number. Never use it for delivery retries. Unsampled (`00`) context propagates but exports no observations; status counts those omissions.

Both helpers use full W3C IDs and one deterministic SHA-256 mapping into the existing UUID workflow store. The namespace is `afterspan-otel-v1`, followed by a NUL and full trace ID; span mapping adds another NUL and the full span ID. The first 16 digest bytes are normalized to UUIDv4 bits. This prevents lossy span-ID padding and keeps Python/Node identities identical. The mapping alone is not deployment evidence; the separate official-exporter production check is recorded in [release evidence](outcome-platform-release.md).

## Evidence and limits

Local tests ingest the exact Python and Node payloads through the real service, join a Python parent to a Node child, and resolve a check receipt. Concurrent contexts, sampling, retry clocks, duplicate check names, closed spans, invalid IDs and telemetry failures are tested. These are synthetic local runs, not production adoption. There is no automatic prompt/output capture or provider auto-instrumentation. Optional selected content uses the separate [governed debugging flow](debugging.md), never the normal collector. Model usage and OTLP mappings are described in [OTLP intake](otlp.md).

Standards: [W3C Trace Context](https://www.w3.org/TR/trace-context/), [OpenTelemetry trace API](https://opentelemetry.io/docs/specs/otel/trace/api/). The complete release gate remains in [platform acceptance](outcome-platform-release.md).
