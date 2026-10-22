"""Multi-login instance selection for guard.py and trigger.py (registry in a temp dir)."""
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import guard
import trigger
from test_guard import APP, HEALTHY, FakeWs
from test_logins import SPEC


class RegistryCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / 'logins.json'
        self.env = {k: os.environ.get(k) for k in ('TVBRIDGE_LOGINS', 'TVBRIDGE_LOGIN_ID', 'TVBRIDGE_TOKEN',
                                                  'TVBRIDGE_TOKEN_B')}
        self.addCleanup(self.restore_env)
        for k in self.env:
            os.environ.pop(k, None)
        os.environ['TVBRIDGE_LOGINS'] = str(self.path)

    def restore_env(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def write(self, data=SPEC, extra=()):
        data = copy.deepcopy(data)
        for entry in extra:
            data['logins'].append(entry)
        self.path.write_text(json.dumps(data) if not isinstance(data, str) else data)


C = dict(SPEC['logins'][1], id='c', label='Login C', bridge_url='http://127.0.0.1:8780', token_env='TVBRIDGE_TOKEN_C',
         devtools_port=9558, browser_unit='tvbridge-browser@c.service', daemon_unit='tvbridge-daemon@c.service',
         guard_unit='tvbridge-login-guard@c.service', trigger_unit='tvbridge-login-trigger@c.service',
         config='~/.local/share/decrypt-os/tvbridge/logins/c/config.json',
         guard_state_dir='~/.local/share/decrypt-os/tvbridge-state/login-guard-c')


class GuardInstanceTest(RegistryCase):
    def test_no_registry_is_todays_constants(self):
        inst = guard.resolve_instance()
        self.assertEqual(inst, guard.Instance(None, 9555, guard.DEFAULT_STATE, 'tvbridge-browser.service',
                                              'tvbridge-daemon.service', 8777, 'TVBRIDGE_TOKEN', frozenset(), None))
        self.assertEqual(guard.RESERVED_PORTS | inst.reserved, frozenset({9556, 9566, 9567}))

    def test_default_instance_reserves_other_logins(self):
        self.write(extra=[C])
        inst = guard.resolve_instance()
        self.assertEqual((inst.login_id, inst.port, inst.state_dir), (None, 9555, guard.DEFAULT_STATE))
        self.assertEqual(inst.reserved, frozenset({9557, 9558}))
        self.assertNotIn(9555, inst.reserved)

    def test_default_instance_with_broken_registry_keeps_running_and_reserves(self):
        bad = copy.deepcopy(SPEC)
        bad['logins'][1]['devtools_port'] = 9600
        bad['logins'][1]['primary'] = True  # two primaries: invalid
        self.write(bad)
        inst = guard.resolve_instance()
        self.assertEqual((inst.port, inst.registry_error), (9555, 'login_registry_invalid'))
        self.assertEqual(inst.reserved, frozenset({9557, 9600}))
        self.write('{not json')
        self.assertEqual(guard.resolve_instance().reserved, frozenset({9557}))

    def test_missing_logins_module_keeps_login_a_running(self):
        real = guard.registry
        guard.registry = None
        try:
            self.assertEqual(guard.resolve_instance().reserved, frozenset())  # no registry file: today's
            self.write(extra=[C])
            inst = guard.resolve_instance()
            self.assertEqual((inst.port, inst.registry_error), (9555, 'login_registry_unavailable'))
            self.assertEqual(inst.reserved, frozenset({9557, 9558}))
            with self.assertRaises(guard.Hold) as ctx:
                guard.resolve_instance('b')
            self.assertEqual(ctx.exception.reason, 'login_registry_unavailable')
        finally:
            guard.registry = real
        real = trigger.registry
        trigger.registry = None
        try:
            self.assertEqual(trigger.resolve(), (8777, 'TVBRIDGE_TOKEN', 'tvbridge-login-guard.service'))
            with self.assertRaises(ValueError):
                trigger.resolve('b')
        finally:
            trigger.registry = real

    def test_instance_b_takes_everything_from_its_entry(self):
        self.write(extra=[C])
        inst = guard.resolve_instance('b')
        self.assertEqual(inst.login_id, 'b')
        self.assertEqual(inst.port, 9557)
        self.assertEqual(inst.state_dir, Path(os.path.expanduser('~/.local/share/decrypt-os/tvbridge-state/login-guard-b')))
        self.assertEqual((inst.browser_unit, inst.daemon_unit), ('tvbridge-browser@b.service',
                                                                 'tvbridge-daemon@b.service'))
        self.assertEqual((inst.health_port, inst.token_env), (8778, 'TVBRIDGE_TOKEN_B'))
        self.assertEqual(inst.reserved, frozenset({9555, 9558}))  # own 9557 not reserved

    def test_instance_a_from_registry_reserves_b(self):
        self.write()
        inst = guard.resolve_instance('a')
        self.assertEqual((inst.port, inst.daemon_unit, inst.health_port), (9555, 'tvbridge-daemon.service', 8777))
        self.assertEqual(inst.reserved, frozenset({9557}))

    def test_instance_errors_hold_instead_of_falling_back(self):
        for setup, login_id, reason in ((None, 'b', 'login_unknown'), (SPEC, 'z', 'login_unknown'),
                                        ('{bad', 'b', 'login_registry_invalid'), (SPEC, 'B!', 'login_id_invalid')):
            if setup is None:
                self.path.unlink(missing_ok=True)
            else:
                self.write(setup)
            with self.assertRaises(guard.Hold) as ctx:
                guard.resolve_instance(login_id)
            self.assertEqual(ctx.exception.reason, reason)

    def test_guard_refuses_another_logins_port(self):
        self.write()
        inst = guard.resolve_instance('b')
        for port in (9555, 9556):
            with self.assertRaises(guard.Hold) as ctx:
                guard.Guard(state_dir=self.dir / 's', port=port, reserved=inst.reserved)
            self.assertEqual(ctx.exception.reason, 'reserved_port')
        a = guard.resolve_instance()
        with self.assertRaises(guard.Hold):
            guard.Guard(state_dir=self.dir / 's', port=9557, reserved=a.reserved)
        guard.Guard(state_dir=self.dir / 's', port=9557, reserved=inst.reserved, health=lambda: None)

    def test_instance_guard_starts_its_own_daemon_and_records_login(self):
        self.write()
        inst = guard.resolve_instance('b')
        ws = FakeWs([APP])
        seen, started, owners = [], [], []
        g = guard.Guard(state_dir=self.dir / 'state-b', port=inst.port, targets=lambda p: seen.append(p) or [
            {'type': 'page', 'id': 'ABC', 'url': 'https://trader.tradovate.com/',
             'webSocketDebuggerUrl': 'ws://127.0.0.1:9557/devtools/page/ABC'}],
            connect=lambda u, p: ws, units=lambda u: seen.append(u) or ('failed', 'enabled'),
            starter=started.append, owner_check=owners.append, clock=lambda: 1_610_697_600.0,
            sleep=lambda s: None, reserved=inst.reserved, daemon_unit=inst.daemon_unit,
            login_id=inst.login_id, health=lambda: HEALTHY)
        status = g.run()
        self.assertEqual(status['state'], 'daemon_started')
        self.assertEqual(status['login_id'], 'b')
        self.assertEqual(status['port'], 9557)
        self.assertEqual(seen, [9557, 'tvbridge-daemon@b.service'])
        self.assertEqual(started, ['tvbridge-daemon@b.service'])
        self.assertEqual(owners, [9557])

    def test_default_status_has_no_login_id(self):
        ws = FakeWs([APP])
        g = guard.Guard(state_dir=self.dir / 'state-a', port=9555, targets=lambda p: [
            {'type': 'page', 'id': 'ABC', 'url': 'https://trader.tradovate.com/',
             'webSocketDebuggerUrl': 'ws://127.0.0.1:9555/devtools/page/ABC'}],
            connect=lambda u, p: ws, units=lambda u: ('active', 'enabled'), clock=lambda: 1_610_697_600.0,
            sleep=lambda s: None, health=lambda: HEALTHY)
        self.assertNotIn('login_id', g.run())

    def test_owner_check_uses_the_instance_browser_unit(self):
        calls = []
        real_run, real_pids = guard.subprocess.run, guard.listener_pids

        class Out:
            stdout = '4242\n'
        guard.subprocess.run = lambda argv, **kw: calls.append(argv) or Out()
        guard.listener_pids = lambda port: {4242}
        try:
            guard.check_owner(9557, 'tvbridge-browser@b.service')
            guard.check_owner(9555)
        finally:
            guard.subprocess.run, guard.listener_pids = real_run, real_pids
        self.assertEqual([c[3] for c in calls], ['tvbridge-browser@b.service', 'tvbridge-browser.service'])

    def test_health_reader_uses_instance_port_and_token_env(self):
        made = []
        real = guard.health_reader
        guard.health_reader = lambda token, port=8777: made.append((token, port)) or (lambda: None)
        try:
            os.environ['TVBRIDGE_TOKEN'] = 't' * 30
            os.environ['TVBRIDGE_TOKEN_B'] = 'b' * 30
            guard.Guard(state_dir=self.dir / 's', port=9557, token_env='TVBRIDGE_TOKEN_B', health_port=8778)
            del os.environ['TVBRIDGE_TOKEN_B']
            guard.Guard(state_dir=self.dir / 's', port=9557, token_env='TVBRIDGE_TOKEN_B', health_port=8778)
            guard.Guard(state_dir=self.dir / 's', port=9555)
        finally:
            guard.health_reader = real
        self.assertEqual(made, [('b' * 30, 8778), ('t' * 30, 8778), ('t' * 30, 8777)])

    def test_main_status_reads_the_instance_state_dir(self):
        data = copy.deepcopy(SPEC)
        data['logins'][1]['guard_state_dir'] = str(self.dir / 'state-b')
        self.write(data)
        (self.dir / 'state-b').mkdir()
        (self.dir / 'state-b' / 'status.json').write_text('{"state": "ok", "login_id": "b"}\n')
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(guard.main(['--status', '--login', 'b']), 0)
        self.assertEqual(json.loads(out.getvalue())['login_id'], 'b')
        os.environ['TVBRIDGE_LOGIN_ID'] = 'b'
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(guard.main(['--status']), 0)
        self.assertEqual(json.loads(out.getvalue())['login_id'], 'b')

    def test_main_unknown_login_exits_2(self):
        self.write()
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(guard.main(['--run', '--login', 'zz']), 2)
        self.assertEqual(json.loads(err.getvalue())['reason'], 'login_unknown')


class TriggerInstanceTest(RegistryCase):
    def test_default_is_login_a(self):
        self.assertEqual(trigger.resolve(), (8777, 'TVBRIDGE_TOKEN', 'tvbridge-login-guard.service'))
        self.write()
        self.assertEqual(trigger.resolve(), (8777, 'TVBRIDGE_TOKEN', 'tvbridge-login-guard.service'))

    def test_instance_b(self):
        self.write()
        self.assertEqual(trigger.resolve('b'), (8778, 'TVBRIDGE_TOKEN_B', 'tvbridge-login-guard@b.service'))
        self.assertEqual(trigger.resolve('a'), (8777, 'TVBRIDGE_TOKEN', 'tvbridge-login-guard.service'))

    def test_instance_errors(self):
        for setup, login_id in ((None, 'b'), (SPEC, 'z'), ('{bad', 'b'), (SPEC, 'B!')):
            if setup is None:
                self.path.unlink(missing_ok=True)
            else:
                self.write(setup)
            with self.assertRaises(ValueError):
                trigger.resolve(login_id)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(trigger.main(['--login', 'B!']), 2)

    def test_start_guard_starts_the_instance_unit(self):
        calls = []
        real = trigger.subprocess.run
        trigger.subprocess.run = lambda argv, **kw: calls.append(argv)
        try:
            trigger.start_guard('tvbridge-login-guard@b.service')
        finally:
            trigger.subprocess.run = real
        self.assertEqual(calls, [['systemctl', '--user', 'start', '--no-block', 'tvbridge-login-guard@b.service']])

    def test_main_without_token_exits_2_without_polling(self):
        self.write()
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(trigger.main(['--login', 'b']), 2)
        self.assertIn('TVBRIDGE_TOKEN_B missing', err.getvalue())


if __name__ == '__main__':
    unittest.main()
