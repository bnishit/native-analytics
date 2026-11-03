# Collection health

An owner opens **Collection health**, selects the environment, and sees which declared sources reported, which evidence is missing, and whether queues or checks need attention. The environment and frozen start/end/as_of query stay in the shared page link. Release comparisons link directly to each exact collection window, including its workflow. Refresh or a changed time range/environment starts a new read; project changes discard the prior project’s query. Opening another saved link in the same tab reloads the workspace so its route and report cannot retain different windows; ordinary in-page filters still update in place. Malformed saved queries show an error rather than silently reading another window. The saved window does not preserve records removed by retention/erasure or turn current source declarations into an immutable past inventory. An absent report never means zero loss. Release comparisons also inspect both full follow-up windows and stay unknown when this bounded collection evidence is incomplete.

## Declare expectations before observing a window

An editor declares one opaque source handle per environment, optional workflow handle, expected pulse interval (10–3,600 seconds), and whether the source is expected to report. The screen or `PUT /api/collection/sources?project=PROJECT&revision=0` creates it. Read `GET /api/collection/sources` before changing it, then send the current revision; identical retries return the same version and changed stale writes return 409. A project has at most 100 declarations.

This is current editor configuration, not a past deployment inventory. Changing a declaration after the window starts makes historical coverage uncertain. Disabling an expectation is visible and does not make missing evidence pass. A source handle is operational metadata; use an opaque stable service name, never a customer name or private identifier.

## Producer pulse

`analytics.pipeline` with only `properties.afterspan_health` uses the strict `collection-health-v1` schema in OpenAPI. It carries source/environment/workflow, opaque process UUID, sequence, original process and observation clocks, queue mode/capacity/count/oldest clock, last delivery clock, cumulative loss/rejection/delivery failure counters, optional evaluator failure count, and a fixed error code. It accepts no identity, traits, journey, raw error, body, prompt, key or extra property. Event time must match the pulse clock. Stable event IDs retain normal ingestion deduplication.

Python:

```python
collector = Collector(origin, write_key, private_outbox_path,
                      source='export_worker', environment='production',
                      workflow='document_export', interval_seconds=60)
collector.start()  # existing durable worker sends periodic pulses
# Only after a real evaluator invocation:
collector.record_evaluation(failed=False)
```

The Python queue and cumulative counters persist in SQLite across process restarts; each process receives a new opaque instance ID. Local enqueue errors and rejected payloads, queue overflow, delivery failures and evaluator failures have distinct counters. A disk failure can prevent even the failure counter from persisting; no lossless claim follows. Evaluator health defaults to unknown until explicitly instrumented. A failed answer check is not necessarily an evaluator execution failure: report `failed=True` when the evaluator itself failed, and keep answer quality in its workflow check.

Node:

```typescript
const client = createAnalytics({url: origin, writeKey,
  health: {source:'export_worker', environment:'production',
           workflow:'document_export', intervalSeconds:60}});
// Only after a real evaluator invocation:
client.recordEvaluation(false);
await client.pulse();
await client.flush();
```

The Node queue is bounded **memory**, not durable. `pulse()` uses the actual client counters. The application owns scheduling: call it from a long-running host heartbeat and register completion with the host's after/waitUntil facility when applicable. The SDK creates no timer that keeps a serverless invocation alive. Declaring an interval does not schedule it. A short-lived process can disappear before sending a final pulse; its loss remains unknown. Do not claim durable Node delivery from these APIs.

### Report a drained worker stop

During a controlled handover, stop accepting work and stop application heartbeat timers first. Python callers then `close()` the outbox worker, drain with bounded `flush()` attempts, and call `pulse(state='stopped')` followed by a final flush. Node callers drain with `await flush()`, then `await pulse('stopped')`. The application owns stopping ingress and concurrent producers; this pulse is a report, not a shutdown hook. Both helpers refuse the report while their queue or current error is nonempty; Python also refuses it while its background worker is alive. Normal pulses report `running`; historical absent/null state remains unspecified. The stop itself can still fail delivery, so read back its exact receipt rather than assuming it arrived. Never loop forever waiting to flush or block a business request on this diagnostic.

An accepted `collector_state: stopped` report requires an empty queue and no current error. It ends only that instance's freshness requirement. It does not prove process exit, task completeness, zero historical loss or a successful evaluator. Later or duplicate terminal reports with different event IDs produce `collector_lifecycle_conflict` in the bounded evidence. An identical event retry remains deduplicated. Missing stop evidence stays stale; the service never infers retirement from a new worker. Source-wide pulse coverage must still continue through the selected window, so retiring the only worker does not make a silent source healthy. Earlier unknown evaluator health and observed loss stay visible. Inspection uses the same bounded pulse history as other health checks; it is not a permanent lifecycle ledger.

## Reading and semantics

`POST /api/collection/health?project=PROJECT` accepts environment, optional workflow, and the standard bounded start/end/as_of window. API/UI/CLI/MCP share one engine. Reads require audit-purpose project access or current workspace reader membership. Remote `collection_health(query)` and `collection_sources()` require audit:read. `save_collection_source(definition, revision)` requires new, explicit **collection:write** plus current editor membership; older grants remain unchanged. The consent page names this permission. Downgrades remove writes on the next request; revocation removes reads too.

CLI: `collection-health --project PROJECT --file PRIVATE_QUERY.json`, `collection-sources --project PROJECT`, and `collection-save --project PROJECT --revision N --file PRIVATE_SOURCE.json`. Narrow audit keys work for reads; they cannot edit expectations.

`observed` means the declared reports passed these bounded checks. It never means a complete ledger. Three times the expected interval bounds pulse gaps, end-of-window staleness and old queued data. Missing/undeclared/disabled sources, late expectation changes, sequence conflicts, regressed counters, reported window loss and missing evaluator health remain explicit. Full-window counter changes require a baseline at/before entry and a later report; otherwise changes are unknown. Stable nonzero lifetime counts alone do not imply new loss in a later window or permanently block recovery. Nonzero totals without a baseline cannot establish when the loss occurred. Old instances without an explicit stop remain stale; retirement is never inferred from a replacement worker. The screen distinguishes a report before the selected window from a report within it. It also distinguishes a known latest evaluator total from an unknown full-window change. A successful new evaluation does not retroactively fill earlier missing evidence or clear an old worker’s unknown shutdown. At most 20,000 pulses, 100 observed sources and 200 process instances are examined; larger requests fail with a narrower-window instruction, never partial clean results. Source-wide expectations can cover a workflow; they do not prove per-task instrumentation or all workers are declared. Sampling and requests before instrumentation can still be absent.

Backend counters persist independently for authenticated native/OTLP ingestion, including rejected HTTP requests and OTLP partial-success rejected spans. They are **project-wide across environments and workflows**, not attributed failures of the selected workflow. Only closed minute buckets overlapping the requested window are returned; partial current buckets are unavailable. They count transport requests/spans, not lost business tasks. Oversize, malformed or unauthenticated requests that cannot be safely assigned to a project, and failures to persist health counters, may be absent. Counters expire under normal retention. Unassigned project-wide rejections conservatively prevent a clean release comparison; use the producer source evidence to investigate scope.

Legacy untyped pipeline events remain accepted and explicitly unknown. Their arbitrary property content is not returned by this health API. The aggregate overview keeps safe numeric counters, and typed pulses use their observation clock for freshness so an old pulse arriving late cannot look fresh merely from its ingestion time. Typed source metadata is excluded from generic aggregate queries. No raw payload export, evaluator scheduler, notification delivery or automatic repair is added here. The health screen and scoped tools expose actionable evidence; a separately authorized monitor can consume them.

## Local proof

Tests cover explicit drained stops with a continuing replacement, silent old workers, activity after stop, refused queued/error stops, unknown evaluator health, and a fully retired source that still fails coverage. They also cover strict pulses, old/missing sources, queue/loss/evaluator uncertainty, stale clocks, receipt cutoffs, sequence/counter contradictions, source revisions, project/environment/workflow isolation, explicit consent and role downgrade/revocation. Actual Python and Node pulses roundtrip through the backend. The official MCP transport reads health and saves an expectation under the new scope. A running synthetic browser and real CLI show a full delayed queue, three reported drops, one rejection and unknown evaluator health. The release comparison rejects that collection state. Those fault checks are local synthetic proof. Separately, the deployed Tracker source reported observed health with no current reason codes over one production 90-second window. A later production-service test used three installed Python collectors, test-only sources and 70.513 seconds of real clocks: an explicit drained stop plus its reporting replacement was observed; a silent collector stayed stale with a coverage gap. Repeated frozen API reads agreed and the actual UI displayed the reported stop. These were synthetic diagnostics with no model or business calls. An earlier attempt ended before observation after a transport failure; its exact queued pulse was subsequently acknowledged without replaying the workload. Both trials’ temporary expectations were disabled after verification. Their prior observed verdict is historical: current expectation changes deliberately prevent it from being replayed as current health. Existing Tracker/Shelf producers and historical worker states were not changed. Complete collection is not established. The [release evidence](outcome-platform-release.md) separates these bounded checks from broader adoption, operating and recovery gates.
