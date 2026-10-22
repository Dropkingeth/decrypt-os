#!/usr/bin/env python3
"""Extract the exact source archive, rebuild and compare reviewed UI/recipe."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile

def sha(data):return hashlib.sha256(data).hexdigest()

def tree(root):
    result={}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():raise ValueError('Linked generated file')
        if path.is_file():result[path.relative_to(root).as_posix()]=sha(path.read_bytes())
    if not result:raise ValueError('No generated files')
    return result

def load(path):
    spec=importlib.util.spec_from_file_location('reviewed_recipe',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

def verify(archive, manifest_path, candidate, output):
    # Caller pins archive+manifest in the new media plan. This is local build proof,
    # never a guest apply, system preflight or provider call.
    output=output.absolute()
    if output.exists() or output.is_symlink():raise ValueError('Use a new verification directory')
    for parent in output.parents:
        if parent.is_symlink() or not parent.is_dir():raise ValueError('Invalid verification parent')
    output.mkdir(mode=0o700)
    source=output/'source';source.mkdir(mode=0o700)
    manifest_raw=manifest_path.read_bytes();manifest=json.loads(manifest_raw)
    expected={r['path']:(r['sha256'],r['bytes']) for r in manifest['files']}
    if len(expected)!=len(manifest['files']):raise ValueError('Duplicate manifest source')
    expected['SOURCE-MANIFEST.json']=(sha(manifest_raw),len(manifest_raw))
    seen=set()
    with tarfile.open(archive,'r:gz') as tar:
        for member in tar:
            path=PurePosixPath(member.name)
            if not member.isfile() or path.parts[0]!='decrypt-os-source' or any(p in ('','..','.') for p in member.name.split('/')):
                raise ValueError('Invalid source archive member')
            relative=PurePosixPath(*path.parts[1:]).as_posix()
            if relative not in expected or relative in seen:raise ValueError('Unexpected or duplicate archive member')
            raw=tar.extractfile(member).read()
            if (sha(raw),len(raw))!=expected[relative]:raise ValueError('Archive source mismatch')
            destination=source/relative;destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            destination.write_bytes(raw);seen.add(relative)
    if seen!=set(expected):raise ValueError('Missing source archive member')
    node=Path(shutil.which('node')).resolve();npm=Path(shutil.which('npm')).resolve()
    home=output/'build-home';home.mkdir(mode=0o700)
    for name in ('user.npmrc','global.npmrc'):(output/name).touch(mode=0o600)
    env={'HOME':str(home),'PATH':str(node.parent)+':/usr/bin:/bin','LANG':'en_US.UTF-8','CI':'1'}
    npm_command=[str(node),str(npm),'--userconfig='+str(output/'user.npmrc'),'--globalconfig='+str(output/'global.npmrc')]
    steps=[]
    for label,args in [('npm-ci',['ci','--ignore-scripts','--no-audit','--no-fund']),('ui-build',['run','build'])]:
        result=subprocess.run(npm_command+args,cwd=source/'app/ui',env=env,text=True,
                              stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=240)
        (output/(label+'.txt')).write_text(result.stdout)
        print(label, result.returncode,flush=True)
        if result.returncode:raise ValueError('Extracted build failed: '+label)
        steps.append({'step':label,'exitCode':result.returncode})
    compiled=tree(source/'app/ui/dist');reviewed=tree(candidate/'app/ui/dist')
    if compiled!=reviewed:raise ValueError('Extracted interface differs from reviewed candidate')
    installer=load(source/'installer/clean_base.py')
    receipt=installer.render(source,output/'recipe','customer')
    # Compare a separately rendered reviewed tree with the extracted build.
    # Pin the new manifest on the reviewed candidate before calling this verifier.
    candidate_installer=load(candidate/'installer/clean_base.py')
    candidate_receipt=candidate_installer.render(candidate,output/'reviewed-recipe','customer')
    if tree(output/'recipe/rootfs')!=tree(output/'reviewed-recipe/rootfs'):
        raise ValueError('Extracted installation recipe differs from reviewed candidate')
    result={'schema':'decrypt.extracted-build.v1','passed':True,'nativeInstall':False,
            'sourceFiles':len(seen),'archiveSha256':sha(archive.read_bytes()),
            'manifestSha256':sha(manifest_raw),'interfaceFiles':compiled,
            'recipeFiles':len(receipt['files']),'recipeMatchesReviewed':True,
            'node':subprocess.check_output([str(node),'--version'],text=True).strip(),
            'host':os.uname().sysname,'steps':steps,
            'limits':'Host extraction, UI build and recipe rendering only; not pinned Linux runtime or native installation proof.'}
    (output/'RECEIPT.json').write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    print(json.dumps({k:result[k] for k in ('passed','sourceFiles','recipeFiles','recipeMatchesReviewed')}))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive',type=Path,required=True);p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--candidate',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();verify(a.archive,a.manifest,a.candidate,a.output)
