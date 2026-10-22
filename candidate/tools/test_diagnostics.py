import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location('diagnostics', Path(__file__).with_name('diagnostics.py'))
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

class DiagnosticsTests(unittest.TestCase):
    def test_unsupported_host_does_not_probe_installation(self):
        def fail(*args): self.fail('Unexpected probe')
        result = d.collect(system='Darwin', machine='arm64', uid=501,
                           file_reader=fail, unit_reader=fail, which=fail, disk_reader=fail)
        self.assertEqual(result['status'], 'unsupported_host')

    def test_root_is_refused_without_probes(self):
        def fail(*args): self.fail('Unexpected probe')
        result = d.collect(system='Linux', machine='aarch64', uid=0,
                           file_reader=fail, unit_reader=fail, which=fail, disk_reader=fail)
        self.assertEqual(result['status'], 'wrong_account')

    def test_scope_is_exact_and_not_integrity_or_provider_proof(self):
        calls=[]
        result=d.collect(system='Linux', machine='aarch64', uid=1000,
                         file_reader=lambda p: calls.append(p) or {'status':'present'},
                         unit_reader=lambda u:'inactive',which=lambda n:'/usr/bin/'+n,
                         journal_reader=lambda u:[],
                         disk_reader=lambda p:SimpleNamespace(total=400,free=200))
        self.assertEqual(set(calls), set(d.INSTALL_FILES.values()))
        self.assertEqual(set(result['services']), set(d.UNITS))
        self.assertEqual(set(result['recentErrors']), set(d.UNITS))
        self.assertFalse(result['installationVerified'])
        self.assertFalse(result['providerChecked'])

    def test_file_probe_uses_metadata_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); p=root/'file';p.write_text('private-data-not-for-output');p.chmod(0)
            self.assertEqual(d.file_fact(p)['status'],'present')
            self.assertNotIn('private',str(d.file_fact(p)))
            p.chmod(0o600)

    def test_linked_files_and_parents_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'dir').mkdir();(root/'dir/file').write_text('hidden')
            (root/'alias').symlink_to(root/'dir',target_is_directory=True)
            self.assertEqual(d.file_fact(root/'alias/file')['status'],'linked_path')
            (root/'link').symlink_to(root/'dir/file')
            self.assertEqual(d.file_fact(root/'link')['status'],'linked_path')

    def test_absent_and_special_files_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.assertEqual(d.file_fact(root/'absent')['status'],'missing')
            self.assertEqual(d.file_fact(root)['status'],'unexpected_type')

    def test_unit_probe_is_bounded_and_output_allowlisted(self):
        calls=[]
        def run(argv,**kwargs):
            calls.append((argv,kwargs))
            return SimpleNamespace(stdout='private accidental output',returncode=1)
        self.assertEqual(d.unit_fact(d.UNITS[0],run),'unavailable')
        argv,options=calls[0]
        self.assertEqual(argv,['/usr/bin/systemctl','--user','is-active',d.UNITS[0]])
        self.assertEqual(options['timeout'],4)
        self.assertEqual(options['stderr'],subprocess.DEVNULL)
        self.assertNotIn('shell',options)
        with self.assertRaises(ValueError):d.unit_fact('unrelated.service',run)

    def test_timeout_is_unavailable_not_failed_install(self):
        def run(*args,**kwargs):raise subprocess.TimeoutExpired('systemctl',4)
        self.assertEqual(d.unit_fact(d.UNITS[0],run),'unavailable')

    def test_redact_removes_macos_and_linux_home_paths(self):
        text=d.redact('failed to read /Users/someone/work/secret and /home/somehost/.config')
        self.assertNotIn('someone',text);self.assertNotIn('somehost',text)
        self.assertIn('/Users/<redacted>',text);self.assertIn('/home/<redacted>',text)

    def test_redact_removes_email_addresses(self):
        text=d.redact('contact jane.doe@realcompany.example for help')
        self.assertNotIn('jane.doe',text);self.assertIn('<redacted-email>',text)

    def test_redact_removes_secret_token_password_literals_but_keeps_the_key(self):
        text=d.redact('login failed: password=SuperSecretValue123 token: abcDEF456ghiJKL')
        self.assertNotIn('SuperSecretValue123',text);self.assertNotIn('abcDEF456ghiJKL',text)
        self.assertIn('password=<redacted>',text);self.assertIn('token: <redacted>',text)

    def test_redact_removes_private_key_headers(self):
        text=d.redact('-----BEGIN RSA PRIVATE KEY-----')
        self.assertNotIn('BEGIN RSA PRIVATE KEY',text)

    def test_redact_removes_long_digit_runs_like_account_or_order_ids(self):
        text=d.redact('order 30002222 on account 1234567890 rejected')
        self.assertNotIn('30002222',text);self.assertNotIn('1234567890',text)
        self.assertEqual(text.count('<redacted-number>'),2)

    def test_redact_keeps_short_numbers_like_ports_or_counts(self):
        text=d.redact('listening on port 8810, retried 3 times')
        self.assertIn('8810',text);self.assertIn('3 times',text)

    def test_redact_removes_username_after_user_login_account_keywords(self):
        text=d.redact('user=realowner login: realowner account=realowner')
        self.assertNotIn('realowner',text)

    def test_journal_fact_redacts_every_line(self):
        def run(argv,**kwargs):
            self.assertIn('/usr/bin/journalctl',argv);self.assertIn(d.UNITS[0],argv)
            self.assertEqual(kwargs['stderr'],subprocess.DEVNULL)
            return SimpleNamespace(stdout='token: realsecretvalue1234567890\nok\n',returncode=0)
        lines=d.journal_fact(d.UNITS[0],runner=run)
        self.assertEqual(lines,['token: <redacted>','ok'])
        with self.assertRaises(ValueError):d.journal_fact('unrelated.service',runner=run)

    def test_journal_fact_timeout_is_unavailable(self):
        def run(*args,**kwargs):raise subprocess.TimeoutExpired('journalctl',6)
        self.assertEqual(d.journal_fact(d.UNITS[0],runner=run),['<unavailable>'])

    def test_write_bundle_contains_only_diagnostics_json_and_is_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'bundle.tar.gz'
            facts={'schema':'decrypt.diagnostics.v1','status':'collected'}
            d.write_bundle(facts,out)
            self.assertEqual(stat.S_IMODE(out.stat().st_mode),0o600)
            import tarfile
            with tarfile.open(out) as archive:
                names=archive.getnames()
                self.assertEqual(names,['diagnostics.json'])
                content=json.loads(archive.extractfile('diagnostics.json').read())
            self.assertEqual(content,facts)

    def test_write_bundle_refuses_an_existing_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'bundle.tar.gz';out.write_text('already here')
            with self.assertRaises(SystemExit):d.write_bundle({'status':'collected'},out)

    def test_diagnostics_bundle_never_leaks_a_synthetic_secret_end_to_end(self):
        # Full pipeline: a "leaky" journal reader feeds collect(), collect()
        # feeds write_bundle(), and the secret must not survive into the tar.
        secret='AKIAABCDEFGHIJKLMNOP'
        result=d.collect(system='Linux',machine='aarch64',uid=1000,
                         file_reader=lambda p:{'status':'present'},unit_reader=lambda u:'active',
                         journal_reader=lambda u:[d.redact(f'login failed, key={secret} for user=realowner')],
                         which=lambda n:'/usr/bin/'+n,disk_reader=lambda p:SimpleNamespace(total=1,free=1))
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'bundle.tar.gz'
            d.write_bundle(result,out)
            raw=out.read_bytes()
        self.assertNotIn(secret.encode(),raw)
        self.assertNotIn(b'realowner',raw)

if __name__ == '__main__':unittest.main()
