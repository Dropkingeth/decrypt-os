"""Bounded local encrypted recovery and authenticated immutable app releases.

No network, shell, provider calls, service commands or order replay. Adoption
requires the server's private quiescence context and fresh native-flat callback.
"""
from __future__ import annotations
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import stat
import threading
import time

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAX_FILE=64*1024*1024
MAX_TOTAL=256*1024*1024
MAX_FILES=2048
ID=re.compile(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,95}\Z')
HEX=re.compile(r'[a-f0-9]{64}\Z')
BACKUP_AAD=b'decrypt-os-private-backup-v1'
CREDENTIAL_FILES={'guardian/vault.json','guardian/execution-routes.json','guardian/execution-ingress.json'}
PUBLIC_CONFIG={'guardian/monitor.json','guardian/recovery.json','guardian/manual-lock.json'}
SAFETY_FIELDS={'checkedAt','entriesHeld','allNativeFlat','workingOrders','pendingOrders','unknownCommands','activeInstances'}

class MaintenanceError(ValueError):
    def __init__(self,code,status=409):
        self.code,self.status,self.message=code,status,code.replace('_',' ')
        super().__init__(self.message)

def need(value,code='maintenance_refused'):
    if not value:raise MaintenanceError(code)

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()

def digest(raw):return hashlib.sha256(raw).hexdigest()

def strict_json(raw):
    def pairs(items):
        out={}
        for key,value in items:
            need(key not in out,'duplicate_json_key');out[key]=value
        return out
    def nonfinite(_):raise MaintenanceError('nonfinite_json')
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=nonfinite)

def relative(value):
    need(type(value) is str and 0<len(value)<=240 and '\\' not in value,'invalid_path')
    parts=value.split('/')
    need(all(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9._-]{0,95}',p) and p not in ('.','..') for p in parts),'invalid_path')
    need(not PurePosixPath(value).is_absolute(),'invalid_path')
    return value

def sync_directory(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)

def directory(path,create=False):
    path=Path(path).absolute()
    for parent in reversed((path,*path.parents)):
        if not parent.exists():
            need(create,'missing_directory')
            try:parent.mkdir(mode=0o700)
            except FileExistsError:pass   # the api and ui services create this tree at the same moment on first login
            sync_directory(parent.parent)
        info=parent.lstat()
        need(stat.S_ISDIR(info.st_mode) and not parent.is_symlink(),'linked_directory')
    info=path.stat()
    need(info.st_uid==os.getuid() and not info.st_mode&0o077,'private_directory_required')
    return path

def regular(path,maximum=MAX_FILE):
    path=Path(path).absolute()
    for parent in path.parents:need(not parent.is_symlink(),'linked_directory')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        need(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_uid in (0,os.getuid())
             and not before.st_mode&0o022 and before.st_size<=maximum,'unsafe_file')
        raw=stream.read(maximum+1);after=os.fstat(stream.fileno())
    need(len(raw)==before.st_size and all(getattr(before,k)==getattr(after,k) for k in
         ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')),'file_changed_during_read')
    return raw

def write_new(path,raw,mode=0o600):
    path=Path(path);directory(path.parent,create=True)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    with os.fdopen(fd,'wb') as out:
        out.write(raw);out.flush();os.fsync(out.fileno())
    sync_directory(path.parent)

def atomic(path,value):
    path=Path(path);directory(path.parent,create=True)
    if path.exists() or path.is_symlink():regular(path)
    temporary=path.with_name('.write-'+secrets.token_hex(12))
    write_new(temporary,canonical(value))
    os.replace(temporary,path)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def sqlite_memory(raw):
    # A fully checkpointed WAL-mode main file remains byte-preserved in archives.
    # SQLite deserialize cannot open WAL headers in :memory:. Adapt ONLY the
    # transient in-memory header for integrity/logical checks, never stored bytes.
    view=raw
    if raw[:16]==b'SQLite format 3\x00' and raw[18:20]==b'\x02\x02':
        view=raw[:18]+b'\x01\x01'+raw[20:]
    db=sqlite3.connect(':memory:')
    try:db.deserialize(view);return db
    except BaseException:db.close();raise


def state_path_allowed(name,credentials=False):
    if type(name) is str and re.fullmatch(r'trading/[A-Za-z0-9][A-Za-z0-9_-]{0,95}/\.writer\.lock',name):return True
    relative(name)
    if name.startswith('trading/browser-profile/'):return False
    return bool(name=='onboarding.sqlite' or name in PUBLIC_CONFIG
        or name in {'trading/instances.sqlite','trading/service.sqlite','trading/ingress.sqlite','trading/health.sqlite'}
        or re.fullmatch(r'trading/[A-Za-z0-9][A-Za-z0-9_-]{0,95}/(engine|market)\.sqlite',name)
        or re.fullmatch(r'trading/commissioning/(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,95}|history/[a-f0-9]{64})\.json',name)
        or credentials and name in CREDENTIAL_FILES)

def release_path_allowed(name):
    relative(name)
    suffix=Path(name).suffix.lower()
    # Shell scripts ship only inside the bridge module (its launchers); never in app/ or installer/.
    if suffix=='.sh':return name.startswith('modules/tradovate-bridge/')
    return name.startswith(('app/','installer/','modules/tradovate-bridge/')) and suffix in {
        '.py','.json','.txt','.md','.html','.js','.css','.svg','.png','.jpg','.jpeg','.webp','.woff','.woff2','.service','.timer','.desktop'}


def verify_release(manifest,signature,key_id,files,trust,now_ms,*,enforce_expiry=True):
    need(type(manifest) is dict and set(manifest)=={'schema','version','sequence','stateSchema','createdAt','expiresAt','files'},'invalid_release_manifest')
    need(manifest['schema']=='decrypt.signed-release.v1' and type(manifest['version']) is str and ID.fullmatch(manifest['version']) and manifest['version']!='installed','invalid_release_manifest')
    for key in ('sequence','stateSchema','createdAt','expiresAt'):need(type(manifest[key]) is int,'invalid_release_manifest')
    need(manifest['sequence']>0 and manifest['stateSchema']==1 and 0<=manifest['createdAt']<=now_ms+300000
         and (not enforce_expiry or manifest['expiresAt']>now_ms) and manifest['expiresAt']>manifest['createdAt'],'expired_or_incompatible_release')
    need(type(key_id) is str and key_id in trust,'release_key_not_trusted')
    try:
        key=Ed25519PublicKey.from_public_bytes(trust[key_id]);key.verify(base64.b64decode(signature,validate=True),canonical(manifest))
    except (ValueError,TypeError,InvalidSignature):raise MaintenanceError('invalid_release_signature') from None
    need(type(manifest['files']) is list and 1<=len(manifest['files'])<=MAX_FILES,'invalid_release_inventory')
    expected={};total=0
    for row in manifest['files']:
        need(type(row) is dict and set(row)=={'path','bytes','sha256'},'invalid_release_inventory')
        name=row['path'];need(release_path_allowed(name) and name not in expected,'invalid_release_inventory')
        need(type(row['bytes']) is int and 0<=row['bytes']<=MAX_FILE and type(row['sha256']) is str and HEX.fullmatch(row['sha256']),'invalid_release_inventory')
        total+=row['bytes'];need(total<=MAX_TOTAL,'release_too_large');expected[name]=row
    need({'app/backend/server.py','app/ui/dist/index.html'}<=expected.keys(),'release_missing_backend_or_ui')
    need(type(files) is dict and set(files)==set(expected),'release_inventory_mismatch')
    for name,raw in files.items():
        need(type(raw) is bytes and len(raw)==expected[name]['bytes'] and digest(raw)==expected[name]['sha256'],'release_hash_mismatch')
    return digest(canonical(manifest))

class Maintenance:
    def __init__(self,state_dir,runtime_safety,*,trusted_release_keys=None,quiesce=None,clock=time.time,installed_release=None):
        self.state=directory(state_dir)
        self.root=directory(self.state/'maintenance',create=True)
        self.safety,self.quiesce,self.clock=runtime_safety,quiesce,clock
        self.installed_release=installed_release
        self.trust=dict(trusted_release_keys or {})
        self.mutex=threading.RLock()
        for name in ('backups','restores','releases','transactions'):directory(self.root/name,create=True)

    def _now(self):return int(self.clock()*1000)

    def _safe(self):
        value=self.safety()
        need(type(value) is dict and set(value)==SAFETY_FIELDS,'native_safety_unknown')
        need(type(value['checkedAt']) is int and 0<=self._now()-value['checkedAt']<=10000,'native_safety_stale')
        need(value['entriesHeld'] is True and value['allNativeFlat'] is True,'entries_must_be_held_and_native_flat')
        for key in ('workingOrders','pendingOrders','unknownCommands','activeInstances'):
            need(type(value[key]) is int and value[key]==0,'workers_or_native_orders_not_clear')
        return value

    @contextmanager
    def _operation(self,flat=False):
        need(self.quiesce is not None,'maintenance_quiescence_unavailable')
        with self.mutex:
            path=self.root/'operation.lock'
            if not path.exists():
                try:write_new(path,b'')
                except FileExistsError:pass
            regular(path)
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
            try:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise MaintenanceError('maintenance_busy') from None
                with self.quiesce():
                    if flat:self._safe()
                    yield
            finally:os.close(fd)

    def _files(self,credentials):
        result={}
        names={'onboarding.sqlite',*PUBLIC_CONFIG,'trading/instances.sqlite','trading/service.sqlite','trading/ingress.sqlite','trading/health.sqlite'}
        if credentials:names|=CREDENTIAL_FILES
        trading=self.state/'trading'
        if trading.exists() or trading.is_symlink():
            directory(trading)
            for child in trading.iterdir():
                if child.name in ('browser-profile','commissioning'):continue
                if re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,95}',child.name) and child.is_dir():
                    directory(child)
                    for database in ('engine.sqlite','market.sqlite'):names.add('trading/'+child.name+'/'+database)
                    # A cold restore must be able to exclude every engine writer,
                    # including an active instance held before Runtime.open.
                    names.add('trading/'+child.name+'/.writer.lock')
        commissioning=trading/'commissioning'
        if commissioning.exists() or commissioning.is_symlink():
            directory(commissioning)
            for child in commissioning.iterdir():
                if child.name=='history':
                    directory(child)
                    for receipt in child.iterdir():
                        name=receipt.relative_to(self.state).as_posix()
                        need(state_path_allowed(name),'commissioning_receipt_path_invalid');names.add(name)
                else:
                    name=child.relative_to(self.state).as_posix()
                    need(state_path_allowed(name),'commissioning_receipt_path_invalid');names.add(name)
        need(sum(name.startswith('trading/commissioning/') for name in names)<=128,'too_many_commissioning_receipts')
        # Do not traverse browser, maintenance, home or other application data.
        for name in sorted(names):
            path=self.state/name
            if path.exists() or path.is_symlink():result[name]=path
        if credentials:
            route='guardian/execution-routes.json'
            need(not ({'guardian/execution-routes.json','guardian/execution-ingress.json'} & result.keys()) or 'guardian/vault.json' in result,'route_vault_pair_missing')
        need(len(result)<=MAX_FILES,'too_many_backup_files')
        return result

    def _is_archived(self,instance):
        path=self.state/'trading/instances.sqlite'
        if not path.exists():return False
        regular(path);db=sqlite3.connect(path.absolute().as_uri()+'?mode=ro',uri=True)
        try:
            tables=db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='retired_instances'").fetchall()
            return bool(tables and db.execute('SELECT 1 FROM retired_instances WHERE id=?',(instance,)).fetchone())
        finally:db.close()

    def _snapshot(self,path):
        raw=regular(path)
        name=path.relative_to(self.state).parts
        if len(name)==3 and name[0]=='trading' and self._is_archived(name[1]):
            # Retirement receipts bind exact closed bytes. Do not VACUUM or alter
            # SQLite headers of archived ledgers. Service verifies receipt hashes.
            for suffix in ('-wal','-shm'):
                side=Path(str(path)+suffix);need(not side.exists() and not side.is_symlink(),'archived_database_not_settled')
            target=sqlite_memory(raw)
            try:
                need(target.execute('PRAGMA integrity_check').fetchone()==('ok',),'database_integrity_failed')
            finally:target.close()
            return raw
        db=sqlite3.connect(path.absolute().as_uri()+'?mode=ro',uri=True,timeout=5)
        target=sqlite3.connect(':memory:')
        try:
            db.backup(target)
            need(target.execute('PRAGMA integrity_check').fetchone()==('ok',),'database_integrity_failed')
            target.execute('VACUUM')
            raw=target.serialize();need(len(raw)<=MAX_FILE,'backup_file_too_large')
            return raw
        finally:target.close();db.close()

    @staticmethod
    def _key(passphrase,salt):
        need(type(passphrase) is str and 12<=len(passphrase)<=1024,'backup_passphrase_required')
        return Scrypt(salt=salt,length=32,n=2**15,r=8,p=1).derive(passphrase.encode())

    def create_backup(self,passphrase,include_credentials=False):
        need(type(include_credentials) is bool,'invalid_backup_option')
        with self._operation():
            files={}
            for name,path in self._files(include_credentials).items():
                raw=self._snapshot(path) if name.endswith('.sqlite') else regular(path)
                need(not name.endswith('/.writer.lock') or raw==b'','invalid_writer_lock')
                files[name]={'bytes':len(raw),'sha256':digest(raw),'data':base64.b64encode(raw).decode()}
            need(sum(x['bytes'] for x in files.values())<=MAX_TOTAL,'backup_too_large')
            value={'schema':'decrypt.private-backup.v1','createdAt':self._now(),'credentialsIncluded':include_credentials,'stateSchema':1,'files':files}
            salt,nonce=secrets.token_bytes(16),secrets.token_bytes(12)
            encrypted=AESGCM(self._key(passphrase,salt)).encrypt(nonce,canonical(value),BACKUP_AAD)
            envelope={'schema':'decrypt.encrypted-backup.v1','kdf':'scrypt-32768-8-1','salt':base64.b64encode(salt).decode(),
                      'nonce':base64.b64encode(nonce).decode(),'ciphertext':base64.b64encode(encrypted).decode()}
            identifier=secrets.token_hex(16);raw=canonical(envelope)
            write_new(self.root/'backups'/(identifier+'.json'),raw)
            return {'backupId':identifier,'sha256':digest(raw),'bytes':len(raw),'credentialsIncluded':include_credentials,
                    'fileCount':len(files),'browserCookiesIncluded':False}

    def status(self):
        pointer=self._pointer()
        hold=self.root/'hold.json'
        versions=[]
        for path in sorted((self.root/'releases').iterdir()):
            if path.name.startswith('stage-'):continue
            try:versions.append(self.verified_release(path.name)['version'])
            except (MaintenanceError,OSError,ValueError,KeyError,TypeError):continue
        return {'schema':'decrypt.maintenance-status.v1','backupCount':len(self.list_backups()),
                'stagedVersions':versions,'currentVersion':pointer['version'] if pointer else 'installed',
                'highestSequence':pointer['highestSequence'] if pointer else 0,
                'nativeReconciliationRequired':hold.exists() or hold.is_symlink(),
                'automaticReplay':False,'largeArchiveTransfer':'local_cli',
                'installedBaselineConfigured':self.installed_release is not None}

    def list_backups(self):
        return [{'backupId':p.stem,'bytes':p.stat().st_size} for p in sorted((self.root/'backups').iterdir())
                if re.fullmatch('[a-f0-9]{32}\\.json',p.name) and p.is_file() and not p.is_symlink()]

    def export_backup(self,backup_id):
        need(type(backup_id) is str and re.fullmatch('[a-f0-9]{32}',backup_id),'invalid_backup_id')
        return regular(self.root/'backups'/(backup_id+'.json'),MAX_TOTAL*2)

    def import_backup(self,encrypted_bytes):
        need(type(encrypted_bytes) is bytes and len(encrypted_bytes)<=MAX_TOTAL*2,'backup_too_large')
        outer=strict_json(encrypted_bytes)
        need(type(outer) is dict and set(outer)=={'schema','kdf','salt','nonce','ciphertext'}
             and outer['schema']=='decrypt.encrypted-backup.v1' and outer['kdf']=='scrypt-32768-8-1','invalid_backup')
        for key in ('salt','nonce','ciphertext'):need(type(outer[key]) is str,'invalid_backup')
        need(len(base64.b64decode(outer['salt'],validate=True))==16
             and len(base64.b64decode(outer['nonce'],validate=True))==12,'invalid_backup')
        need(16<=len(base64.b64decode(outer['ciphertext'],validate=True))<=MAX_TOTAL*2,'invalid_backup')
        identifier=secrets.token_hex(16)
        write_new(self.root/'backups'/(identifier+'.json'),encrypted_bytes)
        return {'backupId':identifier,'sha256':digest(encrypted_bytes),'bytes':len(encrypted_bytes),'status':'imported_encrypted_unverified'}

    def prepare_restore(self,backup_id,passphrase):
        need(type(backup_id) is str and re.fullmatch('[a-f0-9]{32}',backup_id),'invalid_backup_id')
        raw=regular(self.root/'backups'/(backup_id+'.json'),MAX_TOTAL*2)
        try:
            outer=strict_json(raw)
            need(type(outer) is dict and set(outer)=={'schema','kdf','salt','nonce','ciphertext'}
                 and outer['schema']=='decrypt.encrypted-backup.v1' and outer['kdf']=='scrypt-32768-8-1','invalid_backup')
            salt=base64.b64decode(outer['salt'],validate=True);nonce=base64.b64decode(outer['nonce'],validate=True)
            need(len(salt)==16 and len(nonce)==12,'invalid_backup')
            clear=AESGCM(self._key(passphrase,salt)).decrypt(nonce,base64.b64decode(outer['ciphertext'],validate=True),BACKUP_AAD)
            payload=strict_json(clear)
        except (ValueError,TypeError,KeyError,InvalidTag):raise MaintenanceError('backup_unlock_failed') from None
        need(type(payload) is dict and set(payload)=={'schema','createdAt','credentialsIncluded','stateSchema','files'}
             and payload['schema']=='decrypt.private-backup.v1' and type(payload['stateSchema']) is int and payload['stateSchema']==1
             and type(payload['createdAt']) is int and payload['createdAt']>=0
             and type(payload['credentialsIncluded']) is bool and type(payload['files']) is dict,'invalid_backup')
        files={};total=0
        need(len(payload['files'])<=MAX_FILES,'backup_too_large')
        need(sum(n.startswith('trading/commissioning/') for n in payload['files'])<=128,'too_many_commissioning_receipts')
        for name,row in payload['files'].items():
            need(state_path_allowed(name,payload['credentialsIncluded']) and type(row) is dict and set(row)=={'bytes','sha256','data'},'invalid_backup')
            data=base64.b64decode(row['data'],validate=True)
            need(not name.endswith('/.writer.lock') or data==b'','invalid_retired_writer_lock')
            need(type(row['bytes']) is int and len(data)==row['bytes'] and len(data)<=MAX_FILE and digest(data)==row['sha256'],'backup_hash_mismatch')
            if name.endswith('.sqlite'):
                check=sqlite_memory(data)
                try:need(check.execute('PRAGMA integrity_check').fetchone()==('ok',),'database_integrity_failed')
                finally:check.close()
            total+=len(data);need(total<=MAX_TOTAL,'backup_too_large');files[name]=data
        need(not ({'guardian/execution-routes.json','guardian/execution-ingress.json'} & files.keys()) or 'guardian/vault.json' in files,'route_vault_pair_missing')
        identifier=secrets.token_hex(16);stage=directory(self.root/'restores'/identifier,create=True)
        for name,data in files.items():write_new(stage/'files'/name,data)
        receipt={'schema':'decrypt.restore-stage.v1','backupId':backup_id,'backupSha256':digest(raw),
                 'credentialsIncluded':payload['credentialsIncluded'],'files':[{'path':n,'bytes':len(d),'sha256':digest(d)} for n,d in sorted(files.items())]}
        write_new(stage/'RECEIPT.json',canonical(receipt))
        return {'restoreId':identifier,'fileCount':len(files),'credentialsIncluded':payload['credentialsIncluded'],
                'status':'staged','requiresHeldFlatAndStoppedWorkers':True,'automaticReplay':False}

    def _hold(self,reason,transaction_id=None):
        value={'schema':'decrypt.maintenance-hold.v1','reason':reason,'at':self._now(),
               'explicitNativeReconciliationRequired':True,'automaticReplay':False}
        if transaction_id is not None:
            need(type(transaction_id) is str and re.fullmatch('[a-f0-9]{32}',transaction_id),'invalid_transaction_id')
            value['transactionId']=transaction_id
        atomic(self.root/'hold.json',value)

    @staticmethod
    def _content_digest(name,raw):
        if not name.endswith('.sqlite'):return digest(raw)
        db=sqlite_memory(raw)
        try:
            return digest('\n'.join(db.iterdump()).encode())
        finally:db.close()

    def apply_restore(self,restore_id):
        need(type(restore_id) is str and re.fullmatch('[a-f0-9]{32}',restore_id),'invalid_restore_id')
        with self._operation(flat=True):
            stage=directory(self.root/'restores'/restore_id)
            receipt=strict_json(regular(stage/'RECEIPT.json'))
            need(type(receipt) is dict and set(receipt)=={'schema','backupId','backupSha256','credentialsIncluded','files'}
                 and receipt['schema']=='decrypt.restore-stage.v1' and type(receipt['credentialsIncluded']) is bool
                 and type(receipt['backupId']) is str and re.fullmatch('[a-f0-9]{32}',receipt['backupId'])
                 and type(receipt['backupSha256']) is str and HEX.fullmatch(receipt['backupSha256'])
                 and type(receipt['files']) is list and len(receipt['files'])<=MAX_FILES,'invalid_restore_stage')
            files={} 
            for row in receipt['files']:
                need(type(row) is dict and set(row)=={'path','bytes','sha256'} and type(row['bytes']) is int
                     and 0<=row['bytes']<=MAX_FILE and type(row['sha256']) is str and HEX.fullmatch(row['sha256']),'invalid_restore_stage')
                name=row['path'];need(name not in files and state_path_allowed(name,receipt['credentialsIncluded']),'invalid_restore_stage')
                raw=regular(stage/'files'/name);need(not name.endswith('/.writer.lock') or raw==b'','invalid_retired_writer_lock');need(len(raw)==row['bytes'] and digest(raw)==row['sha256'],'restore_stage_changed');files[name]=raw
            need(sum(len(x) for x in files.values())<=MAX_TOTAL,'backup_too_large')
            need(sum(n.startswith('trading/commissioning/') for n in files)<=128,'too_many_commissioning_receipts')
            need(not ({'guardian/execution-routes.json','guardian/execution-ingress.json'} & files.keys()) or 'guardian/vault.json' in files,'route_vault_pair_missing')
            transaction=directory(self.root/'transactions'/secrets.token_hex(16),create=True)
            changes=[]
            for name,raw in files.items():
                target=self.state/name
                old=regular(target) if target.exists() or target.is_symlink() else None
                # Preserve a consistent original database, including WAL contents.
                if old is not None and name.endswith('.sqlite'):old=self._snapshot(target)
                if old is not None:write_new(transaction/'before'/name,old)
                write_new(transaction/'after'/name,raw)
                changes.append({'path':name,'hadOriginal':old is not None,'beforeSha256':digest(old) if old is not None else None,
                                'afterSha256':digest(raw),'beforeContentSha256':self._content_digest(name,old) if old is not None else None,
                                'afterContentSha256':self._content_digest(name,raw)})
            journal={'schema':'decrypt.restore-transaction.v1','phase':'prepared','restoreId':restore_id,'changes':changes}
            atomic(transaction/'JOURNAL.json',journal)
            self._safe();self._hold('restore_requires_native_reconciliation',transaction.name)
            try:
                for row in changes:
                    name=row['path'];target=self.state/name;directory(target.parent,create=True)
                    self._replace_state(target,files[name])
                journal['phase']='applied';atomic(transaction/'JOURNAL.json',journal)
            except BaseException:
                journal['phase']='recovery_required';atomic(transaction/'JOURNAL.json',journal);raise
            return {'status':'restored_held','transactionId':transaction.name,'automaticReplay':False,
                    'restartRequired':True,'nativeReconciliationRequired':True,'priorFilesPreserved':True}

    def _replace_state(self,target,raw):
        # Internal only, held and quiesced. Never attach an old WAL to a new DB.
        directory(target.parent,create=True)
        if target.exists() or target.is_symlink():regular(target)
        if target.suffix=='.sqlite':
            if target.exists():
                db=sqlite3.connect(target)
                try:
                    result=db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                    need(result is not None and result[0]==0,'database_writer_not_stopped')
                finally:db.close()
            for suffix in ('-wal','-shm'):
                side=Path(str(target)+suffix)
                if side.exists() or side.is_symlink():regular(side);side.unlink()
        if raw is None:
            if target.exists():target.unlink()
        else:
            temporary=target.with_name('.restore-'+secrets.token_hex(12))
            write_new(temporary,raw);os.replace(temporary,target)
        fd=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)

    def recover_restore(self,transaction_id):
        need(type(transaction_id) is str and re.fullmatch('[a-f0-9]{32}',transaction_id),'invalid_transaction_id')
        with self._operation(flat=True):
            transaction=directory(self.root/'transactions'/transaction_id)
            journal=strict_json(regular(transaction/'JOURNAL.json'))
            need(type(journal) is dict and set(journal)=={'schema','phase','restoreId','changes'}
                 and journal['schema']=='decrypt.restore-transaction.v1' and type(journal['changes']) is list
                 and len(journal['changes'])<=MAX_FILES,'invalid_restore_transaction')
            need(journal['phase'] in ('prepared','recovery_required'),'restore_not_interrupted')
            originals={}
            # Validate every retained copy and current state BEFORE the first swap.
            for row in journal['changes']:
                need(type(row) is dict and set(row)=={'path','hadOriginal','beforeSha256','afterSha256','beforeContentSha256','afterContentSha256'}
                     and type(row['hadOriginal']) is bool,'invalid_restore_transaction')
                name=row['path'];need(state_path_allowed(name,True) and name not in originals,'invalid_restore_transaction')
                for key in ('afterSha256','afterContentSha256'):
                    need(type(row[key]) is str and HEX.fullmatch(row[key]),'invalid_restore_transaction')
                for key in ('beforeSha256','beforeContentSha256'):
                    need((type(row[key]) is str and HEX.fullmatch(row[key])) if row['hadOriginal'] else row[key] is None,'invalid_restore_transaction')
                target=self.state/name
                if target.exists() or target.is_symlink():
                    current=self._snapshot(target) if name.endswith('.sqlite') else regular(target)
                    need(self._content_digest(name,current) in (row['beforeContentSha256'],row['afterContentSha256']),'newer_state_preserved')
                else:need(not row['hadOriginal'],'original_state_missing')
                raw=regular(transaction/'before'/name) if row['hadOriginal'] else None
                need(raw is None or digest(raw)==row['beforeSha256'],'rollback_copy_changed')
                originals[name]=raw
            self._safe();self._hold('interrupted_restore_recovered',transaction.name)
            for name,raw in originals.items():self._replace_state(self.state/name,raw)
            journal['phase']='rolled_back';atomic(transaction/'JOURNAL.json',journal)
            return {'status':'rolled_back_held','automaticReplay':False,'nativeReconciliationRequired':True}

    def stage_release(self,manifest,signature,key_id,files):
        manifest_hash=verify_release(manifest,signature,key_id,files,self.trust,self._now())
        version=manifest['version'];destination=self.root/'releases'/version
        need(not destination.exists() and not destination.is_symlink(),'release_already_exists')
        work=directory(self.root/'releases'/('stage-'+secrets.token_hex(16)),create=True)
        for name,raw in files.items():write_new(work/'payload'/name,raw,0o400)
        write_new(work/'RELEASE.json',canonical({'manifest':manifest,'signature':signature,'keyId':key_id}),0o400)
        for path in sorted(work.rglob('*'),key=lambda p:len(p.parts),reverse=True):
            if path.is_dir():sync_directory(path);path.chmod(0o500)
        sync_directory(work);work.chmod(0o500);os.rename(work,destination);sync_directory(destination.parent)
        return {'version':version,'manifestSha256':manifest_hash,'status':'staged_signed','activationRequired':True}

    def verified_release(self,version,*,for_activation=False):
        need(type(version) is str and ID.fullmatch(version),'invalid_release_version')
        root=self.root/'releases'/version
        directory(root)
        need(not root.stat().st_mode&0o222,'release_not_immutable')
        directory(root/'payload');need(not (root/'payload').stat().st_mode&0o222,'release_not_immutable')
        need(not (root/'RELEASE.json').stat().st_mode&0o222,'release_not_immutable')
        envelope=strict_json(regular(root/'RELEASE.json'))
        need(type(envelope) is dict and set(envelope)=={'manifest','signature','keyId'},'invalid_release_manifest')
        files={}
        for path in (root/'payload').rglob('*'):
            need(not path.is_symlink(),'linked_release_path')
            need(not path.stat().st_mode&0o222,'release_not_immutable')
            if path.is_file():
                files[path.relative_to(root/'payload').as_posix()]=regular(path)
        h=verify_release(envelope['manifest'],envelope['signature'],envelope['keyId'],files,self.trust,self._now(),enforce_expiry=for_activation)
        need(envelope['manifest']['version']==version,'release_version_mismatch')
        return {'version':version,'manifestSha256':h,'root':str(root/'payload'),'manifest':envelope['manifest']}

    def _pointer(self):
        p=self.root/'current-release.json'
        if not p.exists() and not p.is_symlink():return None
        value=strict_json(regular(p))
        need(type(value) is dict and set(value)=={'schema','version','manifestSha256','highestSequence','previous','stateSchema','at'}
             and value['schema']=='decrypt.current-release.v1' and type(value['version']) is str and ID.fullmatch(value['version'])
             and type(value['manifestSha256']) is str and HEX.fullmatch(value['manifestSha256'])
             and type(value['highestSequence']) is int and value['highestSequence']>0 and type(value['stateSchema']) is int
             and value['stateSchema']==1 and type(value['at']) is int
             and (value['previous'] is None or type(value['previous']) is str and ID.fullmatch(value['previous'])),'invalid_release_pointer')
        return value

    def activate_release(self,version):
        with self._operation(flat=True):
            release=self.verified_release(version,for_activation=True);old=self._pointer()
            need(old is None or release['manifest']['sequence']>old['highestSequence'],'release_sequence_not_new')
            if old is None:self._installed()
            self._safe();self._hold('release_restart_requires_native_reconciliation')
            pointer={'schema':'decrypt.current-release.v1','version':version,'manifestSha256':release['manifestSha256'],
                     'highestSequence':release['manifest']['sequence'],'previous':old['version'] if old else 'installed',
                     'stateSchema':1,'at':self._now()}
            atomic(self.root/'current-release.json',pointer)
            return {'status':'activated_for_restart','version':version,'restartRequired':True,'automaticReplay':False}

    def _installed(self):
        need(self.installed_release is not None,'installed_baseline_unavailable')
        value=self.installed_release()
        need(type(value) is dict and value.get('kind')=='installed' and value.get('version')=='installed'
             and type(value.get('manifestSha256')) is str and HEX.fullmatch(value['manifestSha256']),'installed_baseline_invalid')
        return value

    def rollback_release(self):
        with self._operation(flat=True):
            old=self._pointer();need(old and old.get('previous'),'no_previous_release')
            if old['previous']=='installed':release=self._installed()
            else:
                release=self.verified_release(old['previous']);need(release['manifest']['stateSchema']==old['stateSchema'],'state_schema_rollback_unsupported')
            self._safe();self._hold('release_rollback_requires_native_reconciliation')
            atomic(self.root/'current-release.json',{**old,'version':release['version'],'manifestSha256':release['manifestSha256'],
                                                   'previous':None,'at':self._now()})
            return {'status':'rolled_back_for_restart','version':release['version'],'ledgersRewound':False,'restartRequired':True}

    def offboard(self,confirmation):
        need(confirmation=='REMOVE SAVED SIGN-INS','offboarding_confirmation_required')
        with self._operation(flat=True):
            removed=[]
            # Prevalidate before any deletion; a bad later path must not partially remove credentials.
            for name in sorted(CREDENTIAL_FILES):
                path=self.state/name
                if path.exists() or path.is_symlink():regular(path);removed.append(name)
            monitor=self.state/'guardian/monitor.json'
            config=strict_json(regular(monitor)) if monitor.exists() or monitor.is_symlink() else None
            need(config is None or type(config) is dict,'invalid_monitor_config')
            self._safe();self._hold('customer_offboarded')
            for name in removed:(self.state/name).unlink()
            for parent in {(self.state/name).parent for name in removed}:sync_directory(parent)
            if config is not None:
                config['enabled']=False;atomic(monitor,config)
            return {'status':'local_credentials_removed_held','removedFiles':removed,'ledgersPreserved':True,
                    'browserSignoutRequired':True,'providerRevocationRequired':True,
                    'encryptedBackupsPreserved':True}
