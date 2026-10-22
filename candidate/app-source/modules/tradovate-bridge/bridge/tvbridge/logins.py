"""Login registry (1.1.0 multi-login) and the daemon's login settings.

`logins.json` lists every separate Tradovate login (own browser, Chromium profile, DevTools port,
daemon, token, journals and login guard). Default path $TVBRIDGE_ROOT/logins.json (TVBRIDGE_ROOT
defaults to ~/.local/share/decrypt-os/tvbridge),
overridden by the TVBRIDGE_LOGINS env var or a `logins_file` config key (callers pass that as
`path`). Absent file: exactly one implicit login `a`, built from today's defaults.

The guard (login-guard/) and the panel (status-panel/) carry their own copy of this loader with
the same schema and rules. Reads one file; never writes, never sends anything.

`daemon_settings(cfg)` validates the daemon's optional multi-login keys (section 2): login_id,
login_label, mode, master_kill_switch_file, peer_logins, peer_hedge_scope, token_env, read_token_env.
Every default reproduces the 1.0.9.5.2 daemon.

A registry entry may name `read_token_env`: the variable holding that login's read-only token
(GET /login and /health only). Peers, the firing service's /login reads and the panel use it;
without it they use `token_env`, as before.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

SCHEMA = 'decrypt.tvbridge-logins.v1'
# Every bridge component finds the user's bridge folder the same way.
ROOT = os.environ.get('TVBRIDGE_ROOT') or '~/.local/share/decrypt-os/tvbridge'
DEFAULT_PATH = ROOT + '/logins.json'
ENV_PATH = 'TVBRIDGE_LOGINS'
ID_RE = re.compile(r'^[a-z][a-z0-9-]{0,15}\Z')
ENV_NAME_RE = re.compile(r'^[A-Z_][A-Z0-9_]{0,127}\Z')
# DevTools ports of Decrypt OS's other browsers (login guard, Guardian, market); never a login's port.
RESERVED_PORTS = frozenset({9556, 9566, 9567})
UNIT_FIELDS = ('browser_unit', 'daemon_unit', 'guard_unit', 'trigger_unit')
REQUIRED = ('id', 'config', 'bridge_url', 'token_env', 'devtools_port') + UNIT_FIELDS + ('guard_state_dir',)

# Login A exactly as deployed before the registry existed.
DEFAULT_LOGIN = {
    'id': 'a', 'label': 'Login A', 'primary': True,
    'config': ROOT + '/config.json',
    'bridge_url': 'http://127.0.0.1:8777', 'token_env': 'TVBRIDGE_TOKEN',
    'devtools_port': 9555,
    'browser_unit': 'tvbridge-browser.service', 'daemon_unit': 'tvbridge-daemon.service',
    'guard_unit': 'tvbridge-login-guard.service', 'trigger_unit': 'tvbridge-login-trigger.service',
    'guard_state_dir': '~/.local/share/decrypt-os/tvbridge-state/login-guard',
}


@dataclass(frozen=True)
class Login:
    id: str
    label: str
    primary: bool
    config: str           # ~ expanded
    bridge_url: str       # http://host:port, no trailing slash
    token_env: str
    devtools_port: int
    browser_unit: str
    daemon_unit: str
    guard_unit: str
    trigger_unit: str
    guard_state_dir: str  # ~ expanded
    read_token_env: str | None = None  # optional: read-only token (GET /login, /health)

    @property
    def bridge_port(self):
        return urllib.parse.urlsplit(self.bridge_url).port

    @property
    def units(self):
        return tuple(getattr(self, f) for f in UNIT_FIELDS)


def _expand(path):
    return os.path.expanduser(path)


def _resolved(path):
    return os.path.realpath(_expand(path))


def default_label(login_id):
    return 'Login ' + login_id.upper()


def _url(value, where):
    """A loopback-style http(s) URL with an explicit port, returned without a trailing slash."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{where} must be an http URL with a port')
    split = urllib.parse.urlsplit(value.strip())
    try:
        port = split.port
    except ValueError:
        port = None
    if split.scheme not in ('http', 'https') or not split.hostname or port is None or \
            split.path.strip('/') or split.query or split.fragment or split.username or split.password:
        raise ValueError(f'{where} must be an http URL with a port (for example http://127.0.0.1:8778)')
    return f'{split.scheme}://{split.netloc}'


def _login(raw, where):
    if not isinstance(raw, dict):
        raise ValueError(where + ' must be an object')
    missing = [k for k in REQUIRED if k not in raw]
    if missing:
        raise ValueError(f'{where} is missing {", ".join(missing)}')
    lid = raw['id']
    if not isinstance(lid, str) or not ID_RE.match(lid):
        raise ValueError(f'{where}.id must match {ID_RE.pattern[:-2]}$')
    where = f'login {lid}'
    label = raw.get('label', default_label(lid))
    if not isinstance(label, str) or not label.strip():
        raise ValueError(where + '.label must be a non-empty string')
    primary = raw.get('primary', False)
    if not isinstance(primary, bool):
        raise ValueError(where + '.primary must be true or false')
    for field in ('config', 'guard_state_dir') + UNIT_FIELDS:
        if not isinstance(raw[field], str) or not raw[field].strip():
            raise ValueError(f'{where}.{field} must be a non-empty string')
    for field in UNIT_FIELDS:
        if not raw[field].endswith('.service'):
            raise ValueError(f'{where}.{field} must name a .service unit')
    if not isinstance(raw['token_env'], str) or not ENV_NAME_RE.match(raw['token_env']):
        raise ValueError(f'{where}.token_env must be an environment variable name')
    read_env = raw.get('read_token_env')
    if read_env is not None and (not isinstance(read_env, str) or not ENV_NAME_RE.match(read_env)):
        raise ValueError(f'{where}.read_token_env must be an environment variable name')
    if read_env == raw['token_env']:
        raise ValueError(f'{where}.read_token_env must differ from its token_env')
    port = raw['devtools_port']
    if type(port) is not int or not 0 < port < 65536:
        raise ValueError(where + '.devtools_port must be a port number')
    if port in RESERVED_PORTS:
        owner = 'the login guard' if port == 9556 else 'another Decrypt OS browser'
        raise ValueError(f'{where}.devtools_port {port} is reserved by {owner}')
    return Login(id=lid, label=label.strip(), primary=primary, config=_expand(raw['config']),
                 bridge_url=_url(raw['bridge_url'], where + '.bridge_url'), token_env=raw['token_env'],
                 devtools_port=port, browser_unit=raw['browser_unit'], daemon_unit=raw['daemon_unit'],
                 guard_unit=raw['guard_unit'], trigger_unit=raw['trigger_unit'],
                 guard_state_dir=_expand(raw['guard_state_dir']), read_token_env=read_env)


def validate(data):
    """Parsed logins.json -> [Login]. Raises ValueError with a clear message."""
    if not isinstance(data, dict):
        raise ValueError('logins file must be a JSON object')
    if data.get('schema') != SCHEMA:
        raise ValueError(f'logins file schema must be "{SCHEMA}"')
    raw = data.get('logins')
    if not isinstance(raw, list) or not raw:
        raise ValueError('logins must be a non-empty list')
    out = [_login(item, f'logins[{i}]') for i, item in enumerate(raw)]
    primaries = [x.id for x in out if x.primary]
    if len(primaries) != 1:
        raise ValueError(f'exactly one login must be primary (found {len(primaries)})')
    ids = [x.id for x in out]
    for lid in ids:
        if ids.count(lid) > 1:
            raise ValueError(f'login id {lid} is listed more than once')
    seen = {}

    def unique(kind, value, lid):
        other = seen.setdefault((kind, value), lid)
        if other != lid:
            raise ValueError(f'logins {other} and {lid} share the same {kind}')

    for x in out:
        # One port space: no DevTools port may equal another login's port or any bridge port.
        for port in {x.devtools_port, x.bridge_port}:
            unique('port', port, x.id)
        if x.devtools_port == x.bridge_port:
            raise ValueError(f'login {x.id} uses the same port for DevTools and its bridge')
        unique('config', _resolved(x.config), x.id)
        unique('token_env', x.token_env, x.id)
        if x.read_token_env is not None:
            unique('token_env', x.read_token_env, x.id)  # one name space: no read name equals any token name
        for unit in set(x.units):
            unique('unit', unit, x.id)
        if len(set(x.units)) != len(x.units):
            raise ValueError(f'login {x.id} names the same unit twice')
        unique('guard_state_dir', _resolved(x.guard_state_dir), x.id)
    return out


def default_logins():
    """The implicit registry when logins.json is absent: login `a` as deployed today."""
    return validate({'schema': SCHEMA, 'logins': [dict(DEFAULT_LOGIN)]})


def registry_path(path=None):
    """Explicit path, else TVBRIDGE_LOGINS, else the default path. `~` expanded."""
    chosen = path or os.environ.get(ENV_PATH) or DEFAULT_PATH
    return Path(_expand(str(chosen)))


def load_logins(path=None):
    """Validated logins (primary flag, `~` expanded). Absent file: the implicit login `a`."""
    p = registry_path(path)
    if not p.exists():
        return default_logins()
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f'logins file {p} is not readable JSON ({type(exc).__name__})') from None
    return validate(data)


def primary(logins):
    found = [x for x in logins if x.primary]
    if len(found) != 1:
        raise ValueError('exactly one login must be primary')
    return found[0]


def by_id(logins, login_id):
    for x in logins:
        if x.id == login_id:
            return x
    raise ValueError(f'no login with id {login_id!r}')


# --- daemon config keys (section 2) -----------------------------------------------------------

MODES = ('main', 'followers_only')
PEER_HEDGE_SCOPES = ('configured', 'all')
DEFAULT_TOKEN_ENV = 'TVBRIDGE_TOKEN'
PEER_KEYS = frozenset({'id', 'url', 'token_env'})


def daemon_settings(cfg):
    """The daemon's optional multi-login keys, validated, with today's defaults. Raises ValueError.

    explicit: True when the config names a login (`login_id`) or runs followers_only; only then do
    /health and /followers carry `login_id`, so a config without the new keys answers byte for byte
    as before.
    """
    login_id = cfg.get('login_id', 'a')
    if not isinstance(login_id, str) or not ID_RE.match(login_id):
        raise ValueError(f'login_id must match {ID_RE.pattern[:-2]}$')
    label = cfg.get('login_label', default_label(login_id))
    if not isinstance(label, str) or not label.strip():
        raise ValueError('login_label must be a non-empty string')
    mode = cfg.get('mode', 'main')
    if mode not in MODES:
        raise ValueError('mode must be "main" or "followers_only"')
    token_env = cfg.get('token_env', DEFAULT_TOKEN_ENV)
    if not isinstance(token_env, str) or not ENV_NAME_RE.match(token_env):
        raise ValueError('token_env must be an environment variable name')
    read_token_env = cfg.get('read_token_env')
    if read_token_env is not None and (not isinstance(read_token_env, str) or not ENV_NAME_RE.match(read_token_env)
                                       or read_token_env == token_env):
        raise ValueError('read_token_env must be an environment variable name other than token_env')
    scope = cfg.get('peer_hedge_scope', 'configured')
    if scope not in PEER_HEDGE_SCOPES:
        raise ValueError('peer_hedge_scope must be "configured" or "all"')
    master = cfg.get('master_kill_switch_file')
    if master is not None:
        if not isinstance(master, str) or not master.startswith('/'):
            raise ValueError('master_kill_switch_file must be an absolute path')
        # This daemon only reads the master switch. One of its own kill switches (which
        # auto-protect and /follower/.../kill write) must never be the master file.
        own = [cfg.get('kill_switch_file')] + [f.get('kill_switch_file') for f in cfg.get('followers') or []
                                               if isinstance(f, dict)]
        if _resolved(master) in {_resolved(p) for p in own if isinstance(p, str)}:
            raise ValueError('master_kill_switch_file must differ from this daemon\'s own kill-switch files')
    peers = []
    raw = cfg.get('peer_logins', [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError('peer_logins must be a list')
    ids = {login_id}
    for i, p in enumerate(raw):
        where = f'peer_logins[{i}]'
        if not isinstance(p, dict) or set(p) != PEER_KEYS:
            raise ValueError(f'{where} must be {{"id", "url", "token_env"}}')
        if not isinstance(p['id'], str) or not ID_RE.match(p['id']) or p['id'] in ids:
            raise ValueError(f'{where}.id must be a login id other than this login and every other peer')
        ids.add(p['id'])
        if not isinstance(p['token_env'], str) or not ENV_NAME_RE.match(p['token_env']):
            raise ValueError(f'{where}.token_env must be an environment variable name')
        peers.append({'id': p['id'], 'url': _url(p['url'], where + '.url'), 'token_env': p['token_env']})
    return {'login_id': login_id, 'label': label.strip(), 'mode': mode, 'token_env': token_env,
            'read_token_env': read_token_env, 'peer_hedge_scope': scope,
            'master_kill_switch_file': master, 'peers': peers,
            'explicit': 'login_id' in cfg or mode == 'followers_only'}
