"""Lifecycle faults in an isolated headless Chrome against the loopback fake only."""
import json
import threading
import time
import unittest

from harness import Harness
from fake_tradovate import TOKEN
from tvbridge import cdp
from tvbridge.bridge import Bridge
from tvbridge.state import AccountState


def until(fn, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(.04)
    raise AssertionError('condition not reached')


class Reliability(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.state = AccountState()
        self.bridge = Bridge(self.state, devtools_port=self.h.devtools_port,
                             page_url_contains='/trader/', trade_url_pattern=self.h.config()['trade_url_pattern'],
                             read_only=True, lifecycle={'tickMs': 50, 'idleMs': 2500, 'connectMs': 1600,
                                                        'retryMs': 150, 'maxRetryMs': 600,
                                                        'syncRetryMs': 100, 'syncTimeoutMs': 1500})
        self.inspect = cdp.Session(cdp.find_page(self.h.devtools_port, '/trader/'))
        self.inspect.evaluate('window.originals = [WebSocket, fetch, XMLHttpRequest.prototype.open, appSocket.send]')

    def tearDown(self):
        self.bridge.detach()
        self.inspect.close()
        self.h.close()

    def start(self, suffix='', wait=True):
        self.bridge.attach(reload=False)
        self.inspect.evaluate('appSocket.close(); window.appSocket = new WebSocket(%s); appSocket.onmessage = e => { if(e.data==="o") appSocket.send(%s); }' %
                              (json.dumps('ws://127.0.0.1:%d/v1/websocket%s' % (self.h.fake.port, suffix)),
                               json.dumps('authorize\n0\n\n' + TOKEN)))
        return self.bridge.wait_ready(10) if wait else None

    def authorize(self, token):
        self.inspect.evaluate('appSocket.send(%s)' % json.dumps('authorize\n0\n\n' + token))

    def ready(self):
        s = self.bridge.status() or {}
        return s.get('state') == 'authorized' and self.state.is_synced_for(s.get('epoch'))

    def test_read_only_blocks_python_and_direct_page_mutations(self):
        self.start()
        before = len(self.h.fake.requests)
        for endpoint in ('order/placeorder', 'order/cancelorder', 'order/modifyorder', 'position/liquidateposition',
                         'account/list\n99\n\norder/placeorder'):
            self.assertEqual(self.bridge.request(endpoint)['code'], 'read_only_endpoint')
            result = self.inspect.evaluate('window[%s].request(%s, "", {}, 200)' %
                                           (json.dumps(self.bridge.api), json.dumps(endpoint)))
            self.assertFalse(result['sent'])
        self.assertEqual(len(self.h.fake.requests), before)
        self.assertTrue(self.bridge.request('contract/find', query='name=MNQZ1')['ok'])
        self.assertEqual(self.h.fake.order_count(), 0)

    def test_app_cache_buster_is_accepted_and_dropped(self):
        # Tradovate's web trader opens wss://demo.tradovateapi.com/v1/websocket?r=<Math.random()>.
        st = self.start('?r=0.51234567890123456')
        self.assertEqual(st['url'], 'ws://127.0.0.1:%d/v1/websocket' % self.h.fake.port)

    def test_other_query_strings_are_refused(self):
        for suffix in ('?token=abcdefabcdefabcdef', '?r=abc', '?r=0.5&x=1', '?r=0.5&r=0.6'):
            with self.subTest(suffix=suffix):
                self.tearDown()
                self.setUp()
                self.start(suffix, wait=False)
                time.sleep(.5)
                st = self.bridge.status()
                self.assertEqual(st['state'], 'waiting_for_app_login')
                self.assertFalse(st['hasToken'])

    def resync_setup(self):
        st = self.start()
        return st, self.h.fake.requests.count('user/syncrequest')

    def assert_resynced_same_connection(self, st, syncs):
        until(self.ready, timeout=15)
        now = self.bridge.status()
        self.assertEqual(now['epoch'], st['epoch'])
        self.assertEqual(now['connections'], st['connections'])
        self.assertEqual(self.h.fake.requests.count('user/syncrequest'), syncs + 1)
        self.assertEqual(self.bridge.resyncs, 1)

    def test_transient_devtools_error_resyncs_on_same_connection(self):
        # A single CDP stall must not leave the bridge unsynced for the rest of the connection.
        st, syncs = self.resync_setup()
        real = self.bridge.session.evaluate
        calls = {'n': 0}
        def flaky(expr, timeout=10):
            calls['n'] += 1
            if calls['n'] == 1:
                raise TimeoutError('simulated stall')
            return real(expr, timeout=timeout)
        self.bridge.session.evaluate = flaky
        self.assertEqual(self.bridge.status()['state'], 'devtools_error')
        self.assertFalse(self.state.synced)
        self.assert_resynced_same_connection(st, syncs)

    def test_rejected_update_resyncs_on_same_connection(self):
        st, syncs = self.resync_setup()
        self.h.fake.delay_next['user/syncrequest'] = .8
        # Missing tradeDate fails validation: untrusted, not flat.
        self.h.fake._props('position', 'Updated', {'id': 999, 'accountId': 101, 'contractId': 555, 'netPos': 1})
        until(lambda: not self.state.synced)
        # Page itself drops to unsynced while the fresh snapshot is in flight (writes gated there).
        until(lambda: self.bridge.status()['synced'] is False, timeout=10)
        self.assert_resynced_same_connection(st, syncs)

    def test_resync_refused_for_stale_epoch_or_unsynced_page(self):
        st = self.start()
        api = json.dumps(self.bridge.api)
        self.assertFalse(self.inspect.evaluate('window[%s].resync("other:1")' % api))
        self.assertTrue(self.inspect.evaluate('window[%s].resync(%s)' % (api, json.dumps(st['epoch']))))
        self.assertFalse(self.inspect.evaluate('window[%s].resync(%s)' % (api, json.dumps(st['epoch']))))
        until(self.ready)

    def test_iframes_do_not_run_the_page_script(self):
        # Pages with iframes: iframe copies of the script would expire their lease after 30 s and
        # their 'controller_lease_expired' event would make the bridge drop a healthy connection.
        self.bridge = Bridge(self.state, devtools_port=self.h.devtools_port, page_url_contains='/trader/',
                             trade_url_pattern=self.h.config()['trade_url_pattern'], read_only=True,
                             lifecycle={'leaseMs': 1500, 'tickMs': 50, 'syncTimeoutMs': 1500})
        st = self.start()
        self.inspect.evaluate("for (let i = 0; i < 2; i++) { const f = document.createElement('iframe'); "
                              "f.srcdoc = '<p>helper</p>'; document.body.appendChild(f); }")
        time.sleep(3.5)
        self.assertEqual([e for e in self.bridge.events if e.get('type') == 'controller_lease_expired'], [])
        self.assertTrue(self.ready())
        self.assertEqual(self.bridge.status()['epoch'], st['epoch'])
        self.assertEqual(self.bridge.resyncs, 0)

    def test_reconnect_invalidates_and_uses_new_snapshot(self):
        old = self.start()['epoch']
        self.h.fake.delay_next['user/syncrequest'] = .7
        self.h.fake.disconnect_all()
        until(lambda: not self.state.synced)
        self.assertFalse(self.ready())
        until(self.ready)
        self.assertNotEqual(old, self.bridge.status()['epoch'])
        self.assertEqual(self.state.sync_epoch, self.bridge.status()['epoch'])

    def test_auth_failure_waits_for_a_new_token_without_retry_flood(self):
        self.start()
        self.h.fake.token = 'rotated-fake-token-111111111111'
        self.h.fake.disconnect_all()
        until(lambda: self.bridge.status()['state'] == 'auth_failed')
        count = self.h.fake.authorize_count
        time.sleep(.9)
        self.assertEqual(self.h.fake.authorize_count, count)
        # Restore an app socket only inside the fake page; bridge sees its authorize frame.
        self.inspect.evaluate('window.appSocket = new WebSocket(%s); appSocket.onmessage = e => { if(e.data==="o") appSocket.send(%s); }' %
                              (json.dumps('ws://127.0.0.1:%d/v1/websocket' % self.h.fake.port),
                               json.dumps('authorize\n0\n\n' + self.h.fake.token)))
        until(self.ready)
        self.assertEqual(self.bridge.status()['tokenGeneration'], 2)

    def test_receive_watchdog_recovers_silent_open_socket(self):
        old = self.start()['epoch']
        self.h.fake.silent = True
        until(lambda: not self.state.synced, timeout=5)
        self.assertTrue(any(e.get('data', {}).get('reason') == 'receive_timeout' for e in self.bridge.events))
        self.h.fake.silent = False
        until(self.ready)
        self.assertNotEqual(old, self.bridge.status()['epoch'])

    def test_stale_sync_and_props_cannot_mutate_new_epoch(self):
        epoch = self.start()['epoch']
        def event(kind, data, old='obsolete:1'):
            self.bridge._on_event('Runtime.bindingCalled', {'name': self.bridge.binding,
                'payload': json.dumps({'type': kind, 'data': data, 'epoch': old})})
        event('props', {'entityType': 'position', 'eventType': 'Updated',
                       'entity': {'id': 999, 'accountId': 101, 'contractId': 555, 'netPos': 999}})
        event('sync', {'accounts': [], 'snapshot': {}, 'syncEpoch': 'obsolete:1'})
        self.assertEqual(self.state.net_position(101, 555), 0)
        self.assertEqual(self.state.sync_epoch, epoch)
        event('sync', {'accounts': [], 'snapshot': 'bad', 'syncEpoch': epoch}, epoch)
        self.assertFalse(self.state.synced)

    def test_same_frame_snapshot_and_later_props_keep_later_state(self):
        self.h.fake.sync_props_once = {'entityType': 'position', 'eventType': 'Updated',
            'entity': {'id': 999, 'accountId': 101, 'contractId': 555, 'netPos': 3,
                       'tradeDate': {'year': 2021, 'month': 9, 'day': 24}}}
        self.start()
        until(lambda: self.state.net_position(101, 555) == 3)

    def test_old_sync_request_does_not_continue_into_new_connection(self):
        self.start()
        self.h.fake.delay_next['account/list'] = 1
        self.h.fake.disconnect_all()
        until(lambda: not self.ready())
        until(lambda: (self.bridge.status() or {}).get('state') == 'authorized')
        self.h.fake.disconnect_all()
        until(self.ready)
        self.assertEqual(self.state.sync_epoch, self.bridge.status()['epoch'])
        # Every successful reconnect makes only one fresh snapshot request.
        self.assertLessEqual(self.h.fake.requests.count('user/syncrequest'), 3)

    def test_detach_restores_hooks_does_not_reload_and_removes_future_injection(self):
        self.start()
        app_socket = self.inspect.evaluate('appSocket.readyState')
        clients = len(self.h.fake.clients)
        self.assertTrue(self.bridge.detach())
        self.assertTrue(self.bridge.detach())
        self.assertFalse(self.state.synced)
        self.assertEqual(self.inspect.evaluate('appSocket.readyState'), app_socket)
        self.assertTrue(self.inspect.evaluate('WebSocket===originals[0] && fetch===originals[1] && XMLHttpRequest.prototype.open===originals[2] && appSocket.send===originals[3]'))
        self.assertTrue(self.inspect.evaluate('window[%s] === undefined' % json.dumps(self.bridge.api)))
        until(lambda: len(self.h.fake.clients) < clients)
        self.inspect.call('Page.reload')
        time.sleep(.5)
        self.assertTrue(self.inspect.evaluate('window[%s] === undefined' % json.dumps(self.bridge.api)))
        self.assertFalse(self.bridge.request('account/list')['sent'])

    def test_detach_settles_pending_without_replaying(self):
        self.start()
        self.h.fake.drop_next.add('contract/find')
        replies = []
        thread = threading.Thread(target=lambda: replies.append(self.bridge.request('contract/find', query='name=MNQZ1', timeout=4)))
        thread.start()
        until(lambda: self.bridge.status()['pending'] == 1)
        self.assertTrue(self.bridge.detach())
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(replies[0]['ok'])
        self.assertTrue(replies[0]['sent'])
        self.assertEqual(self.h.fake.requests.count('contract/find'), 1)

    def test_fetch_rotation_trusts_exact_origins_and_redacts_lifecycle(self):
        # Install a fake REST response producer BEFORE bridge wrapper installation.
        # Real WebSocket transport remains the isolated fake server.
        self.inspect.evaluate('window.fetch = input => Promise.resolve({ok:true,url:String(input),clone(){return this;},text(){return Promise.resolve(JSON.stringify({accessToken:window.restToken}));}})')
        self.start()
        base = 'http://127.0.0.1:%d' % self.h.fake.port
        self.inspect.evaluate('window.restToken = "rotated-rest-token-1234567890"')
        for url in (base + '.evil.test/v1/auth/renewaccesstoken', base + '/else/accesstoken',
                    base + '/v1/auth/renewaccesstoken?token=oops'):
            self.inspect.evaluate('fetch(%s)' % json.dumps(url))
        time.sleep(.1)
        self.assertEqual(self.bridge.status()['tokenGeneration'], 1)
        self.inspect.evaluate('fetch(%s)' % json.dumps(base + '/v1/auth/renewaccesstoken'))
        until(lambda: self.bridge.status()['tokenGeneration'] == 2)
        self.h.fake.token = 'rotated-rest-token-1234567890'
        self.h.fake.disconnect_all()
        until(lambda: not self.ready())
        until(self.ready)
        encoded = json.dumps(self.bridge.events) + json.dumps(self.bridge.status())
        self.assertNotIn(TOKEN, encoded)
        self.assertNotIn(self.h.fake.token, encoded)

    def test_xhr_rotation_and_redirect_origin_filter(self):
        self.start()
        url = 'http://127.0.0.1:%d/v1/auth/renewaccesstoken' % self.h.fake.port
        token = 'rotated-xhr-token-1234567890'
        script = '''(() => {const x = new XMLHttpRequest(); x.open('GET', %s);
          Object.defineProperties(x, {status:{value:200}, responseURL:{value:%s}, responseText:{value:%s}});
          x.dispatchEvent(new Event('load'));})()'''
        self.inspect.evaluate(script % (json.dumps(url), json.dumps('https://evil.test/v1/auth/renewaccesstoken'),
                                        json.dumps(json.dumps({'accessToken': token}))))
        self.assertEqual(self.bridge.status()['tokenGeneration'], 1)
        self.inspect.evaluate(script % (json.dumps(url), json.dumps(url), json.dumps(json.dumps({'accessToken': token}))))
        until(lambda: self.bridge.status()['tokenGeneration'] == 2)
        self.h.fake.token = token
        self.h.fake.disconnect_all()
        until(lambda: not self.ready())
        until(self.ready)


if __name__ == '__main__':
    unittest.main(verbosity=2)
