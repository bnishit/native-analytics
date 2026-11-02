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
function usageObject(value:unknown){if(!value||typeof value!=='object'||Array.isArray(value))throw Error('Invalid provider metadata object')}
export class ModelObservation {
 private data:Metadata;private first?:number;private closed=false;private anthropicInput:Record<string,number>={};
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
   return this.reject();
  }
 }
 private reject():false {
  this.rejectedUpdates++;this.data=Object.fromEntries(Object.entries(this.data).filter(([k])=>!accounting.includes(k))) as Metadata;this.anthropicInput={};return false;
 }
 private providerUsage(value:unknown,update:Partial<Metadata>):true {
  if(field(value,'model')!==undefined&&field(value,'model')!==null)update.model=field(value,'model') as string;
  if(counts.some(k=>k in update)){
   if(this.data.usage_source!=='provider_reported')update={...Object.fromEntries(counts.map(k=>[k,undefined])),...update};
   const combined={...this.data,...update};update.usage_source=counts.some(k=>combined[k]!==undefined)?'provider_reported':undefined;
  }
  this.record(update);return true;
 }
 observeResponses(event:unknown):boolean {
  if(this.closed)throw Error('Model observation is closed');
  try {
   const kind=field(event,'type'),delta=field(event,'delta'),item=field(event,'item');
   if(['response.output_text.delta','response.refusal.delta','response.function_call_arguments.delta'].includes(kind as string)&&typeof delta==='string'&&delta)this.firstOutput();
   if(kind==='response.output_item.added'&&field(item,'type')==='function_call'&&typeof field(item,'name')==='string'&&field(item,'name'))this.firstOutput();
   const value=['response.created','response.in_progress','response.completed','response.incomplete','response.failed'].includes(kind as string)?field(event,'response'):field(event,'object')==='response'?event:undefined;
   const update:Partial<Metadata>={},usage=field(value,'usage');
   if(usage!==undefined&&usage!==null){
    usageObject(usage);
    for(const name of ['input_tokens_details','output_tokens_details']){const details=field(usage,name);if(details!==undefined&&details!==null)usageObject(details)}
    const values={input_count:field(usage,'input_tokens'),output_count:field(usage,'output_tokens'),cache_read_count:field(field(usage,'input_tokens_details'),'cached_tokens'),reasoning_count:field(field(usage,'output_tokens_details'),'reasoning_tokens')};
    Object.assign(update,Object.fromEntries(Object.entries(values).map(([k,v])=>[k,v??undefined])));
   }
   return this.providerUsage(value,update);
  }catch{return this.reject()}
 }
 observeAnthropic(event:unknown):boolean {
  if(this.closed)throw Error('Model observation is closed');
  try {
   const kind=field(event,'type'),delta=field(event,'delta'),block=field(event,'content_block');
   if(kind==='content_block_delta'){
    const key=field(delta,'type')==='text_delta'?'text':field(delta,'type')==='input_json_delta'?'partial_json':undefined;
    if(key&&typeof field(delta,key)==='string'&&field(delta,key))this.firstOutput();
   }
   if(kind==='content_block_start'){
    const key=field(block,'type')==='text'?'text':field(block,'type')==='tool_use'?'name':undefined;
    if(key&&typeof field(block,key)==='string'&&field(block,key))this.firstOutput();
   }
   const value=kind==='message_start'?field(event,'message'):['message','message_delta'].includes(kind as string)?event:undefined;
   const update:Partial<Metadata>={},usage=field(value,'usage'),components={...this.anthropicInput};
   if(usage!==undefined&&usage!==null){
    usageObject(usage);
    for(const key of ['input_tokens','cache_creation_input_tokens','cache_read_input_tokens']){
     const count=field(usage,key);
     if(count!==undefined&&count!==null){if(!Number.isSafeInteger(count)||Number(count)<0||Number(count)>1e9)throw Error('Invalid model usage count');components[key]=count as number}
    }
    update.input_count=Object.keys(components).length===3?Object.values(components).reduce((a,b)=>a+b,0):undefined;
    for(const [target,key] of [['cache_read_count','cache_read_input_tokens'],['output_count','output_tokens']] as const){const count=field(usage,key);if(count!==undefined&&count!==null)update[target]=count as number}
   }
   this.providerUsage(value,update);this.anthropicInput=components;return true;
  }catch{return this.reject()}
 }
 snapshot():ModelUsage{return {...this.data,...(this.first!==undefined?{first_output_ms:this.first}:{})}}
 close():ModelUsage{this.closed=true;return this.snapshot()}
}
