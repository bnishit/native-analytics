# Native Analytics

Small, self-hosted analytics for several projects. FastAPI, SQLite on a Railway volume,
plain HTML/CSS/JS and SVG charts. No PostHog, external analytics SDK, CDN, or hosted chart service.

## What ships

- Each project gets its own write-only ingestion key. Owner-only dashboard authentication is separate.
- Named events, stable event IDs for deduplication, optional identities, event properties and merged user attributes.
- Default request traffic, HTTP errors, p95 latency, operation/status/IP breakdowns, tracking health.
- Searchable request/event logs with cursor pagination; user traits, editable tags, saved charts per project.
- Graphs: count, unique identified users, numeric sum/average/p95; hourly/daily, property equality filters and top-20 breakdowns.
- A dependency-free Python collector and ASGI middleware with a persistent local outbox, retries and idempotent ingestion.
- Ninety-day retention by default, automatic hourly cleanup, maximum 30 saved charts per project.

## Deploy on Railway

1. Deploy this Dockerfile as one service with **one replica** and a persistent volume at `/data`.
2. Set three independent random secrets of at least 24 characters: `ANALYTICS_ADMIN_TOKEN`,
   `ANALYTICS_PASSWORD`, `ANALYTICS_SESSION_SECRET`.
3. Set `ANALYTICS_PUBLIC_URL` to the service HTTPS origin, `ANALYTICS_DB=/data/analytics.db`,
   `COOKIE_SECURE=true`, `RETENTION_DAYS=90`, `RAILWAY_DEPLOYMENT_OVERLAP_SECONDS=0`.
4. Open `/` and sign in using the dashboard password. Create a project to obtain its write key once.
5. Use the Railway volume backup facility for backups. A persistent volume is not an off-site backup.

For local development use a writable `ANALYTICS_DB` and `COOKIE_SECURE=false` on localhost only.
Run `uvicorn app:app --host 127.0.0.1 --port 8080`.

## Track events from any language

`POST /v1/events` with `Authorization: Bearer <project write key>`:

```json
{"events":[{"id":"unique-event-id","name":"signup.completed","distinct_id":"user-123",
"properties":{"plan":"starter","source":"website"},"attributes":{"plan":"starter","tags":["new-user"]}}]}
```

`id` is optional, but **send a stable ID across retries** to avoid duplicate counts.
A successful response reports `accepted` and `duplicates`. At most 100 events per batch,
2 MB per request, 16 KB per properties/attributes object, 64 keys/object, five nested levels.
Timestamp is optional ISO 8601; UTC is used when no zone is supplied. Events older than retention
or more than five minutes in the future are rejected. Batches are atomic. Rate: 600 ingestion
requests/project/minute per service process. There is no public unauthenticated ingestion or CORS endpoint.

`POST /v1/identify` with the same key updates attributes without recording a separate event:

```json
{"distinct_id":"user-123","attributes":{"plan":"pro","tags":["vip"]}}
```

Attributes merge shallowly. Sending `null` keeps the key with a null value; tags are a normal array property.
Use opaque user IDs. Identity is producer-asserted; an IP is not a person and a shared key is not a staff login.

## Python / FastAPI integration

Copy `sdk/python/native_analytics.py` into your service and install it **after** other middleware:

```python
from native_analytics import install
collector = install(app, '/data/application.db', credentials={'support:partner': partner_key})
```

Configure `NATIVE_ANALYTICS_URL` with this service origin, `NATIVE_ANALYTICS_WRITE_KEY`
with a project key, and `NATIVE_ANALYTICS_SOURCE` with your app name. The queue is stored
beside the supplied DB path, in `analytics-outbox.db`. Keep that directory on a persistent volume.
Optional `NATIVE_ANALYTICS_TRUST_RAILWAY=true` uses Railway ingress's `X-Real-IP` header;
leave it off on other networks. Do not expose a trusted-proxy service through another untrusted ingress.

`collector.track('checkout.started', 'user-123', {'amount': 500}, {'plan': 'pro'})` records
an event plus user attributes. `collector.identify('user-123', {'tags': ['vip']})` records
`user.identified` and updates traits. `collector.status()` exposes queue count, oldest queued time,
last delivery, dropped count and last transport error. A pipeline heartbeat appears every minute.

ASGI middleware records all HTTP attempts reaching the application, including auth rejections,
404s and health checks: route pattern, method, status, duration, IP/source/peer IP, caller, operation,
environment, request ID, bytes read/sent, HTTP version, user agent, origin/referrer host, edge and
deployment ID. Request IDs are returned as `X-Request-ID` for support correlation. Middleware can
read `scope['analytics.operation']`, `scope['analytics.environment']` and
`scope['analytics.action_state']` for application-level outcomes without copying response bodies.

No raw query values, arbitrary headers, cookies, authorization, passwords/OTPs, request bodies,
response bodies, free-text support reasons or customer account IDs are collected by the adapter.
Route parameters and unknown path components are normalized. Properties and attributes also redact
sensitive field names and recognizable credential strings. Do not deliberately put private payloads
or secrets in innocent property names; automatic redaction cannot recognize every possible secret.
HTTP status success is not business success; `action_state` is separate where instrumented.

## Read/manage API

All `/api/*` routes require `Authorization: Bearer <admin token>` or a signed owner session.
Cookie-authenticated writes require `X-Native-CSRF: 1` and same origin. Keys are stored hashed.
Dashboard sessions expire after 12 hours. Login is rate limited in persistent storage.

- `GET/POST /api/projects`: list or create (`id`, `name`); creation returns write key **once**.
- `GET /api/overview?project=...&days=7`: defaults, pipeline report and event names.
- `POST /api/query?project=...`: `{days,event,distinct_id,property,value,metric,measure,breakdown,interval}`.
  All optional; default metric `count`. `measure` is required for numeric metrics. Fixed columns only,
  top-level property names, bound SQL parameters. Maximum 200,000 matching events/query.
- `GET /api/events?project=...`: days, event, distinct_id, property/value, status, ip, operation.
  At most 100 records/page, `next_cursor.before` and `before_id` continue even when timestamps tie.
- `GET /api/people?project=...&search=...&offset=0`: identity/attribute search, 100/page.
- `PATCH /api/people?project=...`: same shape as identify, for owner-edited traits/tags.
- `GET/POST /api/charts?project=...`: save `{title,query}`. Delete via
  `DELETE /api/charts/{chart_id}?project=...`.
- `POST /api/maintenance`: immediate retention cleanup. Hourly cleanup is automatic.

Date windows are relative to now, graph buckets are UTC, UI timestamps use viewer local time.
`count` counts events, `users` counts non-null distinct IDs; anonymous requests do not become synthetic people.
Numeric metrics ignore absent/non-numeric/bool properties. Percentile uses nearest-rank p95.

## Limits and operational honesty

This is a small, single-process tool; one volume, one replica. It is not a warehouse, security log
archive, session replay tool, funnel/experiment engine, or unlimited-scale telemetry service.
Events start at integration time; it cannot recover old IPs. Requests blocked at Railway's edge
never reach ASGI and are not logged. Disconnect/process death before local enqueue can lose a request.
Delivery retries are idempotent; the local queue defaults to 100,000 events, reports drops at capacity,
and should be monitored. A full/failed disk can lose local enqueue; that is logged and never stops
business requests. Ingestion failure retains the queue, it does not retry a business action.
Unauthorized callers are shown as unauthenticated. Forwarded headers and user agents are untrusted
metadata; network IPs may belong to NAT/proxies. This service makes no bank/customer/device outcome claim.

Retention limits disk growth over time, but capacity depends on traffic. Railway compute and storage
still incur normal infrastructure costs. No per-project pricing or billing code exists in this app.
Volume backups must be configured separately; no backup schedule is claimed by this deployment.

## Tests

```sh
python -m unittest discover -s tests -v
node --check static/app.js
```

Tests cover project isolation, key/read separation, cookie/CSRF/login limits, redaction, input bounds,
deduplication, queries, traits/tags, charts, retention, cursor ties, outbox recovery and safe ASGI capture.
