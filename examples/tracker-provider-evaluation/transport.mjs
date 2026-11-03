import fs from 'node:fs';
import crypto from 'node:crypto';
import { spawn } from 'node:child_process';

export const providerUrl = 'https://openrouter.ai/api/v1/chat/completions';
export function validateRequest(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || Buffer.byteLength(JSON.stringify(value),'utf8') > 64000) throw Error('Unsupported provider request');
  const allowed = ['model', 'messages', 'max_tokens', 'temperature', 'response_format', 'provider'];
  if (Object.keys(value).some(key => !allowed.includes(key)) || value.model !== 'openai/gpt-4.1-mini' || value.temperature !== 0 || !Number.isInteger(value.max_tokens) || value.max_tokens < 1 || value.max_tokens > 2200) throw Error('Unsupported provider settings');
  if (!Array.isArray(value.messages) || !value.messages.length || value.messages.length > 24 || value.messages.some(m => !m || !['system', 'user', 'assistant'].includes(m.role) || typeof m.content !== 'string' || m.content.length > 24000 || Object.keys(m).some(k => !['role', 'content'].includes(k)))) throw Error('Unsupported synthetic messages');
  if (value.provider?.require_parameters !== true || Object.keys(value.provider).length !== 1 || value.response_format?.type !== 'json_schema' || value.response_format.json_schema?.strict !== true) throw Error('Strict structured output required');
  return value;
}
export async function executeProvider(payload, fetcher, env) {
  if (!/^[a-f0-9]{40}$/.test(payload.runtime_sha) || env.RAILWAY_GIT_COMMIT_SHA !== payload.runtime_sha || !env.OPENROUTER_API_KEY) throw Error('Runtime does not match the reviewed target');
  const request = validateRequest(payload.request);
  const response = await fetcher('https://openrouter.ai/api/v1/chat/completions', { method: 'POST', redirect: 'error', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + env.OPENROUTER_API_KEY }, body: JSON.stringify(request), signal: AbortSignal.timeout(15000) });
  if (!response.ok) { await response.body?.cancel();const error=Error('Provider request failed');error.httpStatus=response.status;throw error; }
  let text = ''; const reader = response.body.getReader();
  const decoder = new TextDecoder(); let size = 0;
  try { for (;;) { const next = await reader.read(); if (next.done) break; size += next.value.length; if (size > 262144) throw Error('Provider response too large'); text += decoder.decode(next.value, { stream: true }); } text += decoder.decode(); }
  finally { await reader.cancel().catch(() => {}); }
  JSON.parse(text);
  return { status: response.status, body: text };
}
export const remoteProgram = `const validateRequest=${validateRequest.toString()};const executeProvider=${executeProvider.toString()};(async()=>{const p=JSON.parse(Buffer.from(process.argv[1],'base64').toString('utf8'));const r=await executeProvider(p,fetch,process.env);process.stdout.write(JSON.stringify(r))})().catch(error=>{process.stdout.write(JSON.stringify({error:'bounded_provider_failed',http_status:Number.isInteger(error.httpStatus)?error.httpStatus:null}));process.exitCode=1});`;
export function shellQuote(value) { return "'" + value.replaceAll("'", "'\\''") + "'"; }
export function digest(value) { return crypto.createHash('sha256').update(value).digest('hex'); }
export async function remoteResponse(request, plan, directory, number) {
  validateRequest(request);
  if (!Number.isInteger(number) || number < 1 || number > 3) throw Error('Provider call budget exceeded');
  const intent = `${directory}/provider-${number}.json`;
  const record = { phase: 'attempted', request_sha256: digest(JSON.stringify(request)), started_at: new Date().toISOString() };
  fs.writeFileSync(intent, JSON.stringify(record), { mode: 0o600, flag: 'wx' });
  const payload = Buffer.from(JSON.stringify({ request, runtime_sha: plan.runtime_sha })).toString('base64');
  const command = 'node -e ' + shellQuote(remoteProgram) + ' ' + shellQuote(payload);
  const result = await new Promise((resolve, reject) => {
    const child = spawn('railway', ['ssh', '--project', plan.project, '--service', plan.service, '--environment', plan.environment, '--', command], { stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '', size = 0, timedOut = false;
    const timer = setTimeout(() => { timedOut = true; child.kill('SIGTERM'); }, 30000);
    child.stdout.on('data', chunk => { size += chunk.length; if (size > 524288) child.kill('SIGTERM'); else stdout += chunk; });
    child.stderr.resume();
    child.on('error', () => { clearTimeout(timer); reject(Error('Provider process unavailable; attempted call retained')); });
    child.on('close', code => {
      clearTimeout(timer);let value;try { value=JSON.parse(stdout); } catch {}
      if(code===0&&!timedOut&&size<=524288&&value){resolve(value);return;}
      const status=Number.isInteger(value?.http_status)?value.http_status:null;
      fs.writeFileSync(intent,JSON.stringify({...record,phase:status?'failed':'unknown',http_status:status}),{mode:0o600});
      reject(Error('Provider call failed or is unknown; do not retry'));
    });
  });
  if (result.status !== 200 || typeof result.body !== 'string' || result.body.length > 262144) throw Error('Invalid provider receipt');
  fs.writeFileSync(intent, JSON.stringify({ ...record, phase: 'received', response_sha256: digest(result.body), received_at: new Date().toISOString() }), { mode: 0o600 });
  return new Response(result.body, { status: result.status, headers: { 'Content-Type': 'application/json' } });
}
export function guardedFetch(rows, reads, forbidden, complete) {
  return async (input, options) => {
    let url, method;
    try { if (!(typeof input === 'string' || input instanceof URL || input instanceof Request)) throw Error(); url = new URL(input instanceof Request ? input.url : String(input)); method = String(options?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase(); }
    catch { forbidden.push('unsupported_request'); throw Error('Unsupported evaluation transport'); }
    if (url.href === providerUrl && method === 'POST' && typeof options?.body === 'string') {
      let request;try { request=validateRequest(JSON.parse(options.body)); } catch { forbidden.push('invalid_provider_request');throw Error('Unsupported provider request'); }
      return complete(request);
    }
    if (url.origin !== 'https://catalog.synthetic.invalid' || url.pathname !== '/api/v1/features' || method !== 'GET') { forbidden.push('forbidden_transport'); throw Error('Evaluation permits synthetic source reads only'); }
    const limit = Number(url.searchParams.get('limit') || 100), offset = Number(url.searchParams.get('offset') || 0);
    if (!Number.isInteger(limit) || limit < 1 || limit > 1000 || !Number.isInteger(offset) || offset < 0) { forbidden.push('invalid_page'); throw Error('Invalid source page'); }
    const features = rows.slice(offset, offset + limit), hasMore = offset + features.length < rows.length;
    reads.push({ limit, offset, returned: features.length });
    return new Response(JSON.stringify({ features, limit, offset, count: features.length, total: rows.length, hasMore, nextOffset: hasMore ? offset + features.length : null }));
  };
}
