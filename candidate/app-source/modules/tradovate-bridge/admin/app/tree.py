"""File access for tvbridge-admin: profile paths, JSON with preserved style, atomic writes, backups.

Every write goes through `FileOps`, so nothing outside the profile's roots can change. In sandbox
mode the roots are the copied tree under tvbridge-admin/sandbox/.
"""
import hashlib
import json
import os
import re
import shutil
import time

ADMIN_MARK = 'tvbridge-admin'
LIVE_SYSTEMCTL = '/usr/bin/systemctl'
LIVE_STATUS_API = 'http://127.0.0.1:8779/api/status'
SOUP_UNIT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\.service\Z')


class ProfileError(ValueError):
    pass


class Profile:
    """Paths and adapters chosen at start. `mode` is "sandbox" or "live"."""

    REQUIRED = ('mode', 'tvbridge_root', 'soup_state', 'admin_state', 'status_url', 'port', 'user',
                'public_host', 'restarter', 'order_adapter', 'strategies')

    def __init__(self, data, path=None):
        missing = [k for k in self.REQUIRED if k not in data]
        if missing:
            raise ProfileError('profile is missing ' + ', '.join(missing))
        if data['mode'] not in ('sandbox', 'live'):
            raise ProfileError('mode must be sandbox or live')
        if data['restarter'] not in ('simulated', 'systemd'):
            raise ProfileError('restarter must be simulated or systemd')
        if data['order_adapter'] not in ('dry-run', 'bridge'):
            raise ProfileError('order_adapter must be dry-run or bridge')
        ob = data.get('order_bridge') or {}
        if not isinstance(ob, dict):
            raise ProfileError('order_bridge must be an object')
        if data['order_adapter'] == 'bridge':
            if data['mode'] == 'sandbox' and not isinstance(ob.get('logins_override'), dict):
                # Sandbox Flatten now only ever talks to a fake bridge, never the live daemons.
                raise ProfileError('sandbox mode sends Flatten now only to fake bridges (order_bridge.logins_override)')
            if data['mode'] == 'live' and ob.get('logins_override'):
                raise ProfileError('order_bridge.logins_override is for sandbox tests only')
            if not 0 < float(ob.get('timeout_s', 60)) <= 120:
                raise ProfileError('order_bridge.timeout_s must be above 0 and at most 120')
        sd = data.get('systemd') or {}
        if not isinstance(sd, dict):
            raise ProfileError('systemd must be an object')
        if data['mode'] == 'sandbox' and data['restarter'] == 'systemd':
            # The real restart path runs in the sandbox only against a fake systemctl and fake health
            # endpoints: never the system systemctl, never a live daemon, guard or status panel.
            argv = sd.get('systemctl')
            argv = [argv] if isinstance(argv, str) else argv
            if not argv or not all(isinstance(a, str) for a in argv) or not os.path.isabs(argv[0]) \
                    or any(_is_real_systemctl(a) for a in argv) \
                    or not any(a.endswith(os.path.join('tools', 'fake_systemctl.py')) for a in argv):
                raise ProfileError('sandbox mode runs the systemd restarter only through tools/fake_systemctl.py')
            if not isinstance(sd.get('logins_override'), dict) or not sd.get('status_api_url'):
                raise ProfileError('sandbox mode needs systemd.logins_override and systemd.status_api_url '
                                   '(fake health endpoints)')
        if data['mode'] == 'live' and sd.get('logins_override'):
            raise ProfileError('systemd.logins_override is for sandbox tests only')
        if data['mode'] == 'live' and data['restarter'] == 'systemd':
            if sd.get('systemctl') != [LIVE_SYSTEMCTL]:
                raise ProfileError(f'live mode needs systemd.systemctl exactly ["{LIVE_SYSTEMCTL}"]')
            if sd.get('status_api_url') != LIVE_STATUS_API:
                raise ProfileError(f'live mode needs systemd.status_api_url exactly {LIVE_STATUS_API}')
            if float(sd.get('command_timeout_s', 180)) <= 150:
                raise ProfileError('systemd.command_timeout_s must be above the daemons\' TimeoutStartSec=150')
        if data['mode'] == 'live' and data['restarter'] == 'simulated' and not data.get('live_switches_only'):
            # Writing live configs without restarting would leave services on the old config.
            raise ProfileError('live mode needs the systemd restarter (a publish item) or live_switches_only')
        if 'token_files' in data and not isinstance(data['token_files'], bool):
            raise ProfileError('token_files must be true or false')
        _check_login_setup(data)
        _check_soup_service(data)
        self.data = data
        self.path = path
        for k in ('tvbridge_root', 'soup_state', 'admin_state'):
            if not os.path.isabs(data[k]):
                raise ProfileError(f'{k} must be an absolute path')

    def __getattr__(self, k):
        try:
            return self.data[k]
        except KeyError:
            raise AttributeError(k)

    @classmethod
    def load(cls, path):
        with open(path) as f:
            return cls(json.load(f), path)

    @property
    def roots(self):
        return (self.tvbridge_root, self.soup_state, self.admin_state)

    def tb(self, *parts):
        return os.path.join(self.tvbridge_root, *parts)

    def soup(self, *parts):
        return os.path.join(self.soup_state, *parts)

    def st(self, *parts):
        return os.path.join(self.admin_state, *parts)

    def soup_unit(self, lane=None):
        """Soup's own systemd unit from the optional profile `soup_service.unit`, or None: by default Soup
        runs inside the Decrypt OS app, so there is no unit of its own. A lane uses the unit's template
        instance, <name>@<lane>.service."""
        unit = (self.data.get('soup_service') or {}).get('unit')
        if not unit or lane is None:
            return unit
        return f'{unit[:-len(".service")]}@{lane}.service'

    def soup_port(self):
        return (self.data.get('soup_service') or {}).get('port')

    def is_soup_unit(self, name):
        unit = self.soup_unit()
        return bool(unit) and (name == unit or name.startswith(unit[:-len('.service')] + '@'))

    def rel(self, path):
        """Short display path: tvbridge/..., soup-state/..., admin/..."""
        for root, label in ((self.tvbridge_root, 'tvbridge'), (self.soup_state, 'soup-state'),
                            (self.admin_state, 'admin')):
            if path == root or path.startswith(root + os.sep):
                return label + path[len(root):]
        return path

    def inside(self, path):
        real = os.path.realpath(path)
        for root in self.roots:
            r = os.path.realpath(root)
            if real == r or real.startswith(r + os.sep):
                return True
        return False


REAL_UNIT_DIR = os.path.join('~', '.config', 'systemd', 'user')
REAL_BROWSER_DIR = os.path.join('~', '.local', 'share', 'decrypt-os', 'tvbridge-browser')


def _under(path, root):
    path, root = os.path.realpath(os.path.expanduser(path)), os.path.realpath(os.path.expanduser(root))
    return path == root or path.startswith(root + os.sep)


def _check_soup_service(data):
    """Optional `soup_service`: {unit: "<name>.service", port?: int} for an install that runs Soup as its own
    systemd unit. Absent = Soup runs inside the Decrypt OS app."""
    if 'soup_service' not in data:
        return
    ss = data['soup_service']
    if not isinstance(ss, dict) or not isinstance(ss.get('unit'), str) or not SOUP_UNIT_RE.match(ss['unit']) \
            or '@' in ss['unit']:
        raise ProfileError('soup_service.unit must be a plain systemd unit name ending in .service')
    port = ss.get('port')
    if port is not None and (not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536):
        raise ProfileError('soup_service.port must be a TCP port number')


def _check_login_setup(data):
    """`login_setup` (Add login): unit_dir, browser_profile_dir, install_script?, python?,
    guard_dir?, signin_wait_s?. Sandbox mode never points at the real systemd unit dir or the real browser
    profiles, and every login_setup needs the systemd restarter (the sandbox pins it to fake_systemctl.py)."""
    ls = data.get('login_setup')
    if ls is None:
        return
    if not isinstance(ls, dict):
        raise ProfileError('login_setup must be an object')
    for k in ('unit_dir', 'browser_profile_dir'):
        if not isinstance(ls.get(k), str) or not os.path.isabs(ls[k]):
            raise ProfileError(f'login_setup.{k} must be an absolute path')
    for k in ('install_script', 'python', 'guard_dir'):
        if k in ls and (not isinstance(ls[k], str) or not os.path.isabs(ls[k])):
            raise ProfileError(f'login_setup.{k} must be an absolute path')
    tdirs = ls.get('template_dirs', [])
    if not isinstance(tdirs, list) or not all(isinstance(d, str) and os.path.isabs(d) for d in tdirs):
        raise ProfileError('login_setup.template_dirs must be a list of absolute paths')
    if 'install_script' in ls and not ls['install_script'].endswith('/scripts/install.py'):
        raise ProfileError('login_setup.install_script must be a release\'s scripts/install.py')
    try:
        wait = float(ls.get('signin_wait_s', 90))
    except (TypeError, ValueError):
        wait = -1
    if not 1 <= wait <= 300:
        raise ProfileError('login_setup.signin_wait_s must be 1 to 300')
    if data['restarter'] != 'systemd':
        raise ProfileError('login_setup needs the systemd restarter (units are enabled and started through it)')
    if data['mode'] == 'sandbox':
        if _under(ls['unit_dir'], REAL_UNIT_DIR):
            raise ProfileError('sandbox mode never writes the real systemd unit dir')
        if _under(ls['browser_profile_dir'], REAL_BROWSER_DIR):
            raise ProfileError('sandbox mode never touches the real bridge browser profiles')
    elif os.path.normpath(ls['unit_dir']) != os.path.join(os.path.expanduser('~' + str(data['user'])), '.config',
                                                           'systemd', 'user'):
        raise ProfileError('live login_setup.unit_dir must be the service user\'s ~/.config/systemd/user')


READ_TOKEN_RE = re.compile(r'^TVBRIDGE_READ_TOKEN_[A-Z][A-Z0-9_]{0,15}\Z')
MANUAL_TOKEN_RE = re.compile(r'^TVBRIDGE_MANUAL_TOKEN_[A-Z][A-Z0-9_]{0,15}\Z')
LOGIN_ID_RE = re.compile(r'^[a-z][a-z0-9-]{0,15}\Z')


def env_file_value(path, name):
    """One variable from a KEY=VALUE env file (systemd EnvironmentFile syntax, simple quotes), or None.
    The value goes to the caller only; it is never logged or stored."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        if k.strip() == name:
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'':
                v = v[1:-1]
            return v
    return None


def token_from_files(profile, name, login=None):
    """Profile `token_files: true`: a token this service did not get from its drop-ins is read, at use time,
    from the same file the drop-in loads. Only two shapes: TVBRIDGE_READ_TOKEN_<ID> from logins/peers.env
    and TVBRIDGE_MANUAL_TOKEN_<ID> from logins/manual-<login>.env. A login added while this service runs
    (Add login) is then usable without restarting this service. Never an order token."""
    if not profile.data.get('token_files') or not isinstance(name, str):
        return None
    if READ_TOKEN_RE.match(name):
        path = profile.tb('logins', 'peers.env')
    elif MANUAL_TOKEN_RE.match(name) and isinstance(login, str) and LOGIN_ID_RE.match(login):
        path = profile.tb('logins', f'manual-{login}.env')
    else:
        return None
    v = env_file_value(path, name)
    return v if v and len(v) >= 24 else None


def _is_real_systemctl(path):
    real = {os.path.realpath(p) for p in ('/usr/bin/systemctl', '/bin/systemctl')
            if os.path.exists(p)}
    found = shutil.which('systemctl')
    if found:
        real.add(os.path.realpath(found))
    return os.path.basename(path) == 'systemctl' and not os.path.isabs(path) or os.path.realpath(path) in real


def sha(data):
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    """sha256 of a file's bytes, "absent" when missing, "dir" for a directory."""
    if os.path.isdir(path):
        return 'dir'
    try:
        with open(path, 'rb') as f:
            return sha(f.read())
    except FileNotFoundError:
        return 'absent'


def read_text(path):
    with open(path) as f:
        return f.read()


def detect_indent(text):
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(' ')
        if stripped and len(line) != len(stripped):
            return len(line) - len(stripped)
    return 2


def load_json(path):
    with open(path) as f:
        text = f.read()
    return json.loads(text), detect_indent(text), text.endswith('\n')


def dump_json(obj, indent=2, newline=True):
    return json.dumps(obj, indent=indent, ensure_ascii=False) + ('\n' if newline else '')


def json_text_like(path, obj):
    """Serialise obj the way the existing file at `path` is formatted (indent, trailing newline)."""
    try:
        _, indent, nl = load_json(path)
    except FileNotFoundError:
        indent, nl = 2, True
    return dump_json(obj, indent, nl)


def admin_note(reason, change_id, at=None):
    return dump_json({'set_by': ADMIN_MARK, 'reason': reason, 'change_id': change_id,
                      'at': round(at or time.time(), 3)})


def _switch_fault(reason):
    return {'on': True, 'set_by': 'system', 'reason': reason, 'at': None, 'path_ok': False}


def read_switch(path):
    """Kill-switch state, read the way the bridge reads it (tvbridge.executor.kill_switch_state): it fails
    closed. Anything at the path (a broken link included) is on; so is a switch that is not configured or
    cannot be checked (`path_ok` False: resuming cannot help). The content only tells us who set it."""
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        return _switch_fault('No pause switch file is configured, so the bridge keeps entries paused.')
    try:
        os.lstat(path)
    except FileNotFoundError:
        folder = os.path.dirname(path)
        if not os.path.isdir(folder) or not os.access(folder, os.R_OK | os.W_OK | os.X_OK):
            return _switch_fault('The pause switch folder is missing or not usable, so the bridge keeps entries '
                                 'paused.')
        return {'on': False, 'set_by': None, 'reason': None, 'at': None, 'path_ok': True}
    except OSError as exc:
        return _switch_fault(f'The pause switch cannot be checked ({type(exc).__name__}), so the bridge keeps '
                             f'entries paused.')
    try:
        with open(path) as f:
            note = json.loads(f.read() or 'null')
    except (OSError, ValueError):
        note = None
    if isinstance(note, dict) and note.get('set_by') == ADMIN_MARK:
        return {'on': True, 'set_by': 'admin', 'reason': note.get('reason'), 'at': note.get('at'), 'path_ok': True}
    try:
        at = os.lstat(path).st_mtime
    except OSError:
        at = None
    reason = 'Set by a safety event or by hand (not by this page)'
    if os.path.islink(path) and not os.path.exists(path):
        reason = 'The pause switch is a broken link; the bridge counts it as paused'
    return {'on': True, 'set_by': 'system', 'reason': reason, 'at': at, 'path_ok': True}


def is_pause_file(path):
    """A kill-switch file: named ENTRIES-DISABLED* (every pause file the bridge and this page use), or a
    note this page wrote. Rollbacks keep these. A resumed switch (renamed *.removed-<UTC>) is not one."""
    if '.removed-' in os.path.basename(path):
        return False
    if os.path.basename(path).startswith('ENTRIES-DISABLED'):
        return True
    try:
        with open(path) as f:
            note = json.loads(f.read(4096) or 'null')
    except (OSError, ValueError):
        return False
    return isinstance(note, dict) and note.get('set_by') == ADMIN_MARK


def utc_stamp(t=None):
    return time.strftime('%Y%m%dT%H%M%S', time.gmtime(t or time.time()))


class FileOps:
    """Planned file operations. Each op: {path, action, content?, to?}.

    action: write (create or modify a file), mkdir, rename (to = new path), delete.
    """

    def __init__(self, profile):
        self.p = profile

    def check(self, ops):
        for op in ops:
            for k in ('path', 'to'):
                if op.get(k) and not self.p.inside(op[k]):
                    raise PermissionError('refusing a path outside the admin roots: ' + op[k])

    def fingerprints(self, ops):
        paths = sorted({op['path'] for op in ops} | {op['to'] for op in ops if op.get('to')})
        return {p: file_sha(p) for p in paths}

    def backup(self, ops, dest):
        """Copy every touched path that exists into dest; return a manifest for restore."""
        os.makedirs(dest, mode=0o700, exist_ok=True)
        manifest = []
        for i, path in enumerate(sorted({op['path'] for op in ops} | {op['to'] for op in ops if op.get('to')})):
            entry = {'path': path, 'sha': file_sha(path), 'copy': None}
            if os.path.isfile(path):
                copy = os.path.join(dest, f'{i:03d}-' + os.path.basename(path))
                shutil.copy2(path, copy)
                entry['copy'] = copy
            manifest.append(entry)
        with open(os.path.join(dest, 'manifest.json'), 'w') as f:
            f.write(dump_json(manifest))
        return manifest

    def restore(self, manifest):
        """Put every path back exactly as backed up (files restored, created files removed), except that a
        pause (kill-switch) file is never removed: a rollback must not unpause an account."""
        for entry in manifest:
            path = entry['path']
            if entry['sha'] == 'dir':
                continue
            if entry['copy']:
                self._atomic_copy(entry['copy'], path)
            elif entry['sha'] == 'absent':
                if is_pause_file(path):
                    continue
                if os.path.isfile(path):
                    os.remove(path)
                elif os.path.isdir(path) and not os.listdir(path):
                    os.rmdir(path)

    def apply(self, ops):
        self.check(ops)
        for op in ops:
            a = op['action']
            if a == 'write':
                self.atomic_write(op['path'], op['content'])
            elif a == 'mkdir':
                os.makedirs(op['path'], mode=0o700, exist_ok=True)
            elif a == 'rename':
                os.rename(op['path'], op['to'])
            elif a == 'delete':
                if os.path.isfile(op['path']):
                    os.remove(op['path'])
            else:
                raise ValueError('unknown file action ' + a)

    def expected(self, ops):
        """sha each path should have after apply, for verify."""
        out = {}
        for op in ops:
            a = op['action']
            if a == 'write':
                out[op['path']] = sha(op['content'])
            elif a == 'mkdir':
                out[op['path']] = 'dir'
            elif a == 'rename':
                out[op['path']] = 'absent'
            elif a == 'delete':
                out[op['path']] = 'absent'
        for op in ops:
            if op['action'] == 'rename':
                out.setdefault(op['to'], None)  # exists after; content unchanged
        return out

    @staticmethod
    def atomic_write(path, content):
        d = os.path.dirname(path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        tmp = os.path.join(d, '.' + os.path.basename(path) + '.admin-tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, content.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)

    def _atomic_copy(self, src, dst):
        with open(src) as f:
            self.atomic_write(dst, f.read())
