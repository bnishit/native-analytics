"""OTLP/HTTP trace ingestion with a strict metadata-only allowlist."""
import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import re
import time
import uuid
import zlib
from fastapi import Depends, HTTPException, Request, Response
from google.protobuf.json_format import ParseDict, MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest, ExportTraceServiceResponse
from tracing import SpanObservation, ModelUsage

MAX_BYTES=2_000_000
MAX_SPANS=500

def mapped_id(trace_id,span_id=None):
    if not re.fullmatch(r'[0-9a-f]{32}',trace_id) or int(trace_id,16)==0:raise ValueError('invalid trace ID')
    if span_id is not None and (not re.fullmatch(r'[0-9a-f]{16}',span_id) or int(span_id,16)==0):raise ValueError('invalid span ID')
    value='afterspan-otel-v1\0'+trace_id+('\0'+span_id if span_id is not None else '')
    return str(uuid.UUID(bytes=hashlib.sha256(value.encode()).digest()[:16],version=4))

def attributes(items):
    values={}
    for item in items:
        if item.key in values:raise ValueError('duplicate attribute')
        kind=item.value.WhichOneof('value')
        values[item.key]=getattr(item.value,kind) if kind in {'string_value','int_value','double_value','bool_value'} else None
    return values

def decode(body,content_type,encoding):
    if encoding=='gzip':
        try:
            decoder=zlib.decompressobj(16+zlib.MAX_WBITS)
            body=decoder.decompress(body,MAX_BYTES+1)
            if len(body)>MAX_BYTES or decoder.unconsumed_tail:raise HTTPException(413,'decompressed request exceeds 2 MB')
            if not decoder.eof or decoder.unused_data:raise ValueError('invalid gzip')
        except (zlib.error,ValueError):raise HTTPException(400,'invalid OTLP compression') from None
    elif encoding:raise HTTPException(415,'unsupported content encoding')
    try:
        message=ExportTraceServiceRequest()
        if content_type=='application/x-protobuf':message.ParseFromString(body)
        elif content_type=='application/json':
            data=json.loads(body)
            for resource in data.get('resourceSpans',[]):
                for scope in resource.get('scopeSpans',[]):
                    for span in scope.get('spans',[]):
                        for key,length in [('traceId',32),('spanId',16),('parentSpanId',16)]:
                            value=span.get(key)
                            if value is not None:
                                if not isinstance(value,str) or not re.fullmatch('[a-fA-F0-9]{'+str(length)+'}',value):raise ValueError('invalid hex ID')
                                span[key]=base64.b64encode(bytes.fromhex(value)).decode()
                        # Links/events are deliberately not collected, including their content.
                        span.pop('links',None);span.pop('events',None)
            ParseDict(data,message,ignore_unknown_fields=True)
        else:raise HTTPException(415,'use application/x-protobuf or application/json')
        return message
    except HTTPException:raise
    except Exception:raise HTTPException(400,'invalid OTLP trace payload') from None

def convert(span,resource,event_type,retention_days):
    trace_id=span.trace_id.hex();span_id=span.span_id.hex()
    trace=mapped_id(trace_id);sid=mapped_id(trace_id,span_id)
    values={**attributes(resource.attributes),**attributes(span.attributes)}
    start=span.start_time_unix_nano/1e9;end=span.end_time_unix_nano/1e9
    now=time.time()
    if not now-retention_days*86400<=start<=end<=now+300 or end-start>86400:raise ValueError('invalid span clock')
    step=values.get('afterspan.step','model' if 'gen_ai.provider.name' in values else 'processing')
    facts={'schema_version':'workflow-trace-v1','trace_id':trace,'span_id':sid,
           'parent_span_id':mapped_id(trace_id,span.parent_span_id.hex()) if span.parent_span_id else None,
           'attempt_id':values.get('afterspan.attempt_id',trace),'attempt_number':values.get('afterspan.attempt_number',1),
           'workflow':values.get('afterspan.workflow'),'environment':values.get('afterspan.environment'),
           'traffic_kind':values.get('afterspan.traffic_kind','unknown'),'release_sha':values.get('afterspan.release_sha'),'request_started_at_ms':values.get('afterspan.request_started_at_ms'),
           'step':step,'state':'started'}
    begin=SpanObservation.model_validate(facts).model_dump(mode='json')
    state={0:'unknown',1:'succeeded',2:'failed'}[span.status.code]
    facts.update(state=state,duration_ms=(span.end_time_unix_nano-span.start_time_unix_nano)/1e6,outcome_code='internal' if state=='failed' else 'uncertain' if state=='unknown' else 'none')
    if step=='model' and 'gen_ai.provider.name' in values and 'gen_ai.request.model' in values:
        usage={'provider':values['gen_ai.provider.name'],'model':values['gen_ai.request.model']}
        names={'gen_ai.usage.input_tokens':'input_count','gen_ai.usage.output_tokens':'output_count',
               'afterspan.model.cache_read_count':'cache_read_count','afterspan.model.reasoning_count':'reasoning_count',
               'afterspan.model.template_version':'template_version','afterspan.model.first_output_ms':'first_output_ms',
               'afterspan.model.cost_microusd':'cost_microusd','afterspan.model.cost_source':'cost_source','afterspan.model.price_version':'price_version'}
        usage.update({target:values[source] for source,target in names.items() if source in values})
        if any(k in usage for k in ['input_count','output_count','cache_read_count','reasoning_count']):usage['usage_source']=values.get('afterspan.model.usage_source','provider_reported')
        facts['model_usage']=ModelUsage.model_validate(usage).model_dump(mode='json')
    terminal=SpanObservation.model_validate(facts).model_dump(mode='json')
    events=[]
    for at,observation in [(start,begin),(end,terminal)]:
        payload=json.dumps([at,observation],sort_keys=True,separators=(',',':'))
        events.append(event_type(id='otel_'+hashlib.sha256(payload.encode()).hexdigest(),name='trace.observed',timestamp=datetime.fromtimestamp(at,timezone.utc),properties={'afterspan_trace':observation}))
    return events

def install(app,writer,connect,store_events,event_type,retention_days):
    @app.post('/v1/traces')
    async def export(request:Request,project=Depends(writer)):
        content_type=request.headers.get('content-type','').split(';')[0].strip().lower()
        message=decode(await request.body(),content_type,request.headers.get('content-encoding','').lower())
        spans=[(s,r.resource) for r in message.resource_spans for scope in r.scope_spans for s in scope.spans]
        if len(spans)>MAX_SPANS:raise HTTPException(413,'at most 500 spans per request')
        events=[];rejected=0
        for span,resource in spans:
            try:events.extend(convert(span,resource,event_type,retention_days))
            except (ValueError,TypeError,KeyError,OverflowError):rejected+=1
        if events:
            with closing(connect()) as c,c:
                c.execute('BEGIN IMMEDIATE')
                for offset in range(0,len(events),100):store_events(c,project,events[offset:offset+100])
        request.state.rejected_spans=rejected
        result=ExportTraceServiceResponse()
        if rejected:
            result.partial_success.rejected_spans=rejected
            result.partial_success.error_message='Unsupported or invalid metadata; consult the Afterspan OTLP mapping.'
        if content_type=='application/json':return Response(json.dumps(MessageToDict(result)),media_type=content_type)
        return Response(result.SerializeToString(),media_type=content_type)
