"""Login registry (logins.json) for the login guard and trigger. Stdlib only.

A small copy of the bridge's tvbridge/logins.py loader: same schema, same rules.
The guard is installed apart from the bridge release, so it carries its own copy.

Path: TVBRIDGE_LOGINS, else $TVBRIDGE_ROOT/logins.json (default ~/.local/share/decrypt-os/tvbridge). Without the
file there is exactly one implicit login, "a", built from today's defaults.
Every validation error raises ValueError with a clear message.

Optional `read_token_env`: the env var of the login's read token (accepted by its daemon only on
GET /health and GET /login, for the status panel). The guard validates and carries it; it keeps
using its own token for its own daemon's /health.
"""
import json
import os
import re
from collections import namedtuple
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = 'decrypt.tvbridge-logins.v1'
ROOT = os.environ.get('TVBRIDGE_ROOT') or '~/.local/share/decrypt-os/tvbridge'
DEFAULT_PATH = ROOT + '/logins.json'
ID_RE = re.compile(r'^[a-z][a-z0-9-]{0,15}\Z')
ENV_RE = re.compile(r'^[A-Z_][A-Z0-9_]{0,127}\Z')
FORBIDDEN_PORT = 9556  # reserved by the login guard
MAX_BYTES = 1_000_000
UNIT_FIELDS = ('browser_unit', 'daemon_unit', 'guard_unit', 'trigger_unit')
STRING_FIELDS = ('config', 'bridge_url', 'token_env') + UNIT_FIELDS + ('guard_state_dir',)


class Login(namedtuple('Login', ('id', 'label', 'primary', 'config', 'bridge_url', 'token_env', 'devtools_port',
                                  'browser_unit', 'daemon_unit', 'guard_unit', 'trigger_unit',
                                  'guard_state_dir', 'read_token_env'), defaults=(None,))):
    __slots__ = ()

    @property
    def bridge_port(self):
        """The daemon's loopback port, taken from bridge_url."""
        return urlsplit(self.bridge_url).port


# Login A exactly as it runs today (no registry).
DEFAULT_LOGIN = Login(id='a', label='Login A', primary=True,
                      config=os.path.expanduser(ROOT + '/config.json'),
                      bridge_url='http://127.0.0.1:8777', token_env='TVBRIDGE_TOKEN', devtools_port=9555,
                      browser_unit='tvbridge-browser.service', daemon_unit='tvbridge-daemon.service',
                      guard_unit='tvbridge-login-guard.service', trigger_unit='tvbridge-login-trigger.service',
                      guard_state_dir=os.path.expanduser('~/.local/share/decrypt-os/tvbridge-state/login-guard'))


def registry_path(path=None):
    return Path(os.path.expanduser(str(path or os.environ.get('TVBRIDGE_LOGINS') or DEFAULT_PATH)))


def _port(value, where):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError(f'{where} must be an integer port')
    if value == FORBIDDEN_PORT:
        raise ValueError(f'{where} {FORBIDDEN_PORT} is reserved by the login guard')
    return value


def _entry(raw, i):
    where = f'logins[{i}]'
    if not isinstance(raw, dict):
        raise ValueError(f'{where} must be an object')
    ident = raw.get('id')
    if not isinstance(ident, str) or not ID_RE.match(ident):
        raise ValueError(f'{where}.id must match {ID_RE.pattern[:-2]}$')
    where = f'login {ident}'
    for key in STRING_FIELDS:
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise ValueError(f'{where}: {key} must be a non-empty string')
    label = raw.get('label', 'Login ' + ident.upper())
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f'{where}: label must be a non-empty string')
    primary = raw.get('primary', False)
    if type(primary) is not bool:
        raise ValueError(f'{where}: primary must be true or false')
    if not ENV_RE.match(raw['token_env']):
        raise ValueError(f'{where}: token_env must be an environment variable name')
    read_env = raw.get('read_token_env')
    if 'read_token_env' in raw and not (isinstance(read_env, str) and ENV_RE.match(read_env)):
        raise ValueError(f'{where}: read_token_env must be an environment variable name')
    for key in UNIT_FIELDS:
        if not raw[key].endswith('.service'):
            raise ValueError(f'{where}: {key} must name a .service unit')
    # Same URL rule as the bridge loader. The guard and trigger use only the port, always on 127.0.0.1.
    url = urlsplit(raw['bridge_url'].strip())
    try:
        bridge_port = url.port
    except ValueError:
        bridge_port = None
    if url.scheme not in ('http', 'https') or not url.hostname or bridge_port is None or url.path.strip('/') \
            or url.query or url.fragment or url.username or url.password:
        raise ValueError(f'{where}: bridge_url must be an http URL with a port (for example http://127.0.0.1:8778)')
    return Login(id=ident, label=label.strip(), primary=primary,
                 config=os.path.expanduser(raw['config']), bridge_url=f'{url.scheme}://{url.netloc}',
                 token_env=raw['token_env'], devtools_port=_port(raw.get('devtools_port'), f'{where}: devtools_port'),
                 browser_unit=raw['browser_unit'], daemon_unit=raw['daemon_unit'], guard_unit=raw['guard_unit'],
                 trigger_unit=raw['trigger_unit'], guard_state_dir=os.path.expanduser(raw['guard_state_dir']),
                 read_token_env=read_env)


def validate(data):
    """Validated list of Login from parsed logins.json content."""
    if not isinstance(data, dict):
        raise ValueError('logins.json must be a JSON object')
    if data.get('schema') != SCHEMA:
        raise ValueError(f'logins.json schema must be {SCHEMA}')
    raw = data.get('logins')
    if not isinstance(raw, list) or not raw:
        raise ValueError('logins.json logins must be a non-empty list')
    logins = [_entry(r, i) for i, r in enumerate(raw)]
    if sum(1 for l in logins if l.primary) != 1:
        raise ValueError('logins.json must have exactly one primary login')
    seen = {}
    for l in logins:
        for kind, values in (('id', [l.id]), ('port', [l.devtools_port, l.bridge_port]),
                             ('config', [os.path.realpath(l.config)]), ('token_env', [l.token_env]),
                             ('unit', [getattr(l, k) for k in UNIT_FIELDS]),
                             ('guard_state_dir', [os.path.realpath(l.guard_state_dir)])):
            for v in values:
                if (kind, v) in seen:
                    raise ValueError(f'login {l.id}: {kind} {v} is already used by login {seen[(kind, v)]}')
                seen[(kind, v)] = l.id
    # A read token env is unique and never any login's token_env (its own or another's).
    for l in logins:
        if l.read_token_env is None:
            continue
        if ('token_env', l.read_token_env) in seen:
            other = seen[('token_env', l.read_token_env)]
            raise ValueError(f'login {l.id}: read_token_env {l.read_token_env} is the token_env of login {other}')
        if ('read_token_env', l.read_token_env) in seen:
            raise ValueError(f'login {l.id}: read_token_env {l.read_token_env} is already used by login '
                             f'{seen[("read_token_env", l.read_token_env)]}')
        seen[('read_token_env', l.read_token_env)] = l.id
    return logins


def load_logins(path=None):
    """Validated logins; [DEFAULT_LOGIN] when the registry file does not exist."""
    p = registry_path(path)
    try:
        with open(p, 'rb') as f:
            raw = f.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return [DEFAULT_LOGIN]
    except OSError as exc:
        raise ValueError(f'{p} unreadable ({type(exc).__name__})') from None
    if len(raw) > MAX_BYTES:
        raise ValueError(f'{p} is larger than {MAX_BYTES} bytes')
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValueError(f'{p} is not valid JSON') from None
    return validate(data)


def primary(logins):
    for l in logins:
        if l.primary:
            return l
    raise ValueError('no primary login')


def by_id(logins, ident):
    for l in logins:
        if l.id == ident:
            return l
    raise ValueError(f'login {ident!r} is not in the registry')
