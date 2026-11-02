import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { writeFileSync } from 'node:fs';
import { createAnalytics, WorkflowTracer, ModelObservation } from '@afterspan/sdk';
import { ModelObservation as standalone } from '@afterspan/sdk/model';
assert.equal(ModelObservation, standalone);
const events=[];
const client=createAnalytics({url:'https://analytics.invalid',writeKey:'synthetic-package-key',fetch:async(_url,options)=>{
  const batch=JSON.parse(options.body).events;events.push(...batch);
  return new Response(JSON.stringify({accepted:batch.length,duplicates:0}));
}});
const tracer=new WorkflowTracer(client);
await tracer.task({workflow:'package_probe',environment:'test',trafficKind:'synthetic'},async()=>{
  await tracer.span('model',span=>{
    span.modelUsage({provider:'openai',model:'fixture/model'});
    span.observeOpenAI({choices:[{delta:{content:'private-content-sentinel'}}]});
    span.modelUsage({input_count:4,output_count:2,usage_source:'provider_reported',cost_usd_decimal:'0.000000254',cost_source:'provider_reported'});
  });
  await tracer.span('model',span=>{
    span.modelUsage({provider:'openai',model:'fixture/responses'});
    span.observeResponses({type:'response.output_text.delta',delta:'private-content-sentinel'});
    span.observeResponses({type:'response.completed',response:{usage:{input_tokens:10,output_tokens:4,input_tokens_details:{cached_tokens:6},output_tokens_details:{reasoning_tokens:2}}}});
  });
  await tracer.span('model',span=>{
    span.modelUsage({provider:'anthropic',model:'fixture/messages'});
    span.observeAnthropic({type:'message_start',message:{usage:{input_tokens:3,cache_creation_input_tokens:7,cache_read_input_tokens:10,output_tokens:1}}});
    span.observeAnthropic({type:'content_block_delta',delta:{type:'text_delta',text:'private-content-sentinel'}});
    for(const count of [2,5,5])span.observeAnthropic({type:'message_delta',usage:{output_tokens:count}});
  });
  await tracer.span('processing',span=>{assert.equal(2+2,4);span.check('arithmetic','passed',{method:'code',evidenceDigest:createHash('sha256').update('4').digest('hex'),execution:'completed'});span.check('source_facts','unknown',{execution:'not_run',reasonCode:'evidence_absent'});},{milestone:'completion'});
  await tracer.span('response',()=>{},{milestone:'delivery',disposition:'delivered'});
});
await tracer.flush();assert.equal(client.status().queued,0);
assert.ok(!JSON.stringify(events).includes('private-content-sentinel'));
assert.ok(events.some(e=>e.properties.afterspan_trace?.model_usage?.first_output_ms>=0));
const timeouts=[],original=AbortSignal.timeout;
let attempts=0;
AbortSignal.timeout=ms=>{timeouts.push(ms);return original(ms)};
try{
  const offline=createAnalytics({url:'https://analytics.invalid',writeKey:'synthetic-package-key',fetch:async()=>{attempts++;throw Error('offline')}});
  await offline.track('package.probe');assert.equal(attempts,2);
  for(let i=1;i<=500;i++)await offline.track('package.probe');
  assert.equal(offline.status().queued,500);assert.equal(offline.status().dropped,1);
  assert.ok(timeouts.length&&timeouts.every(ms=>ms===2500));
  assert.equal(createAnalytics().status().queued,0); // A new collector cannot recover the old memory queue.
}finally{AbortSignal.timeout=original}
writeFileSync(process.argv[2],JSON.stringify(events));
