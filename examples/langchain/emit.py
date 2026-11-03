"""Run real LangChain callbacks with synthetic work; only telemetry can leave the process."""
import asyncio
import json
import os
from pathlib import Path
import sys
if __name__=='__main__':
    for name in list(os.environ):
        if name.startswith(('LANGCHAIN_','LANGSMITH_','OTEL_')):os.environ.pop(name)
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from afterspan_langchain import AfterspanCallbackHandler
from afterspan_tracing import WorkflowTracer,mapped_id
from native_analytics import Collector


@tool
def synthetic_length(text:str)->int:
    """Return the length of a fixed synthetic reply without accessing a service."""
    return len(text)


async def exercise(tracer):
    async def request(fail):
        def prepare(value):
            if fail:raise ValueError('private failure sentinel')
            return value
        pipeline=RunnableLambda(prepare)|FakeListChatModel(responses=['private synthetic reply'])|StrOutputParser()|synthetic_length
        try:
            with tracer.task('langchain_interop','test',traffic_kind='synthetic') as root:
                with AfterspanCallbackHandler(tracer,parent=root.context) as handler:
                    failed=False
                    try:await pipeline.ainvoke('private synthetic prompt',config={'callbacks':[handler],'run_name':'private run sentinel','metadata':{'private':'private metadata sentinel'}})
                    except ValueError:
                        if not fail:raise
                        failed=True
                    status=handler.status()
                    assert status['active_runs']==0 and not any(status[k] for k in ['dropped_starts','unmatched_ends','metadata_errors','unfinished_runs'])
                    result={'trace_id':mapped_id(root.context.trace_id),'framework_failed':failed,'callback_status':status}
                if failed:raise ValueError('synthetic framework operation failed')
        except ValueError:
            if not fail:raise
        return result
    return await asyncio.gather(request(False),request(False),request(True))


def main():
    os.umask(0o077)
    if len(sys.argv)!=2:raise ValueError('provide one new private outbox path')
    destination=Path(sys.argv[1])
    if destination.exists():raise ValueError('outbox already exists; inspect it rather than rerun work')
    producer=json.load(sys.stdin)
    collector=Collector(producer['origin'],producer['key'],destination)
    traces=asyncio.run(exercise(WorkflowTracer(collector)))
    with collector.db() as db:events=[json.loads(row[0]) for row in db.execute('SELECT payload FROM outbox ORDER BY created')]
    assert 'private' not in json.dumps(events)
    queued=len(events);sent=collector.flush()
    print(json.dumps({'synthetic':True,'provider_calls':0,'observations':queued,'acknowledged':sent,'storage_verified':False,'traces':traces,'collector_status':collector.status()}))


if __name__=='__main__':main()
