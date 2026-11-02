# OpenTelemetry intake (unreleased outcome-platform branch)

An application can send its existing OpenTelemetry spans to `POST /v1/traces` with the project's server-side write key in `Authorization: Bearer …`. Audit readers use the same Traces search, detail and MCP tools as native workflow observations. No separate store or raw-data reader is added.

## Transport and limits

Supported: OTLP/HTTP `application/x-protobuf` using official generated protobuf messages, and OTLP JSON `application/json` with hexadecimal IDs. Gzip is optional. Both compressed and decompressed bodies are bounded to 2 MB. At most 500 spans are accepted per request; the existing 600-ingestion-requests/project/minute limit also applies. This is an intake bound, not a measured capacity claim. gRPC is not supported.

Valid spans commit atomically through the normal event writer. Invalid or unsupported spans return OTLP partial-success rejected counts and a fixed safe error, never raw payload fragments. An invalid envelope returns 400, unsupported encoding/type 415, oversize 413, and an invalid write key 401. Retry the same export for a network uncertainty; deterministic observation IDs deduplicate identical exports. Changed terminal observations remain conflicting evidence rather than silently replacing an earlier report.

## Required mapping

Set these attributes on the resource or span; a span value takes precedence:

| Attribute | Meaning |
|---|---|
| `afterspan.workflow` | Required code-owned slug, e.g. `document_export` |
| `afterspan.environment` | Required production/staging/development/test/local |
| `afterspan.step` | Fixed native step category; default processing, or model when a provider is supplied |
| `afterspan.attempt_id` / `afterspan.attempt_number` | Explicit business attempt UUIDv4 and number; otherwise one initial attempt, deterministically bound to the trace |
| `afterspan.request_started_at_ms` | Optional original task clock; absent stays unknown for outcome population accounting |
| `afterspan.release_sha` | Optional full lowercase Git commit |

W3C full trace and span IDs map through the exact `afterspan-otel-v1` SHA-256 namespace described in [the SDK guide](tracing-sdk.md). Explicit parent IDs preserve nested cross-service relationships. For native-SDK/OTLP joins, attach the same attempt ID, number, workflow, environment, release and original clock. An HTTP baggage carrier does not automatically become OTLP attributes; the application must bind that context. Missing parents stay visible.

OTLP complete spans produce start/end observations at their declared clocks, received now. OK maps to reported step success, ERROR to reported failure, and UNSET to unknown status. Neither OK nor a closed timing bar proves an answer correct. Out-of-retention, future, reversed or longer-than-24-hour spans are rejected. No original task clock or release is inferred from a service name or span start.

## Metadata-only model evidence

The allowlist maps `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens`. Optional extensions are `afterspan.model.template_version`, `cache_read_count`, `reasoning_count`, `first_output_ms`, `usage_source`, `cost_usd_decimal`, `cost_microusd`, `cost_source` and `price_version` (each under `afterspan.model.`).

Usage source is provider_reported by default for emitted gen_ai counts, or explicitly estimated. It is producer-reported provenance, not a service-side provider fetch. Counts are strict nonnegative bounded integers. Cost is a decimal USD string with at most 12 fractional places (`cost_usd_decimal`) or legacy integer millionths (`cost_microusd`), never both. A numeric float is not accepted as a decimal amount. Both forms require a declared provider_reported or price_table source. A price table also requires an immutable version. Missing cost stays unknown, including when token counts exist; Afterspan never invents a price. First-output timing must fit the measured span duration. Template version is a code-owned version, not the prompt. The same validated `model_usage` object can be sent on terminal native model spans.

Span names, arbitrary attributes, raw error/status text, event bodies, links, prompt/output messages, resource hostnames and user identifiers are deliberately omitted. This intake is not a general raw-span archive. Optional reviewed debugging uses a [separate owner-scoped encrypted snapshot flow](debugging.md); this OTLP intake never captures content. Do not put credentials or personal data into allowlisted provider/model/version fields. Invalid metadata rejects its span instead of retaining a partial favorable result.

## Evidence

Local tests run the official OpenTelemetry Python SDK and HTTP exporter against a real loopback service, then read the nested trace from the shared explorer. Other tests cover protobuf/JSON/gzip equivalence, idempotency, partial rejection, unknown status, conflicting updates, model provenance and decompression bounds. This is local synthetic proof. Official exporter → deployed production readback remains a release gate.

Primary standards: [OTLP](https://opentelemetry.io/docs/specs/otlp/), [Trace API status](https://opentelemetry.io/docs/specs/otel/trace/api/), [W3C Trace Context](https://www.w3.org/TR/trace-context/). See [the full release acceptance](outcome-platform-release.md) for pending scale, recovery, privacy and production verification.
