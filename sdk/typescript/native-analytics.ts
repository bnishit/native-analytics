/** Server-side, runtime-neutral Native Analytics client. Never ship write keys to browsers. */
export type Properties = Record<string, unknown>;
export interface AnalyticsConfig {
  url?: string;
  writeKey?: string;
  source?: string;
  fetch?: typeof fetch;
  onError?: (error: { code: string; queued: number }) => void;
}
const forbidden = /password|passwd|secret|authorization|cookie|otp|api.?key|(?:^|_)(?:token|prompt|messages?|input|output|content|body|email|phone|mobile|text)(?:$|_)|Preview$|request.?url|phone|mobile|email|message|prompt|content|(?:reason|error)$/i;
const numericMetric = /^(?:\$?ai[_.$])?(?:input|output|total|cached|reasoning|prompt|completion)[_A-Z]?(?:tokens|Tokens)$|^(?:input|output|total|prompt|completion)Tokens$/;
export function safeProperties(value: unknown, depth = 0): unknown {
  if (depth > 5) return '[omitted]';
  if (Array.isArray(value)) return value.slice(0, 100).map(v => safeProperties(v, depth + 1));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).slice(0, 64).map(([raw, v]) => {
    const key = raw.replace(/^\$/, '').replace(/[^A-Za-z0-9_]/g, '_').slice(0, 100);
    const metric = typeof v === 'number' && Number.isFinite(v) && numericMetric.test(raw);
    return [key, forbidden.test(raw) && !metric ? '[redacted]' : safeProperties(v, depth + 1)];
  }));
  if (typeof value === 'bigint') return String(value);
  if (typeof value === 'string') return value.slice(0, 2000).replace(/\bBearer\s+\S+|\b(?:na_write_|na_admin_|mbb_inv_|sk-)[A-Za-z0-9_-]+/gi, '[redacted]');
  return typeof value === 'number' && !Number.isFinite(value) ? null : value;
}
export function createAnalytics(config: AnalyticsConfig = {}) {
  config = { ...config, url: config.url?.trim(), writeKey: config.writeKey?.trim(), source: config.source?.trim() };
  const queue: object[] = [];
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
            if (attempt === 1) config.onError?.({ code: 'delivery_failed', queued: queue.length });
          }
        }
        if (!delivered) break;
        queue.splice(0, batch.length);
      }
    })();
    try { await running; } finally { running = undefined; }
  }
  function track(name: string, distinctId?: string | null, properties: Properties = {}, attributes: Properties = {}): Promise<void> {
    if (!enabled) return Promise.resolve();
    if (queue.length >= 500) { config.onError?.({ code: 'queue_full', queued: queue.length }); return Promise.resolve(); }
    const normalized = name.replace(/^\$ai_generation$/, 'ai.generation').replace(/^[^A-Za-z0-9]+/, '').replace(/[^A-Za-z0-9_.:@/-]/g, '_').slice(0, 160);
    const identity = distinctId?.replace(/[^A-Za-z0-9_.:@/-]/g, '_').slice(0, 160) || null;
    const clean = safeProperties({ ...properties, source: config.source ?? properties.source }) as Properties;
    const traits = safeProperties(attributes) as Properties;
    if (!normalized || JSON.stringify(clean).length > 15000 || JSON.stringify(traits).length > 15000) {
      config.onError?.({ code: 'invalid_event', queued: queue.length }); return Promise.resolve();
    }
    queue.push({ id: crypto.randomUUID(), name: normalized, distinct_id: identity, timestamp: new Date().toISOString(), properties: clean, attributes: traits });
    return flush();
  }
  async function erase(id: string): Promise<void> {
    if (!enabled) return;
    const response = await (config.fetch ?? fetch)(config.url!.replace(/\/$/, '') + '/v1/erase', {method:'POST', redirect:'error', headers:{'Content-Type':'application/json',Authorization:'Bearer '+config.writeKey},body:JSON.stringify({distinct_id:id}),signal:AbortSignal.timeout(2500)});
    if (!response.ok) throw new Error('analytics_erasure_failed');
    const receipt = await response.json() as {ok?:boolean;stale_identity_blocked?:boolean};
    if (receipt.ok !== true || receipt.stale_identity_blocked !== true) throw new Error('analytics_erasure_unconfirmed');
  }
  return { track, flush, erase, identify: (id: string, attributes: Properties) => track('user.identified', id, {}, attributes),
    status: () => ({ enabled, queued: queue.length }) };
}
