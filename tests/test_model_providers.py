"""Provider wire semantics, without retaining model content or making API calls."""
import json
from types import SimpleNamespace
import unittest
import test_analytics
from afterspan_model import ModelObservation

class ProviderObservationTests(unittest.TestCase):
    def observation(self,provider):
        self.now=100
        return ModelObservation(provider,'fixture/model',clock=lambda:self.now)
    def test_responses_only_meaningful_deltas_start_output_clock(self):
        o=self.observation('openai')
        for event in [{'type':'response.created'},{'type':'response.reasoning_text.delta','delta':'private-thinking'},{'type':'response.output_text.delta','delta':''}]:
            self.assertTrue(o.observe_responses(event))
        self.assertNotIn('first_output_ms',o.snapshot())
        self.now=125;o.observe_responses({'type':'response.output_text.delta','delta':'private-output'})
        self.now=150;o.observe_responses({'type':'response.refusal.delta','delta':'private-refusal'})
        self.assertEqual(o.snapshot()['first_output_ms'],25)
        self.assertNotIn('private-',json.dumps(o.snapshot()))
    def test_responses_usage_from_complete_incomplete_and_failed_objects(self):
        for kind in ['response.completed','response.incomplete','response.failed']:
            o=self.observation('openai')
            response=SimpleNamespace(model='resolved/model',usage=SimpleNamespace(input_tokens=10,output_tokens=4,input_tokens_details=SimpleNamespace(cached_tokens=6),output_tokens_details=SimpleNamespace(reasoning_tokens=2)))
            self.assertTrue(o.observe_responses(SimpleNamespace(type=kind,response=response)))
            self.assertEqual(o.snapshot(),dict(provider='openai',model='resolved/model',unit='token',input_count=10,output_count=4,cache_read_count=6,reasoning_count=2,usage_source='provider_reported'))
    def test_responses_tool_arguments_are_output_but_nonstream_timing_is_unknown(self):
        o=self.observation('openai');self.now=110
        o.observe_responses({'type':'response.function_call_arguments.delta','delta':'private-tool-input'})
        self.assertEqual(o.snapshot()['first_output_ms'],10)
        o=self.observation('openai');o.observe_responses({'object':'response','model':'fixture/model','output':[{'content':'private-output'}],'usage':{'input_tokens':0,'output_tokens':0}})
        self.assertEqual(o.snapshot()['input_count'],0);self.assertNotIn('first_output_ms',o.snapshot());self.assertNotIn('cost_usd_decimal',o.snapshot())
    def test_anthropic_cached_input_and_cumulative_output_are_not_double_counted(self):
        o=self.observation('anthropic')
        o.observe_anthropic({'type':'message_start','message':{'model':'resolved/model','usage':{'input_tokens':3,'cache_creation_input_tokens':7,'cache_read_input_tokens':10,'output_tokens':1}}})
        for count in [2,5,5]:o.observe_anthropic({'type':'message_delta','usage':{'output_tokens':count}})
        self.assertEqual(o.snapshot()['input_count'],20);self.assertEqual(o.snapshot()['cache_read_count'],10);self.assertEqual(o.snapshot()['output_count'],5)
        self.assertNotIn('cost_usd_decimal',o.snapshot())
    def test_anthropic_missing_cache_components_are_unknown_not_zero(self):
        o=self.observation('anthropic');o.observe_anthropic({'type':'message','usage':{'input_tokens':3,'output_tokens':0}})
        self.assertNotIn('input_count',o.snapshot());self.assertEqual(o.snapshot()['output_count'],0)
        o.observe_anthropic({'type':'message_delta','usage':{'cache_read_input_tokens':0}})
        self.assertNotIn('input_count',o.snapshot())
        o.observe_anthropic({'type':'message_delta','usage':{'cache_creation_input_tokens':0}})
        self.assertEqual(o.snapshot()['input_count'],3)
    def test_anthropic_first_output_ignores_thinking_and_ping(self):
        o=self.observation('anthropic')
        for event in [{'type':'ping'},{'type':'content_block_delta','delta':{'type':'thinking_delta','thinking':'private-thinking'}},{'type':'content_block_start','content_block':{'type':'text','text':''}}]:o.observe_anthropic(event)
        self.assertNotIn('first_output_ms',o.snapshot())
        self.now=120;o.observe_anthropic({'type':'content_block_delta','delta':{'type':'input_json_delta','partial_json':'private-tool-input'}})
        self.now=140;o.observe_anthropic({'type':'content_block_delta','delta':{'type':'text_delta','text':'private-output'}})
        self.assertEqual(o.snapshot()['first_output_ms'],20);self.assertNotIn('private-',json.dumps(o.snapshot()))
    def test_invalid_accounting_clears_stale_counts_and_closed_observers_reject(self):
        for method,payload in [('observe_responses',{'object':'response','usage':{'input_tokens':True}}),('observe_anthropic',{'type':'message_delta','usage':{'cache_creation_input_tokens':True}})]:
            o=self.observation('example');o.record(input_count=3,usage_source='provider_reported')
            self.assertFalse(getattr(o,method)(payload));self.assertEqual(o.rejected_updates,1);self.assertNotIn('input_count',o.snapshot())
            o.close()
            with self.assertRaises(ValueError):getattr(o,method)({})
    def test_anthropic_objects_and_tool_start_and_nonstream_content(self):
        o=self.observation('anthropic')
        o.observe_anthropic(SimpleNamespace(type='message',model='resolved/model',usage=SimpleNamespace(input_tokens=1,cache_creation_input_tokens=0,cache_read_input_tokens=0,output_tokens=2),content=['private-output']))
        self.assertEqual(o.snapshot()['input_count'],1);self.assertNotIn('first_output_ms',o.snapshot())
        self.now=115;o.observe_anthropic({'type':'content_block_start','content_block':{'type':'tool_use','name':'private-tool-name'}})
        self.assertEqual(o.snapshot()['first_output_ms'],15)
    def test_provider_update_cannot_relabel_an_estimate_as_reported_usage(self):
        o=self.observation('anthropic');o.record(output_count=99,reasoning_count=12,usage_source='estimated')
        o.observe_anthropic({'type':'message_start','message':{'usage':{'input_tokens':3,'cache_creation_input_tokens':0,'cache_read_input_tokens':0}}})
        self.assertEqual(o.snapshot()['input_count'],3)
        self.assertNotIn('output_count',o.snapshot());self.assertNotIn('reasoning_count',o.snapshot())
