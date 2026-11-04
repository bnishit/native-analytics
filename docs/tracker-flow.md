# One Tracker failure to a reviewed release

An internal teammate gets a wrong or incomplete catalog answer. The coding agent inspects that task, reproduces supported evidence, repairs the owning code and checks the exact release. Afterspan keeps the case/check versions and the outcome evidence. The agent's host executes repository code; the service never becomes a shell or arbitrary model runner.

A retained anonymous catalog fixture can replay its **catalog check**. It cannot replay the original question, model decisions, tool retrieval or Slack exchange. Missing evidence remains blocked. A reviewed synthetic case is useful for a repair, but is never labelled a reconstruction of a missing historical request.

## Fresh Codex setup

Use your own Afterspan deployment and existing Tracker project. Self-hosted use needs no billing account. If the instance/project or provider connections do not exist, an owner sets them up through their existing authorized account flow. Do not reuse another deployment, operator token or project key.

For an OAuth-capable Codex client, no Python checkout or plugin installation is needed:

```sh
codex mcp add afterspan --url https://YOUR_HOST/mcp
codex mcp login afterspan --scopes audit:read
```

Check the origin, client and callback in the browser, choose the project and approve. Then ask:

> Load the Afterspan improve-tracker workflow. Inspect one failed catalog check in this Tracker checkout. Show missing evidence before proposing a repair.

The agent calls `connection_status`, then `get_workflow(name="improve-tracker")`. It does not infer a working connection from a configured URL. `/.well-known/afterspan` and `/openapi.json` on that same origin describe the actual deployment. If the flow tools are unavailable, report that deployment/version gap rather than substituting broader credentials or inventing a result.

### Optional skills package

The same six workflows are packaged for compatible clients. Version0.2.0 adds the Tracker improvement entry, a Codex onboarding skill and a repo-local marketplace. Set `AFTERSPAN_URL` to your own origin in the process that starts the client. Clients without environment substitution should use direct MCP setup above.

After reviewing the package source/version:

```sh
codex plugin marketplace add bnishit/native-analytics
codex plugin add afterspan@afterspan-local
```

Use `--ref REVIEWED_COMMIT` on marketplace add when pinning a reviewed Git release. This local/repo marketplace is not a public Plugins Directory listing. The Claude manifest is included; every client still needs its own install, OAuth, representative-task and reconnect/revocation proof. No project hook, capture policy, monitor or release authority is installed automatically.

Package authors can check references and command-local discovery without installation:

```sh
python scripts/check_agent_package.py --codex --origin https://YOUR_HOST
```

This verifies the exact available package/version. It does not install the package, authenticate, change global config or prove a completed agent task.

Maintainers can run `python scripts/verify_tracker_agent.py --origin http://127.0.0.1:8786 --state /private/new-proof-state --report /private/new-proof.json` against a fresh isolated local-mail preview. The helper uses the actual remote MCP/PKCE flow, a temporary local test project and a fresh restricted Codex client, then revokes the grant. Read its help for exact options. It refuses a public origin; local next-step proof is separate from plugin installation, production consent and a completed repair/release.

## Permissions at the point they are needed

| Work | Required remote grant and role |
| --- | --- |
| Inspect traces, outcome goals and collection health | `audit:read` |
| Read/save reviewed cases, datasets and runs; inspect linked Tracker flow status | `experiments:write`, plus `audit:read` for flow status; current editor |
| Read one owner-approved selected debug fixture | Separate `debug:read`; current editor. This never enables capture. |
| Create a new outcome-goal version when needed | Separate `goals:write`; current editor |
| Prepare external release identity | Separate `releases:write`; current editor. An owner account adopts the exact proposal. |
| Start/read/control a bounded follow-up | Separate `followups:write` plus `audit:read` and `experiments:write`; current editor |
| Change code, merge or deploy | Existing coding-host/repository/provider authorization. An analytics grant gives none. |

Request extra consent only for the selected task. Existing access/refresh tokens do not gain new scopes automatically. Reconnection or expanded consent is a separate human step. Follow-up authority is rechecked while it runs, including role changes and revoked/narrowed credentials.

## 1. Select one failure

Read collection health, then `search_traces` and `open_trace` for the expected environment and `slack-request-v1`. Reuse the exact returned start/end/as_of query and reference. A goal pins the workflow, environment, check version, required evidence, deadline and sample minimum. Call `outcome_goals` to find that existing definition instead of writing a second meaning.

With separate debug consent, call `debug_capture_policy`, then list `review_debug_snapshots` metadata before choosing a snapshot. Preserve its expiry. If capture was off, content expired, source evidence is absent or the projection is unsupported, say exactly why reproduction is blocked. This workflow never broadens/renews capture or retries business work to fill a gap.

## 2. Reproduce on the coding host

Use a private, regular, non-symlink input file with mode 600 and at most 20 KB. A selected debug response carries its content and expiry. A separately reviewed bare case has its own explicit retention decision; it is not an indefinite debug-snapshot export. Never strip expiry from a selected snapshot to turn it into a bare case. Downloaded files cannot be remotely erased by Afterspan: remove the private snapshot after the run and no later than its expiry. The safe result receipt omits its content.

Keep the current reviewed replay runner separate from the clean Tracker source checkout at the recorded revision. Do not check out an old revision and run its old CLI: it does not have the current receipt/exit guards. Both source revisions must already be available in the runner's Git object store; the runner does not fetch or install dependencies. The two declared lockfiles must match, otherwise replay is blocked. This does not reconstruct or attest every historical installed dependency.

```sh
node /absolute/reviewed-runner/scripts/replay-catalog-check.mjs /private/snapshot.json \
  --repo /absolute/exact-source-checkout --output /private/new-replay-result.json
```

The npm convenience command in the reviewed runner checkout is `npm --silent run afterspan:replay -- ...`. `--repo` defaults to that checkout; use the separate path for a historical source. The runner validates both clean source trees and calls the selected source's actual comparator and renderer, never its legacy CLI. The helper calls the real checker and blocks network access. It does not call GrowthBook, Slack or a model. A wrong/dirty revision, unsafe/expired input, missing projection or mismatch exits nonzero. Output is a new atomic mode 600 receipt; it never overwrites an earlier attempt.

Require a `tracker-catalog-replay-result-v1` receipt with `status: reproduced`, `matches_recorded_check: true` and matching recorded/executed releases. Legacy output without this contract is unsupported, even if it exits 0. `reproduced` means actual check facts match the recorded check facts, even if that recorded result is failed. It is not a green answer or an execution of the original model. `blocked` and `mismatch` remain unsuccessful attempts.

For a **checker repair only**, use a clean candidate checkout and separately reviewed expected facts:

```sh
node /absolute/reviewed-runner/scripts/replay-catalog-check.mjs /private/snapshot.json \
  --repo /absolute/exact-candidate-checkout \
  --candidate FULL_CANDIDATE_SHA --expected /private/reviewed-check.json \
  --output /private/new-candidate-result.json
```

The expected file has `privacy_reviewed: true` and a `quality` object containing `check_version`, `reported_outcome`, `checked_claims`, `mismatched_claims` and `unknown_claims`. Review every value; do not copy the failed output as the desired answer. `verified` means the candidate checker matched those reviewed facts. The receipt preserves the reviewed runner release separately from the executed and originally recorded releases and always says full-request replay is false.

For an **application/answer repair**, run reviewed inputs through the actual application-side runner instead. Frozen candidate/source facts cannot demonstrate that answer-generating code changed. The existing [Tracker application example](../examples/tracker-provider-evaluation/README.md) distinguishes offline controlled model steps from explicitly authorized real-provider execution. Do not spend credits or send a live Slack request by default.

## 3. Pin the regression and fix

Explicitly review a safe case and its expected facts. Save the immutable case, then pin its exact version into a dataset bound to the outcome goal id/version/hash. Keep any source link on the exact frozen failure. A reviewed synthetic case remains labelled synthetic.

Run the baseline first and retain its failing assertion. Repair the smallest owning source path, run the candidate on the same cases and run neighboring regressions. Submit actual application-side results under stable run UUIDs; do not create hand-authored passing results. An uncertain upload retries/reads the same prepared body and ID without rerunning the application or provider.

`compare_experiments` requires the same dataset/check versions. Missing and errored cases remain counted. Get independent product and engineering review of the exact tree before using existing release authority.

## 4. Inspect one linked flow

`tracker_flow_status(query)` derives the stage and next supported action from retained objects. It accepts these optional references, not caller-supplied passed flags:

| Field | Value |
| --- | --- |
| `outcome_goal` | Existing `{id, version, sha256}` binding |
| `source` | Selected failure's full frozen trace query plus `reference` |
| `dataset` | Existing `{id, version}` |
| `baseline_run`, `candidate_run` | Stored run UUIDs |
| `release_action` | Adopted release evidence UUID |

Add references as the work completes and preserve the query in private host state. Wrong project, changed goal, unrelated source/case, different dataset or wrong release cannot silently advance the flow. Baseline reproduction here means a complete failing **reviewed case**, not historical full-request replay. Submitted experiment outputs are checked against reviewed facts; they are not execution attestations.

The equivalent CLI is `tracker-flow-status --project PROJECT --file private-query.json`. This read cannot grant code execution, code review, CI clearance or deployment authority.

## 5. Verify the exact release

Use the host's existing reviewed merge and deployment workflow. Require the exact approved tree, CI and independent review. Check the running commit and safe collection/application readback; do not send an unapproved business action to prove telemetry.

Use `prepare_release_evidence` with separate release consent, poll `release_proposal`, then have the owner adopt the exact reread proposal. A newer building/failed/conflicting deployment blocks stronger evidence. Both baseline and candidate source/run bindings must match their release receipts. Experiments must precede the deployment for the integrated release-outcome gate; post-deployment experiments do not become pre-release evidence.

## 6. Start one bounded follow-up

`save_outcome_followup(id, definition)` stores one observe-only job after the release gate. Reuse the same UUID/body on timeout. The definition pins:

- `outcome_goal`: exact goal binding.
- `baseline_release_action` and `release_action`: adopted baseline/candidate release evidence.
- `baseline_run` and `candidate_run`: matching predeployment experiments.
- `before`: explicit frozen baseline `start`, `end` and `as_of` window.
- `window_seconds`: default 86400, at most 86400.
- `interval_seconds`: default 3600, at least 60 and no longer than the window.
- `max_checks`: default 24, at most 24.

The candidate entry window starts at server acceptance. Its deadline and budget cannot be renewed by retry, pause/resume or restart. The server periodically evaluates existing retained outcomes/collection metadata; it executes no code, model, provider, Slack or deployment call. The worker resumes retained due work after a service restart. This is product follow-up, separate from private capture settings and operational backup schedules.

Read `outcome_followup(id)` for progress, the frozen result and the last meaningful status notification. Notifications are retained for client reads; they are not sent through email or Slack. `control_outcome_followup(id, control)` accepts `{operation:"pause"|"resume"|"cancel",revision:LAST_READ_REVISION}`. Read before retrying a control after uncertainty. Resume never starts a new clock. Revoke/narrow the initiating access or lose editor membership and the worker stops authorized progress.

CLI equivalents are `followup-save --project PROJECT --id UUID --file private-definition.json`, `followup --project PROJECT --id UUID`, and `followup-control --project PROJECT --id UUID --file private-control.json`. Normal CLI authentication rules apply; never pass credentials as command arguments.

Finish with six separate claims: reproduced check/case, repaired tests, reviewed source, verified release identity, observed collection and measured user outcome. Low samples, immature deadlines, stale collection, changed meaning or expired evidence keep improvement unknown. No sample count grants readiness, and before/after does not prove causality. Keep the client quiet on unchanged waiting state. A validated lesson may become a reviewed regression or skill change; no instructions are promoted automatically.
