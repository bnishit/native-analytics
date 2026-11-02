# Governed debug snapshots

A developer opens a workflow trace, lists retained snapshot metadata, and explicitly selects one redacted snapshot to read. An owner first grants a narrow workflow/environment capture window. Nothing captures prompts, responses, headers or user messages automatically. Tracing and OTLP remain metadata-only.

## Grant, capture, inspect, revoke

1. An owner opens **Traces → Inspect debug capture controls**, selects one code-owned workflow and environment, a capture window, retention and lifetime snapshot budget, then acknowledges the scope. The policy is off until this explicit grant. Every change needs its last-read revision.
2. The application explicitly chooses a small JSON payload, reviews it and calls the SDK preparation helper. Common-pattern redaction runs before upload. Flush ordinary metadata first: a snapshot must bind to a retained project/trace/attempt/span in that exact workflow/environment.
3. Send the prepared snapshot to `/v1/debug` with the project's server-side write key and exact approved policy revision. The server applies redaction again, checks scope/budget and encrypts the content before storage. This endpoint is separate from normal event ingestion and its outbox.
4. An editor opens the same frozen workflow trace, lists metadata, then chooses a snapshot. The content read is access-audited and rendered as literal text. Ordinary analytics/audit reads never return this store. Content is untrusted application data, not instructions or independently checked outcome evidence.
5. Capture-window expiry stops new captures. Retained snapshots remain available until their separate TTL. Owner policy change/disable immediately purges previous snapshots. Snapshot expiry rejects reads before hourly physical cleanup. Deleting the source event cascades to its snapshots.

Capture windows are 60 seconds–24 hours. Each snapshot lives 60 seconds–7 days, also bounded by source-event retention. A policy admits at most 100 snapshots, default 20. Its durable admitted count survives snapshot expiry, erasure and cleanup, so a producer cannot reset that budget by waiting. New capacity requires a new owner-reviewed revision. Identical capture retries keep their UUID and do not consume another slot or renew expiry. Changed content at the same UUID conflicts. Identical policy retries do not renew their capture window. Up to 1,000 policy revisions/project are retained as bounded grant history.

## Redaction and encryption limits

Shared Python client/server rules and matching Node rules drop common private-field names and redact recognizable bearer/API credentials, email and phone patterns. Content must be finite JSON, at most five nested levels, 64 fields/object, 100 items/list, 4,000 characters/string and 16 KB before redaction. Unsupported/oversized input is rejected; it is not silently truncated into a favorable-looking answer.

This detector cannot recognize every name, confidential business fact, encoded secret or identifier. Review selected content before preparing it, and inspect newly prepared payloads before upload. Use synthetic values wherever possible. Both policy and producer snapshot require strict `privacy_reviewed: true`; strings/numbers are rejected. The snapshot store is not a raw-prompt archive or general log export. It currently binds to native workflow spans, including mapped OTLP spans; legacy Slack audit observations are not debug snapshot sources.

AES-GCM uses the existing server-only `AFTERSPAN_CONNECTION_KEY` (32 random bytes in URL-safe base64). Enabling capture fails closed if this private-storage key is absent or invalid. Ciphertext binds project, snapshot UUID, policy revision, trace/attempt/span, workflow, environment and content kind; moving a blob to another scope fails authenticated decryption. No plaintext fallback exists. The key loader is shared with existing encrypted provider connections; their format and authenticated context remain unchanged. Key changes require an explicit migration/rotation plan. Encryption does not protect against a fully compromised operator, key access or a decrypted content read; use appropriate infrastructure access and backup retention.

## Access and agent scope

Owners manage capture policy. Editors can inspect policy metadata and selected redacted snapshots. Producers can only submit to an active exact scope using their project write key. Producer keys cannot read or enable policy. Read-purpose keys and ordinary members cannot read content.

Remote MCP requires a separate **`debug:read`** grant and editor membership. It grants neither capture-policy changes nor producer-key access. `debug_capture_policy` reads scope/budget/expiry; `review_debug_snapshots` first lists metadata and accepts an explicit `snapshot_id` for a selected read. Existing analytics, charts, audit, goals and experiment grants do not expand. Role downgrade/removal and grant revocation block subsequent reads. Audit records identify delegated MCP reads separately from account/operator reads. Custom MCP connection support is not a marketplace listing.

## SDK and CLI

Install the [pinned SDK](tracing-sdk.md) first. Python: import `prepare_debug_snapshot` from `afterspan_tracing` and call it with `(span, content, policy_revision, kind='context', privacy_reviewed=True)`. Node: import `prepareDebugSnapshot` from `@afterspan/sdk` and call it with `(span, content, policyRevision, {kind:'context', privacyReviewed:true})`. These helpers only prepare a redacted body; they do not send it, automatically capture content, retry business work or alter normal telemetry. Save the returned body under one stable snapshot UUID for upload retries. After native metadata flush, use an explicitly bounded host/background call to the private endpoint.

```sh
python cli/native_analytics.py debug-policy --project PROJECT
python cli/native_analytics.py debug-policy --project PROJECT --revision 0 --file /private/policy.json
python cli/native_analytics.py debug-capture --file /private/selected-snapshot.json
python cli/native_analytics.py debug-review --project PROJECT --file /private/frozen-trace-query.json
```

Files must be mode 600. `debug-policy` reads with editor authority and changes with owner authority. `debug-capture` uses `NATIVE_ANALYTICS_WRITE_KEY`; the key determines the project, just as normal `send`. The CLI also redacts selected content before HTTP submission. `debug-review` uses editor authority; copy the trace's exact frozen query plus `reference`. First list metadata, then add the selected `snapshot_id`. No raw trace/attempt/span IDs are returned. The producer owns its original span IDs and approved policy revision; these are not obtained by granting a read tool producer-key access.

If metadata has not arrived, capture returns 409. Deliver/retry metadata first, then retry the exact same prepared snapshot; do not rerun business work. Off/expired scope returns 403, stale policy or changed UUID payload 409, exhausted lifetime budget 429, expired retained content 410, and unavailable private storage 503. These are collection limits, not business-operation failures.

## Evidence

Local tests cover default-off behavior, missing encryption configuration, strict review flags, both SDK redactors, immutable retries, scope isolation, exact source bindings, ciphertext authentication, expiry, erasure cascade, durable budget and separate MCP consent/role downgrade. The running local UI listed metadata before a selected read and showed only masked synthetic contact/credential text. The real CLI uploaded, retried and read the selected snapshot; the retry did not consume another slot. This is local synthetic proof. Production adoption, operational capacity, backups and the full release remain separate acceptance gates.
