# LangChain → Afterspan

The optional Python callback adapter records chain, model, tool and retrieval steps using LangChain's run/parent IDs. Use one handler per request, inside an existing Afterspan task. It reuses the collector and native trace contract. Inputs, outputs, run names, metadata dictionaries, documents and error text are never copied. No provider call or task-success milestone is created by the adapter.

Install the reviewed pinned source with the optional extra:

```sh
python -m pip install 'afterspan-sdk[langchain] @ git+https://github.com/bnishit/native-analytics.git@457f00b0da00df36c7a7c57cdb3749e75ed552d8#subdirectory=sdk/python' langchain-core==1.7.4
```

Alternatively, install the SDK wheel with its `langchain` extra, or install the wheel normally alongside `langchain-core==1.7.4`. The base SDK remains dependency-free. The pinned Git install with this extra also passed in a new private environment without checkout imports; its three local synthetic pipelines queued 30 observations without flushing. Version 1.7.4 is tested; the extra allows compatible 1.x releases, which still need application validation.

```python
from afterspan_langchain import AfterspanCallbackHandler

with tracer.task('document_answer', traffic_kind='user') as root:
    with AfterspanCallbackHandler(tracer, parent=root.context) as handler:
        answer = pipeline.invoke(question, config={'callbacks': [handler]})
    diagnostics = handler.status()
```

Async `ainvoke` uses the same handler. Create a separate handler for every request. Close it before the parent task exits; the context manager does this even after an exception. Each callback run has a separate Python context, so start/end callbacks can run on different threads without changing the caller's active trace. At most 1,024 runs are remembered per handler, including completed parents. Missing parents, duplicates, budget drops, unmatched terminals, metadata errors and unfinished work have bounded counters in `status()`. Inspect these alongside collector health. Unfinished runs close as unknown, not success. An explicitly reported framework error closes its operation as failed. Neither result is an answer-quality check.

Model identity is off until an exact pair of `ls_provider` / `ls_model_name` values is allowlisted with `models={('openai', 'your-model')}`. Framework names and arbitrary serialized model fields are not used. Model switching requires every intended pair to be listed; other model calls still have a model step but no guessed identity or usage.

Token counts are also off by default. Set `reported_usage=True` only after verifying that your installed model adapter's final `AIMessage.usage_metadata` preserves provider-reported totals and absence. Some adapters synthesize zeros. The reader takes one generation's explicit input/output totals and cache-read/reasoning subsets, never `total_tokens`, raw provider output or a cost estimate. Multiple generations are ambiguous and are skipped with a diagnostic. Missing details stay unknown. Stream first-output timing requires explicit text or tool content in a message chunk; reasoning-only and token-only callbacks do not start the clock. Non-streaming calls keep first-output time unknown. Exact cost is not inferred.

## Bounded synthetic example

Copy `emit.py` to a fresh private consumer with the installed SDK and LangChain Core 1.7.4:

```sh
python emit.py /private/new-langchain-outbox.db < /private/producer.json
```

The mode-600 JSON file contains your reviewed `origin` and that project's `key`. Keys never enter command arguments. The example clears inherited `LANGCHAIN_*`, `LANGSMITH_*` and `OTEL_*` settings before loading the framework so the isolated probe does not enable another tracer. This is only the probe's behavior; the reusable callback handler does not change application environment or other handlers.

Two concurrent real LangChain pipelines run fixed fake chat models and a local tool. A third deliberately fails before the model. No real provider, retrieval service, Slack message or application write is involved. All tasks are explicitly synthetic in environment `test`. The example checks the outbox contains no private-content sentinels, then explicitly flushes its 30 observations. Acknowledgment does not verify storage: the output retains `storage_verified:false`. An existing outbox is refused; inspect it rather than rerunning work after an uncertain result.

Use an account/audit reader separately to freeze the `langchain_interop` search window, open all three results, and verify 6/6/3 spans, linked parents, two successful execution trees and one failed tree. Model usage, cost, checks and business outcome remain unknown. Repeat the same frozen reads to verify stability; never rerun the workload to fix a read timeout. This is framework interoperability proof, not native provider coverage, production performance, complete collection or useful customer outcomes.

Primary contracts: [LangChain callback source](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/callbacks/base.py), [chat start](https://reference.langchain.com/python/langchain-core/callbacks/base/CallbackManagerMixin/on_chat_model_start), and [model end](https://reference.langchain.com/python/langchain-core/callbacks/base/AsyncCallbackHandler/on_llm_end). Chat start closes through the LLM end/error callbacks. The installed package, rather than moving documentation, determines the tested contract.
