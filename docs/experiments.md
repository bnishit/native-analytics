# Regression cases and experiments

## User flow

A project editor opens a failed or incomplete request in **Traces**, chooses **Create a reviewed regression case**, and supplies a safe input plus the facts the answer should contain. Afterspan links the exact frozen trace window without copying a private request or model output. The editor pins selected case versions into a dataset. An application developer runs the baseline and repair on those same inputs in their own environment. **Experiments** shows each check, changed facts and whether the repair meets that dataset's review criteria.

An experiment is a controlled fixture comparison. It is not a production outcome, a deployment receipt or permission to merge. Existing reviewed release policy still applies. Measuring the later real release remains a separate gate.

## Stable definitions

- A case has a stable UUID and immutable versions. It contains a title, reviewed input JSON, expected JSON facts and an evaluator/version. An optional source binds to one failed/incomplete trace and its exact start/end/as_of query.
- A dataset pins case UUIDs, versions and canonical SHA-256 hashes. Changing a case creates another version; it never changes an existing dataset. An editor also sets a minimum pass rate above zero, default 1.0.
- A run pins one dataset version, a variant name, declared application release, runner hash and reviewed configuration. Each case result contains output JSON or a fixed error code, plus measured duration. Submission retries use the same run UUID and exact prepared body. Do not rerun the application to retry telemetry submission.
- A comparison requires the same dataset UUID/version/hash and evaluation runner hash. Different case populations or checks return conflict, rather than a misleading improvement percentage.

Definitions use sorted canonical UTF-8 JSON. Identical UUID/revision retries return the same version; changed content at that revision returns 409. The service verifies stored/pinned hashes on read. Hashes detect changed bytes; they do not attest trustworthy authorship or actual source correctness.

## Fixed checks and honest coverage

The service executes only version 1 of two bounded deterministic checks. `json_equal` requires exact typed JSON. `json_subset` requires all expected dictionary fields inside the output; extra fields are ignored, while arrays retain exact length/order. An empty top-level subset is rejected. Booleans are distinct from numbers. No shell, uploaded evaluator, repository code, arbitrary SQL or model call executes on this service.

| Case state | Meaning | In pass-rate denominator? |
|---|---|---|
| passed | Output matches the pinned reviewed facts | Yes |
| failed | Output disagrees with those facts | Yes |
| error | Runner reports timeout/unavailable/internal/unsupported | Yes |
| missing | No result supplied for this pinned case | Yes |

Every case appears exactly once in a dataset/run. Foreign or changed case versions and duplicate results are rejected. Missing results cannot shrink the denominator. Baseline or candidate missing cases prevent review eligibility. A candidate must meet the dataset's threshold and introduce no regression from a passed baseline case. The response always says `deployment_authorized: false`.

Changed JSON paths show expected/observed previews for the baseline and candidate. At most 20 differences per case and 200 characters per value are shown, with total counts and a truncation label. Exact reviewed inputs/results remain available through their scoped versioned reads. A known check failure is not replaced with a favorable score because another field improved.

These checks establish agreement with curated expected facts. They do not fetch a fresh source, assess every free-form statement, prove subjective usefulness or replace existing Tracker source checks. A model-judge/custom-evaluator platform is not implied by these two check types.

## Privacy and access

Trace content remains off. Cases are explicit curated fixtures, not raw production exports. Case/run writes require strict `privacy_reviewed: true`; strings such as `"true"` and numeric flags are rejected. Shared client/server checks reject common credential, email, phone and sensitive-field patterns before storage. The local runner also rejects unsafe output before preparing an upload, returning a fixed unsupported code without copying its content. Exception messages are not retained.

This detector cannot recognize every private name, confidential fact or identifier. Review fixtures before use. Use synthetic values and a read-only or isolated test application. Dataset text is untrusted input to the application, never permission to perform an external action. The local runner calls only the application callback supplied by its developer; it does not retry that callback or grant it credentials.

Only project editors/owners can read fixture bodies or manage experiments. Aggregate keys, audit keys and ordinary readers cannot read them. Remote MCP requires a **separate `experiments:write` consent scope**, explicitly labelled as access to reviewed inputs/results as well as management. Existing analytics/chart/audit/goal grants do not expand. Role downgrade/removal/revocation blocks subsequent access. List responses contain summaries; exact object reads return reviewed content.

Cases expire under the instance retention policy. Dataset expiry is no later than its earliest pinned case; run expiry is no later than its dataset. Reads reject expired dependencies even before the hourly cleanup physically removes payloads. Retry cannot extend retention. A new version has its own retention, but never restores an expired original. Reproducibility after retention/erasure is unavailable, not silently reconstructed. Backups need their own scoped retention and restoration policy.

Technical bounds: 100 current objects per kind/project, 1,000 stored versions per kind/project, 100 cases/dataset, five nested levels, 100 list items, 64 object fields, 4,000 characters/string, 16 KB per input/expected/output/configuration and 256 KB for a complete saved definition. These are safety bounds, not measured capacity or commercial plan gates.

## CLI and API

Use an authorized editor account session or scoped editor workspace credential. Credentials remain in the existing private receipt/environment, never command arguments. Prepare JSON bodies in mode-600 files. Stable IDs below are caller-generated UUIDs.

```sh
python cli/native_analytics.py experiment-save --project PROJECT --kind cases --id CASE_UUID --file /private/case.json
python cli/native_analytics.py experiment-save --project PROJECT --kind datasets --id DATASET_UUID --file /private/dataset.json
python cli/native_analytics.py experiment-get --project PROJECT --kind datasets --id DATASET_UUID --version 1
python cli/native_analytics.py experiment-save --project PROJECT --kind runs --id RUN_UUID --file /private/run.json
python cli/native_analytics.py experiment-compare --project PROJECT --file /private/comparison.json
```

`experiment-list --kind cases|datasets|runs` returns summaries. For a case/dataset version update, pass its last-read `--revision`; zero means a new object. Runs are immutable single-version submissions. `comparison.json` contains `baseline` and `candidate` run UUIDs.

API equivalents are GET/PUT `/api/experiments/cases/{id}`, `/datasets/{id}`, `/runs/{id}`, collection GETs, and POST `/api/experiments/compare`, all with `project`. Case/dataset PUTs require `revision`; GETs accept `version` (default 1). OpenAPI is authoritative for bodies.

Remote MCP tools are `experiment_objects`, `save_experiment_object`, and `compare_experiments`. The official transport tests create/retry/read cases, pin a dataset, submit baseline/candidate and compare, then verify role downgrade denies reads.

## Application-side Python runner

`sdk/python/afterspan_experiments.py` uses only the standard library. `prepare_run(dataset, load_case, execute, variant=…, release_sha=…, privacy_reviewed=True)` checks exact dataset/case hashes and expiry, gives a copy of each reviewed input to the application callback, measures execution and prepares a submission. It never gives expected answers to that callback. `load_case(id, version)` must fetch the exact object using the caller's authorized scoped API client. `execute(input)` is your application in a read-only/test environment; it returns structured facts. Explicit reviewed configuration can identify a model/template variant without storing prompts.

The runner hashes its own source into the evaluation runner version. App changes belong in the release SHA and variant configuration. Save the returned body privately and review its prepared outputs before submitting it under one stable run UUID. If the network result is unknown, read/retry that same UUID/body; do not call `prepare_run` again merely to resend. Application-side timeouts and network permissions remain the host's responsibility.

## Verification and research

Local checks cover a trace-to-case link, immutable versions, typed facts, scope isolation, common secret/PII patterns, expiry, missing/error accounting, changed hashes, different populations/check versions and a deliberately broken comparison gate. The running local browser created a case from a failed trace, pinned it into a dataset and showed an actual callback baseline/candidate submitted by CLI. Official MCP transport and CLI paths pass. A browser-approved production Streamable HTTP client also saved/retried/read one reviewed synthetic case, verified summary content omission and pinned its exact version into a dataset in the isolated verification project. The temporary grant was revoked after testing. This tests remote fixture transport, not remote model execution or production impact.

On the production Afterspan service, three reviewed synthetic catalog cases were saved into one immutable dataset, including a link to the exact earlier partial-answer trace window. The Python runner executed real pre-scope-correction Tracker code and current Tracker code locally, with intercepted synthetic source reads and controlled model steps. Every callback executed fresh source and bound its revision, harness and runner hashes. The old code failed all three reviewed expectations; the current code passed all three, with no missing or errored case. The production API, account-scoped CLI and real Experiments screen agree. The full-list case passes by keeping incomplete scope unknown, not by calling the task complete. The old total case exercises a forced typed catalog path; it does not prove historical natural-language routing.

This proves local source execution plus production experiment storage/comparison, not real provider execution, normal-user improvement or release authority. Both runs were prepared after the current deployment, so they cannot be used as pre-release evidence for that deployment. A later release comparison still needs a dataset pinned to a user-traffic goal, verified release actions, adequate mature windows and collection-health evidence. See [release acceptance](outcome-platform-release.md).

The data separation follows the [Langfuse experiment data model](https://langfuse.com/docs/evaluation/experiments/data-model) and [dataset workflow](https://langfuse.com/docs/evaluation/experiments/datasets): cases, runs and evaluations stay distinct. Afterspan additionally retains an explicit missing-case denominator and keeps experiment eligibility separate from deployment authority. This is not feature-parity certification.
