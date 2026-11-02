const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function markup(r){const c=vm.createContext({panel:(title,body)=>title+' '+body,esc:v=>String(v??'').replaceAll('<','&lt;'),fmt:String,date:String});vm.runInContext(fs.readFileSync('static/collection.js','utf8'),c);return vm.runInContext('collectionHealthMarkup('+JSON.stringify(r)+')',c)}
const empty={status:'attention',reasons:['no_enabled_source_expectations'],sources:[],backend:[],legacy_pulses:0,snapshot:{start:'start',end:'end',as_of:'cutoff'},limits:['No complete task ledger.']};
test('absence never becomes zero loss or complete collection',()=>{const html=markup(empty);assert.match(html,/Needs attention/);assert.match(html,/does not establish zero loss/);assert.match(html,/not established/);assert.match(html,/does not prove zero rejected requests/)});
test('queue problems and unknown evaluators stay visible and escaped',()=>{const html=markup({...empty,sources:[{source:'<script>',status:'attention',reasons:['queue_delayed'],expectation:null,instances:[{latest:{delivery_mode:'memory',queued:3,capacity:500,observed_at_ms:100000,dropped:1,rejected:0,delivery_failures:2,evaluator_failures:null},issues:['queue_delayed'],window_counter_changes:{dropped:null}}]}]});assert.match(html,/queue delayed/);assert.match(html,/evaluator failures Unknown/);assert.match(html,/Lifetime dropped 1/);assert.match(html,/&lt;script>/);assert.doesNotMatch(html,/<script>/)});
