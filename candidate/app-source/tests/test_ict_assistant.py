"""Packaged ICT assistant: agent fragment, knowledge pack, clock, launcher, packaging.

Pure unit tests. No OpenClaw process, network, sudo or terminal is started.
"""
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
APP = SOURCE / 'app'
PACK = APP / 'ict-assistant'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(PACK))
clock = load('ict_clock', PACK / 'ict_clock.py')
launcher = load('ict_launcher', PACK / 'launcher.py')
bootstrap = load('ict_bootstrap', APP / 'harness/bootstrap.py')
reader_proxy = load('ict_web_reader_proxy', PACK / 'web_reader_proxy.py')


def utc(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


class AgentPackTests(unittest.TestCase):
    def test_fragment_declares_only_the_ict_agent_with_narrow_tools(self):
        fragment = json.loads((PACK / 'openclaw-agent.json').read_text())
        self.assertEqual(set(fragment), {'agents'})
        self.assertEqual(set(fragment['agents']['entries']), {'ict'})
        entry = fragment['agents']['entries']['ict']
        self.assertEqual(entry['tools'], {'profile': 'minimal', 'alsoAllow': ['web_fetch']})
        self.assertEqual(entry['skills'], list(bootstrap.APPROVED_SKILLS))
        self.assertNotIn('workspace', entry)  # bootstrap sets the runtime path
        self.assertNotIn('model', entry)  # inherits Claude defaults and fallback

    def test_bootstrap_installs_composed_workspace(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / 'workspace-ict').mkdir()
            old = bootstrap.ROOT, bootstrap.ICT_SOURCE
            bootstrap.ROOT, bootstrap.ICT_SOURCE = root, PACK
            try:
                entry = bootstrap.install_ict_workspace(APP / 'customer-workspace')
                config = bootstrap.config(entry)
            finally:
                bootstrap.ROOT, bootstrap.ICT_SOURCE = old
            agents = (root / 'workspace-ict/AGENTS.md').read_text()
            self.assertEqual(config['agents']['entries']['ict']['workspace'], str(root / 'workspace-ict'))
            for item in sorted((PACK / 'knowledge').glob('*.md')):
                self.assertIn(item.read_text().strip()[:200], agents)
            self.assertLessEqual(len(agents), entry['bootstrapMaxChars'])
            total = sum(len((root / 'workspace-ict' / n).read_text()) for n in ('AGENTS.md', 'SOUL.md', 'IDENTITY.md'))
            self.assertLessEqual(total, entry['bootstrapTotalMaxChars'])
            for skill in bootstrap.APPROVED_SKILLS:
                self.assertTrue((root / 'workspace-ict/skills' / skill / 'SKILL.md').is_file())

    def test_persona_rules(self):
        soul = (PACK / 'workspace/SOUL.md').read_text()
        agents = (PACK / 'workspace/AGENTS.md').read_text()
        for phrase in ('financial advice', 'cannot place', 'Never ask for passwords'):
            self.assertIn(phrase, soul)
        self.assertIn('session_status', agents)
        self.assertIn('web_fetch', agents)
        self.assertIn('Never use a fixed UTC offset', agents)

    def test_no_private_or_owner_material(self):
        # Generic patterns only; DECRYPT_PRIVATE_PATTERNS (a file outside the repo, one regex per line,
        # '#' comments allowed) adds private terms, the same file tools/privacy_check.py reads.
        patterns = [re.compile(r'/Users/|/home/|\b\d{1,3}(\.\d{1,3}){3}\b|[\w.+-]+@[\w-]+\.[a-z]{2,}|tradovate',
                               re.IGNORECASE)]
        extra = os.environ.get('DECRYPT_PRIVATE_PATTERNS')
        if extra and Path(extra).expanduser().is_file():
            for line in Path(extra).expanduser().read_text().splitlines():
                if line.strip() and not line.strip().startswith('#'):
                    try:
                        patterns.append(re.compile(line.strip()))
                    except re.error:
                        pass   # same rule as tools/privacy_check.py: an invalid line is skipped
        for path in PACK.rglob('*'):
            if path.is_file() and path.suffix in ('.md', '.json', '.py', '.desktop'):
                text = path.read_text()
                if path.name == 'launcher.py':
                    text = text.replace('/usr/local/lib/decrypt-os/installer/customer_control.py', '')
                text = text.replace('127.0.0.1', '')  # the proxy's own loopback bind is not private data
                for forbidden in patterns:
                    self.assertIsNone(forbidden.search(text), path)

    def test_links_are_https_and_official_playlists_well_formed(self):
        text = (PACK / 'knowledge/08-sources.md').read_text()
        urls = re.findall(r'https?://\S+', text)
        self.assertGreater(len(urls), 15)
        for url in urls:
            self.assertTrue(url.startswith('https://'), url)
        for playlist in re.findall(r'list=([\w-]+)', text):
            self.assertRegex(playlist, r'^PLVgHx4Z63pa[\w-]{22}$')  # official channel prefix
        self.assertIn('https://www.youtube.com/@InnerCircleTrader', text)
        self.assertIn('https://theinnercircletrader.com', text)


class ClockTests(unittest.TestCase):
    def test_knowledge_table_matches_clock(self):
        table = (PACK / 'knowledge/04-time-and-sessions.md').read_text()
        for name, kind, start, end in clock.WINDOWS:
            self.assertIn(f'| {name} | {start}-{clock.label(end)} |', table)

    def test_summer_and_winter_use_new_york_time(self):
        summer = clock.snapshot(utc('2021-10-11T14:05:00'))  # Mon 10:05 EDT
        winter = clock.snapshot(utc('2021-12-13T15:05:00'))  # Mon 10:05 EST
        for snap in (summer, winter):
            names = [w['name'] for w in snap['active']]
            self.assertEqual(names[0], 'AM Silver Bullet')
            self.assertIn('New York AM killzone (indices)', names)
            self.assertTrue(snap['futuresOpen'])
        self.assertIn('EDT', summer['newYorkTime'])
        self.assertIn('EST', winter['newYorkTime'])

    def test_weekend_and_daily_pause(self):
        self.assertFalse(clock.snapshot(utc('2021-10-09T15:00:00'))['futuresOpen'])  # Saturday
        self.assertFalse(clock.snapshot(utc('2021-10-08T21:30:00'))['futuresOpen'])  # Friday 17:30 NY
        self.assertFalse(clock.snapshot(utc('2021-10-11T21:30:00'))['futuresOpen'])  # Monday 17:30 pause
        sunday = clock.snapshot(utc('2021-10-10T22:30:00'))  # Sunday 18:30 NY
        self.assertTrue(sunday['futuresOpen'])
        self.assertEqual(sunday['next']['name'], 'Asian killzone')
        self.assertEqual(sunday['next']['minutesUntil'], 90)
        closed = clock.snapshot(utc('2021-10-09T15:00:00'))
        self.assertEqual((closed['active'], closed['next']['name']), ([], 'Asian killzone'))

    def test_waybar_output(self):
        value = clock.waybar(clock.snapshot(utc('2021-10-11T14:05:00')))
        self.assertEqual(set(value), {'text', 'tooltip', 'class'})
        self.assertEqual(value['class'], 'silver_bullet')
        self.assertEqual(clock.waybar(clock.snapshot(utc('2021-10-09T15:00:00')))['class'], 'closed')
        with self.assertRaises(ValueError):
            clock.snapshot(datetime(2026, 10, 12, 10, 5))


class WebReaderProxyTests(unittest.TestCase):
    """Allow-listed loopback proxy for the ict agent's web_fetch. OpenClaw's
    own SSRF policy has no "allow only these public hosts" mode (only private-
    network exceptions or pattern denylists), so this proxy supplies it.
    Network-touching paths (resolve_public) are mocked; nothing here needs a
    real connection to youtube.com or theinnercircletrader.com."""

    def test_allowed_hosts_are_exactly_the_four_ict_sources(self):
        self.assertEqual(reader_proxy.ALLOWED_HOSTS,
                          {'theinnercircletrader.com', 'youtube.com', 'www.youtube.com', 'youtu.be'})

    def test_is_allowed_host_matches_exactly_case_insensitively(self):
        for host in reader_proxy.ALLOWED_HOSTS:
            self.assertTrue(reader_proxy.is_allowed_host(host.upper()))
            self.assertTrue(reader_proxy.is_allowed_host(host + '.'))  # trailing-dot FQDN

    def test_is_allowed_host_refuses_subdomains_suffixes_and_lookalikes(self):
        for host in ('evil.youtube.com', 'youtube.com.evil.example', 'notyoutube.com',
                     'theinnercircletraderxyz.com', 'xtheinnercircletrader.com', '', None,
                     'youtu.be.evil.com'):
            self.assertFalse(reader_proxy.is_allowed_host(host))

    def test_is_allowed_host_refuses_ip_literals_even_if_resolved_from_an_allowed_name(self):
        for literal in ('93.184.216.34', '::1', '127.0.0.1'):
            self.assertFalse(reader_proxy.is_allowed_host(literal))

    def test_is_public_address_refuses_private_loopback_and_link_local(self):
        for address in ('127.0.0.1', '10.0.0.5', '192.168.1.1', '172.16.0.1',
                        '169.254.1.1', '::1', 'fe80::1', '0.0.0.0'):
            self.assertFalse(reader_proxy.is_public_address(address))

    def test_is_public_address_accepts_ordinary_public_addresses(self):
        for address in ('93.184.216.34', '8.8.8.8', '2001:4860:4860::8888'):
            self.assertTrue(reader_proxy.is_public_address(address))

    def _connect(self, server):
        client = socket.create_connection(server.server_address)
        self.addCleanup(client.close)
        return client

    def _run_server(self):
        server = reader_proxy.ReaderServer(('127.0.0.1', 0), reader_proxy.ReaderHandler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        return server

    def test_connect_refuses_a_disallowed_host_without_touching_the_network(self):
        server = self._run_server()
        with mock.patch.object(reader_proxy, 'resolve_public') as resolve:
            client = self._connect(server)
            client.sendall(b'CONNECT evil.example.com:443 HTTP/1.1\r\n\r\n')
            response = client.recv(4096)
        self.assertIn(b'403', response)
        resolve.assert_not_called()

    def test_connect_refuses_a_non_443_port_on_an_allowed_host(self):
        server = self._run_server()
        with mock.patch.object(reader_proxy, 'resolve_public') as resolve:
            client = self._connect(server)
            client.sendall(b'CONNECT youtube.com:8080 HTTP/1.1\r\n\r\n')
            response = client.recv(4096)
        self.assertIn(b'403', response)
        resolve.assert_not_called()

    def test_connect_tunnels_an_allowed_host_once_resolved(self):
        server = self._run_server()
        upstream, far_end = socket.socketpair()
        self.addCleanup(upstream.close)
        with mock.patch.object(reader_proxy, 'resolve_public', return_value=far_end):
            client = self._connect(server)
            client.sendall(b'CONNECT www.youtube.com:443 HTTP/1.1\r\n\r\n')
            response = client.recv(4096)
            self.assertIn(b'200', response)
            client.sendall(b'hello upstream')
            self.assertEqual(upstream.recv(32), b'hello upstream')
            upstream.sendall(b'hello client')
            self.assertEqual(client.recv(32), b'hello client')

    def test_plain_http_refuses_a_disallowed_host(self):
        server = self._run_server()
        with mock.patch.object(reader_proxy, 'resolve_public') as resolve:
            client = self._connect(server)
            client.sendall(b'GET http://evil.example.com/ HTTP/1.1\r\n\r\n')
            response = client.recv(4096)
        self.assertIn(b'403', response)
        resolve.assert_not_called()

    def test_plain_http_sends_one_rebuilt_request_and_nothing_pipelined(self):
        server = self._run_server()
        upstream, far_end = socket.socketpair()
        self.addCleanup(upstream.close)
        with mock.patch.object(reader_proxy, 'resolve_public', return_value=far_end) as resolve:
            client = self._connect(server)
            client.sendall(b'GET http://YouTube.com/watch?v=1 HTTP/1.1\r\nHost: 127.0.0.1:8810\r\n'
                           b'Proxy-Authorization: x\r\nConnection: keep-alive\r\nAccept: */*\r\n\r\n'
                           b'GET http://127.0.0.1:8810/api/session HTTP/1.1\r\nHost: 127.0.0.1:8810\r\n\r\n')
            upstream.settimeout(5)
            sent = b''
            while not sent.endswith(b'\r\n\r\n'):
                sent += upstream.recv(4096)
            upstream.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok')
            upstream.close()
            client.settimeout(5)
            reply = b''
            while chunk := client.recv(4096):
                reply += chunk
        resolve.assert_called_once_with('youtube.com', 80)
        self.assertEqual(sent, b'GET /watch?v=1 HTTP/1.1\r\nHost: youtube.com\r\nAccept: */*\r\nConnection: close\r\n\r\n')
        self.assertTrue(reply.endswith(b'ok'))

    def test_plain_http_refuses_chunked_bodies(self):
        server = self._run_server()
        with mock.patch.object(reader_proxy, 'resolve_public') as resolve:
            client = self._connect(server)
            client.sendall(b'POST http://youtube.com/ HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n')
            response = client.recv(4096)
        self.assertIn(b'400', response)
        resolve.assert_not_called()

    def test_proxy_binds_ipv4_loopback_and_is_required(self):
        server = reader_proxy.start(0)
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        self.assertEqual(server.server_address[0], '127.0.0.1')
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(bootstrap, 'ICT_SOURCE', Path(tmp)):
            with self.assertRaises(RuntimeError):
                bootstrap.start_web_reader_proxy()

    def test_malformed_request_line_refused_as_bad_request(self):
        server = self._run_server()
        client = self._connect(server)
        client.sendall(b'not a request line at all\r\n')
        response = client.recv(4096)
        self.assertIn(b'400', response)


class LauncherTests(unittest.TestCase):
    def test_next_step_mapping(self):
        self.assertEqual(launcher.next_step({'status': 'connected'}), 'ict')
        self.assertEqual(launcher.next_step({'status': 'ready'}), 'ict')
        self.assertEqual(launcher.next_step({'status': 'not_started'}), 'start')
        self.assertEqual(launcher.next_step({'status': 'not_started', 'storage': {'configured': True, 'unlocked': False}}), 'storage_unlock')
        self.assertEqual(launcher.next_step({'status': 'needs_login'}), 'login')
        self.assertEqual(launcher.next_step({'status': 'starting'}), 'wait')
        self.assertIsNone(launcher.next_step({'status': 'error'}))

    def test_only_fixed_actions(self):
        with self.assertRaises(ValueError):
            launcher.command('console')
        argv = launcher.command('ict')
        self.assertEqual(argv[-1], 'ict')
        self.assertTrue(argv[0].startswith('/usr/bin/'))

    def test_walks_to_login_then_opens_ict(self):
        states = iter([{'status': 'needs_login'}, {'status': 'ready'}])
        calls = []

        class Reply:
            def __init__(self, value):
                self.stdout, self.returncode = json.dumps(value), 0

        answers = iter(['', 'y'])
        result = launcher.main([], read=lambda prompt: next(answers),
                               run=lambda *a, **k: Reply(next(states)),
                               call=lambda argv: calls.append(argv[-1]) or 0)
        self.assertEqual(result, 0)
        self.assertEqual(calls, ['login', 'ict'])

    def test_connect_mode_walks_to_login_then_verifies_and_stops(self):
        states = iter([{'status': 'not_started'}, {'status': 'needs_login'}, {'status': 'ready'}])
        calls, runs = [], []

        class Reply:
            def __init__(self, value, code=0):
                self.stdout, self.returncode = json.dumps(value), code

        def run(argv, **kwargs):
            runs.append(argv[-1])
            if argv[-1] == 'status':
                return Reply(next(states))
            return Reply({'verified': True} if argv[-1] == 'verify' else {'status': 'starting'})
        answers = iter(['', 'y', 'y', 'y', ''])
        result = launcher.main(['--connect'], read=lambda prompt: next(answers), run=run,
                               call=lambda argv: calls.append(argv[-1]) or 0)
        self.assertEqual(result, 0)
        self.assertEqual(calls, ['login'])                 # the ICT terminal is never opened in connect mode
        self.assertEqual(runs, ['status', 'start', 'status', 'status', 'verify'])

    def test_verify_and_stop_modes_run_one_step_after_a_yes(self):
        for mode in ('--verify', '--stop'):
            runs = []
            answers = iter(['y', ''])
            result = launcher.main([mode], read=lambda prompt: next(answers),
                                   run=lambda argv, **k: runs.append(argv[-1]) or type('R', (), {'stdout': '{}', 'returncode': 0})())
            self.assertEqual((result, runs), (0, [mode[2:]]), mode)
            answers = iter(['n'])
            runs = []
            result = launcher.main([mode], read=lambda prompt: next(answers),
                                   run=lambda argv, **k: runs.append(argv[-1]) or None)
            self.assertEqual((result, runs), (0, []), mode)

    def test_declining_starts_nothing(self):
        calls = []
        answers = iter(['', 'n'])
        result = launcher.main([], read=lambda prompt: next(answers),
                               run=lambda *a, **k: type('R', (), {'stdout': '{"status": "not_started"}', 'returncode': 0})(),
                               call=lambda argv: calls.append(argv) or 0)
        self.assertEqual((result, calls), (0, []))


class PackagingTests(unittest.TestCase):
    def test_installer_and_bundle_ship_every_pack_file(self):
        clean = load('clean_base_ict', SOURCE / 'installer/clean_base.py')
        bundle = (SOURCE / 'lab/build_source_bundle.py').read_text()
        files = {p.relative_to(PACK).as_posix() for p in PACK.rglob('*')
                 if p.is_file() and '__pycache__' not in p.parts}
        self.assertEqual(set(clean.ICT_ASSISTANT), files)
        for name in files:
            self.assertIn('app/ict-assistant/' + name, clean.COPY_FILES)
            self.assertIn("'app/ict-assistant/" + name + "'", bundle)
        generated = clean.generated_files('customer')
        self.assertEqual(generated['usr/share/applications/decrypt-ict-assistant.desktop'][0],
                         (PACK / 'desktop/decrypt-ict-assistant.desktop').read_bytes())
        self.assertEqual(generated['usr/local/bin/decrypt-ict-assistant'][1], 0o755)
        self.assertIn(b'/app/ict-assistant/launcher.py', generated['usr/local/bin/decrypt-ict-assistant'][0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
