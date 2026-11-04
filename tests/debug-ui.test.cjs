const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
function context(extra={}){
 const c=vm.createContext({panel:(title,body)=>title+body,esc:v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),fmt:String,date:String,environmentOptions:v=>'<option>'+v+'</option>',...extra});
 vm.runInContext(fs.readFileSync('static/debug.js','utf8'),c);return c;
}
test('debug policy stays closed by default and only owners see capture controls',()=>{
 const c=context();const policy={revision:0,definition:null,active:false,captured_count:0,stored_snapshots:0};
 const editor=vm.runInContext('debugPolicyMarkup('+JSON.stringify(policy)+',"document_export","test",false)',c);
 assert.match(editor,/No content-capture policy/);assert.match(editor,/Capture closed/);assert.doesNotMatch(editor,/<form/);assert.match(editor,/An owner can open or revoke/);
 const owner=vm.runInContext('debugPolicyMarkup('+JSON.stringify(policy)+',"document_export","test",true)',c);
 assert.match(owner,/required>I approve/);assert.match(owner,/purge prior snapshots/);assert.match(owner,/Lifetime capture budget/);
});
test('metadata list never renders unsolicited content or raw trace IDs',()=>{
 const c=context();const data={snapshots:[{id:'snapshot-id',kind:'context',created:1,expires:2,content:'private-value',trace_id:'raw-private-trace'}]};
 const html=vm.runInContext('debugSnapshotList('+JSON.stringify(data)+')',c);
 assert.match(html,/Read context snapshot/);assert.doesNotMatch(html,/private/);
 assert.match(vm.runInContext('debugSnapshotList({snapshots:[]})',c),/No content is reconstructed/);
});
test('snapshot content requires a selected read and renders as literal text',async()=>{
 let text='',markup='',calls=0;const button={dataset:{debugId:'chosen'}};
 const c=context({renderVersion:1,url:v=>v,document:{querySelectorAll:()=>[button]},$:id=>id==='#debug-content'?{set textContent(v){text=v}}:{set innerHTML(v){markup=v},scrollIntoView(){}},api:async(_url,body)=>{
  calls++;if(calls===1)return {definition:{enabled:true}};
  if(!body.snapshot_id)return {snapshots:[{id:'chosen',kind:'context',created:1,expires:2}]};
  return {content:{answer:'<img src=x onerror=unsafe>'}};
 }});
 await vm.runInContext('debugSnapshotsView(1,{environment:"test"},"trace_safe")',c);
 assert.equal(calls,2);assert.equal(text,'');assert.doesNotMatch(markup,/<img/);
 await button.onclick();assert.equal(calls,3);assert.equal(text,JSON.stringify({answer:'<img src=x onerror=unsafe>'},null,2));
});
test('check fixtures distinguish available from replayed and explain missing evidence',()=>{
 const c=context();
 const available=vm.runInContext('replayAvailability({status:"available",reasons:[]})',c);
 assert.match(available,/not yet replayed/);assert.match(available,/Full request replay is unavailable/);assert.match(available,/Review expected facts separately/);
 const blocked=vm.runInContext('debugSnapshotList({snapshots:[],replay:{status:"blocked",reasons:["source_unavailable","not_retained_in_window"]}})',c);
 assert.match(blocked,/replay blocked/);assert.match(blocked,/source read is unavailable/);assert.match(blocked,/No fixture is retained/);
});
