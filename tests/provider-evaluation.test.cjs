const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const transport=import('../examples/tracker-provider-evaluation/transport.mjs');
const request=()=>({model:'openai/gpt-4.1-mini',messages:[{role:'user',content:'Synthetic read-only fixture'}],temperature:0,max_tokens:100,response_format:{type:'json_schema',json_schema:{strict:true}},provider:{require_parameters:true}});
const sha='a'.repeat(40);
test('provider transport keeps exact response lexemes and fixed credential destination',async()=>{
 const {executeProvider,remoteProgram}=await transport;let calls=0;
 const body='{"usage":{"cost":0.000000037125},"choices":[{"message":{"content":"{}"}}]}';
 const result=await executeProvider({runtime_sha:sha,request:request()},async(url,options)=>{calls++;assert.equal(url,'https://openrouter.ai/api/v1/chat/completions');assert.equal(options.redirect,'error');assert.equal(options.headers.Authorization,'Bearer synthetic-test-value');return new Response(body)},{RAILWAY_GIT_COMMIT_SHA:sha,OPENROUTER_API_KEY:'synthetic-test-value'});
 assert.equal(calls,1);assert.equal(result.body,body);assert.equal(result.status,200);new vm.Script(remoteProgram);
});
test('wrong runtime, credentials, model, output budget and messages fail before provider access',async()=>{
 const {executeProvider,validateRequest}=await transport;let calls=0;const fetcher=async()=>{calls++;return new Response('{}')};
 await assert.rejects(executeProvider({runtime_sha:sha,request:request()},fetcher,{RAILWAY_GIT_COMMIT_SHA:'b'.repeat(40),OPENROUTER_API_KEY:'synthetic'}));
 await assert.rejects(executeProvider({runtime_sha:sha,request:request()},fetcher,{RAILWAY_GIT_COMMIT_SHA:sha}));
 for(const change of [{model:'unapproved'},{max_tokens:2201},{temperature:1},{stream:true},{messages:[]},{messages:[{role:'user',content:'界'.repeat(22000)}]},{messages:[{role:'user',content:'a'.repeat(24001)}]},{provider:{require_parameters:true,order:['elsewhere']}},{response_format:{type:'text'}}])assert.throws(()=>validateRequest({...request(),...change}));
 assert.equal(calls,0);
});
test('failed and oversized provider responses do not become receipts',async()=>{
 const {executeProvider}=await transport,env={RAILWAY_GIT_COMMIT_SHA:sha,OPENROUTER_API_KEY:'synthetic'};
 await assert.rejects(executeProvider({runtime_sha:sha,request:request()},async()=>new Response('not exposed',{status:401}),env),/Provider request failed/);
 await assert.rejects(executeProvider({runtime_sha:sha,request:request()},async()=>new Response('x'.repeat(262145)),env),/too large/);
});
test('caught Request-object writes and invalid model calls poison the evaluation',async()=>{
 const {guardedFetch,providerUrl}=await transport,reads=[],forbidden=[];let calls=0;
 const fetcher=guardedFetch([{id:'synthetic_flag'}],reads,forbidden,async()=>{calls++;return new Response('{}')});
 try{await fetcher(new Request('https://catalog.synthetic.invalid/api/v1/features',{method:'POST'}))}catch{}
 try{await fetcher(providerUrl,{method:'POST',body:JSON.stringify({...request(),model:'unapproved'})})}catch{}
 assert.deepEqual(forbidden,['forbidden_transport','invalid_provider_request']);assert.equal(calls,0);
 const response=await fetcher(new Request('https://catalog.synthetic.invalid/api/v1/features?limit=1&offset=0'));
 assert.equal((await response.json()).features.length,1);assert.equal(reads.length,1);
 await fetcher(providerUrl,{method:'POST',body:JSON.stringify(request())});assert.equal(calls,1);
});
test('selection rejects empty, duplicate and ambiguous live requests',async()=>{
 const {parseArguments}=await import('../examples/tracker-provider-evaluation/run.mjs');
 const args=['--offline','--repo','/synthetic','--source',sha,'--variant','candidate','--case','total-enabled','--output','/private/synthetic'];
 assert.equal(parseArguments([...args]).mode,'offline');
 for(const bad of [[],[...args,'--live'],[...args,'--case','total-enabled'],args.map(v=>v==='total-enabled'?'missing':v),args.map(v=>v===sha?'main':v)])assert.throws(()=>parseArguments(bad));
 assert.throws(()=>parseArguments([...args.filter(v=>v!=='--offline'),'--live']));
 assert.throws(()=>parseArguments([...args,'--negative-control','unknown']));
});
