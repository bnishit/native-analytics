# Capacity and access readiness

The measurements below cover local execution and one completed, paced production run using warmed connections. The earlier interrupted run is retained separately. None is a hosted SLA, sustained or cold-connection production-capacity result, or enterprise-security certification.

## Measured local envelope — 2026-11-02

Run `python scripts/benchmark.py --output /private/new-report.json` from a development checkout with the test dependencies installed. The bounded harness starts one real loopback Uvicorn process and a new private SQLite WAL database, with fake credentials and no inherited cloud/mail/billing secrets. It removes them afterward. It refuses an existing report path. No real workspace, user or provider is accessed.

The current version reserves a private report before starting and appends fsynced, content-free request receipts to the adjacent `new-report.json.progress.jsonl`. Each request gets a sequence, phase, operation, start time and a received-response or raised-call timing. A waiting request refused at the final admission check records `request_not_dispatched`; its callback does not run. No credentials, bodies or exception text are retained. A received response is not a passed validation; only a passed final report together with the journal's `validated_complete` marker establishes completion. On process interruption an unmatched start remains unknown. Inspect existing records instead of retrying the workload. Recording failures stop new admissions; requests already admitted may still finish. This protects evidence from process interruption, not power loss. Journal request timings exclude journal writes; headline benchmark timings include recording and validation overhead, so they are not directly comparable to the older unjournaled measurement below.

A bounded local verification used 100 seed events, two synthetic tasks, two writers, two repetitions per read shape and the existing 10,000-event mixed phase: 10,104 observations and 118 start/result pairs, with no unmatched request. Seven fault tests cover process death, request failure, concurrent records, receipt-disk failure before/after the callback and existing-report refusal. The overlapping-failure test failed before the final admission check was added: the waiting callback must not run after another request or receipt write fails. These checks validate the recorder, not a production capacity envelope.

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

## Incomplete paced production probe — 2026-11-03

A private, independently reviewed client targeted only the existing isolated verification project on the production service at release `7565e8389cac39f698fb5e9ade91461ad4036ab6`. The plan was 8,000 seed product events, 100 real-time empty synthetic tasks (200 native observations), and 2,000 mixed-phase product events. Native batches contained at most 100 observations, with a one-second pause after every batch. Three read shapes—aggregate p95/breakdown, trace search and outcome evaluation—were checked ten times each after the seed, then started with one paced writer and three readers. The harness stops on the first error or any measured request over five seconds. It does not retry an uncertain write.

The seed and its 30 validation reads completed. During concurrent reads, a TCP connection-establishment timeout stopped the process after three mixed batches. **85 batches acknowledged 8,500 observations; the planned 10,200-observation workload did not finish.** No unresolved write intent remained and no workload was replayed. An existing-intent collision had been tested locally to refuse before HTTP, alongside lost-acknowledgment and slow-request stops.

Separate account-scoped readback verified 8,000 seed product events, 300 mixed product events and 100 synthetic traces. All 100 traces were observed without reported gaps; an opened sample retained its one successful empty-operation span. All 100 mature synthetic tasks correctly remained unclassified, with zero satisfied tasks, because no quality checks or completion milestones were invented. The synthetic goal minimum remained 25. The existing normal-user goals were unchanged. A repeated frozen trace search agreed. The interrupted harness did not persist per-request timing samples before failure, so latency percentiles and a complete mixed-load envelope are unavailable.

| Subsequent fresh-connection control | HTTP 200 | Connection timeouts |
|---|---:|---:|
| Same workstation, HTTPX, nine public health reads | 7/9 | 2/9 |
| Same workstation, curl, nine public health reads | 6/9 | 3/9 |
| Tracker production runtime, Node HTTPS, nine public health reads | 9/9 | 0/9 |

Each control used three workers, new connections and no retries. The local failures occurred before TCP connection completion; the controls sent no credentials or application data. These small, separately timed controls do not identify a route, provider or service root cause, establish general availability, prove load causality, or measure authenticated query capacity. Impact on other users is unknown. They must not be pooled into a customer failure rate.

The production runtime before and after the probe was Linux x86_64, Python 3.12.16 and SQLite 3.46.1 at the same release. Cgroup ceilings were eight CPU-seconds per second and 8,000,000,000 bytes of memory; these are ceilings, not reserved throughput. The shared database grew from 142,372,864 to 149,602,304 bytes between metadata reads; unrelated activity was not controlled, so the difference is not attributed wholly to this probe. No resource, access, rate-limit, deployment or backup setting changed during measurement. Five offline controls and both source reviews matched probe SHA256 `c10dedac19ea2d56a1da49aa15471aec8b9a6989980051faafe33dca4208b13d` before its one execution.

This interrupted attempt remains incomplete; it was not restarted. The separate completed measurement below uses new identities and durable receipts. Neither confirms an application defect or establishes useful user outcomes.

## Completed paced production measurement — 2026-11-03

A new, once-run client targeted only the isolated verification project on production release `3bad5daa073407acf113ffc7dec3f54222f5c67c`. Both independent source reviews and six offline safety controls matched program SHA256 `fe39d6ab10fb1f8ddf1a17b906458e6d5566083e227d9f9c8a66ce284c5f4d0c` before execution. The earlier attempt and its observations were not replayed. The test made no model, Slack or business call and changed no resource, access grant, rate limit or deployment.

The planned **10,200 observations completed**: 8,000 seed product events, 100 real-time empty synthetic tasks with two observations each, then 2,000 mixed-phase product events. There were 100 opaque synthetic product identities. All 102 batches acknowledged exactly 100 new observations, with 10,200 unique acknowledged IDs and no unresolved write intent. Native JSON, the normal rate guards and the 25-task synthetic goal minimum were retained. Separate account-scoped readback matched both product counts and all 100 observed traces without reported gaps; one opened sample had its expected single successful span. All 100 mature synthetic tasks remained unclassified, with zero satisfied, because quality and completion evidence were absent. Normal-user goals were untouched. Repeated frozen search agreed.

One writer paused one second after every batch. Each of three read shapes ran ten times after seeding, then ten times concurrently with the mixed writer, pausing one second after each read. Seed ingestion took 148.464 seconds including pauses; batch p95 was 1,573.02 ms and maximum 1,849.85 ms. Mixed ingestion took 35.349 seconds including pauses; batch p95 was 1,155.91 ms and maximum 2,520.21 ms. These finite paced rates are not saturation or sustained throughput.

| Read | Seed-only p95 | Mixed-phase p95 |
|---|---:|---:|
| Aggregate p95 + breakdown | 1,660.64 ms | 2,173.00 ms |
| Trace search | 769.74 ms | 587.15 ms |
| Outcome evaluation | 489.50 ms | 698.51 ms |

These are workstation-observed summary timings, including network, private receipt fsync and result validation. With only ten repetitions, nearest-rank p95 equals the largest sample; it does not estimate a population tail. All three mixed read windows overlapped the writer, but ended before it. The request journal retained 169 paired start/result records plus 169 content-free connection observations. Completion required a passed report and a final validated-complete journal marker. Stable exclusive batch intents and separate ACK files remained available for reconciliation; no uncertain write was retried.

Client: Darwin arm64, Python 3.14.3, HTTPX 0.28.1. HTTP/1.1 used separate reader/writer pools, each capped at four connections, with client-side keepalive expiry disabled. The server could still close idle connections. Eight credential-free public health reads warmed the pools before phases; none needed its permitted connect-timeout retry. **All 169 application calls reused warmed connections**, with zero measured transport retries. A separate fresh-client public health control afterward timed out before TCP completion at 5.035 seconds; its trace hook recorded a new connection start without TCP/TLS completion. This confirms the warm-connection measurement, not reliable cold connections or general availability. Do not pool these separately timed controls into a customer failure rate or infer a server/network root cause. Impact on other users is unknown.

Server metadata before and after matched the same release: Linux x86_64, Python 3.12.16 and SQLite 3.46.1. Cgroup ceilings were eight CPU-seconds per second and 8,000,000,000 bytes of memory, not reserved throughput. The shared database grew from 150,704,128 to 159,346,688 bytes; unrelated traffic was uncontrolled, so the entire change is not attributed to this test. Free volume space was above 4.6 GB at both metadata reads. Readback health returned HTTP 200.

This establishes one finite, paced, warmed-connection native-JSON measurement on a shared production service. Sustained loads, cold-connection reliability, OTLP/debug throughput, broader datasets, peak resource use, failover and horizontal scaling remain unverified. It does not establish customer latency, useful outcomes, an SLA or full operating readiness.

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

Local tests directly cover unauthenticated/project isolation, distinct read/write scopes, exact evidence binding, fixture privacy rejection, debug encryption/expiry/erasure, source revision controls, OAuth consent, role downgrade, revocation and generic-query isolation. Remote-MCP role tests change membership through the public member API with a second verified local owner, rather than editing database roles directly. Active tokens lose editor permissions immediately; reader access remains, refresh narrows scopes, later promotion cannot expand the refreshed token, and member removal blocks reads and refresh. Denied writes leave the saved source revision unchanged. See the relevant outcome, regression, debugging, collection-health and remote-MCP test modules. These are disposable local accounts, not production membership changes. A bounded production OAuth test separately verifies one project’s approved scopes, goal/fixture access, selected redacted debugging, cross-project/raw-event/ungranted-scope denials, refresh rotation and grant revocation. Its 180-day grant used one-hour access tokens and was revoked after testing. The isolated production workspace has only its sole owner at the role-test preflight; a separately verified second account is required before changing a test member. Production role-downgrade and broader client adoption remain unverified.

Generic analytics queries exclude internal trace, audit, source-evidence and collection-health payloads, including when an explicit event is selected. Diagnostic access uses the separately scoped projections; opaque binding references and digests are not general event properties available to analytics readers.
