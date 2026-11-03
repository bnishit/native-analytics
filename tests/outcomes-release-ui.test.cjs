const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
function context(extra={}){
 const c=vm.createContext({project:'app',location:{hash:''},URLSearchParams,panel:(title,body)=>title+' '+body,esc:v=>String(v??'').replaceAll('<','&lt;'),fmt:String,...extra});
 vm.runInContext(fs.readFileSync('static/collection.js','utf8'),c);vm.runInContext(fs.readFileSync('static/outcomes.js','utf8'),c);return c;
}
test('model cost never turns absent observations or missing amounts into free success',()=>{
 const c=context();let html=vm.runInContext('outcomeModelCost({model_spans:0})',c);assert.match(html,/does not establish zero cost/);
 html=vm.runInContext('outcomeModelCost({model_spans:2,cost_known_spans:1,cost_unknown_spans:1,measured_microusd:20,per_satisfied_task_microusd:null})',c);
 assert.match(html,/1 of 2/);assert.match(html,/Unknown per satisfied task/);assert.match(html,/failed, withheld and retried/);assert.match(html,/not a complete application bill/);
 html=vm.runInContext('outcomeModelCost({model_spans:1,cost_known_spans:1,cost_unknown_spans:0,measured_microusd:0,per_satisfied_task_microusd:0})',c);assert.match(html,/\$0.000000 per satisfied/);
});
test('release comparison keeps unknown evidence and observational limits visible',()=>{
 const c=context();const data={status:'unknown',reasons:['after_evidence_incomplete','partial_answer_not_completed'],experiment:{baseline_pass_rate:.5,candidate_pass_rate:1},satisfied_fraction_difference:null,limits:['Not causal. No deployment authority.']};
 const html=vm.runInContext('outcomeReleaseMarkup('+JSON.stringify(data)+')',c);
 assert.match(html,/Not enough bound evidence/);assert.match(html,/Change unavailable/);assert.match(html,/after evidence incomplete/);assert.match(html,/Only part of the request was answered/);assert.match(html,/No deployment authority/);
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

test('release evidence distinguishes pending checks, owner adoption and final identity',()=>{
 const c=context();
 const pending=vm.runInContext('releaseEvidenceMarkup({status:"pending"},true)',c);
 assert.match(pending,/Checking source and deployment/);assert.doesNotMatch(pending,/Adopt this evidence/);
 const proposal={schema:'external-release-proposal-v1',status:'requires_owner_adoption',id:'proposal',proposal_sha256:'a'.repeat(64),evidence:{source:{repository:'owner/app',pr_number:12,commit:'b'.repeat(40),changed_files:1,manifest:[{path:'src/<unsafe>.ts',before:null,after:{mode:'100644',sha:'c'.repeat(40)}}]},deployment:{environment:'production',released_at:'2026-11-03T00:00:00Z'}},limits:['Identity only. No merge or deployment permission.']};
 const editor=vm.runInContext('releaseEvidenceMarkup('+JSON.stringify(proposal)+',false)',c);
 assert.match(editor,/An owner must adopt/);assert.doesNotMatch(editor,/id="release-evidence-adopt"/);assert.doesNotMatch(editor,/<unsafe>/);
 const owner=vm.runInContext('releaseEvidenceMarkup('+JSON.stringify(proposal)+',true)',c);
 assert.match(owner,/I reviewed this exact source and deployment evidence/);assert.match(owner,/Adopt this evidence/);assert.match(owner,/No merge or deployment permission/);
 const saved=vm.runInContext('releaseEvidenceMarkup('+JSON.stringify({...proposal,schema:'external-release-receipt-v1',status:'adopted'})+',true)',c);
 assert.match(saved,/Saved provider identity evidence/);assert.doesNotMatch(saved,/id="release-evidence-adopt"/);
});

test('release writes reconcile stable IDs and never replay after an uncertain read',async()=>{
 const calls=[];const c=context({url:path=>path,api:async(path,body,method)=>{calls.push({path,body,method});return {schema:'external-release-receipt-v1',status:'adopted',proposal_id:'p',proposal_sha256:'h'}}});
 await vm.runInContext('reconcileReleaseWrite("/receipt/id","/receipt/id/adopt",{proposal_id:"p",proposal_sha256:"h"},"POST")',c);
 assert.equal(calls.length,1);assert.equal(calls[0].body,undefined);
 c.api=async(path,body,method)=>{calls.push({path,body,method});if(!body)throw Object.assign(Error('missing'),{status:404});return {schema:'external-release-receipt-v1',status:'adopted',proposal_id:'p',proposal_sha256:'h'}};
 calls.length=0;
 await vm.runInContext('reconcileReleaseWrite("/receipt/id","/receipt/id/adopt",{proposal_id:"p",proposal_sha256:"h"},"POST")',c);
 assert.equal(calls.length,2);assert.equal(calls[1].path,'/receipt/id/adopt');assert.equal(calls[1].method,'POST');assert.equal(calls[1].body.proposal_id,'p');
 calls.length=0;c.api=async()=>{calls.push('read');throw Object.assign(Error('unknown'),{status:503})};
 await assert.rejects(vm.runInContext('reconcileReleaseWrite("/receipt/id","/receipt/id/adopt",{},"POST")',c));assert.deepEqual(calls,['read']);
 calls.length=0;c.api=async()=>{calls.push('read');return {schema:'external-release-receipt-v1',status:'adopted',proposal_id:'different',proposal_sha256:'h'}};
 await assert.rejects(vm.runInContext('reconcileReleaseWrite("/receipt/id","/receipt/id/adopt",{proposal_id:"p",proposal_sha256:"h"},"POST")',c));assert.deepEqual(calls,['read']);
});

test('shared browser transport retains HTTP status for safe write reconciliation',async()=>{
 const source=fs.readFileSync('static/app.js','utf8'),start=source.indexOf('async function api('),end=source.indexOf('\nfunction url(',start);
 const c=vm.createContext({fetch:async()=>new Response(JSON.stringify({detail:'evidence changed'}),{status:409}),Response,$:()=>({})});
 vm.runInContext(source.slice(start,end),c);
 try{await vm.runInContext('api("/receipt")',c);assert.fail('must reject')}catch(error){assert.equal(error.status,409);assert.equal(error.message,'evidence changed')}
});

test('a reconciled release write retains its original project when the screen changes',async()=>{
 const calls=[],c=context({project:'first'});
 c.url=(path,extra={})=>path+'?project='+(extra.project||c.project);
 c.api=async(path,body)=>{calls.push(path);if(!body){c.project='second';throw Object.assign(Error('missing'),{status:404})}return {schema:'external-release-receipt-v1',status:'adopted',proposal_id:'p',proposal_sha256:'h'}};
 await vm.runInContext('reconcileReleaseWrite("/receipt/id","/receipt/id/adopt",{proposal_id:"p",proposal_sha256:"h"},"POST")',c);
 assert.deepEqual(calls,['/receipt/id?project=first','/receipt/id/adopt?project=first']);
});

function routeContext(hash){
 const location={hash},content={innerHTML:''},calls=[];
 const c=context({location,tab:'outcomes',workspace:'legacy',days:1,pendingPlan:null,journeyEnvironment:'',traceParams:()=>({}),renderVersion:1,canEdit:()=>false,$:selector=>selector==='#content'?content:null,api:async path=>{calls.push(path);return {goals:[]}},url:path=>path,history:{replaceState:(_state,_title,next)=>{location.hash=next}}});
 const app=fs.readFileSync('static/app.js','utf8'),start=app.indexOf('function rememberView('),end=app.indexOf('\nconst fmt=',start);
 vm.runInContext(app.slice(start,end),c);return {c,location,calls};
}
test('an owner proposal link survives the actual initial rememberView call',async()=>{
 const {c,location}=routeContext('#project=app&view=outcomes&release_proposal=proposal-id');
 vm.runInContext('rememberView()',c);assert.equal(new URLSearchParams(location.hash.slice(1)).get('release_proposal'),'proposal-id');
});
test('project switching clears old evidence before rememberView rewrites the route',async()=>{
 const {c,location}=routeContext('#project=first&view=outcomes&release_proposal=old-id');
 vm.runInContext('releaseEvidenceProject="first";releaseProposalSelection="old-id";project="second";rememberView()',c);
 await vm.runInContext('outcomesView(1)',c);
 assert.equal(new URLSearchParams(location.hash.slice(1)).get('release_proposal'),null);
 assert.equal(vm.runInContext('releaseProposalSelection',c),'');
});

test('goal form can describe a stored library answer without silently requiring delivery',()=>{
 const c=context({canEdit:()=>true,environmentOptions:()=>'<option>production</option>'});
 const data=new URLSearchParams({title:'Stored cited answer',contract:'workflow-trace-v1',workflow:'shelf-library-qa',environment:'production',traffic_kind:'synthetic',min_sample_size:'25',deadline_seconds:'60',check_name:'citation_scope',check_version:'1',method:'code',phase:'after_delivery',evidence_required:'true'});data.append('milestones','completion');c.data=data;
 const goal=JSON.parse(vm.runInContext('JSON.stringify(outcomeGoalDefinition(data))',c));
 assert.deepEqual(goal.checks,[{name:'citation_scope',version:1,method:'code',phase:'after_delivery',evidence_required:true}]);assert.deepEqual(goal.milestones,['completion']);assert.equal(goal.min_sample_size,25);
 const html=vm.runInContext('outcomeGoalForm()',c);assert.match(html,/value="before_delivery"/);assert.match(html,/value="after_delivery"/);assert.match(html,/name="milestones" value="delivery" checked/);assert.match(html,/not independent proof/);
 c.canEdit=()=>false;assert.equal(vm.runInContext('outcomeGoalForm()',c),'');
});
test('goal criteria preserve independent checks and reject empty or duplicate criteria',()=>{
 const c=context(),data=new URLSearchParams({title:'Checked useful task',contract:'workflow-trace-v1',workflow:'document_export',environment:'production',traffic_kind:'user',min_sample_size:'25',deadline_seconds:'60'});
 for(const [name,version,method,phase,evidence] of [['source_facts','2','source','before_delivery','true'],['human_review','1','human','after_delivery','false']]){
  for(const [field,value] of Object.entries({check_name:name,check_version:version,method,phase,evidence_required:evidence}))data.append(field,value);
 }
 data.append('milestones','completion');data.append('milestones','usefulness');c.data=data;
 const goal=JSON.parse(vm.runInContext('JSON.stringify(outcomeGoalDefinition(data))',c));
 assert.equal(goal.checks[0].version,2);assert.equal(goal.checks[1].method,'human');assert.equal(goal.checks[1].phase,'after_delivery');assert.equal(goal.checks[1].evidence_required,false);assert.deepEqual(goal.milestones,['completion','usefulness']);
 data.delete('milestones');assert.throws(()=>vm.runInContext('outcomeGoalDefinition(data)',c),/at least one/);data.append('milestones','completion');data.set('check_name','source_facts');data.append('check_name','source_facts');assert.throws(()=>vm.runInContext('outcomeGoalDefinition(data)',c),/different name/);
 data.delete('check_name');assert.throws(()=>vm.runInContext('outcomeGoalDefinition(data)',c),/1 and 20/);for(let n=0;n<21;n++)data.append('check_name','check_'+n);assert.throws(()=>vm.runInContext('outcomeGoalDefinition(data)',c),/1 and 20/);
});

test('saved goal screen shows check timing and receipt requirements, not only names',async()=>{
 const content={innerHTML:'',querySelectorAll:()=>[]},select={};
 const goal={id:'goal',version:1,definition:{title:'Stored cited answer',workflow:'library_qa',environment:'test',deadline_seconds:60,traffic_kind:'synthetic',min_sample_size:25,checks:[{name:'citation_scope',version:1,method:'code',phase:'after_delivery',evidence_required:true},{name:'source_facts',version:2,method:'source',phase:'before_delivery',evidence_required:false}],milestones:['completion']}};
 const report={counts:{satisfied:0},mature_denominator:0,satisfied_fraction:null,coverage:{},tasks:[],snapshot:{start:'2026-11-03T00:00:00Z',end:'2026-11-03T01:00:00Z',as_of:'2026-11-03T01:01:00Z'},limits:[]};
 const c=context({renderVersion:1,days:1,canEdit:()=>false,url:path=>path,$:selector=>selector==='#content'?content:selector==='#outcome-goal'?select:null,api:async path=>path==='/api/outcomes/goals'?{goals:[goal]}:report});
 await vm.runInContext('outcomesView(1)',c);
 assert.match(content.innerHTML,/citation_scope v1 \(code, after delivery, receipt required\)/);assert.match(content.innerHTML,/source_facts v2 \(source, before delivery, receipt not required\)/);assert.match(content.innerHTML,/Required milestones: completion\./);assert.match(content.innerHTML,/25 mature tasks/);
});

test('release comparison explains the pinned sample shortfall and source warning without promising a verdict',()=>{
 const c=context(),cohort={counts:{satisfied:0,failed:0,withheld:2,unclassified:0},mature_denominator:2,snapshot:{start:'start',end:'end',as_of:'cutoff'},tasks:[],collection_health:{status:'attention',reasons:['source_evidence_incomplete'],sources:[{source:'worker_<unsafe>',status:'attention',reasons:['pulse_stale','evaluator_health_unknown']}]}};
 const report={status:'unknown',reasons:['before_sample_below_goal_minimum'],goal_definition:{min_sample_size:25,deadline_seconds:30,traffic_kind:'user'},before:cohort,experiment:{baseline_pass_rate:1,candidate_pass_rate:1},satisfied_fraction_difference:null,limits:[]};
 const html=vm.runInContext('outcomeReleaseMarkup('+JSON.stringify(report)+')',c);
 assert.match(html,/25 mature tasks per window/);assert.match(html,/30 second/);assert.match(html,/2 of 25/);assert.match(html,/23 more mature tasks/);assert.match(html,/More tasks alone do not resolve missing evidence/);
 assert.match(html,/Collector report is stale/);assert.match(html,/Evaluator health was not observed/);assert.doesNotMatch(html,/<unsafe>/);assert.match(html,/Change unavailable/);
 const old=vm.runInContext('outcomeReleaseMarkup('+JSON.stringify({...report,goal_definition:undefined})+')',c);
 assert.doesNotMatch(old,/of 25|23 more mature/);assert.match(old,/Pinned comparison criteria were not returned/);
});

test('release collection drilldown preserves the exact source window and project',()=>{
 const c=context({workspace:'work',days:7}),query={days:7,start:'2026-11-01T00:00:00Z',end:'2026-11-01T00:05:00Z',as_of:'2026-11-01T00:06:00Z',environment:'staging',workflow:'document_export'};
 const cohort={counts:{satisfied:0,failed:0,withheld:0,unclassified:0},mature_denominator:0,snapshot:query,tasks:[],collection_health:{status:'attention',reasons:[],sources:[],query}};
 const data={status:'unknown',reasons:[],experiment:{baseline_pass_rate:1,candidate_pass_rate:1},satisfied_fraction_difference:null,before:cohort,limits:[]};
 const html=vm.runInContext('outcomeReleaseMarkup('+JSON.stringify(data)+')',c),match=html.match(/href="([^"]+)"[^>]*>Inspect Before collection window/);assert.ok(match);
 const link=new URL(match[1],'https://example.com'),params=new URLSearchParams(link.hash.slice(1));assert.equal(params.get('project'),'app');assert.equal(params.get('workspace'),'work');assert.equal(params.get('view'),'collection');assert.deepEqual(JSON.parse(params.get('collection_query')),query);
});

test('the actual initial route rewrite preserves a collection window only for its project',()=>{
 const query={environment:'production',start:'2026-11-01T00:00:00Z',end:'2026-11-01T00:05:00Z',as_of:'2026-11-01T00:06:00Z'},hash='#'+new URLSearchParams({project:'app',view:'collection',collection_query:JSON.stringify(query)}),{c,location}=routeContext(hash);
 vm.runInContext('tab="collection";rememberView()',c);assert.deepEqual(JSON.parse(new URLSearchParams(location.hash.slice(1)).get('collection_query')),query);
 vm.runInContext('project="other";rememberView()',c);assert.equal(new URLSearchParams(location.hash.slice(1)).get('collection_query'),null);
});

test('actual project changes outside Collection discard its query even after a round trip',()=>{
 const query={environment:'production',start:'2026-11-01T00:00:00Z',end:'2026-11-01T00:05:00Z',as_of:'2026-11-01T00:06:00Z'},{c}=routeContext('#'+new URLSearchParams({project:'app',collection_query:JSON.stringify(query)})),picker={value:'other'};
 Object.assign(c,{$:()=>picker,resetJourneys:()=>{},resetAuditWindow:()=>{},resetTraces:()=>{},render:()=>{}});
 const app=fs.readFileSync('static/app.js','utf8'),body=app.match(/\$\('#project'\)\.onchange=\(\)=>\{([^}]+)\}/)[1];
 vm.runInContext(body,c);picker.value='app';vm.runInContext(body,c);assert.equal(vm.runInContext('collectionQuery',c),null);
});
