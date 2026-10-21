# Afterspan

**You ship. Then what?**

**Open-source product and agent analytics. MIT licensed. Run it yourself.**

Funnels, saved behavior/trait segments, linked journeys, retries, event metrics and scoped agent access are included in the core. Self-hosting needs no Stripe account, subscription or commercial quota. Accounts, teams and roles protect your own instance. Optional hosted billing is a separate opt-in; it never gates a core analytics feature.

### Run locally

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-agent.txt
.venv/bin/python scripts/dev.py
```

Open http://127.0.0.1:8769/app. This loopback preview creates private local email files instead of sending mail. Open your sign-in email from `data/mail` and consume its link. Create a workspace/project, store its producer key privately, and connect your app. Never publish that local mail folder or use local-mail mode on a public service. For a public instance configure HTTPS, secure cookies, email delivery and a persistent volume as described below.

`AFTERSPAN_EDITION=self_hosted` is the default. Usage counts remain useful for server capacity; there are no commercial event, payload, project or seat limits. Request rate, query-size, definition-count and retention bounds still protect the service. This is one process on one SQLite volume, not unlimited infrastructure.

### Onboard a repository with existing analytics

Open **Goal loop → Connect a repository**. An owner connects a GitHub repository, scans a branch, reviews an event outcome goal, and adopts its exact proposal. Keep current Native Analytics events or select a connected PostHog project whose event is found in the pinned code. Old PostHog taxonomy does not prove that a current native release sends those events. Unsupported patterns and the 64-file scan cap stay visible.

The versioned `afterspan-repo-goal-v2` contract supports logged event outcomes and native Slack read-request outcomes, with disjoint success, failure and unclassified counts. It does not reuse the original workspace/24-hour answer metric. Missing status, environment or release fields are coverage gaps. Fewer observations than the declared `min_sample_size` leave the rate unavailable. A successful HTTP response describes that logged response, not completion of a customer task.

New local setup generates `AFTERSPAN_CONNECTION_KEY` in its private configuration. On an existing installation, generate and store a URL-safe base64 encoding of 32 random bytes once in the server secret store. Keep it with your encrypted backup keys. Connection writes fail closed without it. Do not replace it on restart or rotate it without a separate credential migration. Provider keys are encrypted with AES-GCM and project/connection/revision context. An owner session can connect, rotate or disconnect; workspace keys cannot issue themselves provider access. Disconnect blocks future Afterspan use and does not revoke the provider key itself. Read probes establish target access and report credential scope as unverified. Use dedicated provider credentials with the least permission needed.

Source connection input comes from the owner UI or a mode-600 file. Never put a credential in a command argument, repo goal, MCP call or browser URL. Exact target configurations are:

| Provider | `kind` and `config` | Read or action boundary |
|---|---|---|
| GitHub | `github`, `{ "repository": "OWNER/REPO" }` | Fixed GitHub API host. Scan needs repository reads; draft PRs need contents and pull-request writes. Automatic merge also needs checks, Actions, deployment and branch-protection reads. |
| PostHog | `posthog`, `{ "region": "us", "project_id": 123 }` | US or EU fixed host, bounded ad-hoc aggregate queries. Supply a dedicated read key. No raw event export, arbitrary SQL or caller host. |
| Railway | `railway`, `{ "project_id": "UUID", "environment_id": "UUID", "service_id": "UUID" }` | Reads the latest deployment of this exact service and actual named environment. Does not deploy or read service variables. |

A private connection file wraps `kind`, `config` and `credential`. Set `AFTERSPAN_SESSION_FILE` to the owner receipt. `loop-source-connect` returns sanitized target metadata, never the secret. Existing scoped editor keys can scan, propose, prepare, reconcile and observe. They cannot adopt a goal or grant action policy.

```sh
python cli/native_analytics.py loop-source-connect --project PROJECT_ID --id CONNECTION_UUID --revision 0 --file /private/github-connection.json
python cli/native_analytics.py loop-sources --project PROJECT_ID
python cli/native_analytics.py loop-scan --project PROJECT_ID --id SCAN_UUID --file scan.json
python cli/native_analytics.py loop-job-status --project PROJECT_ID --id SCAN_UUID
python cli/native_analytics.py loop-scan-status --project PROJECT_ID --id SCAN_UUID
```

`scan.json` contains `connection_id`, `base` and optional immutable `commit`. Omitting `commit` pins the current branch head once. Scan and V2 artifact preparation return HTTP 202 with a stable job ID while provider reads run. Poll `loop-job-status` or MCP `loop_job_status` until `complete`, then read `result`. Pending is not success. A restart marks unfinished jobs interrupted. Resubmit the same original request and ID to recover; changed input under that ID returns 409. Scans read regular committed text without executing repository code, install steps or hooks. Findings contain source citations and hashes, not raw source files.

Use `loop-propose --project PROJECT_ID --id PROPOSAL_UUID --file goal.json`. The body is the exact V2 goal schema in OpenAPI. The event and declared dependencies must have a matching source citation in the pinned scan. The owner uses `loop-adopt --project PROJECT_ID --goal GOAL_ID --file adoption.json` with the returned `proposal_id` and `proposal_sha256`. Adoption stores a scan-bound proposal. It does not yet claim that a goal is committed or tracking is healthy.

Ask the coding agent to prepare one patch. Its file contains `files`, each with `path`, exact `before_sha` or null for the new goal, and complete UTF-8 `content`. The new `.afterspan/goals/GOAL_ID.json` must equal the approved definition serialized as sorted compact UTF-8 JSON plus one newline. Include changed scanned source dependencies. An existing adjacent `.test.ts`, `.test.tsx`, `.test.js` or `.test.jsx` may accompany its changed source; its pinned regular-file bytes are checked independently. New, unrelated or symlink test files are excluded. Workflows, package manifests, locks, secrets, symlinks, renames and arbitrary new source files are excluded. The same eight-file, 128 KiB and 4,000 combined-review-line limits include companion tests. This path verifies exact reviewed bytes, not deterministic repair correctness. Reusing an existing V2 goal file for a later repair is not yet supported; the original Python recipe remains separate.

```sh
python cli/native_analytics.py loop-prepare --project PROJECT_ID --goal GOAL_ID --request-id REQUEST_UUID --file artifact.json
python cli/native_analytics.py loop-job-status --project PROJECT_ID --id REQUEST_UUID
python cli/native_analytics.py loop-policy --project PROJECT_ID --goal GOAL_ID --revision 0 --file policy.json
python cli/native_analytics.py loop-action --project PROJECT_ID --id ACTION_UUID --operation draft-pr
python cli/native_analytics.py loop-action --project PROJECT_ID --id ACTION_UUID --operation reconcile
```

The owner policy pins `artifact_sha256`, the ordered file paths, repository, branch, environment, line/file limits, operations and expiry. The owner page can allow and open that exact draft for one day. Automatic merge retains strict protected-branch checks and exact-head independent approval. An externally merged PR can be reconciled, but is not labelled service-auto-merged. Unknown writes reconcile with the same action ID before retrying.

After merge, use `loop-release --project PROJECT_ID --id ACTION_UUID --file release.json`, with `{ "connection_id": "RAILWAY_CONNECTION_UUID" }`. Afterspan rechecks merged artifact bytes and the latest successful Railway deployment. Commit SHA, project, service, environment ID and actual environment name must match. A newer building, failed or different deployment stays unproved. GitHub deployment proof remains supported for the original contract.

Use the existing `loop-observe` and `loop-compare` commands with explicit frozen windows. V2 observation `release_bound` requires exact deployment proof, a matching release property and a post-release window with observed events. Release, event arrival and enough evidence remain separate. Before/after is observational. Native reads apply the stored receipt cutoff and report retention/erasure gaps. PostHog aggregates are frozen when fetched; ingestion at an earlier requested `as_of` cannot be reconstructed, so release comparisons with that missing cutoff remain unknown. Historical caller imports retain attestation provenance and cannot qualify native checked answers.

The fixed-checkout and workspace MCP bridges expose `loop_sources`, `loop_scan`, `loop_scan_status`, `loop_job_status`, `loop_propose`, `loop_prepare`, `loop_action`, `loop_release`, `loop_observe`, `loop_compare` and `loop_status`. All share the API engine. Credentials, owner adoption and permission grants stay in the owner UI or private CLI session. The source adapter uses the supported bounded [PostHog query API](https://posthog.com/docs/api/queries); it is not a bulk export connector.

### Measure a source-checked bot reply

Use the same proposal, exact owner adoption, reviewed tracking PR, release and observation commands with a native measurement:

```json
{"kind":"slack-read-outcome-v1","unit":"observed_read_request","event":"slack_message_received","command_kind":"list_experiments","verifier":"growthbook-catalog-v1","deadline_seconds":60,"min_sample_size":25}
```

The event is a static repo citation in a declared dependency. The evaluator uses the existing validated `afterspan_audit` request chains, not message-event or tool-call counts. Native mapping uses `environment` and `release_sha`. Each producer stage declares its bounded command kind, exact Git revision and original `request_started_at_ms`; older missing metadata remains a coverage gap. Send the classified parser/planner observations before running the live read, so missing delivery remains observable. Collection is best effort, not a complete Slack ledger.

One latest business attempt is counted per observed request, with its original start clock. Requests wait outside the mature denominator until the full deadline. Pass requires accepted delivery by that deadline plus a separate matching-version catalog source check. Known late/failed delivery or wrong displayed facts fail; missing or conflicting checks stay unclassified. Other command kinds are counted separately, not passed as catalog answers. The source check covers displayed names, enabled state and explicit Identity list-entry counts, not full question fulfilment, human reading or overall usefulness. Afterspan aggregates producer-side checks; it does not fetch GrowthBook itself. Unknown classification, release, start, selection or retention/erasure evidence blocks a clean rate. Use frozen normal-use windows that exclude labelled owner probes; do not backfill them as customer evidence.

### Follow a repo goal through a tracking PR

The **Goal loop** project page keeps one product question beside its tracking repair, PR, release evidence and qualified outcomes. The first supported goal asks whether an observed workspace gets a correctly grounded Codex answer within 24 hours. Each workspace starts its clock once. Retries cannot restart it. Waiting workspaces stay outside the mature denominator.

The committed example is [`.afterspan/goals/first-answer.json`](.afterspan/goals/first-answer.json). Goal wording, measurement meaning, source mapping and owner policy have separate immutable revisions. Existing funnels retain their repeated-start rules. The loop does not claim lifetime-new workspace eligibility.

```sh
# Inspect committed files without a running service.
python cli/native_analytics.py loop-inspect --repo . --repository YOUR_ORG/YOUR_REPO --base YOUR_BRANCH
# Use an existing private owner session or a scoped workspace editor key.
python cli/native_analytics.py loop-sync --project PROJECT_ID --repo . --repository YOUR_ORG/YOUR_REPO --base YOUR_BRANCH
python cli/native_analytics.py loop-status --project PROJECT_ID
python cli/native_analytics.py loop-prepare --project PROJECT_ID --request-id UUID
python cli/native_analytics.py loop-action --project PROJECT_ID --id ACTION_UUID --operation draft-pr
```

The service independently fetches the committed GitHub goal and recomputes the supported repair. Configure `AFTERSPAN_GITHUB_REPOSITORY=YOUR_ORG/YOUR_REPO` and `AFTERSPAN_GITHUB_TOKEN` in the server's secret store. The fine-grained token needs that repository's contents and pull-request writes, plus metadata, checks, Actions, deployments and branch-protection reads. Never give this token to a coding agent. Missing permission blocks the affected operation. No repository code runs on the server.

Production goal sync supports the unfiltered built-in `codex.connection.started` / `codex.answer.ready` lifecycle with a production source mapping. It does not assert that the local pilot emits production traffic, and it grants no tracking-write authority. The first repair recipe remains local-only and repairs only the missing completion hook in `examples/loop_pilot.py`. It sends an opaque workspace and observation reference; it sends no question, answer, credential or user text. A retry with the same prepare request ID recovers the same action after an interrupted preparation. An unrelated edit produces no patch. Unsupported code needs review. This is a narrow supported repair, not a general code editor.

Only an owner session or the instance operator can grant, pause or revoke action policy. Delegated workspace keys cannot grant themselves authority. In **Goal loop**, the owner can allow draft PRs for seven days. The CLI `loop-policy --revision N --file policy.json` exposes the full typed policy. Automatic merge requires protected, current code, pinned independent checks and review. The server token needs repository contents, pull requests and deployments access, plus Administration read access to inspect branch protection. It also requires release-on-merge permission because external deployment hooks may exist. An open or merged PR does not prove release. Reconciliation accepts the verified one-parent squash commit, or a normal merge whose parents are exactly the approved base and tracking head and whose full tree equals the verified tracking head. A successful GitHub deployment must name the exact merge commit and environment and include `artifact_sha256` in its payload. This is provider-reported release evidence, separate from an exact received completion event and a useful outcome.

A completion event alone cannot qualify. `loop-assess` submits a structured answer and its exact event reference. The service compares source, meaning, counts including zero, limitations and next action against an immutable service-generated observation. The caller cannot choose expected counts or a pass/fail verdict. This first rubric checks faithful aggregate interpretation, not subjective usefulness.

Use `loop-observe --id UUID --file window.json` with explicit `entry_start`, `entry_end`, `observed_through` and `as_of`. Use `loop-compare --file comparison.json` with observation IDs `before`, `after` and optional `action_id`. An optional `release` selects a release cohort after the first workspace entry is fixed across all release labels; a retry on another release cannot restart the clock. Excluded release entries are reported. The report separates checked, failed, unassessed, timed-out and waiting workspaces. Before/after remains observational. Missing release proof cannot establish a post-release improvement. Without a bound action the result stays `unknown`; a descriptive `unbound_rate_difference` does not establish impact. Retention, erasure, late arrivals and the 200,000-event read cap remain explicit limits.

For the real Afterspan-to-Codex journey, open **Goal loop → Start measured Codex connection**, or run `loop-connect --project PROJECT_ID`. This records setup intent using the service clock and the authenticated project's workspace identity. It does not issue credentials or prove an MCP handshake. Reconnecting to the same goal meaning and mapping returns the original start. The agent then uses `loop_deliver` (CLI: `loop-deliver --project PROJECT_ID --connection CONNECTION_UUID --id ANSWER_UUID --file answer.json`) with `{"evidence_id":"OBSERVATION_UUID","answer":{...}}`. The answer object is the existing structured aggregate-answer schema. Completion and fact checking commit together, using server timestamps. Reuse the answer UUID for transport retries. A different payload under that UUID is rejected. No producer key, caller workspace identity, timestamp or quality verdict is accepted by these lifecycle endpoints. These local lifecycle events have no runtime release label. Use unfiltered native evidence for submission; release-filtered observations return 422 until the runtime can supply a matching release.

The service records answer submission for presentation, not human reading, inbox delivery or subjective usefulness. A checked answer within a few seconds still leaves a new workspace waiting in the 24-hour cohort. A late checked answer cannot qualify an earlier deadline. A lost or erased start returns `410` instead of creating a fresh clock. The owner page marks this connection blocked and labels earlier answers as historical. Moving a database to a different service environment also blocks new submissions; immutable receipt replays remain readable. Raw-event redaction, erasure checks and usage metering are shared with normal ingestion. The service environment defaults to `local` for a loopback public origin and `production` otherwise; set `AFTERSPAN_ENVIRONMENT` explicitly for staging, development or test. It must match the committed goal mapping. These built-in lifecycle calls support only the unfiltered `codex.connection.started` / `codex.answer.ready` contract; other goals need their own instrumentation.

The fixed-checkout MCP bridge is `agent/loop_server.py`. Configure `AFTERSPAN_REPO_ROOT`, `AFTERSPAN_PROJECT`, `AFTERSPAN_WORKSPACE_KEY`, `NATIVE_ANALYTICS_URL`, `AFTERSPAN_GITHUB_REPOSITORY` and `AFTERSPAN_GITHUB_BASE` before launch. It has no operator fallback, policy-grant tool, shell tool or arbitrary path input. The workspace bridge also exposes remote loop operations. All routes and exact body schemas are in `/openapi.json`; the guide is `/docs#product-loop`.

External aggregate imports remain attestations. They never join native totals or become independent answer checks. Missing source-contract coverage must remain unknown; an absent event schema cannot establish a zero conversion rate. The native loop is the first supported execution path. Automatic querying across arbitrary providers is not included.

### Funnels, saved segments and retention

A **segment** is a saved set of rules that is checked again each time you run it. Open **Segments**, name the group, choose all/any, and add event-count or current-trait rules. Event rules support typed equality and one-of property filters and minimum/maximum counts, including zero. Trait rules support equals, not-equals, exists and numeric bounds. Only identities with a retained product event in the selected window are eligible. A zero-event rule does not find every inactive signup. Traits are current profiles, not historical snapshots.

Open **Funnels**, name the funnel, enter 2–8 event steps, set a conversion duration and optionally choose a saved segment. Run it to see unique entrants, conversion, drop-off and median time for the first completed path. Save it for another person or coding agent to reuse. Events must have a distinct_id: installation/workspace identities stay those units, never automatically people. Steps have strict increasing timestamps; equal timestamps cannot advance. Any valid repeated-start sequence may convert an identity; it counts once per step. All steps must occur within both the selected window and conversion duration. Outcomes after the window are not observed yet; the report counts nonconverters whose observation window is still open. Optional `breakdown` assigns each identity to its first qualifying entry-property value. These disjoint groups do not imply good or bad outcomes. A funnel rate is unique converters / unique entrants, not an average of daily rates. Internal setup/health events and anonymous events are excluded.

All reads return start/end/as_of and a fingerprint. Echo the returned query for the same event/receipt window. Current trait changes, retention and erasure can still change membership. Segment membership is evaluated over the whole selected window, not at the time of funnel entry. Referenced segment definitions/revisions are included in funnel evidence and its fingerprint. No raw member lists are exposed by these aggregate endpoints.

The UI, API, CLI and MCP use the same engine:

| Action | API | CLI |
|---|---|---|
| List | GET /api/{funnels|segments|retention} | funnels, segments or retention |
| Run | POST /api/{funnels|segments|retention}/analyze | analyze-funnel, analyze-segment or analyze-retention --file query.json |
| Create | POST /api/{funnels|segments|retention} | save-funnel, save-segment or save-retention --file saved.json |
| Update | PUT /api/{funnels|segments|retention}/ID, revision=LAST_READ | update-funnel, update-segment or update-retention --id ID --revision N --file saved.json |
| Remove | DELETE /api/{funnels|segments|retention}/ID, revision=LAST_READ | delete-funnel, delete-segment or delete-retention --id ID --revision N |

Every call is project-scoped (`?project=ID` or `--project ID`). Workspace readers and narrow project read keys can analyze/list; editors can save/change/remove. Stale edits return 409. A segment used by a saved funnel cannot be removed until the reference is changed. Cross-project segment IDs are rejected. At most 100 definitions per kind/project and 200,000 events per analysis; oversized reads fail explicitly, never return partial conversion counts.

Query example:

```json
{"days":7,"definition":{"steps":[{"event":"agent.task.started","filters":[{"property":"environment","value":"production"}]},{"event":"agent.task.succeeded","filters":[{"property":"environment","value":"production"}]}],"conversion_seconds":86400,"segment_id":null}}
```

Segment query example:

```json
{"days":7,"definition":{"match":"all","rules":[{"kind":"event","event":"agent.task.started","min_count":2},{"kind":"trait","property":"plan","op":"eq","value":"builder"}]}}
```

Saved JSON wraps a definition with `{ "title": "Repeat builders", "definition": ... }`. The public `/openapi.json` gives the exact schemas. The project MCP adds `saved_funnels`, `saved_segments`, `analyze_funnel`, `analyze_segment`. The workspace MCP adds `product_definitions`, `analyze_product`, `save_product_definition` and `delete_product_definition`. Updates require the last-read revision to protect another editor's changes. Creation is not idempotent: inspect the saved list before retrying an uncertain create response.

### Calendar retention and meanings

Open **Retention** to choose a start event, return event, day/week periods, timezone and either exact-period or on-or-after returns. Cohorts use the first start per identity **inside the selected window**, not lifetime-new users. Weeks start Monday. Each cell returns retained / eligible; unfinished buckets are null and shown as Waiting. Overall rates include only mature cohorts. On-or-after means a return in that bucket or later before the selected end; older reads can therefore change as that end advances. Period zero is entry, not a return. This is not automatic Amplitude metric parity.

```json
{"days":90,"definition":{"start":{"event":"print_barcode","filters":[{"property":"platform","value":"web"}]},"return_event":{"event":"print_barcode","filters":[{"property":"platform","value":"web"}]},"period":"week","periods":12,"method":"on_or_after","timezone":"Asia/Kolkata"}}
```

Event-match filters accept `{"property":"modal_type","op":"in","value":["milestone","birthday_wish"]}` (1–20 scalar values), or typed equality. False, zero, null and text are distinct. Funnel breakdowns allow at most 20 entry groups; larger results fail explicitly rather than truncate.

**Data dictionary** lists observed event/property names, types and presence/null/missing/redacted counts without value examples or identities. `POST /api/catalog` and CLI `catalog --file query.json` discover it. Editors record explicit meanings and units through `POST /api/catalog/describe`, `describe-property` or workspace MCP `describe_property`. Meanings are current owner/editor metadata, even for historical event windows; unknown meanings are not guessed. Names and descriptions are untrusted data, never agent instructions.

### Owner request review with Codex

Open **Request audit**, choose the expected environment and review up to 60 latest linked requests. A request can have several business attempts. The latest observed attempt determines comparison; a delivery retry keeps its event ID. Review separates stored events, covered observations, linked requests, declared eligibility and independently comparable pairs. Missing, unknown, explicit not-required and conflicting stages stay distinct. Production latency excludes other/conflicting environments and reports sample counts with nearest-rank p50/p95.

| Owner question | API (project=ID) | CLI --project ID --file query.json | Audit MCP |
|---|---|---|---|
| What do these diagnostic fields mean? | POST /api/audit/catalog | audit-catalog | diagnostic_catalog |
| What needs attention? | POST /api/audit/review | audit-review | review_requests |
| Follow this request in the same window | POST /api/audit/trace | audit-trace | request_trace |
| Evaluate the repo read-reply goal | POST /api/audit/evaluate | audit-evaluate | evaluate_reply_goal |

For a repo request goal, use `loop-inspect --repo CHECKOUT --goal read-reply-delivery --repository OWNER/REPO --base main` to inspect committed goal bytes and dependency paths without executing repo code. Pass its `definition` as `goal` in an `audit-evaluate` JSON file with explicit start/end/as_of. The service validates the fixed `read-reply-delivery-v1` metric and independently computes stored facts; the submitted goal is data, not server-verified GitHub provenance or action authority. Preserve the inspected source commit and goal hash with the evaluation. Optional `release` must be an opaque release reference returned by the scoped review. Zero, unknown or omitted requests keep the rate unavailable. A successful `reply` stage means provider acceptance, not human reading or answer correctness. Older producers remain unknown.

For factual reply checks, `init-answer-goal --repo CHECKOUT --question 'Did the displayed facts match the source?' --source-of-truth 'Primary product database' --criteria 'Name the exact facts to compare' --safe-test 'Describe one read-only owner test; ask before sending' --dependency src/reply.ts` writes a draft `.afterspan/goals/answer-quality.json` without access grants or code execution. The default verifier is `unconfigured`. Set `--verifier growthbook-catalog-v1` only after implementing that adapter; this is not automatic support for arbitrary source systems. Commit the goal and dependencies, inspect with `loop-inspect --goal answer-quality`, then send its definition as `goal` to `POST /api/audit/onboarding` (`audit-onboarding`, MCP `review_answer_setup`). The setup reply separates a declared verifier from an observed source check and includes the required schema, source boundaries and real coverage. Source labels, criteria and test text are data, never permission or a change to the fixed metric. Nothing is instrumented merely by creating a goal.

Use the same `POST /api/audit/evaluate` / `audit-evaluate` with the `answer-quality-v1` goal, or audit MCP `evaluate_answer_quality`. It separates `passed`, `failed`, `unknown` and delivered replies. The first adapter checks exact displayed catalog names, enabled state and explicit Identity $in list-entry counts against a separate uncached GrowthBook read in the producer. A source version change, missing version, unsupported answer, read failure or missing check stays unknown. The fixed renderer binds the claims to the text actually sent; a later independent read never becomes a backfilled historical pass. Only bounded counts/version labels enter analytics; source records, names, identities, replies and credentials stay in the producer. Afterspan validates and aggregates producer source-check reports; it does not independently fetch or certify GrowthBook. The metric does not assess full feature eligibility, completeness of the requested answer, human reading, usefulness or causality.


Start with `{}` for a review. Reuse its exact returned `query` and add `request` for the trace. Queries echo start/end/as_of and a definition fingerprint. This fixes the observation/receipt window, not an immutable backup: retention and erasure may remove records. At most 200,000 diagnostic observations are scanned; oversized requests fail without partial results. Older request chains beyond the selected limit are counted explicitly. Ordinary raw records are never returned by these routes. Producers whose authoritative planner inputs/plan are not covered report `comparison_scope_unavailable`; the review counts and explains that gap without substituting another planner or timing. The browser link retains the expected audit environment.

After signing into **your** deployment, set `NATIVE_ANALYTICS_URL` and `AFTERSPAN_SESSION_FILE` to your private session receipt. Install `requirements-agent.txt` in the Python environment used by the CLI. Then:

```sh
.venv/bin/python cli/native_analytics.py connect-audit --project PROJECT_ID --days 7 --output /private/audit.json
# The result prints a nonsecret Python command and launcher arguments.
# Register those exact paths; the key stays in the mode-600 receipt:
codex mcp add afterspan_audit -- /absolute/.venv/bin/python /absolute/agent/run_audit.py /private/audit.json
```

Ask a fresh Codex client: “Review up to 60 recent Slack bot requests. Discover meanings first, show disagreements, missing confirmation/shadow evidence, environment mistakes, unsafe reports and latency. Follow one request with the exact review query. State sample limits.” The audit-only key cannot use general queries, taxonomy, raw records or writes. It expires; list/revoke it with `read-keys` / `revoke-read-key`. The launcher rejects a world/group-readable receipt. Public `/.well-known/afterspan` describes discovery, scopes and supported local stdio transport. No operator credential is required and no remote OAuth MCP is provided.

Two fresh ephemeral Codex CLI clients were exercised locally with only this bridge, no prior conversation, no user MCP config and an empty working folder. Both discovered meanings, reviewed requests and traced one using exactly these three tools, with no shell/file/web calls. One synthetic case had 183 events, 8 chains and 3/4 agreement; the other had 183 events, 2 write chains and zero eligible comparisons. This proves the scoped local CLI workflow, not Desktop automatic registration, a production integration, parsing correctness or release readiness.

Producers must emit the explicit `properties.afterspan_audit` contract in OpenAPI. Ordinary historical fields are **not** automatically joined. Use fresh random UUIDv4 request/attempt/release refs, explicit strict booleans, bounded diagnostic codes and measured `duration_ms`. Missing/null requirements mean unknown; only false means not required. Never use redacted keys, message hashes, Slack IDs or customer IDs for correlation. Leave general text/reason redaction intact.

A comparable read requires one unambiguous parser/planner/shadow observation in the latest attempt, the expected environment, one release, matching `canonical-plan-v1` semantics and context digests, plus explicit `independent_proposal: true` on the shadow. Digests are project-keyed HMAC-SHA256 of the approved canonical inputs/decision, never raw content hashes; this server cannot verify that the producer implemented that canonicalization honestly. Digests are compared server-side and never exported. Agreement does not prove correctness: two proposals can be wrong together. Producer success does not prove a business change happened. Readiness, semantic correctness and business safety remain unestablished without independent evidence.

Editors may retain a historical sanitized PostHog aggregate with `PUT /api/audit/evidence/UUID`, CLI `attach-audit-evidence`, or workspace MCP `attach_audit_evidence`. A stable UUID makes identical retries idempotent; a changed body returns 409. Supply the plain source project URL, exact window, schema, counts and exact query SHA-256. The receipt is an **agent-imported attestation**, not an independently fetched source result. It preserves older evidence separately; it does not add to the native denominator or silently reset a quality gate. Source receipt cutoff and release coverage are explicitly unavailable. No raw rows/text/private identifiers are accepted.

## Run the local preview

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-agent.txt httpx
.venv/bin/python scripts/dev.py
```

Open `http://127.0.0.1:8769/` for the landing page, `/brand` for the identity system, `/app` for the private workspace, and `/docs` for the API reference. The preview binds to loopback and creates its own private credentials in `data/dev-access.json`. This file and local databases are ignored by Git; never publish them. Sign-in and invite emails are written to private mode-600 previews in `data/mail/`. This is an explicit loopback-only mail mode; it sends no real email. Open the link from your local preview to verify the account, then create a workspace. No production data or credentials are required. The old shared dashboard login is disabled by default.

The landing page's Shelf illustration uses the aggregate snapshot inspected on 17 October 2026. It is labelled, static, and does not call the production collector. The local workspace is a real app, not that illustration. Browser tests and the MCP smoke check use separately labelled local integration traffic.

## Self-serve workspaces and roles

A new customer opens `/app`, requests a 15-minute one-time email link, confirms the displayed account, and creates a workspace. Merely requesting a link creates no account. Email ownership is established only when its one-use link is consumed. No shared dashboard password grants customer access.

Each workspace contains projects and memberships. One account can belong to several workspaces. Project handles are unique within a workspace; separate customers can both use `my-app`. The returned project `id` is opaque and globally unique; use it in collection/configuration and API queries. Always pass the selected `workspace` when creating a project.

| Role | Analytics, events, people and journeys | Projects, setup, charts and tags | Invites, roles, access keys |
|---|---|---|---|
| Reader | Read | No changes | No grants |
| Editor | Read | Create/configure | No grants or rotation |
| Owner | Read | Create/configure | Manage |

Readers are workspace members with access to retained raw records. **Aggregate project read keys are narrower** and cannot read raw events, people or journey timelines.

An owner opens **Workspace & team**, enters an email and role, and queues an invite. The invitee must sign in with that exact address and explicitly accept. Invites last seven days and are single use. Resending rotates the link; revocation prevents acceptance. The UI distinguishes a queued message, local preview, and mail-server receipt. Provider acceptance does not prove inbox delivery. At least one owner must remain; concurrent role changes cannot remove every owner.

Removing a member removes their workspace agent keys and project read keys issued in that workspace. Demotion caps their workspace agent keys at the lower current member role. Pending invitations issued by an owner are revoked when that owner is removed or demoted. Normal logout revokes the current browser session; **Sign out everywhere** revokes all that account's browser sessions, workspace keys and project read keys. Account recovery is another verified email sign-in. No password reset or impersonation endpoint exists.

Producer keys are project service credentials. They keep running apps working after a member leaves. An owner must rotate producer keys if the departed member may retain them. Legacy/operator-issued project read keys have no personal issuer and also require explicit owner revocation.

## Email delivery for a hosted installation

Set `ANALYTICS_PUBLIC_URL` to the exact trusted HTTPS origin and `COOKIE_SECURE=true`. For AgentMail HTTPS delivery, set `AFTERSPAN_MAIL_MODE=agentmail`, `AFTERSPAN_MAIL_FROM` to your AgentMail inbox address, and `AFTERSPAN_AGENTMAIL_API_KEY` in the server secret store. Limit the key to that inbox with Inbox Read (identity verification) and Message Send; message reading and administrative access are unnecessary. Requests use the fixed AgentMail HTTPS endpoint and the queued mail ID as their idempotency key. A valid provider message receipt is required before the queued link body is discarded. Provider deduplication has its own retention window; this is not an indefinite exactly-once guarantee. See [AgentMail sending](https://docs.agentmail.to/api-reference/inboxes/messages/send) and [idempotency](https://docs.agentmail.to/idempotency).

SMTP remains available with `AFTERSPAN_MAIL_MODE=smtp`, `AFTERSPAN_SMTP_HOST`, `AFTERSPAN_SMTP_PORT` (587 with STARTTLS or 465 with TLS), `AFTERSPAN_MAIL_FROM`, and, if required, `AFTERSPAN_SMTP_USER` / `AFTERSPAN_SMTP_PASSWORD`. No plaintext SMTP option is provided. Railway Free/Trial/Hobby blocks SMTP; use HTTPS delivery on those plans. Verify your sender and actual inbox delivery before inviting real customers. Do not use local mail mode on a public service.

Email jobs are committed with their associated token/invite, survive a process restart, and retry up to five times with bounded backoff. Delivered or expired jobs discard their link body. Failed invite delivery can be retried with **Resend invite**, which creates a fresh link. Login links expire in 15 minutes; a fresh sign-in request is the recovery path. Mail transport failures do not log message bodies, addresses or credentials. The queue is bounded at 200 jobs. Sign-in requests are bounded per email, peer IP and service in a 15-minute window. Deploy behind correctly configured trusted proxy handling; do not trust arbitrary forwarded headers.

The development script sets `AFTERSPAN_MAIL_MODE=local` only with a loopback origin and insecure development cookie override. It saves private `.eml` previews rather than sending mail. SMTP and AgentMail are covered with transport/failure tests; actual provider delivery must be verified after configuration.

## Connect a coding agent

Choose the smallest connection needed:

- **Analytics only:** an expiring project aggregate read key, `AFTERSPAN_READ_KEY`, and `agent/server.py`. Twelve stdio tools: `overview`, `query_metric`, `journey_summary`, `setup_status`, `saved_charts`, `saved_funnels`, `saved_segments`, `analyze_funnel`, `analyze_segment`, `event_catalog`, `retention_report`, `saved_retention`. No write tools or operator fallback.
- **Request audit only:** an audit-purpose read receipt and `agent/run_audit.py`, exposing six scoped tools for catalog, reviews, traces, evaluations and onboarding. Use this when an agent must not see arbitrary event/property names or raw records.
- **Setup or team operations:** an owner creates a workspace role key in **Workspace & team**, or with `create-agent-key`. Configure `AFTERSPAN_WORKSPACE_KEY` and `AFTERSPAN_WORKSPACE`. Use editor for project setup, owner only for explicitly authorized team management. Keys expire in 1–90 days and are stored hashed. The effective role never exceeds the issuer's current membership. A workspace key cannot sign in as an account, create workspaces, accept invites, or issue further workspace keys. An owner-scoped workspace key can issue project service/read credentials through the API or CLI; store their receipts privately.

The workspace bridge exposes 42 tools for scoped team operations, optional billing, product definitions, explicit meanings, audit attestations and product loops. It uses only the selected workspace key and has no operator fallback. Inviting sends email and requires the human's specific recipient and role authorization. Role changes and removals likewise require a specific human request. The fixed-project loop bridge exposes 16 tools for repo discovery, tracking actions and outcome receipts. Owner credential connection, goal adoption and policy grants stay in the private CLI or owner UI. Tool annotations identify writes; the service enforces roles regardless of agent instructions.

Provisioning is available through the same scoped API and JSON CLI. Secret-producing CLI commands require a mode-600 private receipt file rather than printing keys:

```sh
# Request an email link. Complete it from a private {"token":"..."} file.
python cli/native_analytics.py login --email you@example.com
python cli/native_analytics.py complete-login --file /private/login.json --output /private/session.json
# Set AFTERSPAN_SESSION_FILE=/private/session.json for account actions.
python cli/native_analytics.py create-workspace --name 'My studio'
python cli/native_analytics.py create-project --workspace WORKSPACE_ID --id my-app --name 'My app' --output /private/project.json
python cli/native_analytics.py create-agent-key --workspace WORKSPACE_ID --label Codex --role editor --days 30 --output /private/agent.json
# Supply AFTERSPAN_WORKSPACE_KEY from the private receipt through your secret store.
python cli/native_analytics.py members --workspace WORKSPACE_ID
python cli/native_analytics.py invite --workspace WORKSPACE_ID --file /private/invite.json
```

The CLI also supports owner-only `rename-workspace`, `workspaces`, `me`, `invites`, `accept-invite`, `resend-invite`, `revoke-invite`, `change-role`, `remove-member`, `agent-keys`, `revoke-agent-key`, `mail` and `audit`. Read-only project operations accept the aggregate read key. All four bridges use the official MCP Python v1 SDK over local stdio; hosted OAuth/remote MCP remains planned.

Queries accept fixed UTC start/end times and an as_of receipt cutoff. Echo the returned query to repeat the same window, including up to ten typed equality filters. Retention and authorized erasure may remove data; fingerprints identify a query and definition, not an immutable backup.

## Upgrading an existing instance

The additive migration preserves existing event/project IDs, collection keys, charts and data. Existing projects go into the separate `legacy` workspace. The first signup gets no ownership of them. Back up the volume before a hosted upgrade.

Use an email account for ongoing team access. After the real owner verifies an email account, an operator may run `assign-existing --user ACCOUNT_ID --confirm existing-projects` with the operator token. This one-time action assigns that verified account to the existing workspace as owner. Thereafter use team controls.

For password compatibility, the operator must explicitly set `AFTERSPAN_LEGACY_LOGIN=true`. Open `/app` and choose **Sign in to existing projects**. The existing owner password opens the shared dashboard for the `legacy` workspace. It can inspect and create projects there. The screen labels this an **Existing-owner session**. Team, billing, and new customer workspace controls require an email account. The password session cannot access customer workspaces or perform global maintenance.

The CLI uses the same compatibility route. Save a JSON object with a `password` field in a private file with mode `600`. Do not pass the password as an argument. Then run:

```sh
python cli/native_analytics.py login-existing --file /private/existing-password.json --output /private/existing-session.json
export AFTERSPAN_SESSION_FILE=/private/existing-session.json
python cli/native_analytics.py projects --workspace legacy
python cli/native_analytics.py connect-audit --project PROJECT_ID --days 7 --output /private/audit.json
```

Both session completion commands write private session receipts. `complete-login` writes `cookie_name: as_session`; `login-existing` writes `cookie_name: na_session`. Older receipts without `cookie_name` keep the account default. The CLI accepts only these two cookie names. An explicit session file takes precedence over inherited read, workspace, or operator credentials. Producer `send` and `identify` commands still use their separate write key, and `assign-existing` still requires the operator token.

Give the downstream agent only the audit receipt and its returned launcher command. It contains a project-scoped audit key, with no owner session or password. Existing-owner audit keys expire or can be revoked with `revoke-read-key`; password logout and disabling legacy login do not revoke them. Revoke those keys separately when retiring compatibility access. `ANALYTICS_ADMIN_TOKEN` remains a broad operator credential. Never give it to a customer or agent.

## Link a task across retries

Open **Journeys** in the workspace. Events may include optional `journey` metadata:

```json
{"events":[{"id":"save-attempt-1-start","name":"save.started","distinct_id":"opaque-workspace",
"journey":{"id":"stable-task-id","workflow":"save","environment":"production",
"subject_kind":"workspace","attempt_id":"unique-attempt-1","attempt_number":1,"state":"started","release":"v1"}}]}
```

Send a separate stable event ID for each observation. Keep the journey ID across retries; increment attempt_number and change attempt_id for each business attempt. Delivery retries resend the same event ID and do not create a new business attempt. The highest recorded attempt number determines the latest outcome. A failed attempt followed by a higher successful attempt is one recovered journey, already included in succeeded. A higher started attempt remains in progress. Contradictory terminal reports stay unknown. Old unlinked events remain unlinked. These are producer reports, not independent proof of answer quality or business success.

Python: `collector.track_journey(name, journey, distinct_id=None, properties=None, attributes=None)`.
TypeScript: `client.trackJourney(name, journey, distinctId?, properties?, attributes?)`.
Use explicit environment and subject_kind. Anonymous journeys have no distinct_id; a workspace ID never becomes a person count. The server rejects reassigned subjects, reused attempt slots and changed releases within an attempt. Erasure also tombstones linked journeys to prevent stale anonymous replay.

`POST /api/journeys?project=...` returns aggregates and a fixed query. `/api/journeys/list` returns the owner's paged attempt timeline; aggregate read keys cannot use it. Results cover observed tasks with activity in the window and retained history through its end. Missing starts and attempt numbers remain visible. No adoption or eligible-population denominator is inferred.

## Verify a connection

Open **Connect & verify**, choose an environment, create a check, and send its exact event through your existing producer. The check lasts 15 minutes and resumes after a page reload. A matching receipt proves collection into that project/environment only. Check events are excluded from default product and journey totals. The next steps show separately whether a complete real journey and retry recovery were recorded in the last seven days.

CLI: `check-setup --project my-app --environment local` creates a check with owner access; `send --file check.json` uses producer access; `setup --project my-app --environment local` reads progress. Wrap the returned event in an events array before sending. No customer action is retried by this flow.

## Current engine

FastAPI, SQLite on one persistent volume, plain HTML/CSS/JS and SVG charts. No hosted chart service. The existing service documentation follows, with deployment still a separate action.

## What ships

- Each project gets its own write-only ingestion key inside a customer workspace. Customer sessions and delegated roles govern reads and configuration.
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
4. Configure email delivery as above. Open `/app`, verify the email sign-in link, create a workspace and project, then verify a real collection receipt.
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

Customer operations use a revocable account session or workspace role key. All project routes check workspace membership and the required role. The project list, overview, query, journey summary, setup and chart-definition reads also accept scoped aggregate read keys. The operator token is separate and broad.
Cookie-authenticated writes require `X-Native-CSRF: 1` and same origin. Keys are stored hashed.
Account sessions expire after 12 hours and are hashed in persistent storage. Login-link requests are rate limited in persistent storage.

- `GET/POST /api/projects`: list or create (`id`, `name`); creation returns write key **once**.
- `GET /api/overview?project=...&days=7`: defaults, pipeline report and event names.
- `POST /api/query?project=...`: `{days,start,end,as_of,event,distinct_id,property,value,filters,metric,measure,breakdown,interval}`.
  All optional; default metric `count`. `measure` is required for numeric metrics. Fixed columns only,
  top-level property names, bound SQL parameters. Maximum 200,000 matching events/query.
- `GET /api/events?project=...`: days, event, distinct_id, property/value, status, ip, operation.
  At most 100 records/page, `next_cursor.before` and `before_id` continue even when timestamps tie.
- `GET /api/people?project=...&search=...&offset=0`: identity/attribute search, 100/page.
- `PATCH /api/people?project=...`: same shape as identify, for owner-edited traits/tags.
- `GET/POST /api/charts?project=...`: save `{title,query}`. Delete via
  `DELETE /api/charts/{chart_id}?project=...`.
- `POST /api/maintenance`: immediate retention cleanup. Hourly cleanup is automatic.

Relative day windows are frozen once per query. Explicit start/end must be timezone-aware, span at most 90 days, and use inclusive start/exclusive end. as_of excludes later received events. Graph buckets are UTC; UI timestamps use viewer local time.
`count` counts events, `users` counts non-null distinct IDs; anonymous requests do not become synthetic people.
Numeric metrics ignore absent/non-numeric/bool properties. Percentile uses nearest-rank p95.

Default charts exclude collector heartbeats (`analytics.pipeline`) and setup checks (`afterspan.setup.checked`) so they do not inflate product usage. Select either event explicitly to query it.

## Limits and operational honesty

This is a small, single-process tool; one volume, one replica. It is not a warehouse, security log
archive, session replay tool, experiment engine, or unlimited-scale telemetry service.
Events start at integration time; it cannot recover old IPs. Requests blocked at Railway's edge
never reach ASGI and are not logged. Disconnect/process death before local enqueue can lose a request.
Delivery retries are idempotent; the local queue defaults to 100,000 events, reports drops at capacity,
and should be monitored. A full/failed disk can lose local enqueue; that is logged and never stops
business requests. Ingestion failure retains the queue, it does not retry a business action.
Unauthorized callers are shown as unauthenticated. Forwarded headers and user agents are untrusted
metadata; network IPs may belong to NAT/proxies. This service makes no bank/customer/device outcome claim.

Retention limits disk growth over time, but capacity depends on traffic. Railway compute and storage
still incur normal infrastructure costs. Usage metering, bounded draft plans and a hosted Stripe adapter exist locally; real Stripe sandbox receipts exist, while live payments and a production billing rollout are unverified. SSO, MFA, hosted OAuth MCP and multi-replica storage remain absent. Paid plans do not change the single-process storage architecture.
Volume backups must be configured separately; no backup schedule is claimed by this deployment.

## Optional hosted billing

This section applies only when an operator explicitly sets `AFTERSPAN_EDITION=hosted`. Self-hosted instances show **Usage** with no commercial quotas and cannot create checkout links. Hosted installations can retain the existing optional billing adapter and draft catalog. Switching editions does not cancel any existing Stripe subscription; inspect and manage provider subscriptions separately before changing a billed installation. The default local command uses self-hosted mode; use `scripts/dev.py --billing local` only to exercise the separate hosted simulation.

Open **Usage** in a hosted workspace. Readers and editors see counters and plan limits; only owners see subscription state, checkout and invoices. Workspace owner keys can create review links; payment remains a human action in the hosted provider. There is no endpoint or MCP tool that charges a saved card. The local simulation route is unavailable outside explicit loopback mode.

| Draft plan | USD / month, before tax | Events / UTC month | Validated payload | Projects | Seats |
|---|---:|---:|---:|---:|---:|
| Free | $0 | 10,000 | 50 MB | 2 | 3 |
| Builder | $19 | 100,000 | 1 GB | 10 | 10 |
| Team | $79 | 500,000 | 5 GB | 50 | 25 |

These are editable draft prices and limits, version `2026-10-17-v1`. No automatic overage charges. Accepted events count once by project/event ID. A new failed business attempt is still an event; a repeated delivery of the same ID is not. UTC receipt time chooses the usage month, independent of subscription renewal. Upgrade does not reset usage. Quota rejection is HTTP 429 with Retry-After; the whole new batch rolls back. The collectors retain the analytics queue and do not retry the business action. At capacity they can drop queued telemetry as documented above.

Payload means compact UTF-8 JSON after schema validation, not wire bytes/storage. Collector heartbeats and setup checks use payload but not the commercial event allowance, with a daily safety cap of 2,000 per existing project. Both collection paths enforce the workspace ledger. HMAC delivery receipts survive raw retention/erasure for 400 days and contain no raw identity or content. Retained events at migration establish deduplication without backdated counters. Monthly/daily numeric usage stays for account history. History is compared to the current plan, not a reconstructed old entitlement. The existing operator workspace keeps its old unbilled 100-project/100-seat allowance until it enters a paid subscription.

Pending active invitations reserve seats. Resend/accept does not count the same seat twice. Expiry/revocation releases it. Downgrade keeps members, projects and data; additions and new collection can stop when above the new limits. This version meters events/payload and bounds project/seats; it does not separately meter identify calls, query compute, replay storage, surveys or AI token costs.

CLI commands all take `--workspace ID`: `usage [--month YYYY-MM]`, `plans`, `billing`, `billing-invoices`, `billing-quote --plan builder`, `billing-checkout --plan builder [--request-id UUID]`, `billing-portal [--request-id UUID]`, `billing-refresh`, `billing-close-checkout`, `billing-downgrade --confirm-plan builder --request-id UUID`. Portal supports `--action manage|cancel|change_plan|payment_method`. The workspace MCP adds `usage`, `plans`, `billing_status`, `billing_invoices`, `billing_quote`, `billing_checkout`, `billing_portal`, `billing_refresh`, `billing_close_checkout`, `billing_downgrade`. Downgrade requires a stable UUID and `confirm_plan=builder`, is owner-only, schedules a cheaper renewal, and never charges or credits now. Checkout/portal tools require a stable UUID request ID; server-side pending checkout recovery also prevents duplicate sessions from routine retries. Private checkout/portal URLs belong only with the owner, never a public report.

### Local test provider

`scripts/dev.py` explicitly selects `AFTERSPAN_BILLING_MODE=local` and generates a private local webhook signing secret. An owner opens a checkout review link and uses the clearly marked local test page to simulate failed/successful payment, renewal failure, scheduled cancellation and term end. It writes no card data, makes no Stripe request, and moves no funds. Simulation uses the signed webhook queue and canonical provider reconciliation path. It is **not Stripe sandbox verification**.

### Repeat the isolated real Stripe test setup

The 17 October sandbox expires **24 October 2026** unless its owner claims it. No key or claim URL is included in public artifacts. Claiming requires the person to complete Stripe account setup; do not accept account terms for them. Anonymous sandbox keys do not grant test-clock permissions. A claimed test account/full test key is required for real time-advanced renewal tests.

From this source checkout, install the official CLI into ignored data, then create an isolated profile. Use a synthetic address for an unclaimed fixture, not a customer's address. Capture CLI output privately, because it contains credentials and an account-claim link:

```sh
mkdir -p data/stripe-test
npm install --prefix data/stripe-tools @stripe/cli
umask 077
data/stripe-tools/node_modules/.bin/stripe --config data/stripe-test/cli.toml sandbox create --email builder@example.test --non-interactive > data/stripe-test/sandbox-create.log
.venv/bin/python scripts/stripe_test.py --cli-config data/stripe-test/cli.toml --output data/stripe-test/provider.json
.venv/bin/python scripts/stripe_listen.py --stripe-cli data/stripe-tools/node_modules/.bin/stripe --cli-config data/stripe-test/cli.toml --provider-config data/stripe-test/provider.json --port 8770
```

Keep the last command running. It writes the listener's actual signing secret into the private provider config and forwards only billing events. Then start a second terminal:

```sh
.venv/bin/python scripts/dev.py --billing stripe-test --stripe-config data/stripe-test/provider.json --port 8770
```

Open `http://127.0.0.1:8770/pricing`. Choose Builder, verify the private local email preview, name a workspace and review checkout. Use only Stripe's documented synthetic test cards. The preview uses a separate database/mail/credential folder from local simulation. Both development ports use the same host cookie name, so signing into one can replace the other's browser cookie; this is a loopback demo detail, not cross-customer access. Real inbox delivery and public hosting are not proved by local mail previews.

The setup script is idempotent and stores product/price/portal IDs privately. It only accepts a test key. The listener never prints its signing secret. Restart the test preview after restarting the listener so the new signing secret is loaded. `dev.py` always sets live billing off. Expired sandbox credentials need a new isolated profile/database or a deliberate migration; do not swap a different account's keys into existing customer mappings.

To retain the sandbox, the human can run `stripe --config data/stripe-test/cli.toml sandbox claim` using the installed CLI and complete the account flow themselves. This opens a private claim link; keep it out of reports, Git and chat.

### Signup and billing returns

Public `/pricing` reads the same versioned `/api/plans` catalog as the workspace. A chosen plan is carried in the one-time email link and through new-workspace creation, then opens a review. Neither signup nor a forged return flag grants paid access. Checkout and portal return to the selected workspace; Afterspan reconciles canonical provider state and clears the return marker. Invoices load separately so provider delays do not hide usage. Currency receipts retain their cents.

Portal actions can open general settings, change-plan selection, payment-method updates or period-end cancellation. Only an upgrade to Team is selectable in the portal. Team → Builder uses an explicit review and a two-phase subscription schedule: keep Team through this paid term, start Builder at the next term, then continue Builder. The server rejects unpaid/inactive, cancelling, discounted, pending-update or unfamiliar scheduled subscriptions rather than overwriting their billing. A provider timeout retains a recoverable operation; retry the same request/plan. This version does not provide an app button to remove an already scheduled downgrade; an operator must inspect the provider schedule before any change.

### Configure an actual Stripe test deployment

The default mode is `disabled`; Free collection still works. An operator must configure a trusted public HTTPS origin and secure cookies before hosted operation. Do not copy local provider/customer rows into another mode; profiles are environment-bound and require an explicit migration.

1. For hosted billing, set `AFTERSPAN_EDITION=hosted` and `AFTERSPAN_BILLING_MODE=stripe_test` and a private `AFTERSPAN_STRIPE_SECRET_KEY` beginning `sk_test_`, `rk_test_` or `rkcs_test_` (temporary sandbox key).
2. Create one licensed monthly USD Price for Builder ($19) and Team ($79). Set their distinct IDs as `AFTERSPAN_STRIPE_PRICE_BUILDER` / `AFTERSPAN_STRIPE_PRICE_TEAM`. The adapter checks mode, amount, currency and recurrence against the versioned catalog before checkout/portal creation. Do not silently change catalog prices for existing subscriptions.
3. Set `AFTERSPAN_STRIPE_PORTAL_CONFIGURATION` to a test configuration with only the Team product/price allowed for updates. Builder and Team are separate products, because the portal rejects two monthly prices under one product. Enable price-only updates, always-invoice upgrade proration, `decreasing_item_amount` scheduling, and period-end cancellation. Lower-tier changes happen through the explicit Afterspan downgrade review and a provider subscription schedule, never an immediate portal credit. The adapter rejects a broader policy. Enable only customer/payment/invoice portal features appropriate for the service. Set `AFTERSPAN_STRIPE_AUTOMATIC_TAX=true` only after Stripe Tax is set up; checkout collects billing address and shows the final amount before human confirmation.
4. Register `POST /api/billing/webhook` with Stripe for checkout completion/async payment results, subscription created/updated/deleted, invoice paid/payment_failed/voided/marked_uncollectible. Configure its private `AFTERSPAN_STRIPE_WEBHOOK_SECRET` (`whsec_`). The official SDK verifies the unchanged request body and timestamp. Event IDs are durably deduplicated and queued; the background worker retrieves current canonical subscription/invoice state, so stale/duplicate notifications cannot directly write a paid entitlement.
5. Exercise actual test checkout (including provider-required authentication), invoice receipt, upgrades, scheduled downgrade, failed renewal and cancellation in the intended hosted deployment. Real sandbox checkout, card decline, paid invoice, Team upgrade, period-end cancellation, signed delivery, and actual terminal cancellation were exercised on 17 October. Unclaimed sandbox keys block Stripe test clocks; time-advanced renewal/expiry is still unverified.

Paid access requires a matching paid invoice covering the recognized subscription period. A checkout return URL or subscription label alone is insufficient. A failed refresh preserves the last confirmed paid term; a failed renewal does not extend it. At confirmed cancellation or paid-term expiry the workspace falls back to Free. Owner Refresh reconciles missed receipts; failed queue jobs retry with bounded backoff and surface pending status. Keep monitoring these failures; no external alert integration exists yet.

Live mode additionally requires a matching `sk_live_` key and `AFTERSPAN_ENABLE_LIVE_BILLING=true`, with live prices/webhook/portal in an explicitly authorized production release. Merely changing the environment is not a tested release. Backups/restore, verified email delivery, real provider checks, commercial policy/tax review and hosted signup tests remain launch work.

## Tests

```sh
python -m unittest discover -s tests -v
node --check static/app.js
node --check static/accounts.js
node --check static/journeys.js
node --experimental-strip-types --test sdk/typescript/native-analytics.test.ts
```

Tests cover project isolation, key/read separation, cookie/CSRF/login limits, redaction, input bounds,
deduplication, queries, traits/tags, charts, retention, cursor ties, outbox recovery and safe ASGI capture.

## Agents and zero-to-first-event setup

Start with [AGENTS.md](AGENTS.md). The running service exposes `/llms.txt`, `/openapi.json`
and `/docs` so a coding agent can discover the real API instead of guessing it.

```sh
python scripts/setup.py --env-file .env
pip install -r requirements.txt
set -a; source .env; set +a
uvicorn app:app --host 127.0.0.1 --port 8080
# in another shell with the same env:
python cli/native_analytics.py health
python cli/native_analytics.py create-project --id my-app --name 'My app' --output my-app-key.json
```

The setup helper creates independent secrets in a mode-600 file and refuses to overwrite them.
The key receipt is also mode-600 and is never printed. Set `NATIVE_ANALYTICS_WRITE_KEY` from
that private receipt, send an event, then query it using a workspace or project read key:

```sh
python cli/native_analytics.py send --file examples/events.json
python cli/native_analytics.py overview --project my-app
python cli/native_analytics.py events --project my-app
python cli/native_analytics.py query --project my-app --file examples/query.json
```

For Railway: authenticate the Railway CLI, then run
`python scripts/setup.py --railway --workspace YOUR_WORKSPACE_ID --name native-analytics`.
This provisions billable resources only when explicitly invoked, creates persistent storage,
sets secrets over stdin, and submits a deployment. It reports the origin and project ID;
verify `/healthz` and a real event receipt before claiming success. Partial setup keeps your
keys in the private file so you can inspect/recover the cloud project without losing access.

## TypeScript / Next.js / serverless

Copy `sdk/typescript/native-analytics.ts` into your server code. It uses native `fetch`
and `crypto`, no PostHog or client-side secret. Instantiate `createAnalytics({url,writeKey,source})`.
Use `after(() => client.track('save.completed', opaqueUserId, {kind:'article'}))` on Next.js,
or your platform's `waitUntil`. `identify`, `flush`, and `status` are also available.
Stable IDs survive its two bounded delivery attempts. Its 500-event in-memory queue is
best-effort and does not survive serverless termination; use a durable application ledger
when replay is required. Never put the key behind `NEXT_PUBLIC_` or in a desktop binary.

Numeric AI token usage is supported; raw prompts/messages/content and credential fields are
redacted by the SDK. Existing analytics event names can be retained when moving from another
provider. The legacy `$ai_generation` name becomes `ai.generation`, with queryable `ai_*`
properties. Cookie-free application-side event relays must validate/authenticate inputs and
rate-limit them; the collector is intentionally not a browser SDK with a public project key.

Owner key rotation is available at `POST /api/projects/{id}/rotate-key` and through the CLI.
It revokes the previous key immediately. No events or dashboards are deleted.

Licensed under MIT. Fork and deploy your own instance; do not send your users' data to someone
else's installation. See the CI workflow for executable service, Python collector and TypeScript SDK checks.

## Optional native-app relay and erasure

A distributed app must never embed a producer key. Owners may enable a constrained relay with
`POST /api/projects/{project}/relay` and `{enabled:true,events:["app.opened","scan.completed"]}`.
`POST /v1/relay/{project}` then accepts only the typed consent=true payload in OpenAPI: UUID event/install
IDs, an allowed event, app version, enumerated feature/outcome, count and duration. Extra fields,
free text, paths and process names are rejected. Rate: 60 requests/IP/project/minute. Relays are disabled
unless explicitly configured. Records are client-reported, not authenticated proof of usage.
They cannot read data and do not grant access to producer endpoints. The owner can disable a relay.

`POST /v1/erase {distinct_id}` uses that project's producer key to remove the identity and its events.
A tombstone rejects queued/stale events for that exact identity. Producers should derive an opaque
identity from user/workspace generation, capture it before erasing, and use a new generation for new
activity. The SDK erase operation resolves only after a confirming receipt; failures must be reported
as unconfirmed, not silently represented as successful deletion. This service cannot delete data it
never received an authorized erase request for. Project-key rotation also changes key-derived HMAC
identities; retain the old derivation secret if old users must be erased after rotation.

Authentication design references: [OWASP one-use token guidance](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html) and [Python SMTP TLS transport](https://docs.python.org/3/library/smtplib.html).

For a browser site, set exact HTTPS `origins` on its owner-managed relay configuration. Only those origins receive CORS permission for fixed-schema, credential-free POSTs. The relay supports a `web` feature and optional `nav`/`hero`/`install` placement. Keep the same explicit opt-in and privacy signal handling in your site. No write or owner key belongs in browser code.
