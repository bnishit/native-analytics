#!/usr/bin/env python3
"""Bounded local HTTP benchmark with synthetic data and an isolated Afterspan process."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import uuid

REPO=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(REPO/'tests'))
from test_recovery_processes import RecoveryProcessTests

def reference(kind,index):
    return str(uuid.UUID(bytes=hashlib.sha256(f'afterspan-benchmark-v1:{kind}:{index}'.encode()).digest()[:16],version=4))

def events(count,tasks,now):
    timestamp=datetime.fromtimestamp(now-600,timezone.utc).isoformat()
    for index in range(count):
        yield {'id':f'benchmark-event-{index}','name':'benchmark.completed','distinct_id':f'synthetic:{index%100}',
            'timestamp':timestamp,'properties':{'traffic_kind':'synthetic','duration_ms':index%1000,'variant':index%3}}
    for index in range(tasks):
        facts={'schema_version':'workflow-trace-v1','trace_id':reference('trace',index),'attempt_id':reference('attempt',index),'span_id':reference('span',index),
            'attempt_number':1,'workflow':'benchmark_probe','environment':'test','traffic_kind':'synthetic','request_started_at_ms':int((now-600)*1000),'step':'processing'}
        for state,offset in [('started',0),('succeeded',.025)]:
            yield {'id':f'benchmark-trace-{index}-{state}','name':'trace.observed','timestamp':datetime.fromtimestamp(now-600+offset,timezone.utc).isoformat(),
                'properties':{'afterspan_trace':{**facts,'state':state,**({'duration_ms':25,'milestone':'completion'} if offset else {})}}}

def percentiles(values):
    ordered=sorted(values)
    return {'requests':len(values),'p50_ms':round(statistics.median(values),2),'p95_ms':round(ordered[max(0,math.ceil(len(values)*.95)-1)],2),'max_ms':round(max(values),2)}

def memory_bytes():
    try:return os.sysconf('SC_PAGE_SIZE')*os.sysconf('SC_PHYS_PAGES')
    except (ValueError,OSError,AttributeError):
        if sys.platform=='darwin':
            try:return int(subprocess.check_output(['sysctl','-n','hw.memsize'],text=True).strip())
            except (OSError,ValueError,subprocess.CalledProcessError):pass
    return None

def run(count=20000,tasks=1000,concurrency=4,queries=10):
    if not 100<=count<=40000 or not 1<=tasks<=3000 or not 1<=concurrency<=16 or not 2<=queries<=30:raise ValueError('benchmark bounds: 100–40000 events, 1–3000 tasks, 1–16 writers, 2–30 query repetitions')
    harness=RecoveryProcessTests();harness.setUp()
    try:
        project=harness.request('/api/projects',{'id':'benchmark-pilot','name':'Synthetic capacity pilot'},harness.admin)
        writer='Bearer '+project['write_key']
        read='Bearer '+harness.request('/api/projects/benchmark-pilot/read-keys',{'label':'Synthetic aggregate reader','expires_days':1},harness.admin)['read_key']
        audit='Bearer '+harness.request('/api/projects/benchmark-pilot/read-keys',{'label':'Synthetic diagnostic reader','expires_days':1,'purpose':'audit'},harness.admin)['read_key']
        goal=reference('goal',0)
        harness.request('/api/outcomes/goals/'+goal+'?project=benchmark-pilot&revision=0',{'title':'Synthetic benchmark criteria','workflow':'benchmark_probe','environment':'test','traffic_kind':'synthetic','deadline_seconds':60,'checks':[{'name':'benchmark_criterion','version':1,'method':'code','phase':'before_delivery'}],'milestones':['completion']},harness.admin,'PUT')
        dataset=list(events(count,tasks,time.time()));batches=[dataset[i:i+100] for i in range(0,len(dataset),100)]
        def ingest(batch):
            started=time.perf_counter();receipt=harness.request('/v1/events',{'events':batch},writer)
            if receipt.get('accepted')!=len(batch) or receipt.get('duplicates')!=0:raise ValueError('ingestion did not accept the exact synthetic batch')
            return (time.perf_counter()-started)*1000
        started=time.perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:latency=list(pool.map(ingest,batches))
        elapsed=time.perf_counter()-started
        def timed(path,query,auth,verify):
            values=[]
            for _ in range(queries):
                at=time.perf_counter();response=harness.request(path,query,auth);values.append((time.perf_counter()-at)*1000);verify(response)
            return percentiles(values)
        def exact(value,expected):
            if value!=expected:raise ValueError('synthetic readback count did not match the seeded dataset')
        expected_p95=sorted(i%1000 for i in range(count))[math.ceil(count*.95)-1]
        def verify_aggregate(r):exact(r['matched_events'],count);exact(r['total'],expected_p95)
        query_metrics=timed('/api/query?project=benchmark-pilot',{'event':'benchmark.completed','metric':'p95','measure':'duration_ms','breakdown':'variant'},read,verify_aggregate)
        trace_metrics=timed('/api/traces/search?project=benchmark-pilot',{'environment':'test','traffic_kind':'synthetic'},audit,lambda r:exact(r['total'],tasks))
        outcome_metrics=timed('/api/outcomes/evaluate?project=benchmark-pilot',{'goal_id':goal,'version':1},audit,lambda r:exact(r['counts']['unclassified'],tasks))
        mixed_batches=[[{'id':f'benchmark-mixed-{i+j}','name':'benchmark.concurrent','properties':{'traffic_kind':'synthetic'}} for j in range(100)] for i in range(0,10000,100)]
        writer_started=threading.Event()
        def mixed_writer():
            writer_started.set();started=time.perf_counter();values=[]
            for batch in mixed_batches:values.append(ingest(batch));time.sleep(.05)
            return {'elapsed_seconds':round(time.perf_counter()-started,3),**percentiles(values)}
        def mixed_reader(path,query,auth,verify):
            writer_started.wait(timeout=5);return timed(path,query,auth,verify)
        with ThreadPoolExecutor(max_workers=4) as pool:
            writing=pool.submit(mixed_writer)
            reads={
                'aggregate_p95_breakdown':pool.submit(mixed_reader,'/api/query?project=benchmark-pilot',{'event':'benchmark.completed','metric':'p95','measure':'duration_ms','breakdown':'variant'},read,verify_aggregate),
                'trace_search':pool.submit(mixed_reader,'/api/traces/search?project=benchmark-pilot',{'environment':'test','traffic_kind':'synthetic'},audit,lambda r:exact(r['total'],tasks)),
                'outcome_evaluation':pool.submit(mixed_reader,'/api/outcomes/evaluate?project=benchmark-pilot',{'goal_id':goal,'version':1},audit,lambda r:exact(r['counts']['unclassified'],tasks))}
            mixed_queries={name:future.result() for name,future in reads.items()};mixed_ingestion=writing.result()
        try:resident_bytes=int(subprocess.check_output(['ps','-o','rss=','-p',str(harness.process.pid)],text=True).strip())*1024
        except (OSError,ValueError,subprocess.CalledProcessError):resident_bytes=None
        with sqlite3.connect(harness.database) as c:
            exact(c.execute('SELECT count(*) FROM events').fetchone()[0],count+2*tasks+10000)
            storage_bytes=c.execute('PRAGMA page_count').fetchone()[0]*c.execute('PRAGMA page_size').fetchone()[0]
        database_files_bytes=sum(path.stat().st_size for path in [harness.database,Path(str(harness.database)+'-wal'),Path(str(harness.database)+'-shm')] if path.exists())
        return {'benchmark_version':2,'environment':'isolated-local-loopback','synthetic':True,'passed':True,
            'recorded_at':datetime.now(timezone.utc).isoformat(),'runtime':{'python':platform.python_version(),'sqlite':sqlite3.sqlite_version,'os':platform.system(),'architecture':platform.machine(),'logical_cpus':os.cpu_count(),'physical_memory_bytes':memory_bytes(),'service_processes':1,'database':'SQLite WAL on local temporary filesystem'},
            'dataset':{'product_events':count,'workflow_tasks':tasks,'observations':len(dataset)+10000,'seed_observations':len(dataset),'mixed_product_events':10000,'identities':100,'native_batch_size':100,'ingestion_concurrency':concurrency},
            'ingestion':{'observations':len(dataset),'elapsed_seconds':round(elapsed,3),'observations_per_second':round(len(dataset)/elapsed,1),**percentiles(latency)},
            'queries':{'aggregate_p95_breakdown':query_metrics,'trace_search':trace_metrics,'outcome_evaluation':outcome_metrics},'mixed_load':{'writers':1,'readers':3,'write_batch_pause_ms':50,'write_observations':10000,'ingestion':mixed_ingestion,'queries':mixed_queries},'database_logical_bytes':storage_bytes,'database_files_bytes_after_queries':database_files_bytes,'service_resident_bytes_after_queries':resident_bytes,
            'limits':['One finite local HTTP load on this machine; not a sustained-load or production capacity/SLA claim.',
                'Seed-query measurements run after ingestion. Mixed-query workers start with one paced writer; some reads may outlast it. Each request uses a fresh HTTP connection.',
                'Native JSON ingestion only; this does not measure OTLP, debug capture, provider calls, cloud disk/network or adversarial traffic.',
                'Synthetic tasks have no answer-quality evidence and correctly remain unclassified. No useful customer outcome is manufactured.',
                'Uses the same rate limits and guards. No real credentials, workspaces or external services are accessed.']}
    finally:harness.tearDown()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name,default in [('events',20000),('tasks',1000),('concurrency',4),('queries',10)]:parser.add_argument('--'+name,type=int,default=default)
    parser.add_argument('--output',required=True);args=parser.parse_args();destination=Path(args.output)
    if destination.exists():parser.error('output exists; choose a new report path')
    result=run(args.events,args.tasks,args.concurrency,args.queries)
    destination.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'passed':result['passed'],'observations':result['dataset']['observations'],'ingestion':result['ingestion'],'queries':result['queries']}))

if __name__=='__main__':main()
