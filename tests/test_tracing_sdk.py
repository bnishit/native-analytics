import asyncio
import json
from pathlib import Path
import time
import unittest
import uuid
import test_analytics
from native_analytics import Collector
from afterspan_tracing import WorkflowTracer, TraceContext, inject, extract, mapped_id

class TracingSDKTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_analytics.AnalyticsTests();self.fixture.setUp()
        self.path=Path(self.fixture.temp.name)/'outbox.db'
        self.collector=Collector('https://analytics.example','synthetic-write-key',self.path)
        self.tracer=WorkflowTracer(self.collector)
    def tearDown(self):self.fixture.tearDown()
    def events(self):
        with self.collector.db() as c:return [json.loads(r[0]) for r in c.execute('SELECT payload FROM outbox ORDER BY created')]
    def test_explicit_not_run_emits_diagnostics_without_creating_evidence(self):
        with self.tracer.task('document_export','test'):
            with self.tracer.span('processing') as span:
                for options in [dict(execution='not_run',evidence_digest='a'*64),dict(execution='completed',reason_code='evidence_absent'),dict(reason_code='evidence_absent'),dict(execution='raw text')]:
                    with self.assertRaises(ValueError):span.check('bad','unknown',**options)
                with self.assertRaises(ValueError):span.check('bad','passed',execution='not_run')
                span.check('source_facts','unknown',execution='not_run',reason_code='evidence_absent')
                span.check('old','unknown')
        events=self.events();self.assertFalse(any(e['name']=='evidence.observed' for e in events))
        self.assertEqual(self.fixture.ingest(events).status_code,200)
        reviewed=self.fixture.client.post('/api/traces/review?project=alpha',headers=self.fixture.admin,json={'environment':'test'}).json()
        checks=reviewed['traces'][0]['checks'];self.assertEqual(checks[0]['execution'],'not_run');self.assertNotIn('execution',checks[1])

    def test_durable_sdk_events_round_trip_into_nested_trace_and_resolved_receipt(self):
        with self.tracer.task('document_export','test'):
            with self.tracer.span('processing',milestone='completion') as span:
                span.check('source_facts','passed',evidence_digest='a'*64,execution='completed')
            with self.tracer.span('response',milestone='delivery',disposition='delivered'):pass
        events=self.events();self.assertEqual(len(events),7)
        receipt=next(e['properties']['afterspan_evidence'] for e in events if e['name']=='evidence.observed')
        self.assertNotIn('execution',receipt);self.assertNotIn('reason_code',receipt)
        reopened=Collector('https://analytics.example','synthetic-write-key',self.path)
        self.assertEqual(reopened.status()['queued'],7)
        response=self.fixture.ingest(events);self.assertEqual(response.status_code,200,response.text)
        reviewed=self.fixture.client.post('/api/traces/review?project=alpha',headers=self.fixture.admin,json={'environment':'test'}).json()
        trace=reviewed['traces'][0]
        self.assertEqual(trace['checks'][0]['evidence_status'],'resolved')
        self.assertEqual(trace['checks'][0]['result'],'passed')
        self.assertEqual(trace['checks'][0]['execution'],'completed')
        self.assertEqual(trace['issues'],[])
        self.assertEqual(trace['completion']['response']['state'],'succeeded')
    def test_model_chunks_export_only_usage_and_first_meaningful_output(self):
        with self.tracer.task('model_probe','test'):
            with self.tracer.span('model') as span:
                span.model_usage(provider='openrouter',model='example/model',template_version='prompt-v1')
                span.observe_openai({'choices':[{'delta':{'role':'assistant','content':''}}]})
                span.observe_openai({'choices':[{'delta':{'content':'private output sentinel'}}]})
                span.observe_openai({'model':'example/resolved','choices':[], 'usage':{'prompt_tokens':4,'completion_tokens':2,'cost':'0.000000037125'}})
        terminal=next(e['properties']['afterspan_trace'] for e in self.events() if e['properties']['afterspan_trace'].get('model_usage'))
        usage=terminal['model_usage']
        self.assertEqual(usage['model'],'example/resolved');self.assertEqual(usage['input_count'],4)
        self.assertEqual(usage['cost_usd_decimal'],'0.000000037125');self.assertEqual(usage['usage_source'],'provider_reported')
        self.assertGreaterEqual(usage['first_output_ms'],0);self.assertLessEqual(usage['first_output_ms'],terminal['duration_ms'])
        self.assertNotIn('private output sentinel',json.dumps(self.events()))
        response=self.fixture.ingest(self.events());self.assertEqual(response.status_code,200,response.text)
        opened=self.fixture.client.post('/api/traces/review?project=alpha',headers=self.fixture.admin,json={'environment':'test'}).json()
        model=next(x for x in opened['traces'][0]['spans'] if x['step']=='model')
        self.assertEqual(model['model_usage']['cost_usd_decimal'],'0.000000037125')

    def test_empty_model_stream_keeps_timing_and_cost_unknown_and_closed_spans_reject_changes(self):
        with self.tracer.task('model_probe','test'):
            with self.tracer.span('model') as span:
                span.model_usage(provider='openai',model='example-model')
                span.observe_openai({'choices':[{'delta':{'role':'assistant','content':''}}],'usage':None})
        usage=next(e['properties']['afterspan_trace']['model_usage'] for e in self.events() if 'model_usage' in e['properties']['afterspan_trace'])
        self.assertNotIn('first_output_ms',usage);self.assertNotIn('cost_usd_decimal',usage);self.assertNotIn('input_count',usage)
        with self.assertRaises(ValueError):span.first_output()
        with self.assertRaises(ValueError):span.model_usage(output_count=1,usage_source='provider_reported')
        with self.tracer.task('model_probe','test') as other:
            with self.assertRaises(ValueError):other.model_usage(provider='openai',model='example')

    def test_context_roundtrip_requires_trust_and_retries_keep_original_clock(self):
        with self.tracer.task('document_export','test'):
            with self.tracer.span('http'):
                carrier=inject();self.assertIsNone(extract(carrier))
                context=extract(carrier,trusted=True);self.assertIsNotNone(context)
                self.assertEqual(inject(context),carrier)
                self.assertTrue(extract({**carrier,'traceparent':carrier['traceparent'][:-2]+'03'},trusted=True).sampled)
                self.assertFalse(extract({**carrier,'traceparent':carrier['traceparent'][:-2]+'02'},trusted=True).sampled)
        with self.tracer.task(context=context):pass
        retry=context.retry();self.assertEqual(retry.request_started_at_ms,context.request_started_at_ms)
        self.assertEqual(retry.trace_id,context.trace_id);self.assertEqual(retry.attempt_number,2)
        self.assertNotEqual(retry.attempt_id,context.attempt_id);self.assertIsNone(retry.span_id)
        self.assertEqual(inject(),{})
        response=self.fixture.ingest(self.events());self.assertEqual(response.status_code,200,response.text)
        reviewed=self.fixture.client.post('/api/traces/review?project=alpha',headers=self.fixture.admin,json={'environment':'test'}).json()
        self.assertEqual(reviewed['traces'][0]['issues'],[])
    def test_async_contexts_do_not_leak_between_tasks(self):
        async def work():
            with self.tracer.task('document_export','test'):
                before=inject();await asyncio.sleep(0)
                self.assertEqual(inject(),before)
                return before['traceparent'][3:35]
        async def main():return await asyncio.gather(work(),work())
        first,second=asyncio.run(main());self.assertNotEqual(first,second)
        self.assertEqual(inject(),{})
    def test_exceptions_are_rethrown_without_capturing_messages_and_transport_never_retries_work(self):
        with self.assertRaisesRegex(RuntimeError,'private-output'):
            with self.tracer.task('document_export','test'):
                with self.tracer.span('model'):raise RuntimeError('private-output')
        payload=json.dumps(self.events());self.assertNotIn('private-output',payload)
        self.assertTrue(any(e['properties']['afterspan_trace']['state']=='failed' for e in self.events()))
        class Broken:
            def track(self,*args,**kwargs):raise RuntimeError('not retained')
            def status(self):return {'queued':0}
        tracer=WorkflowTracer(Broken());calls=[]
        with tracer.task('document_export','test'):calls.append('executed')
        self.assertEqual(calls,['executed']);self.assertEqual(tracer.status()['emit_failures'],2)
    def test_unsampled_context_is_forwarded_without_exporting_and_mapping_is_stable(self):
        with self.tracer.task('document_export','test'):carrier=inject()
        before=self.collector.status()['queued'];carrier['traceparent']=carrier['traceparent'][:-2]+'00'
        context=extract(carrier,trusted=True);self.assertFalse(context.sampled)
        with self.tracer.task(context=context):self.assertTrue(inject()['traceparent'].endswith('00'))
        self.assertEqual(self.collector.status()['queued'],before)
        self.assertEqual(self.tracer.status()['sampled_out_events'],2)
        self.assertEqual(mapped_id('0123456789abcdef0123456789abcdef'),'5c577e0c-a1dc-4c34-91b8-35f2c8f7e98a')
        self.assertEqual(mapped_id('0123456789abcdef0123456789abcdef','0123456789abcdef'),'f2ca744a-5b55-4565-abbc-09020a62de45')

    def test_python_to_node_context_and_receipts_round_trip_through_real_ingestion(self):
        import subprocess
        with self.tracer.task('document_export','test'):
            with self.tracer.span('http'):carrier=inject()
        script="""
import {readFileSync} from 'node:fs';
import {createAnalytics} from './sdk/typescript/native-analytics.ts';
import {WorkflowTracer,extract} from './sdk/typescript/tracing-node.ts';
const events=[];const client=createAnalytics({url:'https://analytics.example',writeKey:'synthetic-key',source:'cross-language-test',fetch:async(_u,o)=>{const batch=JSON.parse(o.body);events.push(...batch.events);return new Response(JSON.stringify({accepted:batch.events.length,duplicates:0}),{status:200})}});
const tracer=new WorkflowTracer(client),context=extract(JSON.parse(readFileSync(0,'utf8')),true);
await tracer.task({context},async()=>{await tracer.span('model',s=>{s.modelUsage({provider:'openai',model:'example'});s.observeOpenAI({choices:[{delta:{content:'private cross-language output'}}]});s.modelUsage({input_count:3,output_count:2,usage_source:'provider_reported',cost_usd_decimal:'0.000000037125',cost_source:'provider_reported'})});await tracer.span('processing',s=>s.check('source_facts','passed',{evidenceDigest:'a'.repeat(64)}),{milestone:'completion'});await tracer.span('response',()=>{},{milestone:'delivery'})});
await tracer.flush();process.stdout.write(JSON.stringify(events));
"""
        run=subprocess.run(['node','--experimental-strip-types','--input-type=module','-e',script],input=json.dumps(carrier),cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True)
        self.assertEqual(run.returncode,0,run.stderr)
        events=self.events()+json.loads(run.stdout)
        self.assertNotIn('private cross-language output',run.stdout)
        model=next(e['properties']['afterspan_trace'] for e in events if e['properties']['afterspan_trace'].get('model_usage'))
        self.assertLessEqual(model['model_usage']['first_output_ms'],model['duration_ms'])
        self.assertEqual(model['model_usage']['cost_usd_decimal'],'0.000000037125')
        response=self.fixture.ingest(events);self.assertEqual(response.status_code,200,response.text)
        report=self.fixture.client.post('/api/traces/review?project=alpha',headers=self.fixture.admin,json={'environment':'test'}).json()
        self.assertEqual(len(report['traces']),1)
        self.assertEqual(report['traces'][0]['issues'],[])
        self.assertEqual(report['traces'][0]['checks'][0]['evidence_status'],'resolved')

    def test_invalid_propagation_and_closed_checks_fail_without_false_linkage(self):
        for carrier in [{'traceparent':'00-'+'0'*32+'-'+'1'*16+'-01','baggage':''},
                        {'traceparent':'bad','baggage':'raw private text'}, {'traceparent':'x'*2000}]:
            self.assertIsNone(extract(carrier,trusted=True))
        with self.tracer.task('document_export','test') as span:pass
        with self.assertRaises(ValueError):span.check('source_facts','passed',evidence_digest='a'*64)
        with self.assertRaises(ValueError):mapped_id('0'*32)

    def test_explicit_debug_preparation_redacts_before_upload_without_automatic_collection(self):
        import base64,os
        from unittest.mock import patch
        from afterspan_tracing import prepare_debug_snapshot
        with self.tracer.task('document_export','test'):
            with self.tracer.span('model') as span:
                with self.assertRaises(ValueError):prepare_debug_snapshot(span,{'prompt':'chosen'},1)
                snapshot=prepare_debug_snapshot(span,{'prompt':'Contact private@example.com or +91 98765 43210. Bearer private-value','password':'private-password','metadata':{'cookie':'private-cookie','status':'public'}},1,privacy_reviewed=True)
        self.assertEqual(snapshot['content'],{'prompt':'Contact [redacted] or [redacted]. [redacted]','metadata':{'status':'public'}})
        self.assertNotIn('prompt',json.dumps(self.events()))
        self.assertEqual(self.fixture.ingest(self.events()).status_code,200)
        policy={'enabled':True,'workflow':'document_export','environment':'test','privacy_reviewed':True}
        with patch.dict(os.environ,{'AFTERSPAN_CONNECTION_KEY':base64.urlsafe_b64encode(b'k'*32).decode()}):
            self.assertEqual(self.fixture.client.put('/api/debug/policy?project=alpha&revision=0',headers=self.fixture.admin,json=policy).status_code,200)
            response=self.fixture.client.post('/v1/debug',headers=self.fixture.a,json=snapshot)
            self.assertEqual(response.status_code,200,response.text)

    def test_declared_user_traffic_propagates_without_promoting_unknown_context(self):
        with self.tracer.task('document_export','test',traffic_kind='user'):
            carrier=inject();context=extract(carrier,trusted=True)
            self.assertEqual(context.traffic_kind,'user')
        with self.tracer.task('document_export','test'):
            self.assertEqual(extract(inject(),trusted=True).traffic_kind,'unknown')
        events=self.events();self.assertTrue(any(e['properties']['afterspan_trace']['traffic_kind']=='user' for e in events))
        self.assertEqual(self.fixture.ingest(events).status_code,200)

class ModelObservationTests(unittest.TestCase):
    def test_output_clock_ignores_accounting_and_keeps_first_signal(self):
        from afterspan_model import ModelObservation
        now=[100.0];o=ModelObservation('openai','example',clock=lambda:now[0])
        now[0]=110;o.observe_openai({'choices':[{'delta':{'role':'assistant'}}]})
        self.assertNotIn('first_output_ms',o.snapshot())
        now[0]=125;o.observe_openai({'choices':[{'delta':{'tool_calls':[{'function':{'arguments':'private tool sentinel'}}]}}]})
        now[0]=140;o.observe_openai({'choices':[{'delta':{'content':'private text sentinel'}}]})
        self.assertEqual(o.snapshot()['first_output_ms'],25)
        self.assertNotIn('private',json.dumps(o.snapshot()));o.close()
        with self.assertRaises(ValueError):o.first_output()

    def test_invalid_metadata_never_overwrites_valid_state_or_invents_cost(self):
        from afterspan_model import ModelObservation
        o=ModelObservation('openai','example')
        for fields in [{'prompt':'private sentinel'},{'first_output_ms':1},{'input_count':True,'usage_source':'provider_reported'},{'output_count':1},{'cost_usd_decimal':0.01,'cost_source':'provider_reported'},{'cost_usd_decimal':'0.01'},{'cost_usd_decimal':'0.1','cost_source':'price_table'}]:
            with self.assertRaises(ValueError):o.record(**fields)
        self.assertEqual(o.snapshot(),{'provider':'openai','model':'example','unit':'token'})
        o.record(input_count=4,usage_source='provider_reported',cost_usd_decimal='0.000000037125',cost_source='provider_reported')
        self.assertFalse(o.observe_openai({'usage':{'prompt_tokens':True}}));self.assertEqual(o.rejected_updates,1)
        self.assertNotIn('input_count',o.snapshot());self.assertNotIn('cost_usd_decimal',o.snapshot())
        o.observe_openai({'usage':{'prompt_tokens':0,'completion_tokens':0,'cost':0.01}})
        self.assertEqual(o.snapshot()['input_count'],0);self.assertNotIn('cost_usd_decimal',o.snapshot())
        for choices in ['malformed',{'delta':{}},None]:
            o.record(input_count=10,usage_source='provider_reported',cost_usd_decimal='0.1',cost_source='provider_reported')
            self.assertFalse(o.observe_openai({'choices':choices}))
            self.assertNotIn('input_count',o.snapshot());self.assertNotIn('cost_usd_decimal',o.snapshot())
