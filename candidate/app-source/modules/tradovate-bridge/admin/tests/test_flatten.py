"""Flatten now through the bridge (order_adapter "bridge", tvbridge 1.1.3 /manual route). VM only.

Every call goes to FAKE loopback bridges that follow tvbridge/manual.py's contract (tokens, keys, body,
switch first with the admin note, duplicate change ids); nothing here talks to a live daemon.
"""
import json
import os
import re
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), 'app'))

import adapters  # noqa: E402
from engine import Conflict  # noqa: E402
from test_admin import Base  # noqa: E402
from tree import Profile, ProfileError  # noqa: E402

TOK = {'TVBRIDGE_READ_TOKEN_A': 'ra-' + 'x' * 30, 'TVBRIDGE_READ_TOKEN_B': 'rb-' + 'y' * 30,
       'TVBRIDGE_MANUAL_TOKEN_A': 'ma-' + 'm' * 30, 'TVBRIDGE_MANUAL_TOKEN_B': 'mb-' + 'n' * 30}
CHANGE_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$')   # tvbridge/manual.py
ERRORS = {'refused': ['logged_out', 'not_synced', 'unhealthy', 'busy', 'daemon_stopping', 'kill_switch_unavailable'],
          'incomplete': ['cancel_not_confirmed', 'liquidation_rejected', 'not_flat', 'orders_still_working'],
          'unknown': ['liquidation_unknown', 'in_progress']}


class FakeBridge:
    """One login's daemon: GET /health/<read token>, POST /manual/<manual token>/<key>/flatten."""

    def __init__(self, login, switches):
        self.login, self.switches = login, switches     # {key: kill switch path}
        self.synced, self.calls, self.journal, self.script = True, [], {}, {}
        self.read_tok = TOK[f'TVBRIDGE_READ_TOKEN_{login.upper()}']
        self.manual_tok = TOK[f'TVBRIDGE_MANUAL_TOKEN_{login.upper()}']
        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), self.handler())
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.srv.server_address[1]}'

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, obj):
                data = json.dumps(obj).encode()
                try:
                    self.send_response(code)
                except BrokenPipeError:
                    return
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except BrokenPipeError:   # the client already timed out (the timeout tests)
                    pass

            def do_GET(self):
                if self.path != f'/health/{fake.read_tok}':
                    return self.send(404, {'error': 'not found'})
                page = {'state': 'authorized', 'synced': True} if fake.synced else \
                    {'state': 'waiting_for_app_login', 'synced': False}
                self.send(200, {'health': {'ok': fake.synced, 'problems': [] if fake.synced else
                                           ['page_state:waiting_for_app_login'], 'page': page}})

            def do_POST(self):
                parts = self.path.split('/')
                body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                fake.calls.append({'path': '/'.join(parts[:2] + ['<tok>'] + parts[3:]), 'body': body.decode(),
                                   'token_ok': len(parts) > 2 and parts[2] == fake.manual_tok, 'at': time.time()})
                if len(parts) < 3 or parts[2] != fake.manual_tok:
                    return self.send(403, {'error': 'forbidden'})
                if len(parts) != 5 or parts[4] != 'flatten' or parts[3] not in fake.switches:
                    return self.send(404, {'error': 'unknown_account'})
                try:
                    b = json.loads(body)
                    assert set(b) == {'reason', 'change_id'} and b['reason'].strip()
                    assert CHANGE_ID_RE.match(b['change_id'])
                except Exception:  # noqa: BLE001
                    return self.send(400, {'error': 'invalid request'})
                key, cid = parts[3], b['change_id']
                if cid in fake.journal:
                    return self.send(200, dict(fake.journal[cid], duplicate=True))
                sc = fake.script.get(key) or {}
                if sc.get('status') in (503, 500):
                    return self.send(sc['status'], {'error': 'account_unavailable' if sc['status'] == 503
                                                    else 'internal_error', 'account': key})
                path = fake.switches[key]                        # switch first, with the admin note
                if not os.path.exists(path):
                    with open(path, 'w') as f:
                        json.dump({'set_by': 'tvbridge-admin', 'reason': b['reason'], 'change_id': cid,
                                   'at': time.time()}, f)
                    switch = 'set'
                else:
                    switch = 'already_set'
                err = sc.get('error')
                reply = {'ok': err is None, 'error': err, 'account': key, 'account_id': 1000 + len(key),
                         'net_position': 0 if err is None else 1, 'cancelled': ['501'], 'still_working': [],
                         'kill_switch': switch, 'entries_disabled': True,
                         'outcome': 'accepted' if err is None else 'unknown', 'command_id': 'manual-flatten:' + cid,
                         'receipt': {'source': 'operator', 'change_id': cid}}
                reply.update(sc.get('extra') or {})
                fake.journal[cid] = reply
                if sc.get('delay'):
                    time.sleep(sc['delay'])
                self.send(200, reply)
        return H


class FlattenBase(Base):
    def profile_extra(self):
        sb = self.tmp + '/tree/tvbridge'
        self.fakes = {
            'a': FakeBridge('a', {'main': sb + '/ENTRIES-DISABLED', 'account-b': sb + '/ENTRIES-DISABLED-account-b'}),
            'b': FakeBridge('b', {'account-d': sb + '/logins/b/ENTRIES-DISABLED-account-d',
                                  'account-e': sb + '/logins/b/ENTRIES-DISABLED-account-e'})}
        self._env = {k: os.environ.get(k) for k in TOK}
        os.environ.update(TOK)
        return {'order_adapter': 'bridge', 'order_bridge': {
            'timeout_s': 1.0, 'logins_override': {lg: {'bridge_url': f.url} for lg, f in self.fakes.items()}}}

    def tearDown(self):
        super().tearDown()
        for f in self.fakes.values():
            f.close()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def flatten(self, key, label, reason=None):
        params = {'account': key} if reason is None else {'account': key, 'reason': reason}
        ch = self.e.preview('flatten', params)
        return self.e.confirm(ch['id'], ch['preview_hash'], label)


class TestFlattenBridge(FlattenBase):
    def test_follower_ok_records_reply_and_pause(self):
        ch = self.e.preview('flatten', {'account': 'account-b', 'reason': 'leaving for the day'})
        self.assertEqual(ch['plan']['files'], [])          # the bridge sets the switch, not the page
        self.assertEqual(ch['plan']['flatten']['key'], 'account-b')
        ch = self.e.confirm(ch['id'], ch['preview_hash'], 'Account B')
        self.assertEqual(ch['status'], 'applied', ch['result'])
        fl = ch['result']['flatten']
        self.assertEqual((fl['outcome_class'], fl['http_status'], fl['reply']['ok']), ('flat', 200, True))
        call = self.fakes['a'].calls[-1]
        self.assertEqual(call['path'], '/manual/<tok>/account-b/flatten')
        self.assertEqual(json.loads(call['body']), {'reason': 'leaving for the day', 'change_id': ch['id']})
        self.assertTrue(CHANGE_ID_RE.match(ch['id']))
        self.assertEqual(fl['kill_switch_after']['set_by'], 'admin')
        acct = self.acct('account-b')
        self.assertEqual((acct['pause']['on'], acct['pause']['reason'], acct['status']['label']),
                         (True, 'leaving for the day', 'Paused'))
        entry = self.e.audit.read(1)[0]
        self.assertEqual(entry['flatten']['reply']['command_id'], 'manual-flatten:' + ch['id'])
        self.assertTrue(self.e.audit.verify_chain())
        self.assertNotIn(TOK['TVBRIDGE_MANUAL_TOKEN_A'], json.dumps(ch) + json.dumps(entry))   # never stored

    def test_login_b_follower_goes_to_login_b(self):
        ch = self.flatten('account-d', 'Account D')
        self.assertEqual(ch['status'], 'applied')
        self.assertEqual([c['path'] for c in self.fakes['b'].calls], ['/manual/<tok>/account-d/flatten'])
        self.assertEqual(self.fakes['a'].calls, [])

    def test_leader_uses_main_and_lists_also_blocks(self):
        self.fakes['a'].script['main'] = {'extra': {'also_blocks': ['account-b', 'account-c', 'account-h', 'account-d', 'account-e'],
                                                    'protective_stop_working': False}}
        pv = self.e.preview('flatten', {'account': 'account-a'})
        self.assertEqual(pv['plan']['flatten']['key'], 'main')
        self.assertEqual(pv['plan']['flatten']['also_blocks'], ['account-b', 'account-c', 'account-h', 'account-d', 'account-e'])
        warn = next(w for w in pv['plan']['warnings'] if w.startswith('This is a leader'))
        for label in ('Account B', 'Account C', 'Account H', 'Account D', 'Account E'):
            self.assertIn(label, warn)
        label = pv['plan']['confirm']['expect']
        ch = self.e.confirm(pv['id'], pv['preview_hash'], label)
        self.assertEqual(ch['status'], 'applied')
        self.assertEqual(self.fakes['a'].calls[-1]['path'], '/manual/<tok>/main/flatten')
        self.assertEqual(ch['result']['flatten']['reply']['also_blocks'], ['account-b', 'account-c', 'account-h', 'account-d', 'account-e'])

    def test_every_error_class(self):
        for klass, errors in ERRORS.items():
            for err in errors:
                with self.subTest(err=err):
                    self.fakes['a'].script['account-b'] = {'error': err}
                    ch = self.flatten('account-b', 'Account B')
                    fl = ch['result']['flatten']
                    self.assertEqual((ch['status'], fl['outcome_class'], fl['reply']['error']), ('failed', klass, err))
                    self.assertFalse(ch['result']['ok'])
                    want = {'refused': 'Nothing sent', 'incomplete': 'Not completed',
                            'unknown': 'Check the Bridge window'}[klass]
                    self.assertIn(want, ch['error'])
                    self.assertIn(want, ch['eta']['text'])
                    self.assertTrue(self.acct('account-b')['pause']['on'])   # the switch stays on

    def test_timeout_is_unknown_never_success_and_not_retried(self):
        self.fakes['a'].script['account-b'] = {'delay': 2.5}
        ch = self.flatten('account-b', 'Account B')
        fl = ch['result']['flatten']
        self.assertEqual((ch['status'], fl['outcome_class'], fl['http_status']), ('failed', 'unknown', None))
        self.assertIn('Unknown outcome', fl['detail'])
        self.assertIn('Check the Bridge window', fl['detail'])
        time.sleep(2)
        self.assertEqual(len(self.fakes['a'].calls), 1)

    def test_http_status_classes(self):
        cases = [(403, 'rejected_request', 'manual token'), (404, 'rejected_request', 'no Flatten now route'),
                 (503, 'rejected_request', 'unavailable'), (500, 'unknown', 'Unknown outcome'),
                 (400, 'rejected_request', 'request body')]
        for status, klass, words in cases:
            with self.subTest(status=status):
                f = self.fakes['b']
                f.script = {}
                if status == 403:
                    f.manual_tok = 'other-' + 'q' * 30
                elif status == 404:
                    f.manual_tok = TOK['TVBRIDGE_MANUAL_TOKEN_B']
                    f.switches.pop('account-e', None)
                elif status in (503, 500):
                    f.switches['account-e'] = self.tb('logins/b/ENTRIES-DISABLED-account-e')
                    f.script['account-e'] = {'status': status}
                if status == 400:
                    f.script = {}
                    r = adapters.BridgeOrders(self.profile).http_post(
                        f.url + '/manual/' + f.manual_tok + '/account-e/flatten', {'reason': ''}, 1.0)
                    self.assertEqual(adapters.classify_flatten(r[0], r[1], r[2])[0], klass)
                    continue
                ch = self.flatten('account-e', 'Account E')
                fl = ch['result']['flatten']
                self.assertEqual((ch['status'], fl['http_status'], fl['outcome_class']), ('failed', status, klass))
                self.assertIn(words, fl['detail'])

    def test_no_answer_is_unknown(self):
        self.e.orders.overrides = {'a': {'bridge_url': 'http://127.0.0.1:9'}, 'b': {'bridge_url': 'http://127.0.0.1:9'}}
        self.e.orders.readiness = lambda account: ([], [])     # pass the checks, then the call cannot connect
        ch = self.flatten('account-b', 'Account B')
        self.assertEqual(ch['result']['flatten']['outcome_class'], 'unknown')
        self.assertEqual(ch['status'], 'failed')

    def test_resend_after_unknown_reuses_change_id_and_gets_duplicate(self):
        self.fakes['a'].script['account-b'] = {'delay': 2.5}     # the bridge did it, the answer came too late
        ch = self.flatten('account-b', 'Account B')
        self.assertEqual(ch['result']['flatten']['outcome_class'], 'unknown')
        time.sleep(2)
        self.fakes['a'].script = {}
        with self.assertRaises(Conflict):
            self.e.resend_flatten(ch['id'], 'wrong')
        ch = self.e.resend_flatten(ch['id'], 'Account B')
        self.assertEqual(ch['status'], 'applied')
        fl = ch['result']['flatten']
        self.assertTrue(fl['reply']['duplicate'])
        self.assertIn('nothing sent again', fl['detail'])
        bodies = [json.loads(c['body'])['change_id'] for c in self.fakes['a'].calls]
        self.assertEqual(bodies, [ch['id'], ch['id']])
        self.assertEqual(ch['result']['flatten_attempts'], 2)
        with self.assertRaises(Conflict):     # only an unknown outcome can be sent again
            self.e.resend_flatten(ch['id'], 'Account B')

    def test_duplicate_in_progress_is_unknown(self):
        self.fakes['a'].script['account-b'] = {'error': 'in_progress', 'extra': {'duplicate': True}}
        ch = self.flatten('account-b', 'Account B')
        self.assertEqual(ch['result']['flatten']['outcome_class'], 'unknown')
        self.assertIn('in progress', ch['error'])

    def test_missing_manual_token_blocks_before_sending(self):
        os.environ.pop('TVBRIDGE_MANUAL_TOKEN_B')
        pv = self.e.preview('flatten', {'account': 'account-d'})
        self.assertTrue(any('TVBRIDGE_MANUAL_TOKEN_B' in b for b in pv['plan']['blockers']), pv['plan']['blockers'])
        self.assertEqual(pv['eta']['kind'], 'blocked')
        with self.assertRaises(Conflict) as cm:
            self.e.confirm(pv['id'], pv['preview_hash'], 'Account D')
        self.assertEqual(cm.exception.code, 'blocked')
        self.assertEqual(self.fakes['b'].calls, [])

    def test_logged_out_login_blocks_with_sign_in_message(self):
        self.fakes['b'].synced = False
        pv = self.e.preview('flatten', {'account': 'account-d'})
        self.assertTrue(any('Sign in to Tradovate first' in b for b in pv['plan']['blockers']), pv['plan']['blockers'])
        with self.assertRaises(Conflict):
            self.e.confirm(pv['id'], pv['preview_hash'], 'Account D')
        self.assertEqual(self.fakes['b'].calls, [])
        self.assertFalse(self.acct('account-d')['pause']['on'])

    def test_typed_confirmation_still_required(self):
        pv = self.e.preview('flatten', {'account': 'account-b'})
        self.assertTrue(pv['plan']['confirm']['required'])
        with self.assertRaises(Conflict) as cm:
            self.e.confirm(pv['id'], pv['preview_hash'], 'Account Z')
        self.assertEqual(cm.exception.code, 'confirm_text')
        self.assertEqual(self.fakes['a'].calls, [])

    def test_resend_over_http(self):
        import http.client
        import server
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.make_handler(self.e, 'tok', 0))
        port = httpd.server_address[1]
        httpd.RequestHandlerClass = server.make_handler(self.e, 'tok', port)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            ch = self.flatten('account-b', 'Account B')
            c = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            c.request('POST', f'/api/changes/{ch["id"]}/resend', json.dumps({'confirm_text': 'Account B'}),
                      {'Host': f'127.0.0.1:{port}', 'Origin': f'http://127.0.0.1:{port}', 'X-Admin-Token': 'tok',
                       'Content-Type': 'application/json'})
            r = c.getresponse()
            self.assertEqual((r.status, json.loads(r.read())['error']), (409, 'not_resendable'))
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestFlattenUnits(unittest.TestCase):
    def test_classify_table(self):
        c = adapters.classify_flatten
        self.assertEqual(c(200, {'ok': True, 'kill_switch': 'set'})[0], 'flat')
        self.assertEqual(c(200, {'ok': True, 'duplicate': True})[0], 'flat')
        for klass, errors in ERRORS.items():
            for err in errors:
                self.assertEqual(c(200, {'ok': False, 'error': err})[0], klass, err)
        self.assertEqual(c(200, {'ok': False, 'error': 'something_new'})[0], 'unknown')
        self.assertEqual(c(None, None, 'TimeoutError: timed out')[0], 'unknown')
        self.assertEqual(c(200, None)[0], 'unknown')
        for st in (400, 403, 404, 503):
            self.assertEqual(c(st, {'error': 'x'})[0], 'rejected_request')
        self.assertEqual(c(500, {'error': 'internal_error'})[0], 'unknown')
        self.assertEqual(c(502, None)[0], 'unknown')

    def test_key_mapping(self):
        self.assertEqual(adapters.flatten_key({'key': 'account-a', 'daemon_role': 'main'}), 'main')
        self.assertEqual(adapters.flatten_key({'key': 'account-d', 'daemon_role': 'follower'}), 'account-d')

    def test_bad_change_id_is_never_sent(self):
        class P:
            data, mode = {'order_bridge': {}}, 'live'

            def st(self, *p):
                return '/nonexistent'
        sent = []
        o = adapters.BridgeOrders(P(), http_post=lambda *a: sent.append(a))
        r = o.flatten({'key': 'x', 'login': 'a', 'daemon_role': 'follower'}, 'bad id!')
        self.assertEqual((r['ok'], r['outcome_class'], sent), (False, 'rejected_request', []))


class TestFlattenProfiles(unittest.TestCase):
    BASE = {'mode': 'sandbox', 'tvbridge_root': '/x/tb', 'soup_state': '/x/soup', 'admin_state': '/x/admin',
            'status_url': 'http://127.0.0.1:8779/api/status', 'port': 8781, 'user': 'u', 'public_host': 'h',
            'restarter': 'simulated', 'order_adapter': 'bridge', 'strategies': []}

    def test_sandbox_bridge_needs_fake_bridges(self):
        Profile(dict(self.BASE, order_bridge={'logins_override': {}}))
        with self.assertRaises(ProfileError):
            Profile(self.BASE)
        with self.assertRaises(ProfileError):
            Profile(dict(self.BASE, order_adapter='tradovate'))

    def test_live_bridge_refuses_overrides(self):
        live = dict(self.BASE, mode='live', live_switches_only=True)
        Profile(live)
        with self.assertRaises(ProfileError):
            Profile(dict(live, order_bridge={'logins_override': {'a': {'bridge_url': 'http://127.0.0.1:1'}}}))
        with self.assertRaises(ProfileError):
            Profile(dict(live, order_bridge={'timeout_s': 0}))


if __name__ == '__main__':
    unittest.main()
