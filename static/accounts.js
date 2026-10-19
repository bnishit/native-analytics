'use strict';
let account=null,legacyOwner=false,workspace='',workspaceList=[],workspaceRole='reader';
let pendingPlan=['free','builder','team'].includes(new URLSearchParams(location.hash.slice(1)).get('plan'))?new URLSearchParams(location.hash.slice(1)).get('plan'):null;
let pendingInvite=new URLSearchParams(location.hash.slice(1)).get('invite'),signInToken=new URLSearchParams(location.hash.slice(1)).get('sign-in');
const canEdit=()=>legacyOwner||workspaceRole==='owner'||workspaceRole==='editor';
const isOwner=()=>!legacyOwner&&workspaceRole==='owner';
function showAccountSurface(id){for(const name of ['login','onboarding','invite-screen','shell'])$('#'+name).hidden=name!==id}
function accountError(id,error){$('#'+id).textContent=error.message||String(error)}
async function signOut(){await api('/auth/logout',{});account=null;legacyOwner=false;workspaceList=[];workspaceRole='reader';project='';workspace='';resetJourneys();resetAuditWindow();$('#content').innerHTML='';for(const id of ['project-key','access-key-value'])$('#'+id).textContent='';document.querySelectorAll('dialog[open]').forEach(dialog=>dialog.close());history.replaceState(null,'',pendingInvite?'#'+new URLSearchParams({invite:pendingInvite}):'/app'+(pendingPlan?'#'+new URLSearchParams({plan:pendingPlan}):''));showAccountSurface('login');$('#email-form').hidden=false;$('#link-confirm').hidden=true;await bootAccount()}
async function bootAccount(preferred){
  $('#legacy-login').hidden=true;
  $('#ignore-invite').hidden=!pendingInvite;
  if(signInToken){
    showAccountSurface('login');$('#email-form').hidden=true;
    try{const preview=await api('/auth/link-preview',{token:signInToken});$('#link-confirm').hidden=false;$('#link-confirm').innerHTML='<p>Continue as <strong id="confirm-email"></strong>.</p><button id="confirm-sign-in" class="primary">Sign in to Afterspan</button>';$('#confirm-email').textContent=preview.email;$('#confirm-sign-in').onclick=async()=>{try{await api('/auth/logout',{});await api('/auth/consume-link',{token:signInToken});signInToken=null;$('#link-confirm').hidden=true;history.replaceState(null,'',pendingInvite?'#'+new URLSearchParams({invite:pendingInvite}):'/app'+(pendingPlan?'#'+new URLSearchParams({plan:pendingPlan}):''));await bootAccount()}catch(e){accountError('login-error',e)}}}catch(e){accountError('login-error',e);$('#email-form').hidden=false;signInToken=null;history.replaceState(null,'','/app')}
    return;
  }
  let me;
  try{me=await api('/auth/me')}catch(e){
    account=null;legacyOwner=false;workspaceRole='reader';workspaceList=[];
    showAccountSurface('login');const config=await api('/auth/config');$('#legacy-login').hidden=!config.legacy_login;$('#email-form button').disabled=!config.email_sign_in;$('#login-message').textContent=config.email_sign_in?(config.delivery_mode==='local'?'Local development: emails are saved as private previews. No real email is sent.':''): 'Email sign-in is not configured on this service.';
    if(config.legacy_login){
      let projects;try{projects=await api('/api/projects?workspace=legacy')}catch{return}
      legacyOwner=true;workspace='legacy';workspaceList=[{id:'legacy',name:'Existing projects'}];await openWorkspace(projects);
    }
    return;
  }
  legacyOwner=false;account=me.user;workspaceList=me.workspaces;if(pendingPlan)tab='billing';
  if(pendingInvite){
    showAccountSurface('invite-screen');$('#invite-preview').innerHTML='<p>Checking your invite…</p>';
    try{const invite=await api('/api/invites/preview',{token:pendingInvite});$('#invite-preview').innerHTML=`<p>Join <strong>${esc(invite.name)}</strong> as <strong>${esc(invite.role)}</strong>.</p><p class="muted">Signed in as ${esc(account.email)}. This invite is for ${esc(invite.email)}.</p><p>${invite.role==='owner'?'Owners manage people, project access and agent keys.':invite.role==='editor'?'Editors connect projects and change dashboards. They cannot grant access.':'Readers can inspect analytics. They cannot change projects or grant access.'}</p><button id="accept-invite" class="primary">Accept invite</button>`;$('#accept-invite').onclick=async()=>{try{const joined=await api('/api/invites/accept',{token:pendingInvite});pendingInvite=null;history.replaceState(null,'','/app');await bootAccount(joined.workspace)}catch(e){accountError('invite-error',e)}}}catch(e){accountError('invite-error',e);$('#invite-preview').innerHTML='<p>This invite cannot be used with the current account. Ask an owner for a new invite if it has expired or was revoked.</p><button id="skip-invite">Go to my workspaces</button>';$('#skip-invite').onclick=()=>{pendingInvite=null;history.replaceState(null,'','/app');bootAccount()}}
    return;
  }
  if(!workspaceList.length){showAccountSurface('onboarding');$('#onboarding-user').textContent='Signed in as '+account.email+'. Name your workspace to start. Your projects and team will live here.';return}
  const requested=preferred||workspace||new URLSearchParams(location.hash.slice(1)).get('workspace');workspace=workspaceList.some(w=>w.id===requested)?requested:workspaceList[0].id;
  workspaceRole=workspaceList.find(w=>w.id===workspace).role;
  await openWorkspace();
}
async function openWorkspace(projects){
  $('#workspace').innerHTML=workspaceList.map(w=>`<option value="${esc(w.id)}" ${w.id===workspace?'selected':''}>${esc(w.name)}</option>`).join('');$('#workspace').disabled=legacyOwner;$('#account-role').textContent=legacyOwner?'Existing-owner session':workspaceRole+' · '+account.email;
  $('#new-workspace').hidden=legacyOwner;document.querySelectorAll('nav button').forEach(button=>{button.hidden=legacyOwner&&['team','billing'].includes(button.dataset.tab)});
  $('#new-project').hidden=!canEdit();showAccountSurface('shell');await loadProjects(undefined,projects);
}
async function createWorkspace(form,errorId){if(legacyOwner){accountError(errorId,Error('Sign in by email to create a customer workspace.'));return}try{const result=await api('/api/workspaces',Object.fromEntries(new FormData(form)));if($('#workspace-dialog').open)$('#workspace-dialog').close();project='';resetJourneys();tab=pendingPlan?'billing':'setup';await bootAccount(result.id)}catch(e){accountError(errorId,e)}}
async function teamView(version){
  if(legacyOwner){showError(Error('Sign in by email to manage workspace members.'));return}
  const base='/api/workspaces/'+encodeURIComponent(workspace);
  const members=await api(base+'/members');let invites={invites:[]},keys={keys:[]},audit={entries:[]},mail={messages:[]};
  if(isOwner()){[invites,keys,audit,mail]=await Promise.all([api(base+'/invites'),api(base+'/agent-keys'),api(base+'/audit'),api(base+'/mail')])}
  if(version!==renderVersion)return;
  const options=selected=>['reader','editor','owner'].map(role=>`<option value="${role}" ${role===selected?'selected':''}>${role}</option>`).join('');
  $('#content').innerHTML=panel('Your workspace',`<p><strong>${esc(workspaceList.find(w=>w.id===workspace)?.name)}</strong> · ${members.members.length} ${members.members.length===1?'member':'members'} · Your role: ${esc(workspaceRole)}</p>${isOwner()?'<form id="rename-workspace-form" class="filters"><label>Workspace name<input name="name" id="workspace-name-edit" required maxlength="80"></label><button>Save workspace name</button></form>':''}<button id="create-another-workspace">Create another workspace</button><div class="role-grid"><div><h3>Owner</h3><p>Manage members, invites, access keys and projects.</p></div><div><h3>Editor</h3><p>Connect projects, verify setup and change dashboards or tags.</p></div><div><h3>Reader</h3><p>Inspect analytics, events and journeys. No changes or access grants.</p></div></div><p class="small muted">Every workspace keeps its projects and access separate. At least one owner must remain.</p>`)+
  panel('Members',`<div class="table-scroll"><table class="team-table"><thead><tr><th>Account</th><th>Role</th>${isOwner()?'<th>Change access</th>':''}</tr></thead><tbody>${members.members.map(m=>`<tr><td>${esc(m.email)}${m.id===account.id?' <span class="badge">You</span>':''}</td><td>${esc(m.role)}</td>${isOwner()?`<td><div class="member-actions"><label class="sr-only" for="role-${esc(m.id)}">Role for ${esc(m.email)}</label><select id="role-${esc(m.id)}">${options(m.role)}</select><button class="change-role" data-user="${esc(m.id)}">Apply role</button><button class="remove-member" data-user="${esc(m.id)}">Remove member</button></div></td>`:''}</tr>`).join('')}</tbody></table></div>`)+
  (isOwner()?panel('Invite a teammate',`<p>The invite goes to one email address. They must sign in with that address before joining.</p><form id="invite-form" class="filters"><label>Email address<input name="email" type="email" required maxlength="254"></label><label>Role<select name="role">${options('reader')}</select></label><button class="primary">Send invite</button></form><p id="invite-status" role="status"></p><div class="table-scroll"><table><thead><tr><th>Email</th><th>Role</th><th>Invite</th><th>Email receipt</th><th></th></tr></thead><tbody>${invites.invites.map(i=>{const status=i.revoked?'Revoked':i.used?'Accepted':i.expires<Date.now()/1000?'Expired':'Pending';const delivery=mail.messages.find(m=>m.invite_id===i.id);const state=delivery?delivery.state==='sent'?(mail.delivery_mode==='local'?'Saved local preview':'Accepted by mail server'):delivery.state:'Not queued';return `<tr><td>${esc(i.email)}</td><td>${esc(i.role)}</td><td>${status}</td><td>${esc(state)}</td><td>${!i.revoked&&!i.used?`<button class="resend-invite" data-invite="${esc(i.id)}">Resend invite</button> <button class="revoke-invite" data-invite="${esc(i.id)}">Revoke invite</button>`:''}</td></tr>`}).join('')||'<tr><td colspan="5">No invites yet.</td></tr>'}</tbody></table></div><p class="small muted">Invites last 7 days. Resending replaces the old link. A mail-server receipt does not prove inbox delivery.</p>`)+
  panel('Connect your coding agent',`<p>Create a key for this workspace. Reader is the default. Choose editor for project setup; choose owner only if the agent should manage team access.</p><form id="agent-key-form" class="filters"><label>Key label<input name="label" required maxlength="80" placeholder="Codex on my laptop"></label><label>Agent role<select name="role">${options('reader')}</select></label><label>Expires in days<input name="expires_days" type="number" min="1" max="90" value="30" required></label><button class="primary">Create agent key</button></form><p class="small muted">Keys cannot issue more workspace agent keys. Removing their issuer revokes them; a lower member role lowers their access. For aggregate-only analytics, use a project read key through the API or CLI.</p><div class="table-scroll"><table><thead><tr><th>Label</th><th>Granted / effective role</th><th>Expires</th><th></th></tr></thead><tbody>${keys.keys.map(k=>`<tr><td>${esc(k.label)}</td><td>${esc(k.role)} / ${esc(k.effective_role||'Inactive')}</td><td>${esc(date(k.expires))}</td><td><button class="revoke-agent-key" data-key="${esc(k.id)}">Revoke key</button></td></tr>`).join('')||'<tr><td colspan="4">No workspace agent keys yet.</td></tr>'}</tbody></table></div><details><summary>API and agent setup</summary><p>Use the JSON CLI or the separate workspace MCP bridge with a key in your secret store. The analytics MCP bridge remains aggregate-only.</p><pre id="agent-config"></pre></details>`)+
  panel('Access history',`<p class="small muted">Last 100 access changes in this workspace.</p><div class="table-scroll"><table><thead><tr><th>When</th><th>Action</th><th>Changed by</th><th>Target</th><th>Role</th></tr></thead><tbody>${audit.entries.map(e=>`<tr><td>${esc(date(e.created))}</td><td>${esc(e.action.replaceAll('.',' ').replaceAll('_',' '))}</td><td>${esc(e.actor_label)}</td><td>${esc(e.target_label)}</td><td>${esc(e.role||'—')}</td></tr>`).join('')}</tbody></table></div>`):'')+
  panel('Your account',`<p>${esc(account.email)}</p><p>Lost access to a device? Sign out every browser session and revoke every workspace key you issued. You can sign in again by email.</p><button id="account-sign-out">Sign out</button> <button id="logout-everywhere">Sign out everywhere and revoke my agent keys</button>`);
  $('#create-another-workspace').onclick=()=>$('#new-workspace').click();$('#account-sign-out').onclick=signOut;
  const action=async(path,body,method)=>{try{await api(base+path,body,method);await bootAccount(workspace)}catch(e){showError(e)}};
  document.querySelectorAll('.change-role').forEach(b=>b.onclick=()=>action('/members/'+b.dataset.user,{role:$('#role-'+b.dataset.user).value},'PATCH'));
  document.querySelectorAll('.remove-member').forEach(b=>b.onclick=()=>action('/members/'+b.dataset.user,null,'DELETE'));
  document.querySelectorAll('.revoke-invite').forEach(b=>b.onclick=()=>action('/invites/'+b.dataset.invite,null,'DELETE'));
  document.querySelectorAll('.resend-invite').forEach(b=>b.onclick=()=>action('/invites/'+b.dataset.invite+'/resend',{},'POST'));
  document.querySelectorAll('.revoke-agent-key').forEach(b=>b.onclick=()=>action('/agent-keys/'+b.dataset.key,null,'DELETE'));
  if(isOwner()){
    $('#workspace-name-edit').value=workspaceList.find(w=>w.id===workspace).name;
    $('#rename-workspace-form').onsubmit=async e=>{e.preventDefault();try{await api(base,Object.fromEntries(new FormData(e.target)),'PATCH');await bootAccount(workspace)}catch(error){showError(error)}};
    $('#agent-config').textContent=JSON.stringify({NATIVE_ANALYTICS_URL:location.origin,AFTERSPAN_WORKSPACE:workspace,AFTERSPAN_WORKSPACE_KEY:'Store the key in your agent secret settings'},null,2);
    $('#invite-form').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;try{await api(base+'/invites',Object.fromEntries(new FormData(e.target)));await teamView(version);$('#invite-status').textContent='Invite queued. Refresh to check the mail receipt.'}catch(error){showError(error);button.disabled=false}};
    $('#agent-key-form').onsubmit=async e=>{e.preventDefault();try{const values=Object.fromEntries(new FormData(e.target));values.expires_days=Number(values.expires_days);const key=await api(base+'/agent-keys',values);$('#access-key-value').textContent=key.key;$('#access-key-role').textContent=(workspaceList.find(w=>w.id===key.workspace)?.name||'Workspace')+' · Role: '+key.role+' · Expires '+date(key.expires);$('#access-key-dialog').showModal()}catch(error){showError(error)}};
  }
  $('#logout-everywhere').onclick=async()=>{try{await api('/auth/logout-all',{});account=null;history.replaceState(null,'','/app');showAccountSurface('login')}catch(e){showError(e)}};
}
function wireAccounts(){
  window.addEventListener('hashchange',()=>{const values=new URLSearchParams(location.hash.slice(1));if(['free','builder','team'].includes(values.get('plan')))pendingPlan=values.get('plan');if(values.has('sign-in')||values.has('invite')){signInToken=values.get('sign-in');pendingInvite=values.get('invite');$('#login-error').textContent='';bootAccount().catch(e=>accountError('login-error',e))}});
  $('#ignore-invite').onclick=()=>{pendingInvite=null;signInToken=null;history.replaceState(null,'','/app');$('#login-error').textContent='';$('#link-confirm').hidden=true;$('#email-form').hidden=false;bootAccount()};
  $('#email-form').onsubmit=async e=>{e.preventDefault();$('#login-error').textContent='';const button=e.target.querySelector('button');button.disabled=true;try{await api('/auth/request-link',{email:$('#email').value,...(pendingInvite?{invite:pendingInvite}:{}),...(pendingPlan?{plan:pendingPlan}:{})});const config=await api('/auth/config');$('#login-message').textContent=config.delivery_mode==='local'?'Sign-in link queued for the private local mail preview. No email was sent.':'Check your inbox for a one-time link. It expires in 15 minutes.'}catch(error){accountError('login-error',error)}finally{button.disabled=false}};
  $('#legacy-form').onsubmit=async e=>{e.preventDefault();$('#login-error').textContent='';const button=e.target.querySelector('button');button.disabled=true;try{await api('/auth/login',{password:$('#legacy-password').value});$('#legacy-form').reset();await bootAccount()}catch(error){accountError('login-error',error)}finally{$('#legacy-password').value='';button.disabled=false}};
  $('#onboarding-logout').onclick=signOut;$('#invite-switch').onclick=signOut;
  $('#first-workspace-form').onsubmit=e=>{e.preventDefault();createWorkspace(e.target,'onboarding-error')};
  $('#workspace-form').onsubmit=e=>{e.preventDefault();createWorkspace(e.target,'error')};
  $('#new-workspace').onclick=()=>{if(legacyOwner){showError(Error('Sign in by email to create a customer workspace.'));return}$('#workspace-form').reset();$('#workspace-dialog').showModal()};
  $('#workspace').onchange=()=>{workspace=$('#workspace').value;project='';resetJourneys();bootAccount(workspace).catch(showError)};
  $('#access-key-saved').onclick=()=>{$('#access-key-value').textContent='';$('#access-key-dialog').close();render()};
  $('#access-key-dialog').addEventListener('close',()=>{$('#access-key-value').textContent=''});
  $('#project-dialog').addEventListener('close',()=>{$('#project-key').textContent=''});
}
