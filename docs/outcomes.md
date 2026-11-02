# Versioned workflow outcomes

This capability is on the outcome-platform branch and has not been released to production.

## Owner flow

Open Outcomes, save a workflow goal, then send real task observations. A goal defines the workflow and environment, deadline, exact required check names/versions/methods/phases, and completion/delivery/usefulness milestones. The simple form requires a source-evidence reference and completion plus delivery; the API supports other explicit criteria. No goal executes repository or evaluator code on the service.

The outcome engine applies criteria to producer-reported evidence. A satisfied task is not a claim that Afterspan fetched an independent source, that a human read a reply, or that a release caused an improvement. A source, model, code or human check retains its method. Required evidence resolves to a matching retained workflow-evidence-v1 receipt. Resolution validates correlation and recorded facts; it is not an independent Afterspan source fetch.

## Versions and APIs

`PUT /api/outcomes/goals/{UUID}?project=PROJECT&revision=0` creates version one using editor access. Updates name the last version; they append an immutable version rather than rewriting past definitions. Retrying the same UUID/revision and exact definition returns the same version. Changed definitions under a stale revision return 409. At most 100 goals and 1,000 versions per goal are stored for a project.

Audit-purpose readers use `GET /api/outcomes/goals` and `GET /api/outcomes/goals/{UUID}?version=N`. `POST /api/outcomes/evaluate` accepts `goal_id`, `version`, the standard entry window, optional exact `release_sha`, and bounded `offset`/`limit`. API, primary screen, CLI `outcome-evaluate --project PROJECT --file QUERY.json`, and remote/local audit MCP `evaluate_outcomes(query)` share the engine. Remote MCP `outcome_goals()` discovers current goal versions. Remote `save_outcome_goal(id, revision, definition)` requires a separately approved `goals:write` scope and current editor role. Existing chart scopes do not grant goal writes. CLI `outcome-save`, `outcome-goal` and `outcome-goals` share these APIs; read access never grants goal edits.

## Task evidence

A task reuses its workflow `trace_id`. Every span supplies the same original `request_started_at_ms`; retries keep that clock and increase the business attempt number. Terminal spans may declare one `milestone`: completion, delivery or usefulness. A response can also declare delivered, withheld or clarification disposition. Those are observations, not implicit check passes.

Only the latest attempt's checks and milestones qualify. Required criteria come from the saved goal, not the producer's optional/required flags. Each required check must have one matching version/method/phase. Missing or contradictory checks stay unclassified. Before/after delivery phase must agree with the observed timestamps when delivery is observed. A check without its start cannot establish a reliable ordered evaluation.

## Denominators and evidence windows

One observed task is counted once across retries. Tasks wait outside the mature denominator until the original deadline. Mature tasks are satisfied, failed, withheld or unclassified; all four remain in the denominator. Known wrong or late required evidence fails. Missing evidence does not become a recorded failure or a success. A withheld response is not useful delivery.

The query window selects original task starts. Evidence may be followed through the deadline, bounded by the receipt cutoff. Entry window plus deadline must fit within 90 days. Frozen task-to-trace links preserve the actual follow-up window. Missing clocks cannot prove cohort membership, so the satisfied fraction stays unavailable. Invalid observations or uncertain release selection also prevent a clean fraction. Erasure, retention and late telemetry can change repeated reads; best-effort collection is not a complete task ledger.

The current implementation supports generic workflow traces. Existing Slack audit checks retain their separate historical contract; a shared outcome adapter for those observations remains part of release integration.

## Evidence receipts

Emit `evidence.observed` with only `properties.afterspan_evidence` and no identity, traits or journey payload. The `workflow-evidence-v1` contract binds receipt UUID, trace/attempt/span UUIDs, workflow, environment and optional release to the exact check name/version/result/method/phase. `evidence_digest` is the SHA-256 of the evaluated evidence or artifact, not raw source content. No prompt, body or explanation field is accepted. The producer remains responsible for the source evaluation; the digest does not itself prove correctness.

Receipts use existing immutable, idempotent event storage and retention. Required evidence qualifies only when a receipt resolves in the same project/environment and receipt cutoff, matches all bindings, and was issued no later than the check. A pointer alone, another task's receipt, changed check version, contradictory receipt or missing/expired record leaves required evidence unclassified. Diagnostic reads expose resolution status but neither raw receipt UUID nor digest. Replaying an event cannot overwrite the original; conflicting receipts under a new event ID cannot pick the favorable result.
