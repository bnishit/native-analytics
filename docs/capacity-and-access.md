# Capacity and access readiness

The operating-envelope measurements below are local, even where the capabilities are deployed. It is not a hosted SLA, a production load test or an enterprise-security certification.

## Measured local envelope — 2026-11-02

Run `python scripts/benchmark.py --output /private/new-report.json` from a development checkout with the test dependencies installed. The bounded harness starts one real loopback Uvicorn process and a new private SQLite WAL database, with fake credentials and no inherited cloud/mail/billing secrets. It removes them afterward. It refuses an existing report path. No real workspace, user or provider is accessed.

Hardware/runtime: Darwin arm64, 10 logical CPUs, 16 GiB RAM; Python 3.14.3, SQLite 3.53.3. Local temporary filesystem, one process. The production container uses a different Python/runtime/disk/network; do not transfer these timings to Railway.

Dataset: 20,000 named product events and 1,000 synthetic workflow tasks (two observations each), then 10,000 additional product events during the mixed phase: **32,000 retained observations**. There are 100 opaque synthetic identities. Native JSON batches contain 100 events. The seed uses four concurrent writers. The mixed phase runs one writer with a 50 ms pause between 100 batches and three query workers. Every seed receipt and final stored count is checked. Aggregate p95 matches the generated values; trace counts match 1,000 tasks; all 1,000 tasks correctly remain unclassified because quality evidence is absent.

The seed accepted 22,000 observations in **2.829 seconds** (7,775.7/second finite burst). Seed batch latency p95 was 132.35 ms; the slowest was 677.37 ms. This burst rate is not sustained throughput: the normal per-project 600 ingestion requests/minute guard remains enabled. The mixed writer accepted another 10,000 observations over 8.065 seconds; its batch p95 was 76.27 ms.

| Read | Seed-only p95 | Mixed-phase p95 |
|---|---:|---:|
| Aggregate p95 + breakdown | 84.91 ms | 355.05 ms |
| Trace search | 76.81 ms | 247.31 ms |
| Outcome evaluation | 66.56 ms | 186.62 ms |

Each query has 10 measured repetitions per phase. Query workers start with the paced writer; some reads may finish after writes. Each HTTP call opens a fresh connection. Database logical size after queries was 19.70 MiB; database/WAL/shared-memory files occupied 19.73 MiB at that measurement point. The service resident-memory snapshot was 72.70 MiB. These are point measurements, not peak disk/RAM guarantees.

This covers native JSON, one workload size, bounded local concurrency and these read shapes. It does not cover sustained hour/day loads, production disk/network, OTLP throughput, selected-content encryption, provider/model work, broad customer distributions, adversarial traffic or horizontal scaling. Existing rate limits and query bounds were not bypassed. Use the JSON report from an actual run for this machine's timings; do not use these values as pass thresholds on another host.

## Declared operating limits

- One process/replica, SQLite WAL, one persistent volume. No multi-node writer, automatic failover or horizontal-scaling claim.
- Native batches: at most 100 events and 2 MB per HTTP request; 600 authenticated ingestion requests per project/minute, shared by native/OTLP. OTLP additionally bounds compressed/decompressed size and 500 spans per request. Limits bound inputs; they do not guarantee that maximum sustained throughput is achievable.
- General analytics and Slack audit reads cap scans at 200,000 observations. Native workflow reviews and their scoped evidence receipts each cap at 20,000; larger reads fail with a narrower-window instruction. Outcome entry plus observed follow-up fits 90 days. Collection health has its smaller pulse/source/process bounds.
- The Python outbox is durable only if its private volume is writable and retained. The Node queue is bounded memory. Queue overflow, disk failure and process termination remain possible gaps. A source heartbeat is not a complete task ledger.
- Configure production volume retention, off-site backups, secret retention and restore rehearsal separately. The operator snapshot and local crash tests do not replace those deployment checks.

## Access-readiness assessment

| Surface | Implemented boundary | Current limit |
|---|---|---|
| Producer ingestion | Separate per-project write key; no analytics read rights; rate limits; strict typed metadata | A compromised producer can lie about its own project observations. Evidence resolution does not independently verify source truth. |
| Browser accounts | Verified email link or username/password; salted scrypt password verification; HttpOnly/Secure/SameSite cookies; CSRF/origin checks; server-side sessions | Human sessions last up to the requested 180 days. Possession of a valid stolen session remains access until expiry/revocation. |
| Workspace agent keys | Expiring workspace/role scope; effective access bounded by current membership | An operator credential is intentionally broader; it is never a fallback for a customer workflow. |
| Remote MCP | One project, explicit scopes, PKCE/browser consent, one-hour access token bounded by an up-to-180-day grant; live role/revocation checks | Custom MCP connection is not app-directory publication or corporate single sign-on. Existing grants are never expanded automatically. |
| Outcome goals | Audit-scoped reads; separate goals:write and editor role for immutable versions | A goal checks declared evidence; it is not independent business verification. |
| Reviewed cases/datasets/runs | Separate experiments:write and editor role, including fixture reads; pinned versions/hashes and expiry | Fixture content is explicitly reviewed, not guaranteed free of every private fact. No arbitrary evaluator code executes on the server. |
| Selected debug content | Default off; owner policy; bounded scope/window/budget; separate editor debug:read; pre-upload/server redaction; AES-GCM and expiry | Redaction is best effort. Authorized selected-content access is sensitive. Other normal database rows are not application-encrypted. |
| Collection expectations | Audit-scoped reads; separate collection:write and current editor role; revision guard | Current configuration is not a historical source inventory. Missing or disabled declarations cannot prove clean collection. |
| Release outcome reads | Audit:read plus experiments:write, editor membership and exact version/artifact/receipt bindings | Read-only comparison grants no merge, deployment or provider authority. Before/after remains observational. |
| Backups | Offline operator-entire-database tool, separate archive key, new private restore path | Not a project-scoped customer export. A restored backup may contain since-erased data or revoked access; reconcile before promotion. |

SSO/SAML/OIDC federation, SCIM provisioning and MFA through TOTP/WebAuthn are **not implemented** in the account/auth source or dependency surface reviewed here. OAuth authorization for an agent is not enterprise SSO or MFA. Do not promise enterprise identity readiness. Organizations requiring those controls need them before adopting this account system. This release does not claim a complete security certification or a full tamper-proof audit ledger for every product-definition edit.

Local tests directly cover unauthenticated/project isolation, distinct read/write scopes, exact evidence binding, fixture privacy rejection, debug encryption/expiry/erasure, source revision controls, OAuth consent, role downgrade, revocation and generic-query isolation. See the relevant outcome, regression, debugging, collection-health and remote-MCP test modules. Passing tests do not prove production cookie/configuration or every future integration; a bounded production OAuth test now verifies one project’s approved scopes, goal/fixture access, selected redacted debugging, cross-project/raw-event/ungranted-scope denials, refresh rotation and grant revocation. Its 180-day grant used one-hour access tokens and was revoked after testing. Production role-downgrade and broader client adoption remain unverified.

Generic analytics queries exclude internal trace, audit, source-evidence and collection-health payloads, including when an explicit event is selected. Diagnostic access uses the separately scoped projections; opaque binding references and digests are not general event properties available to analytics readers.
