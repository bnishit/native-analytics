import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import runpy
import unittest
import uuid
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage,AIMessageChunk
from langchain_core.outputs import ChatGeneration,ChatGenerationChunk,LLMResult
from langchain_core.runnables import RunnableLambda,RunnableParallel
import test_tracing_sdk
from afterspan_langchain import AfterspanCallbackHandler
from afterspan_tracing import TraceContext,inject


class LangChainTests(unittest.TestCase):
    setUp=test_tracing_sdk.TracingSDKTests.setUp
    tearDown=test_tracing_sdk.TracingSDKTests.tearDown
    events=test_tracing_sdk.TracingSDKTests.events
    def test_shipped_example_uses_real_framework_callbacks_and_service(self):
        result=asyncio.run(runpy.run_path('examples/langchain/emit.py')['exercise'](self.tracer))
        self.assertEqual(sum(r['framework_failed'] for r in result),1)
        self.assertEqual(len(self.events()),30)
        self.assertNotIn('private',json.dumps(self.events()))
        self.assertEqual(self.fixture.ingest(self.events()).status_code,200)
        traces=self.fixture.client.post('/api/traces/review?project=alpha',headers=self.fixture.admin,json={'environment':'test'}).json()['traces']
        self.assertEqual(sorted(len(t['spans']) for t in traces),[3,6,6])
        for t in traces:
            ids={s['span'] for s in t['spans']}
            self.assertTrue(all(s['parent'] is None or s['parent'] in ids for s in t['spans']))
            self.assertEqual(t['business_outcome'],'not_established')

    def test_actual_stream_failure_keeps_first_output_and_failure(self):
        model=FakeListChatModel(responses=['private answer'],error_on_chunk_number=1).with_config(metadata={'ls_model_name':'fixture'})
        with self.tracer.task('framework_stream','test') as root:
            with AfterspanCallbackHandler(self.tracer,parent=root.context,models={('fakelistchatmodel','fixture')}) as handler:
                with self.assertRaises(Exception):list(model.stream('private prompt',config={'callbacks':[handler]}))
                self.assertEqual(handler.status()['active_runs'],0)
        terminal=next(e['properties']['afterspan_trace'] for e in self.events() if e['properties']['afterspan_trace']['step']=='model' and e['properties']['afterspan_trace']['state']!='started')
        self.assertEqual(terminal['state'],'failed');self.assertIn('first_output_ms',terminal['model_usage'])
        self.assertNotIn('input_count',terminal['model_usage']);self.assertNotIn('private',json.dumps(self.events()))
        self.assertEqual(self.fixture.ingest(self.events()).status_code,200)
    def test_framework_pipeline_preserves_real_nesting_and_omits_content(self):
        model=FakeListChatModel(responses=['private answer sentinel'])
        pipeline=RunnableLambda(lambda x:x)|model|RunnableLambda(lambda x:x.content)
        with self.tracer.task('langchain_probe','test',traffic_kind='synthetic') as root:
            with AfterspanCallbackHandler(self.tracer,parent=root.context) as handler:
                result=pipeline.invoke('private prompt sentinel',config={'callbacks':[handler],'run_name':'private run sentinel','metadata':{'private':'private metadata sentinel'}})
                self.assertEqual(result,'private answer sentinel')
                self.assertEqual(handler.status()['active_runs'],0)
        events=self.events();self.assertEqual(len(events),10)
        raw=json.dumps(events)
        for text in ('private answer sentinel','private prompt sentinel','private run sentinel','private metadata sentinel'):self.assertNotIn(text,raw)
        self.assertEqual(self.fixture.ingest(events).status_code,200)
        trace=self.fixture.client.post('/api/traces/review?project=alpha',headers=self.fixture.admin,json={'environment':'test'}).json()['traces'][0]
        self.assertEqual(len(trace['spans']),5)
        self.assertTrue(all(s['state']=='succeeded' for s in trace['spans']))
        self.assertEqual(trace['checks'],[])
        self.assertFalse(any('milestone' in e['properties']['afterspan_trace'] for e in events))
        terminals=[e['properties']['afterspan_trace'] for e in events if e['properties']['afterspan_trace']['state']!='started']
        root_id=root.facts()['span_id'];chain=next(x for x in terminals if x['parent_span_id']==root_id)
        self.assertEqual(sum(x['parent_span_id']==chain['span_id'] for x in terminals),3)

    def test_real_async_overlap_and_failure_remain_separate(self):
        async def run(fail):
            async def operation(value):
                await asyncio.sleep(.005)
                if fail:raise ValueError('private failure sentinel')
                return value
            pipeline=RunnableParallel(left=RunnableLambda(operation),right=RunnableLambda(operation))
            with self.tracer.task('langchain_async','test',traffic_kind='synthetic') as root:
                before=inject()
                with AfterspanCallbackHandler(self.tracer,parent=root.context) as handler:
                    try:await pipeline.ainvoke('private input sentinel',config={'callbacks':[handler]})
                    except ValueError:
                        if not fail:raise
                    self.assertEqual(inject(),before)
                return handler.status()
        async def both():return await asyncio.gather(run(False),run(True))
        states=asyncio.run(both());self.assertTrue(all(s['unfinished_runs']==0 for s in states))
        events=self.events();self.assertNotIn('private',json.dumps(events))
        terminals=[e['properties']['afterspan_trace'] for e in events if e['properties']['afterspan_trace']['state']!='started']
        self.assertEqual(len({x['trace_id'] for x in terminals}),2)
        self.assertEqual(sum(x['state']=='failed' for x in terminals),3)
        for x in terminals:
            if x['parent_span_id']:self.assertTrue(any(p['span_id']==x['parent_span_id'] and p['trace_id']==x['trace_id'] for p in terminals))
        self.assertEqual(self.fixture.ingest(events).status_code,200)

    def test_callbacks_cross_threads_unknown_cleanup_and_sampled_out(self):
        with self.tracer.task('callback_probe','test') as root:
            handler=AfterspanCallbackHandler(self.tracer,parent=root.context)
            run_id=uuid.uuid4()
            with ThreadPoolExecutor(max_workers=1) as pool:pool.submit(handler.on_tool_start,{},'private tool input',run_id=run_id).result()
            handler.on_tool_end('private output',run_id=run_id)
            handler.on_retriever_start({},'private query',run_id=uuid.uuid4())
            handler.close();handler.close()
            self.assertEqual(handler.status()['unfinished_runs'],1)
            self.assertEqual(handler.status()['active_runs'],0)
        terminal=[e['properties']['afterspan_trace'] for e in self.events() if e['properties']['afterspan_trace']['state']!='started']
        self.assertEqual(next(x for x in terminal if x['step']=='retrieval')['state'],'unknown')
        self.assertEqual(next(x for x in terminal if x['step']=='tool')['state'],'succeeded')
        self.assertEqual(self.fixture.ingest(self.events()).status_code,200)
        count=len(self.events())
        context=TraceContext('a'*32,str(uuid.uuid4()),1,'sampled_out','test',1,sampled=False)
        with self.tracer.task(context=context) as root:
            with AfterspanCallbackHandler(self.tracer,parent=root.context) as handler:
                handler.on_tool_start({},'private',run_id=run_id)
                handler.on_tool_end('private',run_id=run_id)
        self.assertEqual(len(self.events()),count)

    def test_model_identity_usage_opt_in_and_meaningful_output_only(self):
        def response(usage):return LLMResult(generations=[[ChatGeneration(message=AIMessage(content='private',usage_metadata=usage))]])
        usage={'input_tokens':7,'output_tokens':3,'total_tokens':10,'input_token_details':{'cache_read':0},'output_token_details':{'reasoning':1}}
        with self.tracer.task('model_callbacks','test') as root:
            with AfterspanCallbackHandler(self.tracer,parent=root.context,models={('synthetic','fixture')},reported_usage=True) as handler:
                rid=uuid.uuid4();handler.on_chat_model_start({},[['private']],run_id=rid,metadata={'ls_provider':'synthetic','ls_model_name':'fixture'})
                handler.on_llm_new_token('thinking',run_id=rid,chunk=ChatGenerationChunk(message=AIMessageChunk(content=[{'type':'reasoning','reasoning':'private'}])))
                span=handler._runs[rid]['span'];self.assertIsNone(span._model.snapshot().get('first_output_ms'))
                handler.on_llm_new_token('private',run_id=rid,chunk=ChatGenerationChunk(message=AIMessageChunk(content='private')))
                handler.on_llm_end(response(usage),run_id=rid)
                handler.on_llm_end(response(usage),run_id=rid)
            with AfterspanCallbackHandler(self.tracer,parent=root.context,models={('synthetic','fixture')}) as default:
                default.on_chat_model_start({},[],run_id=rid,metadata={'ls_provider':'synthetic','ls_model_name':'fixture'})
                default.on_llm_end(response(usage),run_id=rid)
        observations=[e['properties']['afterspan_trace']['model_usage'] for e in self.events() if 'model_usage' in e['properties']['afterspan_trace']]
        self.assertEqual(observations[0]['input_count'],7);self.assertEqual(observations[0]['cache_read_count'],0)
        self.assertEqual(observations[0]['reasoning_count'],1);self.assertIn('first_output_ms',observations[0]);self.assertNotIn('cost_usd_decimal',observations[0])
        self.assertNotIn('input_count',observations[1]);self.assertEqual(handler.status()['unmatched_ends'],1)
        self.assertNotIn('private',json.dumps(self.events()));self.assertEqual(self.fixture.ingest(self.events()).status_code,200)

    def test_missing_parent_duplicate_and_capacity_are_bounded(self):
        with self.tracer.task('bounded_callbacks','test') as root:
            with AfterspanCallbackHandler(self.tracer,parent=root.context) as handler:
                rid=uuid.uuid4();handler.on_chain_start({},None,run_id=rid,parent_run_id=uuid.uuid4())
                handler.on_chain_start({},None,run_id=rid)
                handler.on_chain_start({},None,run_id=rid)
                handler.on_chain_end(None,run_id=rid)
                for _ in range(1023):
                    other=uuid.uuid4();handler.on_chain_start({},None,run_id=other);handler.on_chain_end(None,run_id=other)
                handler.on_chain_start({},None,run_id=uuid.uuid4())
                self.assertEqual(handler.status()['tracked_runs'],1024);self.assertEqual(handler.status()['dropped_starts'],3)
        self.assertEqual(handler.status()['tracked_runs'],0)

