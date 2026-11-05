const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('static/app.js','utf8');
const viewCode=source.slice(source.indexOf('function chartResult('),source.indexOf("$('#logout').onclick"));
function harness(text,{stale=false}={}){
 const requests=[],content={innerHTML:''};
 const query={event:'http.request',distinct_id:'support:example',start:'2026-11-01T00:00:00Z',end:'2026-11-02T00:00:00Z',as_of:'2026-11-02T00:00:00Z'};
 const context=vm.createContext({metricQueryText:()=>text,viewParams:new URLSearchParams(text===null?'':'metric_query='+encodeURIComponent(text)),renderVersion:1,
  $:()=>content,canEdit:()=>false,url:path=>path,esc:s=>String(s).replaceAll('<','&lt;'),fmt:String,
  graph:()=>'<svg>Real query chart</svg>',bars:()=>'',panel:(title,body)=>'<section><h2>'+title+'</h2>'+body+'</section>',
  api:async(path,body)=>{requests.push({path,body});if(stale)context.renderVersion=2;return path==='/api/charts'?{charts:[]}:{total:4,matched_events:4,query,series:[],breakdown:[]};}});
 vm.runInContext(viewCode,context);
 return {context,requests,content,query};
}
test('reader opens the exact frozen query without saving a chart',async()=>{
 const h=harness(JSON.stringify({event:'http.request',distinct_id:'support:example'}));
 await vm.runInContext('chartsView(1)',h.context);
 assert.deepEqual(h.requests.map(r=>r.path),['/api/query','/api/charts']);
 assert.equal(h.requests[0].body.distinct_id,'support:example');
 assert.match(h.content.innerHTML,/4 matching events/);assert.match(h.content.innerHTML,/Snapshot/);
 assert.match(h.content.innerHTML,/Read-only preview/);assert.doesNotMatch(h.content.innerHTML,/Build a graph/);
});
test('malformed and oversized links do not make requests',async()=>{
 for(const text of ['{','[]','null','"text"',' '.repeat(32769)]){
  const h=harness(text);await assert.rejects(vm.runInContext('chartsView(1)',h.context),/usage-query link is invalid/);
  assert.equal(h.requests.length,0);
 }
});
test('normal saved-chart visits do not execute a query and stale results do not render',async()=>{
 const normal=harness(null);await vm.runInContext('chartsView(1)',normal.context);
 assert.deepEqual(normal.requests.map(r=>r.path),['/api/charts']);
 const stale=harness('{}',{stale:true});await vm.runInContext('chartsView(1)',stale.context);
 assert.equal(stale.content.innerHTML,'');assert.deepEqual(stale.requests.map(r=>r.path),['/api/query']);
});

test('a linked query stays bound to its project and workspace',()=>{
 const code=source.slice(source.indexOf('function metricQueryText('),source.indexOf('function rememberView('));
 const context=vm.createContext({project:'support',workspace:'owner',viewParams:new URLSearchParams({project:'support',workspace:'owner',metric_query:'{}'})});
 vm.runInContext(code,context);assert.equal(vm.runInContext('metricQueryText()',context),'{}');
 context.project='other';assert.equal(vm.runInContext('metricQueryText()',context),null);
 context.project='support';context.workspace='other';assert.equal(vm.runInContext('metricQueryText()',context),null);
});
