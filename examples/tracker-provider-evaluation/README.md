# A provider-backed read-only Tracker experiment

An operator needs to know whether the real model's decisions still produce a source-checked answer. A parser benchmark only tests intent recognition. This example therefore executes the existing typed read-agent entry, real `chatCompletion`, original response parser, answer renderer and independent source checker from one exact Tracker commit.

The application source and provider request are real. The twenty-row catalog is synthetic and fixed. This does not send Slack messages, call live GrowthBook, test the upstream parser, establish streaming timing or measure normal-user benefit. All native telemetry sinks are replaced with metadata-only local capture. Selected numeric usage and exact cost strings come from Tracker's actual response parser; prompt, model text and raw provider envelopes are never persisted by this runner.

## Run it

Use Node 24 and a reviewed Tracker checkout with installed dependencies. The source archive must exist locally. The runner checks installed package bindings and permits only the previously documented SDK-only dependency difference for an older source. Both variants reuse the supplied checkout's dependency installation; it is not a fresh dependency install per revision. The manifest pins source, lock, runner, transport and harness hashes.

First execute one offline case. This uses controlled model responses and cannot establish provider execution:

```sh
node examples/tracker-provider-evaluation/run.mjs \
  --offline --repo /absolute/path/to/experiment-tracker \
  --source FULL_COMMIT_SHA --variant candidate --case bounded-five-enabled \
  --output /private/new-empty-attempt-directory
```

Available cases are `bounded-five-enabled`, `total-enabled` and `full-list-capped`. Their prompts and source rows live with the fixture owner. Run both immutable revisions on the same three cases. Do not substitute different fixture messages or compare different harness hashes.

For a real provider call, replace `--offline` with `--live` and add `--runtime FULL_DEPLOYED_TRACKER_SHA --project RAILWAY_PROJECT_UUID --service RAILWAY_SERVICE_UUID --environment RAILWAY_ENVIRONMENT_UUID`. The existing Railway CLI login must already authorize that service. Each provider child checks its running commit, reads only its existing `OPENROUTER_API_KEY`, and calls the fixed OpenRouter Chat Completions HTTPS endpoint. The key never leaves that process. No runtime route or application bootstrap is imported. Synthetic request bytes travel as a quoted base64 argument because Railway SSH stdin is not assumed to work; never adapt this transport for private user content.

Only `openai/gpt-4.1-mini`, temperature zero, strict JSON output and at most 2,200 output tokens are allowed. There are at most three calls per case and no automatic provider retry. Both the provider and transport have deadlines. This spends provider credits only in explicit live mode. Elapsed application time includes SSH setup and transport; it is not a provider latency estimate.

Every attempted call is recorded before execution. Output directories must be new. If a call or process is interrupted, retain that attempt as unknown/error; never restart it just to resend. Successful responses are consumed in memory and discarded. Receipt files retain hashes and timestamps only. Keep the entire output directory private: it also contains the pinned source archive and test diagnostics.

## Save and compare in Afterspan

Use the existing application-side `prepare_run` SDK and scoped experiment API/CLI. A reviewed case input identifies the exact fixture, public synthetic request and environment; expected results are never passed to application code. The seven output facts are `shown`, `enabled`, `disabled`, `total`, `partial_label`, `check_result` and `unknown_claims`. Read a successful private `result.json` only after checking its manifest source/variant/case, runner/harness/transport/lock hashes, request/source fixture digests and nonempty executed test receipt. Preserve error results in the denominator.

Pin the same case versions into one immutable dataset. Retain the prepared run body and one stable run UUID before upload. Reconcile an uncertain upload by reading that exact ID; never rerun the provider callback. Comparison is against declared synthetic facts. It grants no deployment permission and cannot establish a change in real-user outcomes. A released-outcome comparison additionally needs the existing exact goal, experiment and deployment bindings and mature user windows.

## Controls

```sh
node --test tests/provider-evaluation.test.cjs
```

For a whole application negative control, run the offline example with `--negative-control caught_request_post`. It deliberately catches a forbidden Request-object POST; the retained attempt must still fail. This proves that a swallowed write attempt cannot become a passing evaluation. Malformed/empty selections, changed dependency bindings, reused output directories, wrong runtime and oversized/unapproved provider requests fail before a new provider call.
