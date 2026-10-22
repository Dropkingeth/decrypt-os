"""Isolated installer/state fixtures. No Caddy, native service, DNS or TLS action."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

import customer_ingress as m


class FixtureHost(m.Host):
    def __init__(self, root):
        super().__init__(root, os.getuid())
        self.expected_origin = 'https://alerts.customer.example.com'
        self.socket_rows = [{'port': 8812, 'uid': 1000, 'ipv4Loopback': True}]
        self.commands = []
        self.active = self.enabled = False
        self.validation_failure = self.start_failure = False
        self.other_proxy = False
        self.dropins = False

    def configured_origin(self, enrollment):
        return self.expected_origin

    def listeners(self):
        return self.socket_rows

    def run(self, arguments, **kwargs):
        self.commands.append((arguments, kwargs))
        if arguments == ['/usr/bin/caddy', 'version']:
            return 0, b'v2.11.2 fixture\n'
        if arguments[:2] == ['/usr/bin/caddy', 'validate']:
            # Fixture checks contract, not the actual Caddy parser/module schema.
            config = m.strict(Path(arguments[-1]).read_bytes())
            if config != m.strict(m.caddy_config(self.expected_origin)):
                raise AssertionError('Unexpected config passed to validation')
            return (1 if self.validation_failure else 0), b''
        if arguments[:2] == ['/usr/bin/systemctl', 'show']:
            if self.other_proxy and arguments[2] == 'caddy.service':
                return 0, b'LoadState=loaded\nActiveState=active\nUnitFileState=enabled\nFragmentPath=/usr/lib/systemd/system/caddy.service\nDropInPaths=\n'
            if arguments[2] == m.UNIT_NAME and self.path(m.UNIT).exists():
                return 0, ('LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\nFragmentPath=' + m.UNIT
                           + '\nDropInPaths=' + ('/fixture/override.conf' if self.dropins else '') + '\n').encode()
            return 0, b'LoadState=not-found\nActiveState=inactive\nUnitFileState=disabled\nFragmentPath=\nDropInPaths=\n'
        if arguments == ['/usr/bin/systemctl', 'daemon-reload']:
            return 0, b''
        if arguments == ['/usr/bin/systemctl', 'enable', '--now', m.UNIT_NAME]:
            if self.start_failure:
                return 1, b''
            self.active = self.enabled = True
            return 0, b''
        if arguments == ['/usr/bin/systemctl', 'is-active', m.UNIT_NAME]:
            return (0, b'active') if self.active else (3, b'inactive')
        if arguments == ['/usr/bin/systemctl', 'is-enabled', m.UNIT_NAME]:
            return (0, b'enabled') if self.enabled else (1, b'disabled')
        raise AssertionError('Unexpected command: ' + repr(arguments))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='customer-ingress-')
        self.root = Path(self.temp.name)
        self.host = FixtureHost(self.root)
        for name in ('etc/decrypt-os', 'etc/systemd/system', 'var/lib', 'usr/bin', m.INSTALL.lstrip('/')):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        helper = Path(m.__file__).read_bytes()
        machine = 'a' * 32
        self.put('/etc/machine-id', machine.encode())
        self.put(m.ENTRY, helper)
        self.put('/usr/bin/caddy', b'fixture-binary-not-executed', 0o755)
        control=Path(m.__file__).with_name('customer_control.py').read_bytes()
        self.put(m.INSTALL+'/installer/customer_control.py',control)
        self.manifest = {'schema': 'decrypt.os.source-bundle.v1', 'files': [
            {'path': 'installer/customer_ingress.py', 'bytes': len(helper), 'sha256': m.sha(helper)}, {'path':'installer/customer_control.py','bytes':len(control),'sha256':m.sha(control)}]}
        manifest_raw = m.canonical(self.manifest)
        self.put(m.INSTALL + '/SOURCE-MANIFEST.json', manifest_raw)
        self.enrollment = {'schema': 'decrypt.customer-test.v1', 'role': 'clean-customer-test',
                           'machineSha256': m.sha(machine.encode()), 'desktopUser': 'customer', 'desktopUid': 1000,
                           'sourceManifestSha256': m.sha(manifest_raw),'instanceId':'11111111-2222-4333-8444-555555555555','imageId':'fixture-image','policySha256':'b'*64}
        self.put('/etc/decrypt-os/customer-test.json', m.canonical(self.enrollment))
        self.put(m.INSTALL + '/INSTALL-RECEIPT.json', m.canonical({
            'schema': 'decrypt.firstboot-install-receipt.v1', 'installed': True, 'stageOnly': False,
            'targetMachineSha256': self.enrollment['machineSha256'], 'desktopUser': 'customer',
            'sourceManifestSha256': self.enrollment['sourceManifestSha256'], 'desktopUid':1000, **{k:self.enrollment[k] for k in ('instanceId','imageId','policySha256')}}))
        self.policy={'schema':'decrypt.customer-host-policy.v2','accountName':'customer',**{k:self.enrollment[k] for k in ('machineSha256','instanceId','imageId','policySha256','desktopUid','sourceManifestSha256')}}
        self.put(m.INSTANCE_POLICY,m.canonical(self.policy))
        self.put('/var/lib/decrypt-firstboot/state.json',m.canonical({'schema':'decrypt.firstboot-state.v1','phase':'notice','status':'pending','accountName':'customer','seq':4,'failure':None,'completedAt':None,'notice':{'documentSha256':'c'*64,'accepted':False,'acceptedAt':None},**{k:self.enrollment[k] for k in ('instanceId','imageId','policySha256')}}),0o600)

    def tearDown(self):
        self.temp.cleanup()

    def put(self, name, raw, mode=0o644):
        path = self.host.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(mode)

    def prepare(self):
        self.prepared = m.prepare(self.host, self.host.expected_origin)
        return self.prepared['planSha256']

    def apply(self, pin=None):
        return m.apply(self.host, pin or self.prepare(), controls_domain=True, public_service=True)

    def assertRefused(self, code, function, *args, **kwargs):
        with self.assertRaisesRegex(m.Refused, '^' + code + '$'):
            function(*args, **kwargs)

    def test_origin_strict_public_canonical_fqdn(self):
        self.assertEqual(m.origin('https://signals.customer.example.com'), ('https://signals.customer.example.com', 'signals.customer.example.com'))
        for value in ('http://alerts.customer.example.com', 'https://localhost', 'https://127.0.0.1', 'https://[::1]',
                      'https://alerts.customer.example.com:443', 'https://alerts.customer.example.com/', 'https://alerts.customer.example.com/a',
                      'https://user@alerts.customer.example.com', 'https://alerts.customer.example.com?x=1', 'https://Alerts.customer.example.com',
                      'https://bad-.customer.example.com', 'https://a..net', 'https://a.local', 'https://a.test',
                      'https://a.invalid', 'https://a.example', 'https://a.home.arpa', 'https://a.net\n'):
            with self.subTest(value=value), self.assertRaises(m.Refused):
                m.origin(value)

    def test_config_only_exact_post_signal_loopback(self):
        value = m.strict(m.caddy_config(self.host.expected_origin))
        server = value['apps']['http']['servers']['signals']
        self.assertEqual(server['listen'], [':443'])
        self.assertEqual(server['protocols'], ['h1', 'h2'])
        self.assertTrue(server['automatic_https']['disable_redirects'])
        self.assertTrue(server['strict_sni_host'])
        self.assertEqual(server['routes'][0]['match'], [
            {'host': ['alerts.customer.example.com'], 'method': ['POST'], 'path': ['/signal/*']}])
        handlers = server['routes'][0]['handle'][0]['routes']
        self.assertEqual(handlers[0]['handle'][0], {'handler': 'request_body', 'max_size': 40960})
        proxy = handlers[1]['handle'][0]
        self.assertEqual(proxy['upstreams'], [{'dial': '127.0.0.1:8812'}])
        self.assertEqual(proxy['headers']['request']['set'], {'Host': ['alerts.customer.example.com'],
                         'X-Forwarded-Host': ['alerts.customer.example.com'], 'X-Forwarded-Proto': ['https']})
        self.assertEqual(server['routes'][1]['handle'][0]['status_code'], 404)
        self.assertTrue(value['apps']['tls']['automation']['policies'][0]['issuers'][0]['challenges']['http']['disabled'])

    def test_config_no_uri_access_or_error_logs_no_admin_api(self):
        raw = m.caddy_config(self.host.expected_origin)
        value = m.strict(raw)
        self.assertEqual(value['admin'], {'disabled': True})
        self.assertEqual(value['logging'], {'logs': {'default': {'writer': {'output': 'discard'}}}})
        self.assertNotIn('logs', value['apps']['http']['servers']['signals'])
        for text in (b'8811', b'8810', b'19789', b'9566', b'9567', b'access.decryptlabs.io', b'token', b'password'):
            self.assertNotIn(text, raw)

    def test_unit_persistent_private_dynamic_user_and_exact_privileges(self):
        text = m.service_unit().decode()
        for directive in ('DynamicUser=yes', 'StateDirectory=decrypt-os-ingress', 'StateDirectoryMode=0700',
                          'UMask=0077', 'ProtectSystem=strict', 'ProtectHome=yes', 'NoNewPrivileges=yes',
                          'CapabilityBoundingSet=CAP_NET_BIND_SERVICE', 'AmbientCapabilities=CAP_NET_BIND_SERVICE',
                          'StandardOutput=null', 'StandardError=null', 'WantedBy=multi-user.target',
                          'ExecStartPre=+/usr/bin/python3 -I -B ' + m.ENTRY + ' verify-start'):
            self.assertIn(directive, text)
        self.assertNotIn('ExecReload=', text)
        self.assertNotIn('--resume', text)

    def test_prepare_validates_before_any_system_service_install(self):
        result = m.prepare(self.host, self.host.expected_origin)
        self.assertFalse(self.host.path(m.UNIT).exists())
        self.assertFalse(self.host.path(m.CONFIG).exists())
        self.assertFalse(self.host.path(m.SETUP + '/ATTEMPT.json').exists())
        self.assertTrue(any(a[:2] == ['/usr/bin/caddy', 'validate'] for a, _ in self.host.commands))
        self.assertFalse(any(a[0] == '/usr/bin/systemctl' for a, _ in self.host.commands))
        self.assertEqual(self.host.path(m.SETUP).stat().st_mode & 0o777, 0o700)
        self.assertFalse(result['publicTlsVerified'])

    def test_prepare_origin_must_match_saved_customer_origin(self):
        self.assertRefused('configured_origin_mismatch', m.prepare, self.host, 'https://other.customer.example.com')
        self.assertFalse(self.host.path(m.SETUP).exists())

    def test_repeated_prepare_same_second_retains_distinct_review_plans(self):
        original = m.time.time
        try:
            m.time.time = lambda: 1900000000
            first = self.prepare()
            second = self.prepare()
        finally:
            m.time.time = original
        self.assertNotEqual(first, second)
        self.assertTrue(self.host.path(m.SETUP + '/plans/' + first + '/VALIDATED.json').is_file())
        self.assertTrue(self.host.path(m.SETUP + '/plans/' + second + '/VALIDATED.json').is_file())

    def test_plan_inspection_includes_reviewed_identity_and_expiry_without_writes(self):
        pin = self.prepare()
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result = m.inspect(self.host, pin)
        self.assertEqual(result['identity'], self.host.identity())
        self.assertEqual(result['expiresAt'], result['createdAt'] + 86400)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_receiver_must_be_single_loopback_owned_by_customer(self):
        for rows in ([], [{'port': 8812, 'uid': 0, 'ipv4Loopback': True}],
                     [{'port': 8812, 'uid': 1000, 'ipv4Loopback': False}], self.host.socket_rows * 2):
            self.host.socket_rows = rows
            self.assertRefused('customer_loopback_receiver_required', m.prepare, self.host, self.host.expected_origin)

    def test_occupied_public_port_refused(self):
        self.host.socket_rows.append({'port': 443, 'uid': 0, 'ipv4Loopback': False})
        self.assertRefused('public_port_in_use', m.prepare, self.host, self.host.expected_origin)

    def test_installed_helper_hash_and_ownership_required(self):
        self.host.path(m.ENTRY).write_text('changed')
        self.assertRefused('installed_helper_changed', self.host.identity)

    def test_group_writable_or_linked_caddy_binary_refused(self):
        binary = self.host.path('/usr/bin/caddy')
        binary.chmod(0o775)
        self.assertRefused('root_owned_unlinked_path_required', self.host.identity)
        binary.unlink()
        binary.symlink_to(self.host.path(m.ENTRY))
        self.assertRefused('root_owned_unlinked_path_required', self.host.identity)

    def test_python_version_alias_is_bounded_to_root_owned_usr_bin(self):
        self.put('/usr/bin/python3.14', b'fixture-python', 0o755)
        python = self.host.path('/usr/bin/python3')
        python.symlink_to('python3.14')
        self.assertEqual(self.host.binary('/usr/bin/python3'), b'fixture-python')
        python.unlink()
        python.symlink_to('../../../tmp/python3.14')
        self.assertRefused('trusted_python_alias_required', self.host.binary, '/usr/bin/python3')

    def test_instance_policy_missing_or_other_machine_refused(self):
        path=self.host.path(m.INSTANCE_POLICY);path.unlink()
        with self.assertRaises((m.Refused,FileNotFoundError)):self.host.identity()
        self.policy['machineSha256']='f'*64
        self.put(m.INSTANCE_POLICY,m.canonical(self.policy))
        with self.assertRaisesRegex(m.Refused,'instance_authorization_differs'):self.host.identity()

    def test_wrong_machine_enrollment_refused(self):
        self.enrollment['machineSha256'] = 'b' * 64
        self.put('/etc/decrypt-os/customer-test.json', m.canonical(self.enrollment))
        self.assertRefused('customer_installation_required', self.host.identity)

    def test_prepare_caddy_schema_failure_never_installs(self):
        self.host.validation_failure = True
        self.assertRefused('caddy_configuration_invalid', m.prepare, self.host, self.host.expected_origin)
        self.assertFalse(self.host.path(m.UNIT).exists())
        self.assertFalse(self.host.path(m.SETUP + '/ATTEMPT.json').exists())

    def test_apply_explicit_acknowledgments(self):
        pin = self.prepare()
        self.assertRefused('explicit_domain_and_public_service_acknowledgments_required', m.apply, self.host, pin)
        self.assertFalse(self.host.path(m.UNIT).exists())

    def test_changed_origin_after_prepare_refused(self):
        pin = self.prepare()
        self.host.expected_origin = 'https://changed.customer.example.com'
        self.assertRefused('reviewed_installation_changed', self.apply, pin)

    def test_changed_dependency_after_prepare_refused(self):
        pin = self.prepare()
        self.host.path('/usr/bin/caddy').write_bytes(b'new binary')
        self.assertRefused('reviewed_installation_changed', self.apply, pin)

    def test_modified_plan_and_config_refused(self):
        pin = self.prepare()
        folder = self.host.path(m.SETUP + '/plans/' + pin)
        (folder / 'caddy.json').write_text('{}')
        self.assertRefused('prepared_files_changed', self.apply, pin)
        (folder / 'caddy.json').write_bytes(m.caddy_config(self.host.expected_origin))
        (folder / 'PLAN.json').write_text('{}')
        self.assertRefused('reviewed_plan_changed', self.apply, pin)

    def test_expired_plan_cannot_apply(self):
        pin = self.prepare()
        original = m.time.time
        try:
            m.time.time = lambda: original() + 86401
            self.assertRefused('prepare_a_fresh_plan', self.apply, pin)
        finally:
            m.time.time = original

    def test_existing_caddy_service_preserved(self):
        pin = self.prepare()
        self.host.other_proxy = True
        self.assertRefused('existing_proxy_service_preserved', self.apply, pin)
        self.assertFalse(self.host.path(m.SETUP + '/ATTEMPT.json').exists())

    def test_existing_certificate_state_is_not_adopted_by_prepare(self):
        for name in m.RETAINED_STATE:
            with self.subTest(name=name):
                self.put(name + '/retained.txt', b'preserve-customer-certificate-state', 0o600)
                self.assertRefused('existing_certificate_state_preserved', self.prepare)
                self.assertEqual(m.inspect(self.host)['state'], 'partial_requires_review')
                self.assertEqual(self.host.path(name + '/retained.txt').read_bytes(), b'preserve-customer-certificate-state')
                self.host.path(name + '/retained.txt').unlink()
                self.host.path(name).rmdir()

    def test_certificate_state_appearing_after_plan_prevents_apply(self):
        pin = self.prepare()
        self.put(m.RETAINED_STATE[0] + '/retained.txt', b'preserved')
        self.assertRefused('existing_or_partial_ingress_preserved', self.apply, pin)
        self.assertFalse(self.host.path(m.SETUP + '/ATTEMPT.json').exists())

    def test_operation_lock_prevents_parallel_apply_without_mutation(self):
        pin = self.prepare()
        with m.locked(self.host):
            with self.assertRaises(BlockingIOError):
                self.apply(pin)
        self.assertFalse(self.host.path(m.UNIT).exists())
        self.assertFalse(self.host.path(m.SETUP + '/ATTEMPT.json').exists())

    def test_second_caddy_validation_failure_never_installs(self):
        pin = self.prepare()
        self.host.validation_failure = True
        self.assertRefused('caddy_configuration_invalid', self.apply, pin)
        self.assertFalse(self.host.path(m.UNIT).exists())
        self.assertFalse(self.host.path(m.SETUP + '/ATTEMPT.json').exists())

    def test_apply_installs_exact_files_and_enables_only_dedicated_unit(self):
        result = self.apply()
        self.assertEqual(self.host.path(m.CONFIG).read_bytes(), m.caddy_config(self.host.expected_origin))
        self.assertEqual(self.host.path(m.UNIT).read_bytes(), m.service_unit())
        self.assertTrue(result['serviceActive'])
        self.assertTrue(result['serviceEnabled'])
        self.assertFalse(result['publicTlsVerified'])
        self.assertFalse(result['publicReachabilityVerified'])
        self.assertFalse(result['nativeRebootVerified'])
        actions = [a for a, _ in self.host.commands if a[0] == '/usr/bin/systemctl' and a[1] in ('enable', 'restart', 'stop')]
        self.assertEqual(actions, [['/usr/bin/systemctl', 'enable', '--now', m.UNIT_NAME]])
        self.assertEqual(result['state'], 'installed')
        self.assertTrue(result['serviceStartRecorded'])

    def test_private_inherited_umask_preserves_customer_service_readability(self):
        previous = os.umask(0o077)
        try:
            self.apply()
        finally:
            os.umask(previous)
        self.assertEqual(self.host.path(m.CONFIG).stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.host.path(m.UNIT).stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.host.path(m.CONFIG).parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.host.path(m.SETUP).stat().st_mode & 0o777, 0o700)
        for path in self.host.path(m.SETUP).rglob('*.json'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_started_unit_does_not_make_native_tls_claim(self):
        self.apply()
        result = m.strict(self.host.path(m.SETUP + '/SERVICE-STARTED.json').read_bytes())
        self.assertFalse(result['publicTlsVerified'])
        self.assertFalse(result['publicReachabilityVerified'])
        self.assertFalse(result['nativeRebootVerified'])

    def test_service_failure_preserved_no_automatic_retry_or_cleanup(self):
        pin = self.prepare()
        self.host.start_failure = True
        self.assertRefused('service_action_failed_partial_state_preserved', self.apply, pin)
        self.assertTrue(self.host.path(m.UNIT).exists())
        self.assertTrue(self.host.path(m.SETUP + '/ATTEMPT.json').exists())
        self.assertFalse(self.host.path(m.SETUP + '/SERVICE-STARTED.json').exists())
        self.assertRefused('existing_or_partial_ingress_preserved', self.apply, pin)
        self.assertFalse(m.inspect(self.host)['serviceActive'])
        self.assertEqual(m.inspect(self.host)['state'], 'installed_service_start_unconfirmed')
        self.assertFalse(m.inspect(self.host)['serviceStartRecorded'])

    def test_apparent_active_service_without_completion_receipt_remains_unconfirmed(self):
        self.apply()
        self.host.path(m.SETUP + '/SERVICE-STARTED.json').unlink()
        result = m.inspect(self.host)
        self.assertTrue(result['serviceActive'])
        self.assertEqual(result['state'], 'installed_service_start_unconfirmed')
        self.assertFalse(result['serviceStartRecorded'])

    def test_changed_completion_receipt_is_not_reported_as_success(self):
        self.apply()
        path = self.host.path(m.SETUP + '/SERVICE-STARTED.json')
        value = m.strict(path.read_bytes())
        value['planSha256'] = 'b' * 64
        path.write_bytes(m.canonical(value))
        self.assertRefused('service_start_receipt_changed', m.inspect, self.host)

    def test_existing_config_without_our_receipts_requires_inspection(self):
        self.put(m.CONFIG, b'customer-existing-config')
        self.assertEqual(m.inspect(self.host)['state'], 'partial_requires_review')
        self.assertEqual(self.host.path(m.CONFIG).read_bytes(), b'customer-existing-config')

    def test_existing_destination_is_never_overwritten(self):
        pin = self.prepare()
        self.put(m.UNIT, b'customer existing unit')
        self.assertRefused('existing_or_partial_ingress_preserved', self.apply, pin)
        self.assertEqual(self.host.path(m.UNIT).read_bytes(), b'customer existing unit')

    def test_restart_verification_rejects_changed_origin(self):
        self.apply()
        self.host.expected_origin = 'https://different.customer.example.com'
        self.assertRefused('installed_origin_or_dependency_changed', m.installed, self.host)

    def test_restart_verification_does_not_require_desktop_listener_at_boot(self):
        self.apply()
        self.host.socket_rows = []
        self.assertEqual(m.installed(self.host)['publicOrigin'], self.host.expected_origin)
        self.assertRefused('customer_loopback_receiver_required', m.installed, self.host, require_receiver=True)

    def test_restart_verification_is_not_subject_to_prepare_expiry(self):
        self.apply()
        original = m.time.time
        try:
            m.time.time = lambda: original() + 864000
            self.assertEqual(m.installed(self.host)['publicOrigin'], self.host.expected_origin)
        finally:
            m.time.time = original

    def test_installed_config_drift_refused(self):
        self.apply()
        self.host.path(m.CONFIG).write_text('{}')
        self.assertRefused('installed_proxy_files_changed', m.installed, self.host)

    def test_service_override_refused_on_restart(self):
        self.apply()
        self.host.dropins = True
        self.assertRefused('installed_service_override_refused', m.installed, self.host)

    def test_preexisting_dropin_without_unit_preserved(self):
        pin = self.prepare()
        self.host.path('/etc/systemd/system/' + m.UNIT_NAME + '.d').mkdir()
        self.assertRefused('existing_service_overrides_preserved', self.apply, pin)
        self.assertFalse(self.host.path(m.SETUP + '/ATTEMPT.json').exists())

    def test_plan_inspection_and_absent_install_are_honest(self):
        self.assertEqual(m.inspect(self.host)['state'], 'not_installed')
        pin = self.prepare()
        self.assertEqual(m.inspect(self.host, pin)['state'], 'prepared')
        self.host.write(self.host.path(m.SETUP + '/ATTEMPT.json'), b'{}')
        self.assertEqual(m.inspect(self.host)['state'], 'partial_requires_review')

    def test_no_secret_values_or_origin_path_accepted(self):
        secret = 'never-log-this-fixture-secret'
        self.assertRefused('public_fqdn_required', m.prepare, self.host, self.host.expected_origin + '/signal/' + secret)
        self.assertFalse(self.host.path(m.SETUP).exists())
        self.assertNotIn(secret, json.dumps(self.host.commands))

    def test_duplicate_json_refused(self):
        self.assertRefused('duplicate_json_field', m.strict, b'{"a":1,"a":2}')


class OriginReadTests(unittest.TestCase):
    def test_real_readonly_origin_query_no_other_values(self):
        # Use a real owned directory with no /var -> /private/var alias on macOS.
        with tempfile.TemporaryDirectory(prefix='origin-query-', dir=Path(__file__).resolve().parent) as tmp:
            path = Path(tmp) / 'service.sqlite'
            with contextlib.closing(sqlite3.connect(path)) as db:
                with db:
                    db.execute('CREATE TABLE configuration(name TEXT PRIMARY KEY,value TEXT NOT NULL)')
                    db.execute('INSERT INTO configuration VALUES (?,?)', ('ingress', '{"publicOrigin":"https://alerts.customer.example.com"}'))
                    db.execute('INSERT INTO configuration VALUES (?,?)', ('private-not-selected', 'fixture-secret-never-output'))
            path.chmod(0o600)
            result = subprocess.run([sys.executable, '-I', '-B', '-c', m.READ_ORIGIN, str(path)],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(json.loads(result.stdout), {'publicOrigin': 'https://alerts.customer.example.com'})
            self.assertNotIn(b'fixture-secret', result.stdout + result.stderr)
            with contextlib.closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM configuration').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
