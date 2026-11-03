# Versioned workflow outcomes

This capability is included in the early outcome-platform release. Implementation, deployment and live acceptance are separate; see the [release evidence and open gates](outcome-platform-release.md).

## Owner flow

Open Outcomes, save a workflow goal, then send real task observations. A goal defines the workflow and environment, deadline, exact required check names/versions/methods/phases, and completion/delivery/usefulness milestones. The simple form requires a source-evidence reference and completion plus delivery; the API supports other explicit criteria. No goal executes repository or evaluator code on the service.

The outcome engine applies criteria to producer-reported evidence. A satisfied task is not a claim that Afterspan fetched an independent source, that a human read a reply, or that a release caused an improvement. A source, model, code or human check retains its method. Required native evidence resolves to a matching retained workflow-evidence-v1 receipt. Supported integration adapters resolve their own explicit proof bindings. Resolution validates correlation and recorded facts; it is not an independent Afterspan source fetch.

## Versions and APIs

`PUT /api/outcomes/goals/{UUID}?project=PROJECT&revision=0` creates version one using editor access. Updates name the last version; they append an immutable version rather than rewriting past definitions. Retrying the same UUID/revision and exact definition returns the same version. Changed definitions under a stale revision return 409. At most 100 goals and 1,000 versions per goal are stored for a project.

Audit-purpose readers use `GET /api/outcomes/goals` and `GET /api/outcomes/goals/{UUID}?version=N`. `POST /api/outcomes/evaluate` accepts `goal_id`, `version`, the standard entry window, optional exact `release_sha`, and bounded `offset`/`limit`. API, primary screen, CLI `outcome-evaluate --project PROJECT --file QUERY.json`, and remote/local audit MCP `evaluate_outcomes(query)` share the engine. Remote MCP `outcome_goals()` discovers current goal versions. Remote `save_outcome_goal(id, revision, definition)` requires a separately approved `goals:write` scope and current editor role. Existing chart scopes do not grant goal writes. CLI `outcome-save`, `outcome-goal` and `outcome-goals` share these APIs; read access never grants goal edits.

## Task evidence

A task reuses its workflow `trace_id`. Every span supplies the same original `request_started_at_ms`; retries keep that clock and increase the business attempt number. Terminal spans may declare one `milestone`: completion, delivery or usefulness. A response can also declare delivered, withheld or clarification disposition. Those are observations, not implicit check passes.

Explicit check execution is diagnostic, separate from result and task outcome. A `not_run` check must stay unknown without a receipt; it cannot satisfy a required check. `completed` alone never qualifies a pass. Missing execution is not inferred from a successful span, a result label or an old verifier version. Receipt identities and existing goal versions remain unchanged.

Only the latest attempt's checks and milestones qualify. Required criteria come from the saved goal, not the producer's optional/required flags. Each required check must have one matching version/method/phase. Missing or contradictory checks stay unclassified. Before/after delivery phase must agree with the observed timestamps when delivery is observed. A check without its start cannot establish a reliable ordered evaluation.

## Denominators and evidence windows

One observed task is counted once across retries. Tasks wait outside the mature denominator until the original deadline. Mature tasks are satisfied, failed, withheld or unclassified; all four remain in the denominator. Known wrong or late required evidence fails. Missing evidence does not become a recorded failure or a success. A withheld response is not useful delivery.

The query window selects original task starts. Evidence may be followed through the deadline, bounded by the receipt cutoff. The actual entry and observed follow-up window must fit within 90 days. Frozen task-to-trace links preserve the actual follow-up window. Missing clocks cannot prove cohort membership, so the satisfied fraction stays unavailable. Invalid observations or uncertain release selection also prevent a clean fraction. Erasure, retention and late telemetry can change repeated reads; best-effort collection is not a complete task ledger.

## Supported integrations

Goals default to `contract: workflow-trace-v1` for any producer-declared workflow. Select `contract: slack-request-v1` for existing Slack audit requests; the same outcome engine then reads the existing stream without re-ingestion or a parallel trace. `GET /api/traces/catalog` lists supported integrations and check versions.

The Slack adapter maps read/write requests to `slack_read_request` / `slack_write_request`. Its source checks are `source_facts` v1 and `growthbook_catalog` v1/v2, method source, phase before_delivery. A check qualifies only on a real, complete quality operation with matching request, attempt, original clock, environment and release. The immutable quality observation must include a source-check-v1 digest binding. Historical counts, an unrelated pointer or a closed model span cannot substitute. The digest is a producer attestation over the compared facts, not an independent server-side source fetch or HMAC verification.

Final delivery requires an explicit reply operation bound to exactly one matching accepted Slack send. An interim message, dev-log output or multiple ambiguous sends cannot qualify. Unknown traffic, missing required reports, conflicting clocks/releases and parent cycles stay unknown. Original-clock deadlines and latest-attempt quality apply to both contracts. Tracker delivery is verified for two production validation requests; broader user coverage remains a release gate.

## Evidence receipts

Emit `evidence.observed` with only `properties.afterspan_evidence` and no identity, traits or journey payload. The `workflow-evidence-v1` contract binds receipt UUID, trace/attempt/span UUIDs, workflow, environment and optional release to the exact check name/version/result/method/phase. `evidence_digest` is the SHA-256 of the evaluated evidence or artifact, not raw source content. No prompt, body or explanation field is accepted. The producer remains responsible for the source evaluation; the digest does not itself prove correctness.

Receipts use existing immutable, idempotent event storage and retention. Required evidence qualifies only when a receipt resolves in the same project/environment and receipt cutoff, matches all bindings, and was issued no later than the check. A pointer alone, another task's receipt, changed check version, contradictory receipt or missing/expired record leaves required evidence unclassified. Diagnostic reads expose resolution status but neither raw receipt UUID nor digest. Replaying an event cannot overwrite the original; conflicting receipts under a new event ID cannot pick the favorable result.

## Define a goal in the workspace

Editors can use Outcomes → Save a goal to choose one to twenty named, versioned checks. For each check, choose its method, before/after-delivery timing and whether a scoped evidence receipt is required. Choose at least one completion, delivery or usefulness milestone. All selected criteria must qualify; unselected milestones are not silently required. The saved goal summary shows each check’s method, timing and receipt requirement. For example, a library answer can require a code citation check after delivery and completion only, while a source-backed Slack reply can require a check before delivery plus completion and delivery.

The defaults retain the stricter existing reply goal: evidence required, check before delivery, completion and delivery, user traffic and 25 mature tasks for a release comparison. New criteria do not change saved goals or instrument the producer. A producer-reported usefulness milestone is not independently measured usefulness. The API retains immutable versioning; this form creates new goals.

## Release measurement

Goals can select a traffic population and minimum mature comparison sample. Unknown labels never become normal users. Outcomes can join a pinned goal/dataset experiment to two stored verified deployments and frozen mature user-task windows. Reported model cost includes failed and retried mature tasks; missing cost stays unknown. See [release outcomes](release-outcomes.md) for exact bindings, access, limits and interfaces. Normal-use outcome improvement remains unproven.

## Partial answers and checker versions

A delivered response can be useful and still leave part of the request unfinished. Native workflow producers may declare response disposition `partial`; the outcome cannot satisfy the goal: it is `unclassified` with `partial_answer_not_completed` when no explicit failed check or missed deadline takes precedence, even if every declared factual check passes. It remains in the mature denominator. This is producer-reported incompleteness, not Afterspan judging the text.

The Slack adapter keeps `growthbook-catalog-v1` as `growthbook_catalog` version 1. `growthbook-catalog-v2` maps to version 2: the producer additionally binds the original supported list request, status filter, maximum/total intent and validated paginated coverage. V2 allows at most 15 displayed row checks plus one scope check. Missing scope contributes an unknown check; a source-checked partial response is not an overall pass. A version-1 observation cannot satisfy a version-2 goal, and existing goals are not silently changed. The same generic outcome engine supports other products' own versioned criteria.

Tracker's bounded catalog reader may inspect up to ten pages of 100 raw rows. Page counts, offsets, totals, continuation and duplicate identities must agree. Reaching that bound is partial coverage. Full source coverage is distinct from showing the full result list, and neither establishes feature usage or effective eligibility. Afterspan PR #28 and Tracker PR #191 are deployed. Three synthetic-labelled live Tracker requests verified an exact five-result list, a count backed by complete source coverage, and a capped full list explicitly delivered as partial. The version-two goal classified two as satisfied and one as unclassified; all three remained in the mature denominator. Scoped API, CLI and official stdio MCP reproduced the same frozen result. These are validation cases, not a normal-user success rate. Historical traces and version-one goals retain their original semantics.
