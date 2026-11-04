'use strict';
const el=id=>document.getElementById(id);
const labels={'followups:write':'Read, start or control bounded release follow-ups (requires audit and experiment access; no code, model, Slack or deployment actions)','releases:write':'Read provider targets and prepare deployment evidence (editor access; owner adoption required; no merge or deploy)','collection:write':'Declare or change expected collection sources and reporting intervals','debug:read':'Read selected redacted debug content (editor access; no capture-policy changes)','experiments:write':'Read reviewed test inputs/results and manage regression experiments','goals:write':'Create and version workflow outcome goals','analytics:read':'Read aggregate analytics and saved charts','charts:write':'Create saved charts','audit:read':'Read privacy-safe request traces, checks and collection health'};
let version=0;
async function api(path,body,method){
  const r=await fetch(path,{method:method||(body?'POST':'GET'),headers:{'Content-Type':'application/json','X-Native-CSRF':'1'},body:body?JSON.stringify(body):undefined,cache:'no-store'});
  const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'Could not complete this request.');return d;
}
async function load(){
  const current=++version;el('request').hidden=true;el('permissions').disabled=false;el('project').disabled=false;el('status').textContent='';
  try{
    const me=await api('/auth/me');if(current!==version)return;el('identity').textContent='Signed in as '+me.user.email;
    const list=await api('/auth/mcp/connections');if(current!==version)return;el('connections').replaceChildren();
    for(const c of list.connections){const p=document.createElement('p');p.textContent=c.client+' · '+c.project+' · '+c.scopes.map(s=>labels[s]||s).join(', ')+' ';const b=document.createElement('button');b.textContent='Revoke';b.onclick=async()=>{try{await api('/auth/mcp/connections/'+encodeURIComponent(c.id),null,'DELETE');load();}catch(e){el('status').textContent=e.message;}};p.append(b);el('connections').append(p);}
    const rid=new URLSearchParams(location.hash.slice(1)).get('request');if(!rid)return;
    const d=await api('/auth/mcp/request?request_id='+encodeURIComponent(rid));if(current!==version)return;
    el('client').textContent=d.client;el('destination').textContent=d.redirect_uri;el('permissions').replaceChildren();const legend=document.createElement('legend');legend.textContent='Choose permissions for this task';el('permissions').append(legend);
    const defaultRead=d.scopes.includes('audit:read')?'audit:read':'analytics:read';const inputs=d.scopes.map(scope=>{const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.value=scope;input.checked=scope===defaultRead;label.append(input,document.createTextNode(labels[scope]||scope));el('permissions').append(label);return input;});
    function projects(){const selected=el('project').value,chosen=inputs.filter(input=>input.checked).map(input=>input.value),editor=chosen.some(scope=>!['audit:read','analytics:read'].includes(scope));el('project').replaceChildren();for(const p of d.projects){if(editor&&p.role==='reader')continue;const option=document.createElement('option');option.value=p.id;option.textContent=p.name;el('project').append(option);}if([...el('project').options].some(option=>option.value===selected))el('project').value=selected;el('approve').disabled=!chosen.length||!el('project').options.length;el('status').textContent=!chosen.length?'Choose at least one permission.':!el('project').options.length?'These permissions need editor access to a project. Choose read access or ask your workspace owner for help.':'';}
    inputs.forEach(input=>input.onchange=projects);projects();el('deny').disabled=false;el('request').hidden=false;
  }catch(e){if(current===version)el('status').textContent=e.message;}
}
async function decide(approve){
  const current=version;el('approve').disabled=true;el('deny').disabled=true;el('permissions').disabled=true;el('project').disabled=true;
  try{const d=await api('/auth/mcp/decision',{request_id:new URLSearchParams(location.hash.slice(1)).get('request'),project:el('project').value,approve,...(approve?{scopes:[...el('permissions').querySelectorAll('input:checked')].map(input=>input.value)}:{})});if(current!==version)return;location.assign(d.redirect_uri);}
  catch(e){if(current===version){el('status').textContent=e.message;el('permissions').disabled=false;el('project').disabled=false;el('approve').disabled=!el('project').options.length||!el('permissions').querySelectorAll('input:checked').length;el('deny').disabled=false;}}
}
el('approval').onsubmit=e=>{e.preventDefault();decide(true);};el('deny').onclick=()=>decide(false);el('refresh').onclick=load;window.addEventListener('hashchange',load);load();
