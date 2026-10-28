---
name: audit-tracking
description: Compare important user actions with observed Afterspan tracking and explain coverage gaps.
---
Start with connection_status and event_catalog. Read event meanings and property types before query_metric. Compare the user's main journeys with observed events. Classify each as observed, instrumented but unverified, missing, or unknown; a catalog alone cannot establish all UI/code coverage. Inspect the actual app or authorized source when needed. Never invent event names or infer people from browser-tab IDs.

Report event meanings, collection start/selected window, test activity, privacy opt-outs and identity limits. A contact click is intent, not a completed conversation. Essay depth is exposure, not comprehension. Model acceptance, channel acceptance, delivery and human reading are distinct. Produce a short coverage table and the smallest useful repair proposal. Do not deploy or change tracking unless the user authorized it.
