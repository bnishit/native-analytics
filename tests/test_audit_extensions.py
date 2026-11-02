"""Additive same-stream metadata never turns a pointer into verified source truth."""
import json
import unittest
import uuid
from pydantic import ValidationError
from audit import Observation
import test_audit

class AuditExtensionTests(unittest.TestCase):
    def base(self,stage='quality',**changes):
        return {'schema_version':'slack-request-v1','stage':stage,'environment':'production','request_ref':str(uuid.uuid4()),
                'attempt_ref':str(uuid.uuid4()),'attempt_number':1,'release_ref':str(uuid.uuid4()),'request_started_at_ms':0,
                'traffic_kind':'synthetic',**changes}
    def quality(self,**changes):
        return self.base(check_version='source-facts-v1',checked_claims=1,mismatched_claims=0,unknown_claims=0,
            reported_outcome='succeeded',check_phase='before_reply',answer_disposition='verified',quality_span_ref=str(uuid.uuid4()),**changes)
    def test_not_run_check_is_typed_quality_only_without_claims_or_proof(self):
        facts=self.base(check_version='growthbook-catalog-v1',checked_claims=0,mismatched_claims=0,unknown_claims=0,reported_outcome='unknown',check_execution='not_run',check_reason_code='evidence_binding_rejected')
        parsed=Observation.model_validate(facts);self.assertEqual(parsed.check_execution,'not_run')
        for changes in [{'stage':'tool'},{'checked_claims':1},{'mismatched_claims':1},{'reported_outcome':'succeeded'},{'check_execution':'completed'},{'check_execution':None},{'check_reason_code':'raw message'}, {'check_version':None}, {'quality_span_ref':str(uuid.uuid4()),'check_evidence_digest':'a'*64,'check_evidence_version':'source-check-v1','check_phase':'before_reply','answer_disposition':'withheld'}]:
            with self.subTest(changes=changes),self.assertRaises(ValidationError):Observation.model_validate({**facts,**changes})

    def test_quality_and_reply_operation_refs_are_stage_bound_and_proof_is_complete(self):
        facts=self.quality();Observation.model_validate(facts)  # A real operation may exist without source proof.
        full={**facts,'check_evidence_digest':'a'*64,'check_evidence_version':'source-check-v1'};Observation.model_validate(full)
        for changes in [{'stage':'parser'},{'quality_span_ref':None},{'check_evidence_version':None},{'check_phase':'after_reply'},
                        {'check_evidence_digest':'not a digest'},{'reply_span_ref':str(uuid.uuid4())}]:
            with self.assertRaises(ValidationError):Observation.model_validate({**full,**changes})
        Observation.model_validate(self.base('reply',reply_span_ref=str(uuid.uuid4()),reported_outcome='succeeded'))
        with self.assertRaises(ValidationError):Observation.model_validate(self.base('tool',reply_span_ref=str(uuid.uuid4())))
    def test_model_metadata_is_terminal_model_only_and_preserves_tiny_cost(self):
        facts=self.base('trace',span_ref=str(uuid.uuid4()),trace_step='model',span_state='succeeded',duration_ms=20.0,
                        model_usage={'provider':'openrouter','model':'example/model','cost_usd_decimal':'0.000000037125','cost_source':'provider_reported'})
        parsed=Observation.model_validate(facts);self.assertEqual(parsed.model_usage.cost_usd_decimal,'0.000000037125')
        for change in [{'span_state':'started','duration_ms':None},{'trace_step':'tool'},{'model_usage':{**facts['model_usage'],'first_output_ms':21.0}}]:
            with self.assertRaises(ValidationError):Observation.model_validate({**facts,**change})
    def test_reader_projection_exposes_usage_and_kind_without_raw_binding_refs_or_digests(self):
        f=test_audit.AuditTests();f.setUp()
        try:
            request,attempt=f.chain(comparison_eligible=False,traffic_kind='synthetic',request_started_at_ms=0)
            span=str(uuid.uuid4())
            start=f.trace_facts(request,attempt,span,'model','started',traffic_kind='synthetic')
            end=f.trace_facts(request,attempt,span,'model','succeeded',traffic_kind='synthetic',model_usage={'provider':'example','model':'model-v1','cost_usd_decimal':'0.000000037125','cost_source':'provider_reported'})
            f.observe(start,4);f.observe(end,5)
            quality=self.quality();quality.update(request_ref=request,attempt_ref=attempt,release_ref=f.release,comparison_eligible=False,check_evidence_digest='c'*64,check_evidence_version='source-check-v1')
            f.observe(quality,6)
            r=f.review();text=json.dumps(r)
            for raw in [span,quality['quality_span_ref'],'c'*64]:self.assertNotIn(raw,text)
            row=r['requests'][0];self.assertEqual(row['traffic_kind'],'synthetic');self.assertEqual(row['trace']['spans'][0]['model_usage']['cost_usd_decimal'],'0.000000037125')
            catalog=f.review(path='catalog');self.assertIn('check_evidence_digest',{x['property'] for x in catalog['fields']})
        finally:f.tearDown()
