"""The bridge's 1-second feed as the runtime's price input (beta4). Lumi RTH Gap requires it.

The Tradovate bridge builds 1-second bars from the broker's own quotes for the binding's dated contract
(tvbridge/marketfeed.py) and serves them at GET /feed/<TVBRIDGE_TOKEN>. This reader polls that answer in its
own thread and, on the runtime worker's tick, queues each new closed 1-second bar as one `quote` input
(time = the second's close, price = its last trade) from the binding's feed producer. The strategy engine
then runs touch entries, break-even and price-hit exits on those prices (bridge_v2_lumi.py).

Safety only, the same rules as the bridge's per-contract price reading:
- own contract only: an answer for any other contract is refused;
- prices never open a trade: this module only queues price inputs; the engine decides;
- stale means closed: an answer that is not fresh, older than ANSWER_MAX_AGE_MS, unreachable or malformed
  queues nothing and withdraws the market (runtime.invalidate_market), so entries wait;
- a restart starts empty: only bars at most 2 s old are ever queued (the runtime's own quote limit).

No socket, thread or token read happens on import.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SCHEMA = 'decrypt.tvbridge-1s-feed.v1'
EVENT_PREFIX = 'bridge1s:'          # event ids of the quotes this reader queues: bridge1s:<contract>:<close ms>
SECOND_MS = 1000
ANSWER_MAX_AGE_MS = 2000            # the bridge's answer (and its newest bar) must be at most this old
POLL_S = 0.25
TIMEOUT_S = 1.0

# Strategy adapters that require the feed: their route is not built without it (open_route).
REQUIRED_BY = {'lumi-gap-bridge-v2': 'Lumi RTH Gap'}

MESSAGES = {
    'feed_unavailable': 'The bridge did not answer for the 1-second MES feed.',
    'feed_not_configured': 'The bridge has no 1-second MES feed. Add "market_feed": {"enabled": true} to its '
                           'config (setup.py init --adapter lumi-gap-bridge-v2 writes it) and restart it.',
    'feed_wrong_contract': 'The bridge\'s 1-second feed is for a different contract than this binding.',
    'feed_not_fresh': 'The bridge\'s 1-second MES feed is not live and fresh.',
    'feed_stale': 'The bridge\'s 1-second MES feed answer is stale.',
}


def milliseconds():
    return time.time_ns() // 1_000_000


def event_id(symbol, close_time):
    return '%s%s:%d' % (EVENT_PREFIX, symbol, close_time)


def message(problem):
    """Plain-language text for a problem code (a code may carry ':<bridge reason>')."""
    if problem is None:
        return 'The 1-second MES feed is live and fresh.'
    code, _, detail = problem.partition(':')
    text = MESSAGES.get(code, MESSAGES['feed_not_fresh'])
    return text + (' (%s)' % detail if detail else '')


def refusal(adapter_label, problem):
    """The plain refusal when a strategy that requires the feed cannot start."""
    return ('%s needs the live 1-second MES feed from the bridge and did not start. %s'
            % (adapter_label, message(problem)))


def _bar(value):
    return (isinstance(value, dict) and type(value.get('t')) is int and type(value.get('close_time')) is int
            and value['close_time'] - value['t'] == SECOND_MS and value['t'] % SECOND_MS == 0
            and all(type(value.get(k)) in (int, float) and value[k] > 0 and value[k] == value[k] and value[k] != float('inf')
                    for k in ('o', 'h', 'l', 'c'))
            and value['l'] <= min(value['o'], value['c']) <= max(value['o'], value['c']) <= value['h'])


def snapshot_problem(symbol, snap, now):
    """None when the bridge's answer is a fresh feed for `symbol` with a current bar, else a problem code."""
    if not isinstance(snap, dict) or snap.get('schema') != SCHEMA:
        return 'feed_unavailable'
    if snap.get('configured') is not True:
        return 'feed_not_configured'
    if snap.get('symbol') != symbol:
        return 'feed_wrong_contract'
    if snap.get('fresh') is not True:
        reason = snap.get('reason')
        return 'feed_not_fresh' + (':' + reason if isinstance(reason, str) and re.fullmatch(r'[a-z_:]{1,60}', reason) else '')
    observed, bars = snap.get('observed_at'), snap.get('bars')
    if type(observed) is not int or not -1000 <= now - observed <= ANSWER_MAX_AGE_MS:
        return 'feed_stale'
    if not isinstance(bars, list) or not bars or len(bars) > 120 or not all(_bar(b) for b in bars):
        return 'feed_unavailable'
    if not 0 <= now - bars[-1]['close_time'] <= ANSWER_MAX_AGE_MS:
        return 'feed_stale'
    return None


class BridgeSecondFeed:
    """Background reader of GET /feed/<token>; queues new 1-second bars as quotes on the runtime's tick."""

    def __init__(self, symbol, token, base_url, *, label='This strategy', fetch=None, clock=milliseconds,
                 poll_s=POLL_S, timeout_s=TIMEOUT_S):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path not in ('', '/'):
            raise ValueError('bridge_not_loopback')
        self.symbol, self.label, self._token, self.base = symbol, label, token, base_url.rstrip('/')
        self.fetch = fetch or self._http_fetch
        self.clock, self.poll_s, self.timeout_s = clock, poll_s, timeout_s
        self.lock = threading.Lock()
        self.latest = None          # (fetched_at_ms, answer)
        self.queued_through = 0     # close time of the newest bar queued (in memory: a restart starts empty)
        self.problem_shown = None
        self.counts = {'queued': 0, 'refused_by_runtime': 0}
        self.stopping = threading.Event()
        self.thread = None

    def _http_fetch(self):
        try:
            with urllib.request.urlopen(self.base + '/feed/' + self._token, timeout=self.timeout_s) as response:
                raw = response.read(65537)
        except (urllib.error.URLError, OSError, ValueError):
            return None
        try:
            data = json.loads(raw) if len(raw) <= 65536 else None
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def refresh(self):
        answer = self.fetch()
        with self.lock:
            self.latest = (self.clock(), answer)
        return answer

    def _latest(self):
        with self.lock:
            return self.latest

    @staticmethod
    def _problem_of(symbol, latest, now):
        if latest is None or not 0 <= now - latest[0] <= ANSWER_MAX_AGE_MS:
            return 'feed_unavailable'
        return snapshot_problem(symbol, latest[1], now)

    def problem(self, now=None):
        now = self.clock() if now is None else now
        return self._problem_of(self.symbol, self._latest(), now)

    def check_start(self):
        """Refresh once and return the problem that stops the strategy from starting, or None."""
        self.refresh()
        return self.problem()

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name='bridge-1s-feed', daemon=True)
            self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=self.timeout_s + 2)

    def _run(self):
        while not self.stopping.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - the reader must survive; a missing answer fails closed
                with self.lock:
                    self.latest = (self.clock(), None)
            self.stopping.wait(self.poll_s)

    def tick(self, runtime):
        """Runtime worker: queue every new fresh bar as a quote; withdraw the market when the feed is not fresh."""
        now = runtime.clock()
        latest = self._latest()            # one copy: a poll finishing meanwhile cannot change what is checked
        problem = self._problem_of(self.symbol, latest, now)
        producer = runtime.binding['feedProducerId']
        if problem is not None:
            if problem != self.problem_shown:
                code = re.sub(r'[^a-z0-9_]', '_', ('bridge_1s_' + problem).lower())[:96]
                runtime.invalidate_market(code, producer_id=producer)
                self.problem_shown = problem
            return 0
        self.problem_shown = None
        bars = list(latest[1]['bars'])
        queued = 0
        for bar in bars:
            close_time = bar['close_time']
            if close_time <= self.queued_through or not 0 <= now - close_time <= ANSWER_MAX_AGE_MS:
                continue
            quote = {'event_id': event_id(self.symbol, close_time), 'symbol': self.symbol, 'time': close_time,
                     'price': bar['c']}
            try:
                runtime.admit_authenticated('quote', json.dumps(quote), producer_id=producer,
                                            event_id=quote['event_id'], sequence=close_time)
                queued += 1
                self.counts['queued'] += 1
            except Exception:  # noqa: BLE001 - refused (stale, sequence, closed runtime): never retried
                self.counts['refused_by_runtime'] += 1
            self.queued_through = close_time
        return queued

    def status(self, now=None):
        now = self.clock() if now is None else now
        latest = self._latest()
        problem = self._problem_of(self.symbol, latest, now)
        answer = (latest or (None, None))[1] or {}
        return {'feed': '1s', 'symbol': self.symbol, 'fresh': problem is None, 'reason': problem,
                'message': message(problem), 'bridge_message': answer.get('message'),
                'last_bar_at': self.queued_through or None, 'queued': self.counts['queued'],
                'refused_by_runtime': self.counts['refused_by_runtime']}
