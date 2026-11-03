const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function markup(r){const c=vm.createContext({panel:(title,body)=>title+' '+body,esc:v=>String(v??'').replaceAll('<','&lt;'),fmt:String,date:String});vm.runInContext(fs.readFileSync('static/collection.js','utf8'),c);return vm.runInContext('collectionHealthMarkup('+JSON.stringify(r)+')',c)}
const empty={status:'attention',reasons:['no_enabled_source_expectations'],sources:[],backend:[],legacy_pulses:0,snapshot:{start:'start',end:'end',as_of:'cutoff'},limits:['No complete task ledger.']};
test('absence never becomes zero loss or complete collection',()=>{const html=markup(empty);assert.match(html,/Needs attention/);assert.match(html,/does not establish zero loss/);assert.match(html,/not established/);assert.match(html,/does not prove zero rejected requests/)});
test('queue problems and unknown evaluators stay visible and escaped',()=>{const html=markup({...empty,sources:[{source:'<script>',status:'attention',reasons:['queue_delayed'],expectation:null,instances:[{latest:{delivery_mode:'memory',queued:3,capacity:500,observed_at_ms:100000,dropped:1,rejected:0,delivery_failures:2,evaluator_failures:null},issues:['queue_delayed'],window_counter_changes:{dropped:null}}]}]});assert.match(html,/queue delayed/);assert.match(html,/evaluator failures Unknown/);assert.match(html,/Lifetime dropped 1/);assert.match(html,/&lt;script>/);assert.doesNotMatch(html,/<script>/)});

function routeContext(query,extra={}){
 const location={hash:'#'+new URLSearchParams({workspace:'work',project:'app',view:'collection',collection_query:JSON.stringify(query)})};
 const content={innerHTML:''},environment={};
 const c=vm.createContext({location,URLSearchParams,project:'app',workspace:'work',tab:'collection',days:1,renderVersion:1,canEdit:()=>false,panel:(t,b)=>t+' '+b,esc:v=>String(v??'').replaceAll('<','&lt;'),fmt:String,date:String,environmentNames:{production:'Production',staging:'Staging'},url:p=>p,$:s=>s==='#content'?content:environment,...extra});
 vm.runInContext(fs.readFileSync('static/collection.js','utf8'),c);return {c,content,environment};
}
test('a collection link reads its exact frozen query and keeps it until an explicit refresh',async()=>{
 const query={days:7,start:'2026-11-01T00:00:00Z',end:'2026-11-01T00:05:00Z',as_of:'2026-11-01T00:06:00Z',environment:'staging',workflow:'document_export'},calls=[];
 const {c,environment}=routeContext(query,{api:async(path,body)=>{calls.push({path,body});return path==='/api/collection/sources'?{sources:[]}:{...empty,query,snapshot:query}},render:()=>{}});
 await vm.runInContext('collectionView(1)',c);assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)),query);
 assert.deepEqual(JSON.parse(vm.runInContext('collectionParams().collection_query',c)),query);
 environment.value='production';environment.onchange();assert.equal(vm.runInContext('collectionQuery',c),null);
 await vm.runInContext('collectionView(1)',c);assert.deepEqual(JSON.parse(JSON.stringify(calls[2].body)),{days:1,environment:'production'});
});
test('switching projects never carries a frozen collection query into another project',async()=>{
 const {c}=routeContext({environment:'staging'},{api:async path=>path==='/api/collection/sources'?{sources:[]}:{...empty,query:{days:1,environment:'production'}}});
 vm.runInContext('project="other"',c);assert.equal(vm.runInContext('collectionParams().collection_query',c),undefined);
 await vm.runInContext('collectionView(1)',c);assert.equal(vm.runInContext('collectionQuery.environment',c),'production');
});
test('an invalid collection link is reported rather than silently querying another window',async()=>{
 const {c}=routeContext(['invalid'],{api:()=>{throw Error('must not query')}});
 await assert.rejects(vm.runInContext('collectionView(1)',c),/saved collection window is invalid/);
});
test('latest evaluator totals do not rewrite an earlier unknown window or imply retirement',()=>{
 const data={...empty,snapshot:{start:'2026-11-03T06:00:00Z',end:'2026-11-03T06:10:00Z',as_of:'2026-11-03T06:10:00Z'},sources:[{source:'worker',status:'attention',reasons:['evaluator_health_unknown'],expectation:null,instances:[{latest:{delivery_mode:'memory',queued:0,capacity:500,observed_at_ms:Date.parse('2026-11-03T05:00:00Z'),dropped:0,rejected:0,delivery_failures:0,evaluator_failures:0},issues:['pulse_stale'],window_counter_changes:{evaluator_failures:null}}]}]};
 const html=markup(data);assert.match(html,/No report in the selected window/);assert.match(html,/retirement is not verified/);assert.match(html,/Latest evaluator count is known/);assert.match(html,/full-window change is unknown/);assert.match(html,/Needs attention/);
});

test('a reported empty stop is distinct from a silent or contradictory worker',()=>{
 const instance={latest:{delivery_mode:'memory',queued:0,capacity:500,observed_at_ms:0,dropped:0,rejected:0,delivery_failures:0,evaluator_failures:null},issues:[],window_counter_changes:{evaluator_failures:null}};
 const response=lifecycle=>({...empty,sources:[{source:'worker',status:'attention',reasons:['evaluator_health_unknown'],instances:[{...instance,lifecycle}]}]});
 const stopped=markup(response('reported_stopped'));assert.match(stopped,/reported an empty queue and stopped/);assert.match(stopped,/Process exit is not independently verified/);assert.match(stopped,/Evaluator execution was not reported/);assert.match(stopped,/not established/);
 assert.match(markup(response('conflicting')),/reported a stop and later activity/);
});

test('an undeclared invocation does not invent a continuous declaration',()=>{
 const instance={latest:{reporting_mode:'on_demand',delivery_mode:'memory',queued:0,capacity:500,observed_at_ms:0,dropped:0,rejected:0,delivery_failures:0,evaluator_failures:0},lifecycle:'reported_stopped',issues:[],window_counter_changes:{evaluator_failures:0}};
 const source={source:'serverless',status:'attention',reasons:['source_not_declared'],expectation:null,instances:[instance]};
 const html=markup({...empty,sources:[source]});assert.match(html,/Reporting mode not declared/);assert.doesNotMatch(html,/Continuous worker/);assert.match(html,/This invocation/);
 assert.match(markup({...empty,sources:[{...source,expectation:{definition:{}}}]}),/Continuous worker/);
});

test('on-demand idle evidence is unavailable and an invocation stop is not host exit',()=>{
 const source={source:'serverless',status:'attention',reasons:['no_invocations_observed'],expectation:{definition:{reporting_mode:'on_demand'}},instances:[]};
 const idle=markup({...empty,sources:[source]});assert.match(idle,/On-demand invocations/);assert.match(idle,/No invocation observed in this window/);assert.match(idle,/Needs attention/);assert.match(idle,/not established/);
 const instance={latest:{reporting_mode:'on_demand',delivery_mode:'memory',queued:0,capacity:500,observed_at_ms:0,dropped:0,rejected:0,delivery_failures:0,evaluator_failures:0},lifecycle:'reported_stopped',issues:[],window_counter_changes:{evaluator_failures:0}};
 const stopped=markup({...empty,sources:[{...source,instances:[instance]}]});assert.match(stopped,/This invocation reported an empty queue and stopped/);assert.match(stopped,/Process exit is not independently verified/);
});

test('missing, null, naive or invalid saved boundaries never fall back to a rolling API read',async()=>{
 const valid={environment:'production',start:'2026-11-01T00:00:00.000001Z',end:'2026-11-01T00:05:00.000001Z',as_of:'2026-11-01T00:06:00.000001Z'};
 for(const query of [{environment:'production'},{...valid,start:null},{...valid,end:null},{...valid,as_of:null},{...valid,start:'2026-11-01T00:00:00'},{...valid,start:'invalid'},{...valid,environment:'untrusted'}]){
  let calls=0;const {c}=routeContext(query,{api:()=>{calls++;throw Error('must not query')}});
  await assert.rejects(vm.runInContext('collectionView(1)',c),/saved collection window is invalid/);assert.equal(calls,0);
 }
});

test('a valid saved window survives collection loading before the shared environment labels',async()=>{
 const query={environment:'production',start:'2026-11-01T00:00:00Z',end:'2026-11-01T00:05:00Z',as_of:'2026-11-01T00:06:00Z'},calls=[];
 const {c}=routeContext(query,{environmentNames:undefined,api:async(path,body)=>{calls.push({path,body});return path==='/api/collection/sources'?{sources:[]}:{...empty,query,snapshot:query}}});
 const page=fs.readFileSync('static/index.html','utf8');assert.ok(page.indexOf('/assets/collection.js')<page.indexOf('/assets/journeys.js'));
 const declaration=fs.readFileSync('static/journeys.js','utf8').match(/^const environmentNames=.*;$/m)[0];vm.runInContext(declaration,c);
 await vm.runInContext('collectionView(1)',c);assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)),query);
});

test('same-tab saved links read the new exact window instead of keeping the former report',async()=>{
 const app=fs.readFileSync('static/app.js','utf8'),prefix=app.slice(0,app.indexOf('\nconst fmt='));
 const initial={environment:'production',start:'2026-11-01T00:00:00Z',end:'2026-11-01T00:05:00Z',as_of:'2026-11-01T00:06:00Z',workflow:'first'},next={...initial,workflow:'second'};
 const hash=q=>'#'+new URLSearchParams({workspace:'work',project:'app',view:'collection',collection_query:JSON.stringify(q)});
 const location={hash:hash(initial),reload:()=>{reloads++;current=boot()}},requests=[];let reloads=0,current;
 function boot(){
  const handlers={},nodes={};
  const c=vm.createContext({location,URLSearchParams,window:{addEventListener:(name,fn)=>{handlers[name]=fn}},document:{querySelector:s=>nodes[s]||(nodes[s]={}),querySelectorAll:()=>[{dataset:{tab:'collection'}}]},workspace:'work',environmentNames:{production:'Production'},canEdit:()=>false,panel:(t,b)=>t+b,esc:String,fmt:String,date:String,traceParams:()=>({}),releaseEvidenceParams:()=>({}),pendingPlan:null,journeyEnvironment:'',history:{replaceState:(_,__,value)=>{location.hash=value}},api:async(path,body)=>{if(path.startsWith('/api/collection/sources'))return {sources:[]};requests.push(body);return {...empty,query:body,snapshot:body}}});
  vm.runInContext(fs.readFileSync('static/collection.js','utf8'),c);vm.runInContext(prefix,c);
  vm.runInContext(app.slice(app.indexOf('function url('),app.indexOf('\nfunction graph(')),c);
  return {c,handlers};
 }
 current=boot();await vm.runInContext('collectionView(0)',current.c);
 vm.runInContext('rememberView()',current.c);assert.equal(reloads,0);
 location.hash=hash(next);current.handlers.hashchange?.();await vm.runInContext('collectionView(0)',current.c);
 assert.deepEqual(JSON.parse(JSON.stringify(requests.at(-1))),next);assert.equal(reloads,1);
 const before=requests.length;location.hash=hash({...next,start:null});current.handlers.hashchange?.();
 await assert.rejects(vm.runInContext('collectionView(0)',current.c),/saved collection window is invalid/);assert.equal(requests.length,before);
});
