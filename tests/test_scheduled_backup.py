import base64
from contextlib import closing
import importlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


class Store:
    def __init__(self):self.objects={};self.uploads=[];self.deleted=[];self.corrupt=False;self.uncertain=False
    def upload_file(self,path,bucket,key,**kwargs):
        self.uploads.append(key);self.objects[key]=Path(path).read_bytes()
        if self.uncertain:raise OSError('synthetic private endpoint credential')
    def head_object(self,*,Bucket,Key):
        from botocore.exceptions import ClientError
        if Key not in self.objects:raise ClientError({'Error':{'Code':'NoSuchKey'},'ResponseMetadata':{'HTTPStatusCode':404}},'HeadObject')
        return {'ContentLength':len(self.objects[Key])}
    def download_file(self,bucket,key,path,**kwargs):Path(path).write_bytes(self.objects[key]+(b'corrupt' if self.corrupt else b''))
    def delete_object(self,*,Bucket,Key):self.deleted.append(Key);self.objects.pop(Key,None)


class ScheduledBackupTests(unittest.TestCase):
    def setUp(self):
        self.jobs=importlib.import_module('scheduled_backup')
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.db=self.root/'analytics.db';self.store=Store()
        self.env=patch.dict(os.environ,{'AFTERSPAN_BACKUP_ENABLED':'true','AFTERSPAN_BACKUP_BUCKET':'synthetic-bucket','AFTERSPAN_BACKUP_ENDPOINT':'https://s3.example.test','AFTERSPAN_BACKUP_REGION':'auto','AFTERSPAN_BACKUP_PREFIX':'deployment-one/','AFTERSPAN_BACKUP_ACCESS_KEY_ID':'synthetic','AFTERSPAN_BACKUP_SECRET_ACCESS_KEY':'synthetic-private','AFTERSPAN_BACKUP_KEY':base64.urlsafe_b64encode(b'b'*32).decode(),'AFTERSPAN_BACKUP_RETAIN':'2'},clear=False);self.env.start()
        self.client=patch.object(self.jobs,'client',return_value=self.store);self.client.start()
        with closing(sqlite3.connect(self.db)) as c,c:c.executescript('PRAGMA journal_mode=WAL;CREATE TABLE projects(id TEXT PRIMARY KEY);CREATE TABLE events(id TEXT,project TEXT REFERENCES projects(id));CREATE TABLE charts(id TEXT);INSERT INTO projects VALUES("one");')
    def tearDown(self):self.client.stop();self.env.stop();self.tmp.cleanup()
    def test_default_off_and_invalid_config_never_contact_storage(self):
        with patch.dict(os.environ,{'AFTERSPAN_BACKUP_ENABLED':''}):self.assertEqual(self.jobs.run_due(self.db)['status'],'disabled')
        for change in [{'AFTERSPAN_BACKUP_KEY':''},{'AFTERSPAN_BACKUP_ENDPOINT':'http://metadata.example'},{'AFTERSPAN_BACKUP_PREFIX':''},{'AFTERSPAN_BACKUP_PREFIX':'../'},{'AFTERSPAN_BACKUP_RETAIN':'1'}]:
            with patch.dict(os.environ,change):self.assertEqual(self.jobs.run_due(self.db)['reason'],'configuration_invalid')
        self.assertFalse(self.store.uploads)
    def test_actual_wal_snapshot_readback_restore_and_due_clock_survive_restart(self):
        with closing(sqlite3.connect(self.db)) as c:
            c.execute('INSERT INTO events VALUES("wal","one")');c.commit()
            first=self.jobs.run_due(self.db,now=100000);self.assertEqual(first['status'],'succeeded')
        self.assertEqual(len(self.store.objects),1);payload=next(iter(self.store.objects.values()));self.assertNotIn(b'CREATE TABLE',payload)
        self.assertEqual(self.jobs.run_due(self.db,now=100060)['last_success'],100000);self.assertEqual(len(self.store.uploads),1)
        state=json.loads(self.jobs.state_path(self.db).read_text());self.assertEqual(state['history'][0]['verified']['sqlite_checked'],True)
        archive=self.root/'copy.asb';archive.write_bytes(payload);dest=self.root/'restore.db'
        self.jobs.restore(archive,dest)
        with closing(sqlite3.connect(dest)) as c:self.assertEqual(c.execute('SELECT id FROM events').fetchone()[0],'wal')
        self.assertEqual(self.jobs.state_path(self.db).stat().st_mode&0o777,0o600)
    def test_corrupt_readback_keeps_pending_bytes_and_never_reports_success_or_prunes(self):
        self.jobs.run_due(self.db,now=100000);self.jobs.run_due(self.db,now=200000)
        self.store.corrupt=True;failed=self.jobs.run_due(self.db,now=300000)
        self.assertEqual(failed['status'],'failed');self.assertEqual(failed['last_success'],200000);self.assertFalse(self.store.deleted)
        state=json.loads(self.jobs.state_path(self.db).read_text());pending=state['pending'];original=self.jobs.pending_path(self.db).read_bytes()
        self.assertEqual(self.jobs.run_due(self.db,now=300001)['status'],'failed');self.assertEqual(len(self.store.uploads),3)
        self.store.corrupt=False;fixed=self.jobs.run_due(self.db,now=301000);self.assertEqual(fixed['status'],'succeeded')
        self.assertEqual(len(self.store.uploads),3);self.assertEqual(self.store.objects[pending['key']],original)
        self.assertEqual(len(self.store.deleted),1);self.assertFalse(self.jobs.pending_path(self.db).exists())
    def test_uncertain_upload_reconciles_exact_object_without_repeating_capture(self):
        self.store.uncertain=True
        result=self.jobs.run_due(self.db,now=100000);self.assertEqual(result['status'],'succeeded');self.assertEqual(len(self.store.uploads),1)
    def test_authenticated_restore_failure_never_marks_a_matching_hash_as_recoverable(self):
        original=self.jobs.restore
        def restore(archive,destination):
            if Path(destination).name=='restored.db':raise ValueError('synthetic private failure')
            return original(archive,destination)
        with patch.object(self.jobs,'restore',side_effect=restore):
            with self.assertLogs(self.jobs.__name__,level='ERROR') as logged:result=self.jobs.run_due(self.db,now=100000)
        self.assertEqual(result['status'],'failed');self.assertNotIn('last_success',result);self.assertNotIn('synthetic private failure',''.join(logged.output));self.assertFalse(self.store.deleted)
    def test_retention_failure_retries_cleanup_without_creating_another_snapshot(self):
        self.jobs.run_due(self.db,now=100000);self.jobs.run_due(self.db,now=200000)
        with patch.object(self.store,'delete_object',side_effect=OSError('unavailable')):result=self.jobs.run_due(self.db,now=300000)
        self.assertEqual(result['status'],'failed');self.assertEqual(result['last_success'],300000);self.assertEqual(len(self.store.uploads),3)
        self.assertEqual(self.jobs.run_due(self.db,now=301000)['status'],'succeeded');self.assertEqual(len(self.store.uploads),3);self.assertEqual(len(self.store.deleted),1)
    def test_restart_after_interrupted_upload_keeps_the_exact_pending_archive(self):
        self.store.corrupt=True;self.jobs.run_due(self.db,now=100000)
        state=json.loads(self.jobs.state_path(self.db).read_text());state['status']='running';self.jobs.save(self.db,state);key=state['pending']['key']
        self.store.corrupt=False;self.assertEqual(self.jobs.run_due(self.db,now=101000)['status'],'succeeded');self.assertEqual(self.store.uploads,[key])
    def test_readback_timeout_never_restarts_an_accepted_upload(self):
        with patch.object(self.store,'head_object',side_effect=TimeoutError('observation expired')):
            self.jobs.run_due(self.db,now=100000);self.jobs.run_due(self.db,now=101000)
        self.assertEqual(len(self.store.uploads),1)
        self.assertEqual(self.jobs.run_due(self.db,now=102000)['status'],'succeeded');self.assertEqual(len(self.store.uploads),1)
    def test_definitely_missing_upload_retries_original_bytes_not_a_new_database_snapshot(self):
        with patch.object(self.store,'upload_file',side_effect=OSError('not sent')):self.jobs.run_due(self.db,now=100000)
        original=self.jobs.pending_path(self.db).read_bytes()
        with closing(sqlite3.connect(self.db)) as c,c:c.execute('INSERT INTO events VALUES("after-capture","one")')
        self.assertEqual(self.jobs.run_due(self.db,now=101000)['status'],'succeeded');self.assertEqual(next(iter(self.store.objects.values())),original)
    def test_corrupted_state_and_clock_rollback_do_not_start_storage_work(self):
        self.jobs.state_path(self.db).write_text('{broken');self.jobs.state_path(self.db).chmod(0o600)
        self.assertEqual(self.jobs.run_due(self.db,now=100000)['reason'],'state_invalid');self.assertFalse(self.store.uploads)
        self.jobs.state_path(self.db).unlink();self.jobs.run_due(self.db,now=100000)
        self.assertEqual(self.jobs.run_due(self.db,now=90000)['reason'],'clock_invalid');self.assertTrue(self.jobs.status(self.db,now=90000)['overdue'])
    def test_worker_is_default_off_and_an_enabled_worker_stops_cleanly(self):
        import threading
        stop=threading.Event();called=threading.Event()
        with patch.dict(os.environ,{'AFTERSPAN_BACKUP_ENABLED':''}):self.assertIsNone(self.jobs.start(self.db,stop))
        with patch.object(self.jobs,'run_due',side_effect=lambda _:called.set()):
            worker=self.jobs.start(self.db,stop);self.assertTrue(called.wait(1));stop.set();worker.join(1);self.assertFalse(worker.is_alive())
    def test_pending_scope_tamper_cannot_upload_to_an_unrelated_key(self):
        self.store.corrupt=True;self.jobs.run_due(self.db,now=100000)
        state=json.loads(self.jobs.state_path(self.db).read_text());state['pending']['key']='unrelated/overwrite';self.jobs.save(self.db,state)
        self.assertEqual(self.jobs.run_due(self.db,now=101000)['reason'],'pending_invalid');self.assertEqual(len(self.store.uploads),1)
    def test_real_s3_sdk_configuration_is_bounded_and_uses_explicit_credentials(self):
        from botocore.stub import Stubber
        self.client.stop()
        try:
            store=self.jobs.client(self.jobs.settings())
            self.assertEqual(store.meta.config.signature_version,'s3v4');self.assertEqual(store.meta.config.s3['addressing_style'],'virtual')
            self.assertEqual(store.meta.config.retries['total_max_attempts'],2);self.assertEqual(store.meta.config.read_timeout,15)
            config=self.jobs.transfer_config();self.assertFalse(config.use_threads);self.assertEqual(config.max_concurrency,1)
            with Stubber(store) as stub:
                stub.add_response('head_object',{'ContentLength':123},{'Bucket':'synthetic-bucket','Key':'deployment-one/probe'})
                self.assertEqual(store.head_object(Bucket='synthetic-bucket',Key='deployment-one/probe')['ContentLength'],123)
        finally:self.client.start()
    def test_retention_only_touches_known_verified_keys_and_configuration_change_fails_closed(self):
        self.store.objects['unrelated/keep']=b'untouched'
        for stamp in [100000,200000,300000]:self.jobs.run_due(self.db,now=stamp)
        self.assertEqual(len(self.store.deleted),1);self.assertIn('unrelated/keep',self.store.objects)
        with patch.dict(os.environ,{'AFTERSPAN_BACKUP_PREFIX':'different/'}):self.assertEqual(self.jobs.run_due(self.db,now=400000)['reason'],'configuration_changed')
        self.assertEqual(len(self.store.deleted),1)
    def test_cross_process_lock_and_public_status_hide_credentials(self):
        import fcntl
        with self.jobs.lock_path(self.db).open('w') as f:
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);self.assertEqual(self.jobs.run_due(self.db,now=100000)['status'],'busy')
        self.jobs.run_due(self.db,now=100000)
        text=json.dumps(self.jobs.status(self.db,now=200000));self.assertNotIn('synthetic-private',text);self.assertNotIn('s3.example',text);self.assertNotIn('deployment-one',text)
        self.assertTrue(self.jobs.status(self.db,now=200000)['overdue'])
