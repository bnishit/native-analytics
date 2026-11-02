const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
function context(extra={}){
 const c=vm.createContext({panel:(title,body)=>title+' '+body,esc:v=>String(v??'').replaceAll('<','&lt;'),fmt:String,...extra});
 vm.runInContext(fs.readFileSync('static/outcomes.js','utf8'),c);return c;
}
test('model cost never turns absent observations or missing amounts into free success',()=>{
 const c=context();let html=vm.runInContext('outcomeModelCost({model_spans:0})',c);assert.match(html,/does not establish zero cost/);
 html=vm.runInContext('outcomeModelCost({model_spans:2,cost_known_spans:1,cost_unknown_spans:1,measured_microusd:20,per_satisfied_task_microusd:null})',c);
 assert.match(html,/1 of 2/);assert.match(html,/Unknown per satisfied task/);assert.match(html,/failed, withheld and retried/);assert.match(html,/not a complete application bill/);
 html=vm.runInContext('outcomeModelCost({model_spans:1,cost_known_spans:1,cost_unknown_spans:0,measured_microusd:0,per_satisfied_task_microusd:0})',c);assert.match(html,/\$0.000000 per satisfied/);
});
test('release comparison keeps unknown evidence and observational limits visible',()=>{
 const c=context();const data={status:'unknown',reasons:['after_evidence_incomplete'],experiment:{baseline_pass_rate:.5,candidate_pass_rate:1},satisfied_fraction_difference:null,limits:['Not causal. No deployment authority.']};
 const html=vm.runInContext('outcomeReleaseMarkup('+JSON.stringify(data)+')',c);
 assert.match(html,/Not enough bound evidence/);assert.match(html,/Change unavailable/);assert.match(html,/after evidence incomplete/);assert.match(html,/No deployment authority/);
 const known=vm.runInContext('outcomeReleaseMarkup('+JSON.stringify({...data,status:'comparable',reasons:[],satisfied_fraction_difference:0})+')',c);
 assert.match(known,/0 percentage-point change/);assert.doesNotMatch(known,/Change unavailable/);
});
test('synthetic or mixed goals cannot start a user-outcome comparison',async()=>{
 let html='',calls=0;const c=context({$:()=>({set innerHTML(v){html=v}}),api:()=>{calls++}});
 await vm.runInContext('outcomeReleaseView(1,{definition:{traffic_kind:"synthetic"}})',c);
 assert.equal(calls,0);assert.match(html,/Synthetic, internal and mixed traffic goals stay separate/);
});

test('release boundary retains provider microseconds instead of starting before deployment',()=>{
 const c=context();const base={released_at:'2026-11-02T11:42:28.829987+00:00'},release={released_at:'2026-11-02T11:47:28.829987+00:00'};
 const r=vm.runInContext('outcomeReleaseWindows('+JSON.stringify(base)+','+JSON.stringify(release)+',30,1,Date.parse("2026-11-02T12:00:00Z"))',c);
 assert.equal(r.before.start,base.released_at);assert.equal(r.before.as_of,release.released_at);assert.equal(r.after.start,release.released_at);
 assert.ok(Date.parse(r.before.end)+30000<=Date.parse(release.released_at));
});

test('decimal provider cost stays visible below one micro-dollar',()=>{
 const c=context();const html=vm.runInContext('outcomeModelCost('+JSON.stringify({model_spans:1,cost_known_spans:1,cost_unknown_spans:0,measured_usd:'0.000000037125',measured_microusd:null,per_satisfied_task_usd:'0.000000012375',per_satisfied_task_microusd:null})+')',c);
 assert.match(html,/\$0\.000000037125 measured/);assert.match(html,/\$0\.000000012375 per satisfied/);assert.doesNotMatch(html,/Unknown per/);
 const tiny=vm.runInContext('outcomeModelCost('+JSON.stringify({model_spans:1,cost_known_spans:1,cost_unknown_spans:0,measured_usd:'0.000000000001',per_satisfied_task_usd:'0',per_satisfied_task_below_display_precision:true,per_satisfied_task_rounded:true})+')',c);
 assert.match(tiny,/&lt;\$0\.000000000000000001/);assert.match(tiny,/rounded/);
});
