"""Synthetic-only vault, storage and local HTTP privacy boundaries."""
import base64
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/backend'))
from guardian_auth import GuardianError, GuardianVault, PrivateFiles
from server import LabServer, Store

PASSPHRASE = 'fixture-vault-passphrase-ONLY'
USERNAME = 'fixture-user@example.invalid'
PASSWORD = 'fixture-login-password-ONLY'


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name).resolve()
        self.files = PrivateFiles(self.directory)
        self.vault = GuardianVault(self.files)

    def tearDown(self):
        self.vault.lock()
        self.files.close()
        self.temp.cleanup()

    def test_encryption_and_restart_lock(self):
        self.vault.initialize(PASSPHRASE)
        self.vault.save_provider('tradingview', USERNAME, PASSWORD, True)
        raw = (self.directory / 'guardian/vault.json').read_bytes()
        for value in (PASSPHRASE, USERNAME, PASSWORD):
            self.assertNotIn(value.encode(), raw)
        self.assertEqual((self.directory / 'guardian/vault.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn(USERNAME, json.dumps(self.vault.metadata()))
        other = GuardianVault(self.files)
        self.assertFalse(other.metadata()['unlocked'])
        other.unlock(PASSPHRASE)
        with other.credential('tradingview') as credentials:
            self.assertEqual(credentials, (USERNAME, PASSWORD, True))
        key = other.key
        username = other.entries['tradingview']['username']
        other.lock()
        self.assertTrue(all(v == 0 for v in key))
        self.assertTrue(all(v == 0 for v in username))
        self.assertEqual(other.entries, {})

    def test_providers_separate_delete_and_reunlock(self):
        self.vault.initialize(PASSPHRASE)
        self.vault.save_provider('tradingview', 'fixture-tradingview', 'fixture-chart-password', False)
        self.vault.lock()
        self.vault.unlock(PASSPHRASE)
        with self.vault.credential('tradingview') as credentials:
            self.assertEqual(credentials[1], 'fixture-chart-password')
        self.vault.delete_provider('tradingview')
        self.assertNotIn('tradingview', self.vault.metadata()['providers'])
        with self.assertRaises(GuardianError):
            self.vault.save_provider('tradovate', USERNAME, PASSWORD, True)

    def test_wrong_password_locks_and_has_bounded_cooldown(self):
        self.vault.initialize(PASSPHRASE)
        with self.assertRaises(GuardianError) as error:
            self.vault.unlock('wrong-fixture-passphrase')
        self.assertEqual(error.exception.code, 'unlock_failed')
        self.assertFalse(self.vault.metadata()['unlocked'])
        with self.assertRaises(GuardianError) as error:
            self.vault.unlock(PASSPHRASE)
        self.assertEqual(error.exception.code, 'unlock_cooldown')

    def test_tampered_header_cannot_enable_recovery(self):
        self.vault.initialize(PASSPHRASE)
        self.vault.save_provider('tradingview', USERNAME, PASSWORD, False)
        raw = self.files.read('vault.json')
        raw['providers']['tradingview']['autoLogin'] = True
        self.files.write('vault.json', raw)
        with self.assertRaises(GuardianError):
            self.vault.unlock(PASSPHRASE)
        self.assertFalse(self.vault.metadata()['unlocked'])

    def test_bad_kdf_rejected_without_derivation(self):
        self.vault.initialize(PASSPHRASE)
        data = self.files.read('vault.json')
        data['kdf'] = 'scrypt-1073741824-8-1'
        self.files.write('vault.json', data)
        self.vault.derive = lambda *_: self.fail('Unbounded KDF was invoked')
        with self.assertRaises(GuardianError):
            self.vault.unlock(PASSPHRASE)

    def test_symlink_hardlink_modes_and_fifo_rejected(self):
        path = self.directory / 'guardian/boundary.json'
        target = self.directory / 'target'
        target.write_text('{}')
        target.chmod(0o600)
        for kind in ('symlink', 'hardlink', 'mode', 'fifo'):
            if kind == 'symlink': path.symlink_to(target)
            elif kind == 'hardlink': os.link(target, path)
            elif kind == 'mode': path.write_text('{}'); path.chmod(0o644)
            else: os.mkfifo(path, 0o600)
            begin = time.monotonic()
            with self.assertRaises(GuardianError):
                self.files.read('boundary.json')
            self.assertLess(time.monotonic() - begin, 1)
            path.unlink()

    def test_second_process_lock_and_bad_directory(self):
        with self.assertRaises(GuardianError):
            PrivateFiles(self.directory)
        bad = self.directory / 'unsafe'
        bad.mkdir(mode=0o777)
        bad.chmod(0o777)
        with self.assertRaises(GuardianError):
            PrivateFiles(bad)

    def test_no_save_while_locked_unknown_provider_or_duplicate_init(self):
        with self.assertRaises(GuardianError):
            self.vault.save_provider('tradovate', USERNAME, PASSWORD, True)
        self.vault.initialize(PASSPHRASE)
        with self.assertRaises(GuardianError):
            self.vault.initialize(PASSPHRASE)
        with self.assertRaises(GuardianError):
            self.vault.save_provider('https://attacker.invalid', USERNAME, PASSWORD, True)


class GuardianHTTPTests(unittest.TestCase):
    def test_csrf_origin_and_response_privacy(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            captured = io.StringIO()
            with contextlib.redirect_stderr(captured), contextlib.redirect_stdout(captured):
                server = LabServer(('127.0.0.1', 0), Store(directory), guardian_enabled=True)
                server.first_start.admitted = lambda: True  # first-start notice gate: tests/test_first_start.py
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = 'http://127.0.0.1:' + str(server.server_port)
                def request(method, path, data=None, headers=None):
                    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
                    merged = {'Origin': base, 'Content-Type': 'application/json'}
                    merged.update(headers or {})
                    connection.request(method, path, body=json.dumps(data) if data is not None else None, headers=merged)
                    result = connection.getresponse()
                    body, status, cookies = result.read().decode(), result.status, result.getheader('Set-Cookie')
                    connection.close()
                    return status, body, cookies
                try:
                    code, raw, cookie = request('GET', '/api/session', headers={'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty'})
                    self.assertEqual(code, 200)
                    headers = {'Cookie': cookie.split(';')[0], 'X-CSRF-Token': json.loads(raw)['csrfToken']}
                    for override in ({'Origin': 'https://attacker.invalid'}, {'X-CSRF-Token': 'wrong'}):
                        code, raw, _ = request('POST', '/api/guardian/initialize', {'passphrase': PASSPHRASE}, {**headers, **override})
                        self.assertEqual(code, 403)
                        self.assertNotIn(PASSPHRASE, raw)
                    code, raw, _ = request('POST', '/api/guardian/initialize', {'passphrase': PASSPHRASE}, headers)
                    self.assertEqual(code, 200, raw)
                    code, raw, _ = request('POST', '/api/guardian/save-provider', {'provider': 'tradingview', 'username': USERNAME, 'password': PASSWORD, 'autoLogin': True}, headers)
                    self.assertEqual(code, 200, raw)
                    for value in (PASSPHRASE, USERNAME, PASSWORD): self.assertNotIn(value, raw)
                    code, raw, _ = request('POST', '/api/guardian/save-provider', {'provider': PASSWORD, 'username': USERNAME, 'password': PASSWORD, 'autoLogin': True}, headers)
                    self.assertEqual(code, 400)
                    self.assertNotIn(PASSWORD, raw)
                    code, raw, _ = request('GET', '/api/state')
                    for value in (PASSPHRASE, USERNAME, PASSWORD): self.assertNotIn(value, raw)
                finally:
                    server.shutdown(); server.server_close(); thread.join(5)
            for path in directory.rglob('*'):
                if path.is_file():
                    for value in (PASSPHRASE, USERNAME, PASSWORD): self.assertNotIn(value.encode(), path.read_bytes(), str(path))
            self.assertEqual(captured.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
