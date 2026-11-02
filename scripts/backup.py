#!/usr/bin/env python3
"""Offline operator-only encrypted SQLite snapshots; never an account-scoped export API."""
import argparse
import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import time
from urllib.parse import quote
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC=b'AFTERSPAN-BACKUP-1\n'
CHUNK=4*1024*1024
HEADER=json.dumps({'format':1,'scope':'operator-entire-database','chunk_bytes':CHUNK},sort_keys=True,separators=(',',':')).encode()

def cipher():
    try:
        key=base64.b64decode(os.environ.get('AFTERSPAN_BACKUP_KEY',''),altchars=b'-_',validate=True)
        if len(key)!=32:raise ValueError()
        return AESGCM(key)
    except (ValueError,TypeError):raise ValueError('set AFTERSPAN_BACKUP_KEY to a separate 32-byte base64 key') from None

def private_create(path):
    return os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb')

def check_database(path):
    with closing(sqlite3.connect('file:'+quote(str(Path(path).resolve()))+'?mode=ro',uri=True)) as c:
        if c.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('SQLite consistency check failed')
        if c.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('SQLite foreign key check failed')
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'projects','events','charts'}.issubset(tables):raise ValueError('not an Afterspan database')

def backup(database,archive):
    database=Path(database).resolve();archive=Path(archive).resolve();aes=cipher()
    if not database.is_file():raise ValueError('source database is missing')
    if archive==database:raise ValueError('archive must differ from source')
    # SQLite backup includes committed WAL pages and produces one consistent read snapshot.
    with tempfile.TemporaryDirectory(prefix='afterspan-backup-') as directory:
        snapshot=Path(directory)/'snapshot.db'
        with private_create(snapshot):pass
        with closing(sqlite3.connect('file:'+quote(str(database))+'?mode=ro',uri=True)) as source,closing(sqlite3.connect(snapshot)) as target:
            deadline=time.monotonic()+300
            def progress(status,remaining,total):
                if time.monotonic()>deadline:raise ValueError('snapshot exceeded five-minute limit')
            source.backup(target,pages=256,progress=progress,sleep=.05)
        check_database(snapshot)
        created=False
        try:
            with private_create(archive) as out,snapshot.open('rb') as src:
                created=True;out.write(MAGIC+HEADER+b'\n');prefix=os.urandom(8);count=0;size=0;digest=hashlib.sha256()
                while block:=src.read(CHUNK):
                    if count>=0xfffffffe:raise ValueError('snapshot too large')
                    nonce=prefix+struct.pack('>I',count);sealed=aes.encrypt(nonce,block,HEADER+struct.pack('>I',count))
                    out.write(struct.pack('>I',len(sealed))+nonce+sealed);digest.update(block);size+=len(block);count+=1
                receipt={'chunks':count,'bytes':size,'sha256':digest.hexdigest()}
                nonce=prefix+b'\xff'*4;sealed=aes.encrypt(nonce,json.dumps(receipt,sort_keys=True).encode(),HEADER+b'final')
                out.write(struct.pack('>I',0)+nonce+struct.pack('>I',len(sealed))+sealed);out.flush();os.fsync(out.fileno())
        except BaseException:
            if created:archive.unlink(missing_ok=True)
            raise
    return {'scope':'operator-entire-database','encrypted':True,'consistent_sqlite_snapshot':True,'bytes':size,'chunks':count}

def exact(src,size):
    data=src.read(size)
    if len(data)!=size:raise ValueError('truncated archive')
    return data

def restore(archive,destination):
    archive=Path(archive).resolve();destination=Path(destination).resolve();aes=cipher()
    if destination.exists():raise ValueError('restore requires a new isolated database path')
    if not destination.parent.is_dir():raise ValueError('create a private isolated restore directory first')
    if destination.parent.stat().st_mode&0o077:raise ValueError('restore directory must be private (mode 700)')
    created=False
    try:
        with archive.open('rb') as src,private_create(destination) as out:
            created=True
            if exact(src,len(MAGIC))!=MAGIC or exact(src,len(HEADER)+1)!=HEADER+b'\n':raise ValueError('unsupported archive format or scope')
            count=0;size=0;digest=hashlib.sha256();prefix=None
            while True:
                length=struct.unpack('>I',exact(src,4))[0];nonce=exact(src,12)
                if prefix is None:prefix=nonce[:8]
                if nonce[:8]!=prefix:raise ValueError('archive nonce prefix changed')
                if length==0:
                    if nonce[8:]!=b'\xff'*4:raise ValueError('invalid archive footer')
                    length=struct.unpack('>I',exact(src,4))[0]
                    if not 16<=length<=1024:raise ValueError('invalid archive footer size')
                    receipt=json.loads(aes.decrypt(nonce,exact(src,length),HEADER+b'final'))
                    if receipt!={'chunks':count,'bytes':size,'sha256':digest.hexdigest()} or src.read(1):raise ValueError('archive completeness check failed')
                    break
                if not 17<=length<=CHUNK+16 or count>=0xfffffffe or nonce[8:]!=struct.pack('>I',count):raise ValueError('invalid archive frame')
                block=aes.decrypt(nonce,exact(src,length),HEADER+struct.pack('>I',count))
                out.write(block);digest.update(block);size+=len(block);count+=1
            out.flush();os.fsync(out.fileno())
        check_database(destination)
    except BaseException as exc:
        if created:destination.unlink(missing_ok=True)
        if isinstance(exc,(KeyboardInterrupt,SystemExit)):raise
        raise ValueError('restore failed authentication, completeness or database validation') from None
    return {'scope':'operator-entire-database','restored_to_new_path':True,'sqlite_checked':True,'bytes':size}

def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    b=sub.add_parser('create');b.add_argument('--database',required=True);b.add_argument('--archive',required=True)
    r=sub.add_parser('restore');r.add_argument('--archive',required=True);r.add_argument('--destination',required=True)
    args=parser.parse_args()
    try:result=backup(args.database,args.archive) if args.command=='create' else restore(args.archive,args.destination)
    except Exception as exc:parser.exit(1,'Backup operation failed: '+str(exc)+'\n')
    print(json.dumps(result))

if __name__=='__main__':main()
