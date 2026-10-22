import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

HERE=Path(__file__).parent

def load(name):
    s=importlib.util.spec_from_file_location(name,HERE/(name+'.py'));m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

bootstrap=load('bootstrap_alarm_root');fetch=load('fetch_omarchy')

class InputsTests(unittest.TestCase):
    def test_alarm_pins_loaded_from_build_inputs_json(self):
        pins=bootstrap.load_alarm_pins(HERE.parent.parent/'build/inputs.json')
        self.assertEqual(set(pins),{'source','rootfsSha256','signatureSha256','keyringSha256','signingKeyFingerprint'})
        self.assertRegex(pins['rootfsSha256'],'^[0-9a-f]{64}$')
        self.assertRegex(pins['signingKeyFingerprint'],'^[A-F0-9]{40}$')

    def test_alarm_pins_refuse_incomplete_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'inputs.json';p.write_text(json.dumps({'alarm':{'rootfsSha256':'a'*64}}))
            with self.assertRaises(ValueError):bootstrap.load_alarm_pins(p)

    def test_local_git_archive_mode_is_retired(self):
        with self.assertRaisesRegex(ValueError, 'local Git repository mode retired'):
            fetch.make_archive('untrusted-repository', 'a'*40, 'b'*64,
                               'https://example.org/public.git', 'output.tar')

    def test_rootfs_hash_and_key_status_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp).resolve()/'rootfs';p.write_bytes(b'archive');digest=hashlib.sha256(b'archive').hexdigest()
            self.assertEqual(bootstrap.checked(p,digest),p)
            with self.assertRaises(ValueError):bootstrap.checked(p,'0'*64)
            primary='A'*40;valid='[GNUPG:] VALIDSIG '+primary+' 2026-09-27 1790467200 0 4 0 22 8 00 '+primary+'\n'
            with patch.object(bootstrap.subprocess,'run',return_value=subprocess.CompletedProcess([],0,valid,'')):
                self.assertEqual(bootstrap.verify_signature(p,p,p,primary)['primaryFingerprint'],primary)
            for status in ('EXPKEYSIG','BADSIG','REVKEYSIG'):
                with patch.object(bootstrap.subprocess,'run',return_value=subprocess.CompletedProcess([],0,valid+'[GNUPG:] '+status+' key\n','')):
                    with self.assertRaises(ValueError):bootstrap.verify_signature(p,p,p,primary)


if __name__=='__main__':unittest.main()
