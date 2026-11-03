"""Official Anthropic SDK against a loopback fixture; no native provider access."""
from contextlib import closing,contextmanager
import json
import os
from pathlib import Path
import sys
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
if __name__=='__main__':
    for name in list(os.environ):
        if name.startswith(('ANTHROPIC_','OTEL_')):os.environ.pop(name)
from anthropic import Anthropic,DefaultHttpxClient
from native_analytics import Collector
from afterspan_tracing import WorkflowTracer,mapped_id

DUMMY_KEY='sk-ant-fixture-not-a-real-key'
MODEL='fixture/messages'
CASES=('complete','stream_error','missing_stop','reasoning_only','auth_rejected','cancelled')


def wire_events(case):
    usage={'input_tokens':3,'output_tokens':1}
    if case!='reasoning_only':usage.update(cache_creation_input_tokens=7,cache_read_input_tokens=10)
    else:usage['output_tokens']=0
    events=[{'type':'message_start','message':{'id':'msg_fixture','type':'message','role':'assistant','model':MODEL,'content':[],
        'stop_reason':None,'stop_sequence':None,'usage':usage}},
        {'type':'ping'},
        {'type':'content_block_start','index':0,'content_block':{'type':'thinking','thinking':'','signature':''}},
        {'type':'content_block_delta','index':0,'delta':{'type':'thinking_delta','thinking':'DO_NOT_LEAK_THINKING'}},
        {'type':'content_block_stop','index':0}]
    if case!='reasoning_only':events.extend([
        {'type':'content_block_start','index':1,'content_block':{'type':'text','text':''}},
        {'type':'content_block_delta','index':1,'delta':{'type':'text_delta','text':'DO_NOT_LEAK_OUTPUT_é'}},
        {'type':'content_block_stop','index':1}])
    if case=='stream_error':events.append({'type':'error','error':{'type':'overloaded_error','message':'DO_NOT_LEAK_ERROR'}})
    for count,reason in [(2,None),(5,'end_turn'),(5,None)] if case!='reasoning_only' else [(3,'end_turn')]:
        events.append({'type':'message_delta','delta':{'stop_reason':reason,'stop_sequence':None},'usage':{'output_tokens':count}})
    if case!='missing_stop':events.append({'type':'message_stop'})
    return events


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*args):pass
    def do_POST(self):
        self.close_connection=True
        case=self.server.case
        valid=self.path=='/v1/messages' and self.headers.get('Authorization')=='Bearer '+DUMMY_KEY
        length=int(self.headers.get('Content-Length','0'))
        if not valid or not 0<length<=4096:
            self.send_error(400);return
        body=json.loads(self.rfile.read(length));valid=body=={'model':MODEL,'max_tokens':16,'messages':[{'role':'user','content':'DO_NOT_LEAK_PROMPT'}],'stream':True}
        if not valid:self.send_error(400);return
        self.server.requests.append(case)
        if case=='auth_rejected':
            data=json.dumps({'type':'error','error':{'type':'authentication_error','message':'DO_NOT_LEAK_AUTH_ERROR'}}).encode()
            self.send_response(401);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.send_header('Connection','close');self.end_headers();self.wfile.write(data);return
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Transfer-Encoding','chunked');self.send_header('Connection','close');self.end_headers()
        data=''.join('event: '+e['type']+'\r\ndata: '+json.dumps(e,ensure_ascii=False)+'\r\n\r\n' for e in wire_events(case)).encode()
        try:
            for offset in range(0,len(data),7):
                chunk=data[offset:offset+7];self.wfile.write(f'{len(chunk):x}\r\n'.encode()+chunk+b'\r\n');self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n');self.wfile.flush()
        except (BrokenPipeError,ConnectionResetError):pass


@contextmanager
def fixture():
    server=ThreadingHTTPServer(('127.0.0.1',0),FixtureHandler);server.case='complete';server.requests=[]
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.05},daemon=True);thread.start()
    try:yield server
    finally:server.shutdown();server.server_close();thread.join(timeout=2)


class IncompleteFixtureStream(Exception):pass
class FixtureCancelled(Exception):pass


def exercise(tracer):
    results=[]
    with fixture() as server:
        port=server.server_address[1]
        def loopback_only(request):
            if request.url.scheme!='http' or request.url.host!='127.0.0.1' or request.url.port!=port or request.url.path!='/v1/messages':raise ValueError('fixture destination refused')
            if request.headers.get('authorization')!='Bearer '+DUMMY_KEY:raise ValueError('non-fixture credential refused')
            # Ambient custom headers never enter even this local fixture.
            allowed={'authorization','content-type','content-length','accept','accept-encoding','connection','host','user-agent','anthropic-version'}
            for name in list(request.headers):
                if name.lower() not in allowed and not name.lower().startswith('x-stainless-'):del request.headers[name]
        with Anthropic(api_key=DUMMY_KEY,base_url=f'http://127.0.0.1:{port}',max_retries=0,timeout=5,open_telemetry=False,
                       http_client=DefaultHttpxClient(trust_env=False,event_hooks={'request':[loopback_only]})) as client:
            for case in CASES:
                server.case=case;state='succeeded';failure_type=None
                try:
                    with tracer.task('anthropic_fixture_'+case,'test',traffic_kind='synthetic') as root:
                        reference=mapped_id(root.context.trace_id)
                        with tracer.span('model') as span:
                            span.model_usage(provider='anthropic_fixture',model=MODEL)
                            started=False;stopped=False;reason=None
                            with client.messages.create(model=MODEL,max_tokens=16,messages=[{'role':'user','content':'DO_NOT_LEAK_PROMPT'}],stream=True) as stream:
                                for event in stream:
                                    span.observe_anthropic(event)
                                    if event.type=='message_start':started=True
                                    if event.type=='message_delta' and event.delta.stop_reason is not None:reason=event.delta.stop_reason
                                    if event.type=='message_stop':stopped=True
                                    if case=='cancelled' and event.type=='content_block_delta' and event.delta.type=='text_delta':raise FixtureCancelled()
                            if not started or not stopped or reason!='end_turn':raise IncompleteFixtureStream()
                except Exception as error:
                    state='failed';failure_type=type(error).__name__
                results.append({'case':case,'trace_id':reference,'execution_state':state,'failure_type':failure_type})
        assert server.requests==list(CASES),'each fixture request must run exactly once'
    assert all(r['execution_state']==('succeeded' if r['case'] in {'complete','reasoning_only'} else 'failed') for r in results)
    return {'anthropic_sdk_version':__import__('anthropic').__version__,'synthetic':True,'native_provider_calls':0,'loopback_fixture_requests':len(results),'results':results}


def main():
    os.umask(0o077)
    if len(sys.argv)!=2:raise ValueError('provide a new private outbox path')
    path=Path(sys.argv[1])
    if path.exists():raise ValueError('existing outbox refused; inspect it instead of replaying work')
    producer=json.load(sys.stdin);collector=Collector(producer['origin'],producer['key'],path);tracer=WorkflowTracer(collector)
    result=exercise(tracer)
    with closing(collector.db()) as db:events=[json.loads(row[0]) for row in db.execute('SELECT payload FROM outbox ORDER BY created')]
    assert len(events)==24 and 'DO_NOT_LEAK_' not in json.dumps(events) and DUMMY_KEY not in json.dumps(events)
    result.update(observations=len(events),acknowledged=collector.flush(),storage_verified=False,trace_diagnostics=tracer.status())
    print(json.dumps(result))


if __name__=='__main__':main()
