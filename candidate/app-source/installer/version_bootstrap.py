#!/usr/bin/env python3
"""Trusted fixed-path loader; one verified release serves both API and UI.

Installed by the root installer, never loaded from a mutable update. No downloads,
keys from environment, service mutation, shell, or automatic recovery of ledgers.
"""
from __future__ import annotations
import argparse
import base64
import importlib.util
import os
from pathlib import Path
import runpy
import stat
import sys
import time

INSTALL=Path('/usr/local/lib/decrypt-os')
PYTHON=Path('/usr/local/lib/decrypt-os-runtime/python/bin/python')
TRUST=INSTALL/'release-trust.json'

def load_module(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

# During tests the source module is imported explicitly. Shipping main ignores
# PYTHONPATH and imports this one reviewed root-owned installed implementation.
def trusted_file(path,maintenance):
    for p in (Path(path),*Path(path).parents):
        info=p.lstat()
        maintenance.need(not stat.S_ISLNK(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022,'installed_root_not_trusted')
    return maintenance.regular(path)

def load_trust(path,maintenance,reader=trusted_file):
    value=maintenance.strict_json(reader(path,maintenance))
    maintenance.need(type(value) is dict and set(value)=={'schema','keys'} and value['schema']=='decrypt.release-trust.v1'
                     and type(value['keys']) is dict and len(value['keys'])<=8,'invalid_release_trust')
    result={}
    for name,encoded in value['keys'].items():
        maintenance.need(type(name) is str and maintenance.ID.fullmatch(name) and type(encoded) is str,'invalid_release_trust')
        try:raw=base64.b64decode(encoded,validate=True)
        except ValueError:raise maintenance.MaintenanceError('invalid_release_trust') from None
        maintenance.need(len(raw)==32,'invalid_release_trust');result[name]=raw
    return result

def installed_release(install,maintenance,reader=trusted_file):
    receipt_raw=reader(install/'INSTALL-RECEIPT.json',maintenance)
    receipt=maintenance.strict_json(receipt_raw)
    maintenance.need(type(receipt) is dict and receipt.get('installed') is True and receipt.get('stageOnly') is False,
                     'installed_receipt_invalid')
    files=receipt.get('files')
    if files is None and receipt.get('schema')=='decrypt.firstboot-install-receipt.v1':
        # Image installs: first start writes the account receipt; the file list
        # was rendered at build time next to it (root-owned, same trust checks).
        listing=maintenance.strict_json(reader(install/'INSTALLED-FILES.json',maintenance))
        maintenance.need(type(listing) is dict and listing.get('schema')=='decrypt.installed-files.v1','installed_receipt_invalid')
        files=listing.get('files')
    maintenance.need(type(files) is list,'installed_receipt_invalid')
    prefix='usr/local/lib/decrypt-os/';seen=set()
    for row in files:
        maintenance.need(type(row) is dict and type(row.get('path')) is str,'installed_receipt_invalid')
        if not row['path'].startswith(prefix):continue
        name=row['path'][len(prefix):]
        # systemd template units (name@.service) are the one shipped name form
        # maintenance.relative() does not accept.
        maintenance.relative(name.replace('@.','.'))
        maintenance.need(name not in seen,'installed_receipt_invalid');seen.add(name)
        raw=reader(install/name,maintenance)
        maintenance.need(type(row.get('bytes')) is int and len(raw)==row['bytes'] and maintenance.digest(raw)==row.get('sha256'),'installed_payload_changed')
    maintenance.need({'app/backend/server.py','app/ui/dist/index.html'}<=seen,'installed_receipt_incomplete')
    return {'kind':'installed','version':'installed','root':str(install),'manifestSha256':maintenance.digest(receipt_raw)}

def selected_release(manager,install,maintenance,reader=trusted_file):
    # A missing/invalid pointer never executes unverified code. The installed
    # package is independently verified, not merely a trusted pathname.
    try:
        pointer=manager._pointer()
        if pointer is not None:
            if pointer['version']=='installed':
                release=installed_release(install,maintenance,reader)
                maintenance.need(release['manifestSha256']==pointer['manifestSha256'],'release_pointer_mismatch')
                return release
            release=manager.verified_release(pointer['version'])
            maintenance.need(release['manifestSha256']==pointer['manifestSha256']
                and release['manifest']['sequence']<=pointer['highestSequence'],'release_pointer_mismatch')
            return {'kind':'signed','version':release['version'],'root':release['root'],'manifestSha256':release['manifestSha256']}
    except (maintenance.MaintenanceError,OSError,ValueError,KeyError,TypeError):
        manager._hold('invalid_release_fallback_requires_native_reconciliation')
    return installed_release(install,maintenance,reader)

def process_identity(pid,maintenance):
    maintenance.need(type(pid) is int and pid>0,'api_adoption_not_live')
    proc=Path('/proc')/str(pid)
    # Process stat command can contain spaces and parentheses; split after its
    # final ')'. starttime is field22 = index19 after that boundary.
    try:
        fields=(proc/'stat').read_text().rsplit(')',1)[1].split()
        argv=(proc/'cmdline').read_bytes().split(b'\0')
    except OSError:
        # The recorded API process has exited (for example a previous session).
        maintenance.need(False,'api_adoption_not_live')
    maintenance.need(str(INSTALL/'installer/version_bootstrap.py').encode() in argv
                     and any(argv[i:i+2]==[b'--component',b'api'] for i in range(len(argv)-1)),'api_adoption_not_live')
    maintenance.need((proc/'exe').resolve()==PYTHON.resolve(),'api_adoption_not_live')
    return {'pid':pid,'startTicks':int(fields[19]),'bootId':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}

def adopt_api(manager,release,maintenance,identity=process_identity):
    value={'schema':'decrypt.api-release-adoption.v1',**release,**identity(os.getpid(),maintenance)}
    maintenance.atomic(manager.root/'api-adoption.json',value)
    return value

def ui_release(manager,install,maintenance,identity=process_identity,reader=trusted_file):
    value=maintenance.strict_json(maintenance.regular(manager.root/'api-adoption.json'))
    maintenance.need(type(value) is dict and set(value)=={'schema','kind','version','root','manifestSha256','pid','startTicks','bootId'}
                     and value['schema']=='decrypt.api-release-adoption.v1','api_adoption_invalid')
    actual=identity(value['pid'],maintenance)
    maintenance.need(all(type(value[k]) is type(actual[k]) and value[k]==actual[k] for k in actual),'api_adoption_not_live')
    if value['kind']=='signed':
        release=manager.verified_release(value['version'])
        result={'kind':'signed','version':release['version'],'root':release['root'],'manifestSha256':release['manifestSha256']}
    else:
        maintenance.need(value['kind']=='installed','api_adoption_invalid')
        result=installed_release(install,maintenance,reader)
    maintenance.need(all(value[k]==result[k] for k in result),'api_adoption_changed')
    return result

def wait_for_ui_release(manager,install,maintenance,timeout=20,clock=time.monotonic,sleep=time.sleep,release=ui_release):
    # The UI unit starts as soon as the API process is spawned, before the API
    # records its adoption, so a missing or stale record is expected briefly.
    # Retrying never relaxes any check; the last refusal is raised on timeout.
    deadline=clock()+timeout
    while True:
        try:return release(manager,install,maintenance)
        except (maintenance.MaintenanceError,OSError):
            if clock()>=deadline:raise
            sleep(0.25)

def verify_running_api(manager,install,maintenance,server_file,service_file,*,
                       identity=process_identity,reader=trusted_file,modules=None,pid=None):
    """Verify this loaded API, full immutable payload and current selection.

    Internal paths come from the actual server class/module, never an API body.
    The installed maintenance kernel remains pinned across signed app updates.
    """
    pid=os.getpid() if pid is None else pid
    descriptor=maintenance.strict_json(maintenance.regular(manager.root/'api-adoption.json'))
    maintenance.need(type(descriptor) is dict and descriptor.get('pid')==pid,'reconciliation_adoption_unverified')
    adopted=ui_release(manager,install,maintenance,identity,reader)
    pointer=manager._pointer()
    if pointer is None:
        maintenance.need(adopted==installed_release(install,maintenance,reader),'reconciliation_adoption_unverified')
    else:
        maintenance.need(adopted['version']==pointer['version'] and adopted['manifestSha256']==pointer['manifestSha256'],
                         'reconciliation_adoption_unverified')
        if adopted['kind']=='signed':
            verified=manager.verified_release(adopted['version'])
            maintenance.need(verified['manifest']['sequence']<=pointer['highestSequence'],'reconciliation_adoption_unverified')
    root=Path(adopted['root'])
    maintenance.need(Path(server_file).absolute()==root/'app/backend/server.py'
                     and Path(service_file).absolute()==root/'app/backend/maintenance_service.py','reconciliation_adoption_unverified')
    modules=sys.modules if modules is None else modules
    for name,module in tuple(modules.items()):
        filename=getattr(module,'__file__',None)
        if not filename:continue
        path=Path(filename).absolute()
        if name=='maintenance':
            maintenance.need(path==install/'app/backend/maintenance.py','reconciliation_adoption_unverified')
        elif '/app/backend/' in str(path) or '/app/trading-runtime/' in str(path):
            maintenance.need(path.is_relative_to(root/'app'),'reconciliation_adoption_unverified')
    return adopted

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--component',choices=('api','ui'),required=True)
    parser.add_argument('--state-dir',type=Path,required=True)
    parser.add_argument('--enable-harness',action='store_true')
    parser.add_argument('--enable-guardian',action='store_true')
    parser.add_argument('--enable-trading',action='store_true')
    args=parser.parse_args()
    # Validate bootstrap's own immutable installation chain before any imports.
    for path in (INSTALL,INSTALL/'app',INSTALL/'app/backend',INSTALL/'app/backend/maintenance.py'):
        info=path.lstat()
        if path.is_symlink() or info.st_uid!=0 or info.st_mode&0o022:raise SystemExit('Installed maintenance loader is not trusted.')
    maintenance=load_module(INSTALL/'app/backend/maintenance.py','maintenance')
    sys.modules['maintenance']=maintenance
    trust=load_trust(TRUST,maintenance)
    manager=maintenance.Maintenance(args.state_dir,lambda:None,trusted_release_keys=trust)
    if args.component=='api':
        release=selected_release(manager,INSTALL,maintenance)
        adopt_api(manager,release,maintenance)
        backend=Path(release['root'])/'app/backend'
        sys.path.insert(0,str(backend))
        sys.argv=[str(backend/'server.py'),'--state-dir',str(args.state_dir)]
        for flag in ('harness','guardian','trading'):
            if getattr(args,'enable_'+flag):sys.argv.append('--enable-'+flag)
        runpy.run_path(str(backend/'server.py'),run_name='__main__')
    else:
        release=wait_for_ui_release(manager,INSTALL,maintenance)
        trusted_file(INSTALL/'installer/frontend.py',maintenance)
        frontend=load_module(INSTALL/'installer/frontend.py','trusted_frontend')
        httpd=frontend.ThreadingHTTPServer(('127.0.0.1',8811),frontend.Handler)
        httpd.dist=Path(release['root'])/'app/ui/dist'
        httpd.serve_forever()

if __name__=='__main__':main()
