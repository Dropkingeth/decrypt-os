#!/usr/bin/env python3
"""Materialize reviewed runtime bytes; never install, extract or execute packages."""
from __future__ import annotations
import argparse
import base64
import ctypes
import hashlib
import types
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import urllib.parse

PROVISIONER_SHA256='59137d69aa49e16d27a7c76a795f1597411af18af156cb482f78f28d63392233'
RATE=20*1024**2
MAX_ARCHIVE=512*1024**2
NODE_URL='https://nodejs.org/dist/v24.19.0/node-v24.19.0-linux-arm64.tar.xz'
# One exact Node archive URL per image architecture (provisioner NODE_NAME).
NODE_URLS={'node-v24.19.0-linux-arm64':NODE_URL,
           'node-v24.19.0-linux-x64':'https://nodejs.org/dist/v24.19.0/node-v24.19.0-linux-x64.tar.xz'}
HEX=re.compile(r'[0-9a-f]{64}')
TAG=re.compile(r'(?:cp[0-9]+t?|py[0-9]+)-(?:cp[0-9]+t?|abi3|none)-(?:manylinux(?:_[0-9]+_[0-9]+|2014|2010|1)_aarch64|linux_aarch64|any)')
TAG_X86_64=re.compile(r'(?:cp[0-9]+t?|py[0-9]+)-(?:cp[0-9]+t?|abi3|none)-(?:manylinux(?:_[0-9]+_[0-9]+|2014|2010|1)_x86_64|linux_x86_64|any)')
# Native target report "platform" -> (accepted wheel tags, machine name in soabi/tags).
PLATFORMS={'linux-aarch64':(TAG,'aarch64'),'linux-x86_64':(TAG_X86_64,'x86_64')}

class Refused(ValueError):pass

def need(value,reason):
    if not value:raise Refused(reason)

def sha(raw):return hashlib.sha256(raw).hexdigest()

def canonical(value):return (json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode()

def strict(raw):
    def pairs(rows):
        value={}
        for key,item in rows:
            need(key not in value,'duplicate_json_field');value[key]=item
        return value
    def invalid(_):raise Refused('nonfinite_json')
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=invalid)

def absolute(path):
    path=Path(path)
    need(path.is_absolute() and '..' not in path.parts,'absolute_normalized_path_required')
    return path

def chain(path):
    for item in (Path(path),*Path(path).parents):
        info=item.lstat();need(not stat.S_ISLNK(info.st_mode),'linked_input_path')

def identity(info):return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)

def read(path,expected=None,maximum=4*1024**2):
    path=absolute(path);chain(path);before=path.lstat()
    need(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_size<=maximum,'bounded_regular_input_required')
    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as stream:
        raw=stream.read(maximum+1);after=os.fstat(stream.fileno())
    need(identity(before)==identity(after) and len(raw)==before.st_size,'input_changed')
    need(expected is None or type(expected) is str and HEX.fullmatch(expected) and sha(raw)==expected,'input_pin_mismatch')
    return raw

def sync_dir(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)

def write(path,raw):
    path=absolute(path);chain(path.parent)
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600),'wb') as out:
        os.fchmod(out.fileno(),0o600);out.write(raw);out.flush();os.fsync(out.fileno())
    sync_dir(path.parent)

def load_provisioner(path):
    path=absolute(path);raw=read(path,PROVISIONER_SHA256,512*1024)
    module=types.ModuleType('_reviewed_runtime_provisioner');module.__file__=str(path)
    exec(compile(raw,str(path),'exec'),module.__dict__)
    return module

def public_url(value,host):
    need(type(value) is str and len(value)<=4096,'public_url_required')
    parsed=urllib.parse.urlsplit(value)
    need(parsed.scheme=='https' and parsed.hostname==host and not parsed.username and not parsed.password
         and not parsed.port and not parsed.query and not parsed.fragment and parsed.path.startswith('/')
         and not any(ord(c)<33 or ord(c)>126 for c in value),'unapproved_public_url')
    return value

def sri(value):
    need(type(value) is str and re.fullmatch(r'sha(?:256|512)-[A-Za-z0-9+/]+={0,2}',value),'locked_integrity_required')
    algorithm,encoded=value.split('-',1);raw=base64.b64decode(encoded,validate=True)
    need(len(raw)==hashlib.new(algorithm).digest_size and base64.b64encode(raw).decode()==encoded,'noncanonical_integrity')
    return algorithm,raw.hex()

def target_tags(report):
    need(type(report) is dict and report.get('schema')=='decrypt.runtime-target-python.v1'
         and report.get('execution')=='native-target-observation' and report.get('platform') in PLATFORMS,'native_target_python_report_required')
    pattern,machine=PLATFORMS[report['platform']]
    value=report.get('targetPythonIdentity')
    need(type(value) is dict and set(value)=={'version','cacheTag','soabi'},'target_python_identity_shape')
    version=value['version']
    need(type(version) is list and len(version)==3 and all(type(v) is int and v>=0 for v in version)
         and version[0]==3 and version[1]>=10,'target_python_version')
    need(value['cacheTag']=='cpython-'+str(version[0])+str(version[1])
         and type(value['soabi']) is str and re.fullmatch(r'cpython-'+str(version[0])+str(version[1])+r't?-'+machine+r'-linux-gnu',value['soabi']), 'target_python_abi')
    tags=report.get('supportedTags')
    need(report.get('packagingAvailable') is True and type(tags) is list and 0<len(tags)<=5000
         and all(type(v) is str and pattern.fullmatch(v) for v in tags) and len(set(tags))==len(tags),'native_supported_tags_required')
    # A caller cannot combine one Python identity with another interpreter's tags.
    native='cp'+str(version[0])+str(version[1])
    need(tags[0].split('-')[0] in (native,native+'t') and machine in tags[0],'target_tag_identity_mismatch')
    return value,tags

def npm_rows(lock):
    need(type(lock) is dict and type(lock.get('lockfileVersion')) is int and lock['lockfileVersion']==3
         and type(lock.get('packages')) is dict and '' in lock['packages'],'npm_lock_v3_required')
    rows=[]
    for name,row in lock['packages'].items():
        if name=='':continue
        need(type(name) is str and name.startswith('node_modules/') and all(p not in ('','.','..') for p in name.split('/'))
             and '\\' not in name and type(row) is dict and row.get('link') is not True,'npm_package_path')
        if row.get('inBundle') is True:
            # Shipped inside its parent's own tarball (e.g. npm's dependency
            # tree); never fetched or cached separately, so it has no
            # resolved/integrity of its own to pin here.
            continue
        url=public_url(row.get('resolved'),'registry.npmjs.org');algorithm,expected=sri(row.get('integrity'))
        rows.append({'path':'npm-archives/'+sha(url.encode())+'.tgz','role':'npm','url':url,
                     'algorithm':algorithm,'digest':expected,'expectedBytes':None,'maximumBytes':MAX_ARCHIVE,'retainedRelative':None})
    return rows

def make_plan(provisioner,inputs,inputs_sha,public_path,public_sha,target_path,target_sha,additional_locks=(),maximum_total_bytes=8*1024**3,refetch_public_runtime=False):
    need(type(refetch_public_runtime) is bool,'explicit_public_refetch_flag')
    need(type(maximum_total_bytes) is int and 1024**3<=maximum_total_bytes<=32*1024**3,'total_download_budget')
    inputs=absolute(inputs);chain(inputs)
    records={name:sha(read(inputs/name,maximum=3*1024**2)) for name in provisioner.INPUT_FILES}
    aggregate=sha(json.dumps(records,sort_keys=True,separators=(',',':')).encode())
    need(type(inputs_sha) is str and HEX.fullmatch(inputs_sha) and aggregate==inputs_sha,'runtime_metadata_aggregate_mismatch')
    need(records['npm/package-lock.json']==provisioner.NPM_LOCK_HASH,'runtime_npm_lock_changed')
    lock=strict(read(inputs/'npm/package-lock.json',records['npm/package-lock.json']))
    need(lock['packages']['']['dependencies']=={'openclaw':'2026.9.9'},'runtime_npm_root_changed')
    need(strict(read(inputs/'npm/package.json',records['npm/package.json']))['dependencies']=={'openclaw':'2026.9.9'},'runtime_package_root_changed')
    public=strict(read(public_path,public_sha))
    need(public.get('schema')=='decrypt.installer-v2.public-inputs.v1' and type(public.get('files')) is list,'public_inputs_manifest_required')
    public_files={}
    for row in public['files']:
        need(type(row) is dict and set(row)=={'destination','bytes','sha256','role'} and type(row['destination']) is str
             and row['destination'] not in public_files and type(row['bytes']) is int and 0<row['bytes']<=MAX_ARCHIVE
             and type(row['sha256']) is str and HEX.fullmatch(row['sha256']),'public_archive_row')
        public_files[row['destination']]=row
    report=strict(read(target_path,target_sha));python_id,tags=target_tags(report)
    # The Node archive and wheels must be for the same machine as the target Python.
    need(report['platform']=='linux-'+getattr(provisioner,'TARGET_ARCH','aarch64') and provisioner.NODE_NAME in NODE_URLS,'target_architecture_mismatch')
    metadata={name:strict(read(inputs/(name+'.json'),records[name+'.json'])) for name in provisioner.VERSIONS}
    for name,value in metadata.items():
        need(value.get('name')==name and value.get('version')==provisioner.VERSIONS[name] and type(value.get('files')) is list,'python_metadata_version')
        for row in value['files']:
            public_url(row.get('url'),'files.pythonhosted.org')
            need(type(row.get('sha256')) is str and HEX.fullmatch(row['sha256']),'wheel_metadata_digest')
    selected=provisioner.select_wheels(metadata,tags)
    openclaw_meta=strict(read(inputs/'openclaw.json',records['openclaw.json']))
    need(openclaw_meta.get('name')=='openclaw' and openclaw_meta.get('version')=='2026.9.9','openclaw_metadata_version')
    openclaw_url=public_url(openclaw_meta['dist']['tarball'],'registry.npmjs.org')
    need(sri(openclaw_meta['dist']['integrity'])==('sha512',provisioner.OPENCLAW_SHA512),'openclaw_metadata_integrity')
    if refetch_public_runtime:
        node_metadata=strict(read(inputs/'node.json',records['node.json']))
        need(node_metadata.get('name')=='node' and node_metadata.get('source')=='https://nodejs.org/dist/v24.19.0/SHASUMS256.txt'
             and node_metadata.get('files')==[{'filename':provisioner.NODE_NAME+'.tar.xz','sha256':provisioner.NODE_HASH}], 'node_refetch_metadata_binding')
    archives=[]
    for name,algorithm,expected in ((provisioner.NODE_NAME+'.tar.xz','sha256',provisioner.NODE_HASH),('openclaw-2026.9.9.tgz','sha512',provisioner.OPENCLAW_SHA512)):
        relative='archives/'+name;row=public_files.get('dependencies/'+relative)
        need(row is not None and row['role']=='required-runtime','retained_archive_public_pin_missing')
        if algorithm=='sha256':need(row['sha256']==expected,'node_public_pin_mismatch')
        url=(NODE_URLS[provisioner.NODE_NAME] if algorithm=='sha256' else openclaw_url) if refetch_public_runtime else None
        archives.append({'path':relative,'role':'pinned-runtime' if refetch_public_runtime else 'retained-runtime','url':url,'algorithm':algorithm,'digest':expected,
                         'sha256':row['sha256'],'expectedBytes':row['bytes'],'maximumBytes':row['bytes'],'retainedRelative':None if refetch_public_runtime else relative})
    npm=npm_rows(lock);additional=[]
    for path,pin in additional_locks:
        rows=npm_rows(strict(read(path,pin,3*1024**2)));npm.extend(rows);additional.append(pin)
    need(len(set(additional))==len(additional),'duplicate_additional_lock')
    unique={}
    for row in npm:
        old=unique.get(row['url'])
        need(old is None or (old['algorithm'],old['digest'])==(row['algorithm'],row['digest']),'conflicting_npm_integrity')
        unique[row['url']]=row
    for row in selected:
        name=row['filename']
        need(type(name) is str and PurePosixPath(name).name==name and '/' not in name and '\\' not in name and name.endswith('.whl'),'wheel_basename')
        archives.append({'path':'wheels/'+name,'role':'wheel','url':row['url'],'algorithm':'sha256','digest':row['sha256'],
                         'expectedBytes':None,'maximumBytes':MAX_ARCHIVE,'retainedRelative':None})
    # Reuse retained OpenClaw for its matching npm record; still verify the exact SRI.
    if openclaw_url in unique:
        row=unique[openclaw_url]
        need((row['algorithm'],row['digest'])==('sha512',provisioner.OPENCLAW_SHA512),'openclaw_npm_integrity')
        source=archives[1];row.update(retainedRelative=source['retainedRelative'],preparedRelative=source['path'] if refetch_public_runtime else None,
                                    expectedBytes=source['expectedBytes'],maximumBytes=source['maximumBytes'],sha256=source['sha256'])
    archives.extend(unique.values());archives.sort(key=lambda row:row['path'])
    for row in archives:row.setdefault('preparedRelative',None)
    need(len({r['path'] for r in archives})==len(archives),'duplicate_archive_path')
    return {'schema':'decrypt.runtime-fetch-plan.v1','fetcherSha256':sha(read(Path(__file__).absolute(),maximum=512*1024)),'provisionerSha256':PROVISIONER_SHA256,'metadataSha256':aggregate,'metadataFiles':records,
            'publicInputsSha256':public_sha,'targetObservationSha256':target_sha,'targetPythonIdentity':python_id,
            'runtimeArchiveMode':'explicit-public-refetch' if refetch_public_runtime else 'retained-copy',
            'additionalNpmLockSha256':sorted(additional),'selectedWheels':[{'filename':r['filename'],'sha256':r['sha256']} for r in selected],
            'archives':archives,'maximumTotalBytes':maximum_total_bytes,'rateBytesPerSecond':RATE,'parallelWorkers':1,'retryCount':0,'lifecycleScriptsExecuted':False,
            'nativeRuntimeBuildVerified':False,'signatureVerification':False}

def check_archive(path,row):
    path=absolute(path);chain(path);before=path.stat()
    need(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and 0<before.st_size<=row['maximumBytes']
         and (row['expectedBytes'] is None or before.st_size==row['expectedBytes']),'archive_size_mismatch')
    digests={name:hashlib.new(name) for name in set((row['algorithm'],'sha256'))};total=0;started=time.monotonic()
    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as stream:
        while block:=stream.read(1024**2):
            total+=len(block);need(total<=row['maximumBytes'],'archive_exceeds_size_bound')
            for value in digests.values():value.update(block)
            time.sleep(max(0,total/RATE-(time.monotonic()-started)))
        after=os.fstat(stream.fileno())
    need(identity(before)==identity(after)==identity(path.stat()) and total==before.st_size,'archive_changed')
    need(digests[row['algorithm']].hexdigest()==row['digest'] and (not row.get('sha256') or digests['sha256'].hexdigest()==row['sha256']),'archive_integrity_mismatch')
    return {'path':row['path'],'role':row['role'],'bytes':total,'sha256':digests['sha256'].hexdigest(),
            'integrityAlgorithm':row['algorithm'],'integrityDigest':row['digest'],'sizeBinding':'origin-pinned' if row['expectedBytes'] is not None else 'measured-bounded-hash-verified'}

def copy_archive(source,destination,row):
    source=absolute(source);chain(source);before=source.stat()
    need(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_size==row['expectedBytes'],'retained_archive_size')
    started=time.monotonic();total=0
    with os.fdopen(os.open(source,os.O_RDONLY|os.O_NOFOLLOW),'rb') as src, os.fdopen(os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600),'wb') as out:
        while block:=src.read(1024**2):
            total+=len(block);need(total<=row['maximumBytes'],'retained_archive_grew');out.write(block)
            time.sleep(max(0,total/RATE-(time.monotonic()-started)))
        out.flush();os.fsync(out.fileno());after=os.fstat(src.fileno())
    need(identity(before)==identity(after)==identity(source.stat()),'retained_archive_changed');sync_dir(destination.parent)

def curl_argv(binary,url,maximum):
    host=urllib.parse.urlsplit(url).hostname
    need(host in ('registry.npmjs.org','files.pythonhosted.org','nodejs.org'),'download_host_not_allowed');public_url(url,host)
    need(host!='nodejs.org' or url in NODE_URLS.values(),'node_refetch_url_not_pinned')
    return [str(binary),'--disable','--fail','--silent','--show-error','--proto','=https','--proto-redir','=https',
            '--connect-timeout','15','--max-time','1200','--max-filesize',str(maximum),'--limit-rate','20M',
            '--retry','0','--noproxy','*','--url',url]

def stop(process):
    if process.poll() is None:
        try:os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        try:process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.wait(timeout=5)

def child_parent_death(prctl,parent_pid):
    # Only the forked exact curl child runs this, before exec. No generic PID signal.
    if prctl(1,signal.SIGKILL,0,0,0) != 0:
        os._exit(126)
    if os.getppid() != parent_pid:
        os._exit(125)

def curl_containment(binary):
    need(sys.platform.startswith('linux'),'native_fetch_requires_linux')
    raw=read(binary,maximum=128*1024**2);info=Path(binary).stat()
    need(raw.startswith(b'\x7fELF') and not info.st_mode & (stat.S_ISUID|stat.S_ISGID),'curl_must_be_unprivileged_native_elf')
    # Privileged exec can clear PDEATHSIG; capability absence must be observed.
    need('security.capability' not in os.listxattr(binary,follow_symlinks=False),'curl_file_capabilities_refused')
    try:
        library=ctypes.CDLL(None,use_errno=True);prctl=library.prctl
        prctl.argtypes=[ctypes.c_int,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong]
        prctl.restype=ctypes.c_int
    except (AttributeError,OSError):raise Refused('parent_death_signal_unavailable') from None
    parent_pid=os.getpid()
    return lambda:child_parent_death(prctl,parent_pid)

def curl_download(binary,destination,row):
    # Curl cannot follow redirects, read user configuration, use credentials, resume or retry.
    process=subprocess.Popen(curl_argv(binary,row['url'],row['maximumBytes']),stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,
                             preexec_fn=curl_containment(binary),env={'PATH':'/usr/bin:/bin','LC_ALL':'C'})
    selector=selectors.DefaultSelector();selector.register(process.stdout,selectors.EVENT_READ,'body');selector.register(process.stderr,selectors.EVENT_READ,'error')
    size=0;stderr_bytes=0;started=time.monotonic()
    try:
        with os.fdopen(os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600),'wb') as out:
            while selector.get_map():
                need(time.monotonic()-started<=1210,'download_deadline')
                for key,_ in selector.select(timeout=0.25):
                    block=os.read(key.fileobj.fileno(),65536)
                    if not block:selector.unregister(key.fileobj);continue
                    if key.data=='body':
                        size+=len(block);need(size<=row['maximumBytes'],'download_exceeds_size_bound');out.write(block)
                    else:
                        stderr_bytes+=len(block);need(stderr_bytes<=65536,'download_stderr_unbounded')
            need(process.wait(timeout=5)==0 and size>0,'download_failed');out.flush();os.fsync(out.fileno())
        sync_dir(destination.parent)
    finally:
        stop(process);selector.close();process.stdout.close();process.stderr.close()

def fetch(plan,inputs,output,curl_path,curl_sha,download=curl_download):
    inputs=absolute(inputs);output=absolute(output);chain(output.parent)
    need(not os.path.lexists(output),'new_output_required')
    binary=absolute(curl_path);read(binary,curl_sha,128*1024**2);need(os.access(binary,os.X_OK),'curl_not_executable')
    free=os.statvfs(output.parent)
    reserved=plan['maximumTotalBytes']+1024**3
    need(sum(r['expectedBytes'] or 0 for r in plan['archives'])<=plan['maximumTotalBytes'],'known_archives_exceed_budget')
    # Reserve the explicit whole-fetch cap, not one worst-case cap per URL.
    need(free.f_bavail*free.f_frsize>=reserved,'insufficient_space_for_bounded_inputs')
    output.mkdir(mode=0o700);sync_dir(output.parent)
    write(output/'FETCH-PLAN.json',canonical(plan));results=[];total=0
    try:
        for name,pin in plan['metadataFiles'].items():
            destination=output/name;destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            write(destination,read(inputs/name,pin,3*1024**2))
        for original_row in plan['archives']:
            remaining=plan['maximumTotalBytes']-total
            need(remaining>0,'total_download_budget_exhausted')
            row={**original_row,'maximumBytes':min(original_row['maximumBytes'],remaining)}
            need(row['expectedBytes'] is None or row['expectedBytes']<=row['maximumBytes'],'archive_exceeds_remaining_budget')
            destination=output/row['path'];destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            read(binary,curl_sha,128*1024**2)
            if row['preparedRelative'] is not None:copy_archive(output/row['preparedRelative'],destination,row)
            elif row['retainedRelative'] is not None:copy_archive(inputs/row['retainedRelative'],destination,row)
            else:download(binary,destination,row)
            verified=check_archive(destination,row);results.append(verified);total+=verified['bytes']
        receipt={'schema':'decrypt.runtime-fetch-receipt.v1','status':'passed','planSha256':sha(canonical(plan)),
                 'metadataSha256':plan['metadataSha256'],'targetObservationSha256':plan['targetObservationSha256'],
                 'runtimeArchiveMode':plan['runtimeArchiveMode'],
                 'targetPythonIdentity':plan['targetPythonIdentity'],'additionalNpmLockSha256':plan['additionalNpmLockSha256'],
                 'curlSha256':curl_sha,'maximumTotalBytes':plan['maximumTotalBytes'],'actualTotalBytes':total,'rateBytesPerSecond':RATE,'parallelWorkers':1,'retryCount':0,
                 'archives':results,'dependencyBytesVerified':True,'nativeRuntimeBuildVerified':False,
                 'lifecycleScriptsExecuted':False,'signatureVerification':False}
        write(output/'FETCH-RECEIPT.json',canonical(receipt));return receipt
    except BaseException as error:
        write(output/'FETCH-FAILURE.json',canonical({'schema':'decrypt.runtime-fetch-failure.v1','status':'failed',
              'planSha256':sha(canonical(plan)),'completedArchives':len(results),'errorType':type(error).__name__,
              'partialOutputRetained':True,'automaticRetryAllowed':False,'nativeRuntimeBuildVerified':False}))
        raise

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    parser.add_argument('action',choices=('plan','fetch'))
    for flag in ('provisioner','inputs','public-inputs','target-python'):
        parser.add_argument('--'+flag,type=Path,required=True)
    for flag in ('inputs-sha256','public-inputs-sha256','target-python-sha256'):
        parser.add_argument('--'+flag,required=True)
    parser.add_argument('--additional-npm-lock',nargs=2,action='append',default=[],metavar=('PATH','SHA256'))
    parser.add_argument('--maximum-total-bytes',type=int,default=8*1024**3)
    parser.add_argument('--refetch-public-runtime',action='store_true')
    parser.add_argument('--plan-output',type=Path);parser.add_argument('--plan',type=Path);parser.add_argument('--plan-sha256')
    parser.add_argument('--output',type=Path);parser.add_argument('--curl',type=Path);parser.add_argument('--curl-sha256')
    args=parser.parse_args(argv)
    try:
        provisioner=load_provisioner(args.provisioner)
        plan=make_plan(provisioner,args.inputs,args.inputs_sha256,args.public_inputs,args.public_inputs_sha256,
                       args.target_python,args.target_python_sha256,args.additional_npm_lock,args.maximum_total_bytes,args.refetch_public_runtime)
        if args.action=='plan':
            need(args.plan_output is not None,'new_plan_output_required');write(args.plan_output,canonical(plan))
            result={'status':'planned','planSha256':sha(canonical(plan)),'archives':len(plan['archives']),
                    'selectedWheels':plan['selectedWheels'],'nativeRuntimeBuildVerified':False}
        else:
            need(args.plan is not None and type(args.plan_sha256) is str and HEX.fullmatch(args.plan_sha256),'accepted_plan_required')
            need(strict(read(args.plan,args.plan_sha256,8*1024**2))==plan,'fetch_plan_changed')
            need(args.output is not None and args.curl is not None and type(args.curl_sha256) is str and HEX.fullmatch(args.curl_sha256),'fetch_output_and_pinned_curl_required')
            receipt=fetch(plan,args.inputs,args.output,args.curl,args.curl_sha256)
            result={'status':'passed','receiptSha256':sha(canonical(receipt)),'archives':len(receipt['archives']),
                    'nativeRuntimeBuildVerified':False}
        print(json.dumps(result,sort_keys=True));return 0
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as error:
        # Paths belong only to the caller's private command line, not distributable receipts.
        print(json.dumps({'status':'refused','errorType':type(error).__name__,'nativeRuntimeBuildVerified':False}),file=sys.stderr);return 1
if __name__=='__main__':raise SystemExit(main())
