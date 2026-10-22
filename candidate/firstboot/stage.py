#!/usr/bin/env python3
"""Pure first-start bundle preparation. The image build owns exclusive writes inside its guarded target."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import stat

HERE=Path(__file__).resolve().parent
UNIT='decrypt-firstboot.service'
spec=importlib.util.spec_from_file_location('firstboot_contract',HERE/'firstboot.py')
fb=importlib.util.module_from_spec(spec);spec.loader.exec_module(fb)


def read_source(path,maximum=131072):
    path=Path(path);info=path.lstat()
    fb.need(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and 0<info.st_size<=maximum,'unsafe-source')
    for parent in path.parents:fb.need(not parent.is_symlink(),'linked-source-parent')
    raw=path.read_bytes();fb.need(len(raw)==info.st_size,'changed-source')
    return raw


def build_bundle(policy_path,expected_sha256,image_id):
    fb.need(type(expected_sha256) is str and fb.HEX.fullmatch(expected_sha256),'policy-pin')
    fb.need(type(image_id) is str and re.fullmatch('[a-z0-9][a-z0-9._-]{0,95}',image_id),'image-id')
    raw=read_source(policy_path);fb.need(fb.digest(raw)==expected_sha256,'policy-pin-mismatch')
    policy=fb.policy_check(fb.strict_json(raw))
    source=read_source(HERE/'firstboot.py');fb.need(fb.digest(source)==policy['service']['sha256'],'service-pin-mismatch')
    unit=read_source(HERE/UNIT);gate=read_source(HERE/'login-gate.conf')
    fb.need(unit==fb.UNIT_BYTES and gate==fb.LOGIN_UNIT_BYTES,'unit-source-mismatch')
    notice=read_source(HERE/'NOTICE.txt');fb.need(fb.digest(notice)==policy['notice']['documentSha256'],'notice-pin-mismatch')
    config={'schema':'decrypt.firstboot-image.v1','imageId':image_id,'policySha256':expected_sha256}
    bundle={
        'usr/local/lib/decrypt-firstboot/firstboot.py':{'bytes':source,'mode':0o644},
        'etc/decrypt-firstboot/policy.json':{'bytes':raw,'mode':0o644},
        'etc/decrypt-firstboot/image.json':{'bytes':(json.dumps(config,sort_keys=True)+'\n').encode(),'mode':0o644},
        'usr/share/decrypt-firstboot/NOTICE.txt':{'bytes':notice,'mode':0o644},
        'etc/nologin':{'bytes':fb.GATE_BYTES,'mode':0o644},
        'etc/systemd/system/'+UNIT:{'bytes':unit,'mode':0o644},
    }
    # RuntimeDirectory is baked into the single unit file (fb.UNIT_BYTES) above,
    # not a stacked .service.d override: beta4 ships exactly one ExecStart per unit.
    for name in ('display-manager.service','systemd-user-sessions.service','sshd.service'):
        bundle['etc/systemd/system/'+name+'.d/10-decrypt-firstboot.conf']={'bytes':gate,'mode':0o644}
    # No state, machine identity, SSH keys, account or password is created here.
    return bundle
