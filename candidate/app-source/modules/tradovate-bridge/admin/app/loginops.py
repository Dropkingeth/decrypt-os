"""Add, start and remove a Tradovate login.

A guided wrapper around the bridge's own `scripts/install.py add-login`, plus the systemd pieces a
second login needs (multi-login, Flatten now). What a login is (login B as the example):
- install.py add-login <id>: logins/<id>/{config.json, login.env, tvbridge.env (its order token),
  journal/, ENTRIES-DISABLED}, a browser profile `profile-<id>` beside login A's (marked .tvbridge-login),
  a logins.json entry, login A's peer_logins + read_token_env, peers.env (+ its read-only token) and
  firing.env (+ its order token, for the firing service only). Ports: the next free DevTools port from
  9557 and loopback port from 8778.
- the template units tvbridge-{browser,daemon,login-trigger}@.service and tvbridge-login-guard@.{service,
  timer} (in the unit dir since 1.1.0). Login B's instances are enabled: browser@b (graphical session),
  daemon@b and login-trigger@b (default.target), login-guard@b.timer (timers.target).
- Flatten now (tvbridge 1.1.3): logins/manual-<id>.env (0600, TVBRIDGE_MANUAL_TOKEN_<ID>), the drop-in
  tvbridge-daemon@<id>.service.d/manual-token.conf and a line in tvbridge-admin.service.d/manual-tokens.conf.
- the login-wide ENTRIES-DISABLED is retired once the login is wired (renamed *.removed-<UTC>): every
  account on it has its own switch, and every account added here starts paused.

Three changes, each previewed and confirmed (plans.p_add_login / p_start_login / p_remove_login):
- add_login {label}: runs install.py add-login --dry-run (preview and again right before applying), then
  for real, and checks the result against the dry run; then the Flatten-now token and drop-ins,
  daemon-reload, enables and starts ONLY the new browser window, and restarts the read-only status panel
  through the restarter. Daemons A/B, firing and Soup are never touched; nothing signs in.
- start_login {login}: after the trader signed in by hand. Enables the daemon, trigger and guard timer, starts the
  trigger and the guard timer. The daemon is started by the login guard (its existing rule: only when it
  sees the page signed in), never from here, so a signed-out daemon never starts and ends `failed`.
- remove_login {login}: a non-primary login with no accounts. Stops and disables its units, removes its
  registry entry, peer entries and tokens, and moves logins/<id> (with its token file and drop-in) and
  its browser profile aside as *.removed-<UTC>. Nothing else restarts.

systemctl goes only through the restarter's argv (tools/fake_systemctl.py in the sandbox) and only for the
units of the login being changed. Secrets are never printed, logged, diffed or copied into backups.
"""
import glob
import hashlib
import importlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time

from tree import FileOps, dump_json, file_sha, json_text_like, load_json, utc_stamp

ACTIONS = ('add_login', 'start_login', 'remove_login')
LOGIN_ID_RE = re.compile(r'^[a-z][a-z0-9-]{0,15}\Z')
TEMPLATES = ('tvbridge-browser@.service', 'tvbridge-daemon@.service', 'tvbridge-login-guard@.service',
             'tvbridge-login-guard@.timer', 'tvbridge-login-trigger@.service')
KNOWN_PORTS = {9556: 'the login guard', 9566: 'the Guardian browser', 9567: 'the market browser',
               8779: 'the Bridge window (status panel)', 8781: 'Bridge Advanced',
               8783: 'the ICT web-reader proxy'}
STATUS = 'tvbridge-status.service'
ACTION_RE = re.compile(r'^  (backup|create|replace|keep|warn)\s+(/\S*)\s+(.*)\Z')
HEAD_RE = re.compile(r'^tvbridge add-login(?: plan \(nothing is changed\))?: login ([a-z][a-z0-9-]*), '
                     r'DevTools (\d+), loopback (\d+)$')
BAK_RE = re.compile(r'\.bak-\d{8}T\d{6}Z(-\d+)?$')
ENV_NAME_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)=')
INSTALL_TIMEOUT_S = 60


class StepFailed(Exception):
    pass


def units_of(lid):
    return {'browser': f'tvbridge-browser@{lid}.service', 'daemon': f'tvbridge-daemon@{lid}.service',
            'trigger': f'tvbridge-login-trigger@{lid}.service', 'guard_timer': f'tvbridge-login-guard@{lid}.timer',
            'guard': f'tvbridge-login-guard@{lid}.service'}


def up(lid):
    return lid.upper().replace('-', '_')


def token_names(lid):
    return {'order': f'TVBRIDGE_TOKEN_{up(lid)}', 'read': f'TVBRIDGE_READ_TOKEN_{up(lid)}',
            'manual': f'TVBRIDGE_MANUAL_TOKEN_{up(lid)}'}


def env_digests(path):
    """{name: [sha256 hex of the value, value length]} of an env file. Values are hashed as bytes and dropped;
    no token value (order tokens included) is kept, logged or compared in the clear."""
    out = {}
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except FileNotFoundError:
        return out
    for line in data.splitlines():
        line = line.strip()
        if line and not line.startswith(b'#') and b'=' in line:
            k, v = line.split(b'=', 1)
            v = v.strip()
            if len(v) >= 2 and v[:1] == v[-1:] and v[:1] in (b'"', b"'"):
                v = v[1:-1]          # the value systemd passes (quotes are syntax, not part of the token)
            out[k.strip().decode('ascii', 'replace')] = [hashlib.sha256(v).hexdigest(), len(v)]
    return out


def env_names(path):
    return list(env_digests(path))


def write_private(path, data, exclusive=False):
    """0600 file, atomic (temp + rename); exclusive = must not exist yet. `data` is str or bytes."""
    if isinstance(data, str):
        data = data.encode()
    d = os.path.dirname(path)
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        return
    tmp = os.path.join(d, '.' + os.path.basename(path) + '.admin-tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def drop_env_names(path, names):
    """Rewrite an env file without the lines for `names`. Lines are handled as opaque bytes (only the name
    before '=' is looked at); every other line is kept byte for byte. Returns the names removed."""
    with open(path, 'rb') as f:
        lines = f.read().splitlines(keepends=True)
    keep, removed = [], []
    for line in lines:
        name = line.strip().split(b'=', 1)[0].decode('ascii', 'replace') if b'=' in line else None
        if name in names and not line.strip().startswith(b'#'):
            removed.append(name)
            continue
        keep.append(line)
    if removed:
        write_private(path, b''.join(keep))
    return removed


def keep_original(path, tag):
    """Hard link `<path>.<tag>` to the file's current inode. The tools that rewrite these files (install.py,
    this page) always write a new file and rename it over, so the link keeps the original bytes without this
    service reading them. Returns the link path, or None when the file does not exist."""
    if not os.path.isfile(path):
        return None
    link = f'{path}.{tag}'
    if os.path.lexists(link):
        os.remove(link)
    os.link(path, link)
    return link


def put_back(path, original, want_sha):
    """Make `path` byte-identical to its original again (or absent when it did not exist). True if verified."""
    if file_sha(path) != want_sha:
        if want_sha == 'absent':
            if os.path.isfile(path):
                os.remove(path)
        elif original and os.path.isfile(original):
            tmp = os.path.join(os.path.dirname(path), '.' + os.path.basename(path) + '.admin-restore')
            if os.path.lexists(tmp):
                os.remove(tmp)
            os.link(original, tmp)
            os.replace(tmp, path)
    return file_sha(path) == want_sha


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ---------- the durable login record (admin/logins/<id>.json) ----------
RECORD_SCHEMA = 'decrypt.tvbridge-admin.login-record.v1'
LIVE_RECORD = ('adding', 'interrupted', 'add_failed', 'created', 'removing')   # left over / owned by this page
INCOMPLETE = ('adding', 'interrupted', 'add_failed', 'removing')


def record_path(profile, lid):
    return profile.st('logins', f'{lid}.json')


def read_record(profile, lid):
    if not isinstance(lid, str) or not LOGIN_ID_RE.match(lid):
        return None
    try:
        with open(record_path(profile, lid)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def all_records(profile):
    out = []
    for path in sorted(glob.glob(profile.st('logins', '*.json'))):
        rec = read_record(profile, os.path.basename(path)[:-5])
        if rec:
            out.append(rec)
    return out


def write_record(profile, rec, clock=time.time):
    """Durable: written to a temp file, fsynced, renamed, and the folder fsynced."""
    d = profile.st('logins')
    os.makedirs(d, mode=0o700, exist_ok=True)
    rec['updated_at'] = clock()
    write_private(record_path(profile, rec['id']), dump_json(rec))
    fsync_dir(d)


class Killed(BaseException):
    """Tests only: stands for the service being killed at a step (never caught by the change code)."""


class UndoIncomplete(Exception):
    def __init__(self, msg, paths=(), units=()):
        super().__init__(msg)
        self.paths, self.units = list(paths), list(units)


def listening_ports():
    """Loopback/any TCP ports in LISTEN state, from /proc/net/tcp{,6} (read only). None when unreadable."""
    ports = set()
    found = False
    for name in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            with open(name) as f:
                rows = f.read().splitlines()[1:]
        except OSError:
            continue
        found = True
        for row in rows:
            parts = row.split()
            if len(parts) > 3 and parts[3] == '0A':
                ports.add(int(parts[1].rsplit(':', 1)[1], 16))
    return ports if found else None


def run_cmd(argv, timeout, env):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, '', f'timed out after {timeout:.0f} s'
    except OSError as exc:
        return 127, '', f'{type(exc).__name__}: {exc}'


def parse_install(out):
    """(header {id, devtools, listen} | None, [{verb, path, detail}]) from install.py add-login output.
    Only the action table right after the header is read (the dry run prints config text after it)."""
    lines = out.splitlines()
    head, actions = None, []
    for i, line in enumerate(lines):
        m = HEAD_RE.match(line)
        if m:
            head = {'id': m.group(1), 'devtools': int(m.group(2)), 'listen': int(m.group(3))}
            for row in lines[i + 1:]:
                if not row.strip():
                    break
                a = ACTION_RE.match(row)
                if a:
                    actions.append({'verb': a.group(1), 'path': a.group(2), 'detail': a.group(3).strip()})
            break
    return head, actions


def norm_actions(actions):
    """Comparable form: backups carry a timestamp, so only their target file counts."""
    return [[a['verb'], BAK_RE.sub('.bak-<UTC>', a['path'])] for a in actions]


class LoginSetup:
    def __init__(self, profile, restarter, run=None, listening=None, clock=time.time, sleep=time.sleep):
        self.p = profile
        self.cfg = profile.data.get('login_setup') or {}
        self.restarter = restarter
        self.run = run or run_cmd
        self.listening = listening or listening_ports
        self.clock, self.sleep = clock, sleep
        self.files = FileOps(profile)
        self._unit_cache = (0.0, None)
        self._signin_cache = {}

    # ----- configuration -----
    @property
    def configured(self):
        return bool(self.cfg)

    @property
    def unit_dir(self):
        return self.cfg.get('unit_dir')

    @property
    def script(self):
        return self.cfg.get('install_script') or self.p.tb('current', 'scripts', 'install.py')

    @property
    def python(self):
        return self.cfg.get('python') or '/usr/bin/python3'

    def available(self):
        """(ok, reason) for the whole feature."""
        if not self.configured:
            return False, 'Add login is not set up in this install (profile login_setup).'
        if not hasattr(self.restarter, 'sc'):
            return False, 'Add login needs the live restarter (systemd).'
        return True, None

    def profile_dir(self, lid):
        return os.path.join(os.path.dirname(self.cfg['browser_profile_dir'].rstrip('/')), f'profile-{lid}')

    def paths(self, lid):
        home = self.p.tb('logins', lid)
        return {'home': home, 'config': os.path.join(home, 'config.json'),
                'login_kill': os.path.join(home, 'ENTRIES-DISABLED'),
                'manual_env': self.p.tb('logins', f'manual-{lid}.env'),
                'peers_env': self.p.tb('logins', 'peers.env'), 'firing_env': self.p.tb('logins', 'firing.env'),
                'registry': self.p.tb('logins.json'),
                'dropin_dir': os.path.join(self.unit_dir, f'tvbridge-daemon@{lid}.service.d'),
                'dropin': os.path.join(self.unit_dir, f'tvbridge-daemon@{lid}.service.d', 'manual-token.conf'),
                'admin_dropin': os.path.join(self.unit_dir, 'tvbridge-admin.service.d', 'manual-tokens.conf'),
                'profile': self.profile_dir(lid)}

    def show_path(self, path):
        """Display path: tvbridge/..., or ~/... outside the roots."""
        rel = self.p.rel(path)
        if rel != path:
            return rel
        home = os.path.expanduser('~')
        return '~' + path[len(home):] if path.startswith(home + os.sep) else path

    def manual_line(self, lid):
        path = self.p.tb('logins', f'manual-{lid}.env')
        home = os.path.expanduser('~')
        shown = '%h' + path[len(home):] if path.startswith(home + os.sep) else path
        return f'EnvironmentFile=-{shown}\n'

    def dropin_text(self, lid):
        return (f'# Flatten now (tvbridge 1.1.3): login {lid.upper()} manual token only. Opens nothing but '
                f'POST /manual/<token>/<key>/flatten.\n'
                f'# Written by tvbridge-admin (Add login).\n[Service]\n{self.manual_line(lid)}')

    # ----- systemctl, only for this login's units -----
    def sc(self, lid, verb, unit=None):
        allowed = set(units_of(lid).values())
        if not LOGIN_ID_RE.match(lid) or verb not in ('daemon-reload', 'enable', 'disable', 'start', 'stop'):
            raise PermissionError(f'refusing systemctl {verb}')
        if verb == 'daemon-reload':
            return self.restarter.sc('daemon-reload', timeout=30)
        if unit not in allowed:
            raise PermissionError(f'refusing systemctl {verb} {unit}: not a unit of login {lid}')
        return self.restarter.sc(verb, unit, timeout=60)

    def unit_states(self, lid):
        """{role: {active, enabled}} for the login's units (systemctl show; read only)."""
        out = {}
        for role, unit in units_of(lid).items():
            sh = self.restarter.show(unit, props=('ActiveState', 'UnitFileState'))
            out[role] = {'unit': unit, 'active': sh['raw'].get('ActiveState') or sh['active'],
                         'enabled': sh['raw'].get('UnitFileState') or 'unknown'}
        return out

    # ----- install.py -----
    def install(self, lid, label, dry):
        argv = [self.python, self.script, 'add-login', lid, '--label', label,
                '--root', self.p.tvbridge_root, '--unit-dir', self.unit_dir,
                '--profile-dir', self.cfg['browser_profile_dir']] + (['--dry-run'] if dry else [])
        env = {'PATH': '/usr/bin:/bin', 'HOME': os.path.expanduser('~'), 'LANG': 'C.UTF-8',
               'PYTHONDONTWRITEBYTECODE': '1'}   # none of this service's tokens
        code, out, err = self.run(argv, INSTALL_TIMEOUT_S, env)
        head, actions = parse_install(out)
        error = None
        if code != 0:
            error = (err.strip().splitlines() or [f'exit {code}'])[-1][:300]
        elif not head or head['id'] != lid or not actions:
            error = 'install.py printed no plan this page can read'
        return {'ok': error is None, 'error': error, 'head': head, 'actions': actions,
                'norm': norm_actions(actions), 'code': code}

    # ----- preview -----
    def inspect(self, state, plan, store=None, status_data=None):
        act = plan['action']
        ok, why = self.available()
        if not ok:
            plan['blockers'].append(why)
            return plan
        if act == 'add_login':
            self._inspect_add(state, plan)
        elif act == 'start_login':
            self._inspect_start(state, plan)
        elif act == 'remove_login':
            self._inspect_remove(state, plan, store)
        hold = ((status_data or {}).get('guard') or {}).get('reason') == 'weekend_maintenance_hold'
        if hold and act in ('add_login', 'start_login'):
            window = (((status_data or {}).get('guard') or {}).get('weekend_hold') or {}).get('text') \
                or 'Fri 6 PM to Sun 6 PM ET'
            plan['warnings'].insert(0, f'Weekend hold now ({window}): sign in to Tradovate only after it ends.')
        return plan

    def _leftovers(self, lid):
        p = self.paths(lid)
        found = [x for x in (p['home'], p['manual_env'], p['dropin_dir'], p['profile']) if os.path.lexists(x)]
        found += glob.glob(os.path.join(self.unit_dir, '*.wants', f'*@{lid}.*'))
        return found

    def _inspect_add(self, state, plan):
        lid, label = plan['login']['id'], plan['login']['label']
        b, p = plan['blockers'], self.paths(lid)
        if not os.path.isfile(self.script):
            b.append(f'The bridge install tool is missing ({self.show_path(self.script)}).')
            return
        # Templates may be the user's own copies or the system's (Decrypt OS ships them in /etc/systemd/user).
        folders = [self.unit_dir] + list(self.cfg.get('template_dirs') or [])
        missing = [t for t in TEMPLATES if not any(os.path.isfile(os.path.join(d, t)) for d in folders)]
        if missing:
            b.append('The login unit templates are missing from the unit folder: ' + ', '.join(missing) + '.')
        if self.cfg.get('guard_dir') and not os.path.isfile(os.path.join(self.cfg['guard_dir'], 'logins.py')):
            b.append('The login guard has no logins.py beside guard.py, so it cannot watch a second login.')
        for x in self._leftovers(lid):
            b.append(f'{self.show_path(x)} already exists (left from an earlier login {lid}); move it aside first.')
        primary = state.login_cfg[state.primary_login()][1]
        if not primary.get('read_token_env'):
            b.append('Login A\'s config has no read_token_env yet; install.py would set it, which needs a daemon A '
                     'restart. Not done from here.')
        peers = env_digests(p['peers_env'])
        for lg in state.logins:
            if lg.get('read_token_env') and peers.get(lg['read_token_env'], [None, 0])[1] < 24:
                b.append(f'peers.env has no read token for {lg.get("label", lg["id"])}; install.py would make a '
                         f'new one, which that login\'s daemon would only learn at a restart.')
        dry = self.install(lid, label, dry=True)
        if not dry['ok']:
            b.append(f'install.py add-login --dry-run refused: {dry["error"]}')
            return
        head = dry['head']
        for a in dry['actions']:
            if a['verb'] == 'warn':
                b.append(f'install.py warns: {a["detail"]}')
            if 'new for the primary login' in a['detail']:
                b.append('install.py would make a new read token for login A (daemon A restart needed).')
            if a['verb'] == 'keep':
                plan['warnings'].append(f'{self.show_path(a["path"])}: {a["detail"]}.')
        used = self.listening()
        lanes = {w.get('port') for w in state.registry.get('webhooks', [])}
        for what, port in (('DevTools', head['devtools']), ('bridge', head['listen'])):
            owner = KNOWN_PORTS.get(port) or ('this Advanced page' if port == int(self.p.port) else None) \
                or ('a planned webhook lane' if port in lanes else None)
            if owner:
                b.append(f'install.py picked {what} port {port}, which belongs to {owner}.')
            elif used is not None and port in used:
                b.append(f'install.py picked {what} port {port}, which is already in use on this machine.')
        if used is None:
            plan['warnings'].append('Could not read the open ports on this machine; ports were checked against '
                                    'the known services only.')
        files = []
        for a in dry['actions']:
            if a['verb'] in ('warn', 'keep'):
                continue
            action = {'backup': 'create', 'replace': 'modify'}.get(a['verb']) or \
                ('mkdir' if 'directory' in a['detail'].split(',')[0] else 'create')
            files.append({'path': BAK_RE.sub('.bak-<UTC>', self.show_path(a['path'])), 'action': action, 'to': None,
                          'diff': '',
                          'detail': a['detail'], 'by': 'install.py'})
        files += [
            {'path': self.show_path(p['login_kill']), 'action': 'rename',
             'to': self.show_path(p['login_kill']) + '.removed-<UTC>', 'diff': '', 'by': 'this page',
             'detail': f'retire {label}\'s login-wide pause switch, as on the other logins; every account added later '
                       f'starts paused with its own switch'},
            {'path': self.show_path(p['manual_env']), 'action': 'create', 'to': None, 'diff': '', 'by': 'this page',
             'detail': f'mode 0600; new {token_names(lid)["manual"]} for Flatten now (value not shown)'},
            {'path': self.show_path(p['dropin']), 'action': 'create', 'to': None, 'by': 'this page',
             'diff': '\n'.join('+' + ln for ln in self.dropin_text(lid).splitlines()),
             'detail': f'loads the Flatten-now token into tvbridge-daemon@{lid}.service, as for the other logins'}]
        for lg in state.logins:
            if not lg.get('primary'):
                files.append({'path': self.show_path(lg['config']), 'action': 'modify', 'to': None, 'diff': '',
                              'by': 'this page',
                              'detail': f'add {label} to {lg.get("label", lg["id"])}\'s peer_logins (read token '
                                        f'{token_names(lid)["read"]}), as install.py does for login A; its daemon '
                                        f'reads it at its next restart'})
        if os.path.isfile(p['admin_dropin']):
            files.append({'path': self.show_path(p['admin_dropin']), 'action': 'modify', 'to': None,
                          'by': 'this page', 'diff': '+' + self.manual_line(lid).rstrip('\n'),
                          'detail': 'this page\'s own drop-in: the new token at its next restart'})
        files.append({'path': self.show_path(record_path(self.p, lid)), 'action': 'create', 'to': None, 'diff': '',
                      'by': 'this page', 'detail': 'the login record: written before the first change, it lists '
                                                   'every path and unit, so an interrupted add can be cleaned up'})
        u = units_of(lid)
        plan['public_files'] = files
        plan['login'].update({
            'devtools_port': head['devtools'], 'bridge_port': head['listen'],
            'bridge_url': f'http://127.0.0.1:{head["listen"]}', 'profile_dir': self.show_path(p['profile']),
            'units': [
                {'unit': 'systemctl --user daemon-reload', 'action': 'run', 'when': 'now'},
                {'unit': u['browser'], 'action': 'enable, start', 'when': 'now',
                 'why': 'its own Tradovate window, for your sign-in'},
                {'unit': u['daemon'], 'action': 'enable', 'when': 'Start step',
                 'why': 'the login guard starts it once it sees you signed in'},
                {'unit': u['trigger'], 'action': 'enable, start', 'when': 'Start step'},
                {'unit': u['guard_timer'], 'action': 'enable, start', 'when': 'Start step'},
                {'unit': STATUS, 'action': 'restart', 'when': 'now', 'why': 'reads the new read-only token'}],
            'tokens': [
                {'name': token_names(lid)['order'], 'kind': 'order', 'files': [self.show_path(os.path.join(
                    p['home'], 'tvbridge.env')), self.show_path(p['firing_env'])],
                 'note': 'its daemon, guard, trigger and the firing service only'},
                {'name': token_names(lid)['read'], 'kind': 'read-only', 'files': [self.show_path(p['peers_env'])],
                 'note': '/health and /login only: other daemons, firing, the Bridge window, this page'},
                {'name': token_names(lid)['manual'], 'kind': 'flatten', 'files': [self.show_path(p['manual_env'])],
                 'note': 'Flatten now on this login only'}],
            'dry_run': dry['norm']})
        plan['watch'] = [p['registry'], p['peers_env'], p['firing_env'], p['admin_dropin']] + \
            [lg['config'] for lg in state.logins]

    def _inspect_start(self, state, plan):
        lid, label = plan['login']['id'], plan['login']['label']
        rec = read_record(self.p, lid)
        if not rec or rec.get('state') != 'created':
            plan['blockers'].append(f'{label} was not added by this page' + (f' (its add is {rec["state"]})' if rec
                                    else '') + ', so it is not started from here. Start its units by hand.')
            return
        st = self.unit_states(lid)
        plan['login']['units_now'] = st
        if st['browser']['active'] != 'active':
            plan['blockers'].append(f'{label}\'s browser window ({st["browser"]["unit"]}) is not running.')
        if st['trigger']['active'] == 'active' or st['guard_timer']['active'] == 'active':
            plan['blockers'].append(f'{label} is already started.')
        if st['daemon']['active'] in ('active', 'activating'):
            plan['blockers'].append(f'{label}\'s bridge daemon is already running.')
        u = units_of(lid)
        plan['login']['units'] = [
            {'unit': u['daemon'], 'action': 'enable', 'when': 'now', 'why': 'the login guard starts it after it '
                                                                            'sees you signed in'},
            {'unit': u['trigger'], 'action': 'enable, start', 'when': 'now'},
            {'unit': u['guard_timer'], 'action': 'enable, start', 'when': 'now'}]

    def _inspect_remove(self, state, plan, store):
        lid, label = plan['login']['id'], plan['login']['label']
        rec = read_record(self.p, lid)
        if not rec or rec.get('state') not in LIVE_RECORD:
            plan['blockers'].append(f'{label} was not added by this page, so it is not removed from here. '
                                    f'Remove it by hand.')
            return
        if rec.get('state') in INCOMPLETE:
            plan['warnings'].insert(0, f'{label}\'s add did not finish ({rec["state"]}). This cleans up what its '
                                       f'record lists, even parts that never reached logins.json.')
        if store is not None:
            busy = [c['id'] for c in store.all() if c['status'] in ('waiting_flat', 'applying')
                    and (c.get('params') or {}).get('login') == lid]
            if busy:
                plan['blockers'].append(f'A change for {label} is still pending ({", ".join(busy)}). Cancel it first.')
        p = self.paths(lid)
        stamp = '<UTC>'
        plan['public_files'] = [
            {'path': self.show_path(p['registry']), 'action': 'modify', 'to': None, 'diff': '', 'by': 'this page',
             'detail': f'remove login {lid}'},
            *[{'path': self.show_path(cfg_path), 'action': 'modify', 'to': None, 'diff': '', 'by': 'this page',
               'detail': f'remove {label} from peer_logins'}
              for cfg_path, cfg in state.login_cfg.values()
              if any(isinstance(x, dict) and x.get('id') == lid for x in cfg.get('peer_logins') or [])],
            {'path': self.show_path(p['peers_env']), 'action': 'modify', 'to': None, 'diff': '', 'by': 'this page',
             'detail': f'remove {token_names(lid)["read"]} (value not shown)'},
            {'path': self.show_path(p['firing_env']), 'action': 'modify', 'to': None, 'diff': '', 'by': 'this page',
             'detail': f'remove {token_names(lid)["order"]} (value not shown)'},
            {'path': self.show_path(p['home']), 'action': 'rename', 'to': self.show_path(p['home']) + f'.removed-{stamp}',
             'diff': '', 'by': 'this page', 'detail': 'the backup: config, journals, its token files and drop-in'},
            {'path': self.show_path(p['profile']), 'action': 'rename',
             'to': self.show_path(p['profile']) + f'.removed-{stamp}', 'diff': '', 'by': 'this page',
             'detail': 'its browser profile, kept'},
            {'path': self.show_path(p['dropin_dir']), 'action': 'rename', 'to': self.show_path(p['home'])
             + f'.removed-{stamp}/units/', 'diff': '', 'by': 'this page', 'detail': 'its daemon drop-in'}]
        if os.path.isfile(p['admin_dropin']):
            plan['public_files'].append({'path': self.show_path(p['admin_dropin']), 'action': 'modify', 'to': None,
                                         'diff': '-' + self.manual_line(lid).rstrip('\n'), 'by': 'this page',
                                         'detail': 'this page\'s own drop-in'})
        u = units_of(lid)
        plan['login']['units'] = [{'unit': x, 'action': 'stop, disable' if r != 'guard' else 'stop', 'when': 'now'}
                                  for r, x in (('trigger', u['trigger']), ('guard_timer', u['guard_timer']),
                                               ('guard', u['guard']), ('daemon', u['daemon']),
                                               ('browser', u['browser']))]
        plan['watch'] = [p['registry'], p['peers_env'], p['firing_env'], p['admin_dropin'],
                         record_path(self.p, lid)] + [lg['config'] for lg in state.logins]

    # ----- apply -----
    def apply(self, ch, state, plan, step, restart):
        """Run the login change. `step(name, ok, detail)` records progress on the change; `restart(units,
        phase)` runs the restarter. Returns the result's `login` block. Raises StepFailed after a verified
        undo, or UndoIncomplete (needs a person) with the exact paths and units when the undo did not finish."""
        act = plan['action']
        if act == 'add_login':
            return self._apply_add(ch, state, plan, step, restart)
        if act == 'start_login':
            return self._apply_start(ch, state, plan, step)
        return self._apply_remove(ch, state, plan, step)

    def _bridge(self):
        root = self.p.data.get('bridge_release')
        if not root or not os.path.isdir(os.path.join(root, 'tvbridge')):
            return None
        if root not in sys.path:
            sys.path.insert(0, root)
        return (importlib.import_module('tvbridge.logins'), importlib.import_module('tvbridge.followers'))

    kill_hook = None   # tests: called with each record step name; raising Killed stands for a killed service

    def _point(self, rec, name):
        """Progress on the durable record (fsynced), then the test hook."""
        rec.setdefault('steps', []).append(name)
        write_record(self.p, rec, self.clock)
        if self.kill_hook:
            self.kill_hook(name)

    def _apply_add(self, ch, state, plan, step, restart):
        lid, label = plan['login']['id'], plan['login']['label']
        p, u = self.paths(lid), units_of(lid)
        primary_path = state.login_cfg[state.primary_login()][0]
        others = [lg['config'] for lg in state.logins if not lg.get('primary')]
        dry = self.install(lid, label, dry=True)
        if not dry['ok'] or dry['norm'] != plan['login'].get('dry_run'):
            raise StepFailed('install.py add-login --dry-run no longer matches the preview'
                             + (f' ({dry["error"]})' if dry['error'] else '') + '. Preview again. Nothing was changed.')
        step('dry run', True, f'install.py add-login {lid} --dry-run: {len(dry["actions"])} action(s), the same as '
                              f'the preview')
        tag = f'addlogin-{ch["id"]}'
        creates = [a['path'] for a in dry['actions'] if a['verb'] == 'create']
        mods = list(dict.fromkeys([p['registry'], primary_path, p['peers_env'], p['firing_env']] + others +
                                  [p['admin_dropin']]))
        rec = {'schema': RECORD_SCHEMA, 'id': lid, 'label': label, 'change_id': ch['id'], 'state': 'adding',
               'created_at': self.clock(), 'steps': [],
               'devtools_port': dry['head']['devtools'], 'bridge_port': dry['head']['listen'],
               'creates': creates + [p['manual_env'], p['dropin_dir'], p['dropin'], p['login_kill'] + '.removed-*'],
               'precreated': {x: os.path.lexists(x) for x in creates},
               'modifies': [{'path': x, 'sha': file_sha(x), 'original': f'{x}.{tag}' if os.path.isfile(x) else None}
                            for x in mods],
               'units': list(u.values()), 'profile_dir': p['profile'],
               'token_digests': {'peers': env_digests(p['peers_env']), 'firing': env_digests(p['firing_env'])},
               'undo': f'Remove login {label} (id {lid}) cleans up everything listed here.'}
        self._point(rec, 'record')                     # durable (fsynced), before the first write
        step('record', True, f'login record {self.show_path(record_path(self.p, lid))}: {len(rec["creates"])} '
                             f'path(s) to create, {len(mods)} to modify, {len(rec["units"])} unit(s)')
        done = {'installed': False, 'manual_env': False, 'dropin': False, 'reload': False, 'enabled': [],
                'started': [], 'status': False}
        real = None
        try:
            for m in rec['modifies']:
                if m['original']:
                    keep_original(m['path'], tag)
            self._point(rec, 'originals')
            step('backup', True, f'{sum(1 for m in rec["modifies"] if m["original"])} original(s) kept as hard links '
                                 f'(*.{tag}); no token value is read')
            try:
                real = self.install(lid, label, dry=False)
            finally:
                # Only an install.py that got past its own checks (it printed its header) may have written
                # anything; a refusal (for example a folder that appeared meanwhile) leaves it all alone.
                done['installed'] = bool(real and real['head'])
            self._point(rec, 'install.py')
            if not real['ok']:
                raise StepFailed(f'install.py add-login failed: {real["error"]}')
            if real['norm'] != dry['norm']:
                raise StepFailed('install.py did something other than its dry run')
            step('install.py add-login', True, f'login {lid}: DevTools {real["head"]["devtools"]}, loopback '
                                              f'{real["head"]["listen"]}; {len(real["actions"])} action(s)')
            self._check_install(state, lid, label, p, primary_path, rec, real['head'])
            self._point(rec, 'check')
            step('check', True, 'registry entry and login A\'s new peer as in the dry run; tokens of the existing '
                                'logins unchanged (SHA-256); the bridge validators accept the new login')
            peer = {'id': lid, 'url': f'http://127.0.0.1:{real["head"]["listen"]}', 'token_env': token_names(lid)['read']}
            for path in others:
                cfg = load_json(path)[0]
                if not any(isinstance(x, dict) and x.get('id') == lid for x in cfg.get('peer_logins') or []):
                    cfg['peer_logins'] = list(cfg.get('peer_logins') or []) + [peer]
                    FileOps.atomic_write(path, json_text_like(path, cfg))
            rec['peers_written_at'] = self.clock()
            self._point(rec, 'peers')
            step('peers', True, f'{label} added to the peer list of ' + (', '.join(self.show_path(x) for x in others)
                                                                         or 'no other login') +
                 '; those daemons read it at their next restart')
            if os.path.exists(p['login_kill']):
                os.rename(p['login_kill'], f'{p["login_kill"]}.removed-{utc_stamp(self.clock())}')
            self._point(rec, 'login switch')
            step('login switch', True, f'{label}\'s login-wide pause switch retired, as on the other logins (each account '
                                       f'added later starts paused with its own switch)')
            done['manual_env'] = True
            write_private(p['manual_env'], f'{token_names(lid)["manual"]}={secrets.token_urlsafe(32)}\n',
                          exclusive=True)
            self._point(rec, 'flatten token')
            done['dropin'] = True
            os.makedirs(p['dropin_dir'], mode=0o700)
            write_private(p['dropin'], self.dropin_text(lid), exclusive=True)
            self._point(rec, 'drop-in')
            if os.path.isfile(p['admin_dropin']):
                with open(p['admin_dropin']) as f:
                    text = f.read()
                line = self.manual_line(lid)
                if line not in text:
                    write_private(p['admin_dropin'], text + ('' if text.endswith('\n') or not text else '\n') + line)
            self._point(rec, 'admin drop-in')
            step('flatten token', True, f'{token_names(lid)["manual"]} written (0600, not shown); drop-in for '
                                        f'{u["daemon"]} and this page')
            done['reload'] = True
            self._sc_ok(lid, 'daemon-reload')
            self._point(rec, 'daemon-reload')
            done['enabled'].append(u['browser'])
            self._sc_ok(lid, 'enable', u['browser'])
            self._point(rec, 'enable browser')
            done['started'].append(u['browser'])
            self._sc_ok(lid, 'start', u['browser'])
            self._wait_active(u['browser'], 10)
            self._point(rec, 'start browser')
            step('browser', True, f'{u["browser"]} enabled and started (DevTools {real["head"]["devtools"]}); its '
                                  f'daemon, trigger and guard wait for the Start step')
            done['status'] = True
            results = restart(plan['restarts'], 'apply')
            self._point(rec, 'restart status')
            bad = [r for r in results if r['ok'] is False]
            step('restart', not bad, '; '.join(f'{r["unit"]}: {r["detail"]}' for r in results))
            if bad:
                raise StepFailed('the Bridge window\'s data service did not come back')
            st = self.unit_states(lid)
            early = [st[r]['unit'] for r in ('daemon', 'trigger', 'guard_timer')
                     if st[r]['active'] in ('active', 'activating')]
            health = self.restarter.health(plan['restarts'])
            ok = st['browser']['active'] == 'active' and not early and all(health.values()) \
                and os.path.isfile(p['config']) and os.path.isfile(p['manual_env']) and os.path.isfile(p['dropin'])
            step('verify', ok, 'browser running, daemon not started, files in place, Bridge window answers' if ok else
                 f'mismatch: browser {st["browser"]["active"]}, started too early: {early or "none"}, '
                 f'health {health}')
            if not ok:
                raise StepFailed('verify failed')
            self._point(rec, 'verify')
        except Exception as exc:
            msg = str(exc) if isinstance(exc, StepFailed) else f'{type(exc).__name__}: {exc}'
            incomplete = None
            try:
                self._undo_add(lid, p, rec, done, step)
            except UndoIncomplete as ui:
                incomplete = ui
            finally:
                if done['status']:       # the panel was restarted with the new files: restart it on the old ones
                    try:
                        results = restart(plan['restarts'], 'rollback')
                        step('restart old files', all(r['ok'] is not False for r in results),
                             '; '.join(f'{r["unit"]}: {r["detail"]}' for r in results))
                    except Exception as rexc:  # noqa: BLE001
                        step('restart old files', False, f'{type(rexc).__name__}: {rexc}')
            rec['state'] = 'add_failed' if incomplete else 'undone'
            rec['error'] = msg[:300]
            write_record(self.p, rec, self.clock)
            if incomplete:
                raise UndoIncomplete(f'{msg}. Undo incomplete: {incomplete}', incomplete.paths, incomplete.units)
            raise StepFailed(msg)
        for m in rec['modifies']:                 # done: the hard-linked originals are not needed any more
            if m['original'] and os.path.lexists(m['original']):
                os.remove(m['original'])
        rec['state'] = 'created'
        write_record(self.p, rec, self.clock)
        return {'id': lid, 'label': label, 'stage': 'awaiting_signin', 'devtools_port': real['head']['devtools'],
                'bridge_port': real['head']['listen'], 'browser_unit': u['browser'], 'next': 'start_login',
                'text': f'{label} is ready for your sign-in. Sign in to Tradovate in its window, then press Start.'}

    def _check_install(self, state, lid, label, p, primary_path, rec, head):
        orig = {m['path']: m for m in rec['modifies']}
        before_reg = load_json(orig[p['registry']]['original'])[0]
        before_primary = load_json(orig[primary_path]['original'])[0]
        reg, _, _ = load_json(p['registry'])
        old_ids = [e['id'] for e in before_reg['logins']]
        if [e['id'] for e in reg['logins']] != old_ids + [lid] or reg['logins'][:-1] != before_reg['logins']:
            raise StepFailed('logins.json changed in an unexpected way')
        e = reg['logins'][-1]
        u = units_of(lid)
        want = {'label': label, 'primary': False, 'config': p['config'], 'bridge_url': f'http://127.0.0.1:{head["listen"]}',
                'devtools_port': head['devtools'], 'browser_unit': u['browser'], 'daemon_unit': u['daemon'],
                'trigger_unit': u['trigger'], 'token_env': token_names(lid)['order'],
                'read_token_env': token_names(lid)['read']}
        if any(e.get(k) != v for k, v in want.items()):
            raise StepFailed('the new logins.json entry is not what the dry run showed')
        prim, _, _ = load_json(primary_path)
        exp = dict(before_primary, peer_logins=list(before_primary.get('peer_logins') or []) + [
            {'id': lid, 'url': f'http://127.0.0.1:{head["listen"]}', 'token_env': token_names(lid)['read']}])
        if prim != exp:
            raise StepFailed('login A\'s config changed beyond the new peer entry')
        for key, path, new in (('peers', p['peers_env'], token_names(lid)['read']),
                               ('firing', p['firing_env'], token_names(lid)['order'])):
            after, before = env_digests(path), rec['token_digests'][key]
            changed = sorted(k for k, v in before.items() if after.get(k) != v)
            if changed:
                raise StepFailed(f'{os.path.basename(path)}: token(s) of existing logins changed: {", ".join(changed)}')
            if after.get(new, [None, 0])[1] < 24 or set(after) - set(before) != {new}:
                raise StepFailed(f'{os.path.basename(path)}: the new token is missing or another name was added')
            if os.stat(path).st_mode & 0o077:
                raise StepFailed(f'{os.path.basename(path)} is readable by others')
        with open(p['config']) as f:
            cfg = json.load(f)
        if cfg.get('mode') != 'followers_only' or cfg.get('followers') or cfg.get('login_id') != lid \
                or cfg.get('account_name'):
            raise StepFailed('the new login config is not an empty followers-only login')
        mods = self._bridge()
        if mods:
            try:
                mods[0].validate(reg)
                mods[0].daemon_settings(cfg)
                mods[1].parse(cfg)
            except ValueError as exc:
                raise StepFailed(f'the bridge rejects the new login: {exc}')
        from model import Draft, State, validate
        validate(Draft(State(self.p)))

    def _undo_add(self, lid, p, rec, done, step):
        """Exact undo, in order: units; the files install.py and this page rewrote, byte for byte from their
        hard-linked originals (verified by sha256); daemon-reload; then what was created. Raises
        UndoIncomplete naming every path or unit that is not back."""
        errors, bad_paths, bad_units = [], [], []

        def attempt(what, fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - keep undoing the rest
                errors.append(f'{what}: {type(exc).__name__}: {exc}')
        for unit in reversed(done['started']):
            attempt(f'stop {unit}', lambda unit=unit: self._sc_ok(lid, 'stop', unit))
        for unit in reversed(done['enabled']):
            attempt(f'disable {unit}', lambda unit=unit: self._sc_ok(lid, 'disable', unit))
        if done['dropin'] and os.path.isdir(p['dropin_dir']):
            attempt('drop-in', lambda: shutil.rmtree(p['dropin_dir']))
        if done['manual_env'] and os.path.exists(p['manual_env']):
            attempt('token file', lambda: os.remove(p['manual_env']))
        for m in rec['modifies']:
            ok = False
            try:
                ok = put_back(m['path'], m['original'], m['sha'])
            except Exception as exc:  # noqa: BLE001
                errors.append(f'restore {m["path"]}: {type(exc).__name__}')
            if not ok:
                bad_paths.append(m['path'])
        if done['reload']:
            attempt('daemon-reload', lambda: self._sc_ok(lid, 'daemon-reload'))
        if done['installed']:
            attempt('created files', lambda: self._remove_created(lid, rec))
        st = {}
        try:
            st = self.unit_states(lid)
        except Exception as exc:  # noqa: BLE001
            errors.append(f'unit check: {type(exc).__name__}')
        bad_units = [x['unit'] for x in st.values() if x['active'] in ('active', 'activating')
                     or x['enabled'] == 'enabled']
        left = [x for x in (p['manual_env'], p['dropin_dir']) if os.path.lexists(x)]
        left += [x for x in (p['home'], p['profile']) if os.path.lexists(x) and not rec['precreated'].get(x)]
        ok = not errors and not bad_paths and not bad_units and not left
        if ok:
            for m in rec['modifies']:
                if m['original'] and os.path.lexists(m['original']):
                    os.remove(m['original'])
        step('rollback', ok, 'everything undone; every rewritten file is byte-identical to its original (sha256)'
             if ok else 'undo incomplete. Not restored: ' + ', '.join(bad_paths + left + bad_units)
             + (f'; errors: {"; ".join(errors)[:300]}' if errors else ''))
        if not ok:
            raise UndoIncomplete('not back: ' + ', '.join(bad_paths + left + bad_units),
                                 bad_paths + left, bad_units)

    def _remove_created(self, lid, rec):
        """Remove what install.py created for this login, only paths that did not exist before it ran. The
        login folder is deleted only when it holds nothing but install.py's own files; the browser profile only
        while it holds just its marker (a used profile, or anything else, is moved aside as *.failed-<UTC>)."""
        p = self.paths(lid)
        pre = rec.get('precreated') or {}
        home = p['home']
        if os.path.isdir(home) and not pre.get(home):
            known = {'config.json', 'login.env', 'tvbridge.env', 'journal', 'ENTRIES-DISABLED'}
            names = set(os.listdir(home))
            extra = {n for n in names if n not in known and not n.startswith('ENTRIES-DISABLED.removed-')}
            if extra or (os.path.isdir(os.path.join(home, 'journal')) and os.listdir(os.path.join(home, 'journal'))):
                os.rename(home, f'{home}.failed-{utc_stamp(self.clock())}')
            else:
                shutil.rmtree(home)
        prof = p['profile']
        if os.path.isdir(prof) and not pre.get(prof):
            if set(os.listdir(prof)) <= {'.tvbridge-login'}:
                shutil.rmtree(prof)
            else:
                os.rename(prof, f'{prof}.failed-{utc_stamp(self.clock())}')

    def _sc_ok(self, lid, verb, unit=None):
        code, _, err = self.sc(lid, verb, unit)
        if code != 0:
            raise StepFailed(f'systemctl {verb} {unit or ""} failed: {(err or "").strip()[:200] or code}')

    def _wait_active(self, unit, seconds):
        deadline = self.clock() + seconds
        while True:
            sh = self.restarter.show(unit)
            if sh['active'] == 'active':
                return
            if sh['active'] == 'failed' or self.clock() >= deadline:
                raise StepFailed(f'{unit} is {sh["active"]}/{sh["sub"]}, not running')
            self.sleep(0.2)

    def _restore_units(self, lid, before):
        """Put the login's units back as they were (enabled / running). The daemon is never started from here;
        the login guard starts it again when it sees the login signed in."""
        u = units_of(lid)
        for role in ('browser', 'daemon', 'trigger', 'guard_timer'):
            if before[role]['enabled'] == 'enabled':
                self._sc_ok(lid, 'enable', u[role])
            elif before[role]['enabled'] in ('disabled', ''):
                self._sc_ok(lid, 'disable', u[role])
        for role in ('browser', 'guard_timer', 'trigger'):
            if before[role]['active'] == 'active':
                self._sc_ok(lid, 'start', u[role])
            else:
                self._sc_ok(lid, 'stop', u[role])

    def _units_differ(self, lid, before):
        now = self.unit_states(lid)
        return [f'{now[r]["unit"]} ({now[r]["active"]}, {now[r]["enabled"]})' for r in ('browser', 'trigger',
                                                                                         'guard_timer', 'daemon')
                if (now[r]['active'] == 'active') != (before[r]['active'] == 'active')
                or (now[r]['enabled'] == 'enabled') != (before[r]['enabled'] == 'enabled')]

    def _apply_start(self, ch, state, plan, step):
        """Enable and start the watchers, then return at once (the engine lock is not held while the login
        signs in and syncs). The Logins row (overview) reports connected / signed out / starting."""
        lid, label = plan['login']['id'], plan['login']['label']
        rec = read_record(self.p, lid)
        if not rec or rec.get('state') != 'created':
            raise StepFailed(f'{label} was not added by this page; nothing was started')
        u = units_of(lid)
        before = self.unit_states(lid)
        try:
            for unit in (u['daemon'], u['trigger'], u['guard_timer']):
                self._sc_ok(lid, 'enable', unit)
            for unit in (u['guard_timer'], u['trigger']):
                self._sc_ok(lid, 'start', unit)
        except Exception as exc:
            msg = str(exc) if isinstance(exc, StepFailed) else f'{type(exc).__name__}: {exc}'
            try:
                self._restore_units(lid, before)
            except Exception:  # noqa: BLE001 - the check below says what is left
                pass
            left = self._units_differ(lid, before)
            if left:
                step('rollback', False, 'not back as before: ' + ', '.join(left))
                raise UndoIncomplete(f'Start failed ({msg}). Still not as before: {", ".join(left)}', [],
                                     [x.split(' ')[0] for x in left])
            step('rollback', True, f'{label}\'s units are back as they were')
            raise StepFailed(msg)
        rec['started_at'] = self.clock()
        rec.setdefault('steps', []).append('started')
        write_record(self.p, rec, self.clock)
        step('start', True, f'enabled {u["daemon"]}; started {u["guard_timer"]} and {u["trigger"]}. The login '
                            f'guard starts the daemon when it sees {label} signed in')
        self._signin_cache.pop(lid, None)
        return {'id': lid, 'label': label, 'stage': 'running', 'connected': False, 'state': 'starting',
                'text': f'Started. The login guard starts {label}\'s bridge when it sees you signed in (it checks '
                        f'about every 30 s). The {label} row shows when it is synced.'}

    def signin_state(self, lid, label=None):
        """(state, text) for a started login: connected | signed_out | starting | bad_token. Read only."""
        label = label or f'Login {lid.upper()}'
        u = units_of(lid)
        verdict, detail, _ = self.restarter.daemon_reading(u['daemon'])
        if verdict == 'synced':
            return 'connected', f'{label} is signed in and its bridge is synced. Add its accounts with Add account.'
        if verdict == 'bad_token':
            return 'bad_token', f'{label}\'s bridge rejected the read-only token ({detail}).'
        entry = next((e for e in self.restarter.logins().values() if e['id'] == lid), None)
        gstate, greason = self.restarter.guard_reading(entry)
        if gstate == 'out':
            return 'signed_out', (f'The login guard sees {label} signed out ({greason}). Sign in by hand in the '
                                  f'{label} window; the guard starts its bridge once you are signed in.')
        return 'starting', (f'Not synced yet ({detail}). The login guard starts {label}\'s bridge when it sees you '
                            f'signed in; it checks about every 30 s.')

    def peer_blockers(self, state, lid):
        """add_account on a login this page created: every other login's config must list it as a peer and
        that login's daemon must have (re)started since, so its cross-login hedge check sees the new login."""
        rec = read_record(self.p, lid)
        if not rec or rec.get('state') != 'created':
            return []
        label = rec.get('label') or f'Login {lid.upper()}'
        since = rec.get('peers_written_at') or rec.get('created_at') or 0
        missing, stale = [], []
        for lg in state.logins:
            if lg['id'] == lid:
                continue
            name = lg.get('label', lg['id'])
            cfg = state.login_cfg[lg['id']][1]
            if not any(isinstance(x, dict) and x.get('id') == lid for x in cfg.get('peer_logins') or []):
                missing.append(name)
                continue
            code, out, _ = self.restarter.sc('show', '--timestamp=unix', lg['daemon_unit'], '-p',
                                             'ActiveEnterTimestamp', timeout=15)
            m = re.search(r'ActiveEnterTimestamp=@(\d+)', out or '') if code == 0 else None
            if not m or int(m.group(1)) < since:
                stale.append(name)
        out = []
        if missing:
            out.append(f'{" and ".join(missing)} do not list {label} as a peer in their config yet.')
        if stale:
            names = 'Login ' + '/'.join(n.replace('Login ', '') for n in stale) if all(
                n.startswith('Login ') for n in stale) else '/'.join(stale)
            out.append(f'Adding accounts to {label} needs a restart of {names} while flat and signed in to see '
                       f'{label}.')
        return out

    def _apply_remove(self, ch, state, plan, step):
        lid, label = plan['login']['id'], plan['login']['label']
        p, u = self.paths(lid), units_of(lid)
        rec = read_record(self.p, lid)
        if not rec or rec.get('state') not in LIVE_RECORD:
            raise StepFailed(f'{label} was not added by this page; nothing was removed')
        stamp = utc_stamp(self.clock())
        dest = f'{p["home"]}.removed-{stamp}'
        prof_dest = f'{p["profile"]}.removed-{stamp}'
        tag = f'rmlogin-{ch["id"]}'
        prior_state = rec['state']
        units_before = self.unit_states(lid)
        before_reg = load_json(p['registry'])[0]
        cfg_paths = [path for path, cfg in state.login_cfg.values()
                     if any(isinstance(x, dict) and x.get('id') == lid for x in cfg.get('peer_logins') or [])]
        mods = list(dict.fromkeys([p['registry'], p['peers_env'], p['firing_env'], p['admin_dropin']] + cfg_paths))
        rec['state'], rec['remove'] = 'removing', {
            'change_id': ch['id'], 'dest': dest, 'profile_dest': prof_dest,
            'modifies': [{'path': x, 'sha': file_sha(x), 'original': f'{x}.{tag}' if os.path.isfile(x) else None}
                         for x in mods]}
        write_record(self.p, rec, self.clock)        # durable, before the first change
        digests = {k: env_digests(path) for k, path in (('peers', p['peers_env']), ('firing', p['firing_env']))}
        moved = []                                    # (from, to) for the undo
        try:
            for m in rec['remove']['modifies']:
                if m['original']:
                    keep_original(m['path'], tag)
            for unit in (u['trigger'], u['guard_timer'], u['guard'], u['daemon'], u['browser']):
                self._sc_ok(lid, 'stop', unit)
            for unit in (u['trigger'], u['guard_timer'], u['daemon'], u['browser']):
                self._sc_ok(lid, 'disable', unit)
            st = self.unit_states(lid)
            running = [x['unit'] for x in st.values() if x['active'] in ('active', 'activating')]
            if running:
                raise StepFailed('still running: ' + ', '.join(running))
            step('units', True, f'stopped and disabled {label}\'s browser, daemon, trigger and guard')
            ops = []
            if any(e['id'] == lid for e in before_reg['logins']):
                ops.append({'path': p['registry'], 'action': 'write', 'content': json_text_like(
                    p['registry'], dict(before_reg, logins=[e for e in before_reg['logins'] if e['id'] != lid]))})
            for path in cfg_paths:
                cfg = load_json(path)[0]
                cfg['peer_logins'] = [x for x in cfg['peer_logins'] if not (isinstance(x, dict) and x.get('id') == lid)]
                ops.append({'path': path, 'action': 'write', 'content': json_text_like(path, cfg)})
            self.files.apply(ops)
            for key, path, name in (('peers', p['peers_env'], token_names(lid)['read']),
                                    ('firing', p['firing_env'], token_names(lid)['order'])):
                if os.path.isfile(path):
                    drop_env_names(path, {name})
                after = env_digests(path)
                changed = sorted(k for k, v in digests[key].items() if k != name and after.get(k) != v)
                if changed or name in after:
                    raise StepFailed(f'{os.path.basename(path)}: unexpected token change ({", ".join(changed) or name})')
            step('write', True, f'{label} removed from logins.json, the peer lists, peers.env and firing.env; the '
                                f'other logins\' tokens are unchanged (SHA-256)')
            if os.path.isdir(p['home']):
                os.rename(p['home'], dest)
                moved.append((p['home'], dest))
            else:                                     # an add that stopped before install.py made the folder
                os.makedirs(dest, mode=0o700)
            if os.path.exists(p['manual_env']):
                kept = os.path.join(dest, os.path.basename(p['manual_env']))
                os.rename(p['manual_env'], kept)
                moved.append((p['manual_env'], kept))
            if os.path.isdir(p['dropin_dir']):
                os.makedirs(os.path.join(dest, 'units'), mode=0o700, exist_ok=True)
                kept = os.path.join(dest, 'units', os.path.basename(p['dropin_dir']))
                shutil.move(p['dropin_dir'], kept)
                moved.append((p['dropin_dir'], kept))
            if os.path.isfile(p['admin_dropin']):
                with open(p['admin_dropin']) as f:
                    text = f.read()
                if self.manual_line(lid) in text:
                    write_private(p['admin_dropin'], text.replace(self.manual_line(lid), ''))
            if os.path.isdir(p['profile']):
                os.rename(p['profile'], prof_dest)
                moved.append((p['profile'], prof_dest))
            else:
                prof_dest = None
            # originals left by an interrupted add (*.addlogin-<id>): kept inside the backup folder
            for m in (rec.get('modifies') or []):
                if m.get('original') and os.path.lexists(m['original']):
                    os.makedirs(os.path.join(dest, 'originals'), mode=0o700, exist_ok=True)
                    kept = os.path.join(dest, 'originals', os.path.basename(m['original']))
                    os.rename(m['original'], kept)
                    moved.append((m['original'], kept))
            step('backup kept', True, self.show_path(dest) + (f' and {self.show_path(prof_dest)}' if prof_dest else ''))
            self._sc_ok(lid, 'daemon-reload')
            from model import Draft, State, validate
            validate(Draft(State(self.p)))
            left = self._leftovers(lid)
            ok = not left and lid not in [e['id'] for e in load_json(p['registry'])[0]['logins']]
            step('verify', ok, 'login removed; its backup is in place' if ok else 'left over: ' +
                 ', '.join(self.show_path(x) for x in left))
            if not ok:
                raise StepFailed('verify failed')
        except Exception as exc:
            msg = str(exc) if isinstance(exc, StepFailed) else f'{type(exc).__name__}: {exc}'
            errors, bad = [], []
            for src, dst in reversed(moved):
                try:
                    shutil.move(dst, src)
                except Exception as e2:  # noqa: BLE001
                    errors.append(f'{src}: {type(e2).__name__}')
                    bad.append(src)
            for m in rec['remove']['modifies']:
                try:
                    if not put_back(m['path'], m['original'], m['sha']):
                        bad.append(m['path'])
                except Exception as e2:  # noqa: BLE001
                    errors.append(f'{m["path"]}: {type(e2).__name__}')
                    bad.append(m['path'])
            try:
                self._sc_ok(lid, 'daemon-reload')
                self._restore_units(lid, units_before)
            except Exception as e2:  # noqa: BLE001
                errors.append(f'units: {type(e2).__name__}: {e2}')
            left_units = self._units_differ(lid, units_before)
            rec['state'] = prior_state
            write_record(self.p, rec, self.clock)
            if bad or errors or left_units:
                step('rollback', False, 'undo incomplete. Not restored: ' + ', '.join(bad + left_units))
                raise UndoIncomplete(f'{msg}. Undo incomplete: ' + ', '.join(bad + left_units),
                                     bad, [x.split(' ')[0] for x in left_units])
            for m in rec['remove']['modifies']:
                if m['original'] and os.path.lexists(m['original']):
                    os.remove(m['original'])
            step('rollback', True, 'everything back as before (files byte-identical, sha256; units as they were)')
            raise StepFailed(msg)
        for m in rec['remove']['modifies']:
            if m['original'] and os.path.lexists(m['original']):
                os.remove(m['original'])
        rec['state'], rec['removed_at'] = 'removed', self.clock()
        rec['remove'].update(dest=dest, profile_dest=prof_dest)
        write_record(self.p, rec, self.clock)
        return {'id': lid, 'label': label, 'stage': 'removed', 'backup': self.show_path(dest),
                'profile_backup': self.show_path(prof_dest) if prof_dest else None,
                'text': f'{label} removed. Backup: {self.show_path(dest)}.'}

    # ----- overview (no engine lock) -----
    def view(self, state, changes):
        """Per-login setup block for the page, the feature switch and logins whose add did not finish."""
        ok, why = self.available()
        out = {'available': ok, 'reason': why, 'next_id': None, 'incomplete': [], 'logins': {}}
        if not ok:
            return out
        try:
            import plans
            out['next_id'] = plans.next_login_id(state)
        except Exception:  # noqa: BLE001
            out['next_id'] = None
        recs = {r['id']: r for r in all_records(self.p)}
        out['incomplete'] = [{'id': r['id'], 'label': r.get('label'), 'state': r['state'], 'change_id': r.get('change_id'),
                              'error': r.get('error'),
                              'text': f'{r.get("label")} (id {r["id"]}): the add did not finish ({r["state"]}). Use '
                                      f'Remove to clean up what its record lists.'}
                             for r in recs.values() if r.get('state') in INCOMPLETE]
        now = self.clock()
        cache_at, cache = self._unit_cache
        if cache is None or now - cache_at > 5:
            cache = {}
            for lg in state.logins:
                if not lg.get('primary'):
                    try:
                        cache[lg['id']] = self.unit_states(lg['id'])
                    except Exception:  # noqa: BLE001
                        cache[lg['id']] = None
            self._unit_cache = (now, cache)
        for lg in state.logins:
            lid = lg['id']
            if lg.get('primary'):
                continue
            label = lg.get('label', lid)
            rec = recs.get(lid)
            mine = rec is not None and rec.get('state') in LIVE_RECORD
            units = cache.get(lid)
            accounts = [k for k, a in state.accounts.items() if a['login'] == lid]
            if rec and rec.get('state') in INCOMPLETE:
                stage = 'incomplete'
            elif units and units['trigger']['active'] == 'active':
                stage = 'running'
            elif units and units['browser']['active'] == 'active':
                stage = 'awaiting_signin'
            else:
                stage = 'stopped' if units else 'unknown'
            signin = None
            if mine and rec.get('started_at') and stage == 'running':
                at, val = self._signin_cache.get(lid, (0, None))
                if val is None or now - at > 5:
                    try:
                        val = self.signin_state(lid, label)
                    except Exception as exc:  # noqa: BLE001
                        val = ('starting', f'No reading yet ({type(exc).__name__}).')
                    self._signin_cache[lid] = (now, val)
                signin = {'state': val[0], 'text': val[1]}
            if not mine:
                blocker = f'{label} was not added by this page. Start or remove it by hand.'
            elif accounts:
                blocker = f'Retire its accounts first ({len(accounts)}).'
            else:
                blocker = None
            out['logins'][lid] = {
                'stage': stage, 'origin': 'this_page' if mine else 'outside', 'units': units,
                'devtools_port': lg.get('devtools_port'), 'bridge_url': lg.get('bridge_url'),
                'added_by': rec.get('change_id') if rec else None, 'signin': signin,
                'can_start': mine and rec.get('state') == 'created' and stage == 'awaiting_signin',
                'start_blocker': None if mine else f'{label} was not added by this page.',
                'can_remove': mine and not accounts, 'remove_blocker': blocker,
                'account_blockers': self._cached(f'peers-{lid}', now, lambda: self.peer_blockers(state, lid))
                if mine and rec.get('state') == 'created' else []}
        return out

    def _cached(self, key, now, fn):
        at, val = self._signin_cache.get(key, (0, None))
        if val is None or now - at > 5:
            try:
                val = fn()
            except Exception as exc:  # noqa: BLE001
                val = [f'Peer check failed ({type(exc).__name__}).']
            self._signin_cache[key] = (now, val)
        return val
