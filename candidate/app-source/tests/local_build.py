"""Reproduce local UI build/render without changing system tools or VM state."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import types

root = Path(__file__).resolve().parents[1]
node = Path(shutil.which('node')).resolve()
npm = Path(shutil.which('npm')).resolve()
result = {'native_vm': False,
          'node': subprocess.check_output([str(node), '--version'], text=True).strip(),
          'npm': subprocess.check_output([str(node), str(npm), '--version'], text=True).strip(),
          'architecture': os.uname().machine, 'platform': os.uname().sysname}
with tempfile.TemporaryDirectory(prefix='decrypt-v2-app-build-') as temp:
    work = Path(temp)
    bundle = work/'source'
    shutil.copytree(root, bundle, ignore=shutil.ignore_patterns('tests', 'evidence', '__pycache__'))
    home = work/'home'
    home.mkdir()
    configs = work/'npm-config'
    configs.mkdir()
    for name in ('user.npmrc', 'global.npmrc'):
        (configs/name).touch()
    env = {'HOME': str(home), 'PATH': str(node.parent)+':/usr/bin:/bin',
           'LANG': 'en_US.UTF-8', 'TMPDIR': temp}
    cmd = [str(node), str(npm), '--userconfig='+str(configs/'user.npmrc'),
           '--globalconfig='+str(configs/'global.npmrc')]
    result['commands'] = []
    for args in (['ci', '--ignore-scripts', '--no-audit', '--no-fund'], ['run', 'build']):
        run = subprocess.run(cmd+args, cwd=bundle/'app/ui', env=env, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=240)
        print(run.stdout, flush=True)
        result['commands'].append({'argv': [str(node), str(npm),
            '--userconfig=<private-temp>/npm-config/user.npmrc',
            '--globalconfig=<private-temp>/npm-config/global.npmrc', *args],
            'exitCode': run.returncode})
        assert run.returncode == 0
    result['buildPassed'] = True
    result['dist'] = [{'path': str(p.relative_to(bundle/'app/ui/dist')), 'bytes': p.stat().st_size,
                      'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                     for p in sorted((bundle/'app/ui/dist').rglob('*')) if p.is_file()]
    installer = types.ModuleType('installer')
    installer.__file__ = str(bundle/'installer/clean_base.py')
    exec(compile((bundle/'installer/clean_base.py').read_bytes(), installer.__file__, 'exec'), installer.__dict__)
    receipt = installer.render(bundle, work/'recipe', 'customer')
    result['renderPassed'] = True
    result['renderFiles'] = len(receipt['files'])
    result['renderReceiptSha256'] = hashlib.sha256((work/'recipe/RECIPE-RECEIPT.json').read_bytes()).hexdigest()
    result['renderSourceHash'] = hashlib.sha256((bundle/'SOURCE-MANIFEST.json').read_bytes()).hexdigest()
    result['limits'] = 'Local host build and render only. Not the pinned Linux Node 24.19.0 runtime; fresh Linux installation remains unverified.'
(root/'tests/local-build-result.json').write_text(json.dumps(result, indent=2)+'\n')
print(json.dumps({key: value for key, value in result.items() if key != 'dist'}, indent=2))
