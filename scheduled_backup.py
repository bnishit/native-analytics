"""Operator-only scheduled recovery. No customer export or remote execution surface."""
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid
from urllib.parse import urlsplit
from scripts.backup import backup,restore,cipher

_LOG=logging.getLogger(__name__)

def state_path(database):return Path(str(database)+'.backup.json')
def pending_path(database):return Path(str(database)+'.backup.asb')
def lock_path(database):return Path(str(database)+'.backup.lock')

def settings():
    if os.environ.get('AFTERSPAN_BACKUP_ENABLED')!='true':return None
    names=['BUCKET','ENDPOINT','REGION','PREFIX','ACCESS_KEY_ID','SECRET_ACCESS_KEY']
    cfg={name:os.environ.get('AFTERSPAN_BACKUP_'+name,'') for name in names}
    endpoint=urlsplit(cfg['ENDPOINT'])
    if not all(cfg.values()) or endpoint.scheme!='https' or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path not in {'','/'}:raise ValueError('configuration_invalid')
    if not re.fullmatch(r'[a-z0-9][a-z0-9/_-]{0,99}/',cfg['PREFIX']) or '//' in cfg['PREFIX']:raise ValueError('configuration_invalid')
    cfg['retain']=int(os.environ.get('AFTERSPAN_BACKUP_RETAIN','7'));cfg['interval']=int(os.environ.get('AFTERSPAN_BACKUP_INTERVAL_SECONDS','86400'));cfg['max_bytes']=int(os.environ.get('AFTERSPAN_BACKUP_MAX_BYTES',str(1024**3)))
    cfg['style']=os.environ.get('AFTERSPAN_BACKUP_ADDRESSING_STYLE','virtual')
    if cfg['style'] not in {'virtual','path'} or not 2<=cfg['retain']<=31 or not 3600<=cfg['interval']<=7*86400 or not 1024**2<=cfg['max_bytes']<=10*1024**3:raise ValueError('configuration_invalid')
    cipher()
    cfg['fingerprint']=hashlib.sha256(json.dumps([cfg[n] for n in ['BUCKET','ENDPOINT','REGION','PREFIX','style']]+[os.environ['AFTERSPAN_BACKUP_KEY']]).encode()).hexdigest()
    return cfg

def client(cfg):
    import boto3
    from botocore.config import Config
    return boto3.client('s3',endpoint_url=cfg['ENDPOINT'],region_name=cfg['REGION'],aws_access_key_id=cfg['ACCESS_KEY_ID'],aws_secret_access_key=cfg['SECRET_ACCESS_KEY'],config=Config(signature_version='s3v4',s3={'addressing_style':cfg['style']},connect_timeout=5,read_timeout=15,retries={'total_max_attempts':2,'mode':'standard'},request_checksum_calculation='when_required',response_checksum_validation='when_required'))

def transfer_config():
    from boto3.s3.transfer import TransferConfig
    return TransferConfig(max_concurrency=1,use_threads=False,preferred_transfer_client='classic')

def load(database):
    path=state_path(database)
    if not path.exists():return {'status':'waiting','history':[]}
    if path.stat().st_mode&0o077 or path.stat().st_size>65536:raise ValueError('state_invalid')
    state=json.loads(path.read_text())
    if not isinstance(state,dict) or state.get('status') not in {'waiting','running','succeeded','failed'} or not isinstance(state.get('history'),list) or len(state['history'])>32:raise ValueError('state_invalid')
    for name in ['last_attempt','last_success']:
        if name in state and (type(state[name]) not in {int,float} or not math.isfinite(state[name]) or state[name]<0):raise ValueError('state_invalid')
    if state.get('reason') not in {None,'readback_invalid','pending_invalid','size_limit','retention_scope_invalid','storage_or_recovery_failed'}:raise ValueError('state_invalid')
    return state

def save(database,state):
    path=state_path(database);temp=path.with_name(path.name+'.tmp')
    with os.fdopen(os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os,'O_NOFOLLOW',0),0o600),'w') as out:
        os.fchmod(out.fileno(),0o600);json.dump(state,out);out.flush();os.fsync(out.fileno())
    os.replace(temp,path)

def status(database,now=None):
    now=time.time() if now is None else now
    try:
        cfg=settings()
        if cfg is None:return {'enabled':False,'status':'disabled','overdue':False}
    except (ValueError,OSError,KeyError):return {'enabled':True,'status':'failed','reason':'configuration_invalid','overdue':True}
    try:
        state=load(database)
        if state.get('configuration',cfg['fingerprint'])!=cfg['fingerprint']:raise ValueError('configuration_changed')
        if max(state.get('last_attempt',0),state.get('last_success',0))>now+60:return {'enabled':True,'status':'failed','reason':'clock_invalid','overdue':True}
        result={k:state[k] for k in ['status','reason','last_attempt','last_success'] if k in state}
        return {'enabled':True,**result,'overdue':not state.get('last_success') or now-state['last_success']>cfg['interval']+600,'verified_copies':len(state['history'])}
    except (ValueError,OSError,KeyError):return {'enabled':True,'status':'failed','reason':'state_or_configuration_changed','overdue':True}

def validate_pending(cfg,pending):
    if type(pending.get('bytes')) is not int or not 0<pending['bytes']<=cfg['max_bytes'] or not re.fullmatch(re.escape(cfg['PREFIX'])+r'[a-f0-9-]{36}\.asb',pending.get('key','')) or not re.fullmatch(r'[a-f0-9]{64}',pending.get('sha256','')):raise ValueError('pending_invalid')

def verify(store,cfg,pending,directory):
    validate_pending(cfg,pending)
    if store.head_object(Bucket=cfg['BUCKET'],Key=pending['key'])['ContentLength']!=pending['bytes']:raise ValueError('readback_invalid')
    downloaded=Path(directory)/'readback.asb';restored=Path(directory)/'restored.db'
    downloaded.unlink(missing_ok=True);restored.unlink(missing_ok=True)
    store.download_file(cfg['BUCKET'],pending['key'],str(downloaded),Config=transfer_config())
    with downloaded.open('rb') as src:
        if downloaded.stat().st_size!=pending['bytes'] or hashlib.file_digest(src,'sha256').hexdigest()!=pending['sha256']:raise ValueError('readback_invalid')
    return restore(downloaded,restored)

def prune(database,state,store,cfg):
    while len(state['history'])>cfg['retain']:
        old=state['history'][0]
        if not re.fullmatch(re.escape(cfg['PREFIX'])+r'[a-f0-9-]{36}\.asb',old['key']):raise ValueError('retention_scope_invalid')
        store.delete_object(Bucket=cfg['BUCKET'],Key=old['key']);state['history'].pop(0);save(database,state)
    state.pop('cleanup_pending',None);state.update(status='succeeded');state.pop('reason',None);save(database,state)

def run_due(database,now=None):
    now=time.time() if now is None else now
    try:cfg=settings()
    except (ValueError,TypeError):return {'status':'failed','reason':'configuration_invalid'}
    if cfg is None:return status(database,now)
    try:import fcntl
    except ImportError:return {'status':'failed','reason':'unsupported_platform'}
    database=Path(database)
    with os.fdopen(os.open(lock_path(database),os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600),'a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'status':'busy'}
        try:state=load(database)
        except (ValueError,OSError):return {'status':'failed','reason':'state_invalid'}
        if state.get('configuration',cfg['fingerprint'])!=cfg['fingerprint']:return {'status':'failed','reason':'configuration_changed'}
        if max(state.get('last_attempt',0),state.get('last_success',0))>now+60:return {'status':'failed','reason':'clock_invalid'}
        pending=state.get('pending')
        if pending is not None:
            try:validate_pending(cfg,pending)
            except (ValueError,TypeError,AttributeError):return {'status':'failed','reason':'pending_invalid'}
        if 'last_attempt' in state and now-state['last_attempt']<(900 if state['status'] in {'failed','running'} else cfg['interval']):return status(database,now)
        state.update(configuration=cfg['fingerprint'],last_attempt=now,status='running');state.pop('reason',None);save(database,state)
        try:
            store=client(cfg)
            from botocore.exceptions import ClientError
            if state.get('cleanup_pending'):
                prune(database,state,store,cfg)
                return status(database,now)
            with tempfile.TemporaryDirectory(prefix='afterspan-recovery-check-') as directory:
                archive=pending_path(database)
                resuming=pending is not None
                if pending is None:
                    archive.unlink(missing_ok=True)
                    if database.stat().st_size>cfg['max_bytes']:raise ValueError('size_limit')
                    backup(database,archive)
                    if archive.stat().st_size>cfg['max_bytes']:raise ValueError('size_limit')
                    with archive.open('rb') as src:checksum=hashlib.file_digest(src,'sha256').hexdigest()
                    pending={'key':cfg['PREFIX']+str(uuid.uuid4())+'.asb','bytes':archive.stat().st_size,'sha256':checksum}
                    state['pending']=pending;save(database,state)
                valid=False
                if resuming:
                    try:verified=verify(store,cfg,pending,directory);valid=True
                    except ClientError as error:
                        if error.response.get('ResponseMetadata',{}).get('HTTPStatusCode')!=404 or error.response.get('Error',{}).get('Code') not in {'404','NoSuchKey','NotFound'}:raise
                if not valid and archive.exists():
                    if archive.is_symlink() or archive.stat().st_mode&0o077:raise ValueError('pending_invalid')
                    with archive.open('rb') as src:
                        if hashlib.file_digest(src,'sha256').hexdigest()!=pending['sha256']:raise ValueError('pending_invalid')
                    local_check=Path(directory)/'local-check.db';restore(archive,local_check);local_check.unlink()
                    try:store.upload_file(str(archive),cfg['BUCKET'],pending['key'],Config=transfer_config())
                    except Exception:
                        # A timed-out PUT may have committed. Reconcile this exact immutable object.
                        verified=verify(store,cfg,pending,directory);valid=True
                if not valid:verified=verify(store,cfg,pending,directory)
                state['history'].append({'key':pending['key'],'verified_at':now,'verified':verified})
                state.update(last_success=now,status='succeeded',cleanup_pending=True);state.pop('pending',None);save(database,state);archive.unlink(missing_ok=True)
                prune(database,state,store,cfg)
        except Exception as error:
            reason=str(error) if isinstance(error,ValueError) and str(error) in {'readback_invalid','pending_invalid','size_limit','retention_scope_invalid'} else 'storage_or_recovery_failed'
            state.update(status='failed',reason=reason);save(database,state);_LOG.error('Scheduled backup failed (%s)',reason)
        return status(database,now)

def start(database,stop):
    if os.environ.get('AFTERSPAN_BACKUP_ENABLED')!='true':return None
    def work():
        while not stop.is_set():
            try:run_due(database)
            except Exception:_LOG.error('Scheduled backup failed (local_state_unavailable)')
            if stop.wait(60):break
    worker=threading.Thread(target=work,name='encrypted-backup',daemon=True);worker.start();return worker
