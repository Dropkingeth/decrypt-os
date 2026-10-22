"""Bridge Advanced: safety checks (retired max_contracts, fail-closed pause switch, rollback keeps pauses)."""
import json
import os
import stat
import subprocess
import sys
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_admin  # noqa: E402
from test_admin import Base  # noqa: E402
from model import Invalid  # noqa: E402
from tree import FileOps, is_pause_file, read_switch  # noqa: E402

MAIN = 'account-a'


class RetiredMaxContracts(Base):
    """No size cap: the strategy alert sets the size. Only a malformed legacy `max_contracts` (one the bridge
    refuses to start with) still blocks arming and structural changes."""
    def set_main_value(self, value, drop=False):
        path = self.tb('config.json')
        with open(path) as f:
            cfg = json.load(f)
        if drop:
            cfg.pop('max_contracts', None)
        else:
            cfg['max_contracts'] = value
        with open(path, 'w') as f:
            json.dump(cfg, f, indent=2)

    def test_resume_refused_with_a_malformed_legacy_value(self):
        self.do('pause', {'account': MAIN, 'reason': 'test'})
        for bad in (0, -1, '5', 'x', True, 2.5):
            with self.subTest(value=bad):
                self.set_main_value(bad)
                ch = self.e.preview('resume', {'account': MAIN})
                self.assertTrue(any('max_contracts is retired' in b for b in ch['plan']['blockers']),
                                ch['plan']['blockers'])

    def test_resume_allowed_with_any_positive_or_no_legacy_value(self):
        self.do('pause', {'account': MAIN, 'reason': 'test'})
        for ok in (1, 10, 11, 40, None):
            with self.subTest(value=ok):
                self.set_main_value(ok, drop=ok is None)
                ch = self.e.preview('resume', {'account': MAIN})
                self.assertFalse(ch['plan']['blockers'], ch['plan']['blockers'])

    def test_structural_change_refused_with_a_malformed_legacy_value(self):
        self.set_main_value(0)
        with self.assertRaises(Invalid) as ctx:
            self.e.preview('set_enabled', {'account': 'account-b', 'enabled': False})
        self.assertIn('max_contracts is retired (the strategy alert sets the size); remove it from the config',
                      str(ctx.exception))
        self.set_main_value(25)    # was refused as over the old cap; a legacy positive value is fine now
        self.assertEqual(self.do('set_enabled', {'account': 'account-b', 'enabled': False})['status'], 'applied')

    def test_waiting_size_cap_change_from_an_earlier_version_fails_cleanly(self):
        self.set_status({'account-a': 0, 'account-b': 2, 'account-c': 0, 'account-d': 0, 'account-e': 0})
        ch = self.do('touch_follower', {'account': 'account-b', 'n': 6})
        self.assertEqual(ch['status'], 'waiting_flat')
        stored = self.e.store.get(ch['id'])
        stored['type'], stored['params'] = 'set_max_contracts', {'account': 'account-b', 'max_contracts': 6}
        self.e.store.put(stored)
        before = self.raw('config.json')
        self.set_status({k: 0 for k in test_admin.KEYS})
        self.e.tick()
        got = self.e.get(ch['id'])
        self.assertEqual(got['status'], 'failed')
        self.assertEqual(got['error'], 'No longer possible: The size cap is retired: the strategy alert sets the size.')
        self.assertEqual(self.raw('config.json'), before)


class PauseSwitchFailsClosed(Base):
    def test_read_switch_states(self):
        folder = os.path.join(self.tmp, 'sw')
        os.mkdir(folder)
        path = os.path.join(folder, 'ENTRIES-DISABLED')
        self.assertEqual(read_switch(path)['on'], False)
        for bad in (None, '', 'relative/ENTRIES-DISABLED'):
            sw = read_switch(bad)
            self.assertTrue(sw['on'])
            self.assertFalse(sw['path_ok'])
            self.assertIn('No pause switch file is configured', sw['reason'])
        os.symlink(os.path.join(folder, 'nowhere'), path)
        sw = read_switch(path)
        self.assertTrue(sw['on'])
        self.assertIn('broken link', sw['reason'])
        sw = read_switch(os.path.join(self.tmp, 'gone', 'ENTRIES-DISABLED'))
        self.assertTrue(sw['on'])
        self.assertFalse(sw['path_ok'])

    def set_switch(self, key, value):
        path = self.tb('config.json')
        with open(path) as f:
            cfg = json.load(f)
        for f_ in cfg.get('followers') or []:
            if f_.get('key') == key:
                f_['kill_switch_file'] = value
        with open(path, 'w') as f:
            json.dump(cfg, f, indent=2)

    def test_resume_cannot_arm_a_missing_switch_folder(self):
        self.set_switch('account-b', os.path.join(self.tmp, 'gone', 'ENTRIES-DISABLED-account-b'))
        self.assertTrue(self.acct('account-b')['pause']['on'])
        ch = self.e.preview('resume', {'account': 'account-b'})
        self.assertTrue(any('stays paused' in b for b in ch['plan']['blockers']), ch['plan']['blockers'])
        ch = self.e.preview('pause', {'account': 'account-b', 'reason': 'x'})
        self.assertTrue(any('already paused' in b for b in ch['plan']['blockers']), ch['plan']['blockers'])


class LaunchCode(Base):
    """The admin token is never on a command line; the window trades a one-time code for it."""
    req = test_admin.TestServer.req

    def setUp(self):
        super().setUp()
        import threading
        from http.server import ThreadingHTTPServer
        import server
        self.server = server
        p = mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': self.tmp})
        p.start()
        self.addCleanup(p.stop)
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.make_handler(self.e, 'tok', 0))
        self.port = self.httpd.server_address[1]
        self.httpd.RequestHandlerClass = server.make_handler(self.e, 'tok', self.port)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def write_code(self, code, age=0, mode=0o600):
        d = self.server.runtime_dir()
        path = os.path.join(d, f'launch-code-{self.port}')
        with open(path, 'w') as f:
            f.write(code)
        os.chmod(path, mode)
        if age:
            t = time.time() - age
            os.utime(path, (t, t))
        return path

    def claim(self, code, origin='same'):
        return self.req('POST', '/api/claim', {'code': code}, token=None, origin=origin)

    def test_code_is_traded_once_for_the_token(self):
        code = 'c' * 43
        path = self.write_code(code)
        self.assertEqual(self.claim('x' * 43)[0], 401)          # a wrong guess
        self.assertTrue(os.path.exists(path))
        self.assertEqual(self.claim(code), (200, {'token': 'tok'}))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.claim(code)[0], 401)              # single use

    def test_old_or_open_code_or_foreign_origin_is_refused(self):
        code = 'd' * 43
        self.write_code(code, age=self.server.CODE_TTL_S + 5)
        self.assertEqual(self.claim(code)[0], 401)
        self.write_code(code, mode=0o644)
        self.assertEqual(self.claim(code)[0], 401)
        self.write_code(code)
        self.assertEqual(self.claim(code, origin='http://evil.example')[0], 403)
        self.assertEqual(self.claim(code, origin=None)[0], 403)
        self.assertEqual(self.claim('short')[0], 401)

    def test_token_file_is_private_and_never_follows_a_link(self):
        d = self.server.runtime_dir()
        target = os.path.join(self.tmp, 'elsewhere')
        os.symlink(target, os.path.join(d, 'token-9.tmp'))   # the old fixed temp name
        path = self.server.write_token('secret-token', 9)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertFalse(os.path.exists(target))
        with open(path) as f:
            self.assertEqual(f.read(), 'secret-token')

    def test_launcher_never_puts_the_token_on_the_command_line(self):
        script = os.path.join(os.path.dirname(test_admin.ADMIN), 'bin', 'decrypt-bridge-advanced')
        with open(script) as f:
            text = f.read()
        self.assertNotIn('#t=', text)
        self.assertNotIn('$(cat "$TOKF")', text)
        self.assertIn('advanced#c=$CODE', text)
        self.assertEqual(subprocess.run(['bash', '-n', script]).returncode, 0)


class StreamIds(Base):
    """Weak stream IDs are refused; no Linux user name in webhook paths; the previewed ID is the one kept."""
    def test_weak_stream_id_is_refused(self):
        for weak in ('abc', '1234567890123456789', 'short-id', 'x' * 49, 'bad id with spaces!'):
            with self.subTest(stream=weak), self.assertRaises(Invalid):
                self.e.preview('add_webhook', {'name': 'Scalper', 'strategy': 'soup-pine-r4',
                                               'leader': 'account-b', 'stream_id': weak})

    def test_strong_custom_stream_id_is_kept(self):
        ch = self.do('add_webhook', {'name': 'Scalper', 'strategy': 'soup-pine-r4', 'leader': 'account-b',
                                     'stream_id': 'my-own-strong-stream-id'})
        self.assertEqual(ch['plan']['generated']['stream_id'], 'my-own-strong-stream-id')

    def test_previewed_stream_id_and_path_are_what_confirm_writes(self):
        ch = self.e.preview('add_webhook', {'name': 'Scalper', 'strategy': 'soup-pine-r4', 'leader': 'account-b'})
        shown = ch['plan']['generated']
        self.assertNotIn(self.profile.data['user'], shown['path'])
        self.e.confirm(ch['id'], ch['preview_hash'])
        w = next(x for x in self.e.overview()['webhooks'] if x['id'] == 'scalper')
        self.assertEqual((w['stream_id'], w['path']), (shown['stream_id'], shown['path']))


class RollbackKeepsPauses(Base):
    """A failed change's rollback removes files it created, but never a pause file."""
    def test_restore_keeps_created_pause_files(self):
        d = os.path.join(self.tmp, 'rb')
        os.mkdir(d)
        pause = os.path.join(d, 'ENTRIES-DISABLED-new')
        note = os.path.join(d, 'custom-switch')
        other = os.path.join(d, 'config-new.json')
        resumed = os.path.join(d, 'ENTRIES-DISABLED-x.removed-20211012T000000')
        for path, text in ((pause, ''), (note, json.dumps({'set_by': 'tvbridge-admin'})), (other, '{}'),
                           (resumed, '')):
            with open(path, 'w') as f:
                f.write(text)
        FileOps(self.profile).restore([{'path': p, 'sha': 'absent', 'copy': None}
                                       for p in (pause, note, other, resumed)])
        self.assertTrue(os.path.exists(pause))
        self.assertTrue(os.path.exists(note))
        self.assertFalse(os.path.exists(other))
        self.assertFalse(os.path.exists(resumed))
        self.assertFalse(is_pause_file(other))


class ExportScrubs(Base):
    """Export scrubs short stream IDs from free text, masks account names and ids, widens secret keys."""
    def test_redaction_helpers(self):
        import export
        self.assertEqual(export.redact_json({'session_cookie': 'abc', 'private_key': 'k', 'account_id': 100001,
                                             'account_name': 'DEMO-0001', 'label': 'Account A'}),
                         {'session_cookie': '<redacted>', 'private_key': '<redacted>', 'account_id': '…0001',
                          'account_name': '…0001', 'label': 'Account A'})
        out = export.scrub_words(b'id abcd12, xabcd12 and abcd12-x stay; abcd12.', [(b'abcd12', b'<redacted>')])
        self.assertEqual(out, b'id <redacted>, xabcd12 and abcd12-x stay; <redacted>.')

    def test_export_masks_accounts_and_short_stream_ids_in_free_text(self):
        reg_path = self.profile.st('webhooks.json')
        self.do('register_webhook', {'name': 'W2'})
        with open(reg_path) as f:
            reg = json.load(f)
        reg['webhooks'].append({'id': 'w3', 'name': 'W3', 'state': 'planned', 'path': '/webhook/w3-x',
                                'stream_id': 'short1', 'ticker': 'MNQZ2021', 'leader': None, 'followers': []})
        with open(reg_path, 'w') as f:
            json.dump(reg, f)
        self.e.store.put({'id': 'c-test', 'type': 'rotate_stream_id', 'status': 'cancelled', 'created_at': 1,
                          'summary': 'W3 short1 on DEMO-0001 (100001)', 'plan': {'files': []}})
        data, _, _ = self.e.export()
        import io
        import tarfile
        blob = b''
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tar:
            for m in tar.getmembers():
                blob += tar.extractfile(m).read()
        for secret in (b'short1', b'DEMO-0001', b'100001'):
            self.assertNotIn(secret, blob)
        self.assertIn('…0001'.encode(), blob)


if __name__ == '__main__':
    unittest.main()
