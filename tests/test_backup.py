"""Operator snapshots remain isolated; customers receive no backup/export endpoint."""
import base64
from contextlib import closing
import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('operator_backup',Path(__file__).parents[1]/'scripts/backup.py')
backup=importlib.util.module_from_spec(spec);spec.loader.exec_module(backup)

class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.db=self.root/'source.db';self.archive=self.root/'snapshot.asb';self.restored=self.root/'restored.db'
        self.env=patch.dict(os.environ,{'AFTERSPAN_BACKUP_KEY':base64.b64encode(b'b'*32).decode()});self.env.start()
        with closing(sqlite3.connect(self.db)) as c,c:
            c.executescript('''PRAGMA journal_mode=WAL;CREATE TABLE projects(id TEXT PRIMARY KEY);CREATE TABLE events(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),value TEXT);CREATE TABLE charts(id TEXT);CREATE TABLE regression_objects(kind TEXT,definition TEXT);CREATE TABLE debug_snapshots(content BLOB);''')
            c.execute("INSERT INTO projects VALUES('alpha')");c.execute("INSERT INTO events VALUES('one','alpha','private synthetic marker')");c.execute("INSERT INTO charts VALUES('chart')")
            c.execute("INSERT INTO regression_objects VALUES('dataset','pinned reviewed cases')");c.execute("INSERT INTO debug_snapshots VALUES(?)",(b'encrypted selected snapshot',))
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def test_committed_wal_and_all_tables_restore_then_accept_new_ingestion(self):
        # Keep the live writer open so the newest committed record stays in WAL.
        with closing(sqlite3.connect(self.db)) as live:
            live.execute("INSERT INTO events VALUES('wal','alpha','committed')");live.commit()
            receipt=backup.backup(self.db,self.archive);self.assertEqual(receipt['scope'],'operator-entire-database');self.assertTrue(receipt['encrypted'])
            self.assertNotIn(b'private synthetic marker',self.archive.read_bytes());self.assertEqual(self.archive.stat().st_mode&0o777,0o600)
            backup.restore(self.archive,self.restored)
        with closing(sqlite3.connect(self.restored)) as c,c:
            self.assertEqual(c.execute('SELECT count(*) FROM events').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT definition FROM regression_objects').fetchone()[0],'pinned reviewed cases')
            self.assertEqual(c.execute('SELECT content FROM debug_snapshots').fetchone()[0],b'encrypted selected snapshot')
            self.assertEqual(c.execute('SELECT id FROM charts').fetchone()[0],'chart')
            c.execute("INSERT INTO events VALUES('new','alpha','after restore')")
            self.assertEqual(c.execute('SELECT count(*) FROM events').fetchone()[0],3)
        with closing(sqlite3.connect(self.db)) as c:self.assertEqual(c.execute('SELECT count(*) FROM events').fetchone()[0],2)
    def test_wrong_key_tamper_truncation_and_extra_data_fail_closed(self):
        backup.backup(self.db,self.archive);original=self.archive.read_bytes()
        with patch.dict(os.environ,{'AFTERSPAN_BACKUP_KEY':base64.b64encode(b'c'*32).decode()}):
            with self.assertRaises(ValueError):backup.restore(self.archive,self.restored)
        for data in [original[:-1],original+b'extra',original[:100]+bytes([original[100]^1])+original[101:]]:
            self.archive.write_bytes(data)
            with self.assertRaises(ValueError):backup.restore(self.archive,self.restored)
            self.assertFalse(self.restored.exists())
    def test_no_key_no_overwrite_no_broad_restore_directory(self):
        with patch.dict(os.environ,{'AFTERSPAN_BACKUP_KEY':''}):
            with self.assertRaises(ValueError):backup.backup(self.db,self.archive)
        backup.backup(self.db,self.archive)
        with self.assertRaises(FileExistsError):backup.backup(self.db,self.archive)
        self.assertTrue(self.archive.exists())
        with self.assertRaises(ValueError):backup.restore(self.archive,self.db)
        broad=self.root/'broad';broad.mkdir(mode=0o755)
        with self.assertRaises(ValueError):backup.restore(self.archive,broad/'restored.db')
        self.assertFalse((broad/'restored.db').exists())

    def test_reordered_encrypted_chunks_fail_without_retaining_plaintext(self):
        with closing(sqlite3.connect(self.db)) as c,c:
            c.execute('CREATE TABLE padding(value BLOB)');c.execute('INSERT INTO padding VALUES(zeroblob(?))',(backup.CHUNK*2,))
        backup.backup(self.db,self.archive);data=self.archive.read_bytes();head=len(backup.MAGIC)+len(backup.HEADER)+1
        import struct
        first_end=head+16+struct.unpack('>I',data[head:head+4])[0]
        second_end=first_end+16+struct.unpack('>I',data[first_end:first_end+4])[0]
        self.archive.write_bytes(data[:head]+data[first_end:second_end]+data[head:first_end]+data[second_end:])
        with self.assertRaises(ValueError):backup.restore(self.archive,self.restored)
        self.assertFalse(self.restored.exists())

    def test_actual_app_outcomes_datasets_receipts_and_new_ingestion_after_restore(self):
        import app
        import test_release_outcomes
        import uuid
        from fastapi import HTTPException
        from pydantic import SecretStr
        from loop_sources import Connections
        fixture=test_release_outcomes.ReleaseOutcomeTests();fixture.setUp()
        try:
            with patch.dict(os.environ,{'AFTERSPAN_CONNECTION_KEY':base64.b64encode(b'k'*32).decode()}):
                connections=Connections(app.connect);active=str(uuid.uuid4());revoked=str(uuid.uuid4())
                for id in [active,revoked]:connections.put('alpha',id,0,'github',{'repository':'owner/application'},SecretStr('synthetic-provider-canary'))
                connections.revoke('alpha',revoked,1)
                before=fixture.compare();self.assertEqual(before.status_code,200)
                backup.backup(app.DB_PATH,self.archive);backup.restore(self.archive,self.restored)
                with patch.object(app,'DB_PATH',self.restored):
                    after=fixture.compare();self.assertEqual(after.status_code,200,after.text)
                    self.assertEqual(after.json()['fingerprint'],before.json()['fingerprint'])
                    self.assertEqual(connections.resolve('alpha',active)['secret'].get_secret_value(),'synthetic-provider-canary')
                    for key in ['',base64.b64encode(b'x'*32).decode()]:
                        with patch.dict(os.environ,{'AFTERSPAN_CONNECTION_KEY':key}):
                            with self.assertRaises(HTTPException) as error:connections.resolve('alpha',active)
                            self.assertEqual(error.exception.status_code,503)
                    with self.assertRaises(HTTPException) as error:connections.resolve('alpha',revoked)
                    self.assertEqual(error.exception.status_code,409)
                    response=fixture.f.fixture.ingest({'id':'after-restore','name':'restore.checked','properties':{'synthetic':True}})
                    self.assertEqual(response.status_code,200,response.text);self.assertEqual(response.json()['accepted'],1)
                    self.assertEqual(fixture.client.get('/api/projects').status_code,401)
        finally:fixture.tearDown()

    def test_actual_cli_backup_and_restore_run_in_separate_processes(self):
        import subprocess,sys,json
        command=[sys.executable,str(Path(__file__).parents[1]/'scripts/backup.py')]
        created=subprocess.run([*command,'create','--database',str(self.db),'--archive',str(self.archive)],capture_output=True,text=True)
        self.assertEqual(created.returncode,0,created.stderr);self.assertTrue(json.loads(created.stdout)['encrypted'])
        restored=subprocess.run([*command,'restore','--archive',str(self.archive),'--destination',str(self.restored)],capture_output=True,text=True)
        self.assertEqual(restored.returncode,0,restored.stderr);self.assertTrue(json.loads(restored.stdout)['sqlite_checked'])
        self.assertNotIn(os.environ['AFTERSPAN_BACKUP_KEY'],created.stdout+restored.stdout)
