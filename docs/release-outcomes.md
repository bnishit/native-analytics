# Experiments, verified releases and observed outcomes

The shared comparison path is included in the early outcome-platform release. Local synthetic fixtures prove its bindings and accounting. A production-stored three-case comparison now also exercises real old/current Tracker source locally; production user improvement has not been established. Those retrospective runs have no user-goal binding and cannot qualify the already-deployed release.

## Owner flow

Save a user-task outcome goal. In Experiments, pin its immutable ID, version and definition hash to the reviewed dataset. Run baseline and candidate against those same case/check versions, before the candidate deployment. In Outcomes, **Verify an existing deployment** resolves a selected merged GitHub change against a configured Railway target. An owner reviews the complete source manifest and exact provider deployment, then adopts that evidence. **Compare verified deployments** joins both saved receipts to matching experiment runs and mature task windows.

The existing Goal loop retains its separate reviewed tracking-artifact policy. External release evidence verifies source/deployment identity; it does not attest independent code review, CI success, safe behavior or permission to merge/deploy. The comparison accepts either evidence kind and keeps its approval basis visible. A run declaring the branch head cannot stand in for the eventual deployed commit. Both experiments must precede the candidate's provider-reported deployment. Unsupported release chains stay unknown.

## Verify an existing deployment

An editor selects configured GitHub and Railway targets, a merged change number and environment. The branch defaults to main. Afterspan independently resolves the commits and checks the same repository, exact parent topology, matching PR-head/deployed trees, and every changed path, mode and blob identity. The paginated PR manifest must agree with the full tree difference. The supported envelope is 200 PR files, a complete repository tree of at most 10,000 entries and the existing one-MB provider response bound; excess, omissions and truncated trees cannot become a shortened successful proof. Source/PR patches are discarded; only identity metadata is saved. No code is executed or changed.

The Railway target is project/service/environment scoped. Its latest deployment must be successful and match the exact merged commit. This does not search older successful deployments or establish when a user first saw the code. Capture the baseline before a newer deployment replaces it. Single-parent and two-parent same-tree merges are supported; other lineages remain unavailable.

Preparation returns a pollable job, then an immutable proposal/hash. Only an owner account session can adopt that exact proposal. Adoption rereads the source and provider evidence, checks connection revisions and current owner access through the final transaction, and rejects a changed deployment ID or timestamp. A proposal cannot qualify as a final receipt. Repeated identical adoption returns the same stored receipt; a conflicting UUID/body fails. Source access can later be revoked without rewriting already stored historical evidence.

Generic release facts are independent of a goal. The experiment dataset and outcome comparison still pin the exact immutable goal version/hash. There is no invented repository goal or expanded tracking-artifact permission.

The baseline window starts after its verified deployment; its entire goal follow-up ends before the candidate deployment. The after window starts at the candidate deployment. Both windows need full follow-up through their frozen receipt cutoffs. The browser preserves exact provider timestamp precision at those boundaries. Provider status time is not proof of when each user first saw a commit.

## Bounded production verification

The deployed adapter prepared and adopted Tracker PR #192 through the real owner UI. Its complete 19-file manifest and exact production commit/deployment matched; account-scoped CLI receipt/catalog reads agreed. The recorded baseline is available for a later candidate comparison. This does not establish improved normal-user outcomes or exercise a new remote OAuth grant; see [release acceptance](outcome-platform-release.md).

The SDK release additionally verified both exact deployment receipts with matching predeployment baseline/candidate runs and a goal-bound dataset. The production UI/API comparison stays unknown when the unchanged 25-task minimum or collection evidence is unmet. Passing fixed synthetic cases does not substitute for missing real-user evidence.

## Population and cost

New goals explicitly choose user, synthetic, internal or all traffic. Old observations default to unknown. Producers classify trusted task entry points and propagate that classification; a label is not independently verified human identity. Only user-task goals qualify for normal-use release comparisons. Known synthetic/internal tasks are excluded, while unknown population labels prevent a clean fraction. Retry attempts retain the original task clock and classification.

The comparison returns `goal_definition` from the exact pinned ID/version/hash, alongside its binding. The same API, CLI and MCP result exposes that policy; it never substitutes the latest goal revision. The workspace shows mature tasks against this minimum, the remaining task count, and each source warning from the frozen collection report. Older responses without criteria do not assume a default minimum. More tasks alone do not clear missing evidence or prove usefulness.

The pinned goal sets the minimum mature task count. Waiting tasks, missing evidence, unclassified tasks, retention gaps or inadequate samples keep the comparison unknown. A sample threshold never grants readiness. Before/after differences are observational, not causal, and only describe retained labelled tasks. The comparison also reads collection health over each full follow-up window. Missing expectations, stale/gapped pulses, reported loss or unassigned project-wide backend rejections keep it unknown. Collection completeness remains **not established** even when the declared source reports pass these bounded checks; a pulse cannot prove that every task was instrumented.

Reported model cost uses exact decimal USD strings with up to 12 fractional places, or legacy integer millionths of USD. Both require provenance, and one span cannot supply both amounts. Totals use decimal arithmetic; per-satisfied-task ratios report rounding and positive values below display precision. This is provider-reported evidence, not a reconciled bill. Reported cost includes all observed model spans and retries in mature tasks, including failures and withheld answers. Waiting tasks are excluded. Missing cost is unknown, never free. Cost per satisfied task is unavailable with incomplete cost or population coverage, or no satisfied task. Provider-reported amounts and versioned price estimates remain distinct; neither is a complete application bill.

## Shared interfaces and access

- `GET /api/outcomes/releases?project=PROJECT&environment=production&limit=20&offset=0` returns a bounded catalog of stored verified release receipts. It is not a fresh deployment health check.
- `POST /api/outcomes/releases/compare?project=PROJECT` accepts an exact `outcome_goal` binding, `baseline_release_action`, `release_action`, `baseline_run`, `candidate_run`, and explicit frozen `before`/`after` start/end/as_of windows. It returns reasons, counts, cost coverage, experiment differences, task links and a report fingerprint. `deployment_authorized` is always false.
- CLI: `outcome-releases` and `outcome-release-compare --file PRIVATE_QUERY.json`, with project-scoped editor credentials. Query files must be private.
- Remote MCP: `released_artifacts` and `compare_release_outcomes`. Both require explicit `audit:read` and `experiments:write` plus current editor membership. No existing grant is expanded automatically. This does not grant provider or merge access.

External evidence uses these additional interfaces:

- `GET /api/outcomes/release-sources` lists safe configured GitHub/Railway targets. `PUT /api/outcomes/release-proposals/{UUID}` prepares evidence; `GET` on that same path polls it. Pass `github_connection_id`, `railway_connection_id`, `pr_number`, `environment`, and optional `base` (main). Agents may pin `base_sha`, `head_sha`, `commit` and `merge_topology` together; partial pin groups are rejected. When omitted, all are independently resolved. Reuse the exact UUID/body after uncertainty.
- `POST /api/outcomes/releases/{DIFFERENT_UUID}/adopt` takes `proposal_id` and `proposal_sha256` through an owner account session. Operator credentials, delegated keys and OAuth tokens cannot adopt. `GET /api/outcomes/releases/{UUID}` reads a final receipt. The legacy `release_action` comparison field accepts a legacy action ID or adopted external receipt ID; tagged resolution rejects collisions and proposals.
- CLI: `release-sources`, `release-prepare --id UUID --file PRIVATE_JSON`, `release-proposal --id UUID`, `release-adopt --id UUID --file PRIVATE_JSON`, and `release-evidence --id UUID`, all with `--project`. Adoption requires `AFTERSPAN_SESSION_FILE`; it never falls back to a workspace key.
- MCP: `release_sources`, `prepare_release_evidence`, `release_proposal`, and `release_evidence` require **new, explicit `releases:write` consent** and current editor membership. Preparation/polling returns an owner review URL. The scope cannot adopt, merge, deploy or change provider connections. Existing analytics, audit and experiment grants gain no capability.

All surfaces share the same engine. Definitions, run bodies and dataset text remain untrusted input and never external-action authority. Existing Slack audit observations keep their historical contract and use the explicit shared outcome adapter described in [outcome semantics](outcomes.md). Historical checks without bound quality proof stay unknown. Production adoption remains a release gate.
