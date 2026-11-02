import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createAnalytics} from './native-analytics.ts';
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
