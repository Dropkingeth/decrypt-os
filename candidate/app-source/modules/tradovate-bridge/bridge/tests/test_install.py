"""Install lifecycle (scripts/install.py) in temp directories only.

systemctl is either absent from PATH or a logging stub; nothing touches the real user
manager, the real profile directory or the ports reserved for other browsers.
"""
import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tvbridge.journal import Journal  # noqa: E402

INSTALL = ROOT / 'scripts' / 'install.py'
NEVER_PROBE = {9555, 9566, 9567, 8812, 8777}  # reserved bridge port, other browsers, app signal ingress, example default
OLDER = '0.9.9'

STUB = r'''#!/bin/sh
echo "$*" >> "$STUB_LOG"
verb="$2"
shift 2
case "$verb" in
  is-active) if [ -e "$STUB_STATE/active-$1" ]; then echo active; exit 0; fi; echo inactive; exit 3;;
  is-enabled) if [ -e "$STUB_STATE/enabled-$1" ]; then echo enabled; exit 0; fi; echo disabled; exit 1;;
  stop) for u in "$@"; do /bin/rm -f "$STUB_STATE/active-$u"; done;;
  disable) for u in "$@"; do /bin/rm -f "$STUB_STATE/enabled-$u"; done;;
esac
exit 0
'''


def free_port():
    while True:
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
        if port not in NEVER_PROBE:
            return port


def snapshot(base):
    """Every path under base, except macOS system Python's own startup bytecode cache in $HOME."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(base):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            rel = os.path.relpath(p, base)
            if rel == 'home/Library' or rel.startswith('home/Library/'):
                continue
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ('link', os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ('dir', stat.S_IMODE(st.st_mode))
            else:
                with open(p, 'rb') as fh:
                    digest = hashlib.sha256(fh.read()).hexdigest()
                out[rel] = ('file', stat.S_IMODE(st.st_mode), st.st_mtime_ns, digest)
    return out


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


class InstallLifecycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = Path(tempfile.mkdtemp(prefix='tvb-build-'))
        proc = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'build_release.py'), str(cls.build / 'out')],
                              capture_output=True, text=True, check=True)
        archive = Path(json.loads(proc.stdout)['archive'])
        cls.archive = archive
        cls.name = archive.name[:-len('.tar.gz')]
        cls.version = cls.name[len('tvbridge-'):]
        with tarfile.open(archive) as tar:
            if hasattr(tarfile, 'data_filter'):
                tar.extractall(cls.build / 'extracted', filter='data')
            else:
                tar.extractall(cls.build / 'extracted')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.build, ignore_errors=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='tvb-install-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / 'root'
        self.releases = self.root / 'releases'
        self.releases.mkdir(parents=True, mode=0o700)
        shutil.copytree(self.build / 'extracted' / self.name, self.releases / self.name)
        shutil.copy(self.archive, self.releases)
        shutil.copy(str(self.archive) + '.sha256', self.releases)
        (self.releases / 'LATEST').write_text(self.name + '\n')
        self.units = self.tmp / 'units'
        self.profile = self.tmp / 'profile'
        self.home = self.tmp / 'home'
        self.home.mkdir()
        self.empty_path = self.tmp / 'empty-path'
        self.empty_path.mkdir()
        self.stub_path = self.tmp / 'stub-path'
        self.stub_path.mkdir()
        (self.stub_path / 'systemctl').write_text(STUB)
        os.chmod(self.stub_path / 'systemctl', 0o755)
        self.stub_state = self.tmp / 'stub-state'
        self.stub_state.mkdir()
        self.stub_log = self.tmp / 'systemctl.log'
        bindir = self.tmp / 'bin'
        bindir.mkdir()
        self.chromium = bindir / 'chromium'
        self.chromium.write_text('#!/bin/sh\nexit 0\n')
        os.chmod(self.chromium, 0o755)
        self.config = self.root / 'config.json'
        self.env = self.root / 'tvbridge.env'
        self.kill = self.root / 'ENTRIES-DISABLED'
        self.current = self.root / 'current'
        self.journal = self.root / 'journal'
        self.outputs = []

    def tearDown(self):
        token = self.token()
        if token:
            for out in self.outputs:
                self.assertNotIn(token, out, 'TVBRIDGE_TOKEN leaked into output')

    # ------------------------------------------------------------ helpers

    def run_tool(self, cmd, *extra, systemctl=False):
        env = {'HOME': str(self.home), 'PATH': str(self.stub_path if systemctl else self.empty_path),
               'STUB_LOG': str(self.stub_log), 'STUB_STATE': str(self.stub_state), 'LANG': 'C.UTF-8'}
        argv = [sys.executable, str(INSTALL), cmd, '--root', str(self.root), '--unit-dir', str(self.units),
                '--profile-dir', str(self.profile), '--chromium', str(self.chromium),
                '--python', sys.executable, *extra]
        proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
        self.outputs.append(proc.stdout + proc.stderr)
        return proc

    def install(self, release=None, **kw):
        proc = self.run_tool('install', '--release', release or self.name, **kw)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def token(self):
        if not self.env.is_file():
            return None
        for line in self.env.read_text().splitlines():
            if line.startswith('TVBRIDGE_TOKEN='):
                return line.split('=', 1)[1]
        return None

    def configure(self, **overrides):
        """Operator edit: real-looking values and free, non-reserved ports. Refreshes the units."""
        cfg = json.loads(self.config.read_text())
        cfg.update(account_name='SIM-51234-07', soup_account_id='sim-07', ticker='MNQZ2021')
        devtools = free_port()
        listen = free_port()
        while listen == devtools:
            listen = free_port()
        cfg.update(devtools_port=devtools, listen_port=listen)
        cfg.update(overrides)
        with open(self.config, 'w') as fh:  # keeps mode 0600
            json.dump(cfg, fh, indent=2)
        return cfg

    def systemctl_verbs(self):
        if not self.stub_log.exists():
            return []
        return [line.split()[1] for line in self.stub_log.read_text().splitlines() if line.strip()]

    def make_older_release(self):
        older = self.releases / f'tvbridge-{OLDER}'
        shutil.copytree(self.releases / self.name, older)
        manifest = json.loads((older / 'MANIFEST.json').read_text())
        manifest['version'] = OLDER
        (older / 'MANIFEST.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
        return older

    def unresolved_command(self):
        Journal(self.journal).start('cmd-unknown-1', kind='entry', trade_id='t-1')

    # ------------------------------------------------------------ plan

    def test_plan_changes_nothing(self):
        before = snapshot(self.tmp)
        proc = self.run_tool('plan', systemctl=True)  # default release from releases/LATEST
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertEqual(self.systemctl_verbs(), [])
        for text in ('ENTRIES-DISABLED', 'config.json', 'tvbridge.env', 'current', 'tvbridge-browser.service',
                     'tvbridge-daemon.service', 'not enabled, not started', 'To apply:'):
            self.assertIn(text, proc.stdout)
        self.assertIn(f'ExecStart={sys.executable} -m tvbridge.daemon --config {self.config}', proc.stdout)

        self.install()
        before = snapshot(self.tmp)
        proc = self.run_tool('plan', '--release', self.name)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertNotIn('create', proc.stdout.split('Actions:')[1])

    def test_plan_without_release_refuses(self):
        (self.releases / 'LATEST').unlink()
        before = snapshot(self.tmp)
        proc = self.run_tool('plan')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('install would REFUSE', proc.stdout)
        self.assertIn('no --release given', proc.stdout)
        self.assertIn('ENTRIES-DISABLED', proc.stdout)
        self.assertEqual(snapshot(self.tmp), before)

        empty_root = self.tmp / 'nothing-here'
        proc = subprocess.run([sys.executable, str(INSTALL), 'plan', '--root', str(empty_root),
                               '--unit-dir', str(self.units)], capture_output=True, text=True,
                              env={'HOME': str(self.home), 'PATH': str(self.empty_path)}, timeout=60)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(f'create   {empty_root}', proc.stdout)
        self.assertFalse(empty_root.exists())
        self.assertEqual(snapshot(self.tmp), before)

    # ------------------------------------------------------------ install

    def test_install_creates_layout_without_touching_systemd(self):
        proc = self.install(systemctl=True)
        self.assertEqual(self.systemctl_verbs(), [], 'install must not call systemctl')
        self.assertEqual(os.readlink(self.current), f'releases/{self.name}')
        self.assertEqual(mode(self.journal), 0o700)
        self.assertEqual(mode(self.config), 0o600)
        self.assertEqual(mode(self.env), 0o600)
        self.assertTrue(self.kill.exists())
        example = json.loads((self.releases / self.name / 'config.example.json').read_text())
        example.update(kill_switch_file=str(self.kill), journal_dir=str(self.journal))
        self.assertEqual(json.loads(self.config.read_text()), example)
        self.assertGreaterEqual(len(self.token()), 24)
        browser = (self.units / 'tvbridge-browser.service').read_text()
        daemon = (self.units / 'tvbridge-daemon.service').read_text()
        self.assertIn(f'ExecStart=/bin/sh {self.current}/scripts/launch-browser.sh', browser)
        self.assertIn(f'Environment=TVBRIDGE_PROFILE={self.profile}', browser)
        self.assertIn('Environment=TVBRIDGE_PASSWORD_STORE=gnome-libsecret', browser)   # never Chromium's basic store
        self.assertIn(f'Environment=TVBRIDGE_DEVTOOLS_PORT={example["devtools_port"]}', browser)
        self.assertIn(f'Environment=PATH={self.chromium.parent}:', browser)
        self.assertIn(f'WorkingDirectory={self.current}', daemon)
        self.assertIn(f'EnvironmentFile={self.env}', daemon)
        self.assertIn('systemctl --user start tvbridge-daemon.service', proc.stdout)
        self.assertFalse(self.profile.exists(), 'install must not create or touch the browser profile')
        self.assertTrue(self.units.is_dir())

    def test_install_is_idempotent_and_never_overwrites_operator_files(self):
        self.install()
        cfg = json.loads(self.config.read_text())
        cfg['account_name'] = 'OPERATOR-EDITED'
        self.config.write_text(json.dumps(cfg))
        with open(self.env, 'a') as fh:
            fh.write('SOUP_BROKER_TOKEN=operator-added-secret-value-000000\n')
        self.kill.write_text('operator note\n')
        before = snapshot(self.tmp)
        proc = self.install()
        self.assertIn('nothing changed', proc.stdout)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertNotIn('operator-added-secret-value', proc.stdout + proc.stderr)

        self.kill.unlink()
        self.install()
        self.assertTrue(self.kill.exists(), 'install recreates a missing kill switch')
        self.assertEqual(json.loads(self.config.read_text())['account_name'], 'OPERATOR-EDITED')

    def test_install_regenerates_units_after_port_change(self):
        self.install()
        cfg = self.configure()
        self.install()
        browser = (self.units / 'tvbridge-browser.service').read_text()
        self.assertIn(f'TVBRIDGE_DEVTOOLS_PORT={cfg["devtools_port"]}', browser)

    def test_tampered_release_refused(self):
        with open(self.releases / self.name / 'README.md', 'a') as fh:
            fh.write('tampered\n')
        before = snapshot(self.tmp)
        proc = self.run_tool('install', '--release', self.name)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('changed README.md', proc.stderr)
        self.assertEqual(snapshot(self.tmp), before)
        self.assertEqual(self.run_tool('plan', '--release', self.name).returncode, 1)

    def test_rehashed_manifest_caught_by_archive(self):
        rel = self.releases / self.name
        with open(rel / 'README.md', 'a') as fh:
            fh.write('tampered\n')
        manifest = json.loads((rel / 'MANIFEST.json').read_text())
        manifest['files']['README.md'] = hashlib.sha256((rel / 'README.md').read_bytes()).hexdigest()
        (rel / 'MANIFEST.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
        proc = self.run_tool('install', '--release', self.name)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('differs from the archive', proc.stderr)
        self.assertFalse(os.path.lexists(self.current))

    def test_release_outside_releases_dir_refused(self):
        outside = self.tmp / 'elsewhere' / self.name
        shutil.copytree(self.releases / self.name, outside)
        proc = self.run_tool('install', '--release', str(outside))
        self.assertEqual(proc.returncode, 1)
        self.assertIn('directly inside', proc.stderr)

    def test_install_refuses_foreign_unit_file(self):
        self.units.mkdir()
        (self.units / 'tvbridge-daemon.service').write_text('[Service]\nExecStart=/bin/true\n')
        proc = self.run_tool('install', '--release', self.name)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('not written by this tool', proc.stderr)
        self.assertFalse(os.path.lexists(self.current))

    def test_upgrade_refused_with_unresolved_journal(self):
        self.make_older_release()
        self.install(f'tvbridge-{OLDER}')
        self.unresolved_command()
        proc = self.run_tool('install', '--release', self.name)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('unresolved', proc.stderr)
        self.assertEqual(os.readlink(self.current), f'releases/tvbridge-{OLDER}')

    # ------------------------------------------------------------ preflight

    def ready(self, **overrides):
        self.install()
        cfg = self.configure(**overrides)
        if not ({cfg['devtools_port'], cfg['listen_port']} & {9566, 9567}):
            self.install()  # refresh units for the new ports
        return cfg

    def preflight(self, **kw):
        proc = self.run_tool('preflight', **kw)
        return proc.returncode, proc.stdout

    def test_preflight_passes_when_ready(self):
        self.ready()
        code, out = self.preflight()
        self.assertEqual(code, 0, out)
        self.assertIn('WARN  systemctl not available', out)
        code, out = self.preflight(systemctl=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(set(self.systemctl_verbs()), {'is-active', 'is-enabled'})

    def test_preflight_rejects_reserved_ports_without_probing(self):
        self.ready(devtools_port=9566)
        code, out = self.preflight()
        self.assertEqual(code, 1)
        self.assertIn('devtools_port 9566 is reserved', out)
        proc = self.run_tool('install', '--release', self.name)
        self.assertEqual(proc.returncode, 1, 'install must not write a unit for a reserved port')
        self.assertIn('reserved', proc.stderr)

        self.configure(listen_port=9567)
        code, out = self.preflight()
        self.assertIn('listen_port 9567 is reserved', out)

    def test_preflight_rejects_used_port(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        self.ready(listen_port=port)
        code, out = self.preflight()
        self.assertEqual(code, 1)
        self.assertIn(f'listen_port {port} is already in use', out)

    def test_preflight_rejects_placeholders(self):
        self.install()
        cfg = json.loads(self.config.read_text())
        cfg.update(devtools_port=free_port(), listen_port=free_port())  # never probe the example ports
        self.config.write_text(json.dumps(cfg))
        code, out = self.preflight()
        self.assertEqual(code, 1)
        self.assertIn('placeholder or empty values: account_name, soup_account_id', out)

    def test_preflight_requires_kill_switch(self):
        self.ready()
        self.kill.unlink()
        code, out = self.preflight()
        self.assertEqual(code, 1)
        self.assertIn('kill switch', out.split('FAIL')[1])

    def test_preflight_blocks_unresolved_journal(self):
        self.ready()
        self.unresolved_command()
        code, out = self.preflight()
        self.assertEqual(code, 1)
        self.assertIn('1 unresolved command', out)
        self.assertIn('1 unresolved', self.run_tool('status').stdout)

    def test_preflight_profile_in_use_and_missing_chromium(self):
        self.ready()
        self.profile.mkdir()
        os.symlink(f'{socket.gethostname()}-{os.getpid()}', self.profile / 'SingletonLock')
        self.chromium.unlink()
        code, out = self.preflight()
        self.assertEqual(code, 1)
        self.assertIn(f'SingletonLock held by running pid {os.getpid()}', out)
        self.assertIn('chromium not found', out)

    def test_preflight_rejects_active_unit_and_stale_units(self):
        self.ready()
        (self.stub_state / 'active-tvbridge-daemon.service').touch()
        code, out = self.preflight(systemctl=True)
        self.assertEqual(code, 1)
        self.assertIn('tvbridge-daemon.service is already active', out)
        (self.stub_state / 'active-tvbridge-daemon.service').unlink()
        self.configure()  # new ports, units not refreshed
        code, out = self.preflight()
        self.assertEqual(code, 1)
        self.assertIn('rerun install', out)

    # ------------------------------------------------------------ status

    def test_status_when_not_installed(self):
        proc = self.run_tool('status')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('current: not installed', proc.stdout)
        self.assertIn('systemctl not available', proc.stdout)

    def test_status_probes_health_without_printing_token(self):
        self.install()
        token = self.token()
        seen = []

        class Health(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append(self.path)
                ok = self.path == '/health/' + token
                body = json.dumps({'health': {'ok': True}, 'entries_disabled': True, 'publisher': {}}
                                  if ok else {'error': 'not found'}).encode()
                self.send_response(200 if ok else 404)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Health)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.configure(listen_port=server.server_address[1])

        proc = self.run_tool('status', systemctl=True)
        self.assertIn('health: not probed (daemon unit not reported active)', proc.stdout)
        self.assertEqual(seen, [])

        (self.stub_state / 'active-tvbridge-daemon.service').touch()
        (self.stub_state / 'enabled-tvbridge-daemon.service').touch()
        proc = self.run_tool('status', systemctl=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('health: HTTP 200', proc.stdout)
        self.assertIn('"entries_disabled": true', proc.stdout)
        self.assertIn('tvbridge-daemon.service: file present; active=active enabled=enabled', proc.stdout)
        self.assertIn(f'current: -> releases/{self.name} (version {self.version}, manifest ok)', proc.stdout)
        self.assertIn('kill switch: PRESENT', proc.stdout)
        self.assertEqual(len(seen), 1)
        self.assertNotIn(token, proc.stdout + proc.stderr)
        self.assertEqual(set(self.systemctl_verbs()), {'is-active', 'is-enabled'})

    # ------------------------------------------------------------ uninstall

    def test_uninstall_stops_units_and_keeps_data(self):
        self.install()
        j = Journal(self.journal)
        j.start('cmd-1', kind='entry', trade_id='t-1')
        j.finish('cmd-1', 'accepted', {'ok': True}, broker_order_id='42')
        for unit in ('tvbridge-browser.service', 'tvbridge-daemon.service'):
            (self.stub_state / f'active-{unit}').touch()
            (self.stub_state / f'enabled-{unit}').touch()
        keep = {p: p.read_bytes() for p in (self.config, self.env, self.kill, self.journal / 'commands.jsonl')}
        releases_before = snapshot(self.releases)

        proc = self.run_tool('uninstall', systemctl=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log = self.stub_log.read_text().splitlines()
        stops = [line for line in log if line.split()[1] == 'stop']
        self.assertEqual(stops, ['--user stop tvbridge-daemon.service', '--user stop tvbridge-browser.service'])
        self.assertIn('--user disable tvbridge-daemon.service', log)
        self.assertEqual(log[-1], '--user daemon-reload')
        self.assertFalse(set(self.systemctl_verbs()) & {'start', 'enable', 'restart'})
        self.assertFalse((self.units / 'tvbridge-browser.service').exists())
        self.assertFalse((self.units / 'tvbridge-daemon.service').exists())
        self.assertFalse(os.path.lexists(self.current))
        for path, data in keep.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertEqual(snapshot(self.releases), releases_before)
        self.assertIn('kept:', proc.stdout)
        self.assertIn(str(self.env), proc.stdout)

    def test_uninstall_without_systemctl_restores_kill_switch(self):
        self.install()
        self.kill.unlink()
        proc = self.run_tool('uninstall')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.kill.exists())
        self.assertFalse(os.path.lexists(self.current))
        self.assertTrue(self.config.exists() and self.env.exists() and self.journal.is_dir())
        self.assertEqual(self.run_tool('uninstall').returncode, 0, 'uninstall is repeatable')

    # ------------------------------------------------------------ rollback

    def test_rollback(self):
        self.make_older_release()
        self.install()
        proc = self.run_tool('rollback', '--release', f'tvbridge-{OLDER}')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(os.readlink(self.current), f'releases/tvbridge-{OLDER}')
        self.assertIn('systemctl --user restart tvbridge-daemon.service', proc.stdout)

        proc = self.run_tool('rollback', '--release', self.name)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('not older', proc.stderr)

    def test_rollback_refused_with_unresolved_commands(self):
        self.make_older_release()
        self.install()
        self.unresolved_command()
        proc = self.run_tool('rollback', '--release', f'tvbridge-{OLDER}')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('unresolved', proc.stderr)
        self.assertEqual(os.readlink(self.current), f'releases/{self.name}')

    def test_rollback_refuses_tampered_release(self):
        older = self.make_older_release()
        self.install()
        (older / 'tvbridge' / 'daemon.py').write_text('# tampered\n')
        proc = self.run_tool('rollback', '--release', f'tvbridge-{OLDER}')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('changed tvbridge/daemon.py', proc.stderr)
        self.assertEqual(os.readlink(self.current), f'releases/{self.name}')


if __name__ == '__main__':
    unittest.main()
