"""Optional LangChain callbacks. Run names, inputs, outputs and errors are never retained."""
from contextvars import Context
import threading
from langchain_core.callbacks import BaseCallbackHandler


class AfterspanCallbackHandler(BaseCallbackHandler):
    """Use one bounded handler per task, inside its parent span; always close it."""
    run_inline=True
    raise_error=False

    def __init__(self,tracer,*,parent,models=(),reported_usage=False):
        parent.validate()
        self.tracer=tracer;self.parent=parent;self.models=frozenset(models);self.reported_usage=reported_usage
        self._runs={};self._lock=threading.RLock();self._closed=False
        self._diagnostics={'dropped_starts':0,'unmatched_ends':0,'metadata_errors':0,'unfinished_runs':0}

    def __enter__(self):return self
    def __exit__(self,*args):self.close()

    def _start(self,step,run_id,parent_run_id,metadata=None):
        with self._lock:
            if self._closed or run_id in self._runs or len(self._runs)>=1024 or parent_run_id is not None and parent_run_id not in self._runs:
                self._diagnostics['dropped_starts']+=1;return
            parent=self._runs[parent_run_id]['span'].context if parent_run_id is not None else self.parent
            context=Context();manager=self.tracer.span(step,context=parent)
            span=context.run(manager.__enter__)
            run={'context':context,'manager':manager,'span':span};self._runs[run_id]=run
            identity=(metadata.get('ls_provider'),metadata.get('ls_model_name')) if isinstance(metadata,dict) else None
            if identity is not None and not all(isinstance(x,str) for x in identity):identity=None
            if step=='model' and identity in self.models:
                try:context.run(span.model_usage,provider=identity[0],model=identity[1])
                except (ValueError,TypeError):self._diagnostics['metadata_errors']+=1

    def _end(self,run_id,*,failed=False,response=None,unknown=False):
        with self._lock:
            run=self._runs.get(run_id)
            if run is None or run['span'].closed:
                self._diagnostics['unmatched_ends']+=1;return
            context=run['context'];span=run['span']
            if response is not None and self.reported_usage and span._model is not None:
                try:
                    generations=response.generations
                    if len(generations)!=1 or len(generations[0])!=1:raise ValueError('ambiguous model usage')
                    usage=getattr(generations[0][0].message,'usage_metadata',None)
                    if usage is not None:
                        fields={'input_count':usage.get('input_tokens'),'output_count':usage.get('output_tokens'),
                                'cache_read_count':(usage.get('input_token_details') or {}).get('cache_read'),
                                'reasoning_count':(usage.get('output_token_details') or {}).get('reasoning')}
                        context.run(span.model_usage,**fields,usage_source='provider_reported' if any(v is not None for v in fields.values()) else None)
                except (ValueError,TypeError,AttributeError):self._diagnostics['metadata_errors']+=1
            if unknown:span.mark_unknown()
            error=RuntimeError('framework operation failed') if failed else None
            context.run(run['manager'].__exit__,type(error) if error else None,error,None)
            # Keep only the bounded parent identity after terminal observation.
            run.pop('context');run.pop('manager')

    def on_chain_start(self,serialized,inputs,*,run_id,parent_run_id=None,**kwargs):self._start('processing',run_id,parent_run_id)
    def on_chain_end(self,outputs,*,run_id,**kwargs):self._end(run_id)
    def on_chain_error(self,error,*,run_id,**kwargs):self._end(run_id,failed=True)
    def on_tool_start(self,serialized,input_str,*,run_id,parent_run_id=None,**kwargs):self._start('tool',run_id,parent_run_id)
    def on_tool_end(self,output,*,run_id,**kwargs):self._end(run_id)
    def on_tool_error(self,error,*,run_id,**kwargs):self._end(run_id,failed=True)
    def on_retriever_start(self,serialized,query,*,run_id,parent_run_id=None,**kwargs):self._start('retrieval',run_id,parent_run_id)
    def on_retriever_end(self,documents,*,run_id,**kwargs):self._end(run_id)
    def on_retriever_error(self,error,*,run_id,**kwargs):self._end(run_id,failed=True)
    def on_chat_model_start(self,serialized,messages,*,run_id,parent_run_id=None,metadata=None,**kwargs):self._start('model',run_id,parent_run_id,metadata)
    def on_llm_start(self,serialized,prompts,*,run_id,parent_run_id=None,metadata=None,**kwargs):self._start('model',run_id,parent_run_id,metadata)
    def on_llm_end(self,response,*,run_id,**kwargs):self._end(run_id,response=response)
    def on_llm_error(self,error,*,run_id,**kwargs):self._end(run_id,failed=True)

    def on_llm_new_token(self,token,*,run_id,chunk=None,**kwargs):
        with self._lock:
            run=self._runs.get(run_id)
            if run is None or run['span'].closed or run['span']._model is None:return
            message=getattr(chunk,'message',None);content=getattr(message,'content',None)
            text=isinstance(content,str) and bool(content) or isinstance(content,list) and any(isinstance(x,dict) and x.get('type')=='text' and isinstance(x.get('text'),str) and bool(x['text']) for x in content)
            tools=any(isinstance(x,dict) and any(isinstance(x.get(k),str) and bool(x[k]) for k in ('name','args')) for x in getattr(message,'tool_call_chunks',[]) or [])
            if text or tools:run['context'].run(run['span'].first_output)

    def close(self):
        with self._lock:
            if self._closed:return
            self._closed=True
            for run_id,run in reversed(list(self._runs.items())):
                if not run['span'].closed:
                    self._diagnostics['unfinished_runs']+=1;self._end(run_id,unknown=True)
            self._runs.clear()

    def status(self):
        with self._lock:return {**self._diagnostics,'tracked_runs':len(self._runs),'active_runs':sum(not r['span'].closed for r in self._runs.values()),'closed':self._closed}
