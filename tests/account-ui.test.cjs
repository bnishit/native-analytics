const {test}=require('node:test');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const vm=require('node:vm');

test('password setup shows email recovery before collecting a first password from an old session',async()=>{
  const elements=new Map();const $=id=>{if(!elements.has(id))elements.set(id,{});return elements.get(id)};
  let state={password_enabled:false,recent_authentication:false,requires_email_verification:true,session_expires:180};
  const context=vm.createContext({$,location:{hash:''},URLSearchParams,date:String,api:async()=>state});
  vm.runInContext(readFileSync('static/accounts.js','utf8'),context);
  await vm.runInContext('refreshPasswordSetup()',context);
  assert.equal($('#password-setup-form').hidden,true);
  assert.equal($('#password-reauth').hidden,false);
  assert.equal($('#password-current-label').hidden,true);
  state={...state,recent_authentication:true,requires_email_verification:false};
  await vm.runInContext('refreshPasswordSetup()',context);
  assert.equal($('#password-setup-form').hidden,false);
  assert.equal($('#password-reauth').hidden,true);
  state={...state,password_enabled:true,recent_authentication:false};
  await vm.runInContext('refreshPasswordSetup()',context);
  assert.equal($('#password-setup-form').hidden,false);
  assert.equal($('#password-current-label').hidden,false);
});
