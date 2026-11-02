/** Server-side, runtime-neutral Native Analytics client. Never ship write keys to browsers. */
export type Properties = Record<string, unknown>;
export interface JourneyContext {
  id: string;
  workflow: string;
  environment: 'production' | 'staging' | 'development' | 'test' | 'local';
  attempt_id: string;
  attempt_number: number;
  state: 'started' | 'succeeded' | 'failed' | 'cancelled';
  subject_kind?: 'person' | 'account' | 'workspace' | 'installation' | 'service' | 'anonymous' | 'unknown';
  release?: string | null;
}
function validJourney(j: JourneyContext): boolean {
  const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,159}$/;
  const allowed = ['id','workflow','environment','attempt_id','attempt_number','state','subject_kind','release'];
  return Boolean(j && typeof j === 'object' && Object.keys(j).every(k => allowed.includes(k)) &&
    typeof j.id === 'string' && identifier.test(j.id) && typeof j.attempt_id === 'string' && identifier.test(j.attempt_id) &&
    typeof j.workflow === 'string' && /^[a-z][a-z0-9_.-]{0,79}$/.test(j.workflow) &&
    ['production','staging','development','test','local'].includes(j.environment) &&
    Number.isInteger(j.attempt_number) && j.attempt_number >= 1 && j.attempt_number <= 10000 &&
    ['started','succeeded','failed','cancelled'].includes(j.state) &&
    (j.subject_kind === undefined || ['person','account','workspace','installation','service','anonymous','unknown'].includes(j.subject_kind)) &&
    (j.release == null || typeof j.release === 'string' && identifier.test(j.release)));
}
export interface AnalyticsConfig {
  url?: string;
  writeKey?: string;
  source?: string;
  fetch?: typeof fetch;
  health?: { source: string; environment: 'production'|'staging'|'development'|'test'|'local'; workflow?: string; intervalSeconds?: number };
  onError?: (error: { code: string; queued: number }) => void;
}
const forbidden = /password|passwd|secret|authorization|cookie|otp|api.?key|(?:^|_)(?:token|prompt|messages?|input|output|content|body|email|phone|mobile|text)(?:$|_)|Preview$|request.?url|phone|mobile|email|message|prompt|content|(?:reason|error)$/i;
const numericMetric = /^(?:\$?ai[_.$])?(?:input|output|total|cached|reasoning|prompt|completion)[_A-Z]?(?:tokens|Tokens)$|^(?:input|output|total|prompt|completion)Tokens$|^(?:input|output|total|cached|reasoning)_count$/;
export function safeProperties(value: unknown, depth = 0): unknown {
  if (depth > 5) return '[omitted]';
  if (Array.isArray(value)) return value.slice(0, 100).map(v => safeProperties(v, depth + 1));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).slice(0, 64).map(([raw, v]) => {
    const key = raw.replace(/^\$/, '').replace(/[^A-Za-z0-9_]/g, '_').slice(0, 100);
    const metric = typeof v === 'number' && Number.isFinite(v) && numericMetric.test(raw);
    return [key, forbidden.test(raw) && !metric ? '[redacted]' : safeProperties(v, depth + 1)];
  }));
  if (typeof value === 'bigint') return String(value);
  if (typeof value === 'string') return value.slice(0, 2000).replace(/\bBearer\s+\S+|\b(?:na_write_|na_admin_|as_read_|as_workspace_|as_session_|as_login_|as_invite_|mbb_inv_|sk-)[A-Za-z0-9_-]+/gi, '[redacted]');
  return typeof value === 'number' && !Number.isFinite(value) ? null : value;
}
export function createAnalytics(config: AnalyticsConfig = {}) {
  config = { ...config, url: config.url?.trim(), writeKey: config.writeKey?.trim(), source: config.source?.trim() };
  const queue: Array<{id:string;timestamp:string;[key:string]:unknown}> = [];
  let dropped=0,rejected=0,deliveryFailures=0,evaluatorFailures:number|null=null,lastDelivery:number|null=null,lastError:string|null=null;
  const instanceId=crypto.randomUUID(),processStarted=Date.now();let pulseSequence=0;
  if(config.health&&(!/^[a-z][a-z0-9_.-]{0,63}$/.test(config.health.source)||!['production','staging','development','test','local'].includes(config.health.environment)||config.health.workflow!=null&&!/^[a-z][a-z0-9_.-]{0,63}$/.test(config.health.workflow)||!Number.isInteger(config.health.intervalSeconds??60)||(config.health.intervalSeconds??60)<10||(config.health.intervalSeconds??60)>3600))throw new Error('invalid collection health configuration');
  function report(code:string){
    lastError=code;if(code==='delivery_failed')deliveryFailures++;if(code==='queue_full')dropped++;if(code.startsWith('invalid_'))rejected++;
    try{config.onError?.({code,queued:queue.length})}catch{}
  }
  let running: Promise<void> | undefined;
  const enabled = Boolean(config.url && config.writeKey);
  if (config.url && !/^https:\/\//.test(config.url) && !/^http:\/\/127\.0\.0\.1:\d+/.test(config.url)) throw new Error('Native Analytics requires HTTPS');
  async function flush(): Promise<void> {
    if (!enabled) return;
    if (running) { await running; if (queue.length) return flush(); return; }
    running = (async () => {
      while (queue.length) {
        const batch = queue.slice(0, 50);
        let delivered = false;
        for (let attempt = 0; attempt < 2; attempt++) {
          try {
            const response = await (config.fetch ?? fetch)(config.url!.replace(/\/$/, '') + '/v1/events', {
              method: 'POST', redirect: 'error', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + config.writeKey },
              body: JSON.stringify({ events: batch }), signal: AbortSignal.timeout(2500),
            });
            if (!response.ok) throw new Error('http_' + response.status);
            const receipt = await response.json() as { accepted: number; duplicates: number };
            if (receipt.accepted + receipt.duplicates !== batch.length) throw new Error('invalid_receipt');
            delivered = true; break;
          } catch {
            if (attempt === 1) report('delivery_failed');
          }
        }
        if (!delivered) break;
        queue.splice(0, batch.length);lastDelivery=Date.now()/1000;lastError=null;
      }
    })();
    try { await running; } finally { running = undefined; }
  }
  function track(name: string, distinctId?: string | null, properties: Properties = {}, attributes: Properties = {}, journey?: JourneyContext): Promise<void> {
    if (!enabled) return Promise.resolve();
    if (queue.length >= 500) { report('queue_full'); return Promise.resolve(); }
    if (journey && (!validJourney(journey) || journey.subject_kind === 'anonymous' && distinctId)) { report('invalid_journey'); return Promise.resolve(); }
    const normalized = name.replace(/^\$ai_generation$/, 'ai.generation').replace(/^[^A-Za-z0-9]+/, '').replace(/[^A-Za-z0-9_.:@/-]/g, '_').slice(0, 160);
    const identity = distinctId?.replace(/[^A-Za-z0-9_.:@/-]/g, '_').slice(0, 160) || null;
    const contract = normalized === 'trace.observed' && Object.keys(properties).length === 1 && 'afterspan_trace' in properties || normalized === 'evidence.observed' && Object.keys(properties).length === 1 && 'afterspan_evidence' in properties || normalized === 'analytics.pipeline' && Object.keys(properties).length === 1 && 'afterspan_health' in properties;
    const clean = safeProperties(contract ? properties : { ...properties, source: config.source ?? properties.source }) as Properties;
    const traits = safeProperties(attributes) as Properties;
    if (!normalized || JSON.stringify(clean).length > 15000 || JSON.stringify(traits).length > 15000) {
      report('invalid_event'); return Promise.resolve();
    }
    queue.push({ id: crypto.randomUUID(), name: normalized, distinct_id: identity, timestamp: normalized==='analytics.pipeline'&&typeof (clean.afterspan_health as Properties)?.observed_at_ms==='number'?new Date((clean.afterspan_health as Properties).observed_at_ms as number).toISOString():new Date().toISOString(), properties: clean, attributes: traits, ...(journey ? {journey: {...journey}} : {}) });
    return flush();
  }
  async function erase(id: string): Promise<void> {
    if (!enabled) return;
    const response = await (config.fetch ?? fetch)(config.url!.replace(/\/$/, '') + '/v1/erase', {method:'POST', redirect:'error', headers:{'Content-Type':'application/json',Authorization:'Bearer '+config.writeKey},body:JSON.stringify({distinct_id:id}),signal:AbortSignal.timeout(2500)});
    if (!response.ok) throw new Error('analytics_erasure_failed');
    const receipt = await response.json() as {ok?:boolean;stale_identity_blocked?:boolean};
    if (receipt.ok !== true || receipt.stale_identity_blocked !== true) throw new Error('analytics_erasure_unconfirmed');
  }
  function recordEvaluation(failed=false):void{if(typeof failed!=='boolean')throw new Error('evaluation failure must be a boolean');evaluatorFailures=(evaluatorFailures??0)+(failed?1:0)}
  function pulse():Promise<void>{
    if(!config.health)return Promise.resolve();
    return track('analytics.pipeline',null,{afterspan_health:{schema_version:'collection-health-v1',source:config.health.source,environment:config.health.environment,workflow:config.health.workflow??null,
      instance_id:instanceId,sequence:++pulseSequence,observed_at_ms:Date.now(),process_started_at_ms:processStarted,interval_seconds:config.health.intervalSeconds??60,
      delivery_mode:'memory',queued:queue.length,capacity:500,oldest_queued_at_ms:queue.length?Date.parse(queue[0].timestamp):null,last_delivery_at_ms:lastDelivery==null?null:Math.floor(lastDelivery*1000),
      dropped,rejected,delivery_failures:deliveryFailures,evaluator_failures:evaluatorFailures,error_code:lastError==='delivery_failed'?'delivery_failed':lastError==='queue_full'?'queue_full':lastError?'invalid_event':null}});
  }
  return { track, pulse, recordEvaluation, trackJourney: (name: string, journey: JourneyContext, id?: string | null, properties: Properties = {}, attributes: Properties = {}) => track(name, id, properties, attributes, journey), flush, erase, identify: (id: string, attributes: Properties) => track('user.identified', id, {}, attributes),
    status: () => ({ enabled, queued: queue.length,dropped,rejected,delivery_failures:deliveryFailures,evaluator_failures:evaluatorFailures,last_delivery:lastDelivery,last_error:lastError,oldest_queued_at:queue.length?Date.parse(queue[0].timestamp)/1000:null }) };
}
