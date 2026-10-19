const {test}=require('node:test');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const vm=require('node:vm');

async function render(observations){
  const content={innerHTML:''},elements=new Map();
  const goal={id:'first-answer',question:'Does the workspace get a checked answer?',measurement_revision:'current',mapping_revision:'native-v1',goal_revision:2,source:{repository:'example/product',base:'main',commit:'a'.repeat(40)}};
  const context=vm.createContext({Date,renderVersion:1,legacyOwner:false,canEdit:()=>false,isOwner:()=>false,
    esc:String,fmt:value=>value===null?'—':String(value),url:value=>value,
    panel:(title,body)=>`<section><h2>${title}</h2>${body}</section>`,act:fn=>fn,
    $:selector=>{if(selector==='#content')return content;if(!content.innerHTML.includes(`id="${selector.slice(1)}"`))return null;if(!elements.has(selector))elements.set(selector,{});return elements.get(selector)},
    api:async path=>path==='/api/loop/goals'?{goals:[goal]}:{goal,observations,actions:[],stages:{},limitations:[],policy:null,next_action:'collect_evidence'}});
  vm.runInContext(readFileSync('static/loop.js','utf8'),context);
  await vm.runInContext('loopView(1)',context);
  return content.innerHTML;
}

const observation={id:'old',source:'native',measurement_revision:'old',mapping_revision:'native-v1',window:{entry_start:'2026-10-01',entry_end:'2026-10-02',observed_through:'2026-10-03'},mature:17,qualified:17,waiting:0,unassessed:0,rate:1,confirmed_lower_bound:1,failed:0,timed_out:0,limitations:[]};

test('changed goal does not display old counts or offer old comparisons',async()=>{
  const html=await render([observation,{...observation,id:'older'}]);
  assert.match(html,/No outcome evidence yet/);
  assert.match(html,/2 earlier observations/);
  assert.doesNotMatch(html,/<b>17<\/b>|id="loop-compare"/);
});

test('current counts remain visible while old source mappings stay historical',async()=>{
  const html=await render([{...observation,measurement_revision:'current',mapping_revision:'old-source'},{...observation,id:'current',measurement_revision:'current',mature:3,qualified:3}]);
  assert.match(html,/<b>3<\/b>/);
  assert.match(html,/1 earlier observations/);
  assert.doesNotMatch(html,/<b>17<\/b>|id="loop-compare"/);
});
