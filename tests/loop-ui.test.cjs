const {test}=require('node:test');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const vm=require('node:vm');

async function render(observations,extras={}){
  const content={innerHTML:''},elements=new Map();
  const goal={id:'first-answer',question:'Does the workspace get a checked answer?',measurement_revision:'current',mapping_revision:'native-v1',goal_revision:2,source:{repository:'example/product',base:'main',commit:'a'.repeat(40)}};
  const context=vm.createContext({Date,renderVersion:1,legacyOwner:false,canEdit:()=>Boolean(extras.canEdit),isOwner:()=>Boolean(extras.isOwner),
    esc:String,fmt:value=>value===null?'—':String(value),url:value=>value,
    panel:(title,body)=>`<section><h2>${title}</h2>${body}</section>`,act:fn=>fn,
    $:selector=>{if(selector==='#content')return content;if(!content.innerHTML.includes(`id="${selector.slice(1)}"`))return null;if(!elements.has(selector))elements.set(selector,{});return elements.get(selector)},
    api:async path=>path==='/api/loop/goals'?{goals:[goal]}:{goal,observations,actions:[],stages:{},limitations:[],policy:null,...extras,next_action:'collect_evidence'}});
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

test('checked submission stays distinct from cohort maturity',async()=>{
  const html=await render([],{connection:{started_at:1700000000,deadline_at:1700086400},latest_answer:{verdict:'qualified',elapsed_seconds:3.2,within_deadline:true}});
  assert.match(html,/passed the aggregate fact check after 3.2 seconds/);
  assert.match(html,/not proof someone read it/);
  assert.match(html,/No outcome evidence yet/);
  assert.match(html,/Cohort rates below still wait/);
});

test('erased connection shows a blocked historical state rather than waiting',async()=>{
  const connection={started_at:1700000000,deadline_at:1700086400};
  const html=await render([],{connection,connection_status:'evidence_unavailable'});
  assert.match(html,/cannot be resumed or given a new clock/);
  assert.match(html,/No new answer can be submitted/);
  assert.doesNotMatch(html,/Waiting for the agent|Reconnecting keeps/);
  const checked=await render([],{connection,connection_status:'evidence_unavailable',latest_answer:{verdict:'qualified',elapsed_seconds:30,within_deadline:true}});
  assert.match(checked,/historical answer passed/);
});

test('an environment change blocks the connection in the owner page',async()=>{
  const html=await render([],{connection:{started_at:1700000000,deadline_at:1700086400},connection_status:'environment_changed'});
  assert.match(html,/different service environment/);
  assert.doesNotMatch(html,/Waiting for the agent|Reconnecting keeps/);
});


test('production connection hides unsupported repair and grant buttons',async()=>{
  for(const policy of [null,{state:'revoked',revision:2},{state:'active',revision:1,operations:['draft-pr']},{state:'paused',revision:2,operations:['draft-pr']}]){
    const html=await render([],{canEdit:true,isOwner:true,tracking_repair_available:false,stages:{tracking:'service_managed'},policy});
    assert.match(html,/Built-in connection contract/);
    assert.match(html,/local pilot repair is unavailable/);
    assert.doesNotMatch(html,/id="loop-prepare"|id="loop-allow-draft"/);
    if(policy&&policy.state!=='revoked')assert.match(html,/id="loop-revoke"/);
  }
});

test('repo scan retries keep their ID and a completed scan permits a fresh head',async()=>{
  const content={innerHTML:''},elements=new Map(),calls=[],errors=[];
  const context=vm.createContext({project:'pilot',renderVersion:1,Date,JSON,Number,String,Error,crypto:require('node:crypto').webcrypto,
    esc:String,isOwner:()=>false,loopView:async()=>{},loopLimits:()=>'',url:value=>value,showError:e=>errors.push(e.message),
    panel:(title,body)=>`<section><h2>${title}</h2>${body}</section>`,
    $:selector=>{if(selector==='#content')return content;if(selector==='#repo-scan button')return {isConnected:false};if(!content.innerHTML.includes(`id="${selector.slice(1)}"`))return null;if(!elements.has(selector))elements.set(selector,{value:selector==='#repo-repository'?'connection':'main'});return elements.get(selector)},
    api:async(path,body)=>{if(path==='/api/loop/sources')return {connections:[{id:'connection',kind:'github',status:'configured',revision:1,target:{repository:'owner/product'}}]};calls.push({path,body});if(calls.length===1)throw Error('read failed');return {id:'scan',commit:'a'.repeat(40),files:[],findings:{events:[]},coverage:{complete:false,reasons:[]}}}});
  vm.runInContext(readFileSync('static/loop-onboarding.js','utf8'),context);
  await vm.runInContext('repoOnboardingView(1)',context);
  for(let i=0;i<3;i++)await elements.get('#repo-scan').onsubmit({preventDefault(){}});
  assert.deepEqual(errors,['read failed']);
  assert.equal(calls[0].path,calls[1].path);
  assert.deepEqual(calls[0].body,calls[1].body);
  assert.notEqual(calls[1].path,calls[2].path);
});
