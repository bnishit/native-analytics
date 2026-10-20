---
name: verify-afterspan
description: Drive Afterspan's real local web app, JSON CLI and scoped MCP before claiming an owner workflow works.
---

# Verify Afterspan

## Launch

Run from the checkout with dependencies installed using the README. Use a fresh checkout or a separate data directory; never replace an existing database. `python scripts/dev.py --port 8786` starts a loopback-only instance with private local email and disabled live billing. Capture its process/session ID. Wait for `/healthz` and `/auth/config`; a usable preview reports local email delivery. Failure is a non-zero exit, an unreachable health endpoint, or a delivery mode other than `local`.

For the real GitHub pilot, the operator supplies `AFTERSPAN_GITHUB_REPOSITORY` and a repository-scoped `AFTERSPAN_GITHUB_TOKEN` through the server's secret environment. Never print, commit or pass the token to Codex. The pilot branch must be separate from main. The normal preview works without GitHub; action operations then report unconfigured instead of inventing progress.

## Doctor

Read `/healthz`, `/auth/config`, and `/.well-known/afterspan` from the exact loopback origin. Confirm the port belongs to the process this run started. Read the intended goal using `python cli/native_analytics.py loop-inspect --repo . --repository bnishit/native-analytics --base afterspan/loop-pilot-base-20261019`. Failure is an unreachable endpoint, missing product-loop capability, wrong checkout, draft goal/dependency, or a non-zero CLI exit. A GitHub source must also be published before sync can verify it.

## Drive

Run the committed helper against a preview configured for the explicit pilot repository/branch:

```sh
python scripts/verify_product_loop.py --origin http://127.0.0.1:8786 --report /tmp/afterspan-loop-proof.json
```

Failure is a non-zero exit or missing `ok: true`. The helper reads local mail, creates synthetic fixtures, drives actual authenticated CLI/API operations, and asserts literal counts. It refuses public origins. `--open-pilot-pr` opens a real draft PR and reconciles its stable action ID. Use that flag only for an authorized pilot; it is an external write. Without it the helper still reads GitHub source and prepares a server-side plan but opens no PR. `--external-gap` accepts the sanitized connected-source schema receipt recorded by this task.

For browser proof, use the enabled computer-use browser API. Open `/app`, sign in using the private local email link, select the synthetic workspace/project, then **Goal loop**. Drive the controls in the feature map. Capture a real screenshot after the action and inspect it. Never replace an existing UI with a mockup or read browser internals through a separate automation channel.

The API/CLI helper saves private connection state under `data/loop-proof`. Once its pilot PR exists, run the other two helpers against that same private directory:

```sh
python scripts/verify_loop_codex.py --state data/loop-proof --report /tmp/afterspan-loop-codex.json
python scripts/verify_loop_pilot.py --state data/loop-proof --report /tmp/afterspan-loop-sdk.json
```

The first starts a measured connection through the CLI, then two fresh clients which submit their actual answers through MCP. It verifies the completed tool results, exact final answers, real elapsed time, idempotent replay and the still-waiting native cohort. It never manufactures producer events. Its ephemeral Codex configuration exposes only inspect/status/connect/deliver and authorizes only the two local lifecycle writes. It keeps the shell sandbox read-only and changes no global configuration. Default Codex write approvals can otherwise block this unattended proof; see the official [configuration reference](https://developers.openai.com/codex/config-reference/). Run it on a newly connected synthetic workspace; an existing clock older than 24 hours must not be reset for this proof. The second fetches the exact immutable PR head, compares it with the verified plan, executes only that opt-in example locally and tests real collector delivery and deduplication. Use the pilot helper only for a reviewed, authorized pilot source. Failure is a non-zero exit or an assertion mismatch. Neither helper proves production release or customer impact. Keep their complete client transcripts private.

For the v2 repo path, run `python scripts/verify_repo_codex.py --state PRIVATE_RUN_DIR --report NEW_REPORT.json` after the repo helper. For an authorized HTTPS deployment, supply `--production-origin EXACT_HTTPS_ORIGIN`; the connection receipt must match that exact origin. It compares one fresh read-only Codex client with the saved API observation through only `loop_status` and `loop_sources`. The status tool includes reviewed source artifacts as untrusted data; the client has no direct filesystem or provider access.

For agent proof, launch `agent/loop_server.py` with one project, checkout and workspace editor key from a private receipt. Run a fresh ephemeral Codex client with only that MCP connection. Give it no owner session, operator token, GitHub credential, shell, direct source-file or browser access. Inspect its actual tool sequence and check the structured answer against the service receipt, including zero/null values. A model's self-report is not proof.

## Evidence

Keep sanitized response summaries, CLI exit codes, actual tool-call names, source/measurement references and screenshots in an outputs directory outside private state. Keep sessions, producer keys, local mail, queue databases and complete private transcripts under ignored `data/` with mode600 files. Capture both action and resulting durable receipt. Distinguish native local fixtures, connected-provider attestations, provider-reported releases and real production outcomes. Fake upstream tests prove action gates; they do not prove a real deployment. Before/after remains observational.

## Cleanup

Stop only the process/session this run started. Do not kill by process name, reset a checkout or delete the user's data. Keep evidence after teardown and confirm the screenshot/report still exist. Keep an authorized review preview open when it is the handoff deliverable, and record its port/process separately. A real draft PR stays reviewable; do not close, merge or delete its branch as cleanup.

## Helpers

`scripts/verify_product_loop.py` is the rerunnable local API/CLI proof. `tests/test_product_loop.py`, `tests/test_loop_github.py`, `tests/test_loop_recipe.py` and `tests/test_loop_cli.py` cover negative boundaries. Run `python -m unittest discover -s tests -v`; failure is a non-zero exit, any `FAIL`/`ERROR`, or no executed tests. Run `node --test tests/loop-ui.test.cjs` to check current-versus-historical rendered outcomes. Run `node --check static/loop.js` and `node --check static/loop-onboarding.js`; failure is a non-zero exit or syntax error. Follow the feature map for the real user path.
