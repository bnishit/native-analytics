"""Bounded model metadata shared by tracing adapters. Never retains model content."""
import math
import re
import time
from decimal import Decimal

_COUNTS=('input_count','output_count','cache_read_count','reasoning_count')
_ACCOUNTING=(*_COUNTS,'usage_source','cost_microusd','cost_usd_decimal','cost_source','price_version')
_FIELDS={'provider','model','template_version','unit',*_ACCOUNTING}

def _validated(value):
    if set(value)-_FIELDS:raise ValueError('unsupported model metadata field')
    data={k:v for k,v in value.items() if v is not None}
    for name,pattern in [('provider',r'[a-z][a-z0-9_.-]{0,63}'),('model',r'[A-Za-z0-9][A-Za-z0-9_./:-]{0,127}'),('template_version',r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}')]:
        if name=='template_version' and name not in data:continue
        if not isinstance(data.get(name),str) or not re.fullmatch(pattern,data[name]):raise ValueError('invalid model identity')
    if data.get('unit','token')!='token':raise ValueError('unsupported model unit')
    data['unit']='token'
    for name in _COUNTS:
        if name in data and (type(data[name]) is not int or not 0<=data[name]<=1000000000):raise ValueError('invalid model usage count')
    has_usage=any(name in data for name in _COUNTS)
    if has_usage!=(data.get('usage_source') in {'provider_reported','estimated'}) or not has_usage and 'usage_source' in data:raise ValueError('usage requires its source')
    if 'cost_microusd' in data and (type(data['cost_microusd']) is not int or not 0<=data['cost_microusd']<=1000000000000):raise ValueError('invalid model cost')
    if 'cost_usd_decimal' in data:
        cost=data['cost_usd_decimal']
        if not isinstance(cost,str) or not re.fullmatch(r'(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,12})?',cost) or Decimal(cost)>Decimal('1000000'):raise ValueError('provide an exact bounded USD string')
        data['cost_usd_decimal']=cost.rstrip('0').rstrip('.') if '.' in cost else cost
    has_cost='cost_microusd' in data or 'cost_usd_decimal' in data
    if 'cost_microusd' in data and 'cost_usd_decimal' in data or has_cost!=(data.get('cost_source') in {'provider_reported','price_table'}) or not has_cost and 'cost_source' in data:raise ValueError('cost requires one amount and its source')
    if (data.get('cost_source')=='price_table')!=('price_version' in data) or 'price_version' in data and (not isinstance(data['price_version'],str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',data['price_version'])):raise ValueError('price-table cost requires a version')
    if any(a in data and b in data and data[a]>data[b] for a,b in [('cache_read_count','input_count'),('reasoning_count','output_count')]):raise ValueError('detail count exceeds total')
    return data

def _field(value,name):
    return value.get(name) if isinstance(value,dict) else getattr(value,name,None)

def _usage_object(value):
    if not isinstance(value,dict) and not hasattr(value,'__dict__'):raise ValueError('invalid provider metadata object')
    return value

class ModelObservation:
    def __init__(self,provider,model,template_version=None,*,clock=None,started_at_ms=None):
        self.clock=clock or (lambda:time.monotonic()*1000)
        self.started=self.clock() if started_at_ms is None else started_at_ms
        if type(self.started) not in {int,float} or not math.isfinite(self.started) or self.started<0:raise ValueError('invalid model clock')
        self.data=_validated({'provider':provider,'model':model,'template_version':template_version})
        self.first=None;self.closed=False;self.rejected_updates=0;self._anthropic_input={}
    def record(self,**fields):
        if self.closed:raise ValueError('model observation is closed')
        self.data=_validated({**self.data,**fields})
    def first_output(self):
        if self.closed:raise ValueError('model observation is closed')
        if self.first is None:
            elapsed=self.clock()-self.started
            if not math.isfinite(elapsed) or not 0<=elapsed<=86400000:raise ValueError('invalid model output clock')
            self.first=elapsed
        return self.first
    def observe_openai(self,chunk,exact_cost_usd=None):
        """Observe parsed Chat Completions chunks, including official SDK objects."""
        if self.closed:raise ValueError('model observation is closed')
        try:
            present='choices' in chunk if isinstance(chunk,dict) else hasattr(chunk,'choices')
            choices=_field(chunk,'choices') if present else []
            if not isinstance(choices,(list,tuple)):raise ValueError('invalid model choices')
            for choice in choices:
                delta=_field(choice,'delta')
                content=any(isinstance(_field(delta,k),str) and bool(_field(delta,k)) for k in ['content','refusal'])
                tools=any(any(isinstance(_field(_field(tool,'function'),k),str) and bool(_field(_field(tool,'function'),k)) for k in ['name','arguments']) for tool in _field(delta,'tool_calls') or [])
                if content or tools:self.first_output()
            fields={}
            if _field(chunk,'model') is not None:fields['model']=_field(chunk,'model')
            usage=_field(chunk,'usage')
            if usage is not None:
                fields.update(input_count=_field(usage,'prompt_tokens'),output_count=_field(usage,'completion_tokens'),cache_read_count=_field(_field(usage,'prompt_tokens_details'),'cached_tokens'),reasoning_count=_field(_field(usage,'completion_tokens_details'),'reasoning_tokens'))
                fields['usage_source']='provider_reported' if any(fields.get(k) is not None for k in _COUNTS) else None
                cost=exact_cost_usd if exact_cost_usd is not None else _field(usage,'cost')
                fields.update(cost_usd_decimal=cost if isinstance(cost,str) else None,cost_source='provider_reported' if isinstance(cost,str) else None)
            self.record(**fields);return True
        except (ValueError,TypeError,AttributeError):
            return self._reject()
    def _reject(self):
        self.rejected_updates+=1
        self.data={k:v for k,v in self.data.items() if k not in _ACCOUNTING}
        self._anthropic_input={}
        return False
    def _provider_usage(self,value,fields):
        if _field(value,'model') is not None:fields['model']=_field(value,'model')
        if any(k in fields for k in _COUNTS):
            if self.data.get('usage_source')!='provider_reported':
                fields={**{k:None for k in _COUNTS},**fields}
            combined={**self.data,**fields}
            fields['usage_source']='provider_reported' if any(combined.get(k) is not None for k in _COUNTS) else None
        self.record(**fields)
        return True
    def observe_responses(self,event):
        """Observe parsed Responses objects/events; transport owns completion/errors."""
        if self.closed:raise ValueError('model observation is closed')
        try:
            kind=_field(event,'type')
            if kind in {'response.output_text.delta','response.refusal.delta','response.function_call_arguments.delta'}:
                delta=_field(event,'delta')
                if isinstance(delta,str) and delta:self.first_output()
            item=_field(event,'item')
            if kind=='response.output_item.added' and _field(item,'type')=='function_call' and isinstance(_field(item,'name'),str) and _field(item,'name'):self.first_output()
            value=_field(event,'response') if kind in {'response.created','response.in_progress','response.completed','response.incomplete','response.failed'} else event if _field(event,'object')=='response' else None
            fields={};usage=_field(value,'usage')
            if usage is not None:
                _usage_object(usage)
                for name in ['input_tokens_details','output_tokens_details']:
                    if _field(usage,name) is not None:_usage_object(_field(usage,name))
                fields=dict(input_count=_field(usage,'input_tokens'),output_count=_field(usage,'output_tokens'),cache_read_count=_field(_field(usage,'input_tokens_details'),'cached_tokens'),reasoning_count=_field(_field(usage,'output_tokens_details'),'reasoning_tokens'))
            return self._provider_usage(value,fields)
        except (ValueError,TypeError,AttributeError):return self._reject()
    def observe_anthropic(self,event):
        """Observe Messages objects/events. Cumulative counts replace, never add."""
        if self.closed:raise ValueError('model observation is closed')
        try:
            kind=_field(event,'type');delta=_field(event,'delta');block=_field(event,'content_block')
            if kind=='content_block_delta':
                key={'text_delta':'text','input_json_delta':'partial_json'}.get(_field(delta,'type'))
                if key and isinstance(_field(delta,key),str) and _field(delta,key):self.first_output()
            if kind=='content_block_start':
                key={'text':'text','tool_use':'name'}.get(_field(block,'type'))
                if key and isinstance(_field(block,key),str) and _field(block,key):self.first_output()
            value=_field(event,'message') if kind=='message_start' else event if kind in {'message','message_delta'} else None
            fields={};usage=_field(value,'usage');components=dict(self._anthropic_input)
            if usage is not None:
                _usage_object(usage)
                for key in ['input_tokens','cache_creation_input_tokens','cache_read_input_tokens']:
                    count=_field(usage,key)
                    if count is not None:
                        if type(count) is not int or not 0<=count<=1000000000:raise ValueError('invalid model usage count')
                        components[key]=count
                fields['input_count']=sum(components.values()) if len(components)==3 else None
                for target,key in [('cache_read_count','cache_read_input_tokens'),('output_count','output_tokens')]:
                    if _field(usage,key) is not None:fields[target]=_field(usage,key)
            self._provider_usage(value,fields);self._anthropic_input=components
            return True
        except (ValueError,TypeError,AttributeError):return self._reject()
    def snapshot(self):
        return {**self.data,**({'first_output_ms':self.first} if self.first is not None else {})}
    def close(self):
        self.closed=True
        return self.snapshot()
