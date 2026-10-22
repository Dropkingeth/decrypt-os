"""Read the bridge + Soup config tree into one model, and check proposed configs.

Sources (all inside the profile roots):
- tvbridge/logins.json and each login's daemon config (main account + followers)
- tvbridge/firing.json: the Main webhook's leader and firing order
- soup-state/pairing.json, quantity-policy.json, bridge-binding.json, control.json
- admin/webhooks.json: webhook registry (names, strategy, planned lanes, webhooks that are off)
"""
import copy
import hashlib
import importlib
import json
import os
import re
import secrets
import sys
import time

from tree import ADMIN_MARK, dump_json, file_sha, json_text_like, load_json, read_switch

KEY_RE = re.compile(r'^[a-z][a-z0-9-]{0,31}\Z')
STREAM_RE = re.compile(r'^[A-Za-z0-9_-]{1,48}\Z')
PROP_PROFILES = ('cross-account-window', 'unrestricted')   # the bridge's own list; apex is always refused
REGISTRY_SCHEMA = 'decrypt.tvbridge-admin.webhooks.v1'
LANE_PORT_BASE = 8800
STREAM_ALPHABET = 'abcdefghjkmnpqrstuvwxyz23456789'  # no 0/o/1/l/i: easy to read and retype


def legacy_max_contracts_problem(value):
    """`max_contracts` is retired: the strategy alert sets the size and there is no contract limit here.
    Absent, or a legacy positive whole number of any size (ignored): None. Anything else is a malformed
    legacy value the bridge refuses to start with: a plain sentence."""
    if value is None or (type(value) is int and value >= 1):
        return None
    return 'max_contracts is retired (the strategy alert sets the size); remove it from the config.'


class Invalid(ValueError):
    """A proposed config breaks a rule. The message is a plain sentence for the page."""


def new_stream_id():
    """Random stream ID, about 78 bits, Pine-safe ([A-Za-z0-9_-], 19 chars)."""
    raw = ''.join(secrets.choice(STREAM_ALPHABET) for _ in range(16))
    return '-'.join(raw[i:i + 4] for i in range(0, 16, 4))


def stream_strength(sid):
    return 'strong' if isinstance(sid, str) and len(sid) >= 16 and not sid.isdigit() else 'weak'


def stream_problem(sid):
    """None for a stream ID this page accepts, else a plain sentence. Weak IDs are refused, not warned about:
    the stream ID is what keeps other people's alerts out of a public webhook path."""
    if not isinstance(sid, str) or not STREAM_RE.match(sid):
        return 'A stream ID may use letters, digits, underscore and hyphen, up to 48 characters.'
    if stream_strength(sid) != 'strong':
        return 'A stream ID needs at least 16 characters and not only digits. Leave it empty to get a random one.'
    return None


def new_webhook_path(wid, change_id):
    """Public path of a new webhook: its id plus a part derived from the change id (the same at preview and
    confirm). No Linux user name in it. The stream ID, not the path, is what authenticates an alert."""
    return f'/webhook/{wid}-{hashlib.sha256(str(change_id).encode()).hexdigest()[:12]}'


def slug(text, limit=24):
    s = re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')
    s = s[:limit].strip('-')
    if not s or not s[0].isalpha():
        s = ('w-' + s)[:limit].strip('-')
    return s


def mask(name):
    return ('…' + str(name)[-4:]) if name else None


class State:
    """Snapshot of every config doc, keyed by absolute path, plus derived accounts and webhooks."""

    def __init__(self, profile):
        self.p = profile
        self.docs = {}       # path -> parsed JSON (deep copies are made by Draft)
        self.drift = []
        self._load()

    # ---------- loading ----------
    def _doc(self, path, required=True):
        if path in self.docs:
            return self.docs[path]
        try:
            obj, _, _ = load_json(path)
        except FileNotFoundError:
            if required:
                raise
            return None
        self.docs[path] = obj
        return obj

    def _load(self):
        p = self.p
        self.logins_path = p.tb('logins.json')
        self.firing_path = p.tb('firing.json')
        self.pairing_path = p.soup('pairing.json')
        self.control_path = p.soup('control.json')
        self.registry_path = p.st('webhooks.json')
        self.logins = self._doc(self.logins_path)['logins']
        self.firing = self._doc(self.firing_path)
        self.pairing = self._doc(self.pairing_path, required=False) or {}   # absent until Soup is paired
        self.control = self._doc(self.control_path, required=False) or {}
        self.qpolicy = self._doc(p.soup('quantity-policy.json'), required=False) or {}
        self.binding = self._doc(p.soup('bridge-binding.json'), required=False) or {}
        self.login_cfg = {}
        for lg in self.logins:
            self.login_cfg[lg['id']] = (lg['config'], self._doc(lg['config']))
        self.registry = self._doc(self.registry_path, required=False)
        if self.registry is None:
            self.registry = self.bootstrap_registry()
        self._derive()

    def bootstrap_registry(self):
        return {'schema': REGISTRY_SCHEMA, 'webhooks': [{
            'id': 'main', 'name': 'Main', 'strategy': self.p.strategies[0]['id'] if self.p.strategies else None,
            'path': self.main_path(), 'state': 'live', 'created_at': time.time()}]}

    def main_path(self):
        """Webhook path: the first /webhook/<name> in the profile's optional `tunnel_config` file, else
        /webhook/soup."""
        cfg = self.p.data.get('tunnel_config')
        if cfg and os.path.exists(cfg):
            with open(cfg) as f:
                m = re.search(r'webhook/([A-Za-z0-9_-]+)', f.read())
            if m:
                return '/webhook/' + m.group(1)
        return '/webhook/soup'

    def primary_login(self):
        for lg in self.logins:
            if lg.get('primary'):
                return lg['id']
        return self.logins[0]['id']

    def _derive(self):
        firing_accts = self.firing.get('accounts', {})
        order = list(self.firing.get('firing_order', []))
        main_key = order[0] if order else None
        primary = self.primary_login()
        self.accounts = {}
        for lg in self.logins:
            path, cfg = self.login_cfg[lg['id']]
            base = dict(login=lg['id'], login_label=lg.get('label', lg['id']), cfg_path=path,
                        bridge_url=lg.get('bridge_url'))
            if cfg.get('account_name'):
                key = main_key if (lg['id'] == primary and main_key) else f'main-{lg["id"]}'
                fa = firing_accts.get(key, {})
                self.accounts[key] = dict(base, key=key, daemon_role='main',
                                          label=fa.get('label') or cfg.get('soup_account_id') or key,
                                          account_name=cfg.get('account_name'), account_id=cfg.get('account_id'),
                                          legacy_max_contracts=cfg.get('max_contracts'),
                                          prop_profile=fa.get('prop_profile', 'unrestricted'),
                                          native_exits=bool(cfg.get('bracket_orders_enabled')),
                                          daemon_enabled=True, kill_switch_file=cfg.get('kill_switch_file'),
                                          journal_dir=cfg.get('journal_dir'))
            for f in cfg.get('followers') or []:
                key = f.get('key')
                if key in self.accounts:
                    self.drift.append(f'Account key {key} is configured on more than one login.')
                    continue
                self.accounts[key] = dict(base, key=key, daemon_role='follower', label=f.get('label') or key,
                                          account_name=f.get('account_name'), account_id=f.get('account_id'),
                                          legacy_max_contracts=f.get('max_contracts'),
                                          prop_profile=f.get('prop_profile'),
                                          native_exits=bool(f.get('native_exits_enabled')),
                                          daemon_enabled=f.get('enabled', True),
                                          kill_switch_file=f.get('kill_switch_file'), journal_dir=f.get('journal_dir'))
        # webhook membership
        self.webhooks = []
        member = {}
        for w in self.registry.get('webhooks', []):
            if w['id'] == 'main':
                leader, followers = main_key, order[1:]
                disabled = [k for k in followers if firing_accts.get(k, {}).get('enabled', True) is False]
                stream, ticker = self.pairing.get('stream_id'), self.pairing.get('ticker')
            else:
                leader, followers = w.get('leader'), list(w.get('followers', []))
                disabled = list(w.get('disabled', []))
                stream, ticker = w.get('stream_id'), w.get('ticker')
            wh = dict(w, leader=leader, followers=followers, disabled=disabled, stream_id=stream, ticker=ticker)
            self.webhooks.append(wh)
            for k in [leader] + followers:
                if not k:
                    continue
                if k in member:
                    self.drift.append(f'{k} is in two webhooks ({member[k]} and {w["id"]}).')
                member[k] = w['id']
                if k not in self.accounts:
                    self.drift.append(f'Webhook {w.get("name") or w["id"]} lists {k}, which no login config has.')
        self.member = member
        # drift between the four places that pin the Main leader
        if main_key and main_key in self.accounts:
            label = self.accounts[main_key]['label']
            pins = {'Soup pairing': self.pairing.get('account_id'),
                    'quantity policy': self.qpolicy.get('account_id'),
                    'bridge binding': self.binding.get('account_id'),
                    'bridge config': self.login_cfg[primary][1].get('soup_account_id')}
            for where, val in pins.items():
                if val is not None and val != label:
                    self.drift.append(f'Main leader is {label} in firing.json but {val} in the {where}.')
        for k, fa in firing_accts.items():
            a = self.accounts.get(k)
            if a and fa.get('role') == 'follower' and fa.get('login', primary) != a['login']:
                self.drift.append(f'firing.json puts {k} on login {fa.get("login", primary)}, '
                                  f'but it is configured on login {a["login"]}.')

    # ---------- queries ----------
    def webhook(self, wid):
        for w in self.webhooks:
            if w['id'] == wid:
                return w
        return None

    def account(self, key):
        a = self.accounts.get(key)
        if not a:
            raise Invalid(f'There is no account "{key}".')
        return a

    def role(self, key):
        wid = self.member.get(key)
        if not wid:
            return 'unassigned', None
        w = self.webhook(wid)
        return ('leader' if w['leader'] == key else 'follower'), w

    def switch_path(self, key):
        return self.accounts[key]['kill_switch_file']

    def fingerprint(self):
        return {path: file_sha(path) for path in sorted(self.docs)}


class Draft:
    """Editable copies of config docs. `ops()` turns the edits into file operations."""

    def __init__(self, state):
        self.s = state
        self.docs = {path: copy.deepcopy(doc) for path, doc in state.docs.items()}
        if state.registry_path not in self.docs:
            self.docs[state.registry_path] = copy.deepcopy(state.registry)
        self.extra = []   # mkdir / rename / write ops that are not JSON docs

    def doc(self, path):
        return self.docs[path]

    @property
    def firing(self):
        return self.docs[self.s.firing_path]

    @property
    def registry(self):
        return self.docs[self.s.registry_path]

    def login_cfg(self, login):
        return self.docs[self.s.login_cfg[login][0]]

    def reg_webhook(self, wid):
        for w in self.registry['webhooks']:
            if w['id'] == wid:
                return w
        raise Invalid(f'There is no webhook "{wid}".')

    def follower_entry(self, key):
        a = self.s.account(key)
        for f in self.login_cfg(a['login']).get('followers') or []:
            if f.get('key') == key:
                return f
        return None

    def ops(self):
        out = []
        for path, doc in self.docs.items():
            before = self.s.docs.get(path)
            if path == self.s.registry_path and before is None and not os.path.exists(path):
                out.append({'path': path, 'action': 'write', 'content': dump_json(doc)})
                continue
            if before is None or json.dumps(before, sort_keys=False) != json.dumps(doc, sort_keys=False):
                out.append({'path': path, 'action': 'write', 'content': json_text_like(path, doc)})
        return out + self.extra


# ---------- validation of a draft (in-house rules + the bridge's own validators) ----------

def _bridge_modules(profile):
    root = profile.data.get('bridge_release')
    if not root or not os.path.isdir(os.path.join(root, 'tvbridge')):
        return None, None, 'bridge validators unavailable (no bridge_release in the profile)'
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        return (importlib.import_module('tvbridge.firing'), importlib.import_module('tvbridge.followers'), None)
    except Exception as exc:  # noqa: BLE001
        return None, None, f'bridge validators unavailable ({type(exc).__name__})'


def validate(draft):
    """Raise Invalid with a plain sentence if the drafted configs break a rule. Returns warnings."""
    s, warnings = draft.s, []
    primary = s.primary_login()
    names, ids, journals, kills = {}, {}, {}, {}
    for lg in s.logins:
        cfg = draft.login_cfg(lg['id'])
        entries = []
        if cfg.get('account_name'):
            entries.append(('main', cfg))
        entries += [(f.get('key'), f) for f in cfg.get('followers') or []]
        for key, e in entries:
            if key != 'main':
                if not isinstance(key, str) or not KEY_RE.match(key):
                    raise Invalid(f'Account key "{key}" must be lowercase letters, digits and hyphens.')
            # Every account, the main one included: the daemon refuses to start with a malformed legacy value.
            problem = legacy_max_contracts_problem(e.get('max_contracts'))
            if problem:
                raise Invalid(f'{e.get("label") or key}: {problem}')
            if key != 'main':
                if e.get('prop_profile') not in PROP_PROFILES:
                    raise Invalid(f'{e.get("label") or key}: prop profile must be one of {", ".join(PROP_PROFILES)}.')
            for field, seen, what in (('account_name', names, 'account name'), ('account_id', ids, 'account id'),
                                      ('journal_dir', journals, 'journal folder'),
                                      ('kill_switch_file', kills, 'pause switch file')):
                v = e.get(field)
                if v is None:
                    continue
                if v in seen:
                    raise Invalid(f'Two accounts share the same {what}.')
                seen[v] = key
    f = draft.firing
    order, accts = f.get('firing_order', []), f.get('accounts', {})
    if not order or accts.get(order[0], {}).get('role') != 'main':
        raise Invalid('The Main webhook must keep its leader first in the firing order.')
    if accts[order[0]].get('login', primary) != primary:
        raise Invalid('The Main leader must be on the primary login.')
    configured = set()
    for lg in s.logins:
        cfg = draft.login_cfg(lg['id'])
        configured |= {x.get('key') for x in cfg.get('followers') or []}
    for k in order[1:]:
        if k not in configured:
            raise Invalid(f'{k} is in the firing order but no login config has it.')
        if accts[k].get('size_policy', 'payload') != 'payload':
            raise Invalid(f'{k}: size must come from the strategy payload.')
    seen = {}
    for w in draft.registry['webhooks']:
        wname = w.get('name') or w['id']
        members = order if w['id'] == 'main' else [w.get('leader')] + list(w.get('followers', []))
        if w.get('state') == 'off' and any(members):
            raise Invalid(f'{wname} is off and waiting for a strategy. It can\'t take accounts yet.')
        for k in members:
            if not k:
                continue
            if k in seen:
                raise Invalid(f'{k} can belong to only one webhook (it is in {seen[k]} and {wname}).')
            seen[k] = wname
        if w['id'] != 'main' and w.get('stream_id') is not None and stream_problem(w['stream_id']):
            raise Invalid(stream_problem(w['stream_id']))
    # the bridge's own validators, imported read-only from the installed release
    firing_mod, followers_mod, why = _bridge_modules(s.p)
    if why:
        warnings.append(why)
    else:
        try:
            firing_mod.validate(f)
        except ValueError as exc:
            raise Invalid('Firing config rejected by the bridge: ' + str(exc))
        for lg in s.logins:
            try:
                followers_mod.parse(draft.login_cfg(lg['id']))
            except ValueError as exc:
                raise Invalid(f'{lg.get("label", lg["id"])} config rejected by the bridge: ' + str(exc))
    return warnings


__all__ = ['State', 'Draft', 'Invalid', 'validate', 'new_stream_id', 'stream_strength', 'stream_problem',
           'new_webhook_path', 'slug', 'mask',
           'legacy_max_contracts_problem', 'KEY_RE', 'PROP_PROFILES', 'LANE_PORT_BASE', 'ADMIN_MARK', 'read_switch']
