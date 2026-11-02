import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createAnalytics} from './native-analytics.ts';
import {ModelObservation} from './model-observation.ts';
import {WorkflowTracer,mappedId,inject,extract,retryContext,prepareDebugSnapshot,redactDebugContent} from './tracing-node.ts';

function fixture(){
 const events:any[]=[];
 const client=createAnalytics({url:'https://analytics.example',writeKey:'synthetic-key',source:'sdk-test',fetch:async(_url,options)=>{
  const batch=JSON.parse(String(options?.body));events.push(...batch.events);
  return new Response(JSON.stringify({accepted:batch.events.length,duplicates:0}),{status:200});
 }});
 return {events,client,tracer:new WorkflowTracer(client)};
}
test('nested workflow emits typed receipts and does not add product source metadata',async()=>{
 const {tracer,events}=fixture();
 await tracer.task({workflow:'document_export',environment:'test'},async()=>{
  await tracer.span('processing',span=>span.check('source_facts','passed',{evidenceDigest:'a'.repeat(64)}),{milestone:'completion'});
  await tracer.span('response',()=>{}, {milestone:'delivery',disposition:'delivered'});
 });await tracer.flush();
 assert.equal(events.length,7);
 assert.ok(events.every(e=>Object.keys(e.properties).length===1));
 const receipt=events.find(e=>e.name==='evidence.observed').properties.afterspan_evidence;
 const checked=events.find(e=>e.properties.afterspan_trace?.checks)?.properties.afterspan_trace;
 assert.equal(checked.checks[0].evidence_ref,receipt.receipt_id);
 assert.equal(checked.span_id,receipt.span_id);assert.equal(checked.trace_id,receipt.trace_id);
 const root=events[0].properties.afterspan_trace;
 assert.equal(events[1].properties.afterspan_trace.parent_span_id,root.span_id);
 assert.equal(tracer.status().queued,0);
});
test('async task context isolates concurrent runs and propagation requires trust',async()=>{
 const {tracer}=fixture();
 const work=()=>tracer.task({workflow:'document_export',environment:'test'},async()=>{
  const before=inject();await Promise.resolve();assert.deepEqual(inject(),before);
  assert.equal(extract(before),undefined);const context=extract(before,true)!;
  assert.deepEqual(inject(context),before);
  const retry=retryContext(context);assert.equal(retry.requestStartedAtMs,context.requestStartedAtMs);assert.equal(retry.traceId,context.traceId);assert.equal(retry.attemptNumber,2);assert.notEqual(retry.attemptId,context.attemptId);assert.equal(retry.spanId,undefined);
  return context.traceId;
 });
 const [a,b]=await Promise.all([work(),work()]);assert.notEqual(a,b);assert.deepEqual(inject(),{});await tracer.flush();
});
test('application exceptions survive and their messages never enter telemetry',async()=>{
 const {tracer,events}=fixture();
 await assert.rejects(tracer.task({workflow:'document_export'},()=>{throw Error('private-output')}),/private-output/);
 await tracer.flush();assert.ok(events.some(e=>e.properties.afterspan_trace?.state==='failed'));assert.ok(!JSON.stringify(events).includes('private-output'));
});
test('unsampled context remains unsampled without exporting events',async()=>{
 const {tracer,events}=fixture();let carrier:Record<string,string>={};
 await tracer.task({workflow:'document_export'},()=>{carrier=inject()});await tracer.flush();events.length=0;
 carrier.traceparent=carrier.traceparent.slice(0,-2)+'00';const context=extract(carrier,true)!;assert.equal(context.sampled,false);
 await tracer.task({context},()=>{assert.ok(inject().traceparent.endsWith('00'))});await tracer.flush();
 assert.equal(events.length,0);assert.equal(tracer.status().sampled_out_events,2);
});
test('invalid and zero IDs cannot link a task, closed checks cannot emit receipts',async()=>{
 assert.throws(()=>mappedId('0'.repeat(32)));assert.equal(extract({traceparent:'bad'},true),undefined);
 const {tracer}=fixture();let span:any;
 await tracer.task({workflow:'document_export'},value=>{span=value});assert.throws(()=>span.check('source_facts','passed'));await tracer.flush();
});

test('W3C identity mapping matches the pinned Python/OTLP vectors',()=>{
 assert.equal(mappedId('0123456789abcdef0123456789abcdef'),'5c577e0c-a1dc-4c34-91b8-35f2c8f7e98a');
 assert.equal(mappedId('0123456789abcdef0123456789abcdef','0123456789abcdef'),'f2ca744a-5b55-4565-abbc-09020a62de45');
});


test('W3C flags use the sampled bit and ignore reserved bits',async()=>{
 const tracer=new WorkflowTracer(createAnalytics());
 await tracer.task({workflow:'document_export',environment:'test'},()=>{
  const carrier=inject();
  assert.equal(extract({...carrier,traceparent:carrier.traceparent.slice(0,-2)+'03'},true)?.sampled,true);
  assert.equal(extract({...carrier,traceparent:carrier.traceparent.slice(0,-2)+'02'},true)?.sampled,false);
 });
});


test('debug snapshots require explicit review, redact before upload, and never enter event collection',async()=>{
 const f=fixture();let snapshot:any;
 await f.tracer.task({workflow:'document_export',environment:'test'},async()=>{
  await f.tracer.span('model',span=>{
   assert.throws(()=>prepareDebugSnapshot(span,{prompt:'chosen'},1));
   snapshot=prepareDebugSnapshot(span,{prompt:'Contact private@example.com or +91 98765 43210. Bearer private-value',password:'private-password',metadata:{cookie:'private-cookie',status:'public'}},1,{privacyReviewed:true});
  });
 });await f.tracer.flush();
 assert.deepEqual(snapshot.content,{prompt:'Contact [redacted] or [redacted]. [redacted]',metadata:{status:'public'}});
 assert.equal(snapshot.policy_revision,1);assert.equal(snapshot.privacy_reviewed,true);
 assert.equal(JSON.stringify(snapshot).includes('private'),false);
 assert.equal(f.events.some(e=>e.properties.afterspan_debug!==undefined),false);
 assert.equal(f.events.some(e=>JSON.stringify(e).includes('prompt')),false);
 assert.deepEqual(redactDebugContent(snapshot.content),snapshot.content);
 for(const value of [{x:Infinity},{x:'a'.repeat(4001)},{x:new Date()},{x:undefined}])assert.throws(()=>redactDebugContent(value));
});


test('traffic kind is explicit, propagated, and unknown by default',async()=>{
 const f=fixture();await f.tracer.task({workflow:'document_export',environment:'test',trafficKind:'user'},()=>{
  assert.equal(extract(inject(),true)?.trafficKind,'user');
 });await f.tracer.task({workflow:'document_export',environment:'test'},()=>{
  assert.equal(extract(inject(),true)?.trafficKind,'unknown');
 });await f.tracer.flush();
 assert.equal(f.events[0].properties.afterspan_trace.traffic_kind,'user');
 assert.equal(f.events.at(-1).properties.afterspan_trace.traffic_kind,'unknown');
});

test('model chunk observations keep exact accounting and first output, never content',async()=>{
 const {tracer,events}=fixture();let modelSpan:any;
 await tracer.task({workflow:'model_probe',environment:'test'},async()=>{
  await tracer.span('model',span=>{
   modelSpan=span;span.modelUsage({provider:'openrouter',model:'example/requested',template_version:'prompt-v1'});
   span.observeOpenAI({choices:[{delta:{role:'assistant',content:''}}]});
   span.observeOpenAI({choices:[{delta:{content:'private output sentinel'}}]});
   span.observeOpenAI({model:'example/resolved',choices:[],usage:{prompt_tokens:4,completion_tokens:2,cost:'0.000000037125'}});
  });
 });await tracer.flush();
 const terminal=events.find(e=>e.properties.afterspan_trace?.model_usage).properties.afterspan_trace;
 assert.equal(terminal.model_usage.model,'example/resolved');assert.equal(terminal.model_usage.cost_usd_decimal,'0.000000037125');
 assert.equal(terminal.model_usage.input_count,4);assert.ok(terminal.model_usage.first_output_ms>=0&&terminal.model_usage.first_output_ms<=terminal.duration_ms);
 assert.ok(!JSON.stringify(events).includes('private output sentinel'));
 assert.throws(()=>modelSpan.firstOutput());assert.throws(()=>modelSpan.modelUsage({output_count:1,usage_source:'provider_reported'}));
});
test('first model output clock ignores role and usage frames, and snapshot cannot mutate metadata',()=>{
 let now=100;const o=new ModelObservation({provider:'openai',model:'example'},{clock:()=>now});
 now=110;o.observeOpenAI({choices:[{delta:{role:'assistant',content:''}}]});assert.equal(o.snapshot().first_output_ms,undefined);
 now=125;o.observeOpenAI({choices:[{delta:{tool_calls:[{function:{arguments:'private tool sentinel'}}]}}]});
 now=140;o.observeOpenAI({choices:[{delta:{refusal:'private refusal sentinel'}}]});
 assert.equal(o.snapshot().first_output_ms,25);assert.ok(!JSON.stringify(o.snapshot()).includes('private'));
 const copy=o.snapshot();copy.model='changed';assert.equal(o.snapshot().model,'example');o.close();assert.throws(()=>o.firstOutput());
});
test('invalid accounting and content fields cannot enter model metadata or invent a zero bill',()=>{
 const o=new ModelObservation({provider:'openai',model:'example'});
 for(const update of [{prompt:'private'},{first_output_ms:1},{input_count:true,usage_source:'provider_reported'},{output_count:1},{cost_usd_decimal:.01,cost_source:'provider_reported'},{cost_usd_decimal:'0.01'},{cost_usd_decimal:'0.1',cost_source:'price_table'}])assert.throws(()=>o.record(update as any));
 assert.deepEqual(o.snapshot(),{provider:'openai',model:'example',unit:'token'});
 o.record({input_count:4,usage_source:'provider_reported',cost_usd_decimal:'0.000000037125',cost_source:'provider_reported'});
 assert.equal(o.observeOpenAI({usage:{prompt_tokens:true}}),false);assert.equal(o.rejectedUpdates,1);
 assert.equal(o.snapshot().input_count,undefined);assert.equal(o.snapshot().cost_usd_decimal,undefined);
 o.observeOpenAI({usage:{prompt_tokens:0,completion_tokens:0,cost:.01}});
 assert.equal(o.snapshot().input_count,0);assert.equal(o.snapshot().cost_usd_decimal,undefined);
});
test('empty streams and non-model spans cannot manufacture model timing',async()=>{
 const {tracer,events}=fixture();
 await tracer.task({workflow:'model_probe',environment:'test'},async root=>{
  assert.throws(()=>root.modelUsage({provider:'openai',model:'example'}));
  await tracer.span('model',span=>{span.modelUsage({provider:'openai',model:'example'});span.observeOpenAI({choices:[{delta:{role:'assistant',content:''}}],usage:null})});
 });await tracer.flush();const usage=events.find(e=>e.properties.afterspan_trace?.model_usage).properties.afterspan_trace.model_usage;
 assert.equal(usage.first_output_ms,undefined);assert.equal(usage.input_count,undefined);assert.equal(usage.cost_usd_decimal,undefined);
});

test('check execution diagnostics preserve unknown and never create evidence',async()=>{
 const {tracer,events}=fixture();
 await tracer.task({workflow:'document_export',environment:'test'},async()=>{
  await tracer.span('processing',span=>{
   for(const options of [{execution:'not_run',evidenceDigest:'a'.repeat(64)},{execution:'completed',reasonCode:'evidence_absent'},{reasonCode:'evidence_absent'},{execution:'raw text'}])assert.throws(()=>span.check('bad','unknown',options as any));
   assert.throws(()=>span.check('bad','passed',{execution:'not_run'}));
   span.check('source_facts','unknown',{execution:'not_run',reasonCode:'evidence_absent'});
   span.check('old','unknown');
  });
 });await tracer.flush();
 assert.ok(!events.some(e=>e.name==='evidence.observed'));
 const checks=events.find(e=>e.properties.afterspan_trace?.checks)?.properties.afterspan_trace.checks;
 assert.equal(checks[0].execution,'not_run');assert.equal(checks[0].reason_code,'evidence_absent');assert.ok(!('execution' in checks[1]));
});
