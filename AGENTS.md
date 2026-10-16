# Native Analytics: operate end to end

This is a self-hosted, multi-project analytics service. Public source does not make the
operator's deployment or its data public. Never reuse someone else's deployment or keys.

## Discover and authenticate

1. Read this file, README.md and `/openapi.json` on your own deployment.
2. Read-only checks need no permission: `GET /healthz`, schema, public docs.
3. Owner operations require `ANALYTICS_ADMIN_TOKEN`. Producers use a separate per-project write key.
4. Keep all keys server-side and out of chat, URLs, logs, screenshots, Git and browser bundles.
   The CLI reads environment variables or a mode-600 env file; never pass keys as command arguments.

## CLI (Python standard library)

`python cli/native_analytics.py --help` lists all operations. Output is JSON. Nonzero exit means
failure; never treat an exit code alone as deployment or end-user verification.

Set `NATIVE_ANALYTICS_URL` to YOUR HTTPS origin and `ANALYTICS_ADMIN_TOKEN` for owner operations.
Set `NATIVE_ANALYTICS_WRITE_KEY` for send/identify. Use `--project` on project-scoped reads.

- `health`, `schema`: availability and actual contracts.
- `projects`: list; `create-project --id my-app --name 'My app' --output /private/path/my-app-key.json`: creates an isolated producer key.
- `send --file events.json`: stable IDs, named events, properties/traits. Retry with same IDs.
- `identify --file person.json`: update traits.
- `overview --project my-app --days 7`, `events`, `people`, `charts`: inspect real stored results.
- `query --project my-app --file query.json`: count/users/sum/average/p95 with filters/breakdowns.
- `save-chart --project my-app --file chart.json`: saves {title,query}; `delete-chart --id UUID` removes one.
- `tag --project my-app --file person.json`: owner changes attributes/tags.
- `rotate-key --project my-app --confirm my-app`: revokes old producer key; only do this when asked.

Create keys only for the user's authorized projects. A write key cannot read analytics.
Project IDs prevent accidental mixing; owner credentials intentionally read all projects.
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
