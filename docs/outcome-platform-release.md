# Outcome platform release acceptance

## Objective

Build and successfully test all four areas of the requested platform, deploy them to production, verify real supported flows, and update the public site and maintained documentation. This document tracks the full objective; a working subset does not complete it.

The existing production baseline is Afterspan 7621fb7 and Tracker 0db0c2e. The outcome-platform branch is unreleased. Existing analytics, journey, audit, team and product-loop evidence remains valid within its recorded scope.

## User story and ownership

A product owner connects an application, declares a useful task result, and sees which tasks achieved it. A developer opens a failed task, inspects its steps and checks, saves a sanitized regression case, compares a repair, deploys under the existing policy, and measures the next real release. An agent can execute the same supported workflow through scoped tools. No arbitrary repository or evaluator code executes on the analytics service.

Reuse journey, attempt, trace, project and release identities. Reuse the current store, authorization and product-loop policy instead of adding duplicate application domains. Evaluations are separate from span terminals and task outcomes. A failed check can coexist with a successful model or delivery span. Producer reports remain labelled; they never become independently verified evidence solely by ingestion.

## Acceptance matrix

All items require implementation plus direct evidence. Proposed targets are acceptance tests, not present production claims.

| Area | Required behavior | Completion evidence | Current state |
|---|---|---|---|
| 1 Outcomes | Versioned generic goals: task population, deadline, required evidence/checks | API/UI/CLI/MCP tests and two different product workflows | Pending |
| 1 Checks | Versioned generic source/code/model/human checks on workflow traces | Contradictions, unknowns, scope isolation and shared projection tests | Local first implementation |
| 1 Task accounting | Completion, correctness, delivery and usefulness stay distinct | Mature denominator includes failure, withheld and unclassified; retries never restart clocks | Pending generic implementation |
| 1 Outcome screen | Goal → failing tasks → evidence → next action | Real running UI and agent workflow | Pending |
| 2 Instrumentation | Python and Node tracing helpers with nested context and lifecycle | Running apps, background jobs and queue/HTTP propagation tests | Pending |
| 2 Interoperability | OpenTelemetry ingestion with explicit safe mapping and W3C propagation | Official exporter → production ingestion → trace readback | Pending |
| 2 Model evidence | Provider/model and prompt version, token usage, cost and streaming timing | Bounds/unit tests and real provider observations; no invented cost | Pending |
| 2 Privacy | Content off by default; explicit governed redacted debugging content | Consent/access/retention tests and secret/PII adversarial cases | Pending |
| 3 Regression cases | Sanitized immutable cases from failures; versioned datasets and checks | Trace-to-case readback and no raw secret transfer | Pending |
| 3 Experiments | Same cases/check versions, candidate/baseline, per-case differences | Real controlled experiment, regression failure and reproducibility | Pending |
| 3 Release loop | Approved change → checks → exact release → normal-use outcome window | Existing policy preserved; exact deployment and outcome evidence | Pending generic extension |
| 4 Collection health | Queue, drop, rejection, delay, stale evidence and evaluator failure visibility | Fault injection plus real health UI/API/MCP readback | Pending integrated experience |
| 4 Recovery | Durable delivery where promised; restart/retry/duplicate behavior | Process restart, outage recovery and no duplicate business writes | Pending proof |
| 4 Capacity | Declare operating envelope and measure it | Repeatable ingestion/query load results with hardware and dataset scope | Pending |
| 4 Restore | Consistent encrypted/scoped backup and tested isolated restoration | Restored events/definitions/outcomes and subsequent ingestion proof | Pending |
| 4 Access | Scope/role/revocation/redaction cover new surfaces; enterprise controls addressed explicitly | Unauthorized/cross-project tests; SSO/MFA readiness assessed without unsupported claims | Pending new-surface proof |
| Release | Full service/SDK/UI checks, independent review, production rollout | Current CI, release IDs, live supported paths and limits | Pending |
| Documentation | Landing, README, discovery, skills and technical guides match behavior | Running navigation/install flow and accurate capability claims | Pending |

## Integration principles

- Additive observation fields retain historical semantics. Missing checks do not mean all checks passed. Required-check completeness comes from a goal version, never whatever the producer happened to send.
- Every check binds to a task/attempt, evaluator version and evidence scope. Conflicting observations remain unknown. Replaying telemetry does not replay a business action.
- OpenTelemetry IDs require an explicit mapping, not lossy UUID coercion or guessed joins. Unsupported attributes remain omitted and counted.
- Evaluators run in the producer or an explicitly bounded supported environment. The service must not become an arbitrary code or shell execution endpoint.
- Experiments compare declared task populations and evidence windows. Release changes are observational unless a controlled experiment establishes a stronger claim.
- Cost per successful outcome must be shown alongside coverage, withholding and failure rates to avoid rewarding refusal.
- Existing source-backed Tracker checks remain supported; no product must use Slack fields to get generic evaluation support.

## Current evidence

The first local unit extends workflow terminal observations with bounded versioned checks and exposes them through the shared trace projection and UI. Step success is preserved separately. Duplicate check names, start-time checks, raw reason text and invalid evidence references are rejected. Evidence UUIDs are not returned by diagnostic readers. Conflicting terminal checks do not become a pass.

This is not yet the complete outcome model, evaluator runner, dataset system, interoperability layer or production release.
