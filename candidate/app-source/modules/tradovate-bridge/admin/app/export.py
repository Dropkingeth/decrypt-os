"""Export whole setup: a read-only .tar.gz backup of every config the Bridge setup runs on.

`GET /api/export` → `bridge-setup-<UTC>.tar.gz`, one folder with:
- admin/: the admin registry (webhooks.json), login records, change records and audit log, the profile
- tvbridge/: logins.json, every login's config.json, firing.json, mirror.json and the env files with every
  value replaced by <redacted> (names kept)
- soup/: quantity-policy.json and the settings contract the profile's strategies reference
- units/: systemd user units and drop-ins named tvbridge-*, plus the profile's optional soup_service unit
- tunnel/: the tunnel config.yml (tunnel id and credentials path redacted)
- MANIFEST.json: sha256 and size of every exported file (as exported), the bridge release in use, versions,
  the time, and what was left out.

No secret value leaves this service:
- env files: every `NAME=value` becomes `NAME=<redacted>`; values are never read into the archive;
- unit files: `Environment=NAME=value` is redacted when NAME looks secret (TOKEN, SECRET, PASSWORD, KEY, AUTH,
  CREDENTIAL, BEARER) or the value looks like a token (24+ characters of [A-Za-z0-9_+=-], not a path);
- JSON: any string under a key that looks secret (except `*_env` names) and every `stream_id` (the webhook's
  alert credential) becomes "<redacted>"; Tradovate `account_name`/`account_id` values are masked (…1234);
- free text (summaries, notes): every stream ID and every configured account name and id, as a whole word;
- skipped on purpose: admin backups (copies of configs), Soup pairing.json (holds the stream ID), browser
  profiles, journals, databases, cloudflared credentials.
Nothing is written except one audit entry (by the engine).
"""
import glob
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import time

REDACTED = '<redacted>'
SECRET_KEY_RE = re.compile(r'(token|secret|password|passwd|api[_-]?key|bearer|credential|auth|cookie|session|'
                           r'private)', re.I)
ACCOUNT_KEYS = ('account_name', 'account_id')
MIN_SCRUB_LEN = 4   # shorter values are not scrubbed from free text (they would hit unrelated words)
TOKENISH_RE = re.compile(r'^[A-Za-z0-9_+=\-]{24,}\Z')
ENV_LINE_RE = re.compile(rb'^(\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=)(.*)$')
UNIT_ENV_RE = re.compile(r'^(\s*Environment\s*=\s*)(.*)\Z')
UNIT_PATTERNS = ('tvbridge-*',)
ADMIN_BUILD = 'tvbridge-admin v1 (add login + W2 off + export)'


def redact_env(data):
    """KEY=value lines → KEY=<redacted>; comments and blank lines kept. Works on bytes; no value is kept."""
    out = []
    for line in data.splitlines(keepends=True):
        body = line.rstrip(b'\r\n')
        end = line[len(body):]
        m = ENV_LINE_RE.match(body)
        if m and not body.lstrip().startswith(b'#'):
            out.append(m.group(1) + REDACTED.encode() + end)
        else:
            out.append(line)
    return b''.join(out)


def _secret_name(name):
    return bool(SECRET_KEY_RE.search(name)) and not name.lower().endswith('_env')


def redact_unit(text):
    out = []
    for line in text.splitlines(keepends=True):
        m = UNIT_ENV_RE.match(line.rstrip('\n'))
        if not m:
            out.append(line)
            continue
        parts = []
        for item in re.findall(r'"[^"]*"|\S+', m.group(2)):
            quoted = item.startswith('"') and item.endswith('"')
            raw = item[1:-1] if quoted else item
            if '=' in raw:
                name, val = raw.split('=', 1)
                if _secret_name(name) or (TOKENISH_RE.match(val) and not val.startswith('/')):
                    raw = f'{name}={REDACTED}'
            parts.append(f'"{raw}"' if quoted else raw)
        out.append(m.group(1) + ' '.join(parts) + ('\n' if line.endswith('\n') else ''))
    return ''.join(out)


def mask_account(value):
    """A Tradovate account name or id, masked to its last 4 characters ('…1234')."""
    return '…' + str(value)[-4:]


def redact_json(obj, key=None):
    if isinstance(obj, dict):
        return {k: redact_json(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_json(v, key) for v in obj]
    if isinstance(obj, str) and key and (key == 'stream_id' or _secret_name(key)):
        return REDACTED
    if key in ACCOUNT_KEYS and isinstance(obj, (str, int)) and not isinstance(obj, bool):
        return mask_account(obj)
    return obj


def scrub_words(data, replacements):
    """Replace each value (bytes) where it stands as a whole word ([A-Za-z0-9_-] boundaries)."""
    for value, new in replacements:
        data = re.sub(rb'(?<![A-Za-z0-9_-])' + re.escape(value) + rb'(?![A-Za-z0-9_-])', new, data)
    return data


def redact_tunnel(text):
    out = []
    for line in text.splitlines(keepends=True):
        m = re.match(r'^(\s*(?:tunnel|credentials-file)\s*:\s*)(.*?)(\s*)$', line)
        out.append(m.group(1) + REDACTED + '\n' if m else line)
    return ''.join(out)


class Exporter:
    def __init__(self, engine):
        self.e = engine
        self.p = engine.p

    def _unit_dir(self):
        ls = self.p.data.get('login_setup') or {}
        if ls.get('unit_dir'):
            return ls['unit_dir']
        if self.p.mode == 'live':
            return os.path.expanduser('~/.config/systemd/user')
        return None      # a sandbox without login_setup never reads the real unit folder

    def _release(self):
        cur = self.p.tb('current')
        if not os.path.islink(cur):
            return None
        target = os.path.realpath(cur)
        out = {'current': os.readlink(cur), 'name': os.path.basename(target), 'version': None}
        try:
            with open(os.path.join(target, 'MANIFEST.json')) as f:
                out['version'] = json.load(f).get('version')
        except (OSError, ValueError):
            pass
        return out

    def collect(self):
        """[(archive path, source path or None, kind)] in a stable order. kind: json | env | unit | tunnel |
        text | generated."""
        p, items, seen = self.p, [], set()

        def add(arc, src, kind):
            if src and (src in seen or not os.path.isfile(src)):
                return
            seen.add(src)
            items.append((arc, src, kind))
        st = self.e.state()
        add('admin/webhooks.json', p.st('webhooks.json'), 'json')
        for path in sorted(glob.glob(p.st('logins', '*.json'))):
            add('admin/logins/' + os.path.basename(path), path, 'json')
        for path in sorted(glob.glob(p.st('changes', '*.json'))):
            add('admin/changes/' + os.path.basename(path), path, 'json')
        add('admin/audit.jsonl', p.st('audit.jsonl'), 'jsonl')
        add('admin/profile.json', None, 'generated')
        add('tvbridge/logins.json', st.logins_path, 'json')
        for lg in st.logins:
            cfg = lg['config']
            rel = os.path.relpath(cfg, p.tvbridge_root) if cfg.startswith(p.tvbridge_root + os.sep) else \
                f'outside/{lg["id"]}-config.json'
            add('tvbridge/' + rel, cfg, 'json')
        add('tvbridge/firing.json', st.firing_path, 'json')
        add('tvbridge/mirror.json', p.tb('mirror.json'), 'json')
        envs = sorted(set(glob.glob(p.tb('*.env')) + glob.glob(p.tb('logins', '*.env')) +
                          glob.glob(p.tb('logins', '*', '*.env'))))
        for path in envs:
            if '.removed-' in path or '.failed-' in path:
                continue
            add('tvbridge/' + os.path.relpath(path, p.tvbridge_root), path, 'env')
        add('soup/quantity-policy.json', p.soup('quantity-policy.json'), 'json')
        for s in p.strategies:
            ref = s.get('contract_ref')
            if ref:
                add(f'soup/contracts/{s["id"]}/settings_contract.json',
                    os.path.join(os.path.expanduser('~/.local/share/decrypt-os'), ref), 'json')
        units = self._unit_dir()
        if units and os.path.isdir(units):
            soup = p.soup_unit()
            soup_pats = (soup + '*', soup[:-len('.service')] + '@*') if soup else ()
            for pat in UNIT_PATTERNS + soup_pats:
                for path in sorted(glob.glob(os.path.join(units, pat))):
                    if os.path.isfile(path) and not os.path.islink(path):
                        add('units/' + os.path.basename(path), path, 'unit')
                    elif os.path.isdir(path) and path.endswith('.d'):
                        for f in sorted(glob.glob(os.path.join(path, '*'))):
                            if os.path.isfile(f):
                                add(f'units/{os.path.basename(path)}/{os.path.basename(f)}', f, 'unit')
        tunnel = p.data.get('tunnel_config')
        if tunnel:
            add('tunnel/config.yml', tunnel, 'tunnel')
        return items

    def render(self, src, kind):
        if kind == 'generated':
            return (json.dumps(redact_json(self.p.data), indent=2, ensure_ascii=False) + '\n').encode()
        with open(src, 'rb') as f:
            data = f.read()
        if kind == 'env':
            return redact_env(data)
        text = data.decode('utf-8', 'replace')
        if kind == 'json':
            try:
                return (json.dumps(redact_json(json.loads(text)), indent=2, ensure_ascii=False) + '\n').encode()
            except ValueError:
                return b'"<unreadable JSON, left out>"\n'
        if kind == 'jsonl':
            rows = []
            for line in text.splitlines():
                try:
                    rows.append(json.dumps(redact_json(json.loads(line)), ensure_ascii=False))
                except ValueError:
                    rows.append('"<unreadable line, left out>"')
            return ('\n'.join(rows) + '\n').encode()
        if kind == 'unit':
            return redact_unit(text).encode()
        if kind == 'tunnel':
            return redact_tunnel(text).encode()
        return data

    def build(self, clock=time.time):
        now = clock()
        stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))
        top = f'bridge-setup-{stamp}'
        files, blobs = [], []
        # every stream ID (a webhook's alert credential) and every configured Tradovate account name and id
        # are also scrubbed from free text such as change summaries and audit notes
        st = self.e.state()
        words = {w.get('stream_id'): REDACTED for w in st.webhooks if isinstance(w.get('stream_id'), str)}
        for a in st.accounts.values():
            for k in ACCOUNT_KEYS:
                if isinstance(a.get(k), (str, int)) and not isinstance(a.get(k), bool):
                    words.setdefault(str(a[k]), mask_account(a[k]))
        scrub = sorted(((k.encode(), v.encode()) for k, v in words.items() if k and len(k) >= MIN_SCRUB_LEN),
                       key=lambda kv: len(kv[0]), reverse=True)
        for arc, src, kind in self.collect():
            try:
                data = scrub_words(self.render(src, kind), scrub)
            except OSError as exc:
                files.append({'path': arc, 'source': self.p.rel(src) if src else None, 'error': type(exc).__name__})
                continue
            blobs.append((arc, data))
            files.append({'path': arc, 'source': (self.p.rel(src) if src else 'profile (in memory)'),
                          'kind': kind, 'redacted': kind in ('env', 'unit', 'json', 'jsonl', 'tunnel', 'generated'),
                          'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        manifest = {
            'schema': 'decrypt.tvbridge-admin.export.v1', 'created_at': now,
            'created_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)), 'mode': self.p.mode,
            'bridge_release': self._release(),
            'versions': {'admin': ADMIN_BUILD, 'python': sys.version.split()[0],
                         'strategies': [{k: s.get(k) for k in ('id', 'name', 'build', 'contract_ref')}
                                        for s in self.p.strategies]},
            'redaction': 'env values, secret-named JSON keys, every stream_id, secret or token-like unit '
                         'Environment= values and the tunnel id/credentials path are "<redacted>"; Tradovate '
                         'account names and ids are masked to their last 4 characters',
            'left_out': ['admin backups', 'Soup pairing.json (holds the stream ID)', 'browser profiles', 'journals',
                         'databases', 'cloudflared credentials'],
            'files': files}
        blobs.append(('MANIFEST.json', (json.dumps(manifest, indent=2, ensure_ascii=False) + '\n').encode()))
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as tar:
            for arc, data in blobs:
                info = tarfile.TarInfo(f'{top}/{arc}')
                info.size, info.mtime, info.mode = len(data), int(now), 0o600
                tar.addfile(info, io.BytesIO(data))
        return buf.getvalue(), f'{top}.tar.gz', manifest
