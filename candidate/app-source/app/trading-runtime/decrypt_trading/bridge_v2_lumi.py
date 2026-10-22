"""Lumi RTH Gap on decrypt-bridge-v2 (beta4): the strategy declaration and its extras.

The Lumi Gap strategy (MES) sends its whole plan in the shared format (PAYLOAD-V2.md): market or touch
entry, resting stop, targets, runner (target, resting limit or trail), break-even, end of day, daily loss
lock, and the management events cancel_entry, flatten and modify_stop, each with the values the alert sends. The shared engine (bridge_v2.py) executes it; this module adds no trading rule.

Extras on the entry, recorded with the trade:
- setup: the strategy's own setup label, a short free-form tag (1-12 of A-Z a-z 0-9 _ -). Recorded only.
- chart_symbol: the chart's contract. It must equal `symbol`, so the plan's prices come from the contract
  the bridge trades (an alert from a continuous chart such as MES1!, or a chart left on the old contract
  after a roll, is refused). This is an identity check, not a trade rule.
- bar_seconds: the chart timeframe the plan was decided on. Recorded only; the strategy owns its timeframe.
- decision_time: close of the decision bar, Unix ms.
- account_profile, risk_usd: a free-text profile label (printable ASCII, up to 120 characters) and the dollar
  risk at the stop. Recorded only.

Lumi requires the 1-second MES feed (the bridge's own broker prices for the binding's dated contract,
tvbridge/marketfeed.py, read by bridge_feed.py). Its touch entries, break-even and price-hit exits run only on
those prices:
- a quote that is not a 1-second feed sample (`bridge1s:<contract>:<second close ms>`) is refused;
- an entry alert (market or touch) is refused unless the feed is live and fresh: a sample at most 2 s old,
  observed since this engine started, at the end of at least 3 consecutive 1-second samples (after a
  start, restart or gap the feed warms up again);
- an armed touch is not triggered while the feed is not fresh (a stop touch still disarms it);
- while a trade is open and the feed is not fresh, an alert is recorded once per episode. The resting
  broker stop keeps protecting the trade; nothing is sent because of a stale feed.
Nothing about prices survives a restart.
"""
from . import bridge_v2

def check(payload):
    """Cross-field format check: the chart the plan came from is the contract the bridge trades."""
    if payload['event'] == 'entry':
        bridge_v2.contract.require(payload['extras']['chart_symbol'] == payload['symbol'],
                                   'extras.chart_symbol must equal symbol (chart the dated contract, not MES1!)')


STRATEGY = bridge_v2.Strategy(
    strategy_id='lumi-gap', adapter_id='lumi-gap-bridge-v2', label='Lumi RTH Gap',
    extras={'entry': {
        'setup': {'type': 'str', 'pattern': r'[A-Za-z0-9_-]{1,12}', 'max_len': 12},
        'chart_symbol': {'type': 'str', 'pattern': r'[A-Za-z0-9!._-]{1,24}', 'max_len': 24},
        'bar_seconds': {'type': 'int', 'min': 1, 'max': 86400},
        'decision_time': {'type': 'ms'},
        'account_profile': {'type': 'str', 'pattern': r'[ -~]{1,120}', 'max_len': 120},
        'risk_usd': {'type': 'number', 'min': 0},
    }},
    check=check)

_V2Engine, input_identity, payload_schema, signal_time, is_entry_payload, bridge_message = \
    bridge_v2.module_api(STRATEGY)

# --- the 1-second MES feed requirement ------------------------------------------------------------------
FEED_PREFIX = 'bridge1s:'        # bridge_feed.EVENT_PREFIX
FEED_STEP_MS = 1000
FEED_MAX_AGE_MS = 2000
FEED_WARMUP_SAMPLES = 3
FEED_MESSAGES = {
    'mes_1s_feed_no_data': 'Lumi refused: the 1-second MES feed has sent no price since Lumi started.',
    'mes_1s_feed_stale': 'Lumi refused: the 1-second MES feed is stale (no price for more than 2 seconds).',
    'mes_1s_feed_warming_up': 'Lumi refused: the 1-second MES feed is warming up after a start or gap '
                              '(3 consecutive seconds needed).',
}


def feed_message(problem):
    """Plain-language text for a feed problem code (None: live and fresh)."""
    return 'The 1-second MES feed is live and fresh.' if problem is None else FEED_MESSAGES[problem]


class Engine(_V2Engine):
    """The shared decrypt-bridge-v2 engine, with Lumi's 1-second MES feed requirement (module docstring)."""

    def __init__(self, database, config, **options):
        super().__init__(database, config, **options)
        # In memory only: after a restart the feed must prove itself again.
        self.feed = {'last': None, 'since': None, 'alerted': False}
        if self.data['last_quote'] is not None:
            self.data['last_quote'] = None              # no price survives a restart
            self.save()

    def feed_problem(self, now):
        f = self.feed
        if f['last'] is None:
            return 'mes_1s_feed_no_data'
        if not 0 <= now - f['last'] <= FEED_MAX_AGE_MS:
            return 'mes_1s_feed_stale'
        if (f['last'] - f['since']) // FEED_STEP_MS + 1 < FEED_WARMUP_SAMPLES:
            return 'mes_1s_feed_warming_up'
        return None

    def feed_status(self, now):
        problem = self.feed_problem(now)
        return {'fresh': problem is None, 'reason': problem, 'message': feed_message(problem),
                'last_sample_at': self.feed['last']}

    def entry_source_ready(self, now, opened_at):
        """The runtime's dispatch gate: a fresh feed sample observed since the runtime opened."""
        return self.feed_problem(now) is None and self.feed['last'] >= opened_at

    def apply(self, channel, p, now, *, entries_enabled=False, locked=False):
        if channel != 'quote':
            return super().apply(channel, p, now, entries_enabled=entries_enabled, locked=locked)
        at, eid = p.get('time'), p.get('event_id')
        bridge_v2.require(type(at) is int and at % FEED_STEP_MS == 0 and type(eid) is str
                          and eid == '%s%s:%d' % (FEED_PREFIX, self.config['ticker'], at),
                          'Lumi reads prices only from the 1-second MES feed')
        before = dict(self.feed)
        f = self.feed
        if f['last'] is None or at > f['last']:
            if f['last'] is None or at - f['last'] != FEED_STEP_MS:
                f['since'] = at                         # a start or a gap: a new run, warm-up again
            f['last'] = at
        try:
            return super().apply(channel, p, now, entries_enabled=entries_enabled, locked=locked)
        except BaseException:
            self.feed = before
            raise

    def entry(self, p, now, allowed):
        if allowed:
            problem = self.feed_problem(now)
            if problem is not None:
                return 'refused:' + problem
        return super().entry(p, now, allowed)

    def observe(self, price, at, now, allowed):
        t = self.data['trade']
        if t and t['phase'] == 'armed' and self.feed_problem(now) is not None:
            p, sign = t['payload'], (1 if t['side'] == 'long' else -1)
            t['last_price'] = price
            if p['touch']['cancel_if_stop_touched'] and sign * (price - p['stop']['price']) <= 0:
                self._close('stop_touched_before_entry', now)
                return 'touch_entry_disarmed'
            return 'quote_recorded_touch_waits_for_fresh_feed'
        return super().observe(price, at, now, allowed)

    def tick(self, now):
        super().tick(now)
        t = self.data['trade']
        problem = self.feed_problem(now)
        if problem is None:
            self.feed['alerted'] = False
        elif t and t['phase'] in bridge_v2.OPEN and not self.feed['alerted']:
            self.feed['alerted'] = True
            self._alert(t, 'mes_1s_feed_not_live:' + problem, now)
            self.save()
