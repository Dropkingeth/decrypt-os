"""Per-UID loopback firewall: generated nftables ruleset and its installation."""
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
APP = HERE.parent / 'app'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load('loopback_guard_under_test', HERE / 'loopback_guard.py')
clean_base = load('clean_base_for_guard', HERE / 'clean_base.py')
ingress = load('customer_ingress_for_guard', HERE / 'customer_ingress.py')
market = load('market_browser_for_guard', HERE / 'market_browser.py')
bootstrap = load('bootstrap_for_guard', APP / 'harness/bootstrap.py')
sys.path.insert(0, str(APP / 'backend'))
guardian_browser = load('guardian_browser_for_guard', APP / 'backend/guardian_browser.py')
runtime = load('runtime_for_guard', APP / 'harness/runtime.py')


def rules(text):
    """(selector, uid range, blocked port set) for every reject rule."""
    out = []
    for line in text.splitlines():
        m = re.fullmatch(r'\s*meta (skuid|skgid) (\d+)(?:-(\d+))? tcp dport \{ ([0-9, -]+) \} reject with tcp reset', line)
        if m:
            ports = set()
            for item in m.group(4).split(', '):
                a, _, b = item.partition('-')
                ports |= set(range(int(a), int(b or a) + 1))
            out.append((m.group(1), (int(m.group(2)), int(m.group(3) or m.group(2))), ports))
    return out


def blocked(text, selector, uid):
    return set().union(*[p for s, (lo, hi), p in rules(text) if s == selector and lo <= uid <= hi])


# Every trading/debug listener the OS and the bridge module use (bridge ports from
# modules/tradovate-bridge: config examples, install.py and the admin server).
BRIDGE = {8777, 8778, 8779, 8780, 8781, 8782}
DEVTOOLS = {9555, 9556, 9557, 9558, 9559, guardian_browser.PORT, market.PORT}
APP_PORTS = {8810, 8811}


class Ruleset(unittest.TestCase):
    def test_structure_is_one_atomic_output_table(self):
        text = guard.render()
        self.assertTrue(text.startswith('table inet decrypt_os_loopback\ndelete table inet decrypt_os_loopback\n'))
        self.assertIn('type filter hook output priority -5; policy accept;', text)
        self.assertEqual(text.count('{'), text.count('}'))
        lines = [l for l in text.splitlines() if 'meta ' in l]
        self.assertEqual(len(rules(text)), len(lines), 'every rule must parse as a reject rule')
        self.assertNotIn('accept\n    meta', text)

    def test_dynamic_users_cannot_reach_trading_or_debug_ports(self):
        text = guard.render()
        for selector in ('skuid', 'skgid'):
            for uid in (61184, 62000, 65519):
                ports = blocked(text, selector, uid)
                self.assertLessEqual(BRIDGE | DEVTOOLS | APP_PORTS, ports, (selector, uid))
        self.assertEqual(blocked(text, 'skuid', 61183), set())
        self.assertEqual(blocked(text, 'skuid', 65520), set())
        self.assertEqual(blocked(text, 'skuid', 1000), set(), 'the desktop user is never filtered')

    def test_sandbox_keeps_its_own_services(self):
        ports = blocked(guard.render(), 'skuid', 62000)
        for own in (bootstrap.WEB_READER_PROXY_PORT, bootstrap.PORT, runtime.PORT):
            self.assertNotIn(own, ports)
        self.assertNotIn(guard.INGRESS_PORT, ports, 'Caddy (DynamicUser) must reach the signal ingress')

    def test_ingress_upstream_matches(self):
        self.assertIn(f"'dial': '127.0.0.1:{guard.INGRESS_PORT}'", Path(ingress.__file__).read_text())

    def test_static_assistant_account_is_covered_when_present(self):
        text = guard.render(971)
        ports = blocked(text, 'skuid', 971)
        self.assertLessEqual(BRIDGE | DEVTOOLS | APP_PORTS | {guard.INGRESS_PORT}, ports)
        self.assertNotIn(bootstrap.PORT, ports)
        self.assertNotIn(bootstrap.WEB_READER_PROXY_PORT, ports)
        self.assertNotIn('skuid 971', guard.render())

    def test_invalid_static_uid_is_refused(self):
        for value in (0, 1000, 62000, '971', True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                guard.render(value)

    def test_static_uid_lookup(self):
        account = mock.Mock(pw_uid=971, pw_gid=971)
        with mock.patch.object(guard.pwd, 'getpwnam', return_value=account):
            self.assertEqual(guard.static_uid(), 971)
        with mock.patch.object(guard.pwd, 'getpwnam', side_effect=KeyError):
            self.assertIsNone(guard.static_uid())
        with mock.patch.object(guard.pwd, 'getpwnam', return_value=mock.Mock(pw_uid=0, pw_gid=0)):
            with self.assertRaises(ValueError):
                guard.static_uid()

    def test_apply_feeds_nft_on_stdin_without_shell(self):
        calls = []
        def fake_run(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, '', '')
        with mock.patch.object(guard.subprocess, 'run', fake_run), \
             mock.patch.object(guard.sys, 'platform', 'linux'), \
             mock.patch.object(guard.os, 'geteuid', return_value=0), \
             mock.patch.object(guard, 'static_uid', return_value=None):
            guard.apply(); guard.apply(check_only=True)
        self.assertEqual(calls[0][0], ['/usr/bin/nft', '-f', '-'])
        self.assertEqual(calls[1][0], ['/usr/bin/nft', '-c', '-f', '-'])
        self.assertEqual(calls[0][1]['input'], guard.render())
        self.assertNotIn('shell', calls[0][1])

    def test_failed_load_is_an_error(self):
        with mock.patch.object(guard.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'x')), \
             mock.patch.object(guard.sys, 'platform', 'linux'), \
             mock.patch.object(guard.os, 'geteuid', return_value=0), \
             mock.patch.object(guard, 'static_uid', return_value=None):
            with self.assertRaises(SystemExit):
                guard.apply()

    @unittest.skipUnless(shutil.which('nft') and sys.platform == 'linux' and os.geteuid() == 0,
                         'nft syntax check needs Linux root with nftables')
    def test_nft_accepts_the_ruleset(self):
        for static in (None, 971):
            result = subprocess.run(['nft', '-c', '-f', '-'], input=guard.render(static), text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)


class Installation(unittest.TestCase):
    def test_unit_and_wants_dropin_are_rendered(self):
        files = clean_base.generated_files()
        unit = files['etc/systemd/system/decrypt-os-loopback-guard.service'][0].decode()
        self.assertIn('ExecStart=/usr/bin/python3 -I -B /usr/local/lib/decrypt-os/installer/loopback_guard.py apply', unit)
        self.assertIn('Type=oneshot', unit)
        self.assertIn('RemainAfterExit=yes', unit)
        self.assertIn('Before=network-pre.target systemd-user-sessions.service display-manager.service', unit)
        dropin = files['etc/systemd/system/multi-user.target.d/decrypt-os-loopback-guard.conf'][0]
        self.assertEqual(dropin, b'[Unit]\nWants=decrypt-os-loopback-guard.service\n')
        self.assertIn('installer/loopback_guard.py', clean_base.COPY_FILES)

    def test_bundle_ships_the_guard(self):
        bundler = load('bundle_for_guard', HERE.parent / 'lab/build_source_bundle.py')
        self.assertIn('installer/loopback_guard.py', bundler.CUSTOMER_BASE_SOURCE)

    def test_ingress_requires_the_guard_and_hides_proc(self):
        text = ingress.service_unit().decode()
        self.assertIn('Requires=decrypt-os-loopback-guard.service', text)
        self.assertIn('After=network-online.target decrypt-os-loopback-guard.service', text)
        self.assertIn('ProtectProc=invisible', text)

    def test_assistant_start_reloads_the_guard_first(self):
        source = (APP / 'harness/runtime.py').read_text()
        start = source[source.index('def start():'):source.index('def stop(')]
        self.assertLess(start.index('"restart", LOOPBACK_GUARD_UNIT'), start.index('systemd-run'))
        self.assertEqual(runtime.LOOPBACK_GUARD_UNIT, guard.UNIT_NAME)


if __name__ == '__main__':
    unittest.main()
