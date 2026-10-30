'use strict';
const element=id=>document.getElementById(id);
let requestVersion=0,decided=false;
function loadRequest(){
  requestVersion++;decided=false;
  element('code').value=new URLSearchParams(location.hash.slice(1)).get('code')||'';
  element('status').textContent='';
  checkSignIn();
}
window.addEventListener('hashchange',loadRequest);
async function checkSignIn(){
  if(decided)return;
  const version=requestVersion;
  try{
    const response=await fetch('/auth/me',{cache:'no-store'});
    if(!response.ok)throw Error('Sign in before approving.');
    const result=await response.json();
    if(decided||version!==requestVersion)return;
    element('identity').textContent='Signed in as '+result.user.email;
    element('allow').disabled=false;element('deny').disabled=false;element('signin').hidden=true;
  }catch(error){if(decided||version!==requestVersion)return;element('identity').textContent=error.message;element('signin').hidden=false;element('allow').disabled=true;element('deny').disabled=true;}
}
async function decide(approve){
  const version=requestVersion;
  element('allow').disabled=true;element('deny').disabled=true;
  try{
    const response=await fetch('/auth/cli/approve',{method:'POST',headers:{'Content-Type':'application/json','X-Native-CSRF':'1'},body:JSON.stringify({code:element('code').value.trim().toUpperCase(),approve})});
    if(version!==requestVersion)return;
    if(!response.ok)throw Error('Could not approve this request. It may have expired or already been decided.');
    decided=true;
    element('status').textContent=approve?'Approved. Your terminal will continue automatically. You can close this tab.':'Denied. Your terminal will stop.';
  }catch(error){if(version!==requestVersion)return;element('status').textContent=error.message;await checkSignIn();}
}
element('approval').onsubmit=event=>{event.preventDefault();decide(true);};
element('deny').onclick=()=>decide(false);
element('refresh').onclick=checkSignIn;
window.addEventListener('focus',checkSignIn);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)checkSignIn()});
loadRequest();
