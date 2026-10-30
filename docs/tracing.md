# Traces as a primary product surface

## Product contract

An operator opens **Traces**, selects the environment, finds a request, and sees its attempts and nested steps on one timeline. Missing starts, finishes, parents and decision evidence stay visible. The user can share the exact observation window or ask an agent to inspect the same request. No new permission is needed for existing project audit readers.

This is a general Afterspan capability. Experiment Tracker is the first live adopter, not the domain model. A trace groups one request; a span is one timed step; an attempt is a business retry, not a telemetry resend. Delivery, processing and independently checked outcomes are separate facts.

## Research and choices — 30 October 2026

| Primary source | Useful pattern | Afterspan choice |
| --- | --- | --- |
| [Datadog Trace Explorer](https://docs.datadoghq.com/tracing/trace_explorer/) | Search retained spans with explicit collection scope | Search safe references, operation categories and release IDs; show the retained frozen window |
| [Datadog Trace View](https://docs.datadoghq.com/tracing/trace_explorer/trace_view/) | Nested timing views and a permalink | Parent-labelled waterfall, attempts, readable timings, exact-window request link |
| [Langfuse data model](https://langfuse.com/docs/observability/data-model) | A request groups nested observations; sessions are another level | Keep request, attempt and span distinct; do not turn unrelated analytics events into traces |
| [Grafana Tempo TraceQL](https://grafana.com/docs/tempo/latest/traceql/) | Search respects trace structure; UI and API share a model | One bounded search/open engine behind the UI and both MCP transports |
| [OpenTelemetry traces](https://opentelemetry.io/docs/concepts/signals/traces/) | Parent context connects operations across services | Preserve explicit parent IDs and missing links; do not invent a cross-service relationship |

These are product patterns, not a claim of vendor feature parity. Afterspan does not ingest OTLP, auto-instrument every service, collect prompts, or reconstruct missing history.

## Two designs considered

1. Turn Request audit into the tracing page. Smallest UI diff, but ties every product to Slack comparison rules and a latest-60 selection. It cannot give reliable search or general workflow browsing.
2. A shared trace explorer over explicit contract adapters. Chosen: reuse existing validation, grouping, scoping and evidence logic; normalize safe display fields; filter before pagination. Keep Request audit for its original decision-comparison purpose.

No new event store, credential type or duplicate Tracker stream. Existing `slack-request-v1` and `workflow-trace-v1` remain distinct and never merge based on guesses. Frozen `start`, `end`, `as_of` apply to list, pages, details and links. Retention/erasure can still remove old evidence.

## Generic producer integration

Send `trace.observed` through `/v1/events` using the project's server-side write key. Its only property is `afterspan_trace`; no identity or extra properties are accepted. The catalog at `/api/traces/catalog` exposes the full validated schema.

```json
{
  "schema_version": "workflow-trace-v1",
  "trace_id": "a3c34615-24cc-4e41-afab-de9194446c6c",
  "attempt_id": "50e18d0e-5519-41fe-bdb2-e0179a18950e",
  "attempt_number": 1,
  "span_id": "f05ce374-8c83-48c8-96dc-e1e250e8eb71",
  "workflow": "document_export",
  "environment": "production",
  "step": "storage",
  "state": "started"
}
```

Generate fresh UUIDv4 IDs in the producer; the example is synthetic. Close the same span with a new event ID, terminal state and measured `duration_ms`. Retries of that telemetry event retain its event ID. Nested spans include `parent_span_id`; business retries keep `trace_id` and create a new attempt. Choose a fixed code-owned workflow name, never user content. Generic categories cover receipt, queue, processing, model, tool, retrieval, storage, HTTP and response. Existing domain categories remain compatible but are not mandatory for other products.

A missing terminal event remains incomplete. A missing start does not become a zero-duration success. Generic elapsed time begins at the first observed event, explicitly labelled; it is not a guaranteed original request clock. The Slack adapter has an explicit request clock. Nested durations overlap and cannot be summed. Best-effort telemetry can be lost; use a durable producer outbox when replay is required.

## Agent and API flow

- Remote OAuth and local audit MCP: `search_traces(query)` then `open_trace(reference, query)` with the exact returned query.
- HTTP: `POST /api/traces/search?project=…`, then `/api/traces/open` with `reference`.
- Search fields: `environment`, `contract`, `search`, `status`, `min_duration_ms`, `limit`, `offset`, and the standard time window.
- Search scans retained, bounded contract evidence before pagination. Existing hard scan limits reject an oversized window instead of silently returning partial results. Reduce the window when rejected.
- `observed` means observations, not verified business success. `failed` is a reported failed step; `gaps` indicates incomplete evidence without a recorded failed span. Failed requests may also have gaps (`has_gaps`).
- Raw refs, raw messages, feature IDs, user content, keys and arbitrary property queries are not exposed. Existing project isolation and `audit:read` guards apply.

## Experiment Tracker adoption

Existing Slack request spans appear immediately through the adapter; no re-ingestion or parallel generic stream. Parser and model span categories extend the existing timing contract. Initial decision-stage reports and resumed attempts stay distinct: a later attempt missing a parser/planner audit report does not mean the parser never ran. Timing completion cannot fill that evidence gap.

## Acceptance and limits

Check safe search before paging, frozen readback, mixed environments, cross-project denial, redaction, failed/missing starts, unknown terminals and overlapping parent/child timing. Verify the real UI and both MCP tools against retained production data. Synthetic tests are not live traffic.

Future independent work: OTLP/W3C propagation with an explicit privacy mapping, richer model usage/cost semantics, sampling/retention controls, trace-derived alerts and saved queries. These require their own contracts and scale evidence; they are not implied by this release. No heyAnaya integration is part of this work.
