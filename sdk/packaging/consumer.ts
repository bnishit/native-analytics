import { createAnalytics, WorkflowTracer, ModelObservation, type TraceContext } from '@afterspan/sdk';
import { ModelObservation as Standalone } from '@afterspan/sdk/model';
const tracer = new WorkflowTracer(createAnalytics(),{subjectId:'account_'+'a'.repeat(64)});
const observation: ModelObservation = new Standalone({ provider: 'openai', model: 'fixture/model' });
const context: TraceContext | undefined = undefined;
void tracer.task({ workflow: 'typed_probe', environment: 'test', context }, () => tracer.span('processing', span => {
  span.check('source_facts', 'unknown', { execution: 'not_run', reasonCode: 'evidence_absent' });
  return observation.snapshot();
}));

observation.observeResponses({type:'response.created'});
observation.observeAnthropic({type:'ping'});
void tracer.span('model',span=>{span.modelUsage({provider:'example',model:'fixture/model'});span.observeResponses({});span.observeAnthropic({});});

const streaming=tracer.response({workflow:'typed_stream',environment:'test'},()=>{
  const events=tracer.stream('model',async function*(span){span.modelUsage({provider:'example',model:'fixture/model'});yield 'synthetic'});
  void events;return new Response('synthetic');
});
void streaming.then(async({response,finished})=>{await response.text();return finished});
