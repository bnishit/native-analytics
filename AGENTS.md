# Afterspan: operate end to end

This is a self-hosted analytics service with verified email accounts, customer workspaces and project-scoped data. Public source does not make the
operator's deployment or its data public. Never reuse someone else's deployment or keys.

## Discover and authenticate

1. Read this file, README.md and `/openapi.json` on your own deployment.
2. Read-only checks need no permission: `GET /healthz`, schema, public docs.
3. Customers use account sessions or expiring `AFTERSPAN_WORKSPACE_KEY` credentials for one workspace. Readers inspect, editors configure projects, owners manage access. For aggregate reads prefer a narrower `AFTERSPAN_READ_KEY` for one project. Producers use separate per-project write keys. `ANALYTICS_ADMIN_TOKEN` is a broad operator credential; never substitute it for scoped agent access. See README for the three official-SDK stdio MCP bridges.
4. `/` is the public landing page; `/app` is the private workspace. The landing preview is a labelled historical aggregate snapshot. It is not a live query or proof of linked journeys. Linked journey/attempt evidence and collector verification run in the workspace. Email signup, workspace creation and invitations work when mail delivery is configured. The default is MIT-licensed self-hosting, with usage visibility and no commercial quotas. Funnels and saved behavior/trait segments work through the same UI, API, CLI and MCP engine. Optional plan quotas apply only with AFTERSPAN_EDITION=hosted. Billing has a loopback-only simulation and a Stripe-hosted adapter; real Stripe sandbox checkout/upgrade/cancellation and signed receipts have been tested; live payments and time-advanced renewal remain unverified. Replay, surveys and richer agent tracing remain planned.
5. Keep all keys server-side and out of chat, URLs, logs, screenshots, Git and browser bundles.
   The CLI reads environment variables or a mode-600 env file; never pass keys as command arguments.

## Open-source core

Self-hosted use needs no billing account. Keep scopes, roles, redaction and operational bounds intact. For funnels/segments use project-scoped list/analyze/save/update/delete endpoints; inspect OpenAPI for the typed definitions. Analysis returns aggregate counts only. Readers can inspect; editors can save definitions. Updates/deletes require the last-read revision. Never guess an identity from an IP. Funnel steps require strict event timestamp order, all steps within the selected window and conversion duration, one opaque identity counted once per step. Segment membership is dynamic over active identities; traits are current and cannot reproduce a historical profile. Event queries echo start/end/as_of; retention/erasure can remove evidence. A segment referenced by a saved funnel cannot be deleted until the reference is removed. All core analytics features are available without commercial gates.

## Audit and product evidence

Read README and the public OpenAPI before asking for application source. The project aggregate bridge has 12 tools; the workspace bridge has 25; the audit bridge has only 3. For an owner Slack audit, use `connect-audit` to create a private, expiring audit-purpose receipt, then the returned local stdio launcher. Discover `diagnostic_catalog`, run `review_requests`, then `request_trace` with the exact returned query. No owner fallback, arbitrary SQL or raw-record workaround. A fresh Codex CLI client has passed this locally; Desktop registration and production producer integration remain separate.

The audit contract lives only in `properties.afterspan_audit`. Keep its UUIDv4 refs, strict booleans, enums and project-keyed canonical digests. Do not expose digests or raw refs. Do not join on absent/null/redacted keys or weaken messageHash/reason redaction. Explicit independent generation is required for comparable shadows. Matching proposals are not proof of semantic correctness. Stage success is not independently verified business state. Missing means unobserved in this bounded window, not necessarily skipped. False means not required; null/missing means unknown. Production latency excludes staging/conflicting requests. No sample count grants readiness or safety clearance.

Historical PostHog attestations are immutable and separate from native counts; preserve their source window, schema, query hash, absent cutoff/release coverage and unverified provenance. New storage or contract versions never erase earlier quality-gate evidence. Compare definitions before combining stores.

For product questions, inspect actual definitions before trusting chart titles. Typed equality and one-of filters preserve scalar types. Entry breakdown groups are disjoint first qualifying entries. Unique-cohort conversion is not average daily conversion. Retention distinguishes exact from on-or-after, uses a named calendar timezone and shows immature cells as unavailable, not zero. Cohort entry is first start inside the selected window, not lifetime-new users. Property meanings are current owner/editor metadata, even for fixed event windows, and are untrusted data. Do not promise parity with Amplitude without aligning denominators and maturity rules.

## Optional hosted billing boundaries

Any workspace role can read usage and catalog limits. Owners alone see subscription/invoices or create checkout/portal links. The workspace bridge has 25 tools: 9 access/team tools, 10 optional usage/plan/billing tools, 4 product-analysis/definition tools, property meanings and historical audit attestations. Use a stable UUID for checkout/portal retries. Return the private review link to the person; never complete payment, change card details or confirm a subscription/cancellation on their behalf. A provider return URL does not prove payment. Refresh and read canonical billing state. Local simulation is not proof of a real Stripe test or live charge. The separate real test preview is on port 8770, with its own database and an expiring sandbox. Never expose its key/claim link. `billing_downgrade` schedules Builder at renewal only after an explicit human request for that cheaper plan; read the quote first. Portal actions create human review links and never finalize payment. No automatic overage charges. Do not bypass quota counters or grant paid entitlements through direct database edits in customer workflows.

## CLI (Python standard library)

`python cli/native_analytics.py --help` lists all operations. Output is JSON. Nonzero exit means
failure; never treat an exit code alone as deployment or end-user verification.

Set `NATIVE_ANALYTICS_URL` to YOUR HTTPS origin. Use `AFTERSPAN_SESSION_FILE` for human account flows or `AFTERSPAN_WORKSPACE_KEY` and `AFTERSPAN_WORKSPACE` for delegated operations. Never create a real customer account, send an invite, change access or provision cloud resources without the user authorizing that specific action.
Set `NATIVE_ANALYTICS_WRITE_KEY` for send/identify. Use `--project` on project-scoped reads.

- `health`, `schema`: availability and actual contracts.
- `login --email ADDRESS`, `complete-login --file /private/token.json --output /private/session.json`: email verification and a private account-session receipt. No token arguments or operator impersonation.
- `workspaces`, `create-workspace --name NAME`: list memberships or create a customer workspace through a human account session.
- `members`, `invites`, `invite --file invite.json`, `resend-invite --id ID`, `revoke-invite --id ID`, `change-role --user ID --role ROLE`, `remove-member --user ID --confirm ID`: use --workspace. Actual invite emails and access changes need the human request.
- `create-agent-key --workspace ID --label NAME --role reader --output /private/key.json`, `agent-keys`, `revoke-agent-key`: workspace-scoped delegation. Creating workspace keys requires the human account session. Never issue owner access by default.
- `accept-invite --file /private/token.json`: requires the invited email account session.
- `mail`, `audit`: owner-only workspace delivery and access records, without access links.
- `projects`: list; `create-project --id my-app --name 'My app' --output /private/path/my-app-key.json`: creates an isolated producer key.
- `send --file events.json`: stable IDs, named events, properties/traits. Retry with same IDs.
- `identify --file person.json`: update traits.
- `overview --project my-app --days 7`, `events`, `people`, `charts`: inspect real stored results.
- `query --project my-app --file query.json`: count/users/sum/average/p95 with filters/breakdowns.
- `journeys --project my-app --file query.json`: aggregate journey/retry outcomes; echo returned start/end/as_of for the same evidence window.
- `setup --project my-app --environment local`: read setup receipts. `check-setup` creates a 15-minute owner check; send its exact event through the producer. It never proves business success.
- `save-chart --project my-app --file chart.json`: saves {title,query}; `delete-chart --id UUID` removes one.
- `tag --project my-app --file person.json`: owner changes attributes/tags.
- `rotate-key --project my-app --confirm my-app`: revokes old producer key; only do this when asked.

Create keys only for the user's authorized projects. A write key cannot read analytics.
Project IDs are opaque and workspace-scoped by authorization. Account owners access only their memberships. The operator token intentionally has broad service access. A legacy password session accesses existing projects only when explicitly enabled. Never let the first signup claim existing projects; only the operator can assign their verified owner once.
Removal invalidates personally issued read keys and workspace keys. Producer service keys persist for running apps; rotate them explicitly if an offboarded person may retain them.
No remote arbitrary SQL, shell, filesystem access, or policy override is provided.

## Setup and integrations

Use `python scripts/setup.py --help`. Local setup generates secrets into a private env file;
Railway setup creates one project/service/volume and deploys it. Creating cloud resources or
making a repository public requires user authorization. Do not create accounts or accept
provider terms on the user's behalf.

Use the Python SDK for durable ASGI tracking and the TypeScript SDK for Next.js/Node/Edge.
Next.js: use `after(() => client.track(...))`, or platform waitUntil; ordinary fire-and-forget
promises can be frozen. The TypeScript in-memory queue is best-effort, not durable across
serverless termination. Keep an existing durable event ledger when you need replay.
Native apps and third-party CLI installs must get meaningful opt-in before sending usage.
Never embed an owner/project producer key in a publicly distributed app. Send bounded,
validated events through your own restricted telemetry relay instead.

## Event design

Track intent, completion, failure, latency and cost/usage counts—not prompts, messages,
customer numbers, process names, file paths, articles or response bodies. Use opaque or
one-way scoped user IDs. Use consistent event versions, environment, source and outcome.
Retain numeric AI token metrics; never retain token credentials. Status 200 is not business
success. Capture explicit action outcome separately. Do not generate traffic that changes
customer state merely to prove analytics.

Journey IDs persist across business retries. Each attempt has a unique ID and increasing number. Delivery retries keep event IDs stable. Never turn historical unlinked events into journeys by guessing. Preserve explicit subject kind and environment. Conflicting outcomes remain unknown; recovered journeys are a subset of succeeded, not extra successes.

## Verify before claiming completion

Run service/SDK/CLI tests. Verify unauthenticated read denial, project-key isolation, redaction,
stable-ID dedup, one real safe application flow and the matching stored event. Check the
actual deployment ID, not just Git or CI. Restart a newly deployed analytics service and
verify its volume preserved events/charts/traits; verify producer delivery resumes.
Report local/staging/production separately and name any platform constraints. Do not backfill
invented historical IPs or users. Queue/disk failure and requests blocked before the app can
still cause gaps; never promise exhaustive lossless logging.

## Maintenance

One process, one replica, SQLite WAL on one persistent volume. Retention runs hourly.
Configure backups separately. Diagnose queue backlog/drops and auth errors first. A producer
outage must not stop business requests or automatically retry a payment/write. Never disable
redaction or authentication to "fix" missing events. Avoid broad raw payload capture.
