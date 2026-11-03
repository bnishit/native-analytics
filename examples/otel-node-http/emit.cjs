'use strict';
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { createHash } = require('node:crypto');
const { NodeSDK } = require('@opentelemetry/sdk-node');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-proto');
const { HttpInstrumentation } = require('@opentelemetry/instrumentation-http');
const { BatchSpanProcessor } = require('@opentelemetry/sdk-trace');
const { resourceFromAttributes } = require('@opentelemetry/resources');
const { trace, SpanStatusCode } = require('@opentelemetry/api');
let config, origin;
try {
 config = JSON.parse(readFileSync(0, 'utf8'));
 origin = new URL(config.origin);
 assert(origin.origin === config.origin);
 assert(origin.protocol === 'https:' || (origin.protocol === 'http:' && origin.hostname === '127.0.0.1'));
 assert(typeof config.key === 'string' && config.key.length > 10);
} catch { console.error('invalid_configuration'); process.exit(1); }
// A standalone synthetic probe must not inherit unrelated telemetry destinations or headers.
for (const name of Object.keys(process.env)) if (name.startsWith('OTEL_')) delete process.env[name];
process.env.OTEL_LOG_LEVEL = 'NONE';
const results = [], captured = [];
const exporter = new OTLPTraceExporter({url: origin.origin + '/v1/traces', headers:{Authorization:'Bearer '+config.key}, timeoutMillis:10000});
const guardedExporter = {
 export(spans, done) {
  captured.push(...spans.map(s => ({trace:s.spanContext().traceId, span:s.spanContext().spanId, parent:s.parentSpanContext?.spanId??null, kind:s.kind, status:s.status.code})));
  exporter.export(spans, r => {results.push(r.code);done(r)});
 },
 shutdown:()=>exporter.shutdown()
};
const processor = new BatchSpanProcessor({exporter:guardedExporter,maxQueueSize:32,maxExportBatchSize:32,scheduledDelayMillis:60000,exportTimeoutMillis:15000});
const sdk = new NodeSDK({
 autoDetectResources:false, metricReaders:[], logRecordProcessors:[],
 resource:resourceFromAttributes({'service.name':'synthetic-node-http','afterspan.workflow':'node_http_interop','afterspan.environment':'test','afterspan.traffic_kind':'synthetic'}),
 spanProcessors:[processor], instrumentations:[new HttpInstrumentation({startIncomingSpanHook:()=>({'afterspan.step':'receive'}),startOutgoingSpanHook:()=>({'afterspan.step':'http'})})]
});
sdk.start();
// Load http only after instrumentation: the actual library adds client/server spans and traceparent.
const http = require('node:http');
const tracer=trace.getTracer('afterspan-node-http-validation');
const headersSeen=[];
const server=http.createServer((req,res)=>{
 headersSeen.push(req.headers.traceparent);
 tracer.startActiveSpan('synthetic inner work', async span=>{
  try { await new Promise(resolve=>setTimeout(resolve,10)); span.setStatus({code:SpanStatusCode.OK}); res.end('OK'); }
  finally { span.end(); }
 });
});
function get(port) {
 return new Promise((resolve,reject)=>{
  const req=http.get({hostname:'127.0.0.1',port,path:'/synthetic-private-path?canary=omit-me'},res=>{
   let bytes=0;res.on('data',chunk=>{bytes+=chunk.length});res.on('end',()=>res.statusCode===200&&bytes===2?resolve():reject(Error('unexpected response')));
  });req.setTimeout(5000,()=>req.destroy(Error('timeout')));req.on('error',reject);
 });
}
(async()=>{
 try {
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const roots=await Promise.all([0,1].map(()=>tracer.startActiveSpan('synthetic concurrent request',async span=>{
   try {const id=span.spanContext().traceId;await get(server.address().port);span.setStatus({code:SpanStatusCode.OK});return id;}
   finally {span.end()}
  })));
  await new Promise(resolve=>server.close(resolve));
  await processor.forceFlush();
  assert.equal(roots.length,2);assert.notEqual(roots[0],roots[1]);assert.equal(captured.length,8);assert.equal(results.length,1);assert(results.every(code=>code===0));
  for(const id of roots){const group=captured.filter(s=>s.trace===id);assert.equal(group.length,4);assert.equal(group.filter(s=>!s.parent).length,1);assert.equal(group.filter(s=>s.kind===1).length,1);assert.equal(group.filter(s=>s.kind===2).length,1);assert(headersSeen.some(h=>h?.split('-')[1]===id));}
  console.log(JSON.stringify({node:process.version,workflow:'node_http_interop',traces:roots.map(id=>({otel_trace_id:id,namespace_digest:createHash('sha256').update('afterspan-otel-v1\0'+id).digest('hex')})),spans:captured,export_results:results,propagated_requests:headersSeen.length,storage_verified:false}));
 } finally {server.close();await sdk.shutdown()}
})().catch(()=>{console.error('node_otlp_validation_failed');process.exitCode=1});
