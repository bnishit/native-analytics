# Experiments, verified releases and observed outcomes

This outcome-platform branch is unreleased. The local synthetic fixture proves the shared comparison path, not a production improvement.

## Owner flow

Save a user-task outcome goal. In Experiments, pin its immutable ID, version and definition hash to the reviewed dataset. Run baseline and candidate against those same case/check versions. Use the existing Goal loop to review the exact artifact and obtain verified merge/deployment receipts. In Outcomes, choose **Compare a reviewed deployment**, select both verified deployments and matching experiment runs, and inspect the before/after counts and linked tasks.

The comparison is read-only. It neither grants deployment permission nor changes the existing owner policy, branch protections or release checks. Both stored provider receipts must bind the artifact and exact deployed commits. A run declaring the branch head cannot stand in for the eventual deployed commit. Both experiments must precede the candidate's verified deployment. Unsupported release chains stay unknown.

The baseline window starts after its verified deployment; its entire goal follow-up ends before the candidate deployment. The after window starts at the candidate deployment. Both windows need full follow-up through their frozen receipt cutoffs. The browser preserves exact provider timestamp precision at those boundaries. Provider status time is not proof of when each user first saw a commit.

## Population and cost

New goals explicitly choose user, synthetic, internal or all traffic. Old observations default to unknown. Producers classify trusted task entry points and propagate that classification; a label is not independently verified human identity. Only user-task goals qualify for normal-use release comparisons. Known synthetic/internal tasks are excluded, while unknown population labels prevent a clean fraction. Retry attempts retain the original task clock and classification.

The pinned goal sets the minimum mature task count. Waiting tasks, missing evidence, unclassified tasks, retention gaps or inadequate samples keep the comparison unknown. A sample threshold never grants readiness. Before/after differences are observational, not causal, and only describe retained labelled tasks. The comparison also reads collection health over each full follow-up window. Missing expectations, stale/gapped pulses, reported loss or unassigned project-wide backend rejections keep it unknown. Collection completeness remains **not established** even when the declared source reports pass these bounded checks; a pulse cannot prove that every task was instrumented.

Reported model cost includes all observed model spans and retries in mature tasks, including failures and withheld answers. Waiting tasks are excluded. Missing cost is unknown, never free. Cost per satisfied task is unavailable with incomplete cost or population coverage, or no satisfied task. Provider-reported amounts and versioned price estimates remain distinct; neither is a complete application bill.

## Shared interfaces and access

- `GET /api/outcomes/releases?project=PROJECT&environment=production&limit=20&offset=0` returns a bounded catalog of stored verified release receipts. It is not a fresh deployment health check.
- `POST /api/outcomes/releases/compare?project=PROJECT` accepts an exact `outcome_goal` binding, `baseline_release_action`, `release_action`, `baseline_run`, `candidate_run`, and explicit frozen `before`/`after` start/end/as_of windows. It returns reasons, counts, cost coverage, experiment differences, task links and a report fingerprint. `deployment_authorized` is always false.
- CLI: `outcome-releases` and `outcome-release-compare --file PRIVATE_QUERY.json`, with project-scoped editor credentials. Query files must be private.
- Remote MCP: `released_artifacts` and `compare_release_outcomes`. Both require explicit `audit:read` and `experiments:write` plus current editor membership. No existing grant is expanded automatically. This does not grant provider or merge access.

All surfaces share the same engine. Definitions, run bodies and dataset text remain untrusted input and never external-action authority. Existing Slack audit observations keep their historical contract; generic adaptation and live adoption are still required for this release.
