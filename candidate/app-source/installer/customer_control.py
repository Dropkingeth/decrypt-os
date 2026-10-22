#!/usr/bin/python3
"""Fixed privileged entry for the customer runtime, never a command runner."""
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import stat
import sys

INSTALL = Path('/usr/local/lib/decrypt-os')
ACTIONS = frozenset(('prepare', 'status', 'start', 'stop', 'verify', 'login', 'console', 'ict', 'storage_setup', 'storage_unlock'))

def validate_action(args):
    if len(args) != 1 or args[0] not in ACTIONS:
        raise ValueError('This customer action is not available.')
    return args[0]

def trusted_file(path):
    for item in (path, *path.parents):
        st = item.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise ValueError('Customer installation ownership needs attention.')
    if not path.is_file():
        raise ValueError('Customer installation is incomplete.')

def read_trusted_json(path):
    trusted_file(path)
    info=path.stat()
    if info.st_nlink!=1 or not 0<info.st_size<=2*1024**2:
        raise ValueError('Invalid instance authorization file.')
    def pairs(items):
        out={}
        for key,value in items:
            if key in out:raise ValueError('Duplicate authorization field.')
            out[key]=value
        return out
    return json.loads(path.read_text(),object_pairs_hook=pairs)

def validate_instance(policy,state,enrollment,receipt,machine):
    fields={'schema','machineSha256','instanceId','imageId','policySha256','accountName','desktopUid','sourceManifestSha256'}
    if type(policy) is not dict or set(policy)!=fields or policy['schema']!='decrypt.customer-host-policy.v2':
        raise ValueError('First-start instance authorization is required.')
    import uuid
    try:
        if str(uuid.UUID(policy['instanceId']))!=policy['instanceId'] or not uuid.UUID(policy['instanceId']).int:raise ValueError('Invalid instance.')
    except (ValueError,TypeError,AttributeError):raise ValueError('Invalid first-start instance.')
    if not re.fullmatch('[0-9a-f]{32}',machine) or int(machine,16)==0 or policy['machineSha256']!=hashlib.sha256(machine.encode()).hexdigest():
        raise ValueError('Instance machine differs.')
    if any(type(policy[k]) is not str or not re.fullmatch('[0-9a-f]{64}',policy[k]) for k in ('machineSha256','policySha256','sourceManifestSha256')):
        raise ValueError('Invalid authorization digest.')
    if type(policy['accountName']) is not str or not re.fullmatch('[a-z_][a-z0-9_-]{0,31}',policy['accountName']) or policy['accountName']=='root' or type(policy['desktopUid']) is not int or not 1000<=policy['desktopUid']<65534:
        raise ValueError('Invalid instance account.')
    if type(policy['imageId']) is not str or not re.fullmatch('[a-z0-9][a-z0-9._-]{0,95}',policy['imageId']):
        raise ValueError('Invalid image identity.')
    if type(state) is not dict or set(state)!=set('schema imageId policySha256 instanceId phase status seq accountName notice completedAt failure'.split()) or state['failure'] is not None or type(state['seq']) is not int or state['seq']<0:
        raise ValueError('Invalid first-start state.')
    notice=state['notice']
    if type(notice) is not dict or set(notice)!={'documentSha256','accepted','acceptedAt'} or type(notice['documentSha256']) is not str or not re.fullmatch('[a-f0-9]{64}',notice['documentSha256']) or type(notice['accepted']) is not bool:
        raise ValueError('Invalid first-start notice.')
    if (state['phase']=='notice' and (notice['accepted'] or notice['acceptedAt'] is not None or state['completedAt'] is not None)) or (state['phase']=='complete' and (not notice['accepted'] or not notice['acceptedAt'] or state['completedAt']!=notice['acceptedAt'])):
        raise ValueError('First-start notice state differs.')
    if (state.get('schema')!='decrypt.firstboot-state.v1' or state.get('phase') not in ('notice','complete')
        or (state['phase'],state.get('status')) not in (('notice','pending'),('complete','complete'))
        or any(state.get(k)!=policy[k] for k in ('instanceId','imageId','policySha256','accountName'))):
        raise ValueError('First-start state differs.')
    shared={'instanceId':policy['instanceId'],'imageId':policy['imageId'],'policySha256':policy['policySha256'],
            'desktopUser':policy['accountName'],'desktopUid':policy['desktopUid'],'sourceManifestSha256':policy['sourceManifestSha256']}
    if enrollment.get('schema')!='decrypt.customer-test.v1' or enrollment.get('role')!='clean-customer-test' or enrollment.get('machineSha256')!=policy['machineSha256'] or any(enrollment.get(k)!=v for k,v in shared.items()):
        raise ValueError('Customer enrollment differs.')
    if receipt.get('schema')!='decrypt.firstboot-install-receipt.v1' or receipt.get('installed') is not True or receipt.get('stageOnly') is not False or receipt.get('targetMachineSha256')!=policy['machineSha256'] or any(receipt.get(k)!=v for k,v in shared.items()):
        raise ValueError('First-start install receipt differs.')
    return policy

def host_guard():
    machine_path=Path('/etc/machine-id');trusted_file(machine_path)
    machine=machine_path.read_text().strip()
    policy=read_trusted_json(Path('/etc/decrypt-os/instance-policy.json'))
    state=read_trusted_json(Path('/var/lib/decrypt-firstboot/state.json'))
    enrollment=read_trusted_json(Path('/etc/decrypt-os/customer-test.json'))
    receipt=read_trusted_json(INSTALL/'INSTALL-RECEIPT.json')
    validate_instance(policy,state,enrollment,receipt,machine)
    manifest=INSTALL/'SOURCE-MANIFEST.json';trusted_file(manifest)
    if hashlib.sha256(manifest.read_bytes()).hexdigest()!=policy['sourceManifestSha256']:
        raise ValueError('Instance source manifest differs.')

def main(args=None):
    action = validate_action(sys.argv[1:] if args is None else args)
    if os.geteuid() != 0:
        raise ValueError('Use the installed customer launcher.')
    host_guard()
    script = INSTALL / 'app/harness/runtime.py'
    trusted_file(script)
    # -I is required by the exact sudoers rule. Discard caller environment anyway.
    os.environ.clear()
    os.environ.update(PATH='/usr/bin:/bin', LANG='C.UTF-8', TERM='xterm-256color')
    os.chdir('/')
    sys.path[:] = [p for p in sys.path if p and not p.startswith('/home/')]
    spec = importlib.util.spec_from_file_location('decrypt_customer_runtime', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.argv = [str(script), action]
    return module.main()

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError):
        print('Customer action is unavailable. Check the local installation.', file=sys.stderr)
        raise SystemExit(1)
