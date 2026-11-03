import test from 'node:test';
import assert from 'node:assert/strict';
import { createAnalytics, safeProperties } from './native-analytics.ts';
test('disabled collector does not perform network work', async()=>{
 let calls=0;const client=createAnalytics({fetch:async()=>{calls++;return new Response('{}')}});
 await client.track('test.event');assert.equal(calls,0);assert.equal(client.status().enabled,false);
});
test('redacts content and credentials while preserving numeric token metrics',()=>{
 const result=safeProperties({phoneNumber:'9876543210',messagePreview:'private message',prompt:'private prompt',api_key:'secret',ai_input_tokens:12,inputTokens:8,output_tokens:4,duration_ms:120}) as Record<string,unknown>;
 assert.equal(result.phoneNumber,'[redacted]');assert.equal(result.messagePreview,'[redacted]');assert.equal(result.prompt,'[redacted]');assert.equal(result.api_key,'[redacted]');assert.equal(result.ai_input_tokens,12);assert.equal(result.inputTokens,8);assert.equal(result.output_tokens,4);
});
test('retry uses the same event ID and receives a duplicate-safe acknowledgement',async()=>{
 const ids:string[]=[];let calls=0;
 const client=createAnalytics({url:'https://analytics.example',writeKey:'na_write_test',fetch:async(_u,init)=>{
  const event=JSON.parse(String(init?.body)).events[0];ids.push(event.id);calls++;if(calls===1)throw Error('offline');
  return Response.json({accepted:0,duplicates:1});
 }});
 await client.track('signup.completed','user-1',{plan:'starter'});assert.equal(ids[0],ids[1]);assert.equal(client.status().queued,0);
});
test('failed delivery stays bounded and can be flushed after recovery',async()=>{
 let online=false;const client=createAnalytics({url:'https://analytics.example',writeKey:'key',fetch:async(_u,init)=>{
 if(!online)throw Error('offline');return Response.json({accepted:JSON.parse(String(init?.body)).events.length,duplicates:0});
 }});
 await client.track('test.event');assert.equal(client.status().queued,1);online=true;await client.flush();assert.equal(client.status().queued,0);
});
test('maps legacy AI events to stable native names and queryable properties',async()=>{
 let captured:any;const client=createAnalytics({url:'https://analytics.example',writeKey:'key',source:'test',fetch:async(_u,init)=>{
 captured=JSON.parse(String(init?.body)).events[0];return Response.json({accepted:1,duplicates:0});
 }});
 await client.track('$ai_generation','slack:user',{'$ai_input_tokens':123,'$ai_input':'private text'});
 assert.equal(captured.name,'ai.generation');assert.equal(captured.properties.ai_input_tokens,123);assert.equal(captured.properties.ai_input,'[redacted]');
});
test('erase requires a confirming server receipt',async()=>{
 let reply={ok:true,stale_identity_blocked:true};
 const client=createAnalytics({url:'https://analytics.example',writeKey:'key',fetch:async()=>Response.json(reply)});
 await client.erase('user-1');reply={ok:true,stale_identity_blocked:false};
 await assert.rejects(client.erase('user-1'),/unconfirmed/);
});

test('journey context stays stable across delivery retries and caller mutation', async () => {
  const sent: string[] = [];
  let offline = true;
  const client = createAnalytics({url:'https://analytics.example',writeKey:'test-key',fetch:async (_url, options) => {
    sent.push(String(options?.body));
    if (offline) throw new Error('offline');
    return new Response(JSON.stringify({accepted:1,duplicates:0}),{status:200});
  }});
  const journey = {id:'task-one',workflow:'summary',environment:'local' as const,attempt_id:'attempt-one',attempt_number:1,state:'started' as const};
  await client.trackJourney('summary.started', journey);
  journey.attempt_number = 2;
  offline = false;
  await client.flush();
  const events = sent.map(body => JSON.parse(body).events[0]);
  assert.equal(new Set(events.map(e => e.id)).size,1);
  assert.ok(events.every(e => e.journey.attempt_number === 1));
});

test('invalid journey context never sends or breaks the caller', async () => {
  const errors: string[] = [];
  let calls = 0;
  const client = createAnalytics({url:'https://analytics.example',writeKey:'test-key',fetch:async () => {calls++;return new Response('{}');},onError:error=>errors.push(error.code)});
  await client.trackJourney('summary.started',{id:'task',workflow:'summary',environment:'local',attempt_id:'a',attempt_number:0,state:'started'});
  assert.equal(calls,0);
  assert.deepEqual(errors,['invalid_journey']);
});


test('diagnostic digests and false flags survive without loosening message or reason redaction', () => {
  const clean = safeProperties({messageHash:'private-hash',rejectionReason:'private-free-text',afterspan_audit:{schema_version:'slack-request-v1',context_digest:'a'.repeat(64),decision_digest:'b'.repeat(64),comparison_eligible:false,independent_proposal:false,proposal_safe:false,diagnostic_code:'input_mismatch'}}) as any;
  assert.equal(clean.messageHash,'[redacted]');
  assert.equal(clean.rejectionReason,'[redacted]');
  assert.equal(clean.afterspan_audit.context_digest,'a'.repeat(64));
  assert.equal(clean.afterspan_audit.comparison_eligible,false);
  assert.equal(clean.afterspan_audit.proposal_safe,false);
  assert.equal(clean.afterspan_audit.independent_proposal,false);
  assert.equal(clean.afterspan_audit.diagnostic_code,'input_mismatch');
});
test('deployment whitespace does not change endpoint, key or source',async()=>{
 let captured: {url?:string;auth?:string|null;source?:string}={};
 const client=createAnalytics({url:'https://example.test\n',writeKey:'write-key\n',source:'shelf\n',fetch:async(url,options)=>{
  const event=JSON.parse(String(options?.body)).events[0];
  captured={url:String(url),auth:new Headers(options?.headers).get('Authorization'),source:event.properties.source};
  return new Response(JSON.stringify({accepted:1,duplicates:0}));
 }});
 await client.track('test.event');
 assert.deepEqual(captured,{url:'https://example.test/v1/events',auth:'Bearer write-key',source:'shelf'});
});

test('collection health exposes retained delivery failures and callback errors do not escape',async()=>{
 const client=createAnalytics({url:'https://analytics.example',writeKey:'synthetic-key',fetch:async()=>new Response('',{status:503}),onError:()=>{throw Error('private callback error')}});
 await client.track('export.completed');
 assert.equal(client.status().queued,1);assert.equal(client.status().last_error,'delivery_failed');assert.ok(client.status().oldest_queued_at);
 await client.track('bad event that normalizes','identity',Object.fromEntries(Array.from({length:65},(_,i)=>['field'+i,'x'.repeat(2000)])));
 assert.equal(client.status().rejected,1);
});

test('typed memory-queue pulse preserves explicit evaluator status and exact clock',async()=>{
 const events:Array<any>=[];const client=createAnalytics({url:'https://analytics.example',writeKey:'synthetic-key',health:{source:'export_worker',environment:'staging',workflow:'document_export'},fetch:async(_,options)=>{const sent=JSON.parse(String(options?.body)).events;events.push(...sent);return new Response(JSON.stringify({accepted:sent.length,duplicates:0}))}});
 await client.pulse();let pulse=events[0].properties.afterspan_health;assert.equal(pulse.delivery_mode,'memory');assert.equal(pulse.evaluator_failures,null);assert.equal(pulse.error_code,null);
 client.recordEvaluation(true);await client.pulse();pulse=events[1].properties.afterspan_health;assert.equal(pulse.evaluator_failures,1);assert.equal(pulse.sequence,2);assert.equal(Date.parse(events[1].timestamp),pulse.observed_at_ms);assert.equal(events[1].properties.source,undefined);
 assert.equal(client.status().queued,0);
});

test('reported stop requires a drained typed collector and preserves delivery uncertainty',async()=>{
 const events:Array<any>=[];let accepting=false;
 const client=createAnalytics({url:'https://analytics.example',writeKey:'synthetic-key',health:{source:'export_worker',environment:'staging'},fetch:async(_,options)=>{if(!accepting)return new Response('',{status:503});const sent=JSON.parse(String(options?.body)).events;events.push(...sent);return new Response(JSON.stringify({accepted:sent.length,duplicates:0}))}});
 await client.track('export.completed');assert.throws(()=>client.pulse('stopped'),/drain/);
 accepting=true;await client.flush();await client.pulse('stopped');const pulse=events[1].properties.afterspan_health;
 assert.equal(pulse.collector_state,'stopped');assert.equal(pulse.queued,0);assert.equal(pulse.evaluator_failures,null);
 assert.throws(()=>createAnalytics().pulse('stopped'),/typed collector/);
});

test('on-demand reporting initializes only explicitly tracked evaluation failure counters',async()=>{
 const events:Array<any>=[];
 const health:NonNullable<Parameters<typeof createAnalytics>[0]>['health']={source:'export_worker',environment:'staging',reportingMode:'on_demand',evaluatorTracking:true};
 const client=createAnalytics({url:'https://analytics.example',writeKey:'synthetic-key',health,fetch:async(_,options)=>{const sent=JSON.parse(String(options?.body)).events;events.push(...sent);return new Response(JSON.stringify({accepted:sent.length,duplicates:0}))}});
 await client.pulse();health.reportingMode='continuous';health.source='different';client.recordEvaluation(true);await client.pulse('stopped');
 const [start,end]=events.map(e=>e.properties.afterspan_health);
 assert.equal(start.reporting_mode,'on_demand');assert.equal(start.evaluator_failures,0);assert.equal(end.evaluator_failures,1);
 assert.equal(end.reporting_mode,'on_demand');assert.equal(end.source,'export_worker');
 assert.equal(start.sequence,1);assert.equal(end.sequence,2);assert.equal(start.instance_id,end.instance_id);assert.equal(end.collector_state,'stopped');
 assert.equal(createAnalytics({health:{source:'export_worker',environment:'staging'}}).status().evaluator_failures,null);
 for(const health of [{reportingMode:'inferred'},{evaluatorTracking:null}])assert.throws(()=>createAnalytics({health:{source:'export_worker',environment:'staging',...health} as any}),/configuration/);
});
test('native model count metrics survive but raw inputs and string impostors stay redacted',()=>{
 const result=safeProperties({input_count:2,output_count:3,input:'private prompt',input_count_text:'private value'}) as any;
 assert.equal(result.input_count,2);assert.equal(result.input,'[redacted]');assert.equal(result.input_count_text,'[redacted]');assert.equal(result.output_count,3);const impostor=safeProperties({input_count:'private value',output_count:'private value'}) as any;assert.equal(impostor.input_count,'[redacted]');assert.equal(impostor.output_count,'[redacted]');
});

test('only bounded numeric first-output timing passes the content redactor',()=>{
 const result=safeProperties({first_output_ms:12.5,output:'private output',nested:{first_output_ms:'private timing text'},negative:{first_output_ms:-1},oversized:{first_output_ms:86400001}}) as any;
 assert.equal(result.first_output_ms,12.5);assert.equal(result.output,'[redacted]');assert.equal(result.nested.first_output_ms,'[redacted]');assert.equal(result.negative.first_output_ms,'[redacted]');assert.equal(result.oversized.first_output_ms,'[redacted]');
});
