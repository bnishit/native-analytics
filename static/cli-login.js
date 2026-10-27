'use strict';
const element=id=>document.getElementById(id);
element('code').value=new URLSearchParams(location.hash.slice(1)).get('code')||'';
history.replaceState(null,'',location.pathname);
async function checkSignIn(){
  try{
    const response=await fetch('/auth/me',{cache:'no-store'});
    if(!response.ok)throw Error('Sign in before approving.');
    const result=await response.json();
    element('identity').textContent='Signed in as '+result.user.email;
    element('allow').disabled=false;element('deny').disabled=false;element('signin').hidden=true;
  }catch(error){element('identity').textContent=error.message;element('signin').hidden=false;element('allow').disabled=true;element('deny').disabled=true;}
}
async function decide(approve){
  element('allow').disabled=true;element('deny').disabled=true;
  try{
    const response=await fetch('/auth/cli/approve',{method:'POST',headers:{'Content-Type':'application/json','X-Native-CSRF':'1'},body:JSON.stringify({code:element('code').value.trim().toUpperCase(),approve})});
    if(!response.ok)throw Error('Could not approve this request. It may have expired or already been decided.');
    element('status').textContent=approve?'Approved. Your terminal will continue automatically. You can close this tab.':'Denied. Your terminal will stop.';
  }catch(error){element('status').textContent=error.message;await checkSignIn();}
}
element('approval').onsubmit=event=>{event.preventDefault();decide(true);};
element('deny').onclick=()=>decide(false);
element('refresh').onclick=checkSignIn;
checkSignIn();
