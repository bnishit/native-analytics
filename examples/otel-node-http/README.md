# Standard Node HTTP → Afterspan

This bounded synthetic example uses the official OpenTelemetry Node SDK, automatic HTTP instrumentation and protobuf HTTP exporter. It makes two concurrent loopback requests, creates four spans per trace, and exports them to an explicitly configured Afterspan project. No model call, application write or Slack message is made.

## Run in an isolated consumer

Use Node 24 or newer. Copy `emit.cjs` into a new private directory; install these exact direct versions there:

```sh
npm init -y
npm install --save-exact --ignore-scripts @opentelemetry/sdk-node@0.300.0 @opentelemetry/sdk-trace@3.0.0 @opentelemetry/exporter-trace-otlp-proto@0.300.0 @opentelemetry/instrumentation-http@0.300.0 @opentelemetry/api@1.10.0 @opentelemetry/resources@3.0.0
node emit.cjs < /private/afterspan-producer.json
```

The mode-600 JSON file has `origin` (your reviewed HTTPS origin, without a trailing slash) and `key` (that destination project's server-side write key). Never paste the key into command arguments or publish the file. HTTP is allowed only on `127.0.0.1` for local testing. The process clears inherited `OTEL_*` settings so another exporter destination, header, logger or resource cannot leak into this isolated probe. No environment changes affect the parent shell.

Start instrumentation before loading `http`. Incoming/outgoing hooks set fixed `receive`/`http` categories; arbitrary HTTP paths, names, headers and messages are not mapped into Afterspan's metadata-only store. W3C trace context links each client call to its server step. Both HTTP spans normally have UNSET status, which Afterspan preserves as unknown. The explicitly completed synthetic root and inner step report success; neither establishes answer quality or a useful outcome. This example sends no original user-task clock, quality check, release or outcome milestone.

## Verify separately

A successful exit verifies the emitted span topology and exporter callbacks, **not storage**. OTLP can acknowledge an export with rejected spans. The result deliberately says `storage_verified:false`. Use an account/audit reader for the same project to search `node_http_interop` in environment `test`, traffic `synthetic`, over the exact send window. Open both returned references and verify four spans each, three linked parents, no missing parents and the expected unknown HTTP statuses. Producer keys cannot read traces. Compare a repeated read using the same frozen query; do not rerun the emitter to repair an uncertain read.

The output includes synthetic OTel IDs for troubleshooting; shared diagnostic views expose their project-scoped references. The HTTP traffic itself stays loopback. Only telemetry leaves the machine. The bounded BatchSpanProcessor collects all eight ended spans into one export on explicit flush. Its queue/batch limit is 32 and its export deadline is 15 seconds; these are this probe's settings, not a recommended capacity for every application. Production applications need their own queue and shutdown/flush policy. Exporter retries never retry a business request. This recipe does not promise durable queues, complete collection or lossless delivery.

The same recipe is exercised by the optional real-service integration test in `tests/test_otlp.py`. Set `AFTERSPAN_OTEL_NODE_MODULES` to this consumer's absolute `node_modules` path and run that test with Node 24+ on PATH. The service's default Python exporter tests do not require npm dependencies.

Primary references: [official JavaScript exporter setup](https://opentelemetry.io/docs/languages/js/exporters/) and [official HTTP instrumentation hooks](https://open-telemetry.github.io/opentelemetry-js/modules/_opentelemetry_instrumentation-http.html). Versioned API details follow the installed packages; this example uses the 3.0 BatchSpanProcessor options object.
