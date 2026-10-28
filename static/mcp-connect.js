'use strict';
const el=id=>document.getElementById(id);
const labels={'analytics:read':'Read aggregate analytics and saved charts','charts:write':'Create saved charts','audit:read':'Read privacy-safe request traces and checks'};
let version=0;
async function api(path,body,method){
  const r=await fetch(path,{method:method||(body?'POST':'GET'),headers:{'Content-Type':'application/json','X-Native-CSRF':'1'},body:body?JSON.stringify(body):undefined,cache:'no-store'});
  const d=await r.json();if(!r.ok)throw Error(typeof d.detail==='string'?d.detail:'Could not complete this request.');return d;
}
async function load(){
  const current=++version;el('request').hidden=true;el('status').textContent='';
  try{
    const me=await api('/auth/me');if(current!==version)return;el('identity').textContent='Signed in as '+me.user.email;
    const list=await api('/auth/mcp/connections');if(current!==version)return;el('connections').replaceChildren();
    for(const c of list.connections){const p=document.createElement('p');p.textContent=c.client+' · '+c.project+' · '+c.scopes.map(s=>labels[s]||s).join(', ')+' ';const b=document.createElement('button');b.textContent='Revoke';b.onclick=async()=>{try{await api('/auth/mcp/connections/'+encodeURIComponent(c.id),null,'DELETE');load();}catch(e){el('status').textContent=e.message;}};p.append(b);el('connections').append(p);}
    const rid=new URLSearchParams(location.hash.slice(1)).get('request');if(!rid)return;
    const d=await api('/auth/mcp/request?request_id='+encodeURIComponent(rid));if(current!==version)return;
    el('client').textContent=d.client;el('destination').textContent=d.redirect_uri;el('permissions').textContent=d.scopes.map(s=>labels[s]||s).join(' · ');
    el('project').replaceChildren();for(const p of d.projects){if(d.scopes.includes('charts:write')&&p.role==='reader')continue;const option=document.createElement('option');option.value=p.id;option.textContent=p.name;el('project').append(option);}
    el('approve').disabled=!el('project').options.length;el('deny').disabled=false;el('request').hidden=false;
  }catch(e){if(current===version)el('status').textContent=e.message;}
}
async function decide(approve){
  const current=version;el('approve').disabled=true;el('deny').disabled=true;
  try{const d=await api('/auth/mcp/decision',{request_id:new URLSearchParams(location.hash.slice(1)).get('request'),project:el('project').value,approve});if(current!==version)return;location.assign(d.redirect_uri);}
  catch(e){if(current===version){el('status').textContent=e.message;el('approve').disabled=!el('project').options.length;el('deny').disabled=false;}}
}
el('approval').onsubmit=e=>{e.preventDefault();decide(true);};el('deny').onclick=()=>decide(false);el('refresh').onclick=load;window.addEventListener('hashchange',load);load();
