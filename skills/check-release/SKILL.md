---
name: check-release
description: Separate deployed code, observed collection and actual user outcomes after a release.
---
Confirm project and scope. Obtain exact release proof from an authorized deployment source; this remote MCP does not deploy or inspect cloud providers. Use setup_status, event_catalog and query_metric to check the intended new signals. For compatible request traces, use diagnostic_catalog, review_requests and request_trace in the expected environment and stated release/window; evaluate_answer supports only declared supported goals.

State separately: local checks, reviewed change, merged change, deployed release, observed events, and demonstrated user outcome. Do not convert a green health check into end-to-end success. Compare definitions and denominators across windows, preserve low sample sizes, and never claim causality from before/after alone. Do not generate artificial production completions to satisfy a gate. If release metadata or outcome evidence is missing, keep the result unverified and name one check that would resolve it.
