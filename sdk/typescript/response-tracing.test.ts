import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createAnalytics} from './native-analytics.ts';
import {WorkflowTracer,inject} from './tracing-node.ts';
function fixture(){
 const events:any[]=[];
 const client=createAnalytics({url:'https://analytics.example',writeKey:'synthetic-key',fetch:async(_url,options)=>{const batch=JSON.parse(String(options?.body)).events;events.push(...batch);return new Response(JSON.stringify({accepted:batch.length,duplicates:0}))}});
 return {events,tracer:new WorkflowTracer(client)};
}
const facts=(events:any[])=>events.filter(e=>e.properties.afterspan_trace).map(e=>e.properties.afterspan_trace);
test('response root stays open through actual byte consumption and preserves headers',async()=>{
 const {tracer,events}=fixture();let pulls=0,root:any;
 const result=await tracer.response({workflow:'export',environment:'test'},span=>{
  root=span;
  return new Response(new ReadableStream({pull(c){pulls++;if(pulls===1)c.enqueue(new Uint8Array([0,255,10]));else c.close()}},{highWaterMark:0}),{status:201,headers:{'Content-Type':'application/octet-stream','Set-Cookie':'synthetic=1'}});
 });
 await tracer.flush();assert.equal(pulls,0);assert.equal(root.closed,false);assert.equal(facts(events).filter(x=>x.state!=='started').length,0);
 assert.equal(result.response.status,201);assert.equal(result.response.headers.get('Set-Cookie'),'synthetic=1');
 assert.deepEqual([...new Uint8Array(await result.response.arrayBuffer())],[0,255,10]);assert.equal(await result.finished,'completed');await tracer.flush();
 assert.equal(root.closed,true);assert.equal(facts(events).filter(x=>x.state==='succeeded').length,2);assert.ok(facts(events).every(x=>!x.milestone&&!x.disposition));
});
test('stream is lazy and retains captured context across outside-context iteration',async()=>{
 const {tracer,events}=fixture();let calls=0,expected='',iterable:AsyncIterable<string>;
 const {response,finished}=await tracer.response({workflow:'answer',environment:'test'},()=>{
  expected=inject().traceparent.slice(3,35);
  iterable=tracer.stream('model',async function*(span){calls++;span.modelUsage({provider:'example',model:'fixture/model'});await Promise.resolve();assert.equal(inject().traceparent.slice(3,35),expected);yield 'private-output';await tracer.span('tool',()=>assert.equal(inject().traceparent.slice(3,35),expected));});
  const iterator=iterable[Symbol.asyncIterator]();
  return new Response(new ReadableStream({async pull(c){const value=await iterator.next();if(value.done)c.close();else c.enqueue(new TextEncoder().encode(value.value))}},{highWaterMark:0}));
 });
 assert.equal(calls,0);assert.equal(await response.text(),'private-output');assert.equal(await finished,'completed');await tracer.flush();assert.equal(calls,1);
 const all=facts(events);assert.equal(new Set(all.map(x=>x.trace_id)).size,1);assert.ok(all.some(x=>x.step==='tool'&&x.state==='succeeded'));assert.ok(!JSON.stringify(events).includes('private-output'));
});
test('consumer cancellation closes upstream once and records cancellation',async()=>{
 const {tracer,events}=fixture();let cancelled=0;
 const result=await tracer.response({workflow:'answer'},()=>new Response(new ReadableStream({pull(c){c.enqueue(new Uint8Array([1]))},cancel(){cancelled++}},{highWaterMark:0})));
 const reader=result.response.body!.getReader();await reader.read();await reader.cancel('private-reason');assert.equal(await result.finished,'cancelled');await tracer.flush();
 assert.equal(cancelled,1);const terminal=facts(events).filter(x=>x.state!=='started');assert.equal(terminal.length,2);assert.equal(terminal[0].state,'failed');assert.equal(terminal[0].outcome_code,'cancelled');assert.ok(!JSON.stringify(events).includes('private-reason'));
});
test('stream return cleans up the source and records cancellation without replay',async()=>{
 const {tracer,events}=fixture();let closed=0,calls=0;
 await tracer.task({workflow:'answer'},async()=>{
  for await(const chunk of tracer.stream('model',async function*(){calls++;try{yield 'first';yield 'second'}finally{closed++}})){assert.equal(chunk,'first');break}
 });await tracer.flush();assert.equal(calls,1);assert.equal(closed,1);assert.equal(facts(events).find(x=>x.step==='model'&&x.state!=='started').outcome_code,'cancelled');
});
test('handler and stream errors remain original and never leak to telemetry',async()=>{
 const {tracer,events}=fixture();const problem=Error('private-error');
 await assert.rejects(tracer.response({workflow:'answer'},()=>{throw problem}),e=>e===problem);
 const result=await tracer.response({workflow:'answer'},()=>new Response(new ReadableStream({pull(){throw problem}})));
 await assert.rejects(result.response.text(),e=>e===problem);assert.equal(await result.finished,'failed');await tracer.flush();assert.ok(!JSON.stringify(events).includes('private-error'));
});
test('HTTP error and bodyless responses keep their actual status without false delivery',async()=>{
 const {tracer,events}=fixture();
 for(const status of [204,404,503]){
  const result=await tracer.response({workflow:'export'},()=>new Response(null,{status}));assert.equal(result.response.status,status);assert.equal(await result.finished,status===204?'completed':'failed');
 }
 await tracer.flush();assert.equal(facts(events).filter(x=>x.state==='failed').length,4);assert.ok(facts(events).every(x=>!x.milestone&&!x.disposition));
});
test('collector failure never changes response bytes or retries a handler',async()=>{
 let calls=0;const tracer=new WorkflowTracer(createAnalytics({url:'https://analytics.example',writeKey:'synthetic-key',fetch:async()=>{throw Error('private-collector-error')}}));
 const {response,finished}=await tracer.response({workflow:'answer'},()=>{calls++;return new Response('answer')});
 assert.equal(await response.text(),'answer');assert.equal(await finished,'completed');await tracer.flush();assert.equal(calls,1);
});
test('cancelling a pending pull cannot be mistaken for a successful EOF',async()=>{
 const {tracer,events}=fixture();let entered!:()=>void,cleanup!:()=>void,cancelled=0;
 const pulling=new Promise<void>(r=>{entered=r}),cleaning=new Promise<void>(r=>{cleanup=r});
 const result=await tracer.response({workflow:'answer'},()=>new Response(new ReadableStream({pull(){entered();return new Promise(()=>{})},cancel(){cancelled++;return cleaning}},{highWaterMark:0})));
 const reader=result.response.body!.getReader();const pending=reader.read();await pulling;const cancel=reader.cancel();await pending;cleanup();await cancel;
 assert.equal(await result.finished,'cancelled');await tracer.flush();assert.equal(cancelled,1);assert.equal(facts(events).filter(x=>x.state==='succeeded').length,0);assert.ok(facts(events).filter(x=>x.state!=='started').every(x=>x.state==='failed'&&x.outcome_code==='cancelled'));
});
test('even an undefined thrown stream error must fail the trace',async()=>{
 const {tracer,events}=fixture();const result=await tracer.response({workflow:'answer'},()=>new Response(new ReadableStream({pull(){throw undefined}})));
 await result.response.text().then(()=>assert.fail('Expected the original stream rejection'),error=>assert.equal(error,undefined));
 assert.equal(await result.finished,'failed');await tracer.flush();assert.equal(facts(events).filter(x=>x.state==='failed').length,2);
});
test('upstream cancellation errors are preserved without declaring success',async()=>{
 const {tracer}=fixture();const error=Error('private-cleanup');
 const result=await tracer.response({workflow:'answer'},()=>new Response(new ReadableStream({cancel(){throw error}},{highWaterMark:0})));
 await assert.rejects(result.response.body!.cancel(),value=>value===error);assert.equal(await result.finished,'failed');
});

test('a Response.error is a failed transport response, not completed work',async()=>{
 const {tracer,events}=fixture();const result=await tracer.response({workflow:'answer'},()=>Response.error());
 assert.equal(result.response.type,'error');assert.equal(await result.finished,'failed');await tracer.flush();assert.equal(facts(events).filter(x=>x.state==='failed').length,2);
});
