"""Real isolated processes: crash/outage/ambiguous receipt never replay business work."""
from contextlib import closing
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid

WORKER='''import os,json,sqlite3
from native_analytics import Collector
c=Collector(os.environ['PROBE_URL'],os.environ['PROBE_WRITE_KEY'],os.environ['PROBE_OUTBOX'])
with sqlite3.connect(os.environ['PROBE_BUSINESS_DB']) as db:
 db.execute('CREATE TABLE IF NOT EXISTS fulfilled(id TEXT PRIMARY KEY)')
 if os.environ['PROBE_PHASE']=='enqueue':
  db.execute("INSERT INTO fulfilled VALUES('one-synthetic-business-action')")
  c.track('recovery.completed','synthetic:recovery',{'traffic_kind':'synthetic'})
c.flush()
with sqlite3.connect(os.environ['PROBE_BUSINESS_DB']) as db:business=db.execute('SELECT count(*) FROM fulfilled').fetchone()[0]
print(json.dumps({'business':business,**c.status()}),flush=True)
os._exit(17)
'''

class RecoveryProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.repo=Path(__file__).parents[1];self.process=None;self.proxy=None;self.proxy_thread=None
        with closing(socket.socket()) as s:s.bind(('127.0.0.1',0));self.port=s.getsockname()[1]
        self.origin='http://127.0.0.1:'+str(self.port);self.database=self.root/'analytics.db';self.log=(self.root/'service.log').open('ab')
        self.env={'PATH':os.environ.get('PATH',''),'PYTHONPATH':str(self.repo),'ANALYTICS_DB':str(self.database),
            'ANALYTICS_ADMIN_TOKEN':'na_admin_'+'a'*40,'ANALYTICS_PASSWORD':'p'+'b'*40,'ANALYTICS_SESSION_SECRET':'s'*40,
            'COOKIE_SECURE':'false','AFTERSPAN_LEGACY_LOGIN':'true','ANALYTICS_PUBLIC_URL':self.origin}
        self.admin='Bearer '+self.env['ANALYTICS_ADMIN_TOKEN']
        try:self.start()
        except BaseException:self.tearDown();raise
    def tearDown(self):
        self.stop()
        if self.proxy:self.proxy.shutdown();self.proxy.server_close()
        if self.proxy_thread:self.proxy_thread.join(timeout=2)
        self.log.close();self.tmp.cleanup()
    def start(self):
        self.process=subprocess.Popen([sys.executable,'-m','uvicorn','app:app','--host','127.0.0.1','--port',str(self.port),'--no-access-log'],cwd=self.repo,env=self.env,stdout=self.log,stderr=self.log)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if self.process.poll() is not None:self.fail('isolated analytics process exited before readiness')
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(self.origin+'/healthz',timeout=.3) as response:
                    if response.status==200:return
            except (OSError,urllib.error.URLError):pass
            time.sleep(.05)
        self.fail('isolated analytics process did not become ready')
    def stop(self):
        if self.process and self.process.poll() is None:self.process.kill();self.process.wait(timeout=10)
    def request(self,path,body=None,authorization=None,method=None):
        req=urllib.request.Request(self.origin+path,data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type':'application/json',**({'Authorization':authorization} if authorization else {})},method=method)
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=5) as response:return json.load(response)
    def worker(self,phase,url,key):
        env={**self.env,'PYTHONPATH':str(self.repo/'sdk/python'),'PROBE_URL':url,'PROBE_WRITE_KEY':key,
             'PROBE_OUTBOX':str(self.root/'outbox.db'),'PROBE_BUSINESS_DB':str(self.root/'business.db'),'PROBE_PHASE':phase}
        result=subprocess.run([sys.executable,'-c',WORKER],cwd=self.repo,env=env,capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,17,'synthetic worker did not crash at the intended checkpoint')
        return json.loads(result.stdout)
    def start_ambiguous_proxy(self):
        origin=self.origin
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=self.rfile.read(int(self.headers['Content-Length']))
                req=urllib.request.Request(origin+'/v1/events',data=body,headers={'Content-Type':'application/json','Authorization':self.headers.get('Authorization','')})
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=5) as response:receipt=response.read()
                if self.server.cut_once:
                    self.server.cut_once=False;self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(receipt)
        self.proxy=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.proxy.cut_once=True
        self.proxy_thread=threading.Thread(target=self.proxy.serve_forever,daemon=True);self.proxy_thread.start()
        return 'http://127.0.0.1:'+str(self.proxy.server_address[1])
    def test_crashes_retain_data_and_ambiguous_delivery_deduplicates_without_business_replay(self):
        project=self.request('/api/projects',{'id':'recovery-pilot','name':'Synthetic recovery pilot'},self.admin)
        key=project['write_key'];writer='Bearer '+key
        read=self.request('/api/projects/recovery-pilot/read-keys',{'label':'Synthetic recovery reader','expires_days':1},self.admin)['read_key']
        audit=self.request('/api/projects/recovery-pilot/read-keys',{'label':'Synthetic audit reader','expires_days':1,'purpose':'audit'},self.admin)['read_key']
        chart=self.request('/api/charts?project=recovery-pilot',{'title':'Synthetic recovered tasks','query':{'event':'recovery.completed'}},self.admin)
        self.request('/v1/identify',{'distinct_id':'synthetic:recovery','attributes':{'tier':'synthetic'}},writer)
        goal=str(uuid.uuid4())
        self.request('/api/outcomes/goals/'+goal+'?project=recovery-pilot&revision=0',{'title':'Synthetic checked response','workflow':'recovery_probe','environment':'test','deadline_seconds':60,'checks':[{'name':'source_facts','version':1,'method':'code','phase':'before_delivery'}],'milestones':['delivery']},self.admin,'PUT')
        self.stop()  # Actual analytics process kill, not a mocked network response.
        offline=self.worker('enqueue',self.origin,key)
        self.assertEqual(offline['business'],1);self.assertEqual(offline['queued'],1);self.assertEqual(offline['delivery_failures'],1)
        with closing(sqlite3.connect(self.database)) as c:self.assertEqual(c.execute("SELECT count(*) FROM events WHERE name='recovery.completed'").fetchone()[0],0)
        self.start();proxy=self.start_ambiguous_proxy()
        ambiguous=self.worker('flush',proxy,key)  # Server commits; proxy discards its reply.
        self.assertEqual(ambiguous['business'],1);self.assertEqual(ambiguous['queued'],1);self.assertEqual(ambiguous['delivery_failures'],2)
        with closing(sqlite3.connect(self.database)) as c:self.assertEqual(c.execute("SELECT count(*) FROM events WHERE name='recovery.completed'").fetchone()[0],1)
        self.stop();self.start()  # Kill between accepted delivery and duplicate acknowledgement.
        recovered=self.worker('flush',proxy,key)
        self.assertEqual(recovered['business'],1);self.assertEqual(recovered['queued'],0);self.assertIsNone(recovered['last_error'])
        reader='Bearer '+read
        result=self.request('/api/query?project=recovery-pilot',{'event':'recovery.completed'},reader);self.assertEqual(result['total'],1)
        charts=self.request('/api/charts?project=recovery-pilot',authorization=reader);self.assertEqual([c['id'] for c in charts['charts']],[chart['id']])
        goals=self.request('/api/outcomes/goals?project=recovery-pilot',authorization='Bearer '+audit);self.assertEqual(goals['goals'][0]['id'],goal)
        with closing(sqlite3.connect(self.database)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE name='recovery.completed'").fetchone()[0],1)
            self.assertEqual(json.loads(c.execute("SELECT attributes FROM people WHERE project='recovery-pilot'").fetchone()[0])['tier'],'synthetic')
            self.assertGreaterEqual(c.execute("SELECT sum(accepted_requests) FROM collection_ingestion WHERE project='recovery-pilot'").fetchone()[0],2)
        with self.assertRaises(urllib.error.HTTPError) as denied:self.request('/api/projects')
        self.assertEqual(denied.exception.code,401);denied.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as denied:self.request('/api/query?project=other-project',{},reader)
        self.assertEqual(denied.exception.code,403);denied.exception.close()
