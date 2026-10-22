import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

import launch_setup as launch
import monitor


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.media = self.root / 'media with spaces'
        self.media.mkdir()
        self.work = self.root / 'setup state'

    def tearDown(self):
        self.temp.cleanup()

    def package(self):
        # Explicit source fixtures, not a native installation or the final release media.
        for name in launch.REQUIRED:
            target = self.media / name
            target.parent.mkdir(exist_ok=True, parents=True)
            shutil.copyfile(launch.ROOT / name, target)
        for name in ('app/decrypt-os-source.tar.gz', 'app/SOURCE-MANIFEST.json',
                     'dependencies/provision_runtime.py'):
            target = self.media / name
            target.parent.mkdir(exist_ok=True, parents=True)
            target.write_text('fixture only')
        return self.manifest()

    def manifest(self):
        lines = []
        for file in sorted(self.media.rglob('*')):
            if file.is_file() and file.name != 'SHA256SUMS':
                lines.append(hashlib.sha256(file.read_bytes()).hexdigest() + '  ' + file.relative_to(self.media).as_posix())
        text = '\n'.join(lines) + '\n'
        (self.media / 'SHA256SUMS').write_text(text)
        return hashlib.sha256(text.encode()).hexdigest()


class InputTests(Fixture):
    def test_packaged_reader_works_without_other_reports(self):
        digest = self.package()
        guided = launch.verify_inputs(self.media, digest, self.media)
        self.assertEqual(guided.current_state(self.work)['status'], 'not_started')
        self.assertFalse(self.work.exists())

    def test_reader_pin_matches_monitor_and_source(self):
        self.assertEqual(launch.GUIDED_SHA256, monitor.PIN)
        self.assertEqual(hashlib.sha256(monitor.GUIDED.read_bytes()).hexdigest(), monitor.PIN)

    def test_changed_reader_rejected_before_execute(self):
        digest = self.package()
        (self.media / 'guided/guided_setup.py').write_text('raise RuntimeError("must not execute")')
        with self.assertRaisesRegex(ValueError, 'does not match'):
            launch.verify_inputs(self.media, digest, self.media)

    def test_changed_monitor_rejected_before_start(self):
        digest = self.package()
        (self.media / 'setup/monitor.py').write_text('raise RuntimeError("must not execute")')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            launch.verify_inputs(self.media, digest, self.media)

    def test_changed_launcher_rejected(self):
        digest = self.package()
        (self.media / 'setup/launch_setup.py').write_text('changed')
        with self.assertRaises(ValueError):
            launch.verify_inputs(self.media, digest, self.media)

    def test_extra_file_rejected(self):
        digest = self.package()
        (self.media / 'unlisted').write_text('extra')
        with self.assertRaisesRegex(ValueError, 'unlisted'):
            launch.verify_inputs(self.media, digest, self.media)

    def test_missing_ui_rejected_even_with_new_manifest(self):
        self.package()
        (self.media / 'setup/app.js').unlink()
        with self.assertRaisesRegex(ValueError, 'missing the visual'):
            launch.verify_inputs(self.media, self.manifest(), self.media)

    def test_manifest_hash_is_external_not_inferred(self):
        self.package()
        with self.assertRaisesRegex(ValueError, 'separate reviewed receipt'):
            launch.verify_inputs(self.media, '0' * 64, self.media)

    def test_linked_media_rejected(self):
        digest = self.package()
        link = self.root / 'alias'
        link.symlink_to(self.media, target_is_directory=True)
        with self.assertRaises(ValueError):
            launch.verify_inputs(link, digest, link)

    def test_wrong_launcher_location_rejected(self):
        digest = self.package()
        with self.assertRaisesRegex(ValueError, 'from the verified'):
            launch.verify_inputs(self.media, digest)


class LaunchTests(Fixture):
    def test_root_refused_before_inputs(self):
        with patch.object(launch.os, 'getuid', return_value=0), patch.object(launch.os, 'geteuid', return_value=0):
            with self.assertRaisesRegex(ValueError, 'never through sudo'):
                launch.desktop_guard()

    def test_non_linux_refused(self):
        with patch.object(launch.os, 'getuid', return_value=1000), patch.object(launch.os, 'geteuid', return_value=1000), patch.object(launch.platform, 'system', return_value='Darwin'):
            with self.assertRaisesRegex(ValueError, 'Linux ARM64'):
                launch.desktop_guard()

    def test_desktop_required(self):
        with patch.object(launch.os, 'getuid', return_value=1000), patch.object(launch.os, 'geteuid', return_value=1000), patch.object(launch.platform, 'system', return_value='Linux'), patch.object(launch.platform, 'machine', return_value='aarch64'), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'Sign in'):
                launch.desktop_guard()

    def test_tty_required_before_inputs(self):
        with patch.object(launch.sys.stdin, 'isatty', return_value=False), patch.object(launch, 'verify_inputs') as verify:
            with self.assertRaisesRegex(ValueError, 'native terminal'):
                launch.terminal_session(self.media, '0' * 64, self.work)
            verify.assert_not_called()

    def test_argv_preserves_spaces_and_shell_metacharacters(self):
        args = launch.child_arguments(Path('/media/a $(do-not-run)'), 'a' * 64, Path('/home/test/a b'))
        self.assertIn('/media/a $(do-not-run)/setup/launch_setup.py', args)
        self.assertIn('/home/test/a b', args)
        self.assertNotIn('sh', args)

    def test_resolver_is_preferred(self):
        with patch.object(launch, 'executable', return_value='/usr/local/bin/xdg-terminal-exec'):
            self.assertEqual(launch.terminal_command(['python', 'a b']), ['/usr/local/bin/xdg-terminal-exec', '--title=Decrypt setup', '--', 'python', 'a b'])

    def test_foot_fallback(self):
        with patch.object(launch, 'executable', side_effect=[None, '/usr/bin/foot']):
            self.assertEqual(launch.terminal_command(['python']), ['/usr/bin/foot', '--title=Decrypt setup', '--', 'python'])

    def test_missing_terminal_refused(self):
        with patch.object(launch, 'executable', return_value=None):
            with self.assertRaisesRegex(ValueError, 'No supported terminal'):
                launch.terminal_command(['python'])

    def test_browser_loopback_only(self):
        with self.assertRaisesRegex(ValueError, 'Invalid local'):
            launch.browser_command('https://example.com/')

    def test_chromium_no_command_shell_or_debug_listener(self):
        with patch.object(launch, 'executable', return_value='/usr/bin/chromium'):
            self.assertEqual(launch.browser_command('http://127.0.0.1:1234/'), ['/usr/bin/chromium', '--no-first-run', '--no-default-browser-check', '--new-window', 'http://127.0.0.1:1234/'])

    def test_default_work_matches_preserved_guide_and_both_launch_modes(self):
        arguments = ['--media', str(self.media), '--manifest-sha256', 'a' * 64]
        expected = Path.home() / '.local/state/decrypt-setup'
        with patch.object(launch, 'desktop_guard'), patch.object(launch, 'verify_inputs'), patch.object(launch, 'browser_command'), patch.object(launch, 'terminal_command', side_effect=lambda value: value), patch.object(launch.subprocess, 'Popen') as popen:
            self.assertEqual(launch.main(arguments), 0)
            child = popen.call_args.args[0]
            self.assertEqual(child[child.index('--work') + 1], str(expected))
        with patch.object(launch, 'desktop_guard'), patch.object(launch, 'verify_inputs'), patch.object(launch.signal, 'signal'), patch.object(launch, 'terminal_session', return_value=0) as terminal:
            self.assertEqual(launch.main(arguments + ['--in-terminal']), 0)
            self.assertEqual(terminal.call_args.args[2], expected)
        guided = launch.load_guided()
        with patch.object(guided, 'current_state', return_value={'status': 'not_started'}) as read:
            self.assertEqual(guided.main(['status']), 0)
            self.assertEqual(read.call_args.args[0], expected)

    def test_no_browser_refused(self):
        with patch.object(launch, 'executable', return_value=None):
            with self.assertRaisesRegex(ValueError, 'No supported browser'):
                launch.browser_command('http://127.0.0.1:1234/')

    def test_monitor_owner_signal_only_unwinds(self):
        with self.assertRaises(SystemExit) as result:
            launch.interrupted_monitor_owner(signal.SIGTERM, None)
        self.assertEqual(result.exception.code, 143)

    def test_terminal_uses_inherited_io_and_one_guided_command(self):
        fake = Mock()
        fake.wait.return_value = 0
        with patch.object(launch.sys.stdin, 'isatty', return_value=True), patch.object(launch.sys.stdout, 'isatty', return_value=True), patch.object(launch, 'verify_inputs'), patch.object(launch, 'browser_command'), patch.object(launch, 'running_monitor', return_value=contextlib.nullcontext('http://127.0.0.1:1234/')), patch.object(launch, 'open_browser'), patch.object(launch.subprocess, 'Popen', return_value=fake) as popen, patch('builtins.input', return_value=''):
            self.assertEqual(launch.terminal_session(self.media, 'a' * 64, self.work), 0)
        self.assertEqual(popen.call_count, 1)
        command = popen.call_args.args[0]
        self.assertIn('start', command)
        self.assertEqual(popen.call_args.kwargs, {})
        fake.terminate.assert_not_called()
        fake.kill.assert_not_called()


class ProcessTests(Fixture):
    def request(self, url):
        port = int(url.split(':')[2].strip('/'))
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
        connection.request('GET', '/api/status')
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return data

    def test_real_monitor_lifecycle_writes_no_state(self):
        self.package()
        with launch.running_monitor(self.work, self.media, sys.executable) as url:
            self.assertEqual(self.request(url)['status'], 'not_started')
            self.assertFalse(self.work.exists())
        with self.assertRaises(OSError):
            self.request(url)
        self.assertFalse(self.work.exists())

    def test_closed_browser_process_cannot_stop_monitor(self):
        self.package()
        with launch.running_monitor(self.work, self.media, sys.executable) as url:
            with patch.object(launch, 'browser_command', return_value=[sys.executable, '-I', '-B', '-c', 'pass']):
                launch.open_browser(url)
            self.assertEqual(self.request(url)['status'], 'not_started')

    def test_monitor_stop_preserves_active_installer_process_and_record(self):
        self.package()
        self.work.mkdir(mode=0o700)
        state = {'schema': 'decrypt.guided-setup.v1', 'status': 'installing'}
        (self.work / 'STATE.json').write_text(json.dumps(state))
        code = "import fcntl,sys; f=open(sys.argv[1],'w'); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); sys.stdin.read()"
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', code, str(self.work / '.setup.lock')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            with launch.running_monitor(self.work, self.media, sys.executable) as url:
                got = self.request(url)
                self.assertEqual(got['status'], 'installing')
                self.assertTrue(got['operationActive'])
            self.assertIsNone(child.poll())
            self.assertEqual(json.loads((self.work / 'STATE.json').read_text()), state)
            got = launch.load_guided(self.media).current_state(self.work)
            self.assertEqual(got['status'], 'installing')
            self.assertTrue(got['operationActive'])
        finally:
            child.stdin.close()
            child.wait(timeout=3)
            child.stdout.close()

    def test_reader_failure_does_not_start_service(self):
        self.package()
        (self.media / 'guided/guided_setup.py').write_text('raise RuntimeError("changed")')
        with self.assertRaisesRegex(ValueError, 'did not start'):
            with launch.running_monitor(self.work, self.media, sys.executable):
                self.fail('A changed reader started.')
        self.assertFalse(self.work.exists())


if __name__ == '__main__':
    unittest.main()
