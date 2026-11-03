import { beforeAll, afterAll, expect, it, vi } from 'vitest';
import { readFileSync, writeFileSync } from 'node:fs';
import { guardedFetch, remoteResponse, digest } from './evaluation-transport.mjs';
const modelMetadata: Array<Record<string, unknown>> = vi.hoisted(() => []);
vi.mock('next/server', () => ({ after: () => {} }));
vi.mock('next/server.js', () => ({ after: () => {} }));
vi.mock('@/lib/analytics/native', () => ({
  trackSlackToolExecution: () => {},
  trackLlmGeneration: (value: Record<string, unknown>) => {
    modelMetadata.push(Object.fromEntries(['provider','model','latencyMs','httpStatus','inputTokens','outputTokens','cacheReadTokens','reasoningTokens','providerCostUsd','templateVersion','isError'].filter(key => value[key] !== undefined).map(key => [key,value[key]])));
    return Promise.resolve();
  },
}));
const plan = JSON.parse(readFileSync(process.env.PROVIDER_EVALUATION_PLAN!, 'utf8'));
const results: unknown[] = [];
beforeAll(() => {
  vi.stubEnv('GROWTHBOOK_API_URL', 'https://catalog.synthetic.invalid');
  vi.stubEnv('GROWTHBOOK_API_KEY', 'synthetic-source-placeholder');
  vi.stubEnv('OPENROUTER_API_KEY', 'synthetic-provider-placeholder');
  vi.stubEnv('SLACK_AGENTIC_MODEL', 'openai/gpt-4.1-mini');
});
afterAll(() => {
  writeFileSync(process.env.PROVIDER_EVALUATION_RESULT!, JSON.stringify({sha:plan.source_sha,variant:plan.variant,case:plan.case,results}),{mode:0o600});
  vi.unstubAllGlobals();vi.unstubAllEnvs();
});
it('runs one selected read-only application case', async () => {
  const fixtures = {
    'bounded-five-enabled': {message:'List up to five enabled experiments in production, with the scope of what you checked',alternating:true},
    'total-enabled': {message:'How many enabled experiments are there in production',alternating:true},
    'full-list-capped': {message:'List all experiments in production',alternating:false},
  };
  const fixture = fixtures[plan.case as keyof typeof fixtures];
  expect(fixture).toBeDefined();
  const raw = Array.from({length:20},(_,index)=>({id:`synthetic_flag_${String(index).padStart(2,'0')}`,archived:false,dateUpdated:'2026-01-01T00:00:00Z',dateCreated:'2025-01-01T00:00:00Z',description:'',owner:'',project:'',valueType:'boolean',defaultValue:false,tags:[],environments:{production:{enabled:!fixture.alternating||index%2===0,defaultValue:false,rules:[]}}}));
  const reads: unknown[] = [], forbidden: string[] = [];let calls=0;
  vi.stubGlobal('fetch',guardedFetch(raw,reads,forbidden,async request=>{
    calls++;if(calls>3)throw Error('Provider call budget exceeded');
    if(plan.mode==='live')return remoteResponse(request,plan,plan.directory,calls);
    const step=calls===1?{kind:'read',call:{name:'experiments.list',input:{environment:'production'}}}:{kind:'answer',text:'Synthetic offline model fixture'};
    return new Response(JSON.stringify({choices:[{message:{content:JSON.stringify({step})}}],usage:{prompt_tokens:12,completion_tokens:8,cost:0.000001}}));
  }));
  if(plan.negative_control==='caught_request_post'){
    try{await fetch(new Request('https://catalog.synthetic.invalid/api/v1/features',{method:'POST'}));}catch{}
  }
  const {runSlackAgentTurn}=await import('@/lib/slack/orchestration/live-agent');
  const quality=await import('@/lib/slack/orchestration/answer-quality');
  const started=performance.now();
  try{
    const answer=await runSlackAgentTurn({message:fixture.message,command:{kind:'list_experiments',environment:'production',confidence:'high',clarificationNeeded:null},featureIds:raw.map(row=>row.id),environment:'production',traceId:'synthetic-provider-evaluation',userId:'USYNTHETIC'});
    if(!answer.catalogEvidence)throw Error('No catalog evidence');
    const checked=await quality.checkCatalogAnswer(answer.catalogEvidence),claims=answer.catalogEvidence.claims;
    if(reads.length<2||calls<1)throw Error('Provider or independent source read absent');
    results.push({output:{shown:claims.length,enabled:claims.filter(c=>c.enabled).length,disabled:claims.filter(c=>!c.enabled).length,total:answer.text.match(/— (\d+) total\b/)?Number(answer.text.match(/— (\d+) total\b/)![1]):null,partial_label:/not the complete experiment list|Partial (?:list|result)/.test(answer.text),check_result:checked.reported_outcome,unknown_claims:checked.unknown_claims},request_sha256:digest(fixture.message),source_fixture_sha256:digest(JSON.stringify(raw)),duration_ms:performance.now()-started,model_calls:calls,model_metadata:modelMetadata,source_reads:reads});
  }catch{
    results.push({error_code:'internal',request_sha256:digest(fixture.message),source_fixture_sha256:digest(JSON.stringify(raw)),duration_ms:performance.now()-started,model_calls:calls,model_metadata:modelMetadata,source_reads:reads});
  }
  expect(forbidden).toEqual([]);
},120000);
