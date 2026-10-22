#!/usr/bin/env python3
"""Tradovate bridge status panel. Version 2.0.

Read-only loopback page (127.0.0.1:8779) for the bridge's own desktop
window. It aggregates the bridge daemon's /health, /state and (1.3) /followers,
the login guard, Soup's status, the kill switches, unit states and recent daemon
log lines. It has no controls: it never sends orders, never changes a kill
switch and never restarts anything. The bridge token is used only server-side
and is scrubbed from every response.

1.2 added the resting stop of the latest native (bracket) trade: quantity, price,
initial stop or break-even, and when Tradovate confirmed it.

1.3 (redesign): Soup sends its orders to the main account directly through the bridge,
so the page no longer describes a copy service. It adds the followers
(tvbridge 1.0.9): the main account leads, the followers follow. Per account it
shows status, kill switch, position, working orders, resting stop and the last
alert. Follower sources, all read-only: the daemon's own GET /followers (in-memory
account state, the same kind of local read as /state; no Tradovate request), the
`followers` block of config.json, the follower kill-switch files, firing.json,
the firing service's status.json, alerts.jsonl and lane journals, and the
systemd state of tvbridge-firing.service. /followers is asked only when
config.json has a `followers` block. The old copy service's data stays in the
JSON (`mirror`); the page no longer shows it.

1.4 (redesign): each account card shows the account balance with its as-of
time. The main account's balance is the daemon's own GET /account reply
(Tradovate's cash row the bridge already holds; asked at most every
BALANCE_CACHE_SECONDS and only when config.json opts in with
`account_view_enabled`). A follower's balance is read from its /followers row when the daemon
reports `cash_balance` there; the 1.0.9.1 daemon does not, so it reads
"unavailable". A missing or stale balance is always "unavailable", never zero.
Alerts whose condition has ended (for example an `order_unknown` command later
reconciled in the follower's bridge journal) are marked resolved from read-only
evidence and shown greyed, so only live problems are red.

2.0 (new front end): the page is rebuilt around the trader's questions: is the system
live and safe (six checks), is a trade on and where is it in its lifecycle on
each account (entry, stop confirmed, TP1, break-even, runners, flat), do the
followers match the leader, and the money. The data collection is the 1.4 one;
/api/status only gains derived fields: `checks`, `trade` and a `lifecycle` on
each card, all computed from data the snapshot already holds (the bridge
journal's entry, TP1 and stop records; the follower lane journals; /state;
/followers; Soup's status).

Multi-login (tvbridge 1.1.0): when the login registry `logins.json` exists (default
ROOT/logins.json, or env TVBRIDGE_LOGINS, or config.json `logins_file`), the page shows one
section per Tradovate login, primary first: its connection state (from that login's guard
status.json, /health and the read-only GET /login), last re-login, visible accounts, its account
cards, units and daemon log. /api/status then gains `logins` and `verdict_logins`, the page's
headline: a connection problem on a non-primary login turns a healthy headline amber and names
that login. `verdict` stays the primary login's own. Without the registry the page and
/api/status are unchanged. A login's /health and /login use its read token (env var
`read_token_env`) when set, else its order token (`token_env`); so does the read-only
/followers report. Every token is used only server-side and is scrubbed like TVBRIDGE_TOKEN.
"""
from datetime import datetime
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request

VERSION = '2.1'
PORT = int(os.environ.get('TVBRIDGE_STATUS_PORT', '8779'))
HOME = Path.home()
ROOT = Path(os.path.expanduser(os.environ.get('TVBRIDGE_ROOT') or '~/.local/share/decrypt-os/tvbridge'))
CONFIG = ROOT / 'config.json'
MIRROR = ROOT / 'mirror/status.json'
GUARD_DIR = HOME / '.local/share/decrypt-os/tvbridge-state/login-guard'
SOUP = HOME / '.local/share/decrypt-os/soup/status.json'
# The follower service has its own entry under `followers`.
UNITS = ('tvbridge-browser.service', 'tvbridge-daemon.service', 'tvbridge-login-guard.timer')
FIRING_UNIT = 'tvbridge-firing.service'
# Webhook line (2.2, read-only display): an optional registry naming the live webhook's
# strategy. The firing order and account labels it is shown next to come from firing.json,
# which the followers view already reads; this file adds only a strategy name on top.
WEBHOOK_REGISTRY = Path(os.path.expanduser(os.environ.get('TVBRIDGE_ADMIN_STATE') or '~/.local/share/decrypt-os/tvbridge-admin/state')) / 'webhooks.json'
STRATEGY_LABELS = {'soup-pine-r4': 'Soup Pine r4'}
NOISE = ('page event connecting', 'page event closed')
ALLOWED_HOSTS = {'127.0.0.1:%d' % PORT, 'localhost:%d' % PORT}
GUARD_STALE_SECONDS = 180    # guard runs about every 30 s; older results say nothing about login
RECENT_RESTART_SECONDS = 300
# Guard records of a daemon (re)start. Both spellings of the unhealthy-restart
# event are accepted: as a state/reason/detail value or as an event kind.
RESTART_MARKERS = ('daemon_restarted', 'daemon_start_attempt', 'daemon_restart_attempt')
# Resting stop (1.2). The evidence log gains ~600 bytes per broker change while a trade is
# open; 4 MB covers several hours of one trade. Older lines are never needed for the
# current stop, only for the time its first confirmation was seen.
SNAPSHOT_TAIL_BYTES = 4_000_000
JOURNAL_TAIL_BYTES = 1_000_000
STOP_KIND_LABELS = {'initial': 'initial stop', 'break_even': 'break-even', 'manual': 'moved by hand',
                    'unknown': 'type unknown'}
# Followers (1.3).
ALERTS_TAIL_BYTES = 256_000
LANE_TAIL_BYTES = 1_000_000
FIRING_STALE_SECONDS = 20    # a firing status older than this is stale
LANE_BLOCK_NAME = 'ENTRIES-BLOCKED-NATIVE-EXIT'
ALERT_LIST = 8
# Balances (1.4).
BALANCE_CACHE_SECONDS = 30       # /account is asked at most this often; balances change slowly
BALANCE_MAX_AGE_SECONDS = 90     # a /account reply older than this is stale: shown as unavailable
BALANCE_CACHE = {}               # {'at': fetched epoch s, 'reply': dict, 'error': str}
BALANCE_LOCK = threading.Lock()
# Alert kinds that block a follower's entries until someone acts (1.4 resolution rules).
BLOCKING_KINDS = ('order_unknown', 'exit_all_unverified', 'lane_stopped', 'follower_unavailable', 'error')


def token():
    return os.environ.get('TVBRIDGE_TOKEN', '')


LOGIN_TOKEN_ENVS = set()   # every registry login's `token_env`, filled in when the registry is read


def scrub(text):
    for t in {token()} | {os.environ.get(name, '') for name in sorted(LOGIN_TOKEN_ENVS)}:
        if t and len(t) >= 24:
            text = text.replace(t, '<token>')
    return text


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


LOOPBACK_HOSTS = ('127.0.0.1', 'localhost', '::1')


def daemon_get(base_url, endpoint, tok, timeout=3):
    """GET <base_url>/<endpoint>/<token> from a bridge daemon on this machine: (reply, error).
    The one HTTP path of the panel. A token is never sent anywhere but loopback."""
    if len(tok or '') < 24:
        return None, 'token_missing'
    try:
        host = urllib.parse.urlsplit(base_url).hostname
    except ValueError:
        host = None
    if host not in LOOPBACK_HOSTS:
        return None, 'not_loopback'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open('%s/%s/%s' % (base_url.rstrip('/'), endpoint, tok), timeout=timeout) as r:
            return json.loads(r.read(200_000)), None
    except Exception as exc:
        return None, type(exc).__name__


def bridge(endpoint, port):
    return daemon_get('http://127.0.0.1:%d' % port, endpoint, token())


def units():
    out = {}
    for unit in UNITS:
        try:
            r = subprocess.run(['systemctl', '--user', 'show', unit, '-p', 'ActiveState', '-p', 'SubState',
                                '-p', 'Result', '-p', 'StateChangeTimestamp', '-p', 'UnitFileState'],
                               capture_output=True, text=True, timeout=5)
            props = dict(line.split('=', 1) for line in r.stdout.splitlines() if '=' in line)
        except Exception:
            props = {}
        out[unit] = {'active': props.get('ActiveState', 'unknown'), 'sub': props.get('SubState', ''),
                     'result': props.get('Result', ''), 'since': props.get('StateChangeTimestamp', ''),
                     'file': props.get('UnitFileState', '')}
    return out


def turned_off(unit):
    """True for a unit stopped and disabled on purpose (setup.py disable: `systemctl disable --now`).
    A unit that stopped on its own stays enabled, so it still reads as a fault."""
    return isinstance(unit, dict) and unit.get('active') in ('inactive', 'failed') and unit.get('file') == 'disabled'


def daemon_log():
    return unit_log('tvbridge-daemon')


def unit_log(unit):
    """The last 12 non-noise journal lines of one user unit (scrubbed) and the noise count."""
    try:
        r = subprocess.run(['journalctl', '--user', '-u', unit, '-n', '300', '--no-pager',
                            '-o', 'cat'], capture_output=True, text=True, timeout=5)
    except Exception:
        return [], 0
    lines = [scrub(l)[:240] for l in r.stdout.splitlines() if l.strip()]
    noise = sum(1 for l in lines[-60:] if any(n in l for n in NOISE))
    return [l for l in lines if not any(n in l for n in NOISE)][-12:], noise


def guard_events(guard_dir=None):
    try:
        raw = (Path(guard_dir or GUARD_DIR) / 'events.jsonl').read_text().splitlines()[-40:]
    except OSError:
        return []
    events = []
    for line in raw:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if e.get('kind') != 'observation' or e.get('state') != 'ok' or mentions_restart(e):
            events.append({k: e.get(k) for k in ('at', 'kind', 'state', 'reason', 'detail')})
    return events[-10:]


def parse_ts(value):
    """Epoch seconds from the guard's ISO strings (or plain numbers); None if unreadable."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value / 1000 if value > 1e12 else float(value)
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return None


def ago(seconds):
    seconds = max(0, int(round(seconds)))
    return '%d s ago' % seconds if seconds < 90 else '%d min ago' % round(seconds / 60)


def mentions_restart(record):
    return record.get('state') == 'daemon_started' or any(
        m in str(record.get(k) or '') for k in ('kind', 'reason', 'detail', 'event') for m in RESTART_MARKERS)


def guard_login(g, now):
    """'out', 'in' or None. Only a fresh login-guard result may say Tradovate is logged out."""
    checked = parse_ts((g or {}).get('checked_at'))
    if checked is None or now - checked > GUARD_STALE_SECONDS:
        return None
    state, reason = g.get('state'), str(g.get('reason') or '')
    if state == 'logged_out_waiting' or g.get('logged_out_since'):
        return 'out'
    if state in ('ok', 'logged_in_now', 'daemon_started') or 'daemon' in reason:
        return 'in'
    return None


def recent_restart(g, events, now):
    """Seconds since the login guard last (re)started the daemon, if within RECENT_RESTART_SECONDS."""
    stamps = [parse_ts(g.get('checked_at'))] if g and mentions_restart(g) else []
    stamps += [parse_ts(e.get('at') or e.get('ts')) for e in events or () if isinstance(e, dict) and mentions_restart(e)]
    stamps = [t for t in stamps if t is not None and -5 <= now - t <= RECENT_RESTART_SECONDS]
    return now - max(stamps) if stamps else None


def restarting(unit):
    return unit.get('active') in ('activating', 'deactivating', 'reloading') or unit.get('sub') == 'auto-restart'


def hold_end(g):
    """'the Sun 6:00 PM open (America/New_York)' from the guard's configured hold text."""
    text = ((g or {}).get('weekend_hold') or {}).get('text')
    if isinstance(text, str) and ' to ' in text:
        return 'the ' + text.split(' to ', 1)[1].replace(' (', ' open (', 1)
    return 'the Sunday 6:00 PM ET open'


def logged_out_detail(g, now):
    if g.get('reason') == 'weekend_maintenance_hold':  # login guard 1.3 weekend hold
        return 'Weekend pause: automatic login is off until %s, while Tradovate runs weekend maintenance.' % hold_end(g)
    tried = parse_ts(g.get('last_login_attempt_at'))
    return 'Automatic login is in progress (checked about every 30 seconds).' + (
        ' Last try %s.' % ago(now - tried) if tried else '')


def disconnected_detail(g, daemon, restart_ago):
    if restarting(daemon):
        return 'Tradovate is logged in. Recovery in progress: the bridge daemon is restarting automatically.'
    if restart_ago is not None:
        return 'Tradovate is logged in. Recovery in progress: the login guard restarted the bridge daemon %s.' % ago(restart_ago)
    if g.get('state') == 'logged_in_now':
        return 'Tradovate just logged in. Recovery in progress: the bridge is reconnecting.'
    return ('Tradovate is logged in, but the bridge lost its session with the page. The login guard restarts '
            'the bridge daemon if this lasts over a minute.')


def verdict(s, now=None):
    """One headline plus the next step. Order matters: the most urgent wins.

    "Tradovate logged out" comes only from the login guard. When the guard says
    logged in but the daemon has no usable page session, it is "Bridge
    disconnected from page". Read-only: this only describes recovery by others.
    """
    now = time.time() if now is None else now
    u, h, st, g = s['units'], s['health'], s['state'], s['guard'] or {}
    hh = (h or {}).get('health') or {}
    page = hh.get('page') or {}
    endpoint = page.get('url') or ''
    daemon = u['tvbridge-daemon.service']
    login = guard_login(g, now)
    restart_ago = recent_restart(g, s.get('guard_events'), now)
    if s.get('configured') is False and u['tvbridge-browser.service']['active'] != 'active':
        # A fresh install: the bridge ships off and nothing has been set up yet.
        return 'off', 'Bridge not set up yet', ('The Tradovate bridge ships turned off. Nothing is sent to a broker '
                                                'until you set it up in Decrypt OS under Connections.')
    if s.get('configured') and turned_off(u['tvbridge-browser.service']):
        # Turned off in Connections (setup.py disable): a choice, not a fault. The login guard is off
        # too, so its last status file is stale and is not shown as a login in progress.
        return 'off', 'Bridge is off', ('Nothing is sent to a broker while the bridge is off. Turning it off does not '
                                        'cancel orders or close positions: check Tradovate. Turn it on again in '
                                        'Decrypt OS under Connections.')
    if u['tvbridge-browser.service']['active'] != 'active':
        return 'coral', 'Bridge browser is not running', 'The Tradovate window is closed. Start it from the Bridge Advanced page or run: systemctl --user start tvbridge-browser.service'
    if g.get('state') in ('needs_human', 'login_repeated'):
        if 'daemon' in str(g.get('reason') or '') and not g.get('logged_out_since'):
            return 'coral', 'Needs you: bridge keeps disconnecting', ('Tradovate is logged in, but automatic daemon restarts stopped (%s). '
                                                                      'Check tvbridge-daemon in Bridge Advanced.' % g.get('reason'))
        return 'coral', 'Needs you: log in to Tradovate', ('Automatic login stopped (%s). Log in in the Tradovate window, choose '
                                                           'Access Simulation, and make sure this Tradovate user is not logged in anywhere else.'
                                                           % g.get('reason', 'unknown'))
    if daemon['active'] != 'active' or h is None:
        if s.get('config_problem'):
            # The daemon refuses to start with this config (tvbridge.daemon checks it before the browser).
            return 'coral', 'Bridge config error', (s['config_problem'] + '. The bridge daemon refuses to start with '
                                                    'it, so no orders are sent.')
        if login == 'out':
            return 'amber', 'Tradovate logged out', logged_out_detail(g, now)
        if login == 'in' and restarting(daemon):
            return 'amber', 'Bridge disconnected from page', disconnected_detail(g, daemon, restart_ago)
        if restarting(daemon):
            return 'amber', 'Bridge daemon offline', 'It is restarting automatically.'
        return 'amber', 'Bridge daemon offline', 'The login guard restarts it within a few minutes once Tradovate is logged in.'
    if 'live.tradovateapi.com' in endpoint:
        return 'coral', 'Connected to Tradovate live, not Simulation', 'The bridge refuses orders. The login guard picks Access Simulation; if it stays, choose it in the Tradovate window.'
    if page.get('state') != 'authorized':
        if login == 'out':
            return 'amber', 'Tradovate logged out', logged_out_detail(g, now)
        if login == 'in':
            return 'amber', 'Bridge disconnected from page', disconnected_detail(g, daemon, restart_ago)
        return 'amber', 'Not connected to Tradovate', ('The login guard has no recent result (%s), so the login state is unknown. '
                                                       'Page state: %s.' % (g.get('state') or 'none', page.get('state') or 'none'))
    if page.get('synced') is False or 'not_synced_for_current_connection' in (hh.get('problems') or []):
        # Authorized page, so Tradovate is logged in; the bridge has not synced with it.
        return 'amber', 'Bridge disconnected from page', disconnected_detail(g, daemon, restart_ago)
    if st and st.get('unresolved_commands'):
        return 'coral', 'Unresolved command', 'New entries stay blocked until it is reconciled by hand. Never replay it.'
    if not (h.get('health') or {}).get('ok'):
        return 'amber', 'Connected but not ready', 'Problems: ' + (', '.join(h['health'].get('problems') or []) or 'unknown')
    pause = s.get('pause') if isinstance(s.get('pause'), dict) else {}
    if s['kill_switch'] and pause.get('fault'):
        return 'amber', 'Healthy · entries off', (pause.get('reason') + ' Exits work; new entries are refused until '
                                                  'the pause switch is fixed.')
    if s['kill_switch']:
        return 'teal', 'Healthy · entries off', ('Paused (kill switch on): exits work, new entries are refused '
                                                  'until it is resumed in Bridge Advanced or the switch is removed.')
    unknown = [x for x in ((h or {}).get('peer_hedge_unknown') or []) if isinstance(x, str)]
    if unknown:
        # The daemon's cross-login hedge check fails closed (tvbridge.peers): unknown exposure refuses entries.
        return 'amber', 'Healthy · entries held', ('Login %s is not answering, so its positions are unknown. New entries '
                                                   'that could oppose them are refused until it answers; exits work.'
                                                   % ', '.join(x.upper() for x in unknown))
    return 'mint', 'Healthy · armed', ("The main account receives the strategy's orders directly, with the exact payload: "
                                        'quantity, TP1, stop, break-even, runners and exits.')


def tail_records(path, limit, needle=None):
    """JSON objects from the last `limit` bytes of a JSONL file, oldest first.

    Returns (records, cut): `cut` is True when older lines were not read. A torn
    first or last line and non-object lines are skipped. Unreadable file: ([], False).
    """
    try:
        with open(path, 'rb') as fh:
            fh.seek(0, 2)
            start = max(0, fh.tell() - limit)
            fh.seek(start)
            data = fh.read(limit)
    except OSError:
        return [], False
    lines = data.split(b'\n')
    if start:
        lines = lines[1:]
    records = []
    for line in lines:
        if not line.strip() or (needle is not None and needle not in line):
            continue
        try:
            rec = json.loads(line.decode('utf-8'))
        except ValueError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records, bool(start)


def number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def same_price(a, b):
    return a is not None and b is not None and abs(a - b) < 1e-6


def clock_text(ms):
    """Local wall time with milliseconds, for example '12:14:07.590 UTC'; None if not a time."""
    ms = number(ms)
    if ms is None or ms <= 0:
        return None
    ms = int(ms)
    dt = datetime.fromtimestamp(ms // 1000).astimezone()
    zone = dt.strftime('%Z')
    return dt.strftime('%H:%M:%S') + '.%03d' % (ms % 1000) + (' ' + zone if zone else '')


def price_text(price):
    return None if price is None else '%.2f' % price


def stop_kind(price, trade_id, entry, modifies, context):
    """('initial' | 'break_even' | 'unknown', source). Never a guess.

    1. Soup's own plan for this trade (broker-context engine.trade.payload): the price
       equals exactly one of `break_even_price` and `initial_hard_stop_price`.
    2. The bridge journal: the price equals the bracket entry's stop price (initial).
    3. Soup's command record for the bridge modify_stop that set this price says
       `reason` 'break_even' (engine.commands, keyed by the journal command id).
    """
    engine = (context or {}).get('engine') if isinstance(context, dict) else None
    engine = engine if isinstance(engine, dict) else {}
    trade = engine.get('trade')
    if isinstance(trade, dict) and trade.get('id') == trade_id and isinstance(trade.get('payload'), dict):
        be = number(trade['payload'].get('break_even_price'))
        initial = number(trade['payload'].get('initial_hard_stop_price'))
        if same_price(price, be) and not same_price(price, initial):
            return 'break_even', 'soup_trade_plan'
        if same_price(price, initial) and not same_price(price, be):
            return 'initial', 'soup_trade_plan'
        # 2.1 (Soup's manual-management release): the stop was moved by hand and Soup adopted that price.
        manual = ((trade.get('native') or {}).get('manual') or {}).get('stop') if isinstance(trade.get('native'), dict) else None
        if isinstance(manual, dict) and same_price(price, number(manual.get('price'))):
            return 'manual', 'soup_manual_stop'
    if same_price(price, number(entry.get('stop_price'))):
        return 'initial', 'bridge_journal_entry'
    commands = engine.get('commands') if isinstance(engine.get('commands'), dict) else {}
    reasons = set()
    for m in modifies:
        if same_price(price, number(m.get('stop_price'))):
            c = commands.get(m.get('command_id'))
            reasons.add(c.get('reason') if isinstance(c, dict) else None)
    if reasons == {'break_even'}:
        return 'break_even', 'soup_command_reason'
    return 'unknown', None


def resting_stop(cfg, state):
    """Resting stop of the latest native (bracket) trade the bridge journal knows, or None.

    Read-only, from data the bridge and Soup already write:
    - bridge journal `commands.jsonl`: the bracket entry (trade id, command id,
      initial stop price) and its `modify_stop` records;
    - bridge evidence log `snapshots.jsonl`: each broker snapshot the bridge
      published, with the stop leg `<entry command id>#stop` (quantity and
      stop_price come from Tradovate's latest orderVersion) and `observed_at`;
    - Soup `broker-context.json` (trade plan and command reasons);
    - the bridge /state reply this panel already fetched (open order ids, position).
    "Confirmed by Tradovate" is the time of the first bridge snapshot that showed the
    stop's current quantity and price. Anything missing gives 'unknown', never a guess.
    """
    jdir = Path(cfg.get('journal_dir') or ROOT / 'journal')
    commands, _ = tail_records(jdir / 'commands.jsonl', JOURNAL_TAIL_BYTES)
    entry = None
    for rec in commands:
        if rec.get('type') == 'start' and rec.get('kind') in ('entry', 'bracket_entry') \
                and isinstance(rec.get('trade_id'), str) and isinstance(rec.get('command_id'), str):
            entry = rec
    if entry is None or entry.get('kind') != 'bracket_entry':
        return None  # no trade, or the latest trade has no bridge-held stop
    trade_id = entry['trade_id']
    modifies = [r for r in commands if r.get('type') == 'start' and r.get('kind') == 'modify_stop'
                and r.get('trade_id') == trade_id and isinstance(r.get('command_id'), str)]
    context = read_json(SOUP.with_name('broker-context.json'))
    out = {'trade_id': trade_id, 'side': entry.get('side'), 'open': None, 'resting': None, 'order_id': None,
           'status': None, 'quantity': None, 'price': None, 'price_text': None, 'kind': 'unknown',
           'kind_source': None, 'confirmed_at': None, 'confirmed_text': None, 'evidence_at': None,
           'break_even': None, 'problem': None,
           # 2.0 (additive): the entry command time, its size, and the first automatic stop shrink
           # after TP1 filled (the bridge's `auto_modify_stop` record), from the same journal.
           'entry_at': int(entry['at']) if number(entry.get('at')) is not None else None,
           'entry_text': clock_text(entry.get('at')), 'entry_quantity': number(entry.get('quantity')),
           'tp1_filled_at': None, 'tp1_filled_text': None, 'first_confirmed_at': None, 'first_confirmed_text': None}

    live = state if isinstance(state, dict) else None
    live_net = number((live or {}).get('net_position'))
    live_ids = (live or {}).get('open_order_ids')
    live_ids = [str(i) for i in live_ids] if isinstance(live_ids, list) else None

    snaps, cut = tail_records(jdir / 'snapshots.jsonl', SNAPSHOT_TAIL_BYTES, trade_id.encode())
    leg_id = entry['command_id'] + '#stop'
    runs = []  # consecutive snapshots with the same (quantity, stop price): the order versions seen
    last = None
    for snap in snaps:
        if snap.get('trade_id') != trade_id or not isinstance(snap.get('orders'), list):
            continue
        leg = next((o for o in snap['orders'] if isinstance(o, dict) and o.get('command_id') == leg_id), None)
        if leg is None:
            continue
        key = (number(leg.get('quantity')), number(leg.get('stop_price')))
        if not runs or runs[-1]['key'] != key:
            at = number(snap.get('observed_at'))
            # With a cut log, the oldest run seen may have started before the lines we read.
            runs.append({'key': key, 'at': None if (cut and not runs) else at})
        last = (snap, leg)
    if last is None:
        out['problem'] = 'no_stop_evidence'
        if live_net is not None:
            out['open'] = live_net != 0
        return out
    snap, leg = last
    qty, price = runs[-1]['key']
    # 2.0: TP1 filled when Tradovate's stop leg first shrank below the entry size (an order
    # version the bridge snapshot showed); the time is that run's first snapshot.
    entry_qty = out['entry_quantity']
    if runs[0]['at'] is not None:   # 2.0: when the first stop version was seen (the initial stop)
        out['first_confirmed_at'] = int(runs[0]['at'])
        out['first_confirmed_text'] = clock_text(runs[0]['at'])
    for run in runs:
        q = run['key'][0]
        if entry_qty is not None and q is not None and q < entry_qty:
            out['tp1_filled_at'] = int(run['at']) if run['at'] is not None else None
            out['tp1_filled_text'] = clock_text(run['at']) if run['at'] is not None else 'time unknown'
            break
    filled = number(leg.get('filled_qty')) or 0
    out.update(order_id=str(leg.get('broker_order_id')) if leg.get('broker_order_id') is not None else None,
               status=leg.get('status') if isinstance(leg.get('status'), str) else None,
               quantity=int(qty - filled) if qty is not None else None, price=price, price_text=price_text(price),
               evidence_at=int(snap['observed_at']) if number(snap.get('observed_at')) is not None else None)
    position = number(snap.get('position_qty'))
    out['open'] = (live_net != 0) if live_net is not None else (position > 0 if position is not None else None)
    working = out['status'] in ('working', 'partial')
    if working and live_ids is not None and out['order_id'] not in live_ids:
        out['problem'] = 'bridge_state_shows_stop_not_working'
        out['resting'] = None
    else:
        out['resting'] = working
    if price is not None:
        out['kind'], out['kind_source'] = stop_kind(price, trade_id, entry, modifies, context)
    if runs[-1]['at'] is not None:
        out['confirmed_at'] = int(runs[-1]['at'])
        out['confirmed_text'] = clock_text(runs[-1]['at'])
    for run in reversed(runs):
        q, p = run['key']
        if p is not None and stop_kind(p, trade_id, entry, modifies, context)[0] == 'break_even':
            out['break_even'] = {'quantity': int(q) if q is not None else None, 'price': p,
                                 'price_text': price_text(p),
                                 'confirmed_at': int(run['at']) if run['at'] is not None else None,
                                 'confirmed_text': clock_text(run['at'])}
            break
    return out


def stop_view(stop):
    """Pill, tone and rows for the Resting stop tile (the page escapes every value)."""
    if not stop:
        return {'pill': 'No open trade', 'tone': '', 'summary': 'No open native trade',
                'rows': [['Stop', 'none']]}
    problem = stop.get('problem')
    if isinstance(problem, str) and problem.startswith('error:'):
        return {'pill': 'Unknown', 'tone': 'amber', 'summary': 'Resting stop unknown (read error)',
                'rows': [['Stop', 'unknown'], ['Problem', problem]]}
    trade = stop.get('trade_id')
    if stop.get('open') is False and stop.get('resting') is True:
        return {'pill': 'Stop still working', 'tone': 'coral',
                'summary': 'Position flat but the stop is still working (%s @ %s)' % (stop.get('quantity'), stop.get('price_text')),
                'rows': [['Stop', '%s @ %s' % (stop.get('quantity'), stop.get('price_text'))],
                         ['Position', 'flat'], ['Order', stop.get('order_id')], ['Trade', trade]]}
    if stop.get('open') is False:
        return {'pill': 'No open trade', 'tone': '', 'summary': 'No open trade',
                'rows': [['Stop', 'none'], ['Last trade', trade]]}
    if stop.get('resting') is True and stop.get('quantity') is not None and stop.get('price') is not None:
        label = STOP_KIND_LABELS.get(stop.get('kind'), 'type unknown')
        confirmed = ('confirmed by Tradovate ' + stop['confirmed_text']) if stop.get('confirmed_text') \
            else 'confirmed by Tradovate (time unknown)'
        pill = {'initial': 'Initial stop', 'break_even': 'Break-even', 'manual': 'Moved by hand'}.get(stop.get('kind'),
                                                                                                      'Type unknown')
        tone = {'initial': 'teal', 'break_even': 'mint', 'manual': 'teal'}.get(stop.get('kind'), 'amber')
        return {'pill': pill, 'tone': tone,
                'summary': '%d @ %s · %s · %s' % (stop['quantity'], stop['price_text'], label, confirmed),
                'rows': [['Stop', '%d @ %s' % (stop['quantity'], stop['price_text'])], ['Type', label],
                         ['Confirmed by Tradovate', stop.get('confirmed_text') or 'time unknown'],
                         ['Order', stop.get('order_id')], ['Trade', trade]]}
    if stop.get('resting') is False:
        return {'pill': 'No resting stop', 'tone': 'coral',
                'summary': 'No resting stop (last status: %s)' % (stop.get('status') or 'unknown'),
                'rows': [['Stop', 'none resting'], ['Last status', stop.get('status') or 'unknown'],
                         ['Order', stop.get('order_id')], ['Trade', trade]]}
    return {'pill': 'Unknown', 'tone': 'amber', 'summary': 'Resting stop unknown',
            'rows': [['Stop', 'unknown'], ['Reason', stop.get('problem') or 'no confirmation'], ['Trade', trade]]}


# --- 1.3: accounts, followers and alerts -----------------------------------------------------
# Everything below reads files the bridge, the firing service and Soup already write, plus the
# daemon's /followers reply. Views carry plain words for the page; the page escapes every value.

def short_label(label, fallback):
    """('Account C', 'demo') from 'Account C (demo)'."""
    text = label if isinstance(label, str) and label.strip() else fallback
    if '(' in text and text.rstrip().endswith(')'):
        name, _, rest = text.partition('(')
        return name.strip(), rest.rstrip()[:-1].strip() or None
    return text.strip(), None


def short_name(name):
    """'7' from 'Account 7', 'B' from 'Account B'; the name itself when it does not end in a number
    or a single letter."""
    last = name.split()[-1] if name.split() else name
    return last if last.isdigit() or (len(last) == 1 and last.isalpha() and len(name.split()) > 1) else name


def position_text(net):
    n = number(net)
    if n is None:
        return 'unknown'
    if n == 0:
        return 'Flat'
    return ('Long %g' if n > 0 else 'Short %g') % abs(n)


ORDER_WORDS = {'STP': 'stop', 'LMT': 'limit', 'MKT': 'market', 'STL': 'stop-limit'}


def orders_text(orders, count=None):
    """'stop 2 · TP1 limit 1' from /followers order rows; a count when only ids are known."""
    if isinstance(orders, list):
        if not orders:
            return 'none'
        parts = []
        for o in orders:
            if not isinstance(o, dict):
                continue
            word = 'TP1 limit' if o.get('tp1') is True else ORDER_WORDS.get(o.get('type'), 'order')
            qty = number(o.get('qty'))
            parts.append(word + ('' if qty is None else ' %g' % qty))
        return ' · '.join(parts) or 'none'
    if isinstance(count, int):
        return 'none' if count == 0 else '%d working' % count
    return 'unknown'


def time_text(ms):
    """'19:38:06 UTC' (no milliseconds) for alert rows."""
    full = clock_text(ms)
    return None if full is None else full[:8] + full[12:]


ADMIN_MARK = 'tvbridge-admin'   # who wrote the switch file; same mark as Bridge Advanced


def config_problem(cfg):
    """A config.json value the daemon refuses to start with, as a sentence (no final period), or None.
    Main login only.

    `max_contracts` is retired: the strategy alert sets the size. An absent key or a legacy positive whole
    number (any size) is fine and ignored; any other value is a malformed legacy value the daemon refuses."""
    if not isinstance(cfg, dict) or not cfg or cfg.get('mode') == 'followers_only':
        return None
    legacy = cfg.get('max_contracts')
    if legacy is not None and not (type(legacy) is int and legacy >= 1):
        return ('max_contracts is retired (the strategy alert sets the size); remove it from the config '
                '(found %s)' % json.dumps(legacy))
    return None


SWITCH_REASONS = {
    'kill_switch_not_configured': 'No pause switch file is configured, so entries stay paused.',
    'kill_switch_folder_missing': 'The pause switch folder is missing, so entries stay paused.',
    'kill_switch_folder_unusable': 'The pause switch folder is not usable, so entries stay paused.',
}


def switch_reason_text(code):
    """A sentence for the daemon's `entries_disabled_reason` (tvbridge.executor.kill_switch_state)."""
    if not isinstance(code, str) or not code:
        return None
    whose = ''
    for prefix, text in (('master_', "The leader login's "), ('main_', "The main account's ")):
        if code.startswith(prefix):
            code, whose = code[len(prefix):], text
    if code == 'kill_switch_on':
        return (whose or 'This account\'s ') + 'pause switch is on.'
    if code.startswith('kill_switch_unreadable'):
        text = 'The pause switch cannot be checked (%s), so entries stay paused.' % code.partition(':')[2]
    else:
        text = SWITCH_REASONS.get(code, 'Entries are paused (%s).' % code)
    return (whose + text[4:]) if whose and text.startswith('The ') else text


def switch_fault(reason):
    """A pause the bridge applies because its switch is missing or cannot be checked (fails closed)."""
    return {'on': True, 'by': 'safety', 'label': 'Paused by safety', 'reason': reason, 'at': None, 'time_text': None,
            'fault': True}


def account_pause(path, entries_disabled=None, reason_code=None):
    """An account's pause as the bridge applies it (fails closed). `path` is the configured switch: none
    configured means paused. The daemon's own answer (`entries_disabled`, `entries_disabled_reason` from
    /health or a /followers row) also counts, so a pause the file alone does not show is never missed."""
    if not isinstance(path, str) or not path or not os.path.isabs(os.path.expanduser(path)):
        return switch_fault(SWITCH_REASONS['kill_switch_not_configured'])
    pause = pause_info(path)
    if not pause['on']:
        folder = Path(os.path.expanduser(path)).parent
        if not folder.is_dir():
            return switch_fault(SWITCH_REASONS['kill_switch_folder_missing'])
    if not pause['on'] and entries_disabled is True:
        out = switch_fault(switch_reason_text(reason_code) or 'The bridge reports entries paused.')
        out['fault'] = not str(reason_code or '').endswith('kill_switch_on')   # a plain pause is not a fault
        return out
    return pause


def pause_info(path):
    """An account's pause (kill-switch file), read the way Bridge Advanced reads it. The bridge only checks that
    the file exists; its content says who set it. Bridge Advanced writes {"set_by": "tvbridge-admin", "reason",
    "at"}; anything else (an empty file from a safety event, or one made by hand) counts as a safety pause."""
    if not path:
        return {'on': False, 'by': None, 'label': None, 'reason': None, 'at': None, 'time_text': None}
    p = Path(os.path.expanduser(str(path)))
    try:
        os.lstat(p)   # the bridge pauses on anything at the path, a broken link included
    except FileNotFoundError:
        return {'on': False, 'by': None, 'label': None, 'reason': None, 'at': None, 'time_text': None}
    except OSError as exc:
        return switch_fault('The pause switch cannot be checked (%s), so entries stay paused.' % type(exc).__name__)
    if p.is_symlink() and not p.exists():
        return switch_fault('The pause switch is a broken link; the bridge counts it as paused.')
    try:
        note = json.loads(p.read_text() or 'null')
    except (OSError, ValueError):
        note = None
    if isinstance(note, dict) and note.get('set_by') == ADMIN_MARK:
        at = number(note.get('at'))
        reason = note.get('reason') if isinstance(note.get('reason'), str) and note.get('reason').strip() else None
        return {'on': True, 'by': 'admin', 'label': 'Paused', 'reason': reason, 'at': at,
                'time_text': time_text(int(at * 1000)) if at is not None else None}
    try:
        at = p.stat().st_mtime
    except OSError:
        at = None
    return {'on': True, 'by': 'safety', 'label': 'Paused by safety',
            'reason': 'Set by a safety event or by hand (not by Bridge Advanced)', 'at': at,
            'time_text': time_text(int(at * 1000)) if at is not None else None}


def pause_status(pause):
    """Card status for a paused account (same label as Bridge Advanced)."""
    head = 'Paused' + (' from Bridge Advanced' if pause['by'] == 'admin' else ' by a safety event or by hand')
    if pause['by'] == 'admin' and pause.get('reason'):
        head += ': ' + pause['reason']
    return {'state': 'paused', 'label': pause['label'], 'tone': 'amber' if pause['by'] == 'admin' else 'coral',
            'reason': head + '. Exits still work; new entries are refused.'}


def unit_state(name):
    """systemd state of one user unit, with LoadState so a missing unit reads 'not installed'."""
    try:
        r = subprocess.run(['systemctl', '--user', 'show', name, '-p', 'LoadState', '-p', 'ActiveState',
                            '-p', 'SubState', '-p', 'Result', '-p', 'UnitFileState'], capture_output=True, text=True,
                           timeout=5)
        props = dict(line.split('=', 1) for line in r.stdout.splitlines() if '=' in line)
    except Exception:
        props = {}
    return {'load': props.get('LoadState', 'unknown'), 'active': props.get('ActiveState', 'unknown'),
            'sub': props.get('SubState', ''), 'result': props.get('Result', ''), 'file': props.get('UnitFileState', '')}


def firing_paths():
    """(firing.json content or None, firing directory). The directory defaults to ROOT/firing."""
    fcfg = read_json(ROOT / 'firing.json')
    fcfg = fcfg if isinstance(fcfg, dict) else None
    d = (fcfg or {}).get('firing_dir')
    return fcfg, Path(d) if isinstance(d, str) and d else ROOT / 'firing'


def leader_key(fcfg=None, s=None):
    """The main account's key: firing.json's account with role "main", else the config label as a key
    ("Account A" -> "account-a"), else 'main'."""
    if fcfg is None:
        fcfg = firing_paths()[0]
    accounts = (fcfg or {}).get('accounts')
    if isinstance(accounts, dict):
        for key, acct in accounts.items():
            if isinstance(acct, dict) and acct.get('role') == 'main' and isinstance(key, str):
                return key
    label = ((s or {}).get('account') or {}).get('label')
    if isinstance(label, str):
        slug = re.sub(r'[^a-z0-9]+', '-', label.strip().lower()).strip('-')
        if re.fullmatch(r'[a-z0-9][a-z0-9-]{0,31}', slug):
            return slug
    return 'main'


def webhook_line(fcfg):
    """One quiet display line for the Trade section, e.g. 'Webhook Main · Soup Pine r4 ·
    leader Account 1 · followers Account 2, Account 3'. The leader and
    follower labels come only from firing.json's `firing_order` and `accounts` (the same
    file `firing_paths` already reads for the follower cards): the account with role
    'main' is the leader, every other account in firing order is a follower. The strategy
    name is optional and comes only from WEBHOOK_REGISTRY, a small file this panel never
    writes (`{"webhooks": [{"id": "main", "strategy": "soup-pine-r4", ...}]}`). Any problem
    reading either file, or a firing.json that is missing or not shaped as expected, leaves
    this None: it must never break the page."""
    try:
        accounts = (fcfg or {}).get('accounts')
        order = (fcfg or {}).get('firing_order')
        if not isinstance(accounts, dict) or not isinstance(order, list):
            return None

        def label(key):
            acc = accounts.get(key)
            lbl = acc.get('label') if isinstance(acc, dict) else None
            return lbl if isinstance(lbl, str) and lbl.strip() else None

        leader_key = next((k for k in order if isinstance(accounts.get(k), dict)
                           and accounts[k].get('role') == 'main'), None)
        leader = label(leader_key) if leader_key is not None else None
        followers = [l for l in (label(k) for k in order if k != leader_key) if l]
        if not leader or not followers:
            return None
        strategy = None
        registry = read_json(WEBHOOK_REGISTRY)
        if isinstance(registry, dict) and isinstance(registry.get('webhooks'), list):
            main = next((w for w in registry['webhooks']
                        if isinstance(w, dict) and w.get('id') == 'main'), None)
            strat_id = (main or {}).get('strategy')
            if isinstance(strat_id, str) and strat_id.strip():
                strategy = STRATEGY_LABELS.get(strat_id, strat_id)
        return {'name': 'Webhook Main', 'strategy': strategy, 'leader': leader, 'followers': followers}
    except Exception:
        return None


def alert_view(a, labels):
    """Plain words for one firing alert. `info` alerts (follower_entry_skipped) are calm notes."""
    kind = str(a.get('kind') or 'alert')
    reason = a.get('reason') or a.get('code')
    reason = str(reason) if reason not in (None, '') else None
    info = a.get('level') == 'info'
    tone, note = 'amber', None
    if kind == 'follower_entry_skipped':
        text, tone = 'Skipped a trade: the entry was refused and nothing filled', 'info'
        note = 'No action needed. This is expected while the account is not open for trading yet.'
    elif kind == 'entry_not_fired':
        text = 'Entry not sent' + (' (%s)' % reason if reason else '')
    elif kind == 'order_rejected':
        text = 'Order refused' + (' (%s)' % reason if reason else '')
    elif kind == 'order_unknown':
        text, tone = 'Order outcome unknown: its entries stay blocked until it is reconciled', 'coral'
    elif kind == 'native_exit':
        if reason == 'leader_closed_externally':
            text = 'Closed with the leader: the leader was closed outside the bridge'
        elif reason == 'position_mismatch':
            text = 'Closed: its position changed outside the bridge'
        else:
            text = 'Exited early' + (' (%s)' % reason if reason else '')
    elif kind == 'exit_all_unverified':
        text, tone = 'Exit not verified: its entries are blocked', 'coral'
    elif kind == 'native_fallback':
        text = 'Stop not confirmed at Tradovate; the bridge watches the stop price instead' + (
            ' (%s)' % reason if reason else '')
    elif kind == 'native_superseded':
        text = 'Trade replaced by a newer trade'
    elif kind == 'follower_unavailable':
        text, tone = 'Not linked' + (' (%s)' % reason if reason else ''), 'coral'
    elif kind == 'lane_stopped':
        text, tone = 'Follower lane stopped', 'coral'
    elif kind == 'foreign_position':
        text = 'Holds a position the bridge did not open'
    elif kind == 'error':
        text, tone = 'Error' + (': ' + str(a.get('error'))[:160] if a.get('error') else ''), 'coral'
    else:
        text = kind.replace('_', ' ') + (' (%s)' % reason if reason else '')
    if info:
        tone = 'info'
    account = a.get('account')
    return {'account': account, 'account_label': labels.get(account, str(account or '')), 'kind': kind,
            'level': 'info' if info else 'alert', 'text': text, 'note': note, 'tone': tone,
            'at': int(a['at']) if number(a.get('at')) is not None else None, 'time_text': time_text(a.get('at')),
            'trade_id': a.get('trade_id') if isinstance(a.get('trade_id'), str) else None,
            'command_id': a.get('command_id') if isinstance(a.get('command_id'), str) else None,
            # 1.4: filled in by resolve_alerts. `live` is true only for an unresolved problem.
            'resolved': None, 'live': not info}


def read_alerts(firing_dir):
    """Firing alerts, oldest first, from alerts.jsonl (append-only)."""
    recs, _ = tail_records(Path(firing_dir) / 'alerts.jsonl', ALERTS_TAIL_BYTES)
    return [r for r in recs if r.get('type', 'alert') == 'alert' and isinstance(r.get('kind'), str)]


# --- 1.4: balances -------------------------------------------------------------------------------

def money_text(amount, sign=False):
    """'$1,950.00'; a real minus for losses ('−$50.00'); with sign=True also '+$12.50'."""
    n = number(amount)
    if n is None:
        return None
    text = '$' + format(abs(n), ',.2f')
    return '−' + text if n < 0 else ('+' + text if sign and n > 0 else text)


def balance_view(state, amount=None, as_of_ms=None, day_pnl=None, source=None, reason=None, checked_at=None):
    """One card's balance. Only state 'ok' carries numbers: stale or missing reads 'unavailable'."""
    ok = state == 'ok'
    return {'state': state, 'amount': amount if ok else None, 'text': money_text(amount) if ok else 'unavailable',
            'as_of': as_of_ms if ok else None, 'as_of_text': time_text(as_of_ms) if ok else None,
            'day_pnl': day_pnl if ok else None, 'day_pnl_text': money_text(day_pnl, sign=True) if ok else None,
            'source': source, 'reason': reason, 'checked_at': checked_at}


def cash_balance_view(cash, reply_ok, fetched_at, now, source):
    """Balance from a tvbridge cash row ({amount, timestamp, realized_pnl, ...}: Tradovate's own values).
    `reply_ok`: the reply was synced and complete. `fetched_at`: when the daemon answered (epoch s)."""
    if fetched_at is not None and now - fetched_at > BALANCE_MAX_AGE_SECONDS:
        return balance_view('stale', source=source, reason='last read %s' % ago(now - fetched_at),
                            checked_at=fetched_at)
    if not reply_ok or not isinstance(cash, dict) or number(cash.get('amount')) is None:
        return balance_view('unavailable', source=source, checked_at=fetched_at,
                            reason='the bridge has no complete balance for this account right now')
    ts = parse_ts(cash.get('timestamp'))
    return balance_view('ok', number(cash['amount']), int(ts * 1000) if ts else None,
                        number(cash.get('realized_pnl')), source, checked_at=fetched_at)


def account_reply(cfg, port, now):
    """The daemon's GET /account reply for the pinned account: (reply, fetched_at, error).

    Asked only when config.json opts in (`account_view_enabled`, the same switch the daemon
    uses for the route) and at most every BALANCE_CACHE_SECONDS; the reply is the account data
    the daemon already holds in memory, no Tradovate request. A failed refresh keeps the last
    reply; its age decides whether it is still shown.
    """
    if cfg.get('account_view_enabled') is not True:
        return None, None, 'account_view_disabled'
    with BALANCE_LOCK:
        c = BALANCE_CACHE
        if c.get('reply') is None or now - c.get('at', 0) >= BALANCE_CACHE_SECONDS:
            reply, err = bridge('account', port)
            if isinstance(reply, dict) and reply.get('schema') == 'tvbridge.account.v1':
                c.update(at=now, reply=reply, error=None)
            else:
                c['error'] = err or 'invalid_reply'
        return c.get('reply'), c.get('at') if c.get('reply') else None, c.get('error')


def leader_balance(cfg, port, now):
    """The main account's balance from the daemon's /account. When it is not usable the card reads stale or
    unavailable with the reason."""
    reply, fetched_at, err = account_reply(cfg, port, now)
    if isinstance(reply, dict):
        data = reply.get('account_data') if isinstance(reply.get('account_data'), dict) else {}
        ok = reply.get('synced') is True and data.get('complete') is True
        v = cash_balance_view(data.get('cash_balance'), ok, fetched_at, now, 'bridge /account')
        return dict(v, reason='bridge /account: ' + v['reason']) if v['reason'] else v
    why = 'not asked (%s)' % err if err == 'account_view_disabled' else 'no answer (%s)' % (err or 'invalid reply')
    return balance_view('unavailable', source='bridge /account', reason='bridge /account: ' + why)


def follower_balance(row, observed_at, now):
    """A follower's balance from its /followers row. The 1.0.9.1 daemon reports no `cash_balance`
    there (the row builder in followers.py has no account data), so this reads unavailable until
    the daemon adds that read-only field; the shape expected is /account's `cash_balance`."""
    if not isinstance(row, dict):
        return balance_view('unavailable', source='bridge /followers', reason='the bridge did not report this account')
    if row.get('available') is not True:
        return balance_view('unavailable', source='bridge /followers', reason='account not linked')
    if 'cash_balance' not in row:
        return balance_view('unavailable', source='bridge /followers',
                            reason='the bridge does not report follower balances yet')
    fetched = observed_at / 1000 if number(observed_at) is not None else now
    return cash_balance_view(row.get('cash_balance'), row.get('synced') is True, fetched, now, 'bridge /followers')


# --- 1.4: resolved alerts --------------------------------------------------------------------------

def journal_outcome(journal_dir, key, command_id):
    """The last bridge-journal record that settled one follower command, or None.

    The firing service names commands without the lane prefix; the follower's bridge journal
    (config.json followers[].journal_dir, commands.jsonl) records them as `f:<key>:<id>`. A
    `reconciled` record is written by hand (tvbridge.reconcile) and carries the reviewer's note.
    """
    if not isinstance(command_id, str) or not isinstance(journal_dir, str) or not journal_dir:
        return None
    wanted = {command_id, 'f:%s:%s' % (key, command_id)}
    recs, _ = tail_records(Path(journal_dir) / 'commands.jsonl', JOURNAL_TAIL_BYTES, command_id.encode())
    last = None
    for r in recs:
        if r.get('command_id') in wanted and r.get('type') in ('result', 'reconciled'):
            last = r
    if last is None:
        return None
    receipt = last.get('receipt') if isinstance(last.get('receipt'), dict) else {}
    return {'type': last['type'], 'outcome': last.get('outcome'),
            'note': receipt.get('note') if isinstance(receipt.get('note'), str) else None,
            'at': number(last.get('at'))}


def resolution(how, at=None, note=None):
    at = int(at) if number(at) is not None else None
    return {'how': how, 'at': at, 'time_text': time_text(at), 'note': note}


def resolve_alerts(alerts, key, journal_dir, lane_recs, row, blocked):
    """Marks one follower's alerts resolved when read-only evidence shows the condition ended.

    - order_unknown: the follower's bridge journal settled the command (`reconciled` by hand,
      or a later definite `result`); failing that, the daemon's /followers row says
      unresolved_commands == 0 and the lane is not blocked (time then unknown).
    - other blocking kinds (exit not verified, lane stopped, not linked, error): the lane is
      not blocked now and a newer trade started on it after the alert.
    - every other kind (notes and one-trade events): that trade finished, or a newer trade
      started after it.
    Unresolved problems keep `live` true and their tone; resolved ones lose the tone.
    """
    starts = sorted((number(r.get('at')), r['trade_id']) for r in lane_recs
                    if r.get('type') == 'native' and r.get('event') == 'start'
                    and isinstance(r.get('trade_id'), str) and number(r.get('at')) is not None)
    ends = [(number(r.get('at')), r.get('trade_id'), r.get('result')) for r in lane_recs
            if number(r.get('at')) is not None and isinstance(r.get('trade_id'), str)
            and ((r.get('type') == 'native' and r.get('event') == 'done')
                 or (r.get('type') == 'reconcile' and r.get('final') is True))]
    unresolved_now = row.get('unresolved_commands') if isinstance(row, dict) and row.get('available') is True \
        else None
    for a in alerts:
        at, kind, res = a.get('at'), a.get('kind'), None
        if kind == 'order_unknown':
            j = journal_outcome(journal_dir, key, a.get('command_id'))
            if j and j['type'] == 'reconciled':
                res = resolution('reconciled by hand', j['at'], j['note'])
            elif j and j['type'] == 'result' and j['outcome'] not in (None, 'unknown'):
                res = resolution('outcome later known: %s' % j['outcome'], j['at'])
            elif unresolved_now == 0 and not blocked:
                res = resolution('the bridge reports no unresolved command')
        else:
            later = [s for s in starts if at is not None and s[0] > at]
            if kind in BLOCKING_KINDS:
                if not blocked and later:
                    res = resolution('a newer trade ran after the reviewed reset', later[0][0])
            elif kind == 'follower_entry_skipped' or a.get('level') == 'info':
                # A skip's lane writes `done` right away; the note stays until the next trade.
                if later:
                    res = resolution('a newer trade started', later[0][0])
            else:
                done = [e for e in ends if e[1] == a.get('trade_id') and at is not None and e[0] >= at]
                if done:
                    res = resolution('the trade finished (%s)' % (done[0][2] or 'done'), done[0][0])
                elif later:
                    res = resolution('a newer trade started', later[0][0])
        if res is not None:
            a['resolved'], a['live'], a['problem_tone'], a['tone'] = res, False, a['tone'], ''
    return alerts


def lane_records(path):
    """The lane journal's `native` and `reconcile` records (the latter carry "native": true too)."""
    recs, _ = tail_records(path, LANE_TAIL_BYTES, b'"native"')
    return [r for r in recs if r.get('type') in ('native', 'reconcile') and isinstance(r.get('trade_id'), str)]


def lane_stop(path, row, recs=None):
    """Resting stop of one follower's latest native trade, from its lane journal (`native` records
    the firing service writes after it proved the stop from the follower's own account state).
    `row` is the follower's /followers row, used to cross-check that a stop is working now."""
    recs = lane_records(path) if recs is None else recs
    recs = [r for r in recs if r.get('type') == 'native']
    starts = [r for r in recs if r.get('event') == 'start']
    if not starts:
        return {'state': 'none', 'line': 'none', 'when': None, 'tone': ''}
    tid = starts[-1]['trade_id']
    plan = starts[-1].get('plan') if isinstance(starts[-1].get('plan'), dict) else {}
    initial, be = number(plan.get('initial')), number(plan.get('break_even'))
    cur = pending = None
    fallback = exit_reason = skipped = failed = None
    done = False
    for r in recs[recs.index(starts[-1]):]:
        if r.get('trade_id') != tid:
            continue
        ev = r.get('event')
        if ev == 'stop_confirmed':
            cur = {'qty': number(r.get('quantity')), 'price': number(r.get('price')), 'at': r.get('at')}
        elif ev == 'modify':
            pending = {'qty': number(r.get('quantity')), 'price': number(r.get('price')), 'at': r.get('at')}
        elif ev == 'modify_confirmed' and pending:
            cur, pending = dict(pending, at=r.get('at')), None
        elif ev == 'stop_cancel':
            pending = None
        elif ev == 'fallback':
            fallback = r.get('code') or 'fallback'
        elif ev == 'exit':
            exit_reason = r.get('reason') or 'exit'
        elif ev == 'skipped':
            skipped = r.get('reason') or 'entry_skipped'
        elif ev == 'failed':
            failed = r.get('reason') or 'failed'
        elif ev == 'done':
            done = True
    out = {'trade_id': tid, 'state': None, 'line': None, 'when': None, 'tone': ''}
    if skipped:
        return dict(out, state='skipped', line='none: skipped this trade', tone='info')
    if failed:
        return dict(out, state='failed', line='exit not verified', when=str(failed), tone='coral')
    if done:
        return dict(out, state='done', line='none', when='last trade finished')
    if exit_reason:
        return dict(out, state='closing', line='closing the trade', when=str(exit_reason), tone='amber')
    if fallback:
        return dict(out, state='synthetic', line='no resting stop: the bridge watches the stop price',
                    when=str(fallback), tone='amber')
    if cur is None or cur['price'] is None:
        return dict(out, state='waiting', line='waiting for Tradovate to confirm the stop', tone='amber')

    def kind_of(price):
        if same_price(price, be) and not same_price(price, initial):
            return 'break_even'
        if same_price(price, initial):
            return 'initial'
        return 'unknown'

    kind = kind_of(cur['price'])
    qty = '%g' % cur['qty'] if cur['qty'] is not None else '?'
    line = '%s @ %s · %s' % (qty, price_text(cur['price']), STOP_KIND_LABELS[kind])
    when = 'confirmed by Tradovate ' + (clock_text(cur['at']) or '(time unknown)')
    tone = {'initial': 'teal', 'break_even': 'mint'}.get(kind, 'amber')
    if pending and pending['price'] is not None:
        when = 'moving to %s @ %s, waiting for Tradovate' % (
            STOP_KIND_LABELS[kind_of(pending['price'])], price_text(pending['price']))
        tone = 'amber'
    orders = (row or {}).get('orders') if isinstance(row, dict) else None
    if isinstance(orders, list) and (row or {}).get('synced') is not False and \
            not any(isinstance(o, dict) and o.get('type') in ('STP', 'STL') for o in orders):
        when, tone = 'not in its working orders right now', 'amber'
    return dict(out, state='resting', kind=kind, line=line, when=when, tone=tone,
                value='%s @ %s' % (qty, price_text(cur['price'])), kind_label=STOP_KIND_LABELS[kind],
                quantity=cur['qty'], price=cur['price'], price_text=price_text(cur['price']),
                confirmed_at=int(cur['at']) if number(cur['at']) is not None else None)


# --- 2.0: lifecycle (derived only; every time comes from a record the 1.4 panel already read) --------

STEPS = (('entry', 'Entry'), ('stop', 'Stop confirmed'), ('tp1', 'TP1'), ('break_even', 'Break-even'),
         ('runners', 'Runners'), ('flat', 'Flat'))
STAGE_LABELS = {'idle': 'No trade', 'entered': 'Entered, stop not confirmed', 'stop_wait': 'Waiting for the stop',
                'tp1_wait': 'Stop confirmed, waiting for TP1', 'be_wait': 'TP1 filled, moving the stop',
                'runners': 'Runners on, stop at break-even', 'closing': 'Closing', 'flat': 'Flat, last trade done',
                'skipped': 'Skipped this trade', 'failed': 'Exit not verified', 'unknown': 'Unknown'}


def steps(**state):
    """The six lifecycle steps with a state each: done, now, pending, none, skipped, failed, warn, unknown."""
    out = []
    for key, label in STEPS:
        v = state.get(key) or ('none', None)
        out.append({'key': key, 'label': label, 'state': v[0], 'time_text': v[1]})
    return out


def lifecycle(stage, step_states, note=None):
    return {'stage': stage, 'label': STAGE_LABELS.get(stage, stage), 'steps': steps(**step_states), 'note': note}


def leader_lifecycle(stop, net, orders):
    """The main account's place in the trade, from the bridge journal (`stop` here is resting_stop()'s
    dict), /state's net position and the working orders the daemon reports."""
    if not isinstance(stop, dict) or stop.get('trade_id') is None:
        return lifecycle('unknown' if isinstance(stop, dict) else 'idle', {})
    entry = ('done', stop.get('entry_text'))
    be = stop.get('break_even') if isinstance(stop.get('break_even'), dict) else None
    tp1_at = stop.get('tp1_filled_text')
    n = number(net)
    if stop.get('open') is False or (n == 0 and stop.get('open') is None):
        st = {'entry': entry, 'stop': ('done', stop.get('first_confirmed_text') or stop.get('confirmed_text'))
              if stop.get('confirmed_at') else ('unknown', None),
              'tp1': ('done', tp1_at) if tp1_at else ('none', None),
              'break_even': ('done', be.get('confirmed_text')) if be else ('none', None),
              'runners': ('done', None) if be else ('none', None), 'flat': ('done', None)}
        return lifecycle('flat', st)
    if stop.get('open') is None:
        return lifecycle('unknown', {'entry': entry})
    st = {'entry': entry}
    if stop.get('resting') is True and stop.get('confirmed_at'):
        st['stop'] = ('done', stop.get('first_confirmed_text') or stop.get('confirmed_text'))
    elif stop.get('resting') is True:
        st['stop'] = ('done', None)
    elif stop.get('resting') is False:
        st['stop'] = ('failed', None)
    elif stop.get('problem'):
        st['stop'] = ('warn', None)
    else:
        st['stop'] = ('now', None)
    tp1_working = isinstance(orders, list) and any(isinstance(o, dict) and o.get('tp1') is True for o in orders)
    if tp1_at:
        st['tp1'] = ('done', tp1_at)
    elif stop.get('kind') == 'break_even':
        st['tp1'] = ('done', None)
    else:
        st['tp1'] = ('now', None) if (st['stop'][0] == 'done' and tp1_working) else ('pending', None)
    if stop.get('kind') == 'break_even':
        st['break_even'] = ('done', stop.get('confirmed_text'))
        st['runners'] = ('now', None)
        stage = 'runners'
    else:
        st['break_even'] = ('now', None) if st['tp1'][0] == 'done' else ('pending', None)
        st['runners'] = ('pending', None)
        stage = 'be_wait' if st['tp1'][0] == 'done' else 'tp1_wait' if st['stop'][0] == 'done' else \
            'stop_wait' if st['stop'][0] == 'now' else 'entered'
    st['flat'] = ('pending', None)
    return lifecycle(stage, st)


def lane_lifecycle(recs, leader_trade_id=None, row=None):
    """A follower's place in its latest native trade, from the lane journal records lane_stop reads.

    The lane's `tp1` record is the TP1 limit being placed, not filled: TP1 counts as done only
    when the daemon's /followers row shows the position below the entry size, or after the
    break-even move (which follows the fill). Its fill time is not recorded, so none is shown."""
    recs = [r for r in recs if r.get('type') == 'native']
    starts = [r for r in recs if r.get('event') == 'start']
    if not starts:
        return lifecycle('idle', {})
    tid = starts[-1]['trade_id']
    entry_qty = number(starts[-1].get('quantity'))
    net = number(row.get('net_position')) if isinstance(row, dict) and row.get('available') is True else None
    ev = {}
    pending_be = False
    for r in recs[recs.index(starts[-1]):]:
        if r.get('trade_id') != tid:
            continue
        e = r.get('event')
        if e == 'modify':
            pending_be = r.get('why') == 'break_even'
            continue
        if e == 'modify_confirmed':
            if pending_be:
                ev['break_even'] = r
            pending_be = False
            continue
        ev.setdefault(e, r)
    t = lambda e: clock_text((ev.get(e) or {}).get('at'))
    if 'skipped' in ev:
        return lifecycle('skipped', {'entry': ('skipped', t('skipped'))},
                         note=None if tid == leader_trade_id or leader_trade_id is None else 'earlier trade')
    st = {'entry': ('done', t('start'))}
    st['stop'] = ('done', t('stop_confirmed')) if 'stop_confirmed' in ev else \
        ('warn', None) if 'fallback' in ev else ('now', None)
    closed = 'done' in ev or 'exit' in ev or 'failed' in ev
    st['break_even'] = ('done', t('break_even')) if 'break_even' in ev else \
        ('now', None) if pending_be else ('pending', None)
    shrank = not closed and entry_qty is not None and net is not None and 0 < abs(net) < entry_qty
    tp1_working = isinstance(row, dict) and any(isinstance(o, dict) and o.get('tp1') is True
                                                 for o in (row.get('orders') or []) if isinstance(row.get('orders'), list))
    st['tp1'] = ('done', None) if (st['break_even'][0] != 'pending' or shrank) else \
        ('now', None) if (not closed and ('tp1' in ev or tp1_working)) else ('pending', None)
    st['runners'] = ('done', None) if (st['break_even'][0] == 'done' and closed) else \
        ('now', None) if st['break_even'][0] == 'done' else ('pending', None)
    if 'failed' in ev:
        st['flat'] = ('failed', t('failed'))
        stage = 'failed'
    elif 'done' in ev:
        st['flat'] = ('done', t('done'))
        stage = 'flat'
        for k in ('tp1', 'break_even', 'runners'):
            if st[k][0] in ('pending', 'now'):
                st[k] = ('none', None)
    elif 'exit' in ev:
        st['flat'] = ('now', t('exit'))
        stage = 'closing'
    else:
        st['flat'] = ('pending', None)
        stage = 'runners' if st['runners'][0] == 'now' else 'be_wait' if st['break_even'][0] == 'now' else \
            'tp1_wait' if st['stop'][0] == 'done' else 'stop_wait' if st['stop'][0] == 'now' else 'entered'
        if st['tp1'][0] == 'done' and st['break_even'][0] == 'pending':
            stage = 'be_wait'
    note = None if leader_trade_id is None or tid == leader_trade_id else 'earlier trade'
    return lifecycle(stage, st, note=note)


def checks(s, now=None):
    """Six yes/no facts for the top strip. `ok` is True, False or None (unknown).
    Empty before the bridge is set up: there is nothing to check yet, and 'Armed' must never show then."""
    if (s.get('verdict') or {}).get('tone') == 'off':
        return []
    now = time.time() if now is None else now
    h = (s.get('health') or {}).get('health') if isinstance(s.get('health'), dict) else None
    page = (h or {}).get('page') or {}
    g, st, soup = s.get('guard') or {}, s.get('state') if isinstance(s.get('state'), dict) else {}, s.get('soup') or {}
    login = guard_login(g, now)
    out = []

    def add(key, ok, yes, no, unknown, bad='bad'):
        # level: ok | bad (a fault, coral) | warn (deliberate or soft: kill switch, Soup held, stale feed) | unknown
        out.append({'key': key, 'ok': ok, 'level': 'ok' if ok is True else bad if ok is False else 'unknown',
                    'text': yes if ok is True else no if ok is False else unknown})
    add('bridge', None if h is None else bool(h.get('ok')), 'Bridge healthy', 'Bridge not ready', 'Bridge unreachable')
    add('login', True if login == 'in' else False if login == 'out' else (True if page.get('state') == 'authorized' else None),
        'Logged in', 'Logged out', 'Login unknown')
    add('sync', page.get('synced') if isinstance(page.get('synced'), bool) else None, 'Synced', 'Not synced', 'Sync unknown')
    if st.get('unresolved_commands'):
        add('armed', False, '', 'Entries blocked', '')
    else:
        add('armed', not s.get('kill_switch'), 'Armed', 'Paused', '', bad='warn')
    released = bool(soup.get('manual_enabled')) and not soup.get('locked')
    add('soup', None if soup.get('updated_at') is None else released, 'Soup released', 'Soup held', 'Soup status missing', bad='warn')
    add('feed', soup.get('quote_fresh') if isinstance(soup.get('quote_fresh'), bool) else None, 'Feed fresh', 'Feed stale', 'Feed unknown', bad='warn')
    return out


def trade_view(s):
    """The trade as one object: open or not, direction and size from the leader, and for each follower
    whether its lifecycle matches the leader's (same, behind, ahead, skipped, fault, not in, unknown)."""
    cards = (s.get('followers') or {}).get('cards') or []
    leader = cards[0] if cards and cards[0].get('role') == 'leader' else None
    stop = s.get('stop') if isinstance(s.get('stop'), dict) else {}
    st = s.get('state') if isinstance(s.get('state'), dict) else {}
    n = number((leader or {}).get('position', {}).get('net')) if leader else None
    open_ = (n is not None and n != 0)
    unknown = leader is not None and leader['position'].get('net') is None
    side = 'long' if (n or 0) > 0 else 'short' if (n or 0) < 0 else stop.get('side')
    out = {'open': open_, 'unknown': unknown, 'side': side if open_ else None,
           'size': abs(n) if open_ else None, 'text': (s.get('glance') or {}).get('trade', {}).get('text'),
           'contract': (st.get('contract') or {}).get('name') if isinstance(st.get('contract'), dict) else None,
           'entered_text': stop.get('entry_text') if open_ else None,
           'stage': (leader or {}).get('lifecycle', {}).get('label') if leader else None,
           'followers': []}
    if not leader:
        return out
    out['followers'] = follower_relations(leader, cards[1:], open_, n)
    return out


def follower_relations(leader, cards, open_, n):
    """Each follower card against the leader's trade: same, behind, ahead, differs, skipped, fault,
    not_in or unknown. Used for the primary login's followers and (multi-login) every other login's."""
    lead_done = {x['key'] for x in leader.get('lifecycle', {}).get('steps', []) if x['state'] == 'done'}
    out = []
    for c in cards:
        lc = c.get('lifecycle') or {}
        done = {x['key'] for x in lc.get('steps', []) if x['state'] == 'done'}
        stage = lc.get('stage')
        if c['status'].get('state') in ('blocked', 'not_linked', 'unknown') or stage == 'failed':
            rel, word = 'fault', c['status'].get('label', 'fault')
        elif not open_:
            rel, word = ('same', 'flat') if number(c['position'].get('net')) == 0 else \
                ('unknown', 'position unknown') if c['position'].get('net') is None else ('ahead', c['position']['text'].lower())
        elif stage == 'skipped' and lc.get('note') is None:
            rel, word = 'skipped', 'skipped this trade'
        elif lc.get('note') == 'earlier trade' or stage in ('idle', 'flat', 'skipped'):
            rel, word = 'not_in', 'not in this trade'
        elif number(c['position'].get('net')) != n:
            rel, word = 'differs', 'size differs: ' + c['position']['text'].lower()
        elif done == lead_done:
            rel, word = 'same', 'same as %s' % leader['short']
        elif done < lead_done:
            rel, word = 'behind', 'behind %s' % leader['short']
        else:
            rel, word = 'ahead', 'ahead of %s' % leader['short']
        out.append({'key': c['key'], 'short': c['short'], 'relation': rel, 'text': word})
    return out


def follower_card(f, row, rows_ok, fcfg, fstatus, fstatus_age, service, firing_dir, leader_kill, alerts,
                  observed_at=None, now=None, leader_trade_id=None):
    """One follower's card: status, kill switch, position, working orders, resting stop, balance,
    last alert. Marks this follower's alerts resolved in place (resolve_alerts)."""
    now = time.time() if now is None else now
    key = f['key']
    name, sub = short_label(f.get('label'), key)
    kill_file = f.get('kill_switch_file')
    pause = account_pause(kill_file, *((row.get('entries_disabled'), row.get('entries_disabled_reason'))
                                       if isinstance(row, dict) else ()))
    kill = pause['on']
    lane_dir = Path(firing_dir) / key
    lane = ((fstatus or {}).get('lanes') or {}).get(key) if isinstance(fstatus, dict) else None
    lane = lane if isinstance(lane, dict) else None
    native = (lane or {}).get('native') if isinstance((lane or {}).get('native'), dict) else {}
    trades = native.get('trades') if isinstance(native.get('trades'), dict) else {}
    blocked = (lane_dir / LANE_BLOCK_NAME).exists() or native.get('blocked') is True
    failed = [t for t, v in trades.items() if isinstance(v, dict) and v.get('failed')]
    acc = ((fcfg or {}).get('accounts') or {}).get(key) if isinstance(fcfg, dict) else None
    enabled = (acc or {}).get('enabled') if isinstance(acc, dict) else None
    installed = service.get('load') == 'loaded'
    active = service.get('active') == 'active'

    issues, calm = [], []   # issues: something is wrong; calm: reasons it is simply not trading
    state = None
    if not rows_ok:
        state, label, tone = 'unknown', 'Link unknown', 'amber'
        issues.append('the bridge did not report this account')
    elif row is None or row.get('available') is not True:
        state, label, tone = 'not_linked', 'Not linked', 'coral'
        issues.append('the bridge could not match this account in the Tradovate login'
                      + (' (%s)' % row.get('reason') if isinstance(row, dict) and row.get('reason') else ''))
    if blocked or failed:
        state, label, tone = 'blocked', 'Blocked', 'coral'
        issues.append('an exit was not verified; entries stay blocked until a reviewed reset')
    if rows_ok and isinstance(row, dict) and row.get('available') is True:
        if row.get('unresolved_commands'):
            issues.append('%s unresolved command(s): entries blocked until reconciled' % row['unresolved_commands'])
        if row.get('synced') is False:
            issues.append('not synced with Tradovate right now')
    if kill:
        calm.append('paused (entries off)')
    if leader_kill:
        calm.append("the leader's kill switch also stops follower entries")
    if not installed:
        calm.append('follower service not installed')
    elif not active:
        (issues if enabled else calm).append('follower service ' + (service.get('active') or 'stopped'))
    if enabled is False:
        calm.append('turned off in the follower settings')
    elif enabled is None and installed:
        calm.append('not in the follower settings')
    if active and enabled:
        if lane is None:
            issues.append('the follower service has not reported this account yet')
        elif lane.get('unavailable'):
            issues.append('the follower service refused it (%s)' % lane['unavailable'])
        elif not lane.get('running'):
            issues.append('its follower lane is stopped')
        if fstatus_age is None or fstatus_age > FIRING_STALE_SECONDS:
            issues.append('follower service status is %s' % ('missing' if fstatus_age is None
                                                              else '%d s old' % fstatus_age))
    if state is None:
        if issues:
            state, label, tone = 'attention', 'Needs a look', 'amber'
        elif calm:
            state, label, tone = 'linked', 'Linked', 'info'
        else:
            state, label, tone = 'trading', 'Trading', 'mint'
    if enabled is False and state == 'linked' and not kill and installed:
        state, label = 'off', 'Off'
    reason = '; '.join(issues + calm)
    if state in ('linked', 'off'):
        reason = 'Not trading: ' + reason + '.'
    else:
        reason = reason[:1].upper() + reason[1:] + '.' if reason else 'Takes each Soup trade after the leader.'
    if kill and state in ('attention', 'linked', 'off', 'trading'):
        ps = pause_status(pause)
        others = [x for x in issues + calm if x != 'paused (entries off)']
        state, label, tone = ps['state'], ps['label'], ps['tone']
        reason = ps['reason'] + (' Also: ' + '; '.join(others) + '.' if others else '')

    recs = lane_records(lane_dir / 'mirror-journal.jsonl')
    stop = lane_stop(lane_dir / 'mirror-journal.jsonl', row, recs)
    life = lane_lifecycle(recs, leader_trade_id, row if rows_ok else None)
    mine = resolve_alerts([a for a in alerts if a.get('account') == key], key, f.get('journal_dir'), recs, row,
                          bool(blocked or failed))
    last = mine[-1] if mine else None
    note = None
    if last and last['kind'] == 'follower_entry_skipped' and not last.get('resolved'):
        note = {'title': '%s skipped a trade%s' % (short_name(name), ' at ' + last['time_text'][:5]
                                                  if last.get('time_text') else ''),
                'text': last['note'], 'tone': 'info'}
    ok_row = isinstance(row, dict) and row.get('available') is True
    return {'key': key, 'role': 'follower', 'name': name, 'short': short_name(name), 'sub': sub,
            'status': {'state': state, 'label': label, 'tone': tone, 'reason': reason},
            'kill_switch': {'on': kill, 'text': 'On · entries off' if kill else 'Off'},
            'pause': pause,
            'position': {'net': row.get('net_position') if ok_row else None,
                         'text': position_text(row.get('net_position')) if ok_row else 'unknown'},
            'orders': {'text': orders_text(row.get('orders')) if ok_row else 'unknown'},
            'stop': stop, 'lifecycle': life,
            'balance': follower_balance(row if rows_ok else None, observed_at, now),
            'alert': ({'text': last['text'], 'time_text': last['time_text'], 'at': last['at'], 'tone': last['tone'],
                       'resolved': last.get('resolved'), 'live': last.get('live', False)}
                      if last else {'text': 'none', 'time_text': None, 'at': None, 'tone': '', 'resolved': None,
                                    'live': False}),
            'note': note,
            'detail': {'account_id': f.get('account_id'), 'profile': f.get('prop_profile'),
                       'native_exits': (row or {}).get('native_exits') if ok_row else None,
                       'enabled_in_settings': enabled, 'lane_running': (lane or {}).get('running')}}


# 2.1: Soup's info alerts for what the trader did by hand on the main account (Soup accepted it; not a fault).
MANUAL_ALERTS = {
    'external_close_detected': lambda a: 'Closed by hand in Tradovate: Soup recorded the trade as closed outside Soup; '
                                         'entries are not blocked',
    'external_reduction': lambda a: '%s contract(s) closed by hand: Soup keeps managing the %s left' % (
        a.get('quantity'), a.get('position')),
    'manual_stop_adopted': lambda a: 'Stop moved by hand to %s: Soup keeps it%s' % (
        a.get('price'), '' if a.get('keep') else ' until TP1 fills (then break-even)'),
}

# Soup's TP1 retry: the bridge refused TP1 because it had not seen the entry fill yet. Soup waits up to
# 30 s and sends the same TP1 again (info); if the wait ends without TP1, Soup exits the trade (amber).
TP1_RETRY_WAIT_S = 30
TP1_ALERTS = {
    'tp1_refused_awaiting_fill': ('info', lambda a: 'TP1 waiting for the entry fill: the bridge still showed the entry as '
                                  'not filled%s. Soup sends the same TP1 again once the fill shows (up to %d s)' % (
                                      ' (%s)' % a['entry_status'] if a.get('entry_status') else '', TP1_RETRY_WAIT_S)),
    'tp1_resent_after_fill': ('info', lambda a: 'TP1 sent %s once the entry fill showed. The bridge had refused it before '
                              'it saw the fill' % ('a third time' if a.get('attempt') == 2 else 'again')),
    'tp1_fill_evidence_timeout': ('amber', lambda a: 'TP1 not placed: the entry fill did not show within %d s, so Soup '
                                  'exited the trade' % TP1_RETRY_WAIT_S),
    'tp1_retry_budget_exhausted': ('amber', lambda a: 'TP1 not placed: the bridge refused it 3 times before it saw the entry '
                                   'fill, so Soup exited the trade'),
}


def soup_alert(soup):
    """The main account's last alert, from Soup's native-exit state (Soup trades it directly)."""
    n = soup.get('native_exits') if isinstance(soup.get('native_exits'), dict) else {}
    ext = n.get('external_close')
    if isinstance(ext, dict):
        return {'text': 'Closed outside the bridge: entries blocked until the reviewed recovery', 'tone': 'coral'}
    failed = n.get('failed')
    if failed:
        reason = failed.get('reason') if isinstance(failed, dict) else failed
        return {'text': 'Exit not verified (%s): entries blocked' % reason, 'tone': 'coral'}
    if n.get('entries_blocked'):
        return {'text': 'Soup is blocking new entries after a native exit', 'tone': 'coral'}
    if n.get('fallback'):
        return {'text': 'Stop fell back to Soup watching it (%s)' % n['fallback'], 'tone': 'amber'}
    items = n.get('alerts') if isinstance(n.get('alerts'), list) else []
    if items and isinstance(items[-1], dict) and items[-1].get('level') == 'info' and items[-1].get('code') in MANUAL_ALERTS:
        return {'text': MANUAL_ALERTS[items[-1]['code']](items[-1]), 'tone': 'info'}
    if items and isinstance(items[-1], dict) and items[-1].get('code') in TP1_ALERTS:
        tone, text = TP1_ALERTS[items[-1]['code']]
        return {'text': text(items[-1]), 'tone': tone}
    if items:
        a = items[-1]
        text = (a.get('kind') or a.get('reason') or a.get('code')) if isinstance(a, dict) else a
        return {'text': str(text).replace('_', ' ')[:160], 'tone': 'amber'}
    return {'text': 'none', 'tone': ''}


def leader_name(s):
    """The main account's name from config.json: `soup_account_id`, else `account_name` (1.1.0:
    no longer a built-in 'Account A')."""
    a = s.get('account') or {}
    return a.get('label') or a.get('name') or 'Leader'


def leader_card(s, main_row, balance=None, key='main'):
    st = s['state'] if isinstance(s.get('state'), dict) else {}
    v, sv, soup = s['verdict'], s.get('stop_view') or {}, s.get('soup') or {}
    kill = bool(s.get('kill_switch'))
    pause = s.get('pause') if isinstance(s.get('pause'), dict) else pause_info(None)
    if v['tone'] == 'mint':
        status = {'state': 'armed', 'label': 'Armed', 'tone': 'mint',
                  'reason': "New entries allowed. Soup's orders arrive here first."}
    elif kill and v['tone'] == 'teal':
        status = {'state': 'entries_off', 'label': 'Entries off', 'tone': 'amber',
                  'reason': 'Exits still work. New entries are refused.'}
    else:
        status = {'state': 'not_ready', 'label': 'Not ready', 'tone': v['tone'], 'reason': v['title'] + '.'}
    if kill and pause.get('on'):
        # Pausing the leader pauses its whole webhook (Bridge Advanced warns about this too).
        ps = pause_status(pause)
        extra = '' if v['tone'] in ('mint', 'teal') else ' Also: ' + v['title'][:1].lower() + v['title'][1:] + '.'
        status = dict(ps, reason=ps['reason'] + ' Followers on this webhook take no new entries either.' + extra)
    ids = st.get('open_order_ids')
    orders = (main_row or {}).get('orders') if isinstance(main_row, dict) else None
    stop = s.get('stop') if isinstance(s.get('stop'), dict) else None
    value = kind_label = None
    if (sv.get('tone') in ('teal', 'mint') or sv.get('pill') == 'Type unknown') and stop \
            and stop.get('quantity') is not None:
        value = '%d @ %s' % (stop['quantity'], stop.get('price_text'))
        kind_label = STOP_KIND_LABELS.get(stop.get('kind'), 'type unknown')
        line = value + ' · ' + kind_label
        when = 'confirmed by Tradovate ' + (stop.get('confirmed_text') or '(time unknown)')
    else:
        line = {'No open trade': 'none'}.get(sv.get('pill'), sv.get('summary') or 'unknown')
        when = None
    unresolved = st.get('unresolved_commands')
    alert = soup_alert(soup)
    if unresolved:
        alert = {'text': '%s unresolved command(s): entries blocked until reconciled' % unresolved, 'tone': 'coral'}
    name = leader_name(s)
    return {'key': key, 'role': 'leader',
            'name': name, 'short': short_name(name),
            'sub': None, 'status': status,
            'kill_switch': {'on': kill, 'text': 'On · entries off' if kill else 'Off'},
            'pause': pause if kill else pause_info(None),
            'position': {'net': st.get('net_position'), 'text': position_text(st.get('net_position'))
                         if s.get('state') is not None else 'unknown'},
            'orders': {'text': orders_text(orders, len(ids) if isinstance(ids, list) else None)},
            'stop': {'line': line, 'when': when, 'tone': sv.get('tone') or '', 'value': value,
                     'kind_label': kind_label},
            'lifecycle': leader_lifecycle(stop, st.get('net_position') if s.get('state') is not None else None, orders),
            'balance': balance or balance_view('unavailable', reason='no balance source'),
            # The leader's alert is Soup's current native-exit state, not history: live while it is shown.
            'alert': dict(alert, time_text=None, at=None, resolved=None, live=alert['text'] != 'none'),
            'note': None, 'detail': {}}


def followers_snapshot(cfg, port, s):
    """Leader + follower cards, the followers' summary, the firing service and its alerts."""
    flist = [f for f in (cfg.get('followers') or []) if isinstance(f, dict) and isinstance(f.get('key'), str)] \
        if isinstance(cfg.get('followers'), list) else []
    out = {'configured': bool(flist), 'service': None, 'status_age_s': None, 'observed_at': None,
           'error': None, 'cards': [], 'alerts': [], 'summary': None}
    main_row = None
    rows, rows_ok = {}, False
    if flist:
        report, err = bridge('followers', port)
        if isinstance(report, dict) and isinstance(report.get('followers'), list):
            rows_ok = True
            rows = {r.get('key'): r for r in report['followers'] if isinstance(r, dict)}
            main_row = report.get('main') if isinstance(report.get('main'), dict) else None
            out['observed_at'] = report.get('observed_at')
        else:
            out['error'] = err or 'invalid_reply'
    fcfg, fdir = firing_paths()
    fstatus = read_json(fdir / 'status.json')
    fstatus = fstatus if isinstance(fstatus, dict) else None
    upd = number((fstatus or {}).get('updated_at'))
    age = (s['generated_at'] - upd / 1000) if upd is not None else None
    out['status_age_s'] = None if age is None else round(age, 1)
    labels = {f['key']: short_label(f.get('label'), f['key'])[0] for f in flist}
    labels.setdefault(leader_key(fcfg, s), leader_name(s))
    alerts = [alert_view(a, labels) for a in read_alerts(fdir)]
    try:
        balance = leader_balance(cfg, port, s['generated_at'])
    except Exception as exc:  # a balance read must never take the cards down
        balance = balance_view('unavailable', reason='error:' + type(exc).__name__)
    leader = leader_card(s, main_row, balance, leader_key(fcfg, s))
    out['cards'].append(leader)
    if flist:
        service = unit_state(FIRING_UNIT)
        out['service'] = service
        for f in flist:
            out['cards'].append(follower_card(f, rows.get(f['key']), rows_ok, fcfg, fstatus, age, service, fdir,
                                              bool(s.get('kill_switch')), alerts, out['observed_at'],
                                              s['generated_at'], (s.get('stop') or {}).get('trade_id')
                                              if isinstance(s.get('stop'), dict) else None))
    # After the cards: follower_card marked the resolved alerts (same dicts).
    out['_all_alerts'] = alerts     # internal (multi-login merge); snapshot() removes it before serving
    out['alerts'] = list(reversed(alerts[-ALERT_LIST:]))
    out['live_alerts'] = sum(1 for a in alerts if a.get('live'))
    out['summary'] = followers_summary(out['cards'][1:])
    return out


def followers_summary(cards):
    """One line for the followers, e.g. 'Both linked, not trading' or 'B trading · C linked, not trading'."""
    if not cards:
        return {'text': 'No followers set up', 'tone': ''}
    words = {'trading': 'trading', 'linked': 'linked, not trading', 'off': 'off', 'blocked': 'blocked',
             'not_linked': 'not linked', 'attention': 'needs a look', 'unknown': 'link unknown',
             'paused': 'paused'}
    states = [c['status']['state'] for c in cards]
    skipped = ' · '.join('%s skipped a trade' % c['short'] for c in cards if c.get('note'))
    tone = 'coral' if any(c['status']['tone'] == 'coral' for c in cards) else \
        'amber' if any(c['status']['tone'] == 'amber' for c in cards) else \
        'mint' if all(x == 'trading' for x in states) else 'info'
    if set(states) == {'unknown'}:
        text = 'Follower status unknown'
    elif len(set(states)) == 1:
        lead = 'Both' if len(cards) == 2 else 'All %d' % len(cards) if len(cards) > 2 else cards[0]['short']
        text = '%s %s' % (lead, words.get(states[0], states[0]))
    else:
        text = ' · '.join('%s %s' % (c['short'], words.get(c['status']['state'], c['status']['state'])) for c in cards)
    return {'text': text + (' · ' + skipped if skipped else ''), 'tone': tone}


def glance(s):
    """The hero's two facts besides the leader's health: is anything in a trade, and are the followers OK."""
    cards = s['followers']['cards']
    open_ = [c for c in cards if number(c['position']['net']) not in (None, 0)]
    unknown = [c for c in cards if c['position']['net'] is None and c['role'] == 'leader']
    if open_ and len(open_) == len(cards) > 2 and len({c['position']['text'] for c in open_}) == 1:
        trade = {'text': 'All %d %s' % (len(cards), open_[0]['position']['text'].lower()), 'tone': 'amber'}
    elif open_:
        trade = {'text': ' · '.join('%s %s' % (c['short'], c['position']['text'].lower()) for c in open_),
                 'tone': 'amber'}
    elif unknown:
        trade = {'text': 'Position unknown', 'tone': 'amber'}
    else:
        trade = {'text': 'No open trade', 'tone': ''}
    return {'trade': trade, 'followers': s['followers']['summary']}


# --- 1.1.0 multi-login ----------------------------------------------------------------------------
# A small copy of tvbridge/logins.py (the panel is its own package): same schema, same rules.

LOGINS_SCHEMA = 'decrypt.tvbridge-logins.v1'
LOGIN_ID_PATTERN = re.compile(r'^[a-z][a-z0-9-]{0,15}\Z')
TOKEN_ENV_PATTERN = re.compile(r'^[A-Z_][A-Z0-9_]{0,127}\Z')
GUARD_RESERVED_PORT = 9556       # reserved by the login guard; never a login's port
LOGIN_TEXT_KEYS = ('config', 'bridge_url', 'token_env', 'browser_unit', 'daemon_unit', 'guard_unit',
                   'trigger_unit', 'guard_state_dir')
LOGIN_UNIT_KEYS = ('browser_unit', 'daemon_unit', 'guard_unit', 'trigger_unit')
LOGIN_PATH_KEYS = ('config', 'guard_state_dir')
GUARD_EVENTS_TAIL_BYTES = 200_000
LOGIN_STATES = {'connected': ('Connected', 'mint'), 'logged_out': ('Logged out', 'amber'),
                'reconnecting': ('Reconnecting', 'amber'), 'unreachable': ('Unreachable', 'coral'),
                'off': ('Off', 'off')}


def implicit_login():
    """The one login there is without a registry: login a, from today's defaults."""
    return {'id': 'a', 'label': 'Login A', 'primary': True, 'config': str(CONFIG),
            'bridge_url': 'http://127.0.0.1:8777', 'token_env': 'TVBRIDGE_TOKEN', 'devtools_port': 9555,
            'browser_unit': 'tvbridge-browser.service', 'daemon_unit': 'tvbridge-daemon.service',
            'guard_unit': 'tvbridge-login-guard.service', 'trigger_unit': 'tvbridge-login-trigger.service',
            'guard_state_dir': str(GUARD_DIR)}


def url_port(url):
    """The explicit port of an http(s)://host:port bridge URL (no path, query or user part)."""
    parts = urllib.parse.urlsplit(url.strip())
    try:
        port = parts.port
    except ValueError:
        port = None
    if parts.scheme not in ('http', 'https') or not parts.hostname or port is None or parts.path.strip('/') \
            or parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError('bridge_url must be an http URL with a port (for example http://127.0.0.1:8778)')
    return port


def load_logins(path=None):
    """The validated login list, primary first, `~` expanded. Without a registry file: [login a].
    Any rule broken raises ValueError with a clear message."""
    path = Path(os.path.expanduser(str(path))) if path else ROOT / 'logins.json'
    if not path.exists():
        return [implicit_login()]
    try:
        raw = json.loads(path.read_text())
    except OSError as exc:
        raise ValueError('%s: cannot read (%s)' % (path, type(exc).__name__))
    except ValueError:
        raise ValueError('%s: not valid JSON' % path)
    if not isinstance(raw, dict) or raw.get('schema') != LOGINS_SCHEMA:
        raise ValueError('%s: schema must be %s' % (path, LOGINS_SCHEMA))
    items = raw.get('logins')
    if not isinstance(items, list) or not items:
        raise ValueError('%s: "logins" must be a non-empty list' % path)
    out, seen = [], {}

    def unique(kind, value, where, label=None):
        if (kind, value) in seen:
            raise ValueError('%s: %s %s is used by both %s and %s' % (path, label or kind, value, seen[(kind, value)],
                                                                     where))
        seen[(kind, value)] = where

    for n, e in enumerate(items, 1):
        if not isinstance(e, dict):
            raise ValueError('%s: login %d is not an object' % (path, n))
        lid = e.get('id')
        if not isinstance(lid, str) or not LOGIN_ID_PATTERN.match(lid):
            raise ValueError('%s: login %d: id must match %s' % (path, n, LOGIN_ID_PATTERN.pattern[:-2] + '$'))
        where = 'login %s' % lid
        for k in LOGIN_TEXT_KEYS:
            if not isinstance(e.get(k), str) or not e[k].strip():
                raise ValueError('%s: %s: %s must be a non-empty string' % (path, where, k))
        e = dict(e, label=e.get('label', 'Login ' + lid.upper()), primary=e.get('primary', False))
        if not isinstance(e['label'], str) or not e['label'].strip():
            raise ValueError('%s: %s: label must be a non-empty string' % (path, where))
        if not isinstance(e['primary'], bool):
            raise ValueError('%s: %s: primary must be true or false' % (path, where))
        for k in LOGIN_UNIT_KEYS:
            if not e[k].endswith('.service'):
                raise ValueError('%s: %s: %s must name a .service unit' % (path, where, k))
        if not TOKEN_ENV_PATTERN.match(e['token_env']):
            raise ValueError('%s: %s: token_env must be an environment variable name' % (path, where))
        if 'read_token_env' in e and not (isinstance(e['read_token_env'], str)
                                          and TOKEN_ENV_PATTERN.match(e['read_token_env'])):
            raise ValueError('%s: %s: read_token_env must be an environment variable name' % (path, where))
        dport = e.get('devtools_port')
        if not isinstance(dport, int) or isinstance(dport, bool) or not 0 < dport < 65536:
            raise ValueError('%s: %s: devtools_port must be a port number' % (path, where))
        try:
            bport = url_port(e['bridge_url'])
        except ValueError as exc:
            raise ValueError('%s: %s: %s' % (path, where, exc))
        login = dict(e, label=e['label'].strip(), bridge_url=e['bridge_url'].strip().rstrip('/'))
        for k in LOGIN_PATH_KEYS:
            login[k] = os.path.expanduser(e[k])
        unique('id', lid, where)
        for p in (dport, bport):
            if p == GUARD_RESERVED_PORT:
                raise ValueError('%s: %s: port %d is reserved for the login guard' % (path, where, p))
            unique('port', p, where)
        unique('config', login['config'], where)
        # token_env and the optional read_token_env share one namespace: no env var names two tokens.
        for k in ('token_env', 'read_token_env'):
            if k in e:
                unique('env', e[k], where if k == 'token_env' else '%s (read_token_env)' % where, k)
        for k in LOGIN_UNIT_KEYS:
            unique('unit', e[k], where)
        unique('guard_state_dir', login['guard_state_dir'], where)
        out.append(login)
    if sum(1 for l in out if l['primary']) != 1:
        raise ValueError('%s: exactly one login must have "primary": true' % path)
    return [l for l in out if l['primary']] + [l for l in out if not l['primary']]


def primary(logins):
    return next(l for l in logins if l['primary'])


def by_id(logins, login_id):
    return next((l for l in logins if l['id'] == login_id), None)


def logins_file(cfg):
    """Where the registry lives: env TVBRIDGE_LOGINS, else config.json `logins_file`, else ROOT/logins.json."""
    p = os.environ.get('TVBRIDGE_LOGINS') or (cfg.get('logins_file') if isinstance(cfg.get('logins_file'), str) else None)
    return Path(os.path.expanduser(p)) if p else ROOT / 'logins.json'


def registry(cfg):
    """(logins, error). (None, None) when there is no registry file: the page stays single-login."""
    path = logins_file(cfg)
    if not path.exists():
        return None, None
    try:
        logins = load_logins(path)
    except ValueError as exc:
        return [], str(exc)
    LOGIN_TOKEN_ENVS.update(l['token_env'] for l in logins)
    LOGIN_TOKEN_ENVS.update(l['read_token_env'] for l in logins if l.get('read_token_env'))
    return logins, None


def plain(value):
    return str(value or '').replace('_', ' ')


def guard_timer(guard_unit):
    """The guard runs from its timer: 'tvbridge-login-guard@b.service' -> 'tvbridge-login-guard@b.timer'."""
    return guard_unit[:-len('.service')] + '.timer' if guard_unit.endswith('.service') else guard_unit


def login_units(entry):
    names = (entry['browser_unit'], entry['daemon_unit'], guard_timer(entry['guard_unit']), entry['trigger_unit'])
    return {n: unit_state(n) for n in names}


def last_relogin(guard_dir, g):
    """Epoch seconds of the last automatic re-login the login guard recorded (state logged_in_now)."""
    recs, _ = tail_records(Path(guard_dir) / 'events.jsonl', GUARD_EVENTS_TAIL_BYTES, b'logged_in_now')
    stamps = [parse_ts(e.get('at') or e.get('ts')) for e in recs if e.get('state') == 'logged_in_now']
    if isinstance(g, dict) and g.get('state') == 'logged_in_now':
        stamps.append(parse_ts(g.get('checked_at')))
    stamps = [t for t in stamps if t is not None]
    return max(stamps) if stamps else None


def login_connection(entry, g, health, health_err, reply, daemon_unit, now):
    """(state, detail) for one login: connected, logged_out, reconnecting or unreachable.

    The daemon's own view wins when it says logged in (/login `page.logged_in` and /health's page
    authorized and synced, each when answered). "Logged out" otherwise comes only from the login guard, as in the
    headline. A daemon that does not answer is "unreachable" unless systemd is restarting it."""
    g = g if isinstance(g, dict) else {}
    hh = health.get('health') if isinstance(health, dict) and isinstance(health.get('health'), dict) else None
    page = reply.get('page') if isinstance(reply, dict) and isinstance(reply.get('page'), dict) else None
    views = []
    if page is not None and isinstance(page.get('logged_in'), bool):
        views.append(page['logged_in'])
    if hh is not None:
        hp = hh.get('page') or {}
        views.append(hp.get('state') == 'authorized' and hp.get('synced') is True)
    logged_in = bool(views) and all(views)    # both replies must agree; either saying no means not connected
    reachable = isinstance(health, dict) or isinstance(reply, dict)
    if reachable and logged_in:
        return 'connected', None
    if not reachable and turned_off(daemon_unit):
        return 'off', 'The bridge is turned off.'   # the guard's last status file is stale while it is off
    reason = str(g.get('reason') or '')
    if g.get('state') in ('needs_human', 'login_repeated') and ('daemon' not in reason or g.get('logged_out_since')):
        return 'logged_out', ('Needs you: log in by hand in the %s window. Automatic login stopped (%s).'
                              % (entry['label'], plain(reason) or 'unknown'))
    login = guard_login(g, now)
    if login == 'out':
        return 'logged_out', logged_out_detail(g, now)
    unit = daemon_unit or {}
    if not reachable:
        if restarting(unit):
            return 'reconnecting', 'Its bridge daemon is restarting automatically.'
        if health_err == 'token_missing':
            # The primary's /health is today's TVBRIDGE_TOKEN read; another login's reads want its read token.
            need = entry['token_env'] if entry['primary'] else (entry.get('read_token_env') or entry['token_env'])
            return 'unreachable', 'The panel has no token for this login (%s is not set).' % need
        if unit.get('active') not in (None, 'active', 'unknown'):
            return 'unreachable', 'Its bridge daemon is not running (%s).' % unit['active']
        return 'unreachable', 'Its bridge daemon does not answer.'   # the error itself is in `health_error`
    if login == 'in':
        return 'reconnecting', 'Tradovate is logged in. The bridge is reconnecting to the page.'
    return 'reconnecting', 'Not connected to Tradovate yet. The login guard has no recent result.'


def plural(n, word):
    return '%d %s%s' % (n, word, '' if n == 1 else 's')


def login_view(entry, s, now, cards, health, health_err, reply, reply_err, units, log, noise, extra=None):
    """The `logins[]` object for one login (the page escapes every value)."""
    g = read_json(Path(entry['guard_state_dir']) / 'status.json')
    g = g if isinstance(g, dict) else None
    state, detail = login_connection(entry, g, health, health_err, reply, units.get(entry['daemon_unit']), now)
    label, tone = LOGIN_STATES[state]
    accounts = reply.get('accounts') if isinstance(reply, dict) and isinstance(reply.get('accounts'), list) else None
    visible = [{'name': str(a.get('name'))[:80], 'configured': a.get('configured') is True, 'role': a.get('role')}
               for a in (accounts or []) if isinstance(a, dict) and a.get('name') not in (None, '')]
    counts = []
    if accounts is not None and state == 'connected':    # a logged-out page lists no accounts: unknown, not 0
        counts.append(plural(len(accounts), 'account') + ' visible')
    counts.append('%d assigned' % len(cards) if cards else 'none assigned')
    paused = sum(1 for c in cards if (c.get('pause') or {}).get('on'))
    if paused:
        counts.append('%d paused' % paused)
    relogin = last_relogin(entry['guard_state_dir'], g)
    down = sum(1 for u in units.values() if u.get('active') != 'active')
    out = {'id': entry['id'], 'label': entry['label'], 'primary': entry['primary'],
           'mode': (reply or {}).get('mode') if isinstance(reply, dict) else None,
           'state': state, 'state_label': label, 'tone': tone, 'detail': detail,
           'counts_text': ' · '.join(counts), 'summary': ' · '.join([label] + counts),
           'visible_count': len(accounts) if accounts is not None else None, 'visible': visible,
           'assigned': len(cards),
           'last_relogin_at': int(relogin * 1000) if relogin else None,
           'last_relogin_text': time_text(relogin * 1000) if relogin else None,
           'guard': {k: (g or {}).get(k) for k in ('state', 'reason', 'checked_at', 'logged_out_since',
                                                   'last_login_attempt_at', 'logins_last_hour')},
           'health_error': health_err, 'login_error': reply_err,
           'units': units, 'units_down': down, 'log': log, 'log_reconnect_noise': noise,
           'cards': cards, 'trade_followers': []}
    out.update(extra or {})
    return out


def read_token(entry):
    """The token for a login's read-only GET /health and GET /login: its read token (env var
    `read_token_env`) when the entry names one and it is set, else its order token (`token_env`)."""
    name = entry.get('read_token_env')
    tok = os.environ.get(name, '') if name else ''
    return tok if len(tok) >= 24 else os.environ.get(entry['token_env'], '')


def other_login(entry, s, now):
    """A non-primary login: its daemon's /health, /login and /followers (only when its config has
    followers), its follower cards (same firing service, same rules as the primary's followers).
    /health, /login and the read-only /followers report use the login's read token when the panel has
    one, else its order token."""
    url = entry['bridge_url']
    rtok = read_token(entry)
    health, health_err = daemon_get(url, 'health', rtok)
    reply, reply_err = daemon_get(url, 'login', rtok)
    lcfg = read_json(entry['config'])
    lcfg = lcfg if isinstance(lcfg, dict) else {}
    flist = [f for f in lcfg.get('followers') if isinstance(f, dict) and isinstance(f.get('key'), str)] \
        if isinstance(lcfg.get('followers'), list) else []
    rows, rows_ok, observed_at, followers_err = {}, False, None, None
    # 1.1.0: the daemon accepts the read token on the read-only GET /followers report too.
    if flist:
        report, err = daemon_get(url, 'followers', rtok)
        if isinstance(report, dict) and isinstance(report.get('followers'), list):
            rows_ok = True
            rows = {r.get('key'): r for r in report['followers'] if isinstance(r, dict)}
            observed_at = report.get('observed_at')
        else:
            followers_err = err or 'invalid_reply'
    master = lcfg.get('master_kill_switch_file')
    master_on = isinstance(master, str) and bool(master) and Path(os.path.expanduser(master)).exists()
    fcfg, fdir = firing_paths()
    fstatus = read_json(fdir / 'status.json')
    fstatus = fstatus if isinstance(fstatus, dict) else None
    upd = number((fstatus or {}).get('updated_at'))
    age = (s['generated_at'] - upd / 1000) if upd is not None else None
    labels = {f['key']: short_label(f.get('label'), f['key'])[0] for f in flist}
    alerts = [alert_view(a, labels) for a in read_alerts(fdir) if a.get('account') in labels]
    service = (s.get('followers') or {}).get('service') or (unit_state(FIRING_UNIT) if flist else None)
    trade_id = (s.get('stop') or {}).get('trade_id') if isinstance(s.get('stop'), dict) else None
    cards = [follower_card(f, rows.get(f['key']), rows_ok, fcfg, fstatus, age, service or {}, fdir, master_on, alerts,
                           observed_at, s['generated_at'], trade_id) for f in flist]
    units = login_units(entry)
    log, noise = unit_log(entry['daemon_unit'])
    view = login_view(entry, s, now, cards, health, health_err, reply, reply_err, units, log, noise,
                      {'followers_error': followers_err, 'master_kill_switch': master_on})
    lead = ((s.get('followers') or {}).get('cards') or [None])[0]
    if cards and isinstance(lead, dict) and lead.get('role') == 'leader':
        n = number(lead['position'].get('net'))
        view['trade_followers'] = follower_relations(lead, cards, n is not None and n != 0, n)
    return view, alerts


def primary_login(entry, s, now):
    """The primary login: today's leader and follower cards, today's /health; plus its /login."""
    reply, reply_err = daemon_get(entry['bridge_url'], 'login', read_token(entry))
    units = login_units(entry)
    return login_view(entry, s, now, (s.get('followers') or {}).get('cards') or [], s.get('health'),
                      s.get('health_error'), reply, reply_err, units, s.get('log') or [],
                      s.get('log_reconnect_noise') or 0)


def logins_snapshot(logins, s):
    """`logins[]` (primary first) and, per non-primary login in registry order, its follower alerts
    (oldest first)."""
    now = s['generated_at']
    out, extra = [], []
    for entry in logins:
        try:
            if entry['primary']:
                out.append(primary_login(entry, s, now))
            else:
                view, alerts = other_login(entry, s, now)
                out.append(view)
                extra.append(alerts)
        except Exception as exc:   # one login's read error never hides the others
            out.append(dict(login_view(entry, s, now, [], None, 'error:' + type(exc).__name__, None, None, {}, [], 0),
                            state='unreachable', state_label='Unreachable', tone='coral',
                            detail='The panel could not read this login (error:%s).' % type(exc).__name__))
    return out, extra


def logins_alerts(all_alerts, extra):
    """(followers.alerts, followers.live_alerts) with a registry. The primary login's alerts come
    first, exactly as without a registry (newest first, the last ALERT_LIST), minus the alerts of
    accounts that belong to another login; each other login's alerts follow, in registry order, the
    same way. Not merged by time: login A's list keeps its order."""
    keys = {a['account'] for alerts in extra for a in alerts}
    own = [a for a in all_alerts if a.get('account') not in keys]
    shown = [a for alerts in [own] + extra for a in reversed(alerts[-ALERT_LIST:])]
    return shown, sum(1 for alerts in [own] + extra for a in alerts if a.get('live'))


def logins_verdict(v, logins, error=None):
    """The headline with a registry. The primary's verdict stands unless it is healthy (mint, or teal
    with its kill switch on); then a connection problem on another login turns it amber and names it."""
    if v['tone'] not in ('mint', 'teal'):
        return v
    p = next((l for l in logins if l['primary']), None)
    pname = p['label'] if p else 'The main login'
    if error and not logins:
        return {'tone': 'amber', 'title': 'Login list unreadable',
                'detail': '%s. Only the main login is shown.' % error}
    bad = [l for l in logins if not l['primary'] and l['state'] != 'connected']
    if not bad:
        return v
    ok = 'trading normally' if v['tone'] == 'mint' else 'entries off'
    title = ' · '.join('%s %s' % (l['label'], l['state_label'].lower()) for l in bad) + ' · %s %s' % (pname, ok)
    parts = []
    for l in bad:
        parts.append('%s: %s' % (l['label'], l['detail'] or l['state_label'] + '.'))
        if l['assigned']:
            parts.append('Its %s new entries until it is back.' % (
                'account skips' if l['assigned'] == 1 else '%d accounts skip' % l['assigned']))
    return {'tone': 'amber', 'title': title, 'detail': ' '.join(parts) + ' %s is not affected.' % pname}


def add_logins(s, cfg, all_alerts):
    """Multi-login fields, only when the registry file exists. Never takes the panel down."""
    logins, error = registry(cfg)
    if logins is None:
        return
    s['logins'] = []
    try:
        if logins:
            s['logins'], extra = logins_snapshot(logins, s)
            fo = s.get('followers')
            if isinstance(fo, dict) and isinstance(fo.get('cards'), list) and all_alerts is not None:
                fo['alerts'], fo['live_alerts'] = logins_alerts(all_alerts, extra)
    except Exception as exc:
        error = error or 'error:' + type(exc).__name__
    if error:
        s['logins_error'] = error
    # `verdict` stays the primary login's own (other /api/status readers use it); the page shows this one.
    s['verdict_logins'] = logins_verdict(s['verdict'], s['logins'], error)


def preview_page(s):
    """The page with one fixed /api/status reply built in, for an offline look (no server needed).
    The reply is scrubbed and embedded as JSON text, never as HTML."""
    data = scrub(json.dumps(s, default=str)).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    stub = ('<script type="application/json" id="preview-data">' + data + '</script>'
            "<script>window.fetch=async()=>({ok:true,status:200,json:async()=>"
            "JSON.parse(document.getElementById('preview-data').textContent)})</script>")
    return PAGE.replace('<script>', stub + '<script>', 1)


SOUP_KEYS = ('phase', 'manual_enabled', 'locked', 'quote_fresh', 'updated_at', 'outbound_attempts',
             'accepted_receipts',
             # 1.3 (read from the same file): route, receipts and native-exit state
             'mode', 'enabled', 'worker_ok', 'remaining_qty', 'rejected_receipts', 'unknown_receipts',
             'native_exits', 'market_session',
             # Read from the same file: the 1-second MES feed status the runtime writes only while a
             # strategy that requires the feed (Lumi) runs through the bridge.
             'price_feed')

# The feed line. The runtime's status.json is written on every tick; a reading older than this says
# nothing about the feed now, so the line reports the age instead of a state.
FEED_STATUS_STALE_SECONDS = 30
FEED_LABEL = '1-second MES feed'
# Reason codes that mean the daemon halted its feed until it is restarted (tvbridge.marketfeed): the runtime's
# own code, and the daemon's reason carried as 'feed_not_fresh:<reason>'.
FEED_WRONG_CONTRACT = ('feed_wrong_contract', 'halted_wrong_contract')


def price_feed_view(soup, now=None):
    """The one-line status of the 1-second MES feed, or None when no feed is in use (then nothing is shown).

    Read-only, from the runtime's status.json `price_feed` (decrypt_trading.bridge_feed.status): `fresh`,
    `reason` (a code, optionally ':<bridge reason>'), `message` and `bridge_message` (plain words), `symbol`.
    Lumi is the one strategy that requires the feed, so the words name it. Tones: '' (quiet), 'amber' (stale:
    Lumi refuses to enter), 'coral' (the daemon stopped the feed; a restart is needed)."""
    if not isinstance(soup, dict) or not isinstance(soup.get('price_feed'), dict):
        return None
    feed = soup['price_feed']
    now = time.time() if now is None else now
    reason = feed.get('reason') if isinstance(feed.get('reason'), str) else ''
    code, _, bridge_reason = reason.partition(':')
    texts = []
    for t in (feed.get('message'), feed.get('bridge_message')):
        if isinstance(t, str) and t.strip() and t.strip() not in texts:
            texts.append(t.strip())
    symbol = feed.get('symbol') if isinstance(feed.get('symbol'), str) and feed['symbol'].strip() else None
    view = {'label': FEED_LABEL, 'symbol': symbol, 'detail': ' '.join(texts) or None, 'reason': reason or None}
    updated = parse_ts(soup.get('updated_at'))
    if updated is None or now - updated > FEED_STATUS_STALE_SECONDS:
        age = ago(now - updated) if updated is not None else 'no time recorded'
        return dict(view, state='unknown', tone='', text='no recent reading (engine status %s)' % age)
    if feed.get('fresh') is True:
        return dict(view, state='live', tone='', text='live')
    if code in FEED_WRONG_CONTRACT or bridge_reason in FEED_WRONG_CONTRACT:
        return dict(view, state='stopped', tone='coral', text='stopped — wrong contract, restart the bridge')
    if code == 'feed_not_configured' or bridge_reason == 'not_configured':
        return dict(view, state='stopped', tone='coral',
                    text='stopped — not set up in the bridge, Lumi won’t enter')
    return dict(view, state='stale', tone='amber', text='stale — Lumi won’t enter')


def snapshot():
    cfg = read_json(CONFIG) or {}
    port = int(cfg.get('listen_port', 8777))
    health, health_err = bridge('health', port)
    state, state_err = bridge('state', port)
    log, noise = daemon_log()
    soup = read_json(SOUP) or {}
    # Not set up yet: the default switch. Set up: exactly the configured one (none configured = paused).
    switch = cfg.get('kill_switch_file') if CONFIG.exists() else str(ROOT / 'ENTRIES-DISABLED')
    hd = health if isinstance(health, dict) else {}
    pause = account_pause(switch, hd.get('entries_disabled'), hd.get('entries_disabled_reason'))
    s = {
        'version': VERSION, 'generated_at': time.time(),
        'account': {'name': cfg.get('account_name'), 'label': cfg.get('soup_account_id'),
                    'ticker': cfg.get('ticker'), 'publish_mode': cfg.get('publish_mode')},
        'health': health, 'health_error': health_err, 'state': state, 'state_error': state_err,
        'pause': pause,
        'kill_switch': pause['on'],
        'guard': read_json(GUARD_DIR / 'status.json'), 'guard_events': guard_events(),
        'mirror': read_json(MIRROR), 'units': units(), 'log': log, 'log_reconnect_noise': noise,
        'configured': CONFIG.exists(),
        'config_problem': config_problem(cfg) if CONFIG.exists() else None,
        'soup': {k: soup.get(k) for k in SOUP_KEYS},
    }
    s['verdict'] = dict(zip(('tone', 'title', 'detail'), verdict(s)))
    try:
        s['stop'] = resting_stop(cfg, state)
    except Exception as exc:  # the stop tile must never take the rest of the panel down
        s['stop'] = {'problem': 'error:' + type(exc).__name__}
    s['stop_view'] = stop_view(s['stop'])
    try:
        s['followers'] = followers_snapshot(cfg, port, s)
    except Exception as exc:  # neither may the followers section
        s['followers'] = {'configured': None, 'error': 'error:' + type(exc).__name__, 'cards': [],
                          'alerts': [], 'summary': {'text': 'Follower status unavailable (read error)',
                                                    'tone': 'amber'}, 'service': None}
        try:
            s['followers']['cards'] = [leader_card(s, None)]
        except Exception:
            pass
    try:
        s['glance'] = glance(s)
    except Exception as exc:
        s['glance'] = {'trade': {'text': 'unknown', 'tone': 'amber'}, 'followers': s['followers'].get('summary')}
    # 2.0 (additive, derived): the checks strip and the trade view. Never take the panel down.
    try:
        s['checks'] = checks(s, s['generated_at'])
    except Exception as exc:
        s['checks'] = []
    try:
        s['trade'] = trade_view(s)
    except Exception as exc:
        s['trade'] = {'open': None, 'unknown': True, 'text': 'unknown', 'followers': [], 'error': 'error:' + type(exc).__name__}
    # 2.2 (additive, read-only display): the webhook line under the Trade header.
    try:
        fcfg, _ = firing_paths()
        s['webhook_line'] = webhook_line(fcfg)
    except Exception:
        s['webhook_line'] = None
    # Read-only display: the 1-second MES feed line; None unless a strategy uses the feed.
    try:
        s['price_feed'] = price_feed_view(s['soup'], s['generated_at'])
    except Exception:
        s['price_feed'] = None
    all_alerts = s['followers'].pop('_all_alerts', None) if isinstance(s.get('followers'), dict) else None
    # 1.1.0 (additive, only with a login registry): one section per login; the headline may name a login.
    add_logins(s, cfg, all_alerts)
    return s


PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark">
<title>Tradovate bridge | Decrypt OS</title>
<style>
:root{color-scheme:dark;--bg:#0b0910;--bg-1:#110e16;--line:#26202e;--line-2:#3a3044;--ink:#fff5eb;--ink-2:#cfc1b3;--ink-3:#8b7f76;--ink-4:#5e554f;--amber:#ff9f43;--purple:#c56cf0;--coral:#ff6b81;--font:'Space Grotesk','Adwaita Sans','Noto Sans',system-ui,sans-serif;--mono:'JetBrains Mono','JetBrainsMono Nerd Font','Noto Sans Mono',ui-monospace,monospace}
*{box-sizing:border-box}html{background:var(--bg)}body{margin:0;font:14px/1.4 var(--font);color:var(--ink);background:var(--bg);min-width:320px}
main{max-width:1280px;margin:0 auto;padding:0 22px 24px;display:grid;grid-template-columns:minmax(0,1fr);grid-template-areas:"top" "verdict" "trade" "money" "att" "more" "foot"}
.side{display:contents}
@media (min-width:1180px){main{grid-template-columns:minmax(0,1fr) 400px;grid-template-rows:auto auto auto 1fr auto;column-gap:36px;grid-template-areas:"top top" "verdict verdict" "trade side" "money side" "foot foot";align-items:start}.side{display:block;grid-area:side;min-width:0}}
@media (min-width:1180px){.side{display:block}}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
/* header */
.top{grid-area:top;display:flex;justify-content:space-between;align-items:center;gap:10px;padding:12px 0 10px;border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px;font:500 12px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--ink-3);min-width:0}
.brand b{color:var(--ink);font-weight:600;white-space:nowrap}
.brand i{width:8px;height:8px;border-radius:2px;background:linear-gradient(135deg,var(--amber),var(--coral) 60%,var(--purple));display:inline-block;flex:none}
.clock{display:flex;align-items:center;gap:8px;margin-left:auto;font:12px/1 var(--mono);color:var(--ink-3);white-space:nowrap}
.gear{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;margin:-8px 0;flex:none;color:var(--ink-3);border-radius:6px}
.gear svg{width:16px;height:16px}
.gear:hover{color:var(--ink)}
.gear:focus-visible{outline:2px solid var(--amber);outline-offset:2px;border-radius:6px}
.clock .dot{width:7px;height:7px;border-radius:50%;background:var(--ink);flex:none}
.stale .clock .dot{background:var(--coral)}
@media (prefers-reduced-motion:no-preference){.live .clock .dot{animation:pulse 3s ease-out infinite}}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(255,245,235,.45)}70%{box-shadow:0 0 0 7px rgba(255,245,235,0)}100%{box-shadow:0 0 0 0 rgba(255,245,235,0)}}
/* verdict + checks */
.verdict{grid-area:verdict;padding:16px 0 14px;border-bottom:1px solid var(--line)}
.verdict h1{margin:0;font-size:34px;line-height:1.05;font-weight:600;letter-spacing:-.01em;color:var(--ink)}
.verdict.amber h1,.verdict.teal h1{color:var(--amber)}.verdict.coral h1{color:var(--coral)}
.verdict p{margin:6px 0 0;max-width:70ch;color:var(--ink-2);font-size:13.5px}
/* before setup: one calm block, no strip, no tables */
.setup{grid-area:trade;padding:18px 0 16px;display:flex;flex-wrap:wrap;align-items:center;gap:10px 18px}
.setup[hidden]{display:none}
.setup .btn{display:inline-flex;align-items:center;gap:8px;padding:10px 16px;border:1px solid var(--line-2);border-radius:8px;color:var(--ink);background:var(--bg-1);font:500 13.5px/1 var(--font);text-decoration:none}
.setup .btn:hover{border-color:var(--amber);color:var(--amber)}
.setup .btn:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
.setup .or{font:12px/1.4 var(--mono);color:var(--ink-3)}
.unconfigured .verdict{border-bottom:0}
.unconfigured .trade,.unconfigured .money,.unconfigured .side,.unconfigured .more{display:none}
.checks{display:grid;grid-template-columns:repeat(6,auto);justify-content:start;gap:6px 28px;margin:14px 0 0;padding:0;list-style:none}
.checks:empty{display:none}
.checks li{display:flex;align-items:center;gap:7px;font:500 11.5px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--ink-2);white-space:nowrap}
.checks i{width:8px;height:8px;border-radius:50%;background:var(--ink);flex:none}
.checks li.no{color:var(--coral)}.checks li.no i{background:var(--coral)}
.checks li.warn{color:var(--amber)}.checks li.warn i{background:var(--amber)}
.checks li.unk{color:var(--ink-3)}.checks li.unk i{background:none;border:1.5px solid var(--amber)}
@media (max-width:820px){.checks{grid-template-columns:repeat(3,auto);justify-content:start;gap:8px 22px}}
/* sections */
section{padding:14px 0 12px;border-bottom:1px solid var(--line);min-width:0}
.sh{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin:0 0 10px}
.sh h2{margin:0;font:600 11px/1 var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--ink-3)}
.sh .r{font:12px/1 var(--mono);color:var(--ink-3);text-align:right}
.sh .r.coral{color:var(--coral)}.sh .r.amber{color:var(--amber)}
/* trade */
.trade{grid-area:trade}
.tr-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 16px;margin:0 0 10px}
.tr-head .big{font:600 26px/1 var(--mono);letter-spacing:-.01em;color:var(--amber);white-space:nowrap}
.tr-head .big.flat{color:var(--ink-2)}.tr-head .big.unknown{color:var(--amber)}
.tr-head .stage{color:var(--ink-2);font-size:13.5px}.tr-head .when{font:12px var(--mono);color:var(--ink-3)}
table{border-collapse:collapse;width:100%;table-layout:fixed}
.matrix th{font:500 10.5px/1.2 var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--ink-3);text-align:center;padding:0 2px 8px;vertical-align:bottom}
.matrix th:first-child,.matrix td:first-child{text-align:left;width:132px}
.matrix th.stp,.matrix td.stp{text-align:right;width:158px}
.matrix td{padding:7px 0 6px;vertical-align:top;border-top:1px solid var(--line)}
.matrix tbody tr:first-child td{border-top:0}
.acct{display:flex;flex-direction:column;gap:3px;min-width:0}
.acct b{font-size:15px;font-weight:600;white-space:nowrap}.acct b small{font:500 10px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--amber);margin-left:6px;vertical-align:middle}
.acct .sub{font-size:10.5px;color:var(--ink-3);line-height:1.2}
.acct .st{font:500 11px/1.25 var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3)}
.acct .st.coral{color:var(--coral)}.acct .st.amber{color:var(--amber)}.acct .st.info{color:var(--purple)}
.acct .vs{font:11.5px/1.25 var(--mono);color:var(--ink-3)}
.acct .vs.same{color:var(--ink-2)}.acct .vs.behind,.acct .vs.ahead,.acct .vs.differs{color:var(--amber)}.acct .vs.fault{color:var(--coral)}.acct .vs.skipped{color:var(--purple)}
/* pause: one line under the account's dots, never inside the account cell, so the row keeps its height */
.matrix tr.pr td{border-top:0;padding:0 0 9px}
.pr .pl{display:flex;align-items:baseline;gap:8px;min-width:0;font:11px/1.4 var(--mono);color:var(--ink-2)}
.pr .tag{flex:none;color:var(--amber)}.pr.coral .tag{color:var(--coral)}
.pr .why{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pr .when,.pr .scope{flex:none;color:var(--ink-3)}.pr .scope{color:var(--amber)}.pr.coral .scope{color:var(--coral)}
.cell{display:flex;flex-direction:column;align-items:center;gap:4px;min-height:34px}
.dot{width:12px;height:12px;border-radius:50%;border:1.5px solid var(--ink-4);position:relative;flex:none}
.dot.done{background:var(--ink);border-color:var(--ink)}
.dot.now{border-color:var(--amber);background:radial-gradient(circle,var(--amber) 0 3px,transparent 3.5px)}
@media (prefers-reduced-motion:no-preference){.dot.now{animation:ring 2.4s ease-out infinite}}
@keyframes ring{0%{box-shadow:0 0 0 0 rgba(255,159,67,.5)}70%{box-shadow:0 0 0 8px rgba(255,159,67,0)}100%{box-shadow:0 0 0 0 rgba(255,159,67,0)}}
.dot.failed{border-color:var(--coral);background:var(--coral)}
.dot.failed::before,.dot.failed::after{content:'';position:absolute;left:2px;right:2px;top:4px;height:1.5px;background:var(--bg);transform:rotate(45deg)}.dot.failed::after{transform:rotate(-45deg)}
.dot.skipped{border-color:var(--purple)}.dot.skipped::before{content:'';position:absolute;left:2px;right:2px;top:4px;height:1.5px;background:var(--purple)}
.dot.warn{border-color:var(--amber)}.dot.unknown{border-style:dashed}
.cell time{font:10.5px/1.2 var(--mono);color:var(--ink-3);white-space:nowrap}
.cell.now time{color:var(--amber)}.cell.failed time{color:var(--coral)}.cell.skipped time{color:var(--purple)}.cell.warn time{color:var(--amber)}
.stop{display:flex;flex-direction:column;align-items:flex-end;gap:3px;min-width:0}
.stop .v{font:600 15px/1.1 var(--mono);white-space:nowrap;display:flex;align-items:center;gap:6px}.stop .v.coral{color:var(--coral)}.stop .v.amber{color:var(--amber)}.stop .v.dim{color:var(--ink-3);font-weight:500;white-space:normal;text-align:right;font-size:13px}
.stop small{font:10.5px/1.3 var(--mono);color:var(--ink-3);text-align:right;overflow-wrap:anywhere;display:flex;flex-wrap:wrap;justify-content:flex-end;gap:3px 6px;align-items:center}
.tag{font:600 9.5px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;padding:2px 5px;border-radius:3px;border:1px solid currentColor;white-space:nowrap}
.tag.be{color:var(--amber)}.tag.init{color:var(--ink-2)}.tag.unk{color:var(--ink-3)}
.note{margin:8px 0 0;font-size:13px;color:var(--purple)}
.webhook{margin:0 0 10px;font:12px/1.4 var(--mono);color:var(--ink-3)}
/* 1-second MES feed: one quiet line, shown only while a strategy uses the feed; amber = stale, coral = stopped */
.feed{margin:0 0 10px;font:12px/1.4 var(--mono);color:var(--ink-3);overflow-wrap:anywhere}
.feed b{font-weight:500;color:var(--ink-2)}.feed.amber,.feed.amber b{color:var(--amber)}.feed.coral,.feed.coral b{color:var(--coral)}
.note strong{font-weight:600}.note span{color:var(--ink-2)}
/* money */
.money{grid-area:money}
.money th{font:500 10.5px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--ink-3);text-align:right;padding:0 0 6px}
.money th:first-child,.money td:first-child{text-align:left;width:132px}
.money th.b,.money td.b{width:150px}.money th.d,.money td.d{width:96px}.money th.t,.money td.t{width:110px}
.money td{padding:7px 0 6px;border-top:1px solid var(--line);text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap;vertical-align:baseline}
.money tbody tr:first-child td{border-top:0}
.money td:first-child{font-family:var(--font);font-weight:600;font-size:15px}
.money .bal{font-size:19px;font-weight:600}.money .bal.un{color:var(--ink-3);font-weight:500;font-size:14px}
.money td.dim{color:var(--ink-3);font-size:12px}.money td.pnl{color:var(--ink-2)}
.money td.why{color:var(--ink-3);font-family:var(--font);font-size:12px;white-space:normal;text-align:right;padding-left:14px}
/* attention */
.att{grid-area:att}
.alerts{list-style:none;margin:0;padding:0;display:grid;gap:4px}
.alerts li{display:grid;grid-template-columns:auto auto minmax(0,1fr) auto;gap:2px 12px;align-items:baseline;padding:7px 10px 7px 12px;border-left:3px solid var(--line-2);background:var(--bg-1);border-radius:0 6px 6px 0;font-size:13px}
.alerts li.coral{border-left-color:var(--coral)}.alerts li.amber{border-left-color:var(--amber)}.alerts li.info{border-left-color:var(--purple)}
.alerts li.resolved{color:var(--ink-3)}.alerts li.resolved .who{color:var(--ink-3)}
.alerts time,.alerts .who{font:12px var(--mono);white-space:nowrap}.alerts .who{font-weight:600}
.alerts li.coral .who{color:var(--coral)}.alerts li.amber .who{color:var(--amber)}.alerts li.info .who{color:var(--purple)}
.alerts .what{min-width:0;overflow-wrap:anywhere}.alerts .what small{display:block;font-size:12px;color:var(--ink-3)}
.alerts .state{font:600 10px/1 var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--ink-3)}
.alerts li.coral .state{color:var(--coral)}.alerts li.amber .state{color:var(--amber)}.alerts li.info .state{color:var(--purple)}
.alerts li.empty{display:block;color:var(--ink-3);border-left-color:var(--line)}
/* attention fold: notes for accounts no longer in the cards lose their accent; older quiet items fold behind one line */
.alerts li.retired{color:var(--ink-3);border-left-color:var(--line-2)}.alerts li.retired .who,.alerts li.retired .state{color:var(--ink-3)}
.alerts li.fold{display:block;padding:0;background:none;border-left-color:transparent;border-radius:0}
.alerts li.fold details{border:0;padding:0}.alerts li.fold summary{justify-content:flex-start;gap:6px;padding:6px 10px 6px 12px;font:12px/1.4 var(--mono);color:var(--ink-3)}
.alerts li.fold summary .tg::before{content:'· show'}.alerts li.fold details[open] summary .tg::before{content:'· hide'}
.alerts li.fold ol{margin-top:4px}
@media (max-width:600px),(min-width:1180px){.alerts li{grid-template-columns:auto minmax(0,1fr) auto}.alerts time{grid-area:1/1}.alerts .who{grid-area:1/2}.alerts .state{grid-area:1/3}.alerts .what{grid-area:2/1/3/4}}
/* secondary */
.more{grid-area:more;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 20px;padding:6px 0 0}
@media (max-width:700px),(min-width:1180px){.more{grid-template-columns:1fr}}
details{border-bottom:1px solid var(--line);padding:9px 0;min-width:0}
summary{cursor:pointer;display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:13px;color:var(--ink-2);list-style:none}
summary::-webkit-details-marker{display:none}
summary .pill{font:500 10.5px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3);white-space:nowrap}
summary .pill.coral{color:var(--coral)}summary .pill.amber{color:var(--amber)}
summary:focus-visible{outline:2px solid var(--amber);outline-offset:4px;border-radius:3px}
dl{margin:8px 0 0;display:grid;grid-template-columns:auto minmax(0,1fr);gap:4px 14px;font-size:12.5px}dt{color:var(--ink-3)}dd{margin:0;font-family:var(--mono);text-align:right;overflow-wrap:anywhere}
.wide{grid-column:1/-1}
pre{margin:8px 0 0;font:11.5px/1.5 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere;color:var(--ink-2);max-height:240px;overflow:auto}
.foot{grid-area:foot;margin:12px 0 0;font-size:12px;color:var(--ink-3)}
.stale .verdict,.stale .trade,.stale .money,.stale .att,.stale .more{opacity:.45}
/* logins (1.1.0, only with a login registry) */
.logins{display:grid;gap:0}
.login{min-width:0;padding:10px 0 12px}
.login+.login{border-top:1px solid var(--line)}
.lh{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 14px;margin:0 0 3px}
.lh h3{margin:0;font-size:16px;font-weight:600;color:var(--ink);border-radius:3px}
.lh h3:focus-visible{outline:2px solid var(--amber);outline-offset:3px}
.ls{display:inline-flex;align-items:center;gap:7px;font:500 11.5px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--ink-2);white-space:nowrap}
.ls i{width:8px;height:8px;border-radius:50%;background:var(--ink);flex:none}
.ls.amber{color:var(--amber)}.ls.amber i{background:var(--amber)}.ls.coral{color:var(--coral)}.ls.coral i{background:var(--coral)}
.lm{margin:0 0 8px;font:12px/1.4 var(--mono);color:var(--ink-3)}
.ld{margin:0 0 8px;max-width:70ch;font-size:13px;color:var(--ink-2)}
.lnone{margin:2px 0 0;font-size:13.5px;color:var(--ink-2)}
.vis{list-style:none;margin:6px 0 0;padding:0;display:flex;flex-wrap:wrap;gap:4px 16px;font:12px/1.4 var(--mono);color:var(--ink-3)}
.lgerr{margin:8px 0 0;font-size:13px;color:var(--amber)}
.money tr.grp th{text-align:left;padding:12px 0 6px;font:600 10.5px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--ink-2);border-top:1px solid var(--line)}
.money tbody tr.grp:first-child th{border-top:0;padding-top:2px}
.money tr.grp+tr td{border-top:0}
.money td.none{font-family:var(--font);font-weight:400;font-size:13px;color:var(--ink-3);text-align:left}
.lgm{display:contents}
@media (max-width:760px){main{padding:0 14px 20px}.verdict h1{font-size:28px}.matrix th:first-child,.matrix td:first-child{width:112px}.matrix th.stp,.matrix td.stp{width:132px}.tr-head .big{font-size:22px}.money th.b,.money td.b{width:130px}}
</style></head><body>
<main>
<header class="top"><div class="brand"><i></i><b>Tradovate bridge</b><span id="acct">—</span></div><div class="clock"><span class="dot"></span><span id="updated" role="status">Loading…</span></div><a class="gear" href="decrypt-advanced://open" title="Open Bridge Advanced" aria-label="Open Bridge Advanced"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg></a></header>
<section class="verdict" id="verdict" aria-live="polite"><h1 id="v-title">Checking the bridge…</h1><p id="v-detail"></p><ul class="checks" id="checks"></ul></section>
<section class="setup" id="setup" hidden aria-label="Set up the bridge"><a class="btn" href="decrypt-os://connections">Open Decrypt OS → Connections</a><span class="or">or press Super+1 and choose Connections. This window updates on its own once the bridge is on.</span></section>
<section class="trade" aria-labelledby="h-trade"><div class="sh"><h2 id="h-trade">Trade</h2><span class="r" id="tr-r"></span></div>
<p class="webhook" id="tr-webhook" hidden></p>
<p class="feed" id="tr-feed" hidden></p>
<div class="tr-head"><span class="big" id="tr-big">—</span><span class="stage" id="tr-stage"></span><span class="when" id="tr-when"></span></div>
<div id="mx-one"><table class="matrix"><thead><tr id="mx-head"></tr></thead><tbody id="mx"></tbody></table></div><div class="logins" id="lg" hidden></div><p class="note" id="tr-note" hidden></p></section>
<section class="money" aria-labelledby="h-money"><div class="sh"><h2 id="h-money">Money</h2><span class="r" id="mo-r"></span></div>
<table class="money"><thead><tr><th>Account</th><th class="b">Balance</th><th class="d">Today</th><th class="t">As of</th><th></th></tr></thead><tbody id="money"></tbody></table></section>
<div class="side"><section class="att" aria-labelledby="h-att"><div class="sh"><h2 id="h-att">Attention</h2><span class="r" id="al-r"></span></div><ol class="alerts" id="alerts"></ol></section>
<div class="more">
<details><summary><span>Tradovate connection<span id="w-conn"></span></span> <span class="pill" id="p-conn">—</span></summary><dl id="d-conn"></dl></details>
<details><summary><span>Automatic login<span id="w-guard"></span></span> <span class="pill" id="p-guard">—</span></summary><dl id="d-guard"></dl></details>
<details><summary>Soup <span class="pill" id="p-soup">—</span></summary><dl id="d-soup"></dl></details>
<details><summary><span>Services<span id="w-units"></span></span> <span class="pill" id="p-units">—</span></summary><dl id="d-units"></dl></details>
<details class="wide"><summary>Account details, balances and resting-stop evidence</summary><dl id="d-acct"></dl></details>
<details class="wide"><summary><span>Recent activity<span id="w-log"></span></span></summary><pre id="events"></pre><pre id="log"></pre></details>
<div class="lgm" id="lg-more"></div>
</div></div>
<p class="foot">Read-only view. It cannot place orders, change a kill switch or restart services. Updates every 3 seconds.</p>
</main>
<script>
// Every element is built with createElement/textContent: no HTML is ever assembled from data.
const $=id=>document.getElementById(id);
const h=(tag,cls,...kids)=>{const e=document.createElement(tag);if(cls)e.className=cls;for(const k of kids){if(k==null||k===false)continue;e.append(k instanceof Node?k:String(k))}return e};
const ago=ms=>{if(ms==null)return '—';const s=Math.max(0,Math.round(ms/1000));return s<90?s+'s ago':s<5400?Math.round(s/60)+' min ago':Math.round(s/3600)+' h ago'};
const agoTs=t=>t?ago(Date.now()-(t>1e12?t:t*1000)):'—';
const agoIso=t=>t?ago(Date.now()-Date.parse(t)):'—';
const dl=(id,rows)=>{$(id).replaceChildren(...rows.flatMap(([k,v])=>[h('dt',null,k),h('dd',null,v??'—')]))};
const pill=(id,text,tone)=>{const p=$(id);p.textContent=text;p.className='pill '+(tone||'')};
const hms=t=>t?String(t).slice(0,8):null;   // '12:14:07.590 UTC' -> '12:14:07'
const words=t=>String(t??'').replace(/_/g,' ');   // 'leader_closed_externally' -> 'leader closed externally'
const STEPS=[['entry','Entry'],['stop','Stop confirmed'],['tp1','TP1'],['break_even','Break-even'],['runners','Runners'],['flat','Flat']];
const isLive=a=>a.live??(!a.resolved&&a.level!=='info');   // 1.3 JSON has no `live`: an alert is live unless it is a note
const resolvedText=r=>'Resolved'+(r.time_text?' '+hms(r.time_text):'')+(r.how?' · '+r.how:'')+(r.note?': '+r.note:'');
function cell(step){
  const st=step.state||'none',c=h('div','cell '+st);
  c.title=step.label+': '+st.replace('_',' ')+(step.time_text?' '+step.time_text:'');
  const word=st==='now'?(step.key==='runners'?'running':step.key==='flat'?'closing':step.key==='tp1'?'working':'waiting'):st==='failed'?'failed':st==='skipped'?'skipped':st==='warn'?'not confirmed':'';
  c.append(h('span','dot '+st),h('time',null,step.time_text?hms(step.time_text)+(st==='failed'||st==='skipped'?'':''):word));
  return c;
}
function matrixRow(c,tr){
  const life=c.lifecycle||{},steps=life.steps||STEPS.map(([key,label])=>({key,label,state:'none'})),st=c.status||{};
  const m=(tr.followers||[]).find(f=>f.key===c.key);
  const showVs=m&&m.relation!=='fault'&&(tr.open===true||m.relation!=='same');
  const a=h('div','acct',h('b',null,c.name,c.role==='leader'?h('small',null,'leader'):null),c.sub?h('span','sub',c.sub):null,
    h('span','st '+(st.tone||''),st.label||'—'),showVs?h('span','vs '+m.relation,m.text):null);
  const row=h('tr',null,h('td',null,a));
  for(const s of steps)row.append(h('td',null,cell(s)));
  const rows=[row];
  const p=c.pause||{};   // older servers omit c.pause; a fault may outrank the pause in c.status, but the pause line still shows
  if(p.on){
    // One line across the row under the dots: who paused, the reason (one line, cut with an ellipsis; full text in the title), when.
    const safety=p.by==='safety',full=st.reason||p.label||'Paused';
    const why=h('span','why',p.reason||(safety?'Set by a safety event or by hand':'No reason recorded'));why.title=full;
    const td=h('td',null,h('div','pl',h('span','tag',safety?'Safety pause':'Admin pause'),why,p.time_text?h('span','when',hms(p.time_text)):null,c.role==='leader'?h('span','scope','pauses the whole webhook'):null));
    td.colSpan=steps.length+2;td.title=full;
    rows.push(h('tr','pr '+(safety?'coral':'amber'),td));
  }
  const sp=c.stop||{},stop=h('div','stop');
  if(sp.value){
    const kind=sp.kind_label==='break-even'?['be','BE']:sp.kind_label==='initial stop'?['init','initial']:sp.kind_label==='moved by hand'?['init','by hand']:['unk','type unknown'];
    stop.append(h('span','v '+(sp.tone==='amber'||sp.tone==='coral'?sp.tone:''),sp.value));
    stop.append(h('small',null,h('span','tag '+kind[0],kind[1]),sp.when?words(sp.when.replace('confirmed by Tradovate ','confirmed ').replace(/ [A-Z]{2,5}$/,'')):''));
  }else{
    const pos=(c.position||{}).text||'unknown';
    stop.append(h('span','v '+(sp.tone==='coral'?'coral':sp.tone==='amber'?'amber':'dim'),sp.line==='none'||!sp.line?(pos==='Flat'?'flat':pos==='unknown'?'unknown':'no stop · '+pos.toLowerCase()):sp.line));
    if(sp.when&&sp.line!=='none')stop.append(h('small',null,words(sp.when)));
  }
  row.append(h('td','stp',stop));
  return rows;
}
const hm=t=>t?String(t).slice(0,5):null;   // '22:03:35 UTC' -> '22:03' (grouped ranges only)
const shortText=t=>String(t||'').split(':')[0].trim();   // 'Skipped a trade: the entry was refused …' -> 'Skipped a trade'
function alertRow(a){
  // a.count>1: several identical notes for one account, shown as one line with the count and the time range.
  // a.retired: the account is no longer in the cards; the line keeps its words but loses its accent.
  const rs=a.resolved,n=a.count||1,li=h('li',(rs?'resolved':(a.tone||''))+(a.retired?' retired':''));
  const when=n>1?h('time',null,(hm(a.first_text)||'?')+'–'+(hm(a.time_text)||'?')):h('time',null,hms(a.time_text)||'now');
  if(n>1)when.title=n+' notes between '+(a.first_text||'?')+' and '+(a.time_text||'?');
  const sub=rs?resolvedText(rs):[a.retired?'retired account':null,a.note].filter(Boolean).join(' · ');
  const what=h('span','what',n>1?shortText(a.text)+' ×'+n:a.text,sub?h('small',null,sub):null);
  if(n>1)what.title=a.text;
  li.append(when,h('span','who',a.account_label||a.account||''),what,h('span','state',rs?'resolved':isLive(a)?'live':'note'));
  return li;
}
// Live items always show in full. Resolved items and notes are the quiet set: identical notes for one account
// become one line, the newest two quiet lines stay visible, the rest fold behind one native <details> line.
function attentionRows(al,known){
  const byAt=(x,y)=>(y.at||0)-(x.at||0),retired=a=>known.size>0&&!!a.account&&!known.has(a.account);
  const live=al.filter(isLive),quiet=[];
  for(const a of al.filter(a=>a.resolved).sort(byAt))quiet.push(Object.assign({},a,{retired:retired(a)}));
  const groups=new Map();
  for(const a of al.filter(a=>!isLive(a)&&!a.resolved).sort(byAt)){
    const k=[a.account||a.account_label||'',a.kind||'',a.text,a.note||''].join('\u0000'),g=groups.get(k);
    if(g){g.count++;g.first_text=a.time_text||g.first_text;g.first_at=a.at??g.first_at}
    else groups.set(k,Object.assign({},a,{count:1,first_text:a.time_text,first_at:a.at,retired:retired(a)}));
  }
  quiet.push(...groups.values());quiet.sort(byAt);
  const shown=quiet.slice(0,2),older=quiet.slice(2),rows=[...live.map(alertRow),...shown.map(alertRow)];
  if(older.length){
    const r=older.filter(a=>a.resolved).length,k=older.filter(a=>!a.resolved).reduce((s,a)=>s+(a.count||1),0);
    const label=[r?r+' older resolved':null,k?k+(k===1?' older note':' older notes'):null].filter(Boolean).join(' · ');   // counts match the header
    rows.push(h('li','fold',h('details',null,h('summary',null,label,h('span','tg')),h('ol','alerts',...older.map(alertRow)))));
  }
  return rows;
}
// 1.1.0: one block per Tradovate login (only when /api/status has `logins`).
function matrixTable(cards,tr){
  const head=((cards[0]||{}).lifecycle||{}).steps||STEPS.map(([key,label])=>({key,label}));
  return h('table','matrix',h('thead',null,h('tr',null,h('th',null,'Account'),...head.map(st=>h('th',null,st.label)),h('th','stp','Resting stop'))),
    h('tbody',null,...cards.flatMap(c=>matrixRow(c,tr))));
}
const tone2=t=>t==='coral'||t==='amber'?t:'';
const relogin=l=>l.last_relogin_text?'last re-login '+hms(l.last_relogin_text)+' ('+agoTs(l.last_relogin_at)+')':'no re-login recorded';
function loginBlock(l,tr){
  const id='lg-h-'+l.id,hd=h('h3',null,l.label||l.id);hd.id=id;hd.tabIndex=0;
  const box=h('div','login',h('div','lh',hd,h('span','ls '+tone2(l.tone),h('i'),l.state_label||'Unknown')));
  box.setAttribute('role','region');box.setAttribute('aria-labelledby',id);
  const cards=l.cards||[];
  box.append(h('p','lm',(cards.length?l.counts_text+' · ':'')+relogin(l)));
  if(l.state!=='connected'&&l.detail)box.append(h('p','ld',l.detail));
  if(cards.length)box.append(matrixTable(cards,tr));
  else{box.append(h('p','lnone',l.summary));
    const vis=l.visible||[];if(vis.length)box.append(h('ul','vis',...vis.map(a=>h('li',null,a.name))));}
  return box;
}
const unitText=u=>u.load==='not-found'?'not installed':(u.active||'unknown')+(u.sub&&u.sub!==u.active?' · '+u.sub:'')+(u.result&&u.result!=='success'?' · '+u.result:'');
function loginMore(l){
  const un=Object.entries(l.units||{}).map(([k,u])=>[k,unitText(u)]),down=l.units_down||0,g=l.guard||{},d=h('dl');
  const rows=[...un,['Login guard',g.state?words(g.state)+(g.reason?' · '+words(g.reason):''):'no status'],['Checked',agoIso(g.checked_at)],
    ['Logged out since',g.logged_out_since?agoIso(g.logged_out_since):'—'],['Last re-login',l.last_relogin_text?hms(l.last_relogin_text)+' ('+agoTs(l.last_relogin_at)+')':'none recorded'],
    ['Bridge',l.health_error?'no answer ('+words(l.health_error)+')':'answering'],['Mode',l.mode?words(l.mode):'—'],
    ['Accounts visible',(l.visible||[]).map(a=>a.name).join(', ')||(l.visible_count===0?'none':'unknown')]];
  d.replaceChildren(...rows.flatMap(([k,v])=>[h('dt',null,k),h('dd',null,v??'—')]));
  return h('details',null,h('summary',null,h('span',null,l.label+' services and log'),' ',h('span','pill '+(down?'coral':''),down?down+' not active':'all active')),
    d,h('pre',null,'Daemon log (reconnect chatter hidden: '+(l.log_reconnect_noise||0)+' of last 60 lines)\n'+((l.log||[]).join('\n')||'No lines.')));
}
let last=0;
async function tick(){
  try{
    const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw new Error('HTTP '+r.status);
    const s=await r.json();last=Date.now();document.body.classList.remove('stale');document.body.classList.add('live');
    const v=s.verdict_logins||s.verdict,off=v.tone==='off';
    document.body.classList.toggle('unconfigured',off);$('setup').hidden=!off;
    $('acct').textContent=off?(s.configured?'bridge off':'not set up'):(s.account.label||'Leader');
    $('verdict').className='verdict '+v.tone;$('v-title').textContent=v.title;$('v-detail').textContent=v.detail;
    $('checks').replaceChildren(...(s.checks||[]).map(c=>h('li',c.ok===true?'yes':c.ok===false?(c.level==='warn'?'warn':'no'):'unk',h('i'),c.text)));
    const fo=s.followers||{cards:[],alerts:[]},cards=fo.cards||[],gl=s.glance||{},tr=s.trade||{open:null,followers:[]};
    const gt=gl.trade||{},open=tr.open===true,anyOpen=cards.some(c=>(c.position||{}).net);
    $('tr-big').textContent=(gt.text||(open?'trade on':'—')).toUpperCase();
    $('tr-big').className='big'+(anyOpen?'':tr.unknown||gt.tone==='amber'?' unknown':' flat');
    $('tr-stage').textContent=[open&&tr.contract?tr.contract:null,open?tr.stage:null].filter(Boolean).join(' · ');
    $('tr-when').textContent=open&&tr.entered_text?'entered '+hms(tr.entered_text):'';
    const wl=s.webhook_line;
    if(wl&&wl.leader&&(wl.followers||[]).length){
      $('tr-webhook').hidden=false;
      $('tr-webhook').textContent=[wl.name,wl.strategy,'leader '+wl.leader+' · followers '+wl.followers.join(', ')].filter(Boolean).join(' · ');
    }else{$('tr-webhook').hidden=true;$('tr-webhook').textContent=''}
    // the 1-second MES feed line: absent or null (no feed in use) hides it.
    const pf=s.price_feed&&typeof s.price_feed==='object'&&typeof s.price_feed.text==='string'?s.price_feed:null;
    const fl=$('tr-feed');fl.hidden=!pf;fl.className='feed '+tone2(pf&&pf.tone);fl.title=pf&&pf.detail?pf.detail:'';
    fl.replaceChildren(...(pf?[h('b',null,(pf.label||'1-second MES feed')+': '),pf.text,pf.symbol?' · '+pf.symbol:null]:[]));
    const sum=fo.summary||gl.followers||{};$('tr-r').textContent=sum.text||'';$('tr-r').className='r '+(sum.tone==='coral'||sum.tone==='amber'?sum.tone:'');
    const head=((cards[0]||{}).lifecycle||{}).steps||STEPS.map(([key,label])=>({key,label}));
    $('mx-head').replaceChildren(h('th',null,'Account'),...head.map(st=>h('th',null,st.label)),h('th','stp','Resting stop'));
    $('mx').replaceChildren(...cards.flatMap(c=>matrixRow(c,tr)));
    const L=Array.isArray(s.logins)&&s.logins.length?s.logins:null,allCards=L?L.flatMap(l=>l.cards||[]):cards;
    $('mx-one').hidden=!!L;$('lg').hidden=!L&&!s.logins_error;
    if(L){const tr2=Object.assign({},tr,{followers:(tr.followers||[]).concat(...L.map(l=>l.trade_followers||[]))});
      $('lg').replaceChildren(...L.map(l=>loginBlock(l,tr2)),...(s.logins_error?[h('p','lgerr','Login list: '+s.logins_error)]:[]));}
    else $('lg').replaceChildren(...(s.logins_error?[h('p','lgerr','Login list unreadable: '+s.logins_error)]:[]));
    const notes=allCards.filter(c=>c.note);
    $('tr-note').hidden=!notes.length;$('tr-note').replaceChildren(...notes.flatMap(c=>[h('strong',null,c.note.title+'. '),h('span',null,c.note.text+' ')]));
    const moneyRow=c=>{const b=c.balance||{},ok=b.state==='ok';
      return h('tr',null,h('td',null,c.name),h('td','b',h('span','bal'+(ok?'':' un'),ok?b.text:'unavailable')),
        h('td',ok&&b.day_pnl_text?'d pnl':'d dim',ok?(b.day_pnl_text||'—'):'—'),h('td','t dim',ok?(b.as_of_text||'time unknown'):'—'),
        h('td','why',ok?'':(b.reason||'no balance source')))};
    const grp=l=>{const th=h('th',null,l.label+' · '+(l.state_label||'unknown').toLowerCase());th.colSpan=5;th.scope='rowgroup';return h('tr','grp',th)};
    $('money').replaceChildren(...(L?L.flatMap(l=>[grp(l),...((l.cards||[]).length?l.cards.map(moneyRow):[h('tr',null,(()=>{const td=h('td','none','No accounts assigned');td.colSpan=5;return td})())])]):cards.map(moneyRow)));
    const stale=allCards.filter(c=>(c.balance||{}).state==='stale').length;
    $('mo-r').textContent=stale?stale+' stale':'';$('mo-r').className='r '+(stale?'amber':'');
    const al=[...(fo.alerts||[])];
    const lead=cards[0];if(lead&&lead.alert&&lead.alert.live&&lead.alert.text!=='none')al.unshift({account_label:lead.name,text:lead.alert.text,tone:lead.alert.tone,time_text:lead.alert.time_text,live:true,resolved:null});
    const live=al.filter(isLive).length,res=al.filter(a=>a.resolved).length,nts=al.length-live-res;
    $('al-r').textContent=al.length?[live+' live',res?res+' resolved':null,nts?nts+(nts===1?' note':' notes'):null].filter(Boolean).join(' · '):'';$('al-r').className='r '+(live?'coral':'');
    const known=new Set(allCards.concat(cards).map(c=>c.key).filter(Boolean));   // accounts still in the cards; others are retired
    $('alerts').replaceChildren(...(al.length?attentionRows(al,known):
      [h('li','empty',fo.configured===false?'No followers are set up in the bridge.':(fo.error&&String(fo.error).startsWith('error:'))?'Follower alerts unavailable (read error).':'Nothing needs you. No alerts.')]));
    const hh=s.health||{},hp=(hh.health||{}).page||{},st=s.state||{};
    const ep=(hp.url||'').includes('demo.')?'Simulation (demo)':(hp.url||'').includes('live.')?'LIVE':'—';
    pill('p-conn',hp.state==='authorized'?(hp.synced?'Synced':'Syncing'):(s.health?'Not connected':'Offline'),hp.state==='authorized'&&hp.synced&&ep.startsWith('Sim')?'':'amber');
    dl('d-conn',[['Endpoint',ep],['Page state',hp.state||s.health_error],['Last message',hp.lastRxAgoMs!=null?ago(hp.lastRxAgoMs):'—'],['Reconnects',hp.reconnectAttempts],['Problems',((hh.health||{}).problems||[]).join(', ')||'none']]);
    const g=s.guard||{};pill('p-guard',(g.state||'unknown').replace(/_/g,' '),g.state==='ok'?'':['needs_human','login_repeated','error'].includes(g.state)?'coral':'amber');
    dl('d-guard',[['Reason',g.reason?String(g.reason).replace(/_/g,' '):'—'],['Checked',agoIso(g.checked_at)],['Logged out since',g.logged_out_since?agoIso(g.logged_out_since):'—'],['Last login attempt',agoIso(g.last_login_attempt_at)],['Logins this hour',g.logins_last_hour!=null?g.logins_last_hour+' of 3':'—']]);
    const sp=s.soup||{},released=sp.manual_enabled&&!sp.locked,ms=sp.market_session||{};
    pill('p-soup',sp.phase==null&&sp.updated_at==null?'No status':released?'Entries released':'Entries held',sp.updated_at==null||!released?'amber':'');
    dl('d-soup',[['Orders go to',sp.mode==='bridge'?(s.account.label||'leader')+', directly':(sp.mode||'—')],['Phase',sp.phase],['Sent / accepted',(sp.outbound_attempts??'—')+' / '+(sp.accepted_receipts??'—')],['Rejected / unknown',(sp.rejected_receipts??'—')+' / '+(sp.unknown_receipts??'—')],['Quotes',sp.quote_fresh==null?'—':sp.quote_fresh?'fresh':'stale'],...(pf?[[pf.label||'1-second MES feed',pf.text+(pf.detail?' · '+pf.detail:'')]]:[]),['Market',ms.state||'—'],['Updated',agoTs(sp.updated_at)]]);
    const un=Object.entries(s.units||{}).map(([k,u])=>[k.replace('tvbridge-','').replace('.service','').replace('.timer',' (timer)'),u.active+(u.sub&&u.sub!==u.active?' · '+u.sub:'')+(u.result&&u.result!=='success'?' · '+u.result:'')]);
    const fs=fo.service;if(fs)un.push(['followers',fs.load==='not-found'?'not installed':fs.active+(fs.sub&&fs.sub!==fs.active?' · '+fs.sub:'')]);
    const down=un.filter(([,x])=>!x.startsWith('active')&&x!=='not installed').length;pill('p-units',down?down+' not active':'all active',down?'coral':'');
    dl('d-units',un);
    const sv=s.stop_view||{};
    const rows=[['Account',s.account.name],['Contract',(st.contract||{}).name],['Net position',st.net_position],['Working orders',(st.open_order_ids||[]).length],['Unresolved commands',st.unresolved_commands],['Kill switch',s.kill_switch?'ON · entries off':'OFF · entries allowed'],['Publish mode',s.account.publish_mode],['Resting stop',sv.pill]];
    for(const x of (Array.isArray(sv.rows)?sv.rows:[]))rows.push(['Stop · '+x[0],x[1]]);
    for(const c of cards){rows.push([c.name+' working orders',(c.orders||{}).text],[c.name+' kill switch',(c.kill_switch||{}).text]);const d=c.detail||{};if(c.role!=='leader')rows.push([c.name+' account',d.account_id],[c.name+' profile',d.profile],[c.name+' native exits',d.native_exits==null?'—':d.native_exits?'on':'off'])}
    rows.push(['Follower service',fs?(fs.load==='not-found'?'not installed':fs.active):'—'],['Service status age',fo.status_age_s!=null?fo.status_age_s+' s':'—'],['Bridge report',fo.error||(fo.observed_at?agoTs(fo.observed_at):'—')]);
    for(const c of cards){const b=c.balance||{};rows.push([c.name+' balance',b.state==='ok'?b.text+' as of '+(b.as_of_text||'time unknown'):'unavailable ('+(b.state==='stale'?'stale: ':'')+(b.reason||'no source')+')'],[c.name+' balance source',(b.source||'—')+(b.checked_at?' · read '+agoTs(b.checked_at):'')])}
    dl('d-acct',rows);
    $('events').textContent='Login guard\n'+((s.guard_events||[]).map(e=>`${e.at?.slice(11,19)||''} ${e.kind} ${e.state} ${e.reason}${e.detail?' '+e.detail:''}`).join('\n')||'No login activity recorded.');
    $('log').textContent='Daemon log (reconnect chatter hidden: '+s.log_reconnect_noise+' of last 60 lines)\n'+((s.log||[]).join('\n')||'No lines.');
    const pl=L?L.find(l=>l.primary):null;for(const w of ['w-conn','w-guard','w-units','w-log'])$(w).textContent=pl?' · '+pl.label:'';
    $('lg-more').replaceChildren(...(L?L.filter(l=>!l.primary).map(loginMore):[]));
    $('updated').textContent='live · '+new Date().toLocaleTimeString([],{hour12:false});
  }catch(e){
    document.body.classList.add('stale');document.body.classList.remove('live');
    $('updated').textContent='Status unavailable ('+e.message+'). Last update '+(last?ago(Date.now()-last):'never');
  }
}
tick();setInterval(tick,3000);
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = 'tvbridge-status/' + VERSION

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        data = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; "
                                                    "style-src 'unsafe-inline'; connect-src 'self'; "
                                                    "frame-ancestors 'none'")
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _cross_site(self):
        """A request a web page made: another site's fetch, or any Origin that is not this page.
        The Bridge window itself and local status readers (no browser headers) pass."""
        if (self.headers.get('Sec-Fetch-Site') or '').lower() == 'cross-site':
            return True
        origin = self.headers.get('Origin')
        return origin is not None and origin not in {'http://' + h for h in ALLOWED_HOSTS}

    def do_GET(self):
        if self.headers.get('Host') not in ALLOWED_HOSTS or self._cross_site():
            return self._send(403, 'forbidden', 'text/plain')
        if self.path in ('/', '/bridge'):
            return self._send(200, PAGE, 'text/html; charset=utf-8')
        if self.path == '/api/status':
            try:
                body = status_body()
            except Exception as exc:
                return self._send(500, json.dumps({'error': type(exc).__name__}), 'application/json')
            return self._send(200, body, 'application/json')
        return self._send(404, 'not found', 'text/plain')


# 2.2 memory: one snapshot reads several MB of journal tails. Overlapping requests (each open panel tab polls
# every 3 s, plus any local status reader) used to build their own copies at once and could reach the unit's MemoryMax while
# the bridge was slow. Now one snapshot is built at a time, reused for SNAPSHOT_REUSE_S by every request that
# arrives meanwhile, and the freed memory is handed back to the system afterwards.
SNAPSHOT_REUSE_S = 1.0
SNAPSHOT_LOCK = threading.Lock()
SNAPSHOT_CACHE = {'at': None, 'body': None}


def release_memory():
    """Return freed heap pages to the system (glibc malloc_trim); a no-op where unavailable."""
    try:
        import ctypes
        ctypes.CDLL('libc.so.6').malloc_trim(0)
    except Exception:
        pass


def status_body():
    """The /api/status JSON text: built by one request at a time and shared for SNAPSHOT_REUSE_S."""
    with SNAPSHOT_LOCK:
        at = SNAPSHOT_CACHE['at']
        if at is not None and SNAPSHOT_CACHE['body'] is not None and time.monotonic() - at < SNAPSHOT_REUSE_S:
            return SNAPSHOT_CACHE['body']
        SNAPSHOT_CACHE['body'] = None  # drop the old copy before building the next one
        try:
            body = scrub(json.dumps(snapshot(), default=str))
        finally:
            release_memory()
        SNAPSHOT_CACHE.update(at=time.monotonic(), body=body)
        return body


def main():
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()


if __name__ == '__main__':
    main()
