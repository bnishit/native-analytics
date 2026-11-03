"""Interrupted capacity probes retain receipts without retaining payloads or retrying."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

SOURCE=Path(__file__).resolve().parents[1]/'scripts/benchmark.py'
spec=importlib.util.spec_from_file_location('benchmark',SOURCE)
benchmark=importlib.util.module_from_spec(spec);spec.loader.exec_module(benchmark)


class BenchmarkTests(unittest.TestCase):
    def test_partial_results_survive_an_error_and_new_callbacks_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'progress.jsonl';journal=benchmark.ProgressJournal(path)
            try:
                self.assertEqual(journal.call('aggregate',lambda:{'private':'DO_NOT_LEAK_RESPONSE'}),{'private':'DO_NOT_LEAK_RESPONSE'})
                def fail():raise TimeoutError('DO_NOT_LEAK_EXCEPTION')
                with self.assertRaises(TimeoutError):journal.call('ingestion',fail)
                with self.assertRaisesRegex(RuntimeError,'benchmark stopped'):
                    journal.call('ingestion',lambda:self.fail('must not retry'))
                rows=[json.loads(line) for line in path.read_text().splitlines()]
                self.assertEqual([r['event'] for r in rows],['opened','request_started','response_received','request_started','request_raised'])
                self.assertNotIn('DO_NOT_LEAK',path.read_text())
                self.assertEqual(path.stat().st_mode&0o777,0o600)
                self.assertGreaterEqual(rows[-1]['elapsed_ms'],0)
                with self.assertRaises(FileExistsError):benchmark.ProgressJournal(path)
                self.assertEqual(len(path.read_text().splitlines()),5)
            finally:journal.close()

    def test_overlapping_calls_retain_one_start_and_result_per_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'progress.jsonl';journal=benchmark.ProgressJournal(path)
            try:
                with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(lambda _:journal.call('traces',lambda:None),range(30)))
            finally:journal.close()
            rows=[json.loads(line) for line in path.read_text().splitlines()]
            for number in range(1,31):self.assertEqual([r['event'] for r in rows if r.get('sequence')==number],['request_started','response_received'])

    def test_process_death_preserves_unmatched_start_as_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'progress.jsonl'
            code='import runpy,os,sys; m=runpy.run_path(sys.argv[1]); j=m["ProgressJournal"](sys.argv[2]); j.call("ingestion",lambda:os._exit(17))'
            result=subprocess.run([sys.executable,'-c',code,str(SOURCE),str(path)],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,17)
            rows=[json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([r['event'] for r in rows],['opened','request_started'])
            with self.assertRaises(FileExistsError):benchmark.ProgressJournal(path)

    def test_failed_entrypoint_retains_progress_and_refuses_existing_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'report.json'
            def fail(*args,**kwargs):
                kwargs['journal'].call('aggregate',lambda:None)
                raise ValueError('DO_NOT_LEAK_ERROR')
            with patch.object(sys,'argv',['benchmark','--output',str(path)]),patch.object(benchmark,'run',side_effect=fail):
                with self.assertRaises(ValueError):benchmark.main()
            self.assertEqual(json.loads(path.read_text()),{'state':'incomplete_or_unknown','passed':False,'retry_workload':False})
            before=path.read_bytes();journal=Path(str(path)+'.progress.jsonl');progress=journal.read_bytes()
            self.assertNotIn(b'DO_NOT_LEAK',before+progress)
            with patch.object(sys,'argv',['benchmark','--output',str(path)]),patch.object(benchmark,'run') as run:
                with self.assertRaises(SystemExit):benchmark.main()
                run.assert_not_called()
            self.assertEqual(path.read_bytes(),before);self.assertEqual(journal.read_bytes(),progress)

    def test_receipt_disk_failure_stops_before_network_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            journal=benchmark.ProgressJournal(Path(directory)/'progress.jsonl')
            try:
                with patch.object(benchmark.os,'fsync',side_effect=OSError('DO_NOT_LEAK_DISK')):
                    with self.assertRaises(OSError):journal.call('ingestion',lambda:self.fail('must not send'))
                self.assertTrue(journal.stopped.is_set())
                with self.assertRaises(RuntimeError):journal.call('ingestion',lambda:self.fail('must not send later'))
            finally:journal.close()

    def test_response_receipt_failure_never_repeats_the_completed_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            journal=benchmark.ProgressJournal(Path(directory)/'progress.jsonl');called=[]
            try:
                with patch.object(benchmark.os,'fsync',side_effect=[None,OSError('receipt unavailable')]):
                    with self.assertRaises(OSError):journal.call('ingestion',lambda:called.append('accepted'))
                with self.assertRaises(RuntimeError):journal.call('ingestion',lambda:called.append('replayed'))
                self.assertEqual(called,['accepted'])
            finally:journal.close()

    def test_waiting_start_is_not_dispatched_after_another_call_or_disk_failure(self):
        for disk_failure in [False,True]:
            with self.subTest(disk_failure=disk_failure),tempfile.TemporaryDirectory() as directory:
                journal=benchmark.ProgressJournal(Path(directory)/'progress.jsonl')
                first_started=threading.Event();second_waiting=threading.Event();release=threading.Event();sent=[];first_id=[]
                record=journal.record;sync=os.fsync
                def held_record(**fields):
                    if fields.get('sequence')==2 and fields['event']=='request_started':
                        second_waiting.set()
                        if not release.wait(5):raise TimeoutError('test barrier')
                    record(**fields)
                def fail_sync(fd):
                    if disk_failure and first_id==[threading.get_ident()] and second_waiting.is_set():raise OSError('receipt failed')
                    return sync(fd)
                def first():
                    first_id.append(threading.get_ident());first_started.set()
                    if not second_waiting.wait(5):raise TimeoutError('test barrier')
                    if not disk_failure:raise TimeoutError('request failed')
                try:
                    with patch.object(journal,'record',side_effect=held_record),patch.object(benchmark.os,'fsync',side_effect=fail_sync),ThreadPoolExecutor(max_workers=2) as pool:
                        a=pool.submit(journal.call,'ingestion',first)
                        self.assertTrue(first_started.wait(5))
                        b=pool.submit(journal.call,'ingestion',lambda:sent.append('sent_after_stop'))
                        try:
                            with self.assertRaises(OSError if disk_failure else TimeoutError):a.result(timeout=5)
                        finally:release.set()
                        with self.assertRaises(RuntimeError):b.result(timeout=5)
                    self.assertEqual(sent,[])
                finally:release.set();journal.close()
