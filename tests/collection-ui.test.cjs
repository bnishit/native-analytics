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
