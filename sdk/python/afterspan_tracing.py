"""Context-local workflow tracing over the durable Collector. No content capture."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import hashlib
import re
import secrets
import time
import uuid
from urllib.parse import quote, unquote
from afterspan_model import ModelObservation

_current=ContextVar('afterspan_trace_context',default=None)
_STEPS={'receive','queue','processing','model','tool','retrieval','storage','http','response'}
_ENVIRONMENTS={'production','staging','development','test','local'}

def mapped_id(trace_id,span_id=None):
    if not re.fullmatch(r'[0-9a-f]{32}',trace_id) or int(trace_id,16)==0:raise ValueError('invalid W3C trace ID')
    if span_id is not None and (not re.fullmatch(r'[0-9a-f]{16}',span_id) or int(span_id,16)==0):raise ValueError('invalid W3C span ID')
    value='afterspan-otel-v1\0'+trace_id+('\0'+span_id if span_id is not None else '')
    return str(uuid.UUID(bytes=hashlib.sha256(value.encode()).digest()[:16],version=4))

@dataclass(frozen=True)
class TraceContext:
    trace_id:str
    attempt_id:str
    attempt_number:int
    workflow:str
    environment:str
    request_started_at_ms:int
    release_sha:str|None=None
    span_id:str|None=None
    sampled:bool=True
    traffic_kind:str="unknown"

    def validate(self):
        mapped_id(self.trace_id,self.span_id)
        if self.traffic_kind not in {'user','synthetic','internal','unknown'}:raise ValueError('invalid traffic kind')
        if type(self.sampled) is not bool:raise ValueError('invalid sampling decision')
        if str(uuid.UUID(self.attempt_id,version=4))!=self.attempt_id:raise ValueError('invalid attempt ID')
        if type(self.attempt_number) is not int or not 1<=self.attempt_number<=10000:raise ValueError('invalid attempt number')
        if not isinstance(self.workflow,str) or not re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}',self.workflow) or self.environment not in _ENVIRONMENTS:raise ValueError('invalid workflow context')
        if type(self.request_started_at_ms) is not int or not 0<=self.request_started_at_ms<=4102444800000:raise ValueError('invalid original clock')
        if self.release_sha is not None and not re.fullmatch(r'[a-f0-9]{40}',self.release_sha):raise ValueError('invalid release')
        return self

    def retry(self):
        return replace(self,attempt_id=str(uuid.uuid4()),attempt_number=self.attempt_number+1,span_id=None).validate()

def inject(context=None):
    context=(context or _current.get())
    if context is None or context.span_id is None:return {}
    context.validate()
    values={'workflow':context.workflow,'environment':context.environment,'attempt':context.attempt_id,
            'number':str(context.attempt_number),'clock':str(context.request_started_at_ms),'traffic':context.traffic_kind}
    if context.release_sha:values['release']=context.release_sha
    return {'traceparent':'00-'+context.trace_id+'-'+context.span_id+('-01' if context.sampled else '-00'),
            'baggage':','.join('afterspan.'+k+'='+quote(v,safe='') for k,v in values.items())}

def extract(headers,*,trusted=False):
    if not trusted:return None
    try:
        headers={k.lower():v for k,v in headers.items()}
        parent=headers.get('traceparent','');baggage=headers.get('baggage','')
        if len(baggage)>1024 or not re.fullmatch(r'00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}',parent):return None
        values={}
        for item in baggage.split(','):
            key,value=item.strip().split('=',1)
            if key.startswith('afterspan.'):
                key=key[10:]
                if key in values:return None
                values[key]=unquote(value)
        return TraceContext(parent[3:35],values['attempt'],int(values['number']),values['workflow'],values['environment'],int(values['clock']),values.get('release'),parent[36:52],bool(int(parent[-2:],16)&1),values.get('traffic','unknown')).validate()
    except (ValueError,TypeError,KeyError,AttributeError):return None

class Span:
    def __init__(self,tracer,context,parent,step):
        self.tracer=tracer;self.context=context;self.parent=parent;self.step=step;self.checks=[];self.closed=False;self.uncertain=False
        self.started=time.monotonic()*1000;self._model=None
    def model_usage(self,**fields):
        if self.closed or self.step!='model':raise ValueError('model metadata requires an open model span')
        observation=self._model or ModelObservation(fields.get('provider'),fields.get('model'),fields.get('template_version'),started_at_ms=self.started)
        observation.record(**fields);self._model=observation
    def first_output(self):
        if self.closed or self._model is None:raise ValueError('declare model identity on an open model span first')
        return self._model.first_output()
    def observe_openai(self,chunk,exact_cost_usd=None):
        if self.closed or self._model is None:raise ValueError('declare model identity on an open model span first')
        return self._model.observe_openai(chunk,exact_cost_usd)
    def observe_responses(self,event):
        if self.closed or self._model is None:raise ValueError('declare model identity on an open model span first')
        return self._model.observe_responses(event)
    def observe_anthropic(self,event):
        if self.closed or self._model is None:raise ValueError('declare model identity on an open model span first')
        return self._model.observe_anthropic(event)
    def model_result(self,duration):
        if self._model is None:return None
        result=self._model.close();self.tracer.model_metadata_errors+=self._model.rejected_updates
        if duration>86400000:result.pop('first_output_ms',None)
        return result
    def mark_unknown(self):
        if self.closed:raise ValueError('span already closed')
        self.uncertain=True
    def facts(self):
        c=self.context
        return {'schema_version':'workflow-trace-v1','trace_id':mapped_id(c.trace_id),'attempt_id':c.attempt_id,
                'attempt_number':c.attempt_number,'span_id':mapped_id(c.trace_id,c.span_id),'parent_span_id':self.parent,
                'workflow':c.workflow,'environment':c.environment,'traffic_kind':c.traffic_kind,'release_sha':c.release_sha,'request_started_at_ms':c.request_started_at_ms,'step':self.step}
    def check(self,name,result,*,version=1,method='source',phase='before_delivery',required=True,evidence_digest=None,execution=None,reason_code=None):
        if self.closed:raise ValueError('span already closed')
        if not isinstance(name,str) or not re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}',name) or result not in {'passed','failed','unknown'} or type(version) is not int or not 1<=version<=1000000:raise ValueError('invalid check')
        if method not in {'source','code','model','human'} or phase not in {'before_delivery','after_delivery'} or type(required) is not bool:raise ValueError('invalid check definition')
        if len(self.checks)>=20 or any(c['name']==name for c in self.checks):raise ValueError('checks must be unique and bounded')
        if execution not in (None,'completed','not_run') or reason_code not in (None,'evidence_absent','evidence_binding_rejected'):raise ValueError('invalid check execution diagnostic')
        if reason_code is not None and execution!='not_run' or execution=='not_run' and (result!='unknown' or evidence_digest is not None):raise ValueError('check execution contradicts result or evidence')
        check={'name':name,'version':version,'result':result,'method':method,'phase':phase,'required':required}
        if execution is not None:check['execution']=execution
        if reason_code is not None:check['reason_code']=reason_code
        if evidence_digest is not None:
            if not isinstance(evidence_digest,str) or not re.fullmatch(r'[a-f0-9]{64}',evidence_digest):raise ValueError('provide a SHA-256 evidence digest, never source content')
            receipt=str(uuid.uuid4());facts=self.facts()
            evidence={k:check[k] for k in ['name','version','result','method','phase']}
            evidence.update({k:facts[k] for k in ['trace_id','attempt_id','span_id','workflow','environment','release_sha']})
            evidence.update(schema_version='workflow-evidence-v1',receipt_id=receipt,evidence_digest=evidence_digest)
            self.tracer.emit('evidence.observed',{'afterspan_evidence':evidence})
            check['evidence_ref']=receipt
        self.checks.append(check)
        return result

class WorkflowTracer:
    def __init__(self,collector,*,subject_id=None):
        if subject_id is not None and (not isinstance(subject_id,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,31}_[a-f0-9]{64}',subject_id)):raise ValueError('subject must be an opaque project-scoped digest')
        self.collector=collector;self._subject_id=subject_id;self.emit_failures=0;self.sampled_out_events=0;self.model_metadata_errors=0
    def emit(self,name,properties):
        context=_current.get()
        if context is not None and not context.sampled:self.sampled_out_events+=1;return
        try:
            if self.collector.track(name,distinct_id=self._subject_id,properties=properties) is None:self.emit_failures+=1
        except Exception:self.emit_failures+=1
    @contextmanager
    def task(self,workflow=None,environment='production',release_sha=None,*,context=None,traffic_kind='unknown'):
        context=context or TraceContext(secrets.token_hex(16),str(uuid.uuid4()),1,workflow,environment,int(time.time()*1000),release_sha,traffic_kind=traffic_kind)
        context.validate();token=_current.set(context)
        try:
            with self.span('processing') as span:yield span
        finally:_current.reset(token)
    @contextmanager
    def span(self,step,*,milestone=None,disposition=None,context=None):
        context=context or _current.get()
        if context is None:raise ValueError('start or continue a task before a span')
        context.validate()
        if step not in _STEPS:raise ValueError('unsupported span step')
        if milestone not in {None,'completion','delivery','usefulness'} or disposition not in {None,'delivered','withheld','clarification','partial'} or disposition is not None and step!='response':raise ValueError('invalid terminal declaration')
        parent=mapped_id(context.trace_id,context.span_id) if context.span_id else None
        child=replace(context,span_id=secrets.token_hex(8));span=Span(self,child,parent,step)
        token=_current.set(child);state='succeeded';code='none'
        self.emit('trace.observed',{'afterspan_trace':{**span.facts(),'state':'started'}})
        try:yield span
        except BaseException:
            state='failed';code='internal';raise
        finally:
            duration=time.monotonic()*1000-span.started
            known=duration<=86400000 and not span.uncertain
            end={**span.facts(),'state':state if known else 'unknown','outcome_code':code if known else 'uncertain','duration_ms':duration if duration<=86400000 else None}
            if milestone:end['milestone']=milestone
            if disposition:end['disposition']=disposition
            if span.checks:end['checks']=span.checks
            usage=span.model_result(duration)
            if usage is not None:end['model_usage']=usage
            self.emit('trace.observed',{'afterspan_trace':end});span.closed=True;_current.reset(token)
    def status(self):return {'emit_failures':self.emit_failures,'sampled_out_events':self.sampled_out_events,'model_metadata_errors':self.model_metadata_errors,**self.collector.status()}


def prepare_debug_snapshot(span,content,policy_revision,*,kind='context',privacy_reviewed=False):
    """Prepare selected content only; send explicitly to /v1/debug after metadata flush."""
    from afterspan_experiments import redact_debug
    if privacy_reviewed is not True or type(policy_revision) is not int or not 1<=policy_revision<=1000 or kind not in {'input','output','context'} or not isinstance(content,dict):raise ValueError('review selected content and provide an explicit policy revision')
    facts=span.facts()
    return {'id':str(uuid.uuid4()),'policy_revision':policy_revision,'privacy_reviewed':True,'kind':kind,
            **{k:facts[k] for k in ['trace_id','attempt_id','span_id','workflow','environment']},'content':redact_debug(content)}
