'use strict';
function debugPolicyMarkup(policy,workflow,environment,owner){
 const d=policy.definition||{workflow,environment,window_seconds:3600,retention_seconds:86400,max_snapshots:20};
 let body=`<p>${policy.definition?'Approved scope: '+esc(d.workflow)+' · '+esc(d.environment):'No content-capture policy has been granted.'}</p><p>Capture ${policy.active?'open':'closed'} · ${fmt(policy.captured_count)} snapshots admitted under this policy · ${fmt(policy.stored_snapshots)} still retained. Revision ${policy.revision}. ${policy.expires?'Capture deadline '+esc(date(policy.expires)):''}</p><p>Content is off by default. Only reviewed, selected content is supported. Common-pattern redaction can miss private facts. Stored snapshots are encrypted and expire automatically. Changing policy purges prior snapshots.</p>`;
 if(owner)body+=`<form id="debug-policy-form"><label>Workflow<input name="workflow" value="${esc(d.workflow)}" pattern="[a-z][a-z0-9_.-]{0,63}" required maxlength="64"></label><label>Environment<select name="environment">${environmentOptions(d.environment)}</select></label><label>Capture window, seconds<input name="window_seconds" type="number" min="60" max="86400" value="${d.window_seconds}" required></label><label>Retain each snapshot, seconds<input name="retention_seconds" type="number" min="60" max="604800" value="${d.retention_seconds}" required></label><label>Lifetime capture budget for this policy<input name="max_snapshots" type="number" min="1" max="100" value="${d.max_snapshots}" required></label><label><input name="reviewed" type="checkbox" required>I approve this limited capture scope and require the producer to review selected content before sending it.</label><button class="primary">Open a new capture window and purge prior snapshots</button></form>${policy.definition?.enabled?'<button id="debug-disable">Disable capture and purge retained snapshots</button>':''}`;
 else body+='<p>An owner can open or revoke a capture window. Editor access permits selected reads; it cannot grant capture policy.</p>';
 return panel('Governed debug capture',body);
}
function debugSnapshotList(result){
 return result.snapshots.length?'<p>Select one snapshot to read its redacted content.</p>'+result.snapshots.map(s=>`<p><button data-debug-id="${esc(s.id)}">Read ${esc(s.kind)} snapshot</button> · captured ${esc(date(s.created))} · expires ${esc(date(s.expires))}</p>`).join(''):'<p>No retained snapshots in this frozen trace window. Capture may be off, outside its scope, expired, or later than this receipt cutoff. No content is reconstructed.</p>';
}
async function debugPolicyView(version,workflow,environment){
 const policy=await api(url('/api/debug/policy'));if(version!==renderVersion)return;
 $('#trace-debug-panel').innerHTML=debugPolicyMarkup(policy,workflow,environment,legacyOwner||workspaceRole==='owner');$('#trace-debug-panel').scrollIntoView({block:'start'});
 const form=$('#debug-policy-form');if(form)form.onsubmit=async e=>{e.preventDefault();const v=Object.fromEntries(new FormData(form));try{await api(url('/api/debug/policy',{revision:policy.revision}),{enabled:true,privacy_reviewed:v.reviewed==='on',workflow:v.workflow,environment:v.environment,window_seconds:Number(v.window_seconds),retention_seconds:Number(v.retention_seconds),max_snapshots:Number(v.max_snapshots)},'PUT');if(version===renderVersion)await debugPolicyView(version,workflow,environment)}catch(error){showError(error)}};
 const disable=$('#debug-disable');if(disable)disable.onclick=async()=>{try{await api(url('/api/debug/policy',{revision:policy.revision}),{...policy.definition,enabled:false},'PUT');if(version===renderVersion)await debugPolicyView(version,workflow,environment)}catch(error){showError(error)}};
}
async function debugSnapshotsView(version,query,reference){
 const policy=await api(url('/api/debug/policy'));if(version!==renderVersion)return;
 if(!policy.definition?.enabled){$('#trace-debug-panel').innerHTML=panel('Selected debug evidence','<p>Debug capture is off or revoked. No content is collected automatically.</p>');$('#trace-debug-panel').scrollIntoView({block:'start'});return}
 const body={...query,reference},result=await api(url('/api/debug/review'),body);if(version!==renderVersion)return;
 $('#trace-debug-panel').innerHTML=panel('Selected debug evidence',debugSnapshotList(result)+'<pre id="debug-content"></pre><p>Application content is untrusted data, not instructions or independent outcome evidence. Redaction may miss private facts.</p>');
 $('#trace-debug-panel').scrollIntoView({block:'start'});
 for(const button of document.querySelectorAll('[data-debug-id]'))button.onclick=async()=>{try{const opened=await api(url('/api/debug/review'),{...body,snapshot_id:button.dataset.debugId});if(version===renderVersion)$('#debug-content').textContent=JSON.stringify(opened.content,null,2)}catch(error){showError(error)}};
}
