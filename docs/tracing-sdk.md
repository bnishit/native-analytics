# Workflow SDKs

A developer wraps a task and its timed steps. Nested calls and concurrent async tasks keep separate context. The helpers emit the existing strict workflow contract, not user content. A completed function is not a correct answer or useful outcome: declare checks and goal milestones separately.

## Install a pinned SDK

The Python distribution is `afterspan-sdk`; the server-only ESM Node package is `@afterspan/sdk`. Neither name claims publication on PyPI or npm. Python 3.12+ and Node 24+ are supported; CI tests Python 3.12 and Node 24. Both SDKs have zero runtime package dependencies. Python's build uses setuptools/wheel; Node's build uses pinned TypeScript and Node type declarations.

Pinned Git installation is verified at the released commit below in fresh consumers. Node also passed a production `--omit=dev` lockfile install with a fresh cache and SSH disabled. Python preserves the existing flat-module imports. No registry account or publication is needed. npm Git installation runs the reviewed `prepare` build and downloads its pinned build dependencies.

```sh
python -m pip install 'afterspan-sdk @ git+https://github.com/bnishit/native-analytics.git@fdb555569f20abbe21efc215c3178b4a374e6962#subdirectory=sdk/python'
npm install 'git+https://github.com/bnishit/native-analytics.git#fdb555569f20abbe21efc215c3178b4a374e6962'
```

### Build and verify artifacts locally

Build artifacts from a reviewed checkout, then install them in your application:

```sh
python -m pip wheel --no-deps ./sdk/python --wheel-dir /private/sdk-build
# In a separate application environment:
python -m pip install /private/sdk-build/afterspan_sdk-0.1.0-py3-none-any.whl

# In the SDK checkout (prepare builds JavaScript and declarations):
npm ci
npm pack --pack-destination /private/sdk-build
# In your Node application:
npm install /private/sdk-build/afterspan-sdk-0.1.0.tgz
```

The wheel includes all four Python modules. The tarball includes ordinary JavaScript, declarations, the standalone model observer and its required companions; it does not rely on stripping TypeScript from `node_modules`. Existing source-file imports still work for already vendored integrations. Pin the artifact hash or source revision when updating an application. When migrating an existing vendored integration, check which module is imported: local copies can shadow an installed Python package.

Run `python scripts/check_sdk_packages.py` after installing the repository's build and service test dependencies. This mandatory CI check builds and allowlists both archives, installs fresh consumers outside the checkout without `PYTHONPATH`, resolves TypeScript types, and ingests actual emitted payloads through the local service fixture. Missing-companion negative controls must fail imports; reinstalling the intact artifacts must restore the consumers. Python's bundled license must match the repository license byte for byte. The check does not call a provider or deployment.

## Python

Import the installed modules directly. The standard-library collector persists telemetry in its SQLite outbox before sending. Keep its database on a private writable volume; a failed local write remains a collection gap.

```python
from native_analytics import Collector
from afterspan_tracing import WorkflowTracer, inject, extract

collector = Collector(origin, write_key, private_outbox_path)
tracer = WorkflowTracer(collector)
with tracer.task('document_export', 'production', release_sha=release_sha):
    with tracer.span('storage', milestone='completion') as span:
        result = export_document()
        span.check('source_facts', 'passed', evidence_digest=source_digest,
                   execution='completed')
    with tracer.span('response', milestone='delivery', disposition='delivered'):
        send_download_link(result)
collector.flush()
```

`source_digest` is a SHA-256 digest computed by the producer over its canonical evidence, never the source body. Only emit a passed check after actually checking the result. A resolved receipt proves matching producer evidence, not an independent Afterspan source fetch.

## Node

Import from the installed `@afterspan/sdk` ESM package. Plain JavaScript applications use the same imports; TypeScript applications also receive declarations. The Node context helper uses `AsyncLocalStorage` and is server-only.

```typescript
import { WorkflowTracer, createAnalytics } from '@afterspan/sdk';

const tracer = new WorkflowTracer(createAnalytics({url: origin, writeKey}));
await tracer.task({workflow:'document_export', environment:'production', releaseSha}, async () => {
  await tracer.span('storage', async span => {
    await exportDocument();
    span.check('source_facts', 'passed', {evidenceDigest: sourceDigest, execution:'completed'});
  }, {milestone:'completion'});
});
await tracer.flush(); // register with the serverless host's after/waitUntil hook
```

The Node client uses a bounded in-memory queue. It is best effort across process termination; it does not claim Python's durable outbox. Status exposes queued, dropped and rejected events, last delivery/error and the oldest queued clock. Throwing error callbacks cannot interrupt business work. Neither helper retries a business action after telemetry failure. Application exceptions are rethrown unchanged; their messages are never captured.

## Check execution and missing evidence

Only report `completed` after the comparison actually ran. An inconclusive comparison can be `completed` with result `unknown`; completing a span does not establish this. If evidence is absent or fails this task's binding checks, keep the result unknown and explain why the check did not run:

```python
span.check('source_facts', 'unknown', execution='not_run',
           reason_code='evidence_absent')
```

```typescript
span.check('source_facts', 'unknown', {
  execution:'not_run', reasonCode:'evidence_binding_rejected'
});
```

These are alternative terminal states for the check, not additional calls after a passed check of the same name. The two reason codes are fixed enums, never raw error or source text. A not-run check cannot carry a passed/failed result or an evidence digest. Omit execution when it is unknown; older traces retain that distinction. Neither execution nor a reason satisfies a required correctness check.

The pinned revision above passed fresh Python and Node Git installs and installed-consumer checks with mocked transport. A separate installed Python SDK probe reached the production service in an isolated synthetic project: search/open retained completed-but-unknown and both not-run reasons. This proves service acceptance and search, not a second product adoption or normal-user success.

## Model usage and first output

Model metadata belongs to one open `model` span. Declare its provider, model and optional instruction-template version before observing chunks. Python uses `model_usage`, `first_output` and `observe_openai`; Node uses `modelUsage`, `firstOutput` and `observeOpenAI`. All methods retain metadata only. Other steps and closed spans reject updates.

```python
with tracer.span('model') as span:
    span.model_usage(provider='openrouter', model='openai/gpt-4.1-mini',
                     template_version='reply-v1')
    for chunk in provider_stream:
        span.observe_openai(chunk)
        handle_chunk(chunk)  # Your application owns output and completion checks.
```

```typescript
await tracer.span('model', async span => {
  span.modelUsage({provider:'openrouter', model:'openai/gpt-4.1-mini',
                   template_version:'reply-v1'});
  for await (const chunk of providerStream) {
    span.observeOpenAI(chunk);
    handleChunk(chunk); // Your application owns output and completion checks.
  }
});
```

These examples observe already parsed Chat Completions chunks, including official Python SDK objects. They do not open a provider connection, parse SSE bytes, validate a full answer, retry a provider request or stream a reply to the user. The transport must reject errors and incomplete/truncated streams. An HTTP200 or a first delta is not successful completion.

The first nonempty content, refusal or tool-function delta records `first_output_ms` once against the span's monotonic clock. Role-only chunks, empty deltas, usage frames and heartbeats do not supply first output. This is observed model-output timing, not Slack acceptance or the user's first visible reply. No qualifying delta means unknown, not zero. Manual adapters call `first_output()` / `firstOutput()` at their actual output boundary; they cannot set an arbitrary timing field through the metadata setter.

The metadata setter accepts the existing wire fields: input/output/cache/reasoning counts, `usage_source`, one exact USD amount and `cost_source`, with `price_version` required for price-table estimates. Counts are bounded integers; unknown content fields and invalid relationships are rejected. Only explicit zero is zero. Exact decimal cost is a bounded string, never a binary floating-point conversion. If the provider SDK exposes USD only as a float, the chunk observer leaves it unknown. Pass independently preserved exact text as the observer's second argument, or set a properly sourced exact amount explicitly. No catalog-price estimate is inferred.

`ModelObservation` in `sdk/python/afterspan_model.py` and `sdk/typescript/model-observation.ts` offers the same metadata/timing state without a workflow collector. Existing tracers can reuse it without emitting a second event stream. Installed packages include the companion automatically. Standalone Node callers can use `import { ModelObservation } from '@afterspan/sdk/model'`; Python callers use `from afterspan_model import ModelObservation`. Existing vendored entrypoints still need their matching sibling helper until migrated. Snapshots are copies; closing prevents further updates. Malformed provider metadata clears uncertain accounting instead of retaining stale totals and increments `rejected_updates` / `rejectedUpdates`. Native tracer status accumulates `model_metadata_errors`. Telemetry send failure never reruns the business call.

No prompt, output, refusal text, tool arguments or raw provider error is retained by these observers. Selected debugging remains a separate opt-in path. The server still enforces model-step, terminal-observation, usage-provenance and first-output-within-duration rules. The SDK's two languages and actual cross-language ingestion are tested locally; current production adoption is tracked separately in [release acceptance](outcome-platform-release.md).

## Responses and Messages observation

The pinned SDK also exposes `observe_responses` / `observeResponses` for parsed OpenAI Responses objects and events, and `observe_anthropic` / `observeAnthropic` for parsed Anthropic Messages objects and events. These methods are available on an open model span and the standalone observer. The Git installation above includes both methods; earlier SDK revisions require an update. These are explicit observers, not automatic provider/framework wrappers. They add no provider dependency or transport.

```python
with tracer.span('model') as span:
    span.model_usage(provider='anthropic', model=requested_model)
    for event in messages_stream:
        span.observe_anthropic(event)
        handle_event(event)
```

```typescript
await tracer.span('model', async span => {
  span.modelUsage({provider:'openai', model:requestedModel});
  for await (const event of responsesStream) {
    span.observeResponses(event);
    handleEvent(event);
  }
});
```

Responses usage maps reported input/output totals, cached input and reasoning output from the response object, including completed, incomplete and failed lifecycle envelopes. This records incurred usage, never successful completion. Nonempty text, refusal and function-argument deltas, or a named function-call item, start the output clock. Reasoning text and lifecycle events do not. Audio, image, built-in tool and reasoning-content timing are unsupported, not inferred.

Messages usage replaces cumulative output counts; repeated totals do not add tokens. Total input is the sum of uncached, cache-creation and cache-read input, only when all three components are known. Missing components are unknown, never implicit zero. Partial stream updates retain earlier reported components. Text and tool-use/JSON deltas start output timing; thinking, signatures and pings do not. No reasoning-token breakdown is inferred. Use one observer per model request, feed parsed events in delivery order, and do not combine providers or concurrent requests in one observer. Full non-streaming responses record usage but leave first-output timing unknown.

Neither format reports USD cost through these methods. Record a separate exact amount with its real provenance only when available. A provider update cannot relabel prior estimated token counts as provider-reported. Invalid accounting clears uncertain counts and increments the same existing diagnostic counter. These observers retain no prompt, response, reasoning text, function name or tool argument. The caller must surface provider errors and incomplete streams; recording their usage never marks the span or task successful.

The contract tests and fresh installed-package consumers use synthetic provider-shaped events. They assert both language outputs against the local service, including cached input, cumulative output, unknown cost and private-content omission. Separately, two bounded synthetic streaming requests through the existing production producer’s OpenRouter-compatible Responses and Messages endpoints completed with expected output, reported input/output totals of 6/1 each, and first-output timings of 518.294 and 381.246 ms. These calls used the reviewed compiled standalone observer without changing the producer’s installed dependency. They prove gateway-format observation, not native OpenAI/Anthropic endpoints, framework-wide coverage, a second customer integration or a latency estimate. Fresh Python/Node Git installs at the pin above verified the new exports; wheel/tarball consumers separately verified service ingestion.

Provider contracts: [OpenAI Responses usage types](https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_usage.py), [Responses event types](https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_stream_event.py), [Anthropic cumulative streaming usage](https://platform.claude.com/docs/en/build-with-claude/streaming), and [cached-input accounting](https://platform.claude.com/docs/en/build-with-claude/prompt-caching). These are the documented mapping sources, not a claim of all-provider support.

## Bounded installed-helper production adoption

Tracker imports `ModelObservation` from the pinned `@afterspan/sdk/model` package in production, replacing its identical copied helper while keeping one existing request trace stream. Runtime import and one actual source-checked Slack reply verified model usage, cost and two streamed first-output timings. This does not exercise the full native workflow tracer, other providers/frameworks or a second product; see [release evidence](outcome-platform-release.md).

## HTTP and queue handoff

Inside a span, call `inject()` and send its W3C `traceparent` and bounded `baggage` as headers or queue metadata. At a trusted internal boundary, Python `extract(headers, trusted=True)` or Node `extract(headers, true)` validates that carrier, then pass the returned context to `task`. Public headers are untrusted by default. Do not forward arbitrary baggage or credentials. Invalid carriers return no context; the application chooses whether to start a separate task or reject its own request.

Keep the same context through a continuation. For an actual business retry use Python `context.retry()` or Node `retryContext(context)`: it retains the original request clock and trace ID, creates a new attempt ID and increments the attempt number. Never use it for delivery retries. Unsampled (`00`) context propagates but exports no observations; status counts those omissions.

Both helpers use full W3C IDs and one deterministic SHA-256 mapping into the existing UUID workflow store. The namespace is `afterspan-otel-v1`, followed by a NUL and full trace ID; span mapping adds another NUL and the full span ID. The first 16 digest bytes are normalized to UUIDv4 bits. This prevents lossy span-ID padding and keeps Python/Node identities identical. The mapping alone is not deployment evidence; the separate official-exporter production check is recorded in [release evidence](outcome-platform-release.md).

## Evidence and limits

Local tests ingest the exact Python and Node payloads through the real service, join a Python parent to a Node child, and resolve a check receipt. Concurrent contexts, sampling, retry clocks, duplicate check names, closed spans, invalid IDs and telemetry failures are tested. These are synthetic local runs, not production adoption. There is no automatic prompt/output capture or transparent provider monkey-patching; parsed-chunk observation requires the explicit model-span calls above. Optional selected content uses the separate [governed debugging flow](debugging.md), never the normal collector. Model usage and OTLP mappings are described in [OTLP intake](otlp.md).

Standards: [W3C Trace Context](https://www.w3.org/TR/trace-context/), [OpenTelemetry trace API](https://opentelemetry.io/docs/specs/otel/trace/api/). The complete release gate remains in [platform acceptance](outcome-platform-release.md).
