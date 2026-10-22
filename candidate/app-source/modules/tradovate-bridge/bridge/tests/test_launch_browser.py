"""scripts/launch-browser.sh profile ownership and window identity (1.1.0 multi-login).

Runs the script with a stub browser binary (TVBRIDGE_BROWSER_BIN) and HOME in a temp dir;
no real browser is started.
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'scripts' / 'launch-browser.sh'


def _python_binary():
    """The interpreter that keeps argv[0]: macOS framework Pythons re-exec from a stub, losing it."""
    if sys.platform != 'darwin':
        return sys.executable
    probe = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])
    try:
        for _ in range(50):
            comm = subprocess.run(['ps', '-o', 'comm=', '-p', str(probe.pid)], capture_output=True,
                                  text=True).stdout.strip()
            if comm.startswith('/') and 'Python' in comm and os.path.exists(comm):
                return comm
            time.sleep(0.05)
    finally:
        probe.kill()
        probe.wait()
    return sys.executable


class LaunchBrowser(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='tvb-launch-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / 'home'
        self.home.mkdir()
        self.args_file = self.tmp / 'args'
        self.stub = self.tmp / 'browser'
        self.stub.write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done > "$ARGS_FILE"\n')
        os.chmod(self.stub, 0o755)

    def run_script(self, **env):
        base = {'HOME': str(self.home), 'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                'TVBRIDGE_BROWSER_BIN': str(self.stub), 'ARGS_FILE': str(self.args_file)}
        base.update({k: str(v) for k, v in env.items()})
        if self.args_file.exists():
            self.args_file.unlink()
        return subprocess.run(['/bin/sh', str(SCRIPT)], env=base, capture_output=True, text=True, timeout=30)

    def launched(self):
        return self.args_file.read_text().splitlines() if self.args_file.exists() else None

    def test_login_a_default_is_unchanged(self):
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        profile = self.home / '.local/share/decrypt-os/tvbridge-browser' / 'profile'
        self.assertEqual(self.launched(), [
            f'--user-data-dir={profile}', '--remote-debugging-port=9555', '--remote-debugging-address=127.0.0.1',
            '--no-first-run', '--no-default-browser-check', '--disable-background-timer-throttling',
            '--disable-backgrounding-occluded-windows', '--disable-renderer-backgrounding',
            '--password-store=gnome-libsecret', 'https://trader.tradovate.com/'])
        self.assertFalse((profile / '.tvbridge-login').exists(), 'login A writes no marker')

    def test_login_a_path_matches_previous_release(self):
        """Without TVBRIDGE_LOGIN_ID none of the multi-login checks run (1.0.9.5.2 parity)."""
        profile = self.tmp / 'profile-b'
        profile.mkdir()
        (profile / '.tvbridge-login').write_text('b\n')
        proc = self.run_script(TVBRIDGE_PROFILE=profile, TVBRIDGE_DEVTOOLS_PORT=9558)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('--remote-debugging-port=9558', self.launched())
        holder = self.fake_browser(profile, 9557)
        os.symlink(f'{socket.gethostname()}-{holder.pid}', profile / 'SingletonLock')
        self.assertEqual(self.run_script(TVBRIDGE_PROFILE=profile, TVBRIDGE_DEVTOOLS_PORT=9558).returncode, 0)
        for port in (9566, 9567):
            proc = self.run_script(TVBRIDGE_DEVTOOLS_PORT=port)
            self.assertEqual(proc.returncode, 1, port)
            self.assertEqual(proc.stderr.strip(), f'refusing reserved port {port}')
        self.assertEqual(self.run_script(TVBRIDGE_DEVTOOLS_PORT=9556).returncode, 0, 'as in 1.0.9.5.2')

    def test_login_b_marks_its_profile_and_labels_its_window(self):
        profile = self.home / '.local/share/decrypt-os/tvbridge-browser' / 'profile-b'
        proc = self.run_script(TVBRIDGE_LOGIN_ID='b', TVBRIDGE_DEVTOOLS_PORT=9557, TVBRIDGE_PROFILE=profile)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = self.launched()
        self.assertIn('--remote-debugging-port=9557', args)
        self.assertIn(f'--user-data-dir={profile}', args)
        self.assertIn('--class=tvbridge-login-b', args)
        self.assertIn('--window-name=Tradovate Bridge — Login b', args)
        self.assertEqual(args[-1], 'https://trader.tradovate.com/')
        self.assertEqual((profile / '.tvbridge-login').read_text(), 'b\n')
        self.assertEqual(os.stat(profile / '.tvbridge-login').st_mode & 0o777, 0o600)
        again = self.run_script(TVBRIDGE_LOGIN_ID='b', TVBRIDGE_DEVTOOLS_PORT=9557, TVBRIDGE_PROFILE=profile)
        self.assertEqual(again.returncode, 0, again.stderr)

    def test_password_store_uses_the_keyring_and_never_basic(self):
        proc = self.run_script(TVBRIDGE_LOGIN_ID='a', TVBRIDGE_PASSWORD_STORE='gnome-libsecret')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = self.launched()
        self.assertIn('--password-store=gnome-libsecret', args)
        self.assertIn('--class=tvbridge-login-a', args)
        self.assertEqual(args[-1], 'https://trader.tradovate.com/')
        for bad in ('basic', 'gnome-libsecret --no-sandbox', 'x', ''):   # set but empty is refused too
            proc = self.run_script(TVBRIDGE_LOGIN_ID='a', TVBRIDGE_PASSWORD_STORE=bad)
            self.assertEqual(proc.returncode, 1, bad)
            self.assertIn('TVBRIDGE_PASSWORD_STORE', proc.stderr)

    def test_another_logins_profile_is_refused(self):
        profile = self.tmp / 'profile-b'
        profile.mkdir()
        (profile / '.tvbridge-login').write_text('b\n')
        for login_id in ('c', 'a'):
            proc = self.run_script(TVBRIDGE_PROFILE=profile, TVBRIDGE_DEVTOOLS_PORT=9558, TVBRIDGE_LOGIN_ID=login_id)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("belongs to login 'b'", proc.stderr)
            self.assertIsNone(self.launched())

    def test_unmarked_profile_with_data_is_refused_for_other_logins(self):
        profile = self.tmp / 'old-profile'
        (profile / 'Default').mkdir(parents=True)
        proc = self.run_script(TVBRIDGE_LOGIN_ID='b', TVBRIDGE_PROFILE=profile, TVBRIDGE_DEVTOOLS_PORT=9557)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('no .tvbridge-login marker', proc.stderr)
        self.assertFalse((profile / '.tvbridge-login').exists())
        default = self.home / '.local/share/decrypt-os/tvbridge-browser' / 'profile'
        proc = self.run_script(TVBRIDGE_LOGIN_ID='b', TVBRIDGE_PROFILE=default, TVBRIDGE_DEVTOOLS_PORT=9557)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("login A's default profile", proc.stderr)
        # Login A keeps using its existing unmarked profile.
        self.assertEqual(self.run_script(TVBRIDGE_PROFILE=profile).returncode, 0)
        self.assertFalse((profile / '.tvbridge-login').exists())

    def test_reserved_ports_and_bad_ids_refused(self):
        for port in (9556, 9566, 9567, 'x9'):
            proc = self.run_script(TVBRIDGE_DEVTOOLS_PORT=port, TVBRIDGE_LOGIN_ID='b', TVBRIDGE_PROFILE=self.tmp / 'p')
            self.assertEqual(proc.returncode, 1, port)
        for login_id in ('B', '1b', 'b_c', 'b' * 17):
            proc = self.run_script(TVBRIDGE_LOGIN_ID=login_id, TVBRIDGE_PROFILE=self.tmp / 'p',
                                   TVBRIDGE_DEVTOOLS_PORT=9557)
            self.assertEqual(proc.returncode, 1, login_id)
        self.assertIsNone(self.launched())

    def fake_browser(self, profile, port, name='chromium'):
        """A live process whose argv looks like a browser (argv[0] = name) holding `profile` on `port`."""
        holder = subprocess.Popen([name, '-c', 'import time; time.sleep(60)', f'--user-data-dir={profile}',
                                   f'--remote-debugging-port={port}'], executable=_python_binary())
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        return holder

    def test_profile_locked_by_a_browser_on_another_port_is_refused(self):
        profile = self.tmp / 'profile-b'
        profile.mkdir()
        (profile / '.tvbridge-login').write_text('b\n')
        holder = self.fake_browser(profile, 9557)
        os.symlink(f'{socket.gethostname()}-{holder.pid}', profile / 'SingletonLock')
        env = dict(TVBRIDGE_LOGIN_ID='b', TVBRIDGE_PROFILE=profile)
        proc = self.run_script(TVBRIDGE_DEVTOOLS_PORT=9558, **env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('locked by running pid', proc.stderr)
        self.assertEqual(self.run_script(TVBRIDGE_DEVTOOLS_PORT=9557, **env).returncode, 0)
        holder.kill()
        holder.wait()
        self.assertEqual(self.run_script(TVBRIDGE_DEVTOOLS_PORT=9558, **env).returncode, 0, 'stale lock')

    def test_lock_held_by_something_else_is_ignored(self):
        profile = self.tmp / 'profile-b'
        profile.mkdir()
        (profile / '.tvbridge-login').write_text('b\n')
        env = dict(TVBRIDGE_LOGIN_ID='b', TVBRIDGE_PROFILE=profile, TVBRIDGE_DEVTOOLS_PORT=9558)
        lock = profile / 'SingletonLock'
        # A reused pid that is not a browser, and a browser for another profile.
        for holder in (self.fake_browser(profile, 9557, name='python-worker'),
                       self.fake_browser(self.tmp / 'other-profile', 9557)):
            os.symlink(f'{socket.gethostname()}-{holder.pid}', lock)
            proc = self.run_script(**env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lock.unlink()
        # The lock names the launching shell itself.
        stub = self.tmp / 'lock-self.sh'
        stub.write_text(f'ln -s "$(hostname 2>/dev/null || uname -n)-$$" "{lock}"\nexec /bin/sh "{SCRIPT}"\n')
        base = {'HOME': str(self.home), 'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                'TVBRIDGE_BROWSER_BIN': str(self.stub), 'ARGS_FILE': str(self.args_file),
                'TVBRIDGE_LOGIN_ID': 'b', 'TVBRIDGE_PROFILE': str(profile), 'TVBRIDGE_DEVTOOLS_PORT': '9558'}
        proc = subprocess.run(['/bin/sh', str(stub)], env=base, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)

if __name__ == '__main__':
    unittest.main()
