const {test}=require('node:test');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const vm=require('node:vm');

test('a new approval link in the same tab replaces the old code and survives reload',async()=>{
  const elements=new Map(),handlers={},location={hash:'#code=111111111111'};
  const element=id=>{if(!elements.has(id))elements.set(id,{});return elements.get(id)};
  const context=vm.createContext({document:{getElementById:element},location,URLSearchParams,
    window:{addEventListener:(event,fn)=>handlers[event]=fn},
    fetch:async()=>({ok:true,json:async()=>({user:{email:'test@example.invalid'}})})});
  vm.runInContext(readFileSync('static/cli-login.js','utf8'),context);
  assert.equal(element('code').value,'111111111111');
  element('status').textContent='Approved';
  location.hash='#code=222222222222';handlers.hashchange();
  assert.equal(element('code').value,'222222222222');
  assert.equal(element('status').textContent,'');
  assert.equal(location.hash,'#code=222222222222');
  element('code').value='';vm.runInContext('loadRequest()',context);
  assert.equal(element('code').value,'222222222222');
});

test('an old approval response cannot mark a new request approved',async()=>{
  const elements=new Map(),handlers={},location={hash:'#code=111111111111'};
  const element=id=>{if(!elements.has(id))elements.set(id,{});return elements.get(id)};
  let finish;
  const context=vm.createContext({document:{getElementById:element},location,URLSearchParams,
    window:{addEventListener:(event,fn)=>handlers[event]=fn},
    fetch:async path=>path==='/auth/cli/approve'?new Promise(resolve=>finish=resolve):({ok:true,json:async()=>({user:{email:'test@example.invalid'}})})});
  vm.runInContext(readFileSync('static/cli-login.js','utf8'),context);
  const pending=vm.runInContext('decide(true)',context);
  location.hash='#code=222222222222';handlers.hashchange();
  finish({ok:true});await pending;
  assert.equal(element('code').value,'222222222222');
  assert.equal(element('status').textContent,'');
});
