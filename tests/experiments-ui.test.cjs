const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function context(extra={}){
 const c=vm.createContext({esc:v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;'),fmt:String,panel:(title,body)=>title+' '+body,...extra});
 vm.runInContext(fs.readFileSync('static/experiments.js','utf8'),c);return c;
}
test('comparison shows per-case regressions and keeps release authority separate',()=>{
 const c=context();const value={baseline_pass_rate:1,candidate_pass_rate:.5,eligible_for_review:false,limits:['Not production impact'],cases:[{title:'<unsafe>',version:2,baseline_state:'passed',state:'failed',change:'regressed'}]};
 const html=vm.runInContext('experimentComparison('+JSON.stringify(value)+')',c);
 assert.match(html,/100% → 50%/);assert.match(html,/regressed/);assert.match(html,/does not meet/i);assert.match(html,/Deployment is not authorized/);assert.match(html,/&lt;unsafe>/);assert.doesNotMatch(html,/<unsafe>/);
});
test('missing results remain unknown and eligible experiments only earn review',()=>{
 const c=context();const value={baseline_pass_rate:.5,candidate_pass_rate:1,eligible_for_review:true,limits:[],cases:[{title:'Source facts',version:1,baseline_state:'missing',state:'passed',change:'unknown'}]};
 const html=vm.runInContext('experimentComparison('+JSON.stringify(value)+')',c);
 assert.match(html,/missing/);assert.match(html,/unknown/);assert.match(html,/Meets this dataset’s review criteria/);assert.match(html,/Deployment is not authorized/);
});
test('non-editors never fetch curated fixture content',async()=>{
 let html='',calls=0;const c=context({canEdit:()=>false,$:()=>({set innerHTML(value){html=value}}),api:()=>{calls++;throw Error('must not fetch')}});
 await vm.runInContext('experimentsView(1)',c);assert.equal(calls,0);assert.match(html,/Editor access is required/);
});
