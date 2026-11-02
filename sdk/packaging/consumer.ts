import { createAnalytics, WorkflowTracer, ModelObservation, type TraceContext } from '@afterspan/sdk';
import { ModelObservation as Standalone } from '@afterspan/sdk/model';
const tracer = new WorkflowTracer(createAnalytics());
const observation: ModelObservation = new Standalone({ provider: 'openai', model: 'fixture/model' });
const context: TraceContext | undefined = undefined;
void tracer.task({ workflow: 'typed_probe', environment: 'test', context }, () => tracer.span('processing', span => {
  span.check('source_facts', 'unknown', { execution: 'not_run', reasonCode: 'evidence_absent' });
  return observation.snapshot();
}));
