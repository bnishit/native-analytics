import gzip
import os
import subprocess
from pathlib import Path
from contextlib import contextmanager
import json
import socket
import threading
import time
import unittest
import uvicorn
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest, ExportTraceServiceResponse
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace import Status, StatusCode
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from tracing import ModelUsage
from otlp import mapped_id
import test_analytics

class OTLPTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_analytics.AnalyticsTests();self.fixture.setUp()
        self.client=self.fixture.client
    def tearDown(self):self.fixture.tearDown()
    def payload(self):
        message=ExportTraceServiceRequest();resource=message.resource_spans.add()
        for name,value in [('afterspan.workflow','document_export'),('afterspan.environment','test')]:
            a=resource.resource.attributes.add();a.key=name;a.value.string_value=value
        scope=resource.scope_spans.add();span=scope.spans.add()
        span.trace_id=bytes.fromhex('0123456789abcdef0123456789abcdef');span.span_id=bytes.fromhex('0123456789abcdef')
        span.name='private request body must be omitted'
        span.start_time_unix_nano=int((time.time()-2)*1e9);span.end_time_unix_nano=span.start_time_unix_nano+1000000000
        span.status.code=1;return message,span
    def send(self,message,headers=None):
        return self.client.post('/v1/traces',headers={**(headers or self.fixture.a),'Content-Type':'application/x-protobuf'},content=message.SerializeToString())
    def traces(self):
        r=self.client.post('/api/traces/search?project=alpha',headers=self.fixture.admin,json={'environment':'test'})
        self.assertEqual(r.status_code,200,r.text);return r.json()['traces']
    def detail(self):
        r=self.client.post('/api/traces/review?project=alpha',headers=self.fixture.admin,json={'environment':'test'})
        self.assertEqual(r.status_code,200,r.text);return r.json()['traces'][0]
    def test_protobuf_retries_are_idempotent_and_content_never_enters_store(self):
        message,span=self.payload();self.assertEqual(self.send(message).status_code,200)
        self.assertEqual(self.send(message).status_code,200)
        trace=self.detail();self.assertEqual(len(trace['spans']),1)
        self.assertEqual(trace['issues'],[]);self.assertIsNone(trace['request_started_at_ms'])
        with test_analytics.analytics.connect() as c:
            rows=c.execute('SELECT properties FROM events').fetchall()
        self.assertEqual(len(rows),2);self.assertNotIn('private request body',str(rows))
        self.assertEqual(mapped_id(span.trace_id.hex()),'5c577e0c-a1dc-4c34-91b8-35f2c8f7e98a')
        self.assertEqual(self.client.post('/v1/traces',content=message.SerializeToString()).status_code,401)
        self.assertEqual(self.send(message,self.fixture.admin).status_code,401)
    def test_partial_success_preserves_valid_span_and_reports_invalid_count(self):
        message,span=self.payload();bad=message.resource_spans[0].scope_spans[0].spans.add();bad.CopyFrom(span);bad.trace_id=b'\0'*16
        response=self.send(message);self.assertEqual(response.status_code,200)
        report=ExportTraceServiceResponse.FromString(response.content)
        self.assertEqual(report.partial_success.rejected_spans,1);self.assertEqual(len(self.traces()),1)
        self.assertNotIn('private',report.partial_success.error_message)
        with test_analytics.analytics.connect() as c:health=c.execute("SELECT rejected_spans,accepted_requests FROM collection_ingestion WHERE project='alpha' AND transport='otlp'").fetchone()
        self.assertEqual(health['rejected_spans'],1);self.assertEqual(health['accepted_requests'],1)

    def test_json_hex_ids_and_gzip_share_exact_identity_with_protobuf(self):
        message,span=self.payload();data=MessageToDict(message)
        s=data['resourceSpans'][0]['scopeSpans'][0]['spans'][0]
        s['traceId']=span.trace_id.hex();s['spanId']=span.span_id.hex()
        response=self.client.post('/v1/traces',headers={**self.fixture.a,'Content-Type':'application/json','Content-Encoding':'gzip'},content=gzip.compress(json.dumps(data).encode()))
        self.assertEqual(response.status_code,200,response.text);self.assertEqual(response.json(),{})
        self.assertEqual(self.send(message).status_code,200)
        with test_analytics.analytics.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM events').fetchone()[0],2)
        s['spanId']='bad'
        self.assertEqual(self.client.post('/v1/traces',headers=self.fixture.a,json=data).status_code,400)
        response=self.client.post('/v1/traces',headers={**self.fixture.a,'Content-Type':'application/json','Content-Encoding':'gzip'},content=gzip.compress(b' '*2000001))
        self.assertEqual(response.status_code,413)
    def test_unset_otel_status_is_unknown_and_conflicting_updates_stay_conflicts(self):
        message,span=self.payload();span.status.code=0;self.assertEqual(self.send(message).status_code,200)
        self.assertEqual(self.traces()[0]['status'],'gaps')
        request=self.traces()[0]
        opened=self.client.post('/api/traces/open?project=alpha',headers=self.fixture.admin,json={'environment':'test','reference':request['reference']}).json()['trace']
        self.assertIsNotNone(opened['spans'][0]['end_ms'])
        self.assertEqual(opened['spans'][0]['state'],'unknown')
        span.status.code=1;self.assertEqual(self.send(message).status_code,200)
        self.assertIn('span_conflict',self.detail()['issues'])
    def test_model_usage_requires_provenance_and_rejects_content(self):
        base={'provider':'example','model':'model-v1','input_count':100,'output_count':5,'usage_source':'provider_reported',
              'cost_microusd':0,'cost_source':'price_table','price_version':'2026-11','first_output_ms':10}
        self.assertEqual(ModelUsage.model_validate(base).cost_microusd,0)
        for change in [{'input_count':True},{'cost_source':None},{'price_version':None},{'cache_read_count':101},{'usage_source':None},{'prompt':'private'},{'first_output_ms':float('nan')}]:
            with self.assertRaises(ValueError):ModelUsage.model_validate({**base,**change})
        message,span=self.payload()
        for key,value in [('gen_ai.provider.name','example'),('gen_ai.request.model','model-v1'),('gen_ai.usage.input_tokens',100),('gen_ai.usage.output_tokens',5),('gen_ai.input.messages','private prompt')]:
            a=span.attributes.add();a.key=key
            if isinstance(value,int):a.value.int_value=value
            else:a.value.string_value=value
        self.assertEqual(self.send(message).status_code,200)
        usage=self.detail()['spans'][0]['model_usage']
        self.assertEqual(usage['input_count'],100);self.assertIsNone(usage['cost_microusd'])
        self.assertNotIn('private prompt',json.dumps(self.detail()))
    def test_decimal_model_charge_roundtrips_without_float_conversion(self):
        message,span=self.payload()
        for key,value in [('gen_ai.provider.name','example'),('gen_ai.request.model','model-v1'),
            ('afterspan.model.cost_usd_decimal','0.000000037125'),('afterspan.model.cost_source','provider_reported')]:
            a=span.attributes.add();a.key=key;a.value.string_value=value
        self.assertEqual(self.send(message).status_code,200)
        usage=self.detail()['spans'][0]['model_usage']
        self.assertEqual(usage['cost_usd_decimal'],'0.000000037125');self.assertIsNone(usage['cost_microusd'])
        span.attributes[-2].value.string_value='0.0000000000001'
        response=ExportTraceServiceResponse.FromString(self.send(message).content)
        self.assertEqual(response.partial_success.rejected_spans,1)

    @contextmanager
    def http_service(self):
        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen();port=sock.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(test_analytics.analytics.app,log_level='critical',lifespan='off'))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
        try:
            deadline=time.monotonic()+5
            while not server.started and time.monotonic()<deadline:time.sleep(.01)
            self.assertTrue(server.started)
            yield port
        finally:
            server.should_exit=True;thread.join(5);sock.close()

    def test_official_exporter_over_real_http_produces_nested_trace(self):
        with self.http_service() as port:
            provider=TracerProvider(resource=Resource.create({'afterspan.workflow':'document_export','afterspan.environment':'test'}))
            try:
                provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=f'http://127.0.0.1:{port}/v1/traces',headers=self.fixture.a,timeout=3)))
                tracer=provider.get_tracer('afterspan-integration-test')
                with tracer.start_as_current_span('private outer name') as outer:
                    with tracer.start_as_current_span('private inner name') as inner:
                        inner.set_attribute('afterspan.step','storage');inner.set_status(Status(StatusCode.OK))
                    outer.set_status(Status(StatusCode.OK))
                provider.force_flush()
                trace=self.detail();self.assertEqual(len(trace['spans']),2)
                self.assertEqual(trace['issues'],[]);self.assertEqual(self.traces()[0]['status'],'observed')
                self.assertEqual(sum(s['parent'] is not None for s in trace['spans']),1)
            finally:provider.shutdown()

    @unittest.skipUnless(os.environ.get('AFTERSPAN_OTEL_NODE_MODULES'),'optional official Node consumer is not installed')
    def test_official_node_http_instrumentation_keeps_parallel_requests_separate(self):
        script=Path(__file__).resolve().parents[1]/'examples/otel-node-http/emit.cjs'
        env={'PATH':os.environ['PATH'],'NODE_PATH':os.environ['AFTERSPAN_OTEL_NODE_MODULES']}
        with self.http_service() as port:
            config={'origin':f'http://127.0.0.1:{port}','key':self.fixture.a['Authorization'].split(' ',1)[1]}
            result=subprocess.run(['node',str(script)],input=json.dumps(config),text=True,capture_output=True,env=env,timeout=40)
            self.assertEqual(result.returncode,0,result.stderr)
            emitted=json.loads(result.stdout)
            self.assertFalse(emitted['storage_verified'])
            self.assertEqual(emitted['export_results'],[0])
            self.assertEqual(emitted['propagated_requests'],2)
            rows=self.client.post('/api/traces/review?project=alpha',headers=self.fixture.admin,json={'environment':'test'}).json()['traces']
            self.assertEqual(len(rows),2)
            for row in rows:
                self.assertEqual(row['traffic_kind'],'synthetic');self.assertEqual(row['workflow'],'node_http_interop')
                self.assertEqual(row['issues'],[]);self.assertEqual(row['checks'],[])
                self.assertEqual(len(row['spans']),4);self.assertEqual(sum(s['parent'] is not None for s in row['spans']),3)
                self.assertEqual(sorted(s['step'] for s in row['spans']),['http','processing','processing','receive'])
                self.assertEqual(sorted(s['state'] for s in row['spans']),['succeeded','succeeded','unknown','unknown'])
                self.assertTrue(all(s['state']=='unknown' for s in row['spans'] if s['step'] in ('http','receive')))
                self.assertIsNone(row['request_started_at_ms']);self.assertEqual(row['business_outcome'],'not_established')
            with test_analytics.analytics.connect() as c:
                stored=' '.join(r[0] for r in c.execute('SELECT properties FROM events'))
                self.assertEqual(c.execute('SELECT count(*) FROM events').fetchone()[0],16)
            for canary in ['synthetic-private-path','omit-me','synthetic inner work','synthetic concurrent request','127.0.0.1','traceparent']:
                self.assertNotIn(canary,stored)
            denied=subprocess.run(['node',str(script)],input=json.dumps({**config,'key':'invalid-producer-key'}),text=True,capture_output=True,env=env,timeout=40)
            self.assertNotEqual(denied.returncode,0);self.assertTrue(denied.stderr.strip().endswith('node_otlp_validation_failed'));self.assertNotIn('invalid-producer-key',denied.stderr)
            malformed=subprocess.run(['node',str(script)],input='{"key":"private-canary",bad',text=True,capture_output=True,env=env,timeout=5)
            self.assertNotEqual(malformed.returncode,0);self.assertEqual(malformed.stderr.strip(),'invalid_configuration')

    def test_opaque_subject_is_retained_for_erasure_and_erased_trace_cannot_return_anonymously(self):
        subject='account_'+'a'*64;message,span=self.payload()
        attr=message.resource_spans[0].resource.attributes.add();attr.key='afterspan.subject_id';attr.value.string_value=subject
        response=self.send(message);self.assertEqual(response.status_code,200,response.text)
        with test_analytics.analytics.connect() as c:self.assertEqual({r[0] for r in c.execute('SELECT distinct_id FROM events')},{subject})
        self.assertNotIn(subject,json.dumps(self.detail()))
        self.assertEqual(self.send(message).status_code,200)
        erased=self.client.post('/v1/erase',headers=self.fixture.a,json={'distinct_id':subject});self.assertEqual(erased.json()['erased_events'],2)
        del message.resource_spans[0].resource.attributes[-1]
        self.assertEqual(self.send(message).status_code,200);self.assertEqual(self.traces(),[])
    def test_invalid_explicit_subject_is_rejected_instead_of_falling_back_to_anonymous(self):
        for kind,value in [('string_value','person@example.com'),('bool_value',True),('array_value',None)]:
            message,span=self.payload();attr=span.attributes.add();attr.key='afterspan.subject_id'
            if value is not None:setattr(attr.value,kind,value)
            else:attr.value.array_value.values.add().string_value='invalid'
            response=self.send(message);self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(ExportTraceServiceResponse.FromString(response.content).partial_success.rejected_spans,1)
        self.assertEqual(self.traces(),[])
    def test_exported_subject_change_cannot_hide_behind_duplicate_event_ids(self):
        message,span=self.payload();attr=span.attributes.add();attr.key='afterspan.subject_id';attr.value.string_value='account_'+'a'*64
        self.assertEqual(self.send(message).status_code,200)
        attr.value.string_value='account_'+'b'*64
        response=self.send(message);self.assertEqual(response.status_code,409,response.text)
        with test_analytics.analytics.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM events').fetchone()[0],2)
