import {test} from 'node:test';
import assert from 'node:assert/strict';
import {ModelObservation} from './model-observation.ts';

function observation(provider='example'){
 let now=100;return {o:new ModelObservation({provider,model:'fixture/model'},{clock:()=>now}),at:(value:number)=>{now=value}};
}
test('Responses output clock ignores reasoning, empty deltas and lifecycle events',()=>{
 const {o,at}=observation('openai');
 for(const event of [{type:'response.created'},{type:'response.reasoning_text.delta',delta:'private-thinking'},{type:'response.output_text.delta',delta:''}])assert.ok(o.observeResponses(event));
 assert.equal(o.snapshot().first_output_ms,undefined);
 at(125);o.observeResponses({type:'response.output_text.delta',delta:'private-output'});
 at(150);o.observeResponses({type:'response.refusal.delta',delta:'private-refusal'});
 assert.equal(o.snapshot().first_output_ms,25);assert.ok(!JSON.stringify(o.snapshot()).includes('private-'));
});
test('Responses terminal usage is independent of completion success',()=>{
 for(const type of ['response.completed','response.incomplete','response.failed']){
  const {o}=observation('openai');
  assert.ok(o.observeResponses({type,response:{model:'resolved/model',usage:{input_tokens:10,output_tokens:4,input_tokens_details:{cached_tokens:6},output_tokens_details:{reasoning_tokens:2}}}}));
  assert.deepEqual(o.snapshot(),{provider:'openai',model:'resolved/model',unit:'token',input_count:10,output_count:4,cache_read_count:6,reasoning_count:2,usage_source:'provider_reported'});
 }
});
test('Responses tool output starts the clock; full objects leave first output unknown',()=>{
 const {o,at}=observation();at(110);o.observeResponses({type:'response.function_call_arguments.delta',delta:'private-tool-input'});assert.equal(o.snapshot().first_output_ms,10);
 const {o:full}=observation();full.observeResponses({object:'response',output:[{content:'private-output'}],usage:{input_tokens:0,output_tokens:0}});
 assert.equal(full.snapshot().input_count,0);assert.equal(full.snapshot().first_output_ms,undefined);assert.equal(full.snapshot().cost_usd_decimal,undefined);
});
test('Anthropic input includes all cache categories; output updates are cumulative',()=>{
 const {o}=observation('anthropic');
 o.observeAnthropic({type:'message_start',message:{model:'resolved/model',usage:{input_tokens:3,cache_creation_input_tokens:7,cache_read_input_tokens:10,output_tokens:1}}});
 for(const count of [2,5,5])o.observeAnthropic({type:'message_delta',usage:{output_tokens:count}});
 assert.equal(o.snapshot().input_count,20);assert.equal(o.snapshot().cache_read_count,10);assert.equal(o.snapshot().output_count,5);assert.equal(o.snapshot().cost_usd_decimal,undefined);
});
test('Anthropic missing cache fields cannot mean zero',()=>{
 const {o}=observation();o.observeAnthropic({type:'message',usage:{input_tokens:3,output_tokens:0}});
 assert.equal(o.snapshot().input_count,undefined);assert.equal(o.snapshot().output_count,0);
 o.observeAnthropic({type:'message_delta',usage:{cache_read_input_tokens:0}});assert.equal(o.snapshot().input_count,undefined);
 o.observeAnthropic({type:'message_delta',usage:{cache_creation_input_tokens:0}});assert.equal(o.snapshot().input_count,3);
});
test('Anthropic thinking and ping are not output; tool delta is output',()=>{
 const {o,at}=observation();
 for(const event of [{type:'ping'},{type:'content_block_delta',delta:{type:'thinking_delta',thinking:'private-thinking'}},{type:'content_block_start',content_block:{type:'text',text:''}}])o.observeAnthropic(event);
 assert.equal(o.snapshot().first_output_ms,undefined);
 at(120);o.observeAnthropic({type:'content_block_delta',delta:{type:'input_json_delta',partial_json:'private-tool-input'}});
 at(140);o.observeAnthropic({type:'content_block_delta',delta:{type:'text_delta',text:'private-output'}});
 assert.equal(o.snapshot().first_output_ms,20);assert.ok(!JSON.stringify(o.snapshot()).includes('private-'));
});
test('Malformed usage invalidates stale accounting, closed observers reject changes',()=>{
 for(const [method,payload] of [['observeResponses',{object:'response',usage:{input_tokens:true}}],['observeAnthropic',{type:'message_delta',usage:{cache_creation_input_tokens:true}}]] as const){
  const {o}=observation();o.record({input_count:3,usage_source:'provider_reported'});
  assert.equal(o[method](payload),false);assert.equal(o.rejectedUpdates,1);assert.equal(o.snapshot().input_count,undefined);
  o.close();assert.throws(()=>o[method]({}));
 }
});
test('Anthropic full message does not invent output timing; tool start does',()=>{
 const {o,at}=observation();o.observeAnthropic({type:'message',model:'resolved/model',usage:{input_tokens:1,cache_creation_input_tokens:0,cache_read_input_tokens:0,output_tokens:2},content:['private-output']});
 assert.equal(o.snapshot().input_count,1);assert.equal(o.snapshot().first_output_ms,undefined);
 at(115);o.observeAnthropic({type:'content_block_start',content_block:{type:'tool_use',name:'private-tool-name'}});assert.equal(o.snapshot().first_output_ms,15);
});
test('Provider updates cannot relabel prior estimates as reported counts',()=>{
 const {o}=observation();o.record({output_count:99,reasoning_count:12,usage_source:'estimated'});
 o.observeAnthropic({type:'message_start',message:{usage:{input_tokens:3,cache_creation_input_tokens:0,cache_read_input_tokens:0}}});
 assert.equal(o.snapshot().input_count,3);assert.equal(o.snapshot().output_count,undefined);assert.equal(o.snapshot().reasoning_count,undefined);
});
test('Malformed usage/detail containers clear accounting and increment diagnostics',()=>{
 for(const [method,event] of [
  ...['malformed',[],true,3].map(usage=>['observeAnthropic',{type:'message_delta',usage}]),
  ...['malformed',[],true,3].map(usage=>['observeResponses',{object:'response',usage}]),
  ...['input_tokens_details','output_tokens_details'].map(name=>['observeResponses',{object:'response',usage:{[name]:[]}}])
 ] as ['observeAnthropic'|'observeResponses',unknown][]){
  const {o}=observation();o.record({input_count:3,usage_source:'provider_reported',cost_usd_decimal:'0.001',cost_source:'provider_reported'});
  assert.equal(o[method](event),false);assert.equal(o.rejectedUpdates,1);assert.equal(o.snapshot().input_count,undefined);assert.equal(o.snapshot().cost_usd_decimal,undefined);
 }
});
test('AI SDK normalized usage keeps totals inclusive and ignores private/raw fields',()=>{
 const {o,at}=observation();at(120);o.firstOutput();
 const usage={inputTokens:20,outputTokens:7,inputTokenDetails:{cacheReadTokens:10,cacheWriteTokens:4,noCacheTokens:6},outputTokenDetails:{reasoningTokens:2,textTokens:5},cachedInputTokens:10,reasoningTokens:2,get raw(){throw Error('must not inspect raw metadata')},text:'private-answer',totalTokens:999};
 assert.equal(o.observeAISDKUsage(usage),true);
 assert.deepEqual(o.snapshot(),{provider:'example',model:'fixture/model',unit:'token',input_count:20,output_count:7,cache_read_count:10,reasoning_count:2,usage_source:'provider_reported',first_output_ms:20});
 assert.ok(!JSON.stringify(o.snapshot()).includes('private-'));
});
test('AI SDK legacy normalized aliases and real zero counts remain valid',()=>{
 const {o}=observation();assert.ok(o.observeAISDKUsage({inputTokens:0,outputTokens:0,cachedInputTokens:0,reasoningTokens:0}));
 assert.equal(o.snapshot().input_count,0);assert.equal(o.snapshot().output_count,0);assert.equal(o.snapshot().cache_read_count,0);assert.equal(o.snapshot().reasoning_count,0);
 assert.equal(o.snapshot().first_output_ms,undefined);assert.equal(o.snapshot().cost_usd_decimal,undefined);
});
test('AI SDK normalized snapshots replace counts rather than sum or relabel estimates',()=>{
 const {o}=observation();o.record({input_count:99,output_count:80,cache_read_count:50,reasoning_count:40,usage_source:'estimated'});
 for(let i=0;i<2;i++)assert.ok(o.observeAISDKUsage({inputTokens:3}));
 assert.equal(o.snapshot().input_count,3);assert.equal(o.snapshot().output_count,undefined);assert.equal(o.snapshot().cache_read_count,undefined);assert.equal(o.snapshot().usage_source,'provider_reported');
 for(const usage of [{},undefined,null,{inputTokens:undefined,outputTokens:null}]){
  assert.ok(o.observeAISDKUsage(usage));assert.equal(o.snapshot().input_count,undefined);assert.equal(o.snapshot().usage_source,undefined);
 }
});
test('AI SDK malformed counts, detail containers and conflicting aliases invalidate accounting',()=>{
 for(const usage of [[],true,4,'bad',{inputTokens:true},{inputTokens:{total:3}},{outputTokens:-1},{inputTokens:1e9+1},{inputTokens:Infinity},{inputTokens:3.5},{inputTokenDetails:[]},{outputTokenDetails:true},{inputTokens:3,cachedInputTokens:2,inputTokenDetails:{cacheReadTokens:1}},{outputTokens:3,reasoningTokens:2,outputTokenDetails:{reasoningTokens:1}},{inputTokens:1,inputTokenDetails:{cacheReadTokens:2}},{outputTokens:1,outputTokenDetails:{reasoningTokens:2}}]){
  const {o}=observation();o.record({input_count:3,usage_source:'provider_reported',cost_usd_decimal:'0.001',cost_source:'provider_reported'});
  assert.equal(o.observeAISDKUsage(usage),false);assert.equal(o.rejectedUpdates,1);assert.equal(o.snapshot().input_count,undefined);assert.equal(o.snapshot().cost_usd_decimal,undefined);
 }
});
test('AI SDK usage cannot supply cost, model identity, timing or a closed update',()=>{
 const {o}=observation();assert.ok(o.observeAISDKUsage({inputTokens:3,model:'private-model',cost:0,cost_usd_decimal:'0.01',usage_source:'estimated',first_output_ms:0}));
 assert.equal(o.snapshot().model,'fixture/model');assert.equal(o.snapshot().usage_source,'provider_reported');assert.equal(o.snapshot().cost_usd_decimal,undefined);assert.equal(o.snapshot().first_output_ms,undefined);
 o.close();assert.throws(()=>o.observeAISDKUsage({inputTokens:4}));
});
