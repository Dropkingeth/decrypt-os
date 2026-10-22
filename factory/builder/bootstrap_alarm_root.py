#!/usr/bin/env python3
"""Extract a separately pinned, signed builder root into a new builder directory.

--section alarm (default): the Arch Linux ARM rootfs from build/inputs.json, on
an aarch64 builder. --section archlinux: the Arch Linux x86_64 bootstrap
tarball from build/inputs-x86_64.json, on an x86_64 builder; its single
top-level directory (root.x86_64/) is stripped so the result has the same
layout as the ALARM root."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time


def require(ok,message):
    if not ok:raise ValueError(message)


def checked(path,pin):
    path=Path(path).absolute();require(path.resolve(strict=True)==path,'canonical input required')
    require(re.fullmatch('[a-f0-9]{64}',pin or ''),'SHA256 required')
    before=path.lstat();require(stat.S_ISREG(before.st_mode) and before.st_nlink==1,'regular independent input required')
    digest=hashlib.sha256();start=time.monotonic();count=0
    with path.open('rb') as stream:
        while block:=stream.read(1024*1024):
            digest.update(block);count+=len(block);time.sleep(max(0,count/(20*1024**2)-(time.monotonic()-start)))
    after=path.lstat();require((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)
                            and digest.hexdigest()==pin,'pinned input changed')
    return path


# Pinned-input section -> (builder machine, number of leading path components to strip).
SECTIONS={'alarm':('aarch64',0),'archlinux':('x86_64',1)}


def load_pins(path,section='alarm'):
    """The reviewed rootfs/signature/keyring sha256 and signing-key
    fingerprint from a build inputs file (build/inputs.json "alarm",
    build/inputs-x86_64.json "archlinux"), so an operator refilling the cache
    does not have to retype them from memory."""
    pins=json.loads(Path(path).read_text())[section]
    require(all(k in pins for k in ('rootfsSha256','signatureSha256','keyringSha256','signingKeyFingerprint')),
            f'{Path(path).name} "{section}" section is incomplete')
    return pins


def load_alarm_pins(path):
    """build/inputs.json's "alarm" section (see load_pins)."""
    return load_pins(path,'alarm')


def verify_signature(archive,signature,keyring,fingerprint,gpgv='gpgv'):
    require(re.fullmatch('[A-F0-9]{40}|[A-F0-9]{64}',fingerprint or ''),'full primary fingerprint required')
    result=subprocess.run([gpgv,'--keyring',str(keyring),'--status-fd','1',str(signature),str(archive)],capture_output=True,text=True,timeout=300)
    rows=[row.split() for row in result.stdout.splitlines()]
    bad={'BADSIG','ERRSIG','EXPSIG','EXPKEYSIG','REVKEYSIG','KEYEXPIRED','SIGEXPIRED','KEYREVOKED'}
    require(result.returncode==0 and not any(len(r)>1 and r[1] in bad for r in rows),'rootfs signature failed')
    valid=[r for r in rows if len(r)>=12 and r[:2]==['[GNUPG:]','VALIDSIG']]
    require(len(valid)==1 and valid[0][-1]==fingerprint,'unexpected rootfs signing identity')
    return {'signerFingerprint':valid[0][2],'primaryFingerprint':valid[0][-1]}


def main():
    p=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    for name in ('archive','signature','keyring'):
        p.add_argument('--'+name,type=Path,required=True);p.add_argument('--'+name+'-sha256')
    p.add_argument('--primary-fingerprint')
    p.add_argument('--inputs-json',type=Path,
                    help='build/inputs.json (or build/inputs-x86_64.json); supplies default --*-sha256/--primary-fingerprint '
                         'from its --section unless the flag below is also given')
    p.add_argument('--section',choices=tuple(SECTIONS),default='alarm',
                   help='alarm: Arch Linux ARM rootfs on an aarch64 builder (default); archlinux: Arch Linux x86_64 bootstrap on an x86_64 builder')
    p.add_argument('--build-root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--receipt',type=Path,required=True)
    a=p.parse_args();machine,strip=SECTIONS[a.section]
    require(os.geteuid()==0 and os.uname().sysname=='Linux' and os.uname().machine==machine,
            'disposable root Linux ARM builder required' if machine=='aarch64' else f'disposable root Linux {machine} builder required')
    pins=load_pins(a.inputs_json,a.section) if a.inputs_json else {}
    a.archive_sha256=a.archive_sha256 or pins.get('rootfsSha256')
    a.signature_sha256=a.signature_sha256 or pins.get('signatureSha256')
    a.keyring_sha256=a.keyring_sha256 or pins.get('keyringSha256')
    a.primary_fingerprint=a.primary_fingerprint or pins.get('signingKeyFingerprint')
    require(a.archive_sha256 and a.signature_sha256 and a.keyring_sha256 and a.primary_fingerprint,
            'archive/signature/keyring sha256 and primary fingerprint required (flags or --inputs-json)')
    build=a.build_root.absolute();target=a.output.absolute();receipt=a.receipt.absolute()
    require(build.resolve(strict=True)==build and build!=Path('/') and build.is_dir(),'explicit canonical builder root required')
    require(target.is_relative_to(build) and target!=build and target.parent.resolve(strict=True)==target.parent
            and not os.path.lexists(target),'new target inside builder root required')
    require(receipt.parent.resolve(strict=True)==receipt.parent and receipt.is_relative_to(build) and not receipt.is_relative_to(target)
            and not os.path.lexists(receipt),'new external receipt inside builder root required')
    inputs={n:checked(getattr(a,n),getattr(a,n+'_sha256')) for n in ('archive','signature','keyring')}
    require(all(not x.is_relative_to(target) for x in inputs.values()),'inputs cannot be inside new root')
    sign=verify_signature(**inputs,fingerprint=a.primary_fingerprint)
    target.mkdir(mode=0o755);target.chmod(0o755)
    subprocess.run(['bsdtar','--numeric-owner','-xpf',str(inputs['archive']),'-C',str(target),
                    *(['--strip-components',str(strip)] if strip else [])],check=True,timeout=1800)
    require((target/'usr/bin/pacman').is_file() and (target/'usr/bin/bash').is_file(),'rootfs tools missing')
    result={'schema':'decrypt.signed-builder-root.v1','status':'passed',**sign,
            'inputSha256':{n:getattr(a,n+'_sha256') for n in inputs},'packageResolutionVerified':False,'networkAccessed':False}
    fd=os.open(receipt,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as out:json.dump(result,out,indent=2);out.write('\n');out.flush();os.fsync(out.fileno())

if __name__=='__main__':main()
