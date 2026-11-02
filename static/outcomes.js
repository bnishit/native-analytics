'use strict';
let outcomeSelection='',outcomeReport=null;
function outcomeCounts(report){
 return Object.entries(report.counts).map(([name,count])=>`<div><strong>${fmt(count)}</strong><span>${esc(name)}</span></div>`).join('');
}
async function outcomesView(version){
 const listed=await api(url('/api/outcomes/goals'));if(version!==renderVersion)return;
 if(!listed.goals.length){
  $('#content').innerHTML=panel('Define a useful result','<p>No outcome goal is saved for this project. Set the workflow, deadline, required checks and completion evidence. Tasks will be judged against that version.</p>')+outcomeGoalForm();bindOutcomeGoalForm();return;
 }
 const selected=listed.goals.find(g=>g.id===outcomeSelection)||listed.goals[0];outcomeSelection=selected.id;
 const report=await api(url('/api/outcomes/evaluate'),{goal_id:selected.id,version:selected.version,days});if(version!==renderVersion)return;outcomeReport=report;
 $('#content').innerHTML=panel('Outcome goal',`<label>Goal<select id="outcome-goal">${listed.goals.map(g=>`<option value="${esc(g.id)}" ${g.id===selected.id?'selected':''}>${esc(g.definition.title)} · v${g.version}</option>`).join('')}</select></label><p>${esc(selected.definition.workflow)} · ${esc(selected.definition.environment)} · ${fmt(selected.definition.deadline_seconds)} second deadline</p><p>Criteria: ${selected.definition.checks.map(c=>`${esc(c.name)} v${c.version} (${esc(c.method)})`).join(', ')}. Required milestones: ${selected.definition.milestones.map(esc).join(', ')}.</p>`)+
 panel('Observed task results',`<div class="trace-evidence">${outcomeCounts(report)}</div><p>${fmt(report.mature_denominator)} mature observed tasks · satisfied fraction ${report.satisfied_fraction===null?'unavailable':fmt(report.satisfied_fraction*100)+'%'}. Waiting tasks have not reached the deadline. Withheld and unclassified mature tasks stay in the denominator.</p><p>These results apply declared criteria to producer evidence. They do not establish independent business correctness or causation.</p>`)+
 panel('Tasks to inspect',report.tasks.length?`<table><thead><tr><th>Task trace</th><th>Outcome</th><th>Evidence</th></tr></thead><tbody>${report.tasks.map(t=>`<tr><td><a href="#${new URLSearchParams({workspace,project,view:'traces',days,trace:t.trace,trace_query:JSON.stringify(report.trace_query)})}">${esc(t.trace)}</a></td><td>${esc(t.state)}</td><td>${t.reasons.map(esc).join(', ')||'Declared criteria satisfied'} · ${fmt(t.attempts)} attempts</td></tr>`).join('')}</tbody></table><p>${report.total>report.tasks.length?'More tasks exist; use frozen API/MCP pagination to inspect the full set.':''}</p>`:'<p>No task traces are observed in this entry window. This does not prove no tasks occurred.</p>')+
 panel('Evidence window',`<p>Task entries: ${esc(report.snapshot.start)} → ${esc(report.snapshot.end)}. Receipts through ${esc(report.snapshot.as_of)}.</p>${report.limits.map(l=>`<p class="small muted">${esc(l)}</p>`).join('')}`)+outcomeGoalForm();
 $('#outcome-goal').onchange=()=>{outcomeSelection=$('#outcome-goal').value;outcomesView(renderVersion).catch(showError)};
 for(const a of $('#content').querySelectorAll('a[href^="#"]'))a.onclick=e=>{e.preventDefault();const p=new URLSearchParams(a.hash.slice(1));traceReference=p.get('trace');traceQuery=JSON.parse(p.get('trace_query'));tab='traces';render()};
 bindOutcomeGoalForm();
}
function outcomeGoalForm(){
 if(!canEdit())return '';
 return panel('Save a goal',`<form id="outcome-form"><label>Goal title<input name="title" required maxlength="100" placeholder="Verified answer delivered"></label><label>Workflow<input name="workflow" required pattern="[a-z][a-z0-9_.-]{0,63}" placeholder="document_export"></label><label>Environment<select name="environment">${environmentOptions('production')}</select></label><label>Deadline in seconds<input name="deadline_seconds" type="number" min="1" max="604800" value="60" required></label><label>Required check name<input name="check_name" required pattern="[a-z][a-z0-9_.-]{0,63}" placeholder="source_facts"></label><label>Check version<input name="check_version" type="number" min="1" max="1000000" value="1" required></label><label>Check method<select name="method"><option>source</option><option>code</option><option>human</option><option>model</option></select></label><p>This form requires an evidence reference and a check before delivery, plus completion and delivery milestones. The API supports other explicit criteria and immutable goal versions.</p><button class="primary">Save goal</button></form>`);
}
function bindOutcomeGoalForm(){
 const form=$('#outcome-form');if(!form)return;const id=crypto.randomUUID();
 form.onsubmit=async e=>{e.preventDefault();const v=Object.fromEntries(new FormData(form));try{await api(url('/api/outcomes/goals/'+id,{revision:0}),{title:v.title,workflow:v.workflow,environment:v.environment,deadline_seconds:Number(v.deadline_seconds),checks:[{name:v.check_name,version:Number(v.check_version),method:v.method,phase:'before_delivery',evidence_required:true}],milestones:['completion','delivery']},'PUT');outcomeSelection=id;await outcomesView(renderVersion)}catch(error){showError(error)}};
}
