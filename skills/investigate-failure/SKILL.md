---
name: investigate-failure
description: Investigate one slow or failed workflow using privacy-safe request evidence.
---
Confirm audit:read with connection_status. For workflow traces call workflow_trace_catalog, then workflow_trace_review with an explicit environment, bounded time window and limit. Pass a returned trace ref and its exact query to workflow_trace_detail. For the separate Slack comparison contract use diagnostic_catalog, review_requests and request_trace. Never fabricate a ref, join missing IDs, export raw messages or broaden to unrelated projects.

Explain the user's symptom, observed attempts, ordered operations, failures, timing and missing evidence. Parallel timings overlap and must not be summed. A missing terminal observation is incomplete tracking, not proof that work never ran. A successful model/tool call is not a successful business outcome. Availability is not reservation; booking is a producer report; provider acceptance is not delivery; delivery is not human reading. Workflow tracing needs an instrumented producer and does not automatically cover arbitrary applications. A window can omit earlier starts. Conflicts, missing starts and missing parents must stay unknown. Multiple sends cannot be collapsed into one successful reply.

Separate confirmed cause, plausible explanations and unknowns. Preserve sample counts and release/environment coverage. If a supported repo goal exists, evaluate_answer can check its bounded delivery/source criteria. It cannot independently certify arbitrary answers. Suggest one evidence-backed next step; do not silently modify production.

For message bursts, read the batch receipt count and direct linked inputs separately. Three inputs leading to one booking are three receipts and one booking, not three bookings. Retries retain batch membership; changed membership creates a separate batch. Missing links may be outside this window or retention and cannot establish lost messages. Do not recursively join unrelated traces or copy batch outcomes onto inputs.

For a slow or incomplete request, use `search_traces` with environment and a bounded time window, then `open_trace` with the returned reference and exact query. Inspect parent-linked spans, attempts and evidence gaps. A closed timing span does not establish business success; do not add overlapping durations or infer work was skipped from missing evidence.
