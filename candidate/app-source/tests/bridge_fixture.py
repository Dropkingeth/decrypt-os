"""A fake Tradovate bridge for tests: real config files, a fake GET /state and a fake /order sender.

No socket is opened. All values are fixtures.
"""
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app/trading-runtime'))
from decrypt_trading import customer_contract as contract  # noqa: E402
from decrypt_trading.bridge_route import open_route, tradovate_symbol  # noqa: E402

TOKEN = 'fixture-order-token-0123456789abcdef'


def native_binding(name='bridge-fixture', pair=contract.SOUP, account=12345, offset=0):
    engine_sha, strategy_sha = contract.PAIRS[pair]
    value = {
        'schema': contract.BINDING, 'provider': 'tradovate', 'loginGroupId': 'login-1',
        'instanceId': name, 'customerId': 'fixture-customer', 'installationId': 'fixture-install',
        'pairId': pair, 'engineSha256': engine_sha, 'strategySha256': strategy_sha,
        'accountId': str(account), 'accountMode': 'demo', 'nativeAccountId': account,
        'nativeAccountName': 'Fixture Demo', 'routeId': name + '-route', 'routeSha256': '0' * 64,
        'feedId': 'fixture-feed', 'streamId': 'fixture-stream', 'symbol': 'MNQZ2026', 'root': 'MNQ',
        'tickSize': .25, 'tp1LimitOffsetHandles': offset, 'signalProducerId': 'fixture-signals',
        'feedProducerId': 'fixture-quotes', 'brokerProducerId': 'fixture-broker',
    }
    value['routeSha256'] = contract.route_digest(value)
    return value


class FakeBridge:
    """Writes login A's config.json and tvbridge.env; answers /state and /order in memory."""

    def __init__(self, data_root, binding, *, clock):
        self.data = Path(data_root)
        self.tvbridge, self.soup = self.data / 'tvbridge', self.data / 'soup'
        self.adapter_db = self.data / 'tvbridge-state' / 'events.sqlite'
        self.kill_switch = self.tvbridge / 'ENTRIES-DISABLED'
        self.binding, self.clock = binding, clock
        self.tvbridge.mkdir(parents=True, mode=0o700)
        self.write_config()
        env = self.tvbridge / 'tvbridge.env'
        env.write_text('TVBRIDGE_TOKEN=%s\n' % TOKEN)
        os.chmod(env, 0o600)
        self.fetches = []
        self.sent = []
        self.outcome = ('accepted', {'broker_order_id': '9001'})
        self.state_override = {}
        self.available = True
        # GET /feed: the 1-second feed (tvbridge.marketfeed) for a strategy that requires it (Lumi).
        self.feed_bars = []
        self.feed_override = {}
        self.feed_fetches = 0

    def write_config(self, **changes):
        cfg = {'account_name': self.binding['nativeAccountName'], 'account_id': None,
               'soup_account_id': self.binding['accountId'], 'ticker': self.binding['symbol'],
               'listen_port': 8777, 'publish_mode': 'adapter-db', 'soup_adapter_db': str(self.adapter_db),
               'soup_context_path': str(self.soup / 'broker-context.json'),
               'kill_switch_file': str(self.kill_switch)}
        cfg.update(changes)
        (self.tvbridge / 'config.json').write_text(json.dumps(cfg))

    def state(self, trade_id=None):
        import hashlib
        self.fetches.append(trade_id)
        if not self.available:
            return None
        value = {'account_id': self.binding['nativeAccountId'],
                 'account_name_sha256': hashlib.sha256(self.binding['nativeAccountName'].encode()).hexdigest(),
                 'soup_account_id': self.binding['accountId'],
                 'contract': {'name': tradovate_symbol(self.binding['symbol'])},
                 'publish_mode': 'adapter-db', 'observed_at': self.clock(), 'synced': True, 'healthy': True,
                 'net_position': 0, 'open_order_ids': [], 'unseen_accepted_orders': [], 'unresolved_commands': 0,
                 'entries_disabled': False, 'trade_entry_outcome': None}
        value.update(self.state_override)
        return value

    def feed(self):
        """GET /feed/<token> the way tvbridge.marketfeed.SecondFeed.snapshot() answers it."""
        self.feed_fetches += 1
        if not self.available:
            return None
        live = bool(self.feed_bars)
        value = {'schema': 'decrypt.tvbridge-1s-feed.v1', 'configured': True, 'symbol': self.binding['symbol'],
                 'observed_at': self.clock(), 'live': live, 'fresh': live, 'reason': None if live else 'no_data',
                 'bars': [dict(b) for b in self.feed_bars[-10:]] if live else []}
        value.update(self.feed_override)
        return value

    def send(self, payload):
        self.sent.append(json.loads(json.dumps(payload)))
        return self.outcome

    def opener(self, binding):
        transport = open_route(binding, root=self.tvbridge, soup_root=self.soup, fetch=self.state, clock=self.clock,
                               start=False, feed_fetch=self.feed)
        transport.native = self  # the /order sender; the real one is bridge_transport.TradovateBridgeTransport
        return transport

    def publish(self, seq, payload, observed_at=None):
        """Append a broker event the way the bridge's adapter-db sink does."""
        self.adapter_db.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.adapter_db)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,channel TEXT,event_id TEXT UNIQUE,'
                       'payload TEXT,source TEXT,observed_at INTEGER)')
            db.execute('INSERT INTO events VALUES (?,?,?,?,?,?)',
                       (seq, 'broker', 'tvbridge-%d' % seq, json.dumps(payload), 'tvbridge',
                        self.clock() if observed_at is None else observed_at))
            db.commit()
        finally:
            db.close()
