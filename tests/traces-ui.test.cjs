const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function render(spans){
 const context=vm.createContext({location:{hash:''},URLSearchParams,traceStepLabel:{},esc:v=>String(v??'').replaceAll('<','&lt;'),fmt:v=>String(v)});
 vm.runInContext(fs.readFileSync('static/traces.js','utf8'),context);
 return vm.runInContext('traceWaterfall('+JSON.stringify({clock:'request start',spans})+')',context);
}
test('real waterfall preserves overlapping children and unknown ends',()=>{
 const html=render([{id:'1',parent:null,step:'processing',attempt:1,state:'succeeded',start_ms:0,end_ms:100,duration_ms:100},{id:'2',parent:'1',step:'model',attempt:1,state:'unclosed',start_ms:20,end_ms:null,duration_ms:null}]);
 assert.match(html,/within 1/);assert.match(html,/finish unknown/);assert.match(html,/left:20%/);assert.match(html,/trace-open/);assert.match(html,/Overlaps are not added/);
});
test('missing starts never become zero-duration success bars',()=>{
 const html=render([{id:'1',step:'http',attempt:2,state:'failed',start_ms:null,end_ms:null,duration_ms:30,parent_missing:true}]);
 assert.match(html,/Start unknown/);assert.match(html,/parent missing/);assert.match(html,/trace-open trace-failed/);
});

test('refresh keeps investigation filters but drops frozen time and selection',()=>{
 const context=vm.createContext({location:{hash:''},URLSearchParams,traceStepLabel:{},days:30});
 vm.runInContext(fs.readFileSync('static/traces.js','utf8'),context);
 const result=vm.runInContext(`traceQuery={environment:'staging',search:'model',status:'failed',start:'old',end:'old',as_of:'old',offset:20,limit:20};traceReference='selected';resetTraceWindow();JSON.stringify({query:traceQuery,reference:traceReference})`,context);
 assert.deepEqual(JSON.parse(result),{query:{environment:'staging',search:'model',status:'failed',offset:0,limit:20,days:30},reference:''});
});
