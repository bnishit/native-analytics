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
