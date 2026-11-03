# Workflow SDKs

A developer wraps a task and its timed steps. Nested calls and concurrent async tasks keep separate context. The helpers emit the existing strict workflow contract, not user content. A completed function is not a correct answer or useful outcome: declare checks and goal milestones separately.

## Install a pinned SDK

The Python distribution is `afterspan-sdk`; the server-only ESM Node package is `@afterspan/sdk`. Neither name claims publication on PyPI or npm. Python 3.12+ and Node 24+ are supported; CI tests Python 3.12 and Node 24. The base SDKs have zero runtime package dependencies; Python’s optional LangChain extra adds LangChain Core. Python's build uses setuptools/wheel; Node's build uses pinned TypeScript and Node type declarations.

Pinned Git installation is verified at the released commit below in fresh consumers. Node also passed a production `--omit=dev` lockfile install with a fresh cache and SSH disabled. Python preserves the existing flat-module imports. No registry account or publication is needed. npm Git installation runs the reviewed `prepare` build and downloads its pinned build dependencies.

```sh
python -m pip install 'afterspan-sdk @ git+https://github.com/bnishit/native-analytics.git@2db18488316a0b61f977f14294957ee7fefc1443#subdirectory=sdk/python'
npm install 'git+https://github.com/bnishit/native-analytics.git#2db18488316a0b61f977f14294957ee7fefc1443'
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

The wheel includes four core Python modules and the optional LangChain adapter. The tarball includes ordinary JavaScript, declarations, the standalone model observer and its required companions; it does not rely on stripping TypeScript from `node_modules`. Existing source-file imports still work for already vendored integrations. Pin the artifact hash or source revision when updating an application. When migrating an existing vendored integration, check which module is imported: local copies can shadow an installed Python package.

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

## Normalized AI SDK usage (Node)

`span.observeAISDKUsage(usage)` and standalone `ModelObservation.observeAISDKUsage(usage)` read one normalized AI SDK step's usage without importing the framework. Declare the model identity on an open model span first. Read `inputTokens` and `outputTokens` as inclusive totals; cache-read and reasoning counts are subsets, not extra tokens. Detail objects and the legacy `cachedInputTokens` / `reasoningTokens` aliases are supported; contradictory aliases reject the snapshot. Do not pass raw provider usage objects or aggregate multiple models under one model identity.

Use the already consumed step's `onStepFinish({usage})` callback while that model span is open. Consume the text stream normally. Do not await `result.usage` or `result.totalUsage` solely for telemetry: these accessors can consume the stream and change cancellation or latency. With multi-step or model-switching agents, instrument each actual model call separately rather than attaching summed task usage to one model. The observer never starts a provider call, awaits metadata or retries business work.

Each invocation replaces count fields, including unknowns; repeated snapshots are not summed and old estimates are not relabelled as reported totals. Only explicit zero is zero. Missing counts stay unknown. Malformed count/detail shapes clear uncertain accounting and increment the existing metadata-error counter. Raw usage, text, model identity, total-token shortcuts and numeric cost fields are not copied. This method does not start the first-output clock, set exact cost, declare a passed check or mark task completion. Keep exact cost separately sourced through the existing metadata setter; a provider's floating-point cost is not an exact decimal receipt.

The observer can only preserve what the normalized object still distinguishes. Some third-party adapters synthesize zeros for omitted provider fields. Verify that contract before calling this reader. OpenRouter adapter 2.2.3 defaults missing cache/reasoning details to zero; its preserved Chat Completions usage is therefore read with the existing `span.observeOpenAI({usage: usage.raw})` instead. That shared reader selects supported accounting fields, keeps omitted counts unknown and ignores a numeric cost rather than calling it exact. It does not copy the raw object or content into telemetry.

Bounded live adoption: Shelf's authenticated, synthetic-labelled Standard Q&A uses one existing model call, consumes the full stream to reject explicit errors, aborts and known length truncation, and reads preserved provider usage once per step. The production test reports 13,797 input / 192 output tokens on one model span, with exact cost unknown. Ten nested spans, stored-answer completion and resolved citation membership were read back. These facts do not establish semantic correctness, reading or normal-user benefit; see [release evidence](outcome-platform-release.md#model-stream-and-second-product-evidence-2026-11-03).

The mapping follows the installed AI SDK 6 normalized usage contract (including its detail fields and deprecated aliases). Local integration evidence and deployment are separate; neither a synthetic framework stream nor a unit test proves a real customer flow. References: [AI SDK streaming usage and callbacks](https://ai-sdk.dev/docs/reference/ai-sdk-core/stream-text), and the installed framework's `LanguageModelUsage` declarations and normalization. The current online documentation describes version 7; verify a runtime upgrade independently.

## Optional LangChain callbacks (Python)

The optional `afterspan_langchain.AfterspanCallbackHandler` maps real LangChain callbacks to the existing native chain/model/tool/retrieval steps. Use one handler per task and close it before its parent span exits. Separate callback contexts preserve nesting across threads and overlapping requests. Content, run names and error text are omitted; unfinished callbacks become unknown. The handler has a 1,024-run bound and explicit diagnostics. It never creates quality checks, completion, delivery or usefulness milestones.

Exact model identities require an allowlist. Normalized final token usage additionally requires an explicit opt-in after checking that the installed provider adapter preserves missing versus reported values. Reasoning-only callbacks do not start first-output timing. Missing counts and exact cost stay unknown. See the [complete recipe, install and limits](../examples/langchain/README.md). LangChain Core 1.7.4 has local real-framework callback and fresh-wheel/service-ingestion checks, plus one bounded production-service readback of two successful synthetic pipelines and one deliberate failure. This used fake models and a local tool, not a model provider. Broader versions, native provider execution and product adoption are separate evidence.

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

A later synthetic validation called native `https://api.openai.com/v1/responses` once with streaming and `store:false`. The current standalone observer ran beside Tracker's existing workflow-lifecycle helper; neither its installed dependency nor normal request path changed. Production API and Traces UI readback retained two closed spans, 11 input/2 output tokens, explicit cache/reasoning zeros, provider-reported usage and first output at 1,874.3 ms within a 1,909.8 ms model span. Cost stayed unknown. There was no quality check or completion/delivery/usefulness milestone, so business outcome remained unverified. Eight offline harness cases covered fragmented CRLF frames, HTTP rejection, truncation, reasoning-only output, output overflow and contradictory terminal sequences before the single live call. This proves one native Responses observation, not native Anthropic, an OpenAI framework wrapper, a latency estimate or user benefit. See [release evidence](outcome-platform-release.md#responses-and-messages-sdk-evidence-2026-11-02).

When real provider access is unavailable, use the [official Anthropic SDK dummy-key recipe](../examples/anthropic-sdk-fixture/README.md). It runs the pinned Python SDK 2.4.0 against a numeric loopback server, with explicit dummy credentials, bounded synthetic content and no retries. It tests typed events, fragmented CRLF/UTF-8, cumulative counts, absent cache details, stream errors, missing completion, authentication failure and cancellation. The observer remains metadata-only; the caller owns lifecycle/error checks. Fixture traffic and model identity are explicitly labelled, with no quality or business-success inference. A fresh installed consumer sent 24 observations to the isolated production verification project; account-scoped readback preserved all six two-span traces, including four deliberate failures and missing-usage/timing unknowns. This proves SDK-format interoperability and production trace storage, not a native Anthropic call or a real key’s validity.

Provider contracts: [OpenAI Responses usage types](https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_usage.py), [Responses event types](https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_stream_event.py), [Anthropic cumulative streaming usage](https://platform.claude.com/docs/en/build-with-claude/streaming), and [cached-input accounting](https://platform.claude.com/docs/en/build-with-claude/prompt-caching). These are the documented mapping sources, not a claim of all-provider support.

## Bounded installed-helper production adoption

Tracker imports `ModelObservation` from the pinned `@afterspan/sdk/model` package in production, replacing its identical copied helper while keeping one existing request trace stream. Runtime import and one actual source-checked Slack reply verified model usage, cost and two streamed first-output timings. This does not exercise the full native workflow tracer, other providers/frameworks or a second product; see [release evidence](outcome-platform-release.md).

## Node streaming responses

An HTTP handler can return a streaming `Response` before its model or body has finished. A plain `task` callback closes when the callback returns; use the pinned SDK's `response` helper to keep the request trace open through actual consumption. The Git pin above includes these helpers; older SDK revisions require an update.

```typescript
const {response, finished} = await tracer.response(
  {workflow:'library_answer', environment:'production'},
  async () => {
    const chunks = tracer.stream('model', async function* (span) {
      span.modelUsage({provider:'example', model:requestedModel});
      for await (const text of createProviderStream()) {
        if (text) span.firstOutput();
        yield new TextEncoder().encode(text);
      }
    });
    const iterator = chunks[Symbol.asyncIterator]();
    return new Response(new ReadableStream({
      async pull(controller) {
        const next = await iterator.next();
        if (next.done) controller.close();
        else controller.enqueue(next.value);
      },
      async cancel() { await iterator.return?.(); }
    }, {highWaterMark:0}));
  }
);
after(async () => { await finished; await tracer.flush(); });
return response;
```

`after` is the host's background-completion hook, not an SDK export. Register it while the request lifecycle is active; a host that requires registration before returning the handler response should register inside that handler and retain the returned `finished` promise. The provider must abort its in-flight request when the application or consumer cancels; returning an iterator may wait for a pending provider read. The SDK never retries that request.

`response` returns the body without reading it ahead. Its root processing span stays open while the consumer reads; a child response span times handler return through body EOF, failure or cancellation. Status, status text, headers and bytes are preserved. A bodyless response completes at handler return. HTTP status 400 or higher, or `Response.error()`, records a failed transport response; it does not rewrite the response. The returned `finished` promise resolves to `completed`, `failed` or `cancelled`. Handler exceptions still reject `response` with the original error; body/cleanup exceptions still reach the original stream consumer. A consumer that never reads or cancels leaves the trace unfinished. The application owns deadlines and host lifecycle registration.

`stream(step, factory, options)` captures the current parent context at invocation, but starts the span and calls the factory only on first iteration. It restores that context across asynchronous reads and nested calls, forwards items unchanged and calls the source iterator's return on early exit. Normal EOF succeeds; an early return cancels; source or cleanup errors fail. Cancellation uses the existing wire state `failed` with `outcome_code:cancelled`, while `finished` distinguishes cancellation from other failures. No yielded value, response header, body, cancellation reason or error text is retained. Existing manual model usage/check methods work on its span.

These are local observation boundaries, not proof of remote HTTP receipt or human reading. Neither helper automatically declares a completion/delivery/usefulness milestone, a passed check or a satisfied goal. Active trace emission is nonblocking through the existing bounded collector; final background flush is best effort. Collector failure never reruns the handler, stream factory or business operation. Node's queue is still in memory. Account-bound workflows must use the optional subject binding below; do not decorate native events with raw user identifiers or extra attributes.

The SDK passes 60 Node checks, including 15 response/stream boundary cases. Fresh installed-package consumers emit completed and cancelled stream lifecycles accepted by the local service. Fresh Node Git installation at the pin above executes a consumed response successfully. These are synthetic local checks, not a deployed customer-product adoption or proof of native provider calls.

## Account-linked traces and erasure

Python accepts `WorkflowTracer(collector, subject_id=opaque_id)`; Node accepts `new WorkflowTracer(client, {subjectId: opaqueId})`. This attaches the same top-level `distinct_id` to every native trace and evidence event. Omit it for anonymous workflows. The required format is a lowercase namespace of 1–32 letters/digits/underscores starting with a letter, an underscore, and a 64-character lowercase hexadecimal digest. The format check cannot prove how an application derived that digest. Derive it server-side with a project-specific HMAC key over an account identifier and erasure generation; never use raw identifiers, unkeyed hashes or credentials as identities. Keep that identity key stable across producer-key rotation.

A trace cannot switch between accounts or gain/lose its identity later; conflicting batches return 409 atomically. Start a new trace for a new account or erasure generation. Subjects are absent from diagnostic projections and W3C carriers. At a trusted service handoff, the application must independently bind the same subject; it is not forwarded in baggage.

The existing project-scoped `/v1/erase` removes that subject's stored events and their selected debug snapshots. Private trace tombstones block later replay of the erased trace even if a producer drops or changes its subject. They clear the subject link and remain, like existing erased-identity records; active bindings age out after event retention plus a five-minute clock margin. This does not erase independent application databases, screenshots, previously exported reports or old backups. Before promoting a pre-erasure backup, reconcile subsequent erasures. Historical anonymous traces stay anonymous and cannot be retrospectively attached to an account.

## HTTP and queue handoff

Inside a span, call `inject()` and send its W3C `traceparent` and bounded `baggage` as headers or queue metadata. At a trusted internal boundary, Python `extract(headers, trusted=True)` or Node `extract(headers, true)` validates that carrier, then pass the returned context to `task`. Public headers are untrusted by default. Do not forward arbitrary baggage or credentials. Invalid carriers return no context; the application chooses whether to start a separate task or reject its own request.

Keep the same context through a continuation. For an actual business retry use Python `context.retry()` or Node `retryContext(context)`: it retains the original request clock and trace ID, creates a new attempt ID and increments the attempt number. Never use it for delivery retries. Unsampled (`00`) context propagates but exports no observations; status counts those omissions.

Both helpers use full W3C IDs and one deterministic SHA-256 mapping into the existing UUID workflow store. The namespace is `afterspan-otel-v1`, followed by a NUL and full trace ID; span mapping adds another NUL and the full span ID. The first 16 digest bytes are normalized to UUIDv4 bits. This prevents lossy span-ID padding and keeps Python/Node identities identical. The mapping alone is not deployment evidence; the separate official-exporter production check is recorded in [release evidence](outcome-platform-release.md).

## Evidence and limits

Local tests ingest the exact Python and Node payloads through the real service, join a Python parent to a Node child, and resolve a check receipt. Concurrent contexts, sampling, retry clocks, duplicate check names, closed spans, invalid IDs and telemetry failures are tested. These are synthetic local runs, not production adoption. There is no automatic prompt/output capture or transparent provider monkey-patching; parsed-chunk observation requires the explicit model-span calls above. Optional selected content uses the separate [governed debugging flow](debugging.md), never the normal collector. Model usage and OTLP mappings are described in [OTLP intake](otlp.md).

Standards: [W3C Trace Context](https://www.w3.org/TR/trace-context/), [OpenTelemetry trace API](https://opentelemetry.io/docs/specs/otel/trace/api/). The complete release gate remains in [platform acceptance](outcome-platform-release.md).
