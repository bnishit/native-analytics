/** Node tracing context and metadata-only receipts. Flush via the host's after/waitUntil hook. */
import { AsyncLocalStorage } from 'node:async_hooks';
import { createHash, randomBytes, randomUUID } from 'node:crypto';
import { isUint8Array } from 'node:util/types';
import type { createAnalytics } from './native-analytics.ts';
import { ModelObservation, type ModelUsage } from './model-observation.ts';

type TrafficKind = 'user'|'synthetic'|'internal'|'unknown';
type Environment = 'production'|'staging'|'development'|'test'|'local';
type Step = 'receive'|'queue'|'processing'|'model'|'tool'|'retrieval'|'storage'|'http'|'response';
type Method = 'source'|'code'|'model'|'human';
type Phase = 'before_delivery'|'after_delivery';
type Result = 'passed'|'failed'|'unknown';
type Milestone = 'completion'|'delivery'|'usefulness';
type Disposition = 'delivered'|'withheld'|'clarification'|'partial';
type SpanOptions = {milestone?:Milestone;disposition?:Disposition};
type TaskOptions = {workflow?:string;environment?:Environment;releaseSha?:string;context?:TraceContext;trafficKind?:TrafficKind};
class StreamCancelled extends Error {}
class ResponseFailed extends Error {
 readonly code:'rejected'|'unavailable';
 constructor(status:number){super();this.code=status===0||status>=500?'unavailable':'rejected'}
}
function failure(error:unknown){return error instanceof StreamCancelled?{state:'cancelled',code:'cancelled'}:{state:'failed',code:error instanceof ResponseFailed?error.code:'internal'}}
export interface TraceContext {
 traceId:string; attemptId:string; attemptNumber:number; workflow:string; environment:Environment;
 requestStartedAtMs:number; releaseSha?:string; spanId?:string; sampled:boolean; trafficKind?:TrafficKind;
}
type CheckExecution = 'completed'|'not_run';
type CheckReason = 'evidence_absent'|'evidence_binding_rejected';
type Check = {name:string;version:number;result:Result;required:boolean;method:Method;phase:Phase;evidence_ref?:string;execution?:CheckExecution;reason_code?:CheckReason};
const current = new AsyncLocalStorage<TraceContext>();
const identifier=/^[a-z][a-z0-9_.-]{0,63}$/;
const uuid4=/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const steps=new Set(['receive','queue','processing','model','tool','retrieval','storage','http','response']);
export function mappedId(traceId:string,spanId?:string):string {
 if(typeof traceId!=='string'||!(/^[0-9a-f]{32}$/).test(traceId)||BigInt('0x'+traceId)===0n)throw Error('Invalid W3C trace ID');
 if(spanId!==undefined&&(typeof spanId!=='string'||!(/^[0-9a-f]{16}$/).test(spanId)||BigInt('0x'+spanId)===0n))throw Error('Invalid W3C span ID');
 const bytes=createHash('sha256').update('afterspan-otel-v1\0'+traceId+(spanId===undefined?'':'\0'+spanId)).digest().subarray(0,16);
 bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
 const hex=bytes.toString('hex');return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}
function validate(c:TraceContext):TraceContext {
 mappedId(c.traceId,c.spanId);
 if(c.trafficKind!==undefined&&!['user','synthetic','internal','unknown'].includes(c.trafficKind))throw Error('Invalid traffic kind');
 if(!uuid4.test(c.attemptId)||!Number.isInteger(c.attemptNumber)||c.attemptNumber<1||c.attemptNumber>10000||typeof c.workflow!=='string'||!identifier.test(c.workflow)||!['production','staging','development','test','local'].includes(c.environment)||!Number.isSafeInteger(c.requestStartedAtMs)||c.requestStartedAtMs<0||c.requestStartedAtMs>4102444800000||typeof c.sampled!=='boolean'||c.releaseSha!==undefined&&!/^[a-f0-9]{40}$/.test(c.releaseSha))throw Error('Invalid workflow context');
 return Object.freeze({...c});
}
export function retryContext(c:TraceContext):TraceContext {return validate({...c,attemptId:randomUUID(),attemptNumber:c.attemptNumber+1,spanId:undefined})}
export function inject(context:TraceContext|undefined=current.getStore()):Record<string,string> {
 if(!context?.spanId)return {};
 const c=validate(context);const values:Record<string,string>={workflow:c.workflow,environment:c.environment,attempt:c.attemptId,number:String(c.attemptNumber),clock:String(c.requestStartedAtMs),traffic:c.trafficKind||'unknown'};
 if(c.releaseSha)values.release=c.releaseSha;
 return {traceparent:`00-${c.traceId}-${c.spanId}-${c.sampled?'01':'00'}`,baggage:Object.entries(values).map(([k,v])=>'afterspan.'+k+'='+encodeURIComponent(v)).join(',')};
}
export function extract(headers:Record<string,string>,trusted=false):TraceContext|undefined {
 if(!trusted)return;
 try {
  const h=Object.fromEntries(Object.entries(headers).map(([k,v])=>[k.toLowerCase(),v]));const parent=h.traceparent||'',baggage=h.baggage||'';
  if(baggage.length>1024||!/^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$/.test(parent))return;
  const values:Record<string,string>={};
  for(const item of baggage.split(',')){const i=item.indexOf('=');if(i<0)return;const key=item.slice(0,i).trim();if(key.startsWith('afterspan.')){const name=key.slice(10);if(Object.hasOwn(values,name))return;values[name]=decodeURIComponent(item.slice(i+1))}}
  if(!/^\d+$/.test(values.number)||!/^\d+$/.test(values.clock))return;
  return validate({traceId:parent.slice(3,35),spanId:parent.slice(36,52),attemptId:values.attempt,attemptNumber:Number(values.number),workflow:values.workflow,environment:values.environment as Environment,requestStartedAtMs:Number(values.clock),releaseSha:values.release,sampled:(parseInt(parent.slice(-2),16)&1)===1,trafficKind:(values.traffic||'unknown') as TrafficKind});
 } catch {return}
}
export class Span {
 readonly checks:Check[]=[];closed=false;
 readonly started=performance.now();private model?:ModelObservation;
 readonly tracer:WorkflowTracer;readonly context:TraceContext;readonly parent:string|undefined;readonly step:Step;
 constructor(tracer:WorkflowTracer,context:TraceContext,parent:string|undefined,step:Step){this.tracer=tracer;this.context=context;this.parent=parent;this.step=step}
 modelUsage(fields:Partial<Omit<ModelUsage,'first_output_ms'>>){
  if(this.closed||this.step!=='model')throw Error('Model metadata requires an open model span');
  const observation=this.model||new ModelObservation({provider:fields.provider!,model:fields.model!,template_version:fields.template_version},{startedAtMs:this.started});
  observation.record(fields);this.model=observation;
 }
 firstOutput(){if(this.closed||!this.model)throw Error('Declare model identity on an open model span first');return this.model.firstOutput()}
 observeOpenAI(chunk:unknown,exactCostUsd?:string){if(this.closed||!this.model)throw Error('Declare model identity on an open model span first');return this.model.observeOpenAI(chunk,exactCostUsd)}
 observeResponses(event:unknown){if(this.closed||!this.model)throw Error('Declare model identity on an open model span first');return this.model.observeResponses(event)}
 observeAnthropic(event:unknown){if(this.closed||!this.model)throw Error('Declare model identity on an open model span first');return this.model.observeAnthropic(event)}
 modelResult(duration:number){
  if(!this.model)return;
  const result=this.model.close();this.tracer.modelMetadataErrors+=this.model.rejectedUpdates;
  if(duration>86400000)delete result.first_output_ms;
  return result;
 }
 facts(){const c=this.context;return {schema_version:'workflow-trace-v1',trace_id:mappedId(c.traceId),attempt_id:c.attemptId,attempt_number:c.attemptNumber,span_id:mappedId(c.traceId,c.spanId),parent_span_id:this.parent,workflow:c.workflow,environment:c.environment,traffic_kind:c.trafficKind||'unknown',release_sha:c.releaseSha,request_started_at_ms:c.requestStartedAtMs,step:this.step}}
 check(name:string,result:Result,options:{version?:number;method?:Method;phase?:Phase;required?:boolean;evidenceDigest?:string;execution?:CheckExecution;reasonCode?:CheckReason}={}):Result {
  const {version=1,method='source',phase='before_delivery',required=true,evidenceDigest,execution,reasonCode}=options;
  if(this.closed||typeof name!=='string'||!identifier.test(name)||!['passed','failed','unknown'].includes(result)||!Number.isInteger(version)||version<1||version>1000000||!['source','code','model','human'].includes(method)||!['before_delivery','after_delivery'].includes(phase)||typeof required!=='boolean'||this.checks.length>=20||this.checks.some(c=>c.name===name))throw Error('Invalid or closed check');
  if(execution!==undefined&&!['completed','not_run'].includes(execution)||reasonCode!==undefined&&!['evidence_absent','evidence_binding_rejected'].includes(reasonCode)||reasonCode!==undefined&&execution!=='not_run'||execution==='not_run'&&(result!=='unknown'||evidenceDigest!==undefined))throw Error('Invalid check execution diagnostic');
  const check:Check={name,version,result,method,phase,required,...(execution===undefined?{}:{execution}),...(reasonCode===undefined?{}:{reason_code:reasonCode})};
  if(evidenceDigest!==undefined){
   if(typeof evidenceDigest!=='string'||!/^[a-f0-9]{64}$/.test(evidenceDigest))throw Error('Provide a SHA-256 evidence digest, never content');
   const receipt=randomUUID(),facts=this.facts();
   this.tracer.emit('evidence.observed',{afterspan_evidence:{schema_version:'workflow-evidence-v1',receipt_id:receipt,trace_id:facts.trace_id,attempt_id:facts.attempt_id,span_id:facts.span_id,workflow:facts.workflow,environment:facts.environment,release_sha:facts.release_sha,name,version,result,method,phase,evidence_digest:evidenceDigest}});
   check.evidence_ref=receipt;
  }
  this.checks.push(check);return result;
 }
}
export class WorkflowTracer {
 private pending=new Set<Promise<void>>();private emitFailures=0;private sampledOutEvents=0;
 modelMetadataErrors=0;
 readonly client:ReturnType<typeof createAnalytics>;private readonly subjectId?:string;
 constructor(client:ReturnType<typeof createAnalytics>,options:{subjectId?:string}={}){
  if(options.subjectId!==undefined&&(typeof options.subjectId!=='string'||options.subjectId.trim()!==options.subjectId||!/^[a-z][a-z0-9_]{0,31}_[a-f0-9]{64}$/.test(options.subjectId)))throw Error('Subject must be an opaque project-scoped digest');
  this.client=client;this.subjectId=options.subjectId;
 }
 emit(name:string,properties:Record<string,unknown>):void {
  if(current.getStore()?.sampled===false){this.sampledOutEvents++;return}
  try{const job=this.client.track(name,this.subjectId??null,properties).catch(()=>{this.emitFailures++});this.pending.add(job);void job.finally(()=>this.pending.delete(job))}catch{this.emitFailures++}
 }
 async task<T>(options:TaskOptions,run:(span:Span)=>Promise<T>|T):Promise<T>{
  const context=validate(options.context||{traceId:randomBytes(16).toString('hex'),attemptId:randomUUID(),attemptNumber:1,workflow:options.workflow||'',environment:options.environment||'production',requestStartedAtMs:Date.now(),releaseSha:options.releaseSha,sampled:true,trafficKind:options.trafficKind||'unknown'});
  return current.run(context,()=>this.span('processing',run));
 }
 private openSpan(step:Step,options:SpanOptions,context=current.getStore()):Span {
  if(!context)throw Error('Start or continue a task first');
  if(!steps.has(step)||options.milestone!==undefined&&!['completion','delivery','usefulness'].includes(options.milestone)||options.disposition!==undefined&&(!['delivered','withheld','clarification','partial'].includes(options.disposition)||step!=='response'))throw Error('Invalid span declaration');
  const child=validate({...context,spanId:randomBytes(8).toString('hex')}),span=new Span(this,child,context.spanId?mappedId(context.traceId,context.spanId):undefined,step);
  current.run(child,()=>this.emit('trace.observed',{afterspan_trace:{...span.facts(),state:'started'}}));return span;
 }
 private finishSpan(span:Span,options:SpanOptions,state:string,code:string):void {
  const duration=performance.now()-span.started,usage=span.modelResult(duration);
  current.run(span.context,()=>this.emit('trace.observed',{afterspan_trace:{...span.facts(),state:duration<=86400000?(state==='cancelled'?'failed':state):'unknown',outcome_code:duration<=86400000?code:'uncertain',duration_ms:duration<=86400000?duration:undefined,...(options.milestone?{milestone:options.milestone}:{}),...(options.disposition?{disposition:options.disposition}:{}),...(span.checks.length?{checks:span.checks}:{}),...(usage?{model_usage:usage}:{})}}));span.closed=true;
 }
 async span<T>(step:Step,run:(span:Span)=>Promise<T>|T,options:SpanOptions={}):Promise<T>{
  const span=this.openSpan(step,options);
  return current.run(span.context,async()=>{
   let state='succeeded',code='none';
   try{return await run(span)}catch(error){({state,code}=failure(error));throw error}finally{this.finishSpan(span,options,state,code)}
  });
 }
 stream<T>(step:Step,source:(span:Span)=>AsyncIterable<T>,options:SpanOptions={}):AsyncIterable<T> {
  const context=current.getStore();if(!context)throw Error('Start or continue a task first');
  const tracer=this;let cancelRequested=false;
  const stream=(async function*(){
   const span=tracer.openSpan(step,options,context);let iterator:AsyncIterator<T>|undefined,state='cancelled',code='cancelled';
   try {
    iterator=current.run(span.context,()=>source(span)[Symbol.asyncIterator]());
    while(true){
     const value=await current.run(span.context,()=>iterator!.next());
     if(value.done){state=cancelRequested?'cancelled':'succeeded';code=cancelRequested?'cancelled':'none';return}
     yield value.value;
    }
   }catch(error){({state,code}=failure(cancelRequested?new StreamCancelled():error));throw error}finally{
    try {if(state!=='succeeded'&&iterator?.return)await current.run(span.context,()=>iterator!.return!())}
    catch(error){const alreadyFailed=state==='failed';state='failed';code='internal';if(!alreadyFailed)throw error}
    finally{tracer.finishSpan(span,options,state,code)}
   }
  })();
  const iterator:AsyncIterableIterator<T>={
   [Symbol.asyncIterator](){return this},next:()=>stream.next(),
   return(value){cancelRequested=true;return stream.return(value)},throw:error=>stream.throw(error)
  };
  return iterator;
 }
 async response(options:TaskOptions,handler:(span:Span)=>Response|Promise<Response>):Promise<{response:Response;finished:Promise<'completed'|'failed'|'cancelled'>}> {
  let ready!:(response:Response)=>void,unavailable!:(error:unknown)=>void;
  const responseReady=new Promise<Response>((resolve,reject)=>{ready=resolve;unavailable=reject});
  const finished=this.task(options,async root=>{
   const original=await handler(root);
   await this.span('response',async()=>{
    if(!original.body){ready(original);if(original.status===0||original.status>=400)throw new ResponseFailed(original.status);return}
    const reader=original.body.getReader();let settle!:(failed:boolean,error?:unknown)=>void,settled=false,cancelling=false;
    const consumed=new Promise<void>((resolve,reject)=>{settle=(failed,error)=>{if(settled)return;settled=true;reader.releaseLock();if(failed)reject(error);else resolve()}});
    const body=new ReadableStream<Uint8Array>({
     async pull(controller){
      try{const value=await reader.read();if(settled||cancelling)return;if(value.done){controller.close();settle(false)}else{if(!isUint8Array(value.value))throw new TypeError('Response body must yield Uint8Array');controller.enqueue(value.value)}}
      catch(error){if(!settled&&!cancelling){controller.error(error);void reader.cancel(error).catch(()=>{});settle(true,error)}}
     },
     async cancel(reason){
      cancelling=true;
      try{await reader.cancel(reason)}catch(error){settle(true,error);throw error}finally{settle(true,new StreamCancelled())}
     }
    },{highWaterMark:0});
    ready(new Response(body,{status:original.status,statusText:original.statusText,headers:original.headers}));
    await consumed;if(original.status===0||original.status>=400)throw new ResponseFailed(original.status);
   });
  }).then(()=> 'completed' as const,error=>{unavailable(error);return error instanceof StreamCancelled?'cancelled' as const:'failed' as const});
  return {response:await responseReady,finished};
 }
 async flush(){await Promise.all([...this.pending]);try{await this.client.flush()}catch{this.emitFailures++}}
 status(){return {emit_failures:this.emitFailures,sampled_out_events:this.sampledOutEvents,model_metadata_errors:this.modelMetadataErrors,...this.client.status()}}
}


export function redactDebugContent(value:unknown):unknown {
 if(Buffer.byteLength(JSON.stringify(value),'utf8')>16000)throw Error('Debug content exceeds 16 KB');
 const privateKey=/password|passwd|secret|authorization|cookie|credential|session|otp|api.?key|email|phone|mobile|patient|address|(?:^|_)(?:key|token)(?:$|_)/i;
 const secret=/\b(?:Bearer\s+\S+|(?:na_write_|na_admin_|as_read_|as_workspace_|as_session_|as_device_|as_login_|as_invite_|ghp_|github_pat_|xoxb-|sk-)[A-Za-z0-9_-]+)/gi;
 const pii=/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|(?<!\w)\+?\d(?:[\s().-]*\d){9,14}(?!\w)/gi;
 function clean(item:unknown,depth=0):unknown {
  if(depth>5)throw Error('Debug content exceeds five levels');
  if(Array.isArray(item)){if(item.length>100)throw Error('Debug list too long');return item.map(v=>clean(v,depth+1))}
  if(item&&typeof item==='object'){
   if(![Object.prototype,null].includes(Object.getPrototypeOf(item)))throw Error('Debug content must be plain JSON');
   const entries=Object.entries(item);if(entries.length>64)throw Error('Too many debug fields');
   return Object.fromEntries(entries.flatMap(([key,v])=>{if(!/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(key))throw Error('Unsupported debug field');return privateKey.test(key)?[]:[[key,clean(v,depth+1)]]}));
  }
  if(typeof item==='string'){if(item.length>4000)throw Error('Debug text too long');return item.replace(secret,'[redacted]').replace(pii,'[redacted]')}
  if(item===null||typeof item==='boolean'||typeof item==='number'&&Number.isFinite(item))return item;
  throw Error('Debug content must be finite JSON');
 }
 return clean(value);
}
export function prepareDebugSnapshot(span:Span,content:Record<string,unknown>,policyRevision:number,options:{kind?:'input'|'output'|'context';privacyReviewed?:boolean}={}){
 const {kind='context',privacyReviewed=false}=options;
 if(privacyReviewed!==true||!Number.isInteger(policyRevision)||policyRevision<1||policyRevision>1000||!['input','output','context'].includes(kind)||!content||Array.isArray(content)||typeof content!=='object')throw Error('Review selected content and provide an explicit policy revision');
 const f=span.facts();return {id:randomUUID(),policy_revision:policyRevision,privacy_reviewed:true,kind,trace_id:f.trace_id,attempt_id:f.attempt_id,span_id:f.span_id,workflow:f.workflow,environment:f.environment,content:redactDebugContent(content)};
}
