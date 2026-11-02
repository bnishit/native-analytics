"""Exact provider amounts cannot turn a tiny positive charge into free usage."""
import unittest
from pydantic import ValidationError
from tracing import ModelUsage
from outcomes import model_cost_summary
import test_tracing

class ModelCostTests(unittest.TestCase):
    def test_exact_decimal_is_canonical_bounded_and_has_one_provenance(self):
        def usage(**values):return ModelUsage(provider='example',model='model-v1',**values)
        self.assertEqual(usage(cost_usd_decimal='0.000000000001',cost_source='provider_reported').cost_usd_decimal,'0.000000000001')
        self.assertEqual(usage(cost_usd_decimal='0.010000000000',cost_source='provider_reported').cost_usd_decimal,'0.01')
        self.assertEqual(usage(cost_usd_decimal='0',cost_source='provider_reported').cost_usd_decimal,'0')
        for fields in [{'cost_usd_decimal':'0.0000000000001','cost_source':'provider_reported'},
            {'cost_usd_decimal':'-0.1','cost_source':'provider_reported'},
            {'cost_usd_decimal':'1e-6','cost_source':'provider_reported'},
            {'cost_usd_decimal':True,'cost_source':'provider_reported'},
            {'cost_usd_decimal':'1000000.01','cost_source':'provider_reported'},
            {'cost_usd_decimal':'0.1'},
            {'cost_usd_decimal':'0.1','cost_microusd':100000,'cost_source':'provider_reported'},
            {'cost_usd_decimal':'0.1','cost_source':'price_table'}]:
            with self.assertRaises(ValidationError,msg=str(fields)):usage(**fields)
    def test_mixed_legacy_and_tiny_charges_sum_exactly_without_double_counting(self):
        def span(**amount):return {'step':'model','model_usage':{'provider':'example','model':'model-v1','cost_source':'provider_reported',**amount}}
        r=model_cost_summary([{'spans':[span(cost_microusd=1),span(cost_usd_decimal='0.000000000001')]}],3,True)
        self.assertEqual(r['measured_usd'],'0.000001000001');self.assertIsNone(r['measured_microusd'])
        self.assertEqual(r['per_satisfied_task_usd'],'0.000000333333666667');self.assertTrue(r['per_satisfied_task_rounded'])
        self.assertEqual(r['cost_known_spans'],2);self.assertEqual(r['sources'],{'provider_reported':2})
        self.assertIsNone(r['per_satisfied_task_microusd'])
        r=model_cost_summary([{'spans':[span(cost_usd_decimal='0.000000000001'),{'step':'model','model_usage':None}]}],1,True)
        self.assertEqual(r['measured_usd'],'0.000000000001');self.assertIsNone(r['per_satisfied_task_usd']);self.assertEqual(r['cost_unknown_spans'],1)
    def test_positive_ratio_below_display_precision_is_flagged_and_zero_is_distinct(self):
        def result(cost,satisfied):return model_cost_summary([{'spans':[{'step':'model','model_usage':{'cost_usd_decimal':cost,'cost_source':'provider_reported'}}]}],satisfied,True)
        tiny=result('0.000000000001',1000000000);self.assertTrue(tiny['per_satisfied_task_below_display_precision']);self.assertTrue(tiny['per_satisfied_task_rounded'])
        zero=result('0',1);self.assertFalse(zero['per_satisfied_task_below_display_precision']);self.assertFalse(zero['per_satisfied_task_rounded']);self.assertEqual(zero['per_satisfied_task_usd'],'0')
    def test_decimal_charge_roundtrips_native_service_and_safe_trace_projection(self):
        f=test_tracing.TracingTests();f.setUp()
        try:
            usage={'provider':'example','model':'model-v1','cost_usd_decimal':'0.000000037125','cost_source':'provider_reported'}
            response=f.ingest(f.event(step='model'),f.event('succeeded',1,step='model',model_usage=usage));self.assertEqual(response.status_code,200,response.text)
            span=f.review()['traces'][0]['spans'][0]
            self.assertEqual(span['model_usage']['cost_usd_decimal'],'0.000000037125');self.assertIsNone(span['model_usage']['cost_microusd'])
        finally:f.tearDown()
