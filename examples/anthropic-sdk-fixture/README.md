# Anthropic SDK → local fixture → Afterspan

Use this validation recipe when a real provider key is unavailable or unnecessary. It exercises the official Python `anthropic==2.4.0` SDK against a local HTTP server with a dummy key. The existing Afterspan observer consumes the SDK's actual typed stream events. Only trace metadata can be sent to your own Afterspan project; **no native Anthropic endpoint, model inference or paid provider call is made**.

Build/install the reviewed Afterspan Python wheel from the repository, then install `anthropic==2.4.0` in a fresh private environment. Anthropic is a dependency of this recipe and its tests, not the base Afterspan SDK.

```sh
python -m pip install /private/sdk-build/afterspan_sdk-0.1.0-py3-none-any.whl anthropic==2.4.0
python emit.py /private/new-anthropic-fixture-outbox.db < /private/producer.json
```

Copy `emit.py` into that consumer. The mode-600 producer JSON contains your reviewed `origin` and that project's `key`; this is the Afterspan producer credential, not an Anthropic key. Do not paste credentials into command arguments. Use a new private outbox path. An existing path is refused before reading credentials or running the fixture. After an uncertain result, inspect that outbox and perform scoped readback rather than rerunning the example.

The standalone entry point clears inherited `ANTHROPIC_*` and `OTEL_*` settings in its own process before loading the SDK. The client uses an explicit dummy key and numeric loopback URL, no environment proxy, disabled native SDK telemetry and zero automatic retries. A request hook refuses any other destination or credential and removes ambient custom headers before even the local request. No credential profile or real provider key is selected. The request and response bodies contain only fixed private-content sentinels. Do not import this isolated validation fixture into a running application.

| Fixture case | Execution result | Accounting/timing expectation |
|---|---|---|
| Complete stream | Succeeded | Input total 20 includes uncached 3 + cache creation 7 + cache read 10. Cumulative output 1 →2 →5 →5 ends at 5. |
| Stream error, followed by misleading terminal frames | Failed | The official SDK raises; a later fixture completion cannot turn it into success. |
| Missing message-stop event | Failed | The caller explicitly checks the final lifecycle event; EOF alone is insufficient. |
| Reasoning-only output with absent cache details | Succeeded execution | Input/cache and first meaningful output stay unknown. Fixture-reported output is 3. This is not a useful answer. |
| Rejected dummy authentication | Failed | Official SDK authentication exception; no inferred usage or first output. |
| Caller cancellation after text | Failed | Stream closes; incurred partial fixture usage can remain observed. |

The server writes CRLF SSE frames in seven-byte HTTP chunks, including a split UTF-8 sentinel. The caller retains the last non-null stop reason when a later usage update omits it. A completed stream means the SDK/caller finished the fixture operation; it creates no quality, completion, delivery or usefulness milestone.

All six tasks are labelled synthetic in environment `test`, with workflows prefixed `anthropic_fixture_`, provider `anthropic_fixture` and model `fixture/messages`. Their counts are reported by a controlled fixture, not measured vendor usage or a bill. Exact cost and reasoning-token counts remain unknown. The example checks all 24 observations for prompt/output/error/key sentinels before flushing. Its output deliberately retains `storage_verified:false`: an ingestion acknowledgment is not independent storage proof.

## Verify storage separately

Use an account/audit reader for the same project and freeze the send window. Search `anthropic_fixture_`, open all six traces, and verify two nested spans each. Complete/reasoning-only have successful execution; the other four have failed root and model spans. Check the input/cache/first-output unknowns, cumulative accounting and lack of quality/milestone/business proof. A producer key cannot read traces. Repeat the frozen reads instead of rerunning the workload after a read timeout.

Mandatory CI runs the real official SDK, the existing collector and a real local service fixture. Fresh installed-wheel consumers exercise this recipe outside the checkout and verify scoped subject erasure. Production-service readback is recorded separately in [release acceptance](../../docs/outcome-platform-release.md). This recipe does not validate a real Anthropic key, native endpoint availability, generated answer quality, model latency, billing, load capacity or normal-user benefit.

Primary contracts: [official Python SDK](https://platform.claude.com/docs/en/cli-sdks-libraries/sdks/python), [Messages streaming lifecycle and cumulative usage](https://platform.claude.com/docs/en/build-with-claude/streaming), and [authentication](https://platform.claude.com/docs/en/manage-claude/authentication). The pinned installed package determines the tested behavior; moving documentation is not a version guarantee.
