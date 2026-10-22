"""Current source integration, portable to a clean checkout; no historical fixtures."""
from pathlib import Path
import importlib.util
import json
import tarfile
import tempfile
import unittest

BUNDLE=Path(__file__).resolve().parents[1]

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

installer=load('consolidated_installer',BUNDLE/'installer/clean_base.py')
builder=load('consolidated_builder',BUNDLE/'lab/build_source_bundle.py')
desktop=load('consolidated_desktop',BUNDLE/'installer/desktop_apps.py')

class CurrentIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def extract(self):
        built=builder.build(BUNDLE,self.root/'archive',customer_base=True)
        target=self.root/'source';target.mkdir()
        with tarfile.open(self.root/'archive/decrypt-os-source.tar.gz','r:gz') as archive:
            for row in archive:
                self.assertTrue(row.isfile());self.assertTrue(row.name.startswith('decrypt-os-source/'))
                relative=row.name.removeprefix('decrypt-os-source/')
                self.assertNotIn('..',Path(relative).parts)
                path=target/relative;path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(archive.extractfile(row).read())
        return target,built

    def test_current_allowlist_matches_extracted_archive_and_installer(self):
        target,built=self.extract()
        actual,_=installer.verify_bundle(target)
        self.assertEqual(actual,builder.gather(BUNDLE))
        self.assertEqual(len(actual),built['files'])
        self.assertTrue(set(installer.COPY_FILES)<=actual.keys())

    def test_changed_shipped_source_is_rejected(self):
        target,_=self.extract()
        path=target/'installer/desktop_apps.py';path.write_bytes(path.read_bytes()+b'\n# unreviewed\n')
        with self.assertRaisesRegex(installer.RecipeError,'does not match'):
            installer.verify_bundle(target)

    def test_packaging_deterministic_and_excludes_runtime_and_fixtures(self):
        first=builder.build(BUNDLE,self.root/'a',customer_base=True)
        second=builder.build(BUNDLE,self.root/'b',customer_base=True)
        self.assertEqual(first,second)
        self.assertEqual((self.root/'a/decrypt-os-source.tar.gz').read_bytes(),(self.root/'b/decrypt-os-source.tar.gz').read_bytes())
        manifest=json.loads((self.root/'a/SOURCE-MANIFEST.json').read_text())
        for row in manifest['files']:
            self.assertFalse(set(Path(row['path']).parts)&{'tests','evidence','node_modules','state','__pycache__'})

    def test_current_launchers_use_reviewed_root_owned_dispatcher(self):
        generated=installer.generated_files('customer')
        for key in ('usr/local/bin/decrypt-os','usr/local/bin/decrypt-tradingview'):
            raw,mode=generated[key]
            self.assertEqual(mode,0o755)
            self.assertIn(b'/usr/local/lib/decrypt-os/installer/desktop_apps.py',raw)
            compile(raw,key,'exec')

    def test_browser_sandbox_and_separate_profile_preserved(self):
        command=desktop.browser_command(desktop.resolve_app('decrypt'),Path('/home/customer/.local/share/decrypt-browser/decrypt'))
        self.assertNotIn('--no-sandbox',command);self.assertNotIn('--disable-setuid-sandbox',command)
        self.assertIn('--user-data-dir=/home/customer/.local/share/decrypt-browser/decrypt',command)
        self.assertIn('--app=http://127.0.0.1:8811',command)

if __name__=='__main__':unittest.main()
