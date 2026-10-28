---
name: investigate-failure
description: Investigate one slow or failed workflow using privacy-safe request evidence.
---
Confirm audit:read with connection_status. Call diagnostic_catalog first, then review_requests with the expected environment, a bounded time window and limit. Pass an actual returned opaque request ref and the exact returned query to request_trace. Never fabricate a ref, join missing IDs, export raw messages or broaden to unrelated projects.

Explain the user's symptom, observed attempts, ordered operations, failures, timing and missing evidence. Parallel timings overlap and must not be summed. A missing terminal observation is incomplete tracking, not proof that work never ran. A successful model/tool call is not a successful business outcome; channel acceptance is not human reading. Current tracing covers the declared Slack contract, not arbitrary applications automatically.

Separate confirmed cause, plausible explanations and unknowns. Preserve sample counts and release/environment coverage. If a supported repo goal exists, evaluate_answer can check its bounded delivery/source criteria. It cannot independently certify arbitrary answers. Suggest one evidence-backed next step; do not silently modify production.
