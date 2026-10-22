"""Pine-authoritative v3 lifecycle. No network and no broker-page scraping.

An entry creates a dispatchable command immediately after validation/persistence.
HTTP receipt schedules TP1; only authenticated broker evidence changes fills.
The runtime serializes these methods and dispatches ready commands immediately.
"""
import copy
import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from storage import canonical, digest, strict_json

# The build id the published strategy sends in `strategy_build`; an alert from any other build is refused.
REVISION = 'soup-pine-authority-2026-09-13-r4'
PROFILE = 'soup-pine-authority-v3'
NY = ZoneInfo('America/New_York')
TRANCHES = ('tp1', 'parent', 'bias', 'eod')
FINAL = {'filled', 'cancelled', 'rejected'}
# Decrypt OS: the v4 entry alert is the v3 entry alert plus the TP1 working-limit
# offset, which the strategy then owns (the paired setting is used only for v3 alerts). Management stays v3.
ENTRY_SCHEMAS = ('decrypt-soup-entry-v3', 'decrypt-soup-entry-v4')
ENTRY_V4_FIELDS = ('tp1_limit_offset_handles',)

class Rejected(ValueError):
    pass

def require(ok, message):
    if not ok:
        raise Rejected(message)

def number(v):
    try:
        return type(v) in (int, float) and math.isfinite(v)
    except OverflowError:
        return False

def tick_round(v, tick):
    require(number(v) and number(v/tick),'price arithmetic overflow')
    return math.floor(v / tick + .5) * tick

def in_session(ms, session):
    m = re.fullmatch(r'(\d{2})(\d{2})-(\d{2})(\d{2})(?::([1-7]+))?', session)
    require(m is not None, 'unsupported EOD session')
    h1, m1, h2, m2 = map(int, m.groups()[:4])
    require(h1 < 24 and h2 < 24 and m1 < 60 and m2 < 60, 'invalid EOD session')
    a, b = h1 * 60 + m1, h2 * 60 + m2
    local = datetime.fromtimestamp(ms / 1000, NY)
    minute = local.hour * 60 + local.minute
    active = True if a == b else a <= minute < b if a < b else minute >= a or minute < b
    # Overnight TradingView session days refer to the ending calendar day.
    day = local + timedelta(days=1) if a >= b and minute >= a and a != 0 else local
    return active and str(day.isoweekday() % 7 + 1) in (m.group(5) or '1234567')

def validate_quantity_policy(policy, account_id):
    if policy is None:
        return None
    require(isinstance(policy, dict) and set(policy) == {'id', 'account_id', 'max_contracts', 'exit_allocation', 'native_binding'},
            'quantity policy fields')
    require(policy['id'] in ('account-fixed-one-tp1-v1','account-original-allocation-micro-cap-v1')
            and policy['account_id'] == account_id,
            'quantity policy account binding')
    # Decrypt OS: `max_contracts` is kept as the policy marker (1 = fixed one
    # contract, 10 = the alert's original allocation) so the policy shape, the bridge mirror and the
    # native binding stay compatible. It is never a size limit: the OS has no contract cap.
    expected = (1,'tp1_only') if policy['id']=='account-fixed-one-tp1-v1' else (10,'original')
    require(type(policy['max_contracts']) is int
            and (policy['max_contracts'],policy['exit_allocation']) == expected,
            'supported exact-account quantity policy required')
    binding = policy['native_binding']
    require(isinstance(binding,dict) and set(binding) == {'broker_id','account_value','account_label','account_url_sha256'},
            'quantity policy native binding fields')
    require(all(isinstance(binding[k],str) and 0 < len(binding[k]) <= 240
                for k in ('broker_id','account_value','account_label'))
            and isinstance(binding['account_url_sha256'],str)
            and re.fullmatch(r'[0-9a-f]{64}',binding['account_url_sha256']) is not None,
            'quantity policy native binding values')
    return copy.deepcopy(policy)


# Native broker exits (OFF unless a reviewed policy is passed). The bracket stop rests at
# Tradovate; the synthetic watchers become a delayed backstop. Design and ordering rules:
# advance_native() below.
NATIVE_ID = 'soup-native-exits-v1'
NATIVE_BOUNDS = {'modify_confirm_ms': (500, 10000), 'cancel_confirm_ms': (2000, 30000), 'stop_confirm_ms': (1000, 30000),
                 'backstop_ms': (250, 10000), 'exit_verify_ms': (1000, 30000),
                 'reduce_confirm_ms': (1000, 30000), 'max_exit_attempts': (1, 5)}
NATIVE_ALERTS = 20
# Native fixes:
# (a) EXTERNAL_CLOSE_SNAPSHOTS consecutive bridge snapshots refused only by the fill/position
#     conservation check, each flat with nothing working and every Soup order terminal (no
#     reversal): the account was closed outside Soup. Soup stops sending and blocks entries.
# (b) While a newer snapshot of the trade was refused, native commands are held, never built or
#     sent from the older accepted view. A hold that outlasts NATIVE_STALE_HOLD_MS without (a)
#     is a real unknown: the trade exits all (flatten_all acts on the broker's own position).
CONSERVATION_MISMATCH = 'broker fill/position conservation mismatch'
EXTERNAL_CLOSE_SNAPSHOTS = 2
EXTERNAL_CLOSE_REASON = 'external_flat_unreconciled'
NATIVE_STALE_HOLD_MS = 10000
# Manual management of the leader account. Native trades only.
# A reduction made outside Soup is accepted only when it is fully explained: the same conservation-only
# evidence on consecutive snapshots at least MANUAL_CONFIRM_MS apart, the entry completely filled, no Soup
# command in flight or unknown, no native failure. Anything else keeps (a)/(b) above.
# - flat (the trader closed everything): the trade closes as closed outside Soup; no block (info alert).
# - smaller position: the lost contracts come off the runners, MANUAL_ORDER first, and Soup manages the rest.
# - a stop price the trader set by hand is kept (after TP1; a pre-TP1 move still goes to break-even on the TP1 fill).
MANUAL_CONFIRM_MS = 1500
MANUAL_ORDER = ('eod', 'parent', 'bias')
MANUAL_FAULTS = ('entry_incomplete', 'tp1_order_rejected', 'virtual_tp1_history_full')
MANUAL_FLAT_REASON = 'flat_after_hand_reduction'
# TP1 refused before the bridge saw the entry fill.
# Native trades only. The first TP1 is still sent on the entry's HTTP receipt. A bridge refusal
# 'entry not completely filled (<status>, <filled>/<qty>)' with the entry still open (or its status unknown)
# placed nothing, so it does not end the trade: Soup waits for a later broker snapshot showing the entry fully
# filled, then sends the strategy's TP1 again as a new command (same quantity, working limit and allocation).
# A terminal non-filled entry, any other refusal or rejection, TP1_RETRY_MAX re-sends used, or no full-fill
# evidence within TP1_RETRY_WAIT_MS keeps today's tp1_order_rejected exit.
TP1_ENTRY_REFUSAL = re.compile(r'entry not completely filled \(([A-Za-z]+), (\d+)/(\d+)\)')
TP1_ENTRY_DEAD = frozenset({'Rejected', 'Cancelled', 'Canceled', 'Expired'})
TP1_RETRY_MAX = 2
TP1_RETRY_WAIT_MS = 30000


def validate_native_exits(policy, account_id, quantity_policy):
    if policy is None:
        return None
    require(isinstance(policy, dict) and set(policy) == {'id', 'account_id', *NATIVE_BOUNDS},
            'native exits policy fields')
    require(policy['id'] == NATIVE_ID and policy['account_id'] == account_id, 'native exits account binding')
    for key, (low, high) in NATIVE_BOUNDS.items():
        require(type(policy[key]) is int and low <= policy[key] <= high, 'native exits bound: ' + key)
    require(isinstance(quantity_policy, dict) and quantity_policy.get('exit_allocation') == 'original',
            'native exits require the exact-account original allocation policy')
    return copy.deepcopy(policy)


class Engine:
    def __init__(self, database, config, *, tp1_limit_offset_handles=0, quantity_policy=None, native_exits=None):
        self.config = copy.deepcopy(config)
        require(set(config) == {'account_id', 'ticker', 'root', 'mintick', 'stream_id'}, 'account pairing fields')
        require(all(isinstance(config[k], str) and config[k] for k in ('account_id','ticker','root','stream_id')), 'account pairing')
        require(re.fullmatch(r'[A-Za-z0-9_-]{1,48}',config['stream_id']) is not None,'ASCII stream identity')
        require(number(config['mintick']) and config['mintick'] > 0, 'paired tick')
        self.quantity_policy = validate_quantity_policy(quantity_policy, config['account_id'])
        if self.quantity_policy and self.quantity_policy['exit_allocation']=='original':
            require(config['root']=='MNQ' and re.fullmatch(r'MNQ[HMUZ][0-9]{4}',config['ticker']) is not None,
                    'original-allocation micro cap requires paired MNQ contract')
        require(number(tp1_limit_offset_handles) and tp1_limit_offset_handles >= 0,
                'nonnegative finite TP1 limit offset')
        units = tp1_limit_offset_handles / config['mintick']
        require(number(units) and abs(units-round(units)) < 1e-7, 'TP1 offset tick alignment')
        self.tp1_limit_offset_handles = tp1_limit_offset_handles
        # Governs NEW entries only. An active trade keeps the native policy pinned at entry.
        self.native_exits = validate_native_exits(native_exits, config['account_id'], self.quantity_policy)
        self.native_boot_ms = None  # first trusted clock seen by this process (restart bounds)
        self.refusal_evidence = None  # (a) facts of a snapshot refused by the conservation check only
        self.rules = json.loads((Path(__file__).parent / 'settings_contract.json').read_text())
        self.db = sqlite3.connect(str(database), check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, hash TEXT NOT NULL, outcome TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS history (trade_id TEXT PRIMARY KEY, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS inbox (seq INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL,
            received_ms INTEGER NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', detail TEXT);
        ''')
        row = self.db.execute('SELECT body FROM state WHERE id=1').fetchone()
        self.data = json.loads(row[0]) if row else {'pairing': digest(config), 'trade': None, 'commands': {},
            'broker_seq': 0, 'last_quote': None, 'last_bar': None, 'day_lock': None, 'notices': []}
        try:
            require(self.data['pairing'] == digest(config), 'account pairing changed; migrate state explicitly')
            active=self.data['trade']
            require(not active or active['phase']=='closed' or active['payload']['strategy_build']==REVISION,
                    'active trade uses another strategy build; complete it with its paired engine before upgrading')
            require(not active or active['phase']=='closed' or active.get('quantity_policy') == self.quantity_policy,
                    'active quantity policy changed; complete or reconcile the existing trade first')
            if active and active['phase'] != 'closed':
                self.quantity_guard()
        except BaseException:
            self.db.close();raise
        # A crash between dispatch start and its result is uncertain, never ready.
        for c in self.data['commands'].values():
            if c['status'] == 'started': c['status'] = 'unknown'
        self.save()

    def close(self):
        self.db.close()

    def save(self):
        self.db.execute('INSERT OR REPLACE INTO state VALUES (1,?)', (canonical(self.data),))
        self.db.commit()

    def apply(self, channel, p, now_ms, *, entries_enabled=False, locked=False):
        before = copy.deepcopy(self.data)
        try:
            require(type(now_ms) is int, 'trusted clock required')
            require(isinstance(p, dict), 'object required')
            eid = (p.get('trade_id') if p.get('event') == 'entry' else p.get('signalId')) if channel == 'pine' else p.get('event_id')
            require(isinstance(eid, str) and 0 < len(eid) <= 240, 'event identity')
            key = channel + ':' + eid
            h = digest(p)
            old = self.db.execute('SELECT hash,outcome FROM events WHERE id=?', (key,)).fetchone()
            if old:
                require(old[0] == h, 'event identity reused with different content')
                return 'duplicate:' + old[1]
            if channel == 'pine':
                self.validate_pine(p, now_ms)
                if p['event'] == 'entry':
                    if not entries_enabled or locked:
                        result = 'entry_disabled'
                    else:
                        self.entry(p, now_ms)
                        result = 'entry_accepted'
                else:
                    result = self.facts(p, now_ms)
            elif channel == 'quote': result = self.quote(p, now_ms)
            elif channel == 'bar': result = self.bar(p, now_ms)
            elif channel == 'broker': result = self.broker(p, now_ms)
            else: raise Rejected('unknown channel')
            self.advance(now_ms)
            self.db.execute('INSERT INTO events VALUES (?,?,?)', (key, h, result))
            self.save()
            return result
        except BaseException as error:
            self.db.rollback()
            self.data = before
            if channel == 'broker' and isinstance(error, Rejected):
                self.native_refused(p, now_ms, str(error))
            raise
        finally:
            self.refusal_evidence = None

    def validate_pine(self, p, now):
        require(len(canonical(p).encode()) <= 40960, 'Pine message exceeds byte budget')
        require(p.get('event') in ('entry', 'pine_facts'), 'unsupported Pine event')
        v4 = p['event']=='entry' and p.get('schema_version')==ENTRY_SCHEMAS[1]
        require(set(p)==set(self.rules['entry_fields' if p['event']=='entry' else 'facts_fields'])|(set(ENTRY_V4_FIELDS) if v4 else set()), 'Pine wire fields mismatch')
        require(p.get('schema_version') in (ENTRY_SCHEMAS if p['event']=='entry' else ('decrypt-soup-management-v3',)), 'schema mismatch; v2 is separate')
        require(p.get('strategy_build') == REVISION and p.get('execution_profile') == PROFILE, 'strategy revision')
        require(p.get('symbol') == self.config['ticker'], 'instrument mismatch')
        require(p.get('stream_id') == self.config['stream_id'], 'alert stream mismatch')
        context = p.get('execution_context', {})
        require(isinstance(context,dict) and set(context) == {'chart_seconds','decision_time','entry_bar_open','runner_delay_bars','cbdr_trigger'}, 'execution context fields')
        require(type(context['chart_seconds']) is int and context['chart_seconds'] in (1,5), 'paired interval must be 1s or 5s')
        dt, ot = context['decision_time'], context['entry_bar_open']
        require(type(dt) is int and type(ot) is int and dt-ot == context['chart_seconds']*1000 and ot % (context['chart_seconds']*1000)==0, 'bar alignment')
        require(type(context['runner_delay_bars']) is int and context['runner_delay_bars']==1 and context['cbdr_trigger']=='bar_close_touch', 'unsupported execution rule')
        require(type(p.get('timestamp')) is int and p['timestamp']==ot and -5000 <= now-dt <=120000, 'stale/future Pine decision')
        s = p.get('strategy_snapshot')
        require(isinstance(s,dict) and set(s)=={'inputs','constants'}, 'settings snapshot')
        # The strategy's constants are pinned by digest only (settings_contract.json constants_sha256).
        require(isinstance(s['constants'],dict) and digest(s['constants'])==self.rules['constants_sha256'], 'unsupported strategy constants')
        require(isinstance(s['inputs'],dict), 'complete settings required')
        rules=dict(self.rules['inputs'])
        # Inputs named by a pattern (settings_contract.json input_patterns): exactly `count` names match each.
        for group in self.rules.get('input_patterns',()):
            names=[n for n in s['inputs'] if n not in rules and re.fullmatch(group['pattern'],n)]
            require(len(names)==group['count'], 'complete settings required')
            rules.update({n:{k:v for k,v in group.items() if k not in ('pattern','count')} for n in names})
        # Inputs the engine never reads are not named in settings_contract.json (unread_inputs): every remaining name
        # must have a plain form and a scalar value (true/false, a number or short text), and there are at most
        # `max_count` of them. Their number and value types are not pinned.
        unread=self.rules['unread_inputs']; kinds={}
        for n,v in s['inputs'].items():
            if n in rules: continue
            require(re.fullmatch(unread['name_pattern'],n) is not None, 'complete settings required')
            kinds[n]='bool' if type(v) is bool else 'int' if type(v) is int else 'number' if type(v) is float else 'string' if isinstance(v,str) else None
            require(kinds[n] is not None, 'setting type: '+n)
        require(len(kinds)<=unread['max_count'], 'too many settings')
        rules.update({n:{'type':k} for n,k in kinds.items()})
        require(set(s['inputs'])==set(rules), 'complete settings required')
        for name, rule in rules.items():
            v=s['inputs'][name]
            typ=rule['type']
            require((type(v) is bool if typ=='bool' else type(v) is int if typ=='int' else number(v) if typ=='number' else isinstance(v,str) and len(v)<=64), 'setting type: '+name)
            if 'min' in rule: require(v>=rule['min'], 'setting minimum: '+name)
            if 'max' in rule: require(v<=rule['max'], 'setting maximum: '+name)
            if 'options' in rule: require(v in rule['options'], 'setting option: '+name)
        in_session(dt, s['inputs']['eod_session'])  # Validate even if EOD is OFF.
        require(s['inputs']['enable_live_engine_alerts'] is True,'live-mode snapshot required')
        if p['event']=='entry': self.validate_entry(p)
        else:
            require(isinstance(p.get('trade_id'),str) and 0<len(p['trade_id'])<=200, 'management trade identity')
            require(isinstance(p.get('facts'),list) and 0<len(p['facts'])<=136, 'facts batch')
            for f in p['facts']:
                require(isinstance(f,dict) and f.get('kind') in ('stop_bias_flip','bias_flip','parent_sweep','counter_ifvg','progress_fail'), 'fact kind')
                expected={'kind','close_side'}|({'inversion_time'} if f['kind']=='counter_ifvg' else {'trade_id','adverse_ratio'} if f['kind']=='progress_fail' else set())
                require(set(f)==expected,'fact fields')
                require(f.get('close_side') in ('long','short'), 'fact direction')
                if f['kind']=='counter_ifvg': require(type(f.get('inversion_time')) is int and f['inversion_time']<=dt, 'IFVG time')
                if f['kind']=='progress_fail': require(isinstance(f.get('trade_id'),str) and number(f.get('adverse_ratio')) and 0<=f['adverse_ratio']<=1, 'progress evidence')

    def validate_entry(self,p):
        s=p['strategy_snapshot']['inputs']; tick=self.config['mintick']; side=p.get('side')
        require(side in ('long','short'), 'entry side')
        require(isinstance(p.get('trade_id'),str) and 0<len(p['trade_id'])<=200, 'trade identity')
        require(p.get('entry_action')==('buy' if side=='long' else 'sell'), 'entry action')
        # Decrypt OS: the alert's own sizes are the truth (TradingView owns sizing,
        # e.g. an alert may send less than the snapshot's tranche settings). Tranches are
        # non-negative whole numbers that add up to `quantity`, which is at least 1. No upper bound.
        for k in ('tp1_qty','parent_runner_qty','bias_runner_qty','eod_runner_qty'):
            require(type(p.get(k)) is int and p[k]>=0,'allocation: '+k)
        require(type(p.get('quantity')) is int and p['quantity']>=1, 'empty allocation')
        require(p['quantity']==p['tp1_qty']+p['parent_runner_qty']+p['bias_runner_qty']+p['eod_runner_qty'],
                'allocation mismatch: quantity')
        for k in ('entry_price','tp1_price','runner_cbdr_target','break_even_price','initial_hard_stop_price','catastrophe_stop_price','effective_pre_tp1_stop_price','mintick'):
            require(number(p.get(k)), 'finite numeric field: '+k)
        require(p['mintick']==tick, 'paired tick mismatch')
        entry=p['entry_price']; sign=1 if side=='long' else -1
        require(entry>0, 'entry reference')
        for k in ('entry_price','tp1_price','break_even_price','runner_cbdr_target','initial_hard_stop_price','catastrophe_stop_price'):
            units=p[k]/tick
            require(number(units) and abs(units-round(units))<1e-7, 'tick alignment: '+k)
        require(p['tp1_price']==tick_round(entry+sign*s['tp1_handles'],tick) and p['tp1_price']>0, 'TP1/settings mismatch')
        require(p['break_even_price']==tick_round(entry+sign*s['be_offset_handles'],tick) and p['break_even_price']>0, 'BE/settings mismatch')
        stops=[]
        for flag,distance,field in [('use_initial_hard_stop','initial_hard_stop_handles','initial_hard_stop_price'),('use_catastrophe_stop','cat_stop_handles','catastrophe_stop_price')]:
            value=tick_round(entry-sign*s[distance],tick) if s[flag] else 0
            require(p[field]==value and (value>0 if s[flag] else value==0), 'stop/settings mismatch')
            if s[flag]: stops.append(value)
        effective=(max(stops) if sign==1 else min(stops)) if stops else 0
        require(p['effective_pre_tp1_stop_price']==effective, 'effective stop mismatch')
        require(p.get('initial_hard_stop_enabled') is s['use_initial_hard_stop'] and p.get('catastrophe_stop_enabled') is s['use_catastrophe_stop'], 'stop flags')
        cbdr=p['runner_cbdr_target']
        require(cbdr==0 or (s['use_cbdr_tp_for_eod_runner'] and cbdr>0 and sign*(cbdr-p['tp1_price'])>=0), 'CBDR target geometry')
        require(s['use_ltf_tp_exec'] is False, 'unsupported legacy TP emission')
        if 'tp1_limit_offset_handles' in p:
            offset=p['tp1_limit_offset_handles']
            require(number(offset) and offset>=0, 'nonnegative finite TP1 limit offset')
            units=offset/tick
            require(number(units) and abs(units-round(units))<1e-7, 'TP1 offset tick alignment')

    def tp1_offset(self,p):
        """The TP1 working-limit offset of this alert: its own value (v4), else the paired setting (v3)."""
        return p['tp1_limit_offset_handles'] if 'tp1_limit_offset_handles' in p else self.tp1_limit_offset_handles

    def entry(self,p,now):
        t=self.data['trade']
        require(not t or t['phase']=='closed','active broker lifecycle; entry rejected, not held')
        s=p['strategy_snapshot']['inputs']; day=datetime.fromtimestamp(now/1000,NY).date().isoformat()
        require(not (s['pause_after_cbdr_tp'] and self.data['day_lock']==day), 'strategy CBDR day lock')
        require(not(s['close_all_eod'] and in_session(p['execution_context']['entry_bar_open'],s['eod_session'])), 'strategy EOD window')
        size=p['execution_context']['chart_seconds']*1000
        require(not(s['close_all_eod'] and in_session(now//size*size-size,s['eod_session'])), 'current strategy EOD window')
        if self.quantity_policy and self.quantity_policy['exit_allocation']=='tp1_only':
            require(p['tp1_qty'] > 0, 'fixed-one TP1 policy requires an original TP1 allocation')
        # Pin execution policy to this trade. Do not alter Pine's target or
        # reinterpret an existing trade/working order after restart.
        offset = self.tp1_offset(p) if p['tp1_qty'] else 0
        sign = 1 if p['side']=='long' else -1
        working_limit = tick_round(p['tp1_price'] - sign*offset, self.config['mintick'])
        if offset:
            require(working_limit > 0 and sign*(working_limit-p['entry_price']) > 0,
                    'TP1 offset reaches or crosses entry; entry rejected')
        if t: self.db.execute('INSERT OR REPLACE INTO history VALUES (?,?)',(t['id'],canonical({'trade':t,'commands':self.data['commands']})))
        alloc={'tp1':p['tp1_qty'],'parent':p['parent_runner_qty'],'bias':p['bias_runner_qty'],'eod':p['eod_runner_qty']}
        if self.quantity_policy and self.quantity_policy['exit_allocation']=='tp1_only':
            alloc={'tp1':1,'parent':0,'bias':0,'eod':0}
        self.data['commands']={}
        self.data['trade']={'id':p['trade_id'],'payload':copy.deepcopy(p),'side':p['side'],'phase':'entering',
            'alloc':alloc,'entry_filled':0,'remaining':0,'tp1_done':False,'runner_not_before':None,
            'terminal_reason':None,'reductions':{},'counter':0,'broker_time':None,'open_orders':None,
            'broker_orders':{},'reconcile_requested':False,'barrier_id':None}
        if self.quantity_policy:
            self.data['trade']['quantity_policy'] = copy.deepcopy(self.quantity_policy)
        if offset:
            self.data['trade']['execution_policy'] = {
                'id':'tp1-one-handle-v1', 'tp1_limit_offset_handles':offset,
                'tp1_strategy_target':p['tp1_price'], 'tp1_working_limit':working_limit}
        native = self.native_plan(p) if self.native_exits else None
        cid = self.command('entry', quantity=sum(alloc.values()),price=p['entry_price'])
        if native:
            self.native_attach(cid, native)

    # --- native broker exits (only reached when a trade pinned a native policy) ----------

    def native_plan(self, p):
        """Payload values used exactly; None (with a notice) keeps today's synthetic flow."""
        why = None
        if not (p['initial_hard_stop_enabled'] is True and p['initial_hard_stop_price'] > 0):
            why = 'no_initial_hard_stop'
        elif p['tp1_qty'] <= 0:
            why = 'no_tp1_fill_event'
        if why:
            self.notice('native_exits_unavailable:' + why)
            return None
        return copy.deepcopy(self.native_exits)

    def native_attach(self, cid, policy):
        t = self.data['trade']; entry = self.data['commands'][cid]; plan = t['payload']
        entry.update(native=True, bridge_kind='bracket_entry', stop_price=plan['initial_hard_stop_price'])
        sid = cid + '#stop'  # the bridge reports the bracket stop leg under this id
        self.data['commands'][sid] = {'id': sid, 'trade_id': t['id'], 'kind': 'stop', 'quantity': entry['quantity'],
            'price': plan['initial_hard_stop_price'], 'alloc': {}, 'reason': 'initial_hard_stop', 'status': 'attached',
            'filled_qty': 0, 'started_at': None, 'receipt': None, 'native': True,
            'quantity_limit': self.quantity_policy['max_contracts']}
        t['native'] = {'policy': policy, 'mode': 'native', 'entry_id': cid, 'stop_id': sid,
                       'stop_confirmed': False, 'backstop': None, 'exit_attempts': 0, 'failed': None, 'alerts': []}
        self.quantity_guard()

    def native_active(self):
        t = self.data['trade']
        return bool(t and t.get('native') and t['native']['mode'] == 'native')

    def native_alert(self, code, now=None):
        n = self.data['trade']['native']
        n['alerts'] = (n['alerts'] + [{'code': code, 'at': now}])[-NATIVE_ALERTS:]
        self.notice('native_exits_alert:' + code)

    def native_fallback(self, code, now=None):
        """This trade continues with today's synthetic exits (cancel barrier, then market)."""
        t = self.data['trade']; n = t['native']
        if n['mode'] != 'native':
            return
        n['mode'] = 'synthetic'; n['fallback'] = code; n['backstop'] = None
        self.native_alert('fallback:' + code, now)

    def native_since(self, at):
        """Deadlines never start before this process saw its first clock (restart safety)."""
        return max(at if type(at) is int else 0, self.native_boot_ms or 0)

    def native_conservation_evidence(self, p, entry, commands, remaining):
        """(a) Facts of a snapshot about to be refused by the conservation check alone (every
        earlier check passed). Candidate: flat, nothing working, every Soup order terminal, no
        command in flight, and Soup's own fills below the entry fill (no reversal).
        `external_reduction` counts every contract closed outside Soup, earlier accepted ones included."""
        external = self.manual_external()
        exits = entry['filled_qty'] - remaining - external
        terminal = all(c['status'] in FINAL | {'superseded'} for c in commands if c['kind'] not in ('cancel', 'modify_stop'))
        idle = not any(c['status'] in ('ready', 'started') for c in commands)
        evidence = {'sequence': p['sequence'], 'observed_at': p['observed_at'], 'entry_qty': entry['filled_qty'],
                    'soup_exit_fills': exits, 'external_reduction': remaining + external - p['position_qty'],
                    'candidate': (p['position_qty'] == 0 and p['open_orders'] == [] and remaining > 0
                                  and entry['filled_qty'] > 0 and terminal and idle)}
        evidence['manual'] = self.manual_evidence(p, entry, commands, remaining, evidence)
        return evidence

    # --- manual management of the leader ------------------------------------------------------

    def manual_state(self):
        t = self.data['trade']
        return ((t or {}).get('native') or {}).get('manual') or {}

    def manual_external(self):
        """Contracts of this trade already accepted as closed outside Soup."""
        return sum(self.manual_state().get('external', {}).values())

    def manual_only(self, k):
        """A tranche closed by hand: Soup skips its runner decisions (recorded for the followers)."""
        return self.manual_state().get('external', {}).get(k, 0) > 0 and self.left()[k] == 0

    def pending_reductions(self):
        return [k for k in self.data['trade']['reductions'] if not self.manual_only(k)]

    def manual_split(self, left, quantity, flat):
        """Tranches for `quantity` contracts closed by hand: runners MANUAL_ORDER first. TP1 only when
        flat (its order is gone then); a partial close that would need a smaller TP1 order is None."""
        out = {}
        for k in MANUAL_ORDER + ('tp1',):
            used = min(left[k], quantity)
            if used and k == 'tp1' and not flat:
                return None
            if used:
                out[k] = used; quantity -= used
        return out if quantity == 0 else None

    def manual_evidence(self, p, entry, commands, remaining, evidence):
        """A conservation-only refusal that is a fully explained reduction outside Soup: 'full' or
        'partial' with its fingerprint (what must be identical on the confirming snapshot), else None."""
        t = self.data['trade']; n = t['native']; position = p['position_qty']
        if (n.get('failed') or n.get('external_close') or t.get('dispatch_fault') or entry['status'] != 'filled'
                or entry['filled_qty'] != entry['quantity'] or not 0 <= position < remaining
                or any(c['status'] in ('ready', 'started', 'unknown') for c in commands)
                or any(c['kind'] in ('reduce', 'flatten') and c['status'] == 'accepted' for c in commands)):
            return None
        reason = t['terminal_reason']
        try:
            left = self.left()
        except Rejected:
            return None
        if position == 0:
            if not evidence['candidate'] or (reason and (reason.startswith('native_') or reason in MANUAL_FAULTS)):
                return None
            split = self.manual_split(left, min(remaining, sum(left.values())), True)
        else:
            if reason or sum(left.values()) != remaining:
                return None
            split = self.manual_split(left, remaining - position, False)
        if split is None:
            return None
        orders = sorted((o.get('command_id'), o.get('status'), o.get('filled_qty')) for o in p['orders'])
        fingerprint = digest([position, entry['filled_qty'], evidence['soup_exit_fills'], self.manual_external(),
                              sorted(p['open_orders']), orders])
        return {'kind': 'full' if position == 0 else 'partial', 'fingerprint': fingerprint,
                'quantity': remaining - position, 'tranches': split}

    def manual_confirmed(self, evidence):
        """The previous refused snapshot carried the same explained reduction, far enough apart."""
        m = evidence.get('manual'); refused = self.data['trade']['native'].get('refused') or {}
        previous = refused.get('manual')
        return bool(m and previous and refused.get('expired') is None
                    and type(refused.get('sequence')) is int and evidence['sequence'] > refused['sequence']
                    and (previous['kind'], previous['fingerprint']) == (m['kind'], m['fingerprint'])
                    and evidence['observed_at'] - previous['first_observed_at'] >= MANUAL_CONFIRM_MS)

    def manual_book(self, evidence, now):
        """Accept the confirming snapshot: book the reduction on the tranches it closed."""
        t = self.data['trade']; n = t['native']; m = evidence['manual']
        first = n['refused']['manual']
        state = n.setdefault('manual', {'external': {k: 0 for k in TRANCHES}, 'events': []})
        for k, v in m['tranches'].items():
            state['external'][k] += v
        event = {'kind': 'close' if m['kind'] == 'full' else 'reduction', 'at': now, 'sequence': evidence['sequence'],
                 'first_sequence': first['first_sequence'], 'observed_at': evidence['observed_at'],
                 'quantity': m['quantity'], 'tranches': dict(m['tranches']), 'entry_qty': evidence['entry_qty'],
                 'soup_exit_fills': evidence['soup_exit_fills'], 'external_reduction': evidence['external_reduction'],
                 'position': evidence['entry_qty'] - evidence['soup_exit_fills'] - evidence['external_reduction']}
        state['events'] = (state['events'] + [event])[-NATIVE_ALERTS:]
        alert = {'code': 'external_close_detected' if m['kind'] == 'full' else 'external_reduction', 'level': 'info',
                 'at': now, 'trade_id': t['id'], **{k: event[k] for k in ('quantity', 'tranches', 'entry_qty',
                                                                        'soup_exit_fills', 'external_reduction', 'position')}}
        n['alerts'] = (n['alerts'] + [alert])[-NATIVE_ALERTS:]
        self.notice('native_exits_info:%s:entry=%d,soup_exit_fills=%d,external=%d,position=%d'
                    % (alert['code'], event['entry_qty'], event['soup_exit_fills'], event['external_reduction'],
                       event['position']))
        if m['kind'] == 'full':
            # Closed outside Soup: nothing more is sent. The terminal code stays EXTERNAL_CLOSE_REASON, the one
            # the followers act on (leader_closed_externally); no `failed`, so no block file or kill switch.
            state['close'] = dict(event, previous_terminal_reason=t['terminal_reason'])
            self.terminal(EXTERNAL_CLOSE_REASON)
            n['backstop'] = None
            t.get('pending_runner_facts', {}).clear(); t.get('pending_price_crossings', {}).clear()
            for c in self.data['commands'].values():
                if c['status'] == 'ready':
                    c['status'] = 'superseded'

    def manual_stop_moved(self, stop, previous, p, now):
        """The broker shows the resting stop at a new price no Soup modify asked for: the trader moved it."""
        t = self.data['trade']
        if stop['price'] == previous or any(c['kind'] == 'modify_stop' and c['status'] in ('started', 'accepted', 'unknown')
                                            and c['price'] == stop['price'] for c in self.data['commands'].values()):
            return
        n = t['native']; state = n.setdefault('manual', {'external': {k: 0 for k in TRANCHES}, 'events': []})
        # Before TP1 the TP1 fill still moves it to break-even; after TP1 the trader's price stays for the trade.
        state['stop'] = {'price': stop['price'], 'previous': previous, 'at': now, 'sequence': p['sequence'],
                         'observed_at': p['observed_at'], 'keep': t['tp1_done']}
        state['events'] = (state['events'] + [dict(state['stop'], kind='stop')])[-NATIVE_ALERTS:]
        n['alerts'] = (n['alerts'] + [{'code': 'manual_stop_adopted', 'level': 'info', 'at': now, 'trade_id': t['id'],
                                       'price': stop['price'], 'previous': previous, 'keep': t['tp1_done']}])[-NATIVE_ALERTS:]
        self.notice('native_exits_info:manual_stop_adopted:%s->%s' % (previous, stop['price']))

    def native_backstop_level(self, level):
        """The synthetic backstop watches the resting stop: the trader's price once moved by hand (not after the
        break-even move that a pre-TP1 move still gets). Synthetic trades keep the strategy level."""
        t = self.data['trade']; stop = self.manual_state().get('stop')
        if not stop or not self.native_active() or (t['tp1_done'] and not stop['keep']):
            return level
        return self.data['commands'][t['native']['stop_id']]['price']

    def native_backstop_reason(self, reason):
        t = self.data['trade']
        default = t['payload']['break_even_price' if reason == 'break_even' else 'initial_hard_stop_price']
        return reason if self.native_backstop_level(default) == default else 'manual_stop'

    def native_refused(self, p, now, reason):
        """A bridge snapshot of the active native trade was refused (the state is rolled back).
        Record it, so no native command is built from the older view (b), and count consecutive
        conservation-only flat snapshots toward an external close (a). Never raises."""
        evidence, self.refusal_evidence = self.refusal_evidence, None
        before = None
        try:
            t = self.data['trade']
            # A native trade that fell back to synthetic exits records only a manual reduction.
            if not (t and t['phase'] != 'closed' and t.get('native') and isinstance(p, dict)):
                return
            native_mode = self.native_active()
            if not native_mode and not (evidence and evidence.get('manual')) and not (t['native'].get('refused') or {}).get('manual'):
                return
            n = t['native']; seq = p.get('sequence')
            if (n.get('external_close') or p.get('trade_id') != t['id'] or p.get('account_id') != self.config['account_id']
                    or p.get('symbol') != self.config['ticker'] or type(seq) is not int or seq <= self.data['broker_seq']):
                return
            previous = n.get('refused') or {}
            if type(previous.get('sequence')) is int and seq <= previous['sequence']:
                return  # a redelivered or older refusal
            before = copy.deepcopy(self.data)
            candidate = bool(native_mode and reason == CONSERVATION_MISMATCH and evidence and evidence['sequence'] == seq
                             and evidence['candidate'])
            manual = evidence.get('manual') if (reason == CONSERVATION_MISMATCH and evidence
                                                and evidence['sequence'] == seq) else None
            refused = dict(previous)
            refused.update({'sequence': seq, 'observed_at': p.get('observed_at') if type(p.get('observed_at')) is int else None,
                            'at': now, 'reason': reason[:160], 'candidate': candidate,
                            'external_streak': previous.get('external_streak', 0) + 1 if candidate else 0})
            refused.setdefault('since', now); refused.setdefault('held', None); refused.setdefault('expired', None)
            n['refused'] = refused
            if candidate:
                refused['external'] = {k: evidence[k] for k in ('entry_qty', 'soup_exit_fills', 'external_reduction')}
                refused.setdefault('first_sequence', seq)
            else:
                refused.pop('external', None); refused.pop('first_sequence', None)
            last = previous.get('manual')
            if manual and last and (last['kind'], last['fingerprint']) == (manual['kind'], manual['fingerprint']):
                refused['manual'] = dict(last, streak=last['streak'] + 1, sequence=seq)
            elif manual:
                refused['manual'] = {'kind': manual['kind'], 'fingerprint': manual['fingerprint'],
                                     'quantity': manual['quantity'], 'tranches': manual['tranches'], 'streak': 1,
                                     'first_sequence': seq, 'sequence': seq,
                                     'first_observed_at': p['observed_at'] if type(p.get('observed_at')) is int else now}
            else:
                refused.pop('manual', None)
            # A fully explained close is accepted by broker() on a confirming snapshot instead of blocking.
            if (refused['external_streak'] >= EXTERNAL_CLOSE_SNAPSHOTS
                    and (refused.get('manual') or {}).get('kind') != 'full'):
                self.native_external_close(now, refused)
            self.save()
        except Exception:
            # The refusal itself still stands; nothing is recorded.
            if before is not None:
                self.data = before
            try:
                self.db.rollback()
            except Exception:
                pass

    def native_external_close(self, now, refused):
        """(a) Terminal, no further command, entries blocked (the service writes the block file and
        the bridge kill switch from `failed`), alert with the quantities. Never archived here."""
        t = self.data['trade']; n = t['native']; q = refused['external']
        previous = t['terminal_reason']
        n['external_close'] = {'trade_id': t['id'], 'at': now, 'sequence': refused['sequence'], 'first_sequence': refused.get('first_sequence'),
                               'observed_at': refused['observed_at'], 'entry_qty': q['entry_qty'],
                               'soup_exit_fills': q['soup_exit_fills'], 'external_reduction': q['external_reduction'],
                               'previous_terminal_reason': previous}
        if n['failed']:
            n['external_close']['previous_failed'] = copy.deepcopy(n['failed'])
        t['terminal_reason'] = EXTERNAL_CLOSE_REASON; t['barrier_id'] = None
        t['phase'] = 'closing'; t['reductions'] = {}; n['backstop'] = None
        t.get('pending_runner_facts', {}).clear(); t.get('pending_price_crossings', {}).clear()
        for c in self.data['commands'].values():
            if c['status'] == 'ready':
                c['status'] = 'superseded'  # built but never sent
        # `failed` is what the service turns into the block file and the bridge kill switch.
        n['failed'] = {'at': now, 'reason': EXTERNAL_CLOSE_REASON, 'remaining': t['remaining'],
                       'open_orders': list(t['open_orders'] or [])}
        n['alerts'] = (n['alerts'] + [{'code': 'external_close_detected', 'at': now, 'trade_id': t['id'], 'entry_qty': q['entry_qty'],
                                       'soup_exit_fills': q['soup_exit_fills'],
                                       'external_reduction': q['external_reduction']}])[-NATIVE_ALERTS:]
        self.notice('native_exits_alert:external_close_detected:entry=%d,soup_exit_fills=%d,external=%d'
                    % (q['entry_qty'], q['soup_exit_fills'], q['external_reduction']))

    def native_holds(self, c, now):
        """True when a ready native command must not be sent now: never after an external close
        (superseded), and not while a newer snapshot is refused (held; the exit-all of a real
        unknown still goes)."""
        t = self.data['trade']; n = t.get('native')
        if not n or n['mode'] != 'native':
            return False
        if n.get('external_close'):
            c['status'] = 'superseded'
            return True
        refused = n.get('refused')
        if not refused or refused.get('expired') is not None:
            return False
        if c['kind'] == 'flatten' and not refused['candidate']:
            return False
        if refused.get('held') is None:
            refused['held'] = now
            self.notice('native_hold_stale_broker_view')
        return True

    def execution_quantity(self):
        """Original Pine sizing stays evidence, not the account execution size."""
        return sum(self.data['trade']['alloc'].values())

    # --- TP1 refused before the bridge saw the entry fill ---------------------------------------

    def tp1_retry(self):
        t = self.data['trade']
        return ((t or {}).get('native') or {}).get('tp1_retry') or {}

    def tp1_refused(self):
        """TP1 commands the bridge refused while the entry fill was pending: nothing was placed. They are
        kept as sent (receipts and counts stay true) but never count toward allocation or conservation."""
        return set(self.tp1_retry().get('refused', ()))

    def tp1_live(self):
        """The trade's single live TP1 command, or None (not created yet, or a refused one awaits fill evidence)."""
        refused = self.tp1_refused()
        live = [c for c in self.data['commands'].values() if c['kind'] == 'tp1_limit' and c['id'] not in refused]
        require(len(live) <= 1, 'more than one live TP1 command')
        return live[0] if live else None

    def tp1_limit_price(self):
        t = self.data['trade']
        return t.get('execution_policy', {}).get('tp1_working_limit', t['payload']['tp1_price'])

    def tp1_send(self):
        """The strategy's TP1 exactly as planned: quantity, working limit and allocation never change."""
        t = self.data['trade']
        return self.command('tp1_limit', t['alloc']['tp1'], self.tp1_limit_price(), {'tp1': t['alloc']['tp1']}, 'tp1')

    def tp1_event(self, retry, kind, now, **extra):
        retry['events'] = (retry['events'] + [dict(extra, kind=kind, at=now)])[-NATIVE_ALERTS:]

    def tp1_refusal_wait(self, c, receipt, now):
        """A rejected TP1: True when it is the bridge's fill-race refusal and the trade waits for fill
        evidence instead of ending (native mode, entry still open or unknown, re-send budget left)."""
        t = self.data['trade']
        if not self.native_active() or t['terminal_reason'] or c['filled_qty']:
            return False
        n = t['native']; entry = self.data['commands'][n['entry_id']]
        if entry['kind'] != 'entry' or entry.get('bridge_kind') != 'bracket_entry':
            return False
        reason = receipt.get('reason') if isinstance(receipt, dict) and receipt.get('bridge') == 'refused' else None
        m = TP1_ENTRY_REFUSAL.fullmatch(reason) if isinstance(reason, str) else None
        if (not m or m.group(1) in TP1_ENTRY_DEAD or int(m.group(3)) != entry['quantity']
                or int(m.group(2)) > int(m.group(3))):
            return False
        retry = n.setdefault('tp1_retry', {'refused': [], 'resent': [], 'awaiting': None, 'events': []})
        if len(retry['resent']) >= TP1_RETRY_MAX:
            self.tp1_event(retry, 'budget_exhausted', now, command_id=c['id'], reason=reason)
            self.native_alert('tp1_retry_budget_exhausted', now)
            return False
        retry['refused'].append(c['id'])
        retry['awaiting'] = {'command_id': c['id'], 'at': now, 'broker_seq': self.data['broker_seq'],
                             'entry_status': m.group(1), 'entry_filled': int(m.group(2)), 'entry_qty': int(m.group(3)),
                             'reason': reason}
        self.tp1_event(retry, 'refused', now, command_id=c['id'], reason=reason)
        n['alerts'] = (n['alerts'] + [{'code': 'tp1_refused_awaiting_fill', 'level': 'info', 'at': now, 'trade_id': t['id'],
                                       'command_id': c['id'], 'entry_status': m.group(1),
                                       'attempt': len(retry['refused'])}])[-NATIVE_ALERTS:]
        self.notice('native_exits_info:tp1_refused_awaiting_fill:%s,%s/%s' % m.groups())
        return True

    def tp1_retry_step(self, now):
        """A refused TP1 awaits fill evidence: send it again once a later accepted snapshot shows the entry
        completely filled; with no such snapshot within TP1_RETRY_WAIT_MS the trade exits as today."""
        t = self.data['trade']; n = t['native']; retry = n['tp1_retry']; wait = retry['awaiting']
        entry = self.data['commands'][n['entry_id']]
        if (self.data['broker_seq'] > wait['broker_seq'] and entry['status'] == 'filled'
                and entry['filled_qty'] == t['entry_filled'] == self.execution_quantity()):
            cid = self.tp1_send()
            retry['awaiting'] = None; retry['resent'].append(cid)
            self.tp1_event(retry, 'resent', now, command_id=cid, refused_id=wait['command_id'],
                           broker_seq=self.data['broker_seq'])
            n['alerts'] = (n['alerts'] + [{'code': 'tp1_resent_after_fill', 'level': 'info', 'at': now, 'trade_id': t['id'],
                                           'command_id': cid, 'refused_id': wait['command_id'],
                                           'attempt': len(retry['resent'])}])[-NATIVE_ALERTS:]
            self.notice('native_exits_info:tp1_resent_after_fill:%d' % len(retry['resent']))
        elif now - self.native_since(wait['at']) > TP1_RETRY_WAIT_MS:
            retry['awaiting'] = None
            self.tp1_event(retry, 'timeout', now, command_id=wait['command_id'])
            self.native_alert('tp1_fill_evidence_timeout', now)
            self.terminal('tp1_order_rejected')

    def tp1_retry_end(self, now, why):
        """The trade ends (or leaves native mode) while a refused TP1 still awaits fill evidence."""
        retry = self.tp1_retry()
        if retry.get('awaiting'):
            command_id = retry['awaiting']['command_id']; retry['awaiting'] = None
            self.tp1_event(retry, 'ended', now, command_id=command_id, reason=why)

    def tp1_guard(self):
        """Persisted TP1 commands: one live command; refused ones placed nothing; every one is the plan's TP1."""
        t = self.data['trade']; commands = self.data['commands']
        tps = [c for c in commands.values() if c['kind'] == 'tp1_limit']
        retry = self.tp1_retry()
        if not retry:
            require(len(tps) <= 1, 'one TP1 command per trade')
            return
        require(t.get('native') and set(retry) == {'refused', 'resent', 'awaiting', 'events'}
                and all(isinstance(retry[k], list) for k in ('refused', 'resent', 'events')), 'TP1 retry record')
        refused, resent, wait = retry['refused'], retry['resent'], retry['awaiting']
        # A re-sent command can itself be refused again: resent and refused may share ids.
        require(len(set(refused)) == len(refused) and len(set(resent)) == len(resent)
                and len(resent) <= TP1_RETRY_MAX and len(resent) <= len(refused) <= len(resent) + 1, 'TP1 retry counts')
        require(all(cid in commands and commands[cid]['kind'] == 'tp1_limit' for cid in refused + resent), 'TP1 retry commands')
        require(all(commands[cid]['status'] == 'rejected' and commands[cid]['filled_qty'] == 0 for cid in refused),
                'refused TP1 must have placed nothing')
        require(wait is None or (isinstance(wait, dict) and refused and wait.get('command_id') == refused[-1]
                                 and len(refused) == len(resent) + 1 and type(wait.get('at')) is int
                                 and type(wait.get('broker_seq')) is int), 'TP1 retry wait')
        require(len(tps) - len(refused) <= 1, 'more than one live TP1 command')
        plan = (t['alloc']['tp1'], self.tp1_limit_price(), {'tp1': t['alloc']['tp1']})
        require(all((c['quantity'], c['price'], c['alloc']) == plan for c in tps), 'TP1 re-send differs from the plan')

    def quantity_guard(self):
        t = self.data['trade']
        if not t or t['phase'] == 'closed':
            return
        require(t.get('quantity_policy') == self.quantity_policy, 'active quantity policy mismatch')
        self.tp1_guard()
        if not self.quantity_policy:
            return
        if self.quantity_policy['exit_allocation']=='original':
            plan=t['payload'];marker=self.quantity_policy['max_contracts']  # policy marker, not a limit
            expected={'tp1':plan['tp1_qty'],'parent':plan['parent_runner_qty'],
                      'bias':plan['bias_runner_qty'],'eod':plan['eod_runner_qty']}
            require(t['alloc']==expected and all(type(v) is int and v>=0 for v in t['alloc'].values())
                    and sum(t['alloc'].values())==plan['quantity'] and 0<plan['quantity'],
                    'original allocation mismatch')
            require(all(type(t[k]) is int and 0<=t[k]<=plan['quantity'] for k in ('remaining','entry_filled')),
                    'original position limit exceeded')
            for command in self.data['commands'].values():
                kind=command['kind'];quantity=command['quantity'];allocation=command['alloc']
                if command.get('native') or kind in ('stop','modify_stop','bracket_entry'):
                    self.native_command_guard(command, plan['quantity'], marker)
                    continue
                require(kind in ('entry','tp1_limit','cancel','reduce','flatten'), 'original command kind')
                require(type(quantity) is int and (quantity==0 if kind=='cancel' else 0<quantity<=plan['quantity']),
                        'original command quantity')
                require(type(command['filled_qty']) is int and 0<=command['filled_qty']<=quantity,
                        'original command fill quantity')
                require(type(command.get('quantity_limit')) is int and command['quantity_limit']==marker,
                        'original command policy missing')
                if kind in ('entry','cancel'):
                    require(allocation=={} and (quantity==plan['quantity'] if kind=='entry' else quantity==0),
                            'original entry/cancel command quantity or allocation')
                else:
                    require(isinstance(allocation,dict) and bool(allocation) and set(allocation)<=set(TRANCHES)
                            and all(type(v) is int and 0<v<=t['alloc'][k] for k,v in allocation.items())
                            and sum(allocation.values())==quantity, 'original exit command allocation')
                    if kind=='tp1_limit':
                        require(allocation=={'tp1':t['alloc']['tp1']}, 'original TP1 command allocation')
            return
        require(t['alloc'] == {'tp1':1,'parent':0,'bias':0,'eod':0}
                and all(type(v) is int for v in t['alloc'].values()), 'fixed-one allocation mismatch')
        require(all(type(t[k]) is int and 0 <= t[k] <= 1 for k in ('remaining','entry_filled')),
                'fixed-one position limit exceeded')
        for command in self.data['commands'].values():
            kind = command['kind']
            require(kind in ('entry','tp1_limit','cancel','flatten'), 'fixed-one command kind')
            expected = 0 if kind == 'cancel' else 1
            require(type(command['quantity']) is int and command['quantity'] == expected,
                    'fixed-one command quantity')
            require(type(command['filled_qty']) is int and 0 <= command['filled_qty'] <= expected,
                    'fixed-one command fill quantity')
            require(type(command.get('quantity_limit')) is int and command['quantity_limit'] == 1,
                    'fixed-one command policy missing')
            require(command['alloc'] == ({'tp1':1} if kind in ('tp1_limit','flatten') else {}),
                    'fixed-one command allocation')

    def native_command_guard(self, command, planned, marker):
        t = self.data['trade']; kind = command['kind']; quantity = command['quantity']
        require(t.get('native') and command.get('native') is True, 'native command without a native trade')
        require(kind in ('entry','bracket_entry','stop','modify_stop','flatten','reduce','cancel'), 'native command kind')
        require(type(command.get('quantity_limit')) is int and command['quantity_limit'] == marker, 'native command policy missing')
        require(type(quantity) is int and type(command['filled_qty']) is int and 0 <= command['filled_qty'] <= quantity,
                'native command fill quantity')
        if kind in ('entry','bracket_entry'):
            require(quantity == planned and command['alloc'] == {}, 'native entry quantity or allocation')
        elif kind in ('stop','flatten'):
            require(0 <= quantity <= planned and command['alloc'] == {}, 'native stop/flatten quantity')
        elif kind == 'modify_stop':
            require(0 < quantity <= planned and command['alloc'] == {} and command['filled_qty'] == 0,
                    'native stop modify quantity')
        elif kind == 'cancel':
            require(quantity == 0 and command['alloc'] == {} and command['filled_qty'] == 0, 'native stop cancel')
        else:
            allocation = command['alloc']
            require(isinstance(allocation, dict) and bool(allocation) and set(allocation) <= set(TRANCHES)
                    and all(type(v) is int and 0 < v <= t['alloc'][k] for k, v in allocation.items())
                    and sum(allocation.values()) == quantity and 0 < quantity <= planned, 'native reduce allocation')

    def command(self,kind,quantity=0,price=None,alloc=None,reason=None,extra=None):
        t=self.data['trade'];t['counter']+=1
        cid='soup-'+hashlib.sha256((t['id']+':'+str(t['counter'])+':'+kind).encode()).hexdigest()[:32]
        self.data['commands'][cid]={'id':cid,'trade_id':t['id'],'kind':kind,'quantity':quantity,
            'price':price,'alloc':alloc or {},'reason':reason,'status':'ready','filled_qty':0,
            'started_at':None,'receipt':None}
        if extra:self.data['commands'][cid].update(extra)
        if self.quantity_policy:
            self.data['commands'][cid]['quantity_limit'] = self.quantity_policy['max_contracts']
            self.quantity_guard()
        return cid

    def ready(self):
        return [copy.deepcopy(c) for c in self.data['commands'].values() if c['status']=='ready']

    def start(self,cid,now):
        self.quantity_guard()
        c=self.data['commands'][cid];require(c['status']=='ready','command must be reconciled, not retried')
        if c.get('native') and c['kind']!='entry' and self.native_holds(c,now):
            self.save();return None
        if c['kind']=='entry' and now-self.data['trade']['payload']['execution_context']['decision_time']>120000:
            self.discard_entry('stale_entry_not_replayed');return None
        if c['kind']=='entry':
            s=self.settings();size=self.data['trade']['payload']['execution_context']['chart_seconds']*1000
            if s['close_all_eod'] and in_session(now//size*size-size,s['eod_session']):
                self.discard_entry('eod_entry_not_replayed');return None
        if c.get('native') and c['kind'] in ('reduce','flatten','modify_stop'):
            return self.native_start(c, now)
        if c['kind'] in ('reduce','flatten'):
            t=self.data['trade']
            if t['broker_time'] is None or not 0<=now-t['broker_time']<=15000 or t['open_orders']!=[]:
                self.notice('exit_requires_current_broker_evidence_after_restart_or_delay');self.save();return None
            if c['kind']=='flatten':c['quantity']=t['remaining']
            if self.quantity_policy:
                require(0<c['quantity']<=t['remaining'],
                        'exit requires verified quantity within the position')
                if self.quantity_policy['exit_allocation']=='tp1_only':
                    require(c['quantity'] == t['remaining'] == 1, 'fixed-one exit requires one verified contract')
            c['price']=self.reference_price(now)
        self.quantity_guard()
        c['status']='started';c['started_at']=now;self.save();return copy.deepcopy(c)

    def native_start(self, c, now):
        t = self.data['trade']
        if c['kind'] == 'reduce':
            # A partial market reduce may run beside the resting stop only when fresh broker
            # evidence shows nothing else working and stop + reduce cannot exceed the position.
            stop = self.data['commands'][t['native']['stop_id']]
            stop_order = t['broker_orders'].get(stop['id'])
            allowed = [stop_order['broker_order_id']] if stop_order and stop['status'] == 'accepted' else []
            stop_left = stop['quantity'] - stop['filled_qty'] if allowed else 0
            if (t['broker_time'] is None or not 0 <= now - t['broker_time'] <= 15000
                    or not set(t['open_orders']) <= set(allowed) or stop_left + c['quantity'] > t['remaining']):
                self.notice('native_reduce_waits_for_stop_resize_evidence'); self.save(); return None
            require(0 < c['quantity'] <= t['remaining'],
                    'exit requires verified quantity within the position')
        elif c['kind'] == 'flatten':
            # flatten_all acts on the bridge's own live account state (liquidation at the
            # broker, then cancels). It never waits for Soup-side evidence.
            c['quantity'] = t['remaining']
        c['price'] = self.reference_price(now) if c['kind'] != 'modify_stop' else c['price']
        self.quantity_guard()
        c['status'] = 'started'; c['started_at'] = now; self.save(); return copy.deepcopy(c)

    def native_transport_result(self, c, outcome, receipt, now):
        t = self.data['trade']; n = t['native']; kind = c['kind']
        if kind == 'entry' and c.get('bridge_kind') == 'bracket_entry':
            stop = self.data['commands'][n['stop_id']]
            if outcome == 'rejected':
                # Definitely not placed (bridge refusal or Tradovate rejection): the same
                # signal continues with today's plain market entry and synthetic exits.
                c['kind'] = 'bracket_entry'; stop['status'] = 'superseded'
                self.native_fallback('bracket_entry_rejected', now)
                self.command('entry', quantity=c['quantity'], price=c['price'])
                return
            legs = receipt.get('legs') if isinstance(receipt, dict) else None
            if outcome == 'accepted' and not (isinstance(legs, dict) and legs.get('stop')):
                stop['status'] = 'superseded'
                self.native_fallback('bracket_stop_leg_missing', now)
                return
            stop['status'] = outcome  # accepted, or unknown until the bridge resolves the legs
            return
        if kind == 'flatten' and outcome == 'accepted' and isinstance(receipt, dict) and (
                receipt.get('bridge') == 'noop' or receipt.get('mode') == 'cancel_only'):
            c['status'] = 'cancelled'; c['no_order'] = True  # accepted without an order of its own
        if outcome != 'accepted':
            self.notice('native_dispatch_%s:%s' % (outcome, kind))
        if n['mode'] != 'native':
            return
        if kind == 'modify_stop' and outcome == 'rejected':
            self.native_alert('stop_modify_rejected', now)
            self.native_modify_failed('stop_modify_rejected', now)
        elif kind == 'cancel' and outcome == 'rejected':
            self.native_alert('stop_cancel_rejected', now); self.terminal('native_stop_cancel_rejected')
        elif kind == 'reduce' and outcome == 'rejected':
            self.native_alert('reduce_rejected', now); self.terminal('native_reduce_rejected')

    def discard_entry(self,reason):
        for c in self.data['commands'].values():
            if c['kind']=='entry' and c['status']=='ready':c['status']='superseded'
        self.data['trade']['phase']='closed';self.notice(reason);self.save()

    def transport_result(self,cid,outcome,receipt,now):
        require(outcome in ('accepted','rejected','unknown'),'transport outcome')
        c=self.data['commands'][cid];require(c['status']=='started','unexpected transport result')
        c['status']=outcome;c['receipt']=receipt
        t=self.data['trade']
        if c.get('native'):
            self.native_transport_result(c,outcome,receipt,now)
            self.advance(now);self.save();return
        if c['kind']=='entry' and outcome=='rejected': t['phase']='closed'
        elif outcome=='rejected':
            self.notice('dispatch_rejected:'+c['kind'])
            if c['kind']!='tp1_limit':t['dispatch_fault']=c['id']
        if c['kind']=='tp1_limit' and outcome=='rejected' and not self.tp1_refusal_wait(c,receipt,now):
            self.terminal('tp1_order_rejected')
        self.advance(now);self.save()

    def notice(self,text):
        self.data['notices']=(self.data['notices']+[text])[-100:]

    def settings(self): return self.data['trade']['payload']['strategy_snapshot']['inputs']
    def terminal(self,reason):
        t=self.data['trade']
        if not t['terminal_reason']:
            t['terminal_reason']=reason;t['barrier_id']=None
        t['phase']='closing';t['reductions']={}
        for c in self.data['commands'].values():
            if c['status']=='ready' and c['kind'] in ('tp1_limit','reduce','modify_stop'):c['status']='superseded'
            if c['status']=='ready' and c['kind']=='entry':
                c['status']='superseded';t['phase']='closed'

    def left(self):
        t=self.data['trade'];out=dict(t['alloc'])
        if t['phase']=='closed' and t['remaining']==0:return {k:0 for k in TRANCHES}
        refused=self.tp1_refused()  # refused TP1 commands placed nothing
        for c in self.data['commands'].values():
            if c['id'] in refused:continue
            left=c['filled_qty']
            for k,v in c['alloc'].items():
                used=min(left,v);out[k]-=used;left-=used
        for k,v in self.manual_state().get('external',{}).items():out[k]-=v  # closed by hand
        require(all(v>=0 for v in out.values()),'tranche overfill')
        return out

    def reduce(self,keys,reason):
        t=self.data['trade']
        if t['terminal_reason']:return
        left=self.left()
        if not self.pending_reductions():t['barrier_id']=None
        for k in keys:
            # A tranche closed by hand: Soup skips it; the decision stays visible to the followers.
            if left[k]>0 or self.manual_only(k): t['reductions'].setdefault(k,reason)

    def advance(self,now):
        t=self.data['trade']
        if not t or t['phase']=='closed':return
        if t.get('native'):
            if self.native_boot_ms is None:self.native_boot_ms=now
            if t['native']['mode']=='native':return self.advance_native(now)
        if t.get('dispatch_fault'):return
        commands=list(self.data['commands'].values());entry=next(c for c in commands if c['kind']=='entry')
        tp=self.tp1_live()
        if self.tp1_retry().get('awaiting'):
            # A TP1 refused in native mode after the trade fell back to synthetic exits (for example its
            # stop was not confirmed in time): same evidence and bound.
            if t['terminal_reason']:self.tp1_retry_end(now,t['terminal_reason'])
            else:self.tp1_retry_step(now)
            commands=list(self.data['commands'].values())
        elif not t['terminal_reason'] and entry['status'] in ('accepted','filled','cancelled') and not tp and t['alloc']['tp1']>0:
            # No synchronous page scrape or warm-up gate between receipt and TP1.
            self.tp1_send()
            commands=list(self.data['commands'].values())
        if not t['terminal_reason'] and not self.pending_reductions():return
        for c in commands:
            if c['kind'] in ('reduce','flatten') and c['status'] in ('ready','started','accepted','unknown'):
                if c['kind']=='flatten' or not t['terminal_reason']:return
        # Cancel first, then wait for actual cancellation/position evidence.
        cancel=self.data['commands'].get(t['barrier_id'])
        if cancel is None:
            t['barrier_id']=self.command('cancel',reason=t['terminal_reason'] or 'tranche_exit');return
        if cancel['status'] not in ('accepted','unknown','confirmed'):return
        if t['broker_time'] is None or t['broker_time']<cancel['started_at'] or t['open_orders']!=[]:return
        if any(c['status'] not in FINAL|{'superseded'} for c in commands if c['kind'] not in ('cancel','modify_stop')):return
        cancel['status']='confirmed'
        if t['remaining']==0:
            t['phase']='closed';return
        # Market liquidation never waits for a new quote after cancellation proof.
        price=self.reference_price(now)
        if t['terminal_reason']:
            available=t['remaining'];alloc={}
            for k,v in self.left().items():
                used=min(v,available)
                if used:alloc[k]=used
                available-=used
            self.command('flatten',t['remaining'],price,alloc,reason=t['terminal_reason'])
        else:
            alloc={k:self.left()[k] for k in TRANCHES if k in t['reductions'] and self.left()[k]>0}
            if not alloc:t['reductions']={};return
            self.command('reduce',sum(alloc.values()),price,alloc,';'.join(sorted(set(t['reductions'][k] for k in alloc))))

    def native_command(self, kind, quantity, price=None, alloc=None, reason=None, now=None):
        extra = {'native': True, 'created_at': now}
        if kind in ('flatten', 'modify_stop'):
            extra['bridge_kind'] = 'flatten_all' if kind == 'flatten' else 'modify_stop'
        return self.data['commands'][self.command(kind, quantity, price, alloc, reason, extra=extra)]

    def native_stop_state(self):
        """(stop command, working at the broker per evidence, confirmed broker values)."""
        t = self.data['trade']; stop = self.data['commands'][t['native']['stop_id']]
        order = t['broker_orders'].get(stop['id'])
        working = bool(order) and order['status'] in ('working', 'partial') and stop['status'] == 'accepted'
        return stop, working

    def advance_native(self, now):
        """Resting-stop lifecycle. Ordering rules (never a net reversed position):
        - after TP1 fills: one modify to (break-even, remaining runners);
        - partial runner exit: shrink the stop first, prove it from evidence, then market-reduce;
        - a failed stop move after TP1: cancel the stop, prove it gone, continue synthetic
          (today's break-even watcher); a failed cancel exits all;
        - full exit or other failure: flatten_all (liquidation computed at the broker), then
          verify flat with nothing working, retry, and finally block entries and alert."""
        t = self.data['trade']; n = t['native']; pol = n['policy']
        commands = list(self.data['commands'].values())
        entry = self.data['commands'][n['entry_id']]
        stop, working = self.native_stop_state()
        tp = self.tp1_live()
        if (n.get('tp1_retry') or {}).get('awaiting'):
            # A TP1 refused before the bridge saw the entry fill is re-sent on fill evidence, else the normal exit runs.
            if t['terminal_reason']:
                self.tp1_retry_end(now, t['terminal_reason'])
            else:
                self.tp1_retry_step(now)
        elif not t['terminal_reason'] and entry['status'] in ('accepted','filled','cancelled') and not tp and t['alloc']['tp1'] > 0:
            self.tp1_send()  # first attempt: on the entry's HTTP receipt, as before
        # 0. (a) Closed outside Soup: nothing more is sent for this trade (recovery is a reviewed step).
        if n.get('external_close'):
            return
        # 0b. (b) A newer snapshot was refused: Soup's accepted view is stale, and no stop modify,
        # stop cancel or reduce is built or sent from it (start() holds any that are ready).
        # - An external-close candidate (flat, nothing working, all terminal) holds everything,
        #   exit-all included, until the next snapshot decides (a).
        # - Any other refusal is a real unknown: today's timers and exit-all (flatten_all acts on
        #   the broker's own position) carry on.
        # - A hold that outlasts NATIVE_STALE_HOLD_MS without (a) becomes an exit-all.
        refused = n.get('refused')
        if refused and refused.get('expired') is None:
            if now - self.native_since(refused['since']) > NATIVE_STALE_HOLD_MS:
                refused['expired'] = now
                self.native_alert('broker_view_stale', now)
                self.terminal('native_broker_view_stale')
                for c in commands:
                    if c['status'] == 'ready' and c.get('native') and c['kind'] == 'cancel':
                        c['status'] = 'superseded'
            elif refused['candidate']:
                if refused.get('held') is None:
                    refused['held'] = now
                    self.notice('native_hold_stale_broker_view')
                return
        stale = bool(refused) and refused.get('expired') is None
        # 1. The protective stop must be proven working at the broker after the entry fill.
        if not t['terminal_reason'] and not n['stop_confirmed'] and entry['status'] in ('accepted','filled','unknown'):
            plan = t['payload']
            if (working and t['entry_filled'] > 0 and stop['quantity'] == t['entry_filled']
                    and stop['price'] == plan['initial_hard_stop_price']):
                n['stop_confirmed'] = True
            elif stop['status'] in ('cancelled','rejected','superseded') or (
                    entry['started_at'] is not None and now - self.native_since(entry['started_at']) > pol['stop_confirm_ms']):
                self.native_fallback('stop_not_confirmed_working', now)
                return self.advance(now)
            else:
                return
        # 2. Backstop: a stop/break-even level traded through and still open after the bound.
        b = n['backstop']
        if not t['terminal_reason'] and b and now - self.native_since(b['at']) >= pol['backstop_ms'] and t['remaining'] > 0:
            self.native_alert('backstop_' + b['reason'], now)
            self.terminal('backstop_' + b['reason'])
        if t['terminal_reason']:
            return self.native_exit_all(now)
        # 3a. A failed stop move after TP1: the resting stop is being cancelled so this trade can
        # continue on today's synthetic break-even watcher. Synthetic only once fresh evidence
        # shows the stop no longer working; a stop fill meanwhile is terminal (exit-all above).
        cancel = self.data['commands'].get((n.get('stop_cancel') or {}).get('id'))
        if cancel:
            if cancel['status'] in ('ready', 'started'):
                return
            order = t['broker_orders'].get(stop['id'])
            gone = (stop['status'] in ('cancelled', 'rejected') and stop['filled_qty'] == 0
                    and not (order and order['broker_order_id'] in (t['open_orders'] or [])))
            if gone and t['broker_time'] is not None and t['broker_time'] >= self.native_since(cancel['started_at']):
                n['stop_cancel']['confirmed_at'] = now
                self.native_fallback('stop_move_failed:' + n['stop_cancel']['reason'], now)
                return self.advance(now)
            if cancel['status'] == 'rejected':
                return self.native_exit_all(now)  # terminal was set with the rejection
            if now - self.native_since(cancel['started_at']) > pol['cancel_confirm_ms']:
                self.native_alert('stop_cancel_unconfirmed', now)
                self.terminal('native_stop_cancel_unconfirmed')
                return self.native_exit_all(now)
            return
        # 3. One stop modify at a time; confirmed only by broker evidence.
        pending = [c for c in commands if c['kind'] == 'modify_stop' and c['status'] in ('ready','started','accepted','unknown')]
        if pending:
            m = pending[-1]
            if (working and t['broker_time'] is not None and m['started_at'] is not None and t['broker_time'] >= m['started_at']
                    and stop['quantity'] == m['quantity'] and stop['price'] == m['price']):
                m['status'] = 'confirmed'
            elif m['started_at'] is not None and now - self.native_since(m['started_at']) > pol['modify_confirm_ms']:
                self.native_alert('stop_modify_unconfirmed', now)
                self.native_modify_failed('stop_modify_unconfirmed', now)
                return self.advance_native(now)
            else:
                return
        # 4. A market reduce in flight (the stop was shrunk for it first).
        for c in commands:
            if c['kind'] == 'reduce' and c['status'] in ('ready','started','accepted','unknown'):
                since = c['started_at'] if c['started_at'] is not None else c.get('created_at')
                if now - self.native_since(since) > pol['reduce_confirm_ms']:
                    self.native_alert('reduce_unconfirmed', now)
                    self.terminal('native_reduce_unconfirmed')
                    return self.native_exit_all(now)
                return
        if stop['status'] in ('filled','cancelled','rejected') or not working:
            if t['remaining'] > 0 and stop['status'] != 'accepted':
                # Lost without Soup asking (the stop fill itself is handled in broker()).
                self.native_fallback('stop_no_longer_working', now)
                return self.advance(now)
            return
        if t['remaining'] <= 0 or stale:
            if t['remaining'] == 0 and not stale and self.manual_external():
                # The runners were closed by hand and TP1 took the last contract: the stop is orphaned.
                self.terminal(MANUAL_FLAT_REASON)
                return self.native_exit_all(now)
            return
        # 5. Desired stop: remaining runners minus any pending partial exit, at break-even once TP1 filled.
        left = self.left()
        reserved = sum(left[k] for k in t['reductions'] if left[k] > 0)
        reasons = ';'.join(sorted(set(t['reductions'][k] for k in t['reductions'] if left[k] > 0)))
        if reserved >= t['remaining']:
            self.terminal(reasons)  # the planned exit takes every remaining contract
            return self.native_exit_all(now)
        want_qty = t['remaining'] - reserved
        # A stop price set by hand after TP1 stays; only its quantity follows the position.
        kept = (self.manual_state().get('stop') or {}).get('keep')
        want_price = t['payload']['break_even_price'] if t['tp1_done'] and not kept else stop['price']
        if (stop['quantity'] - stop['filled_qty'], stop['price']) != (want_qty, want_price):
            why = 'break_even' if want_price != stop['price'] else 'resize'
            self.native_command('modify_stop', want_qty, want_price, reason=why, now=now)
            return
        if reserved:
            alloc = {k: left[k] for k in TRANCHES if k in t['reductions'] and left[k] > 0}
            self.native_command('reduce', sum(alloc.values()), None, alloc, reasons, now=now)

    def native_modify_failed(self, reason, now):
        """After the TP1 fill a failed stop move does not flatten. Cancel the resting
        stop (it may still be the initial stop for the full quantity), prove it is gone from fresh
        broker evidence, then continue on today's synthetic break-even watcher. Before TP1 (a
        resize after a partial TP1 fill) the trade still exits all."""
        t = self.data['trade']; n = t['native']
        if t['terminal_reason'] or n.get('stop_cancel'):
            return
        if not t['tp1_done']:
            self.terminal('native_' + reason)
            return
        # Only one modify is ever pending; the cancel branch in advance_native takes over from here.
        c = self.native_command('cancel', 0, None, {}, 'stop_move_failed:' + reason, now=now)
        n['stop_cancel'] = {'id': c['id'], 'reason': reason, 'at': now}

    def native_closable(self):
        t = self.data['trade']
        if t['remaining'] != 0 or t['open_orders'] != [] or t['broker_time'] is None:
            return False
        for c in self.data['commands'].values():
            if c['kind'] in ('cancel', 'modify_stop') or c['status'] in FINAL | {'superseded'}:
                continue
            if (c.get('native') and c['kind'] in ('reduce', 'flatten') and c['status'] == 'unknown'
                    and t['broker_time'] >= self.native_since(c['started_at'])):
                # The bridge publishes nothing while its own record of a command is uncertain, so
                # a later snapshot without this order means it was never placed.
                continue
            return False
        return True

    def native_exit_all(self, now):
        """Exit-all: flatten_all, verify flat with nothing working from fresh evidence, retry;
        after the attempts are used up, record the failure (the service blocks entries and alerts)."""
        t = self.data['trade']; n = t['native']; pol = n['policy']
        flat = [c for c in self.data['commands'].values() if c['kind'] == 'flatten' and c.get('native')]
        last = flat[-1] if flat else None
        if last and last['status'] in ('ready', 'started'):
            return
        if (last is None or (t['broker_time'] is not None and t['broker_time'] >= last['started_at'])) and self.native_closable():
            t['phase'] = 'closed'
            return
        if last:
            # Wait for evidence of the last attempt (the bridge holds snapshots while a cancelled
            # order settles). Only a refused attempt is retried sooner; never a tight loop.
            window = min(pol['exit_verify_ms'], 1000) if last['status'] == 'rejected' else pol['exit_verify_ms']
            if now - self.native_since(last['started_at']) < window:
                return
        if n['exit_attempts'] >= pol['max_exit_attempts']:
            if last and now - self.native_since(last['started_at']) < pol['exit_verify_ms']:
                return
            if not n['failed']:
                n['failed'] = {'at': now, 'reason': t['terminal_reason'], 'remaining': t['remaining'],
                               'open_orders': list(t['open_orders'] or [])}
                self.native_alert('exit_all_unverified', now)
            return
        n['exit_attempts'] += 1
        self.native_command('flatten', t['remaining'], None, {}, t['terminal_reason'], now=now)

    def native_order_values(self, c, order, filled):
        """The broker owns a resting stop's size/price (modifies, auto-shrink) and a liquidation's size."""
        q = order.get('quantity')
        require(type(q) is int and type(filled) is int and max(c['filled_qty'], filled) <= q <= self.data['trade']['payload']['quantity'],
                'native order quantity')
        c['quantity'] = q
        if c['kind'] == 'stop':
            price = order.get('stop_price')
            require(number(price) and price > 0, 'native stop price')
            c['price'] = price

    def native_backstop(self, reason, at):
        t = self.data['trade']
        if t['terminal_reason'] or t['phase'] == 'closed' or t['native']['backstop']:
            return
        t['native']['backstop'] = {'reason': reason, 'at': at}

    def reference_price(self,now):
        q=self.data['last_quote']
        if q and 0<=now-q['time']<=2000:return q['price']
        t=self.data['trade'];q=t.get('trigger_reference')
        if q and 0<=now-q['time']<=5000:return q['price']
        self.notice('exit_uses_last_known_reference; broker market fill determines execution')
        return q['price'] if q else t['payload']['entry_price']

    def facts(self,p,now):
        t=self.data['trade']
        if not t or t['phase']=='closed':return 'facts_no_active_trade'
        if p['trade_id']!=t['id']:return 'facts_other_trade'
        require(p['strategy_snapshot']==t['payload']['strategy_snapshot'],'active-trade settings changed; retain old alert until flat')
        require(p['execution_context']['chart_seconds']==t['payload']['execution_context']['chart_seconds'],'active interval mismatch')
        at=p['execution_context']['decision_time'];opened=t['payload']['execution_context']['decision_time']
        if at<opened:return 'facts_before_entry'
        require(number(p.get('price')) and p['price']>0,'fact reference price')
        t['trigger_reference']={'time':at,'price':p['price']}
        s=self.settings()
        ordered=sorted(p['facts'],key=lambda f:('counter_ifvg','progress_fail','stop_bias_flip','parent_sweep','bias_flip').index(f['kind']))
        for f in ordered:
            if f['close_side']!=t['side']:continue
            kind=f['kind']
            # Delayed Pine delivery must use the action's time, not the phase
            # at delivery. A pre-TP1 failure remains a failure after its fill
            # report, while a post-TP1 guard must not close a running trade.
            pre_tp1=not t['tp1_done'] or at<t.get('tp1_filled_at',opened)
            if pre_tp1:
                if kind=='counter_ifvg' and s['use_counter_ifvg_5s'] and f['inversion_time']>t['payload']['timestamp']:self.terminal('counter_ifvg')
                if kind=='progress_fail' and s['use_progress_fail_rule'] and f['trade_id']==t['id'] and at-opened>=s['progress_fail_checkpoint_seconds']*1000 and f['adverse_ratio']>=s['progress_fail_adverse_ratio']:self.terminal('progress_fail')
                if kind=='stop_bias_flip':self.terminal('stop_bias_flip')
                if kind in ('parent_sweep','bias_flip') and not t['terminal_reason'] and not t['tp1_done']:
                    # Preserve an action that arrives before a delayed broker
                    # report of a fill. Its event time determines eligibility.
                    t.setdefault('pending_runner_facts',{})[kind]={'kind':kind,'at':at,'price':p['price']}

            else:
                if kind=='parent_sweep' and at>=t['runner_not_before']:self.reduce(('parent',),'parent_sweep')
                if kind=='bias_flip' and at>=t.get('tp1_filled_at',opened):self.reduce(('bias',),'bias_flip')
        self.reconcile_runner_facts()
        return 'facts_processed'

    def reconcile_runner_facts(self):
        t=self.data['trade'];pending=t.get('pending_runner_facts',{})
        if t['phase']=='closed' or t['terminal_reason']:
            pending.clear();return
        for kind,fact in list(pending.items()):
            if t['broker_time'] is None or t['broker_time']<fact['at']:continue
            eligible=t['tp1_done'] and fact['at']>=t.get('tp1_filled_at',t['payload']['execution_context']['decision_time'])
            if kind=='parent_sweep':eligible=eligible and fact['at']>=t['runner_not_before']
            if eligible:
                self.reduce(('parent' if kind=='parent_sweep' else 'bias',),kind)
                t['trigger_reference']={'time':fact['at'],'price':fact['price']}
            else:
                self.notice('Pine_runner_action_precedes_verified_TP1_phase:'+kind)
            del pending[kind]

    def retain_price_crossing(self, channel, reason, at, eligible_at, price):
        """Remember an observed crossing, not a hypothetical fill.

        Keep the newest crossing for each channel/reason. Eligibility is a
        lower time bound, so a newer crossing is sufficient proof whenever an
        older one would qualify. If its timestamp is beyond a broker report,
        keep it until a later report instead of applying it prematurely.
        The three slots bound state growth during a broker outage.
        """
        t=self.data['trade']
        if t['terminal_reason'] or t['phase']=='closed':return
        key=channel+':'+reason
        crossing={'reason':reason,'at':at,'eligible_at':eligible_at,'price':price}
        saved=t.setdefault('pending_price_crossings',{})
        previous=saved.get(key)
        if previous is None or eligible_at>previous['eligible_at']:
            saved[key]=crossing

    def reconcile_price_crossings(self):
        t=self.data['trade'];pending=t.get('pending_price_crossings',{})
        if t['phase']=='closed' or t['terminal_reason']:
            pending.clear();return
        if not t['tp1_done'] or t['broker_time'] is None:return
        # Match the normal bar path: protective break-even before CBDR target.
        for key,crossing in sorted(list(pending.items()),key=lambda item:(item[1]['reason']!='break_even',item[1]['at'])):
            if crossing['at']>t['broker_time']:continue
            if crossing['reason']=='break_even' and self.native_active():
                # The resting break-even stop replaces the synthetic reduce; this is its backstop.
                # Only a crossing after the TP1 fill counts (the stop moves on that fill).
                if crossing['eligible_at']>=t['tp1_filled_at']:self.native_backstop('break_even',crossing['at'])
            elif crossing['eligible_at']>=t['runner_not_before']:
                keys=('parent','eod') if crossing['reason']=='break_even' else ('eod',)
                self.reduce(keys,crossing['reason'])
                t['trigger_reference']={'time':crossing['at'],'price':crossing['price']}
            del pending[key]

    def quote(self,p,now):
        require(p.get('symbol')==self.config['ticker'],'quote symbol')
        at=p.get('time');price=p.get('price')
        require(type(at) is int and -1000<=now-at<=2000 and number(price) and price>0,'fresh positive quote')
        last=self.data['last_quote'];require(not last or at>last['time'],'quote must advance')
        self.data['last_quote']={'time':at,'price':price}
        t=self.data['trade']
        if not t or t['phase']=='closed' or at<t['payload']['execution_context']['decision_time']:return 'quote_recorded'
        s=self.settings();plan=t['payload'];sign=1 if t['side']=='long' else -1
        native=self.native_active()  # resting broker stop: the synthetic levels become a delayed backstop
        if not t['tp1_done'] and sign*(price-plan['break_even_price'])<=0:
            self.retain_price_crossing('quote','break_even',at,at,price)
        if s['use_catastrophe_stop'] and sign*(price-plan['catastrophe_stop_price'])<=0:self.terminal('catastrophe_stop')
        elif not t['tp1_done'] and s['use_initial_hard_stop'] and sign*(price-self.native_backstop_level(plan['initial_hard_stop_price']))<=0:
            if native:self.native_backstop(self.native_backstop_reason('initial_hard_stop'),at)
            else:self.terminal('initial_hard_stop')
        elif not t['tp1_done'] and sign*(price-plan['tp1_price'])>=0:
            if t['alloc']['tp1']==0:self.virtual_tp1_touch('quote',at,at)
            else:t['reconcile_requested']=True
        elif native and t['tp1_done'] and at>=t['tp1_filled_at'] and sign*(price-self.native_backstop_level(plan['break_even_price']))<=0:self.native_backstop(self.native_backstop_reason('break_even'),at)
        elif not native and t['tp1_done'] and at>=t['runner_not_before'] and sign*(price-plan['break_even_price'])<=0:self.reduce(('parent','eod'),'break_even')
        return 'quote_processed'

    def virtual_tp1_touch(self,channel,at,eligible_at):
        """Zero-TP1 plans still need an observed target after actual entry.

        A completed bar proves a post-entry touch only when its whole interval
        follows the entry fill. Its opening time preserves Pine's bar grace.
        Preserve touch order while the full entry report is delayed. If the
        bounded history fills, close protectively rather than guess its phase.
        """
        t=self.data['trade']
        if t['tp1_done'] or t['terminal_reason'] or t['alloc']['tp1']!=0:return
        pending=t.setdefault('pending_virtual_tp1',[])
        touch={'channel':channel,'at':at,'eligible_at':eligible_at}
        if touch not in pending:
            if len(pending)>=4096:
                self.notice('virtual_tp1_history_full; entry verification unavailable')
                self.terminal('virtual_tp1_history_full');pending.clear();return
            pending.append(touch)
        self.reconcile_virtual_tp1()

    def reconcile_virtual_tp1(self):
        t=self.data['trade'];pending=t.get('pending_virtual_tp1',[])
        if t['tp1_done'] or t['terminal_reason'] or t['phase']=='closed':
            pending.clear();return
        if t['alloc']['tp1']!=0 or t['entry_filled']!=self.execution_quantity():return
        entry=next(c for c in self.data['commands'].values() if c['kind']=='entry')
        fill_time=t['broker_orders'][entry['id']]['fill_time']
        for touch in sorted(pending,key=lambda item:item['eligible_at']):
            if touch['eligible_at']>=fill_time:
                self.arm_tp1(touch['eligible_at']);pending.clear();return
        pending.clear()

    def arm_tp1(self,fill_time):
        t=self.data['trade'];size=t['payload']['execution_context']['chart_seconds']*1000
        t['tp1_done']=True;t['tp1_filled_at']=fill_time;t['runner_not_before']=(fill_time//size+2)*size
        if not t['terminal_reason']:t['phase']='running'

    def tick(self,now):
        """Configured EOD fallback without a Pine message or a new-entry delay."""
        before=canonical(self.data);t=self.data['trade']
        if t and t['phase']!='closed':
            s=self.settings();size=t['payload']['execution_context']['chart_seconds']*1000
            bar_open=now//size*size-size
            if bar_open>=t['payload']['execution_context']['decision_time'] and s['close_all_eod'] and in_session(bar_open,s['eod_session']):self.terminal('eod')
            self.advance(now)
        if canonical(self.data)!=before:self.save()

    def bar(self,p,now):
        require(p.get('symbol')==self.config['ticker'],'bar symbol')
        a,b=p.get('open_time'),p.get('close_time')
        require(type(a) is int and type(b) is int and b-a in (1000,5000) and a%(b-a)==0 and 0<=now-b<=15000,'completed aligned fresh bar')
        for k in ('open','high','low','close'):require(number(p.get(k)) and p[k]>0,'bar price')
        require(p['low']<=min(p['open'],p['close'])<=max(p['open'],p['close'])<=p['high'],'bar OHLC')
        last=self.data['last_bar'];require(not last or b>last['close_time'],'bar must advance')
        self.data['last_bar']=copy.deepcopy(p)
        t=self.data['trade']
        if not t or t['phase']=='closed':return 'bar_recorded'
        plan=t['payload'];require(b-a==plan['execution_context']['chart_seconds']*1000,'bar interval mismatch')
        if a<plan['execution_context']['decision_time']:return 'bar_before_entry'
        t['trigger_reference']={'time':b,'price':p['close']}
        s=self.settings();long=t['side']=='long';native=self.native_active()
        stop_touch=lambda level: p['low']<=level if long else p['high']>=level
        target_touch=lambda level: p['high']>=level if long else p['low']<=level
        if not t['tp1_done']:
            if stop_touch(plan['break_even_price']):
                self.retain_price_crossing('bar','break_even',b,a,p['close'])
            if s['use_cbdr_tp_for_eod_runner'] and plan['runner_cbdr_target']>0 and target_touch(plan['runner_cbdr_target']):
                self.retain_price_crossing('bar','cbdr',b,b,p['close'])
        if s['use_catastrophe_stop'] and stop_touch(plan['catastrophe_stop_price']):self.terminal('catastrophe_stop')
        elif not t['tp1_done'] and s['use_initial_hard_stop'] and stop_touch(self.native_backstop_level(plan['initial_hard_stop_price'])):
            if native:self.native_backstop(self.native_backstop_reason('initial_hard_stop'),b)
            else:self.terminal('initial_hard_stop')
        elif native and t['tp1_done'] and a>=t['tp1_filled_at'] and stop_touch(self.native_backstop_level(plan['break_even_price'])):self.native_backstop(self.native_backstop_reason('break_even'),b)
        elif not native and t['tp1_done'] and a>=t['runner_not_before'] and stop_touch(plan['break_even_price']):self.reduce(('parent','eod'),'break_even')
        elif not t['tp1_done'] and target_touch(plan['tp1_price']):
            if t['alloc']['tp1']==0:self.virtual_tp1_touch('bar',b,a)
            else:t['reconcile_requested']=True
        if s['close_all_eod'] and in_session(a,s['eod_session']):self.terminal('eod')
        if not t['terminal_reason'] and t['tp1_done'] and b>=t['runner_not_before'] and s['use_cbdr_tp_for_eod_runner'] and plan['runner_cbdr_target']>0 and target_touch(plan['runner_cbdr_target']):self.reduce(('eod',),'cbdr')
        return 'bar_processed'

    def broker(self,p,now):
        require(p.get('account_id')==self.config['account_id'] and p.get('symbol')==self.config['ticker'],'broker account/symbol')
        t=self.data['trade'];require(t and p.get('trade_id')==t['id'],'broker trade correlation')
        seq=p.get('sequence');at=p.get('observed_at')
        require(type(seq) is int and seq>self.data['broker_seq'] and type(at) is int and -1000<=now-at<=15000,'broker sequence/freshness')
        require(t['broker_time'] is None or at>=t['broker_time'],'broker observation time regressed')
        require(p.get('side')==t['side'],'broker side')
        require(type(p.get('position_qty')) is int and p['position_qty']>=0,'broker quantity')
        self.quantity_guard()
        # No size cap (Decrypt OS): the conservation check below proves fills explain the position.
        require(isinstance(p.get('orders'),list) and isinstance(p.get('open_orders'),list),'broker order evidence')
        seen=set()
        for order in p['orders']:
            require(isinstance(order,dict),'broker order object')
            cid=order.get('command_id');require(isinstance(cid,str) and cid in self.data['commands'] and cid not in seen,'unknown/duplicate broker command')
            seen.add(cid);c=self.data['commands'][cid]
            require(c['kind'] not in ('cancel','modify_stop') and c['status'] not in ('ready','superseded','attached'),'unsent broker order')
            status=order.get('status');filled=order.get('filled_qty')
            require(isinstance(status,str) and status in FINAL|{'working','partial'},'broker order status')
            if c.get('native') and c['kind'] in ('stop','flatten'):
                previous=c['price'];self.native_order_values(c,order,filled)
                if c['kind']=='stop' and status in ('working','partial'):self.manual_stop_moved(c,previous,p,now)
            require(type(filled) is int and c['filled_qty']<=filled<=c['quantity'],'cumulative fills')
            require(status!='filled' or filled==c['quantity'],'filled quantity mismatch')
            require(status!='rejected' or filled==0,'rejected filled order')
            require(c['status'] not in FINAL or status==c['status'],'terminal order regression')
            require(isinstance(order.get('broker_order_id'),str) and order['broker_order_id'],'broker order identity')
            previous=t['broker_orders'].get(cid)
            require(not previous or previous['broker_order_id']==order['broker_order_id'],'broker order identity changed')
            if previous and previous['status'] in FINAL:
                require(filled==previous['filled_qty'],'terminal broker fills changed; reconcile adapter evidence')
            if filled:
                require(type(order.get('fill_time')) is int and t['payload']['execution_context']['decision_time']<=order['fill_time']<=at,'broker fill time')
                if previous and previous['filled_qty']:
                    require(order['fill_time']>=previous['fill_time'],'broker fill time regressed')
                    if filled==previous['filled_qty']:
                        require(order['fill_time']==previous['fill_time'],'unchanged fills require stable fill time')
            c['filled_qty']=filled;c['status']=status if status in FINAL else 'accepted'
            t['broker_orders'][cid]=copy.deepcopy(order)
        require(all(isinstance(x,str) for x in p['open_orders']) and len(set(p['open_orders']))==len(p['open_orders']),'open order identities')
        # Every dispatched order must be represented before flat/cancel evidence is authoritative.
        dispatched=[c for c in self.data['commands'].values() if c['kind'] not in ('cancel','modify_stop') and c['status'] not in ('ready','superseded','rejected','attached')
                    and not c.get('no_order') and not (c.get('native') and c['kind'] in ('reduce','flatten') and c['status']=='unknown')]
        require(all(c['id'] in t['broker_orders'] for c in dispatched),'incomplete broker command inventory')
        expected_open=sorted(o['broker_order_id'] for o in t['broker_orders'].values() if o['status'] in ('working','partial'))
        identities=[o['broker_order_id'] for o in t['broker_orders'].values()]
        require(len(identities)==len(set(identities)),'broker order identity reused across commands')
        require(sorted(p['open_orders'])==expected_open,'open-order inventory mismatch')
        commands=list(self.data['commands'].values());entry=next(c for c in commands if c['kind']=='entry')
        refused=self.tp1_refused()  # refused TP1 commands placed nothing
        remaining=entry['filled_qty']-sum(c['filled_qty'] for c in commands if c['kind'] in ('tp1_limit','reduce','flatten','stop') and c['id'] not in refused)-self.manual_external()
        if t.get('native') and not (remaining>=0 and remaining==p['position_qty']):
            self.refusal_evidence=self.native_conservation_evidence(p,entry,commands,remaining)
            if self.manual_confirmed(self.refusal_evidence):
                # A fully explained reduction outside Soup, confirmed: accept this snapshot.
                self.manual_book(self.refusal_evidence,now);remaining=p['position_qty']
        require(remaining>=0 and remaining==p['position_qty'],CONSERVATION_MISMATCH)
        if t.get('native'):t['native'].pop('refused',None)
        t['entry_filled']=entry['filled_qty'];t['remaining']=remaining
        t['broker_time']=at;t['open_orders']=list(p['open_orders']);self.data['broker_seq']=seq
        tp=self.tp1_live()
        if not t['tp1_done'] and tp and tp['status']=='filled' and entry['filled_qty']==self.execution_quantity():
            self.arm_tp1(t['broker_orders'][tp['id']]['fill_time'])
        if t.get('native'):
            stop=self.data['commands'][t['native']['stop_id']]
            if stop['filled_qty']>0 and not t['terminal_reason']:
                # The resting stop (initial or break-even) fired: exit-all follows.
                moved=self.manual_state().get('stop') and stop['price']!=t['payload']['initial_hard_stop_price']
                self.terminal('broker_stop_'+('break_even' if stop['price']==t['payload']['break_even_price'] else 'manual' if moved else 'initial'))
        if entry['status'] in ('cancelled','rejected') and entry['filled_qty']<entry['quantity']:self.terminal('entry_incomplete')
        if tp and tp['status'] in ('rejected','cancelled') and not t['terminal_reason']:
            self.terminal('tp1_order_'+tp['status'])
        for c in commands:
            if c['kind'] in ('reduce','flatten') and not c.get('native') and c['status']=='rejected' and t.get('dispatch_fault')!=c['id']:
                t['dispatch_fault']=c['id'];self.notice('broker_order_rejected:'+c['kind'])
        for c in commands:
            if c['kind']=='reduce' and c['status']=='filled':
                for k in c['alloc']:
                    if self.left()[k]==0:t['reductions'].pop(k,None)
                if c['reason'] and 'cbdr' in c['reason'] and self.settings()['pause_after_cbdr_tp']:
                    self.data['day_lock']=datetime.fromtimestamp(t['broker_orders'][c['id']]['fill_time']/1000,NY).date().isoformat()
            elif c.get('native') and c['kind']=='flatten' and c['status']=='filled' and c['reason'] and 'cbdr' in c['reason'] and self.settings()['pause_after_cbdr_tp']:
                self.data['day_lock']=datetime.fromtimestamp(t['broker_orders'][c['id']]['fill_time']/1000,NY).date().isoformat()
        if remaining==0 and not p['open_orders'] and entry['status'] in FINAL and all(c['status'] in FINAL|{'superseded'} for c in commands if c['kind'] not in ('cancel','modify_stop')):
            t['phase']='closed'
        if t['terminal_reason'] or t['phase']=='closed':self.tp1_retry_end(now,t['terminal_reason'] or 'closed')
        self.reconcile_virtual_tp1()
        self.reconcile_runner_facts()
        self.reconcile_price_crossings()
        return 'broker_reconciled'
