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
    def test_durable_sdk_events_round_trip_into_nested_trace_and_resolved_receipt(self):
        with self.tracer.task('document_export','test'):
            with self.tracer.span('processing',milestone='completion') as span:
                span.check('source_facts','passed',evidence_digest='a'*64)
            with self.tracer.span('response',milestone='delivery',disposition='delivered'):pass
        events=self.events();self.assertEqual(len(events),7)
        reopened=Collector('https://analytics.example','synthetic-write-key',self.path)
        self.assertEqual(reopened.status()['queued'],7)
        response=self.fixture.ingest(events);self.assertEqual(response.status_code,200,response.text)
        reviewed=self.fixture.client.post('/api/traces/review?project=alpha',headers=self.fixture.admin,json={'environment':'test'}).json()
        trace=reviewed['traces'][0]
        self.assertEqual(trace['checks'][0]['evidence_status'],'resolved')
        self.assertEqual(trace['checks'][0]['result'],'passed')
        self.assertEqual(trace['issues'],[])
        self.assertEqual(trace['completion']['response']['state'],'succeeded')
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
await tracer.task({context},async()=>{await tracer.span('processing',s=>s.check('source_facts','passed',{evidenceDigest:'a'.repeat(64)}),{milestone:'completion'});await tracer.span('response',()=>{},{milestone:'delivery'})});
await tracer.flush();process.stdout.write(JSON.stringify(events));
"""
        run=subprocess.run(['node','--experimental-strip-types','--input-type=module','-e',script],input=json.dumps(carrier),cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True)
        self.assertEqual(run.returncode,0,run.stderr)
        events=self.events()+json.loads(run.stdout)
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
