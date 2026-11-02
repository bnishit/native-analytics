/** Model metadata only. No provider transport, content retention or business retries. */
export type ModelUsage={provider:string;model:string;template_version?:string;unit?:'token';input_count?:number;output_count?:number;cache_read_count?:number;reasoning_count?:number;usage_source?:'provider_reported'|'estimated';cost_microusd?:number;cost_usd_decimal?:string;cost_source?:'provider_reported'|'price_table';price_version?:string;first_output_ms?:number};
type Metadata=Omit<ModelUsage,'first_output_ms'>;
const counts=['input_count','output_count','cache_read_count','reasoning_count'] as const;
const accounting=[...counts,'usage_source','cost_microusd','cost_usd_decimal','cost_source','price_version'];
const fields=new Set(['provider','model','template_version','unit',...accounting]);
function validated(value:Record<string,unknown>):Metadata {
 if(Object.keys(value).some(k=>!fields.has(k)))throw Error('Unsupported model metadata field');
 const d=Object.fromEntries(Object.entries(value).filter(([,v])=>v!==undefined));
 for(const [name,pattern] of [['provider',/^[a-z][a-z0-9_.-]{0,63}$/],['model',/^[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}$/],['template_version',/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/]] as const){
  if(name==='template_version'&&d[name]===undefined)continue;
  if(typeof d[name]!=='string'||!pattern.test(d[name]))throw Error('Invalid model identity');
 }
 if(d.unit!==undefined&&d.unit!=='token')throw Error('Unsupported model unit');d.unit='token';
 for(const key of counts)if(d[key]!==undefined&&(!Number.isSafeInteger(d[key])||Number(d[key])<0||Number(d[key])>1e9))throw Error('Invalid model usage count');
 const usage=counts.some(k=>d[k]!==undefined);
 if(usage!==['provider_reported','estimated'].includes(d.usage_source as string)||!usage&&d.usage_source!==undefined)throw Error('Usage requires its source');
 if(d.cost_microusd!==undefined&&(!Number.isSafeInteger(d.cost_microusd)||Number(d.cost_microusd)<0||Number(d.cost_microusd)>1e12))throw Error('Invalid model cost');
 if(d.cost_usd_decimal!==undefined){
  if(typeof d.cost_usd_decimal!=='string'||!/^(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,12})?$/.test(d.cost_usd_decimal))throw Error('Provide an exact bounded USD string');
  const [whole,fraction='']=d.cost_usd_decimal.split('.'),trimmed=fraction.replace(/0+$/,'');
  if(whole.length===7&&(whole!=='1000000'||trimmed))throw Error('Model cost exceeds bound');
  d.cost_usd_decimal=whole+(trimmed?'.'+trimmed:'');
 }
 const cost=d.cost_microusd!==undefined||d.cost_usd_decimal!==undefined;
 if(d.cost_microusd!==undefined&&d.cost_usd_decimal!==undefined||cost!==['provider_reported','price_table'].includes(d.cost_source as string)||!cost&&d.cost_source!==undefined)throw Error('Cost requires one amount and its source');
 if((d.cost_source==='price_table')!==(d.price_version!==undefined)||d.price_version!==undefined&&(typeof d.price_version!=='string'||!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(d.price_version)))throw Error('Price-table cost requires a version');
 if(d.cache_read_count!==undefined&&d.input_count!==undefined&&Number(d.cache_read_count)>Number(d.input_count)||d.reasoning_count!==undefined&&d.output_count!==undefined&&Number(d.reasoning_count)>Number(d.output_count))throw Error('Detail count exceeds total');
 return d as Metadata;
}
function field(value:unknown,key:string):unknown{return value&&typeof value==='object'?(value as Record<string,unknown>)[key]:undefined}
export class ModelObservation {
 private data:Metadata;private first?:number;private closed=false;
 readonly clock:()=>number;readonly started:number;rejectedUpdates=0;
 constructor(identity:Pick<Metadata,'provider'|'model'|'template_version'>,options:{clock?:()=>number;startedAtMs?:number}={}){
  this.clock=options.clock||(()=>performance.now());this.started=options.startedAtMs??this.clock();
  if(!Number.isFinite(this.started)||this.started<0)throw Error('Invalid model clock');
  this.data=validated(identity);
 }
 record(update:Partial<Metadata>){if(this.closed)throw Error('Model observation is closed');this.data=validated({...this.data,...update})}
 firstOutput(){
  if(this.closed)throw Error('Model observation is closed');
  if(this.first===undefined){const elapsed=this.clock()-this.started;if(!Number.isFinite(elapsed)||elapsed<0||elapsed>86400000)throw Error('Invalid model output clock');this.first=elapsed}
  return this.first;
 }
 observeOpenAI(chunk:unknown,exactCostUsd?:string):boolean {
  if(this.closed)throw Error('Model observation is closed');
  try {
   const choices=field(chunk,'choices');
   if(choices!==undefined&&!Array.isArray(choices))throw Error('Invalid model choices');
   for(const choice of (choices||[]) as unknown[]){
    const delta=field(choice,'delta'),tools=field(delta,'tool_calls');
    const content=['content','refusal'].some(k=>typeof field(delta,k)==='string'&&Boolean(field(delta,k)));
    const tool=Array.isArray(tools)&&tools.some(t=>['name','arguments'].some(k=>typeof field(field(t,'function'),k)==='string'&&Boolean(field(field(t,'function'),k))));
    if(content||tool)this.firstOutput();
   }
   const update:Partial<Metadata>={};
   if(field(chunk,'model')!==undefined&&field(chunk,'model')!==null)update.model=field(chunk,'model') as string;
   const usage=field(chunk,'usage');
   if(usage!==undefined&&usage!==null){
    const values={input_count:field(usage,'prompt_tokens'),output_count:field(usage,'completion_tokens'),cache_read_count:field(field(usage,'prompt_tokens_details'),'cached_tokens'),reasoning_count:field(field(usage,'completion_tokens_details'),'reasoning_tokens')};
    Object.assign(update,Object.fromEntries(Object.entries(values).map(([k,v])=>[k,v??undefined])),{usage_source:Object.values(values).some(v=>v!==undefined&&v!==null)?'provider_reported':undefined});
    const cost=exactCostUsd??field(usage,'cost');Object.assign(update,{cost_usd_decimal:typeof cost==='string'?cost:undefined,cost_source:typeof cost==='string'?'provider_reported':undefined});
   }
   this.record(update);return true;
  } catch {
   this.rejectedUpdates++;this.data=Object.fromEntries(Object.entries(this.data).filter(([k])=>!accounting.includes(k))) as Metadata;return false;
  }
 }
 snapshot():ModelUsage{return {...this.data,...(this.first!==undefined?{first_output_ms:this.first}:{})}}
 close():ModelUsage{this.closed=true;return this.snapshot()}
}
