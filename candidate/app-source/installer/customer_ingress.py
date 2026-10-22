#!/usr/bin/python3
"""Optional dedicated customer HTTPS ingress. No DNS or provider configuration.

Installed administrator CLI only; never a browser/API privilege bridge. Prepare
and inspect do not start a service. Apply is one-shot and leaves partial failures
for inspection, not automatic retry. No request targets or credentials are logged.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import time

INSTALL = '/usr/local/lib/decrypt-os'
ENTRY = INSTALL + '/installer/customer_ingress.py'
SETUP = '/var/lib/decrypt-os-ingress-setup'
CONFIG = '/etc/decrypt-os/ingress/caddy.json'
UNIT = '/etc/systemd/system/decrypt-os-ingress.service'
UNIT_NAME = 'decrypt-os-ingress.service'
RETAINED_STATE = ('/var/lib/decrypt-os-ingress', '/var/lib/private/decrypt-os-ingress')
INSTANCE_POLICY = '/etc/decrypt-os/instance-policy.json'
HEX = re.compile(r'[a-f0-9]{64}\Z')
MAX_JSON = 2 * 1024**2


class Refused(ValueError):
    pass


def need(ok, code):
    if not ok:
        raise Refused(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def strict(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, 'duplicate_json_field')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def origin(value):
    need(type(value) is str and len(value) <= 261 and value.startswith('https://'), 'exact_https_origin_required')
    host = value[8:]
    need(host == host.lower() and 3 <= len(host) <= 253 and '.' in host, 'public_fqdn_required')
    labels = host.split('.')
    need(all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)
         and re.fullmatch(r'[a-z]{2,63}', labels[-1]), 'public_fqdn_required')
    need(labels[-1] not in ('localhost', 'local', 'internal', 'invalid', 'test', 'example', 'onion')
         and not host.endswith('.home.arpa'), 'public_fqdn_required')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise Refused('public_fqdn_required')
    return value, host


def caddy_config(public_origin):
    _, host = origin(public_origin)
    return canonical({
        'admin': {'disabled': True},
        'logging': {'logs': {'default': {'writer': {'output': 'discard'}}}},
        'apps': {
            'http': {'servers': {'signals': {
                'listen': [':443'],
                'protocols': ['h1', 'h2'],
                'automatic_https': {'disable_redirects': True},
                'strict_sni_host': True,
                'routes': [
                    {'match': [{'host': [host], 'method': ['POST'], 'path': ['/signal/*']}],
                     'handle': [{'handler': 'subroute', 'routes': [
                         {'handle': [{'handler': 'request_body', 'max_size': 40960}]},
                         {'handle': [{'handler': 'reverse_proxy', 'upstreams': [{'dial': '127.0.0.1:8812'}],
                                      'headers': {'request': {'set': {'Host': [host], 'X-Forwarded-Host': [host],
                                                                    'X-Forwarded-Proto': ['https']}}}}]}]}],
                     'terminal': True},
                    {'handle': [{'handler': 'static_response', 'status_code': 404, 'body': 'Not found'}],
                     'terminal': True},
                ],
            }}},
            'tls': {'automation': {'policies': [{'subjects': [host], 'issuers': [
                {'module': 'acme', 'challenges': {'http': {'disabled': True}}}
            ]}]}},
        },
    })


def service_unit():
    return f'''[Unit]
Description=Decrypt customer HTTPS signal ingress
After=network-online.target decrypt-os-loopback-guard.service
Wants=network-online.target
# Never serve without the per-UID loopback firewall (installer/loopback_guard.py).
Requires=decrypt-os-loopback-guard.service
ConditionPathExists={CONFIG}

[Service]
Type=notify
User=decrypt-os-ingress
DynamicUser=yes
StateDirectory=decrypt-os-ingress
StateDirectoryMode=0700
Environment=HOME=/var/lib/decrypt-os-ingress
Environment=XDG_DATA_HOME=/var/lib/decrypt-os-ingress/data
Environment=XDG_CONFIG_HOME=/var/lib/decrypt-os-ingress/config
ExecStartPre=+/usr/bin/python3 -I -B {ENTRY} verify-start
ExecStartPre=/usr/bin/caddy validate --config {CONFIG}
ExecStart=/usr/bin/caddy run --config {CONFIG}
Restart=on-failure
RestartSec=10
TimeoutStartSec=60
TimeoutStopSec=15
UMask=0077
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
AmbientCapabilities=CAP_NET_BIND_SERVICE
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
ProtectKernelLogs=yes
ProtectClock=yes
ProtectProc=invisible
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
StandardOutput=null
StandardError=null

[Install]
WantedBy=multi-user.target
'''.encode()


# Executed as the enrolled desktop UID, not as root. Only the public-origin row
# can leave this subprocess; no tokens, browser profile or arbitrary SQL input.
READ_ORIGIN = r'''
import json,os,pathlib,resource,sqlite3,stat,sys
resource.setrlimit(resource.RLIMIT_CPU,(3,3))
if sys.platform=='linux':resource.setrlimit(resource.RLIMIT_AS,(256*1024**2,256*1024**2))
p=pathlib.Path(sys.argv[1]);uid=os.getuid()
for item in (p,*p.parents):
 s=item.lstat()
 if stat.S_ISLNK(s.st_mode) or s.st_mode&0o022 or s.st_uid not in (0,uid):raise ValueError()
s=p.lstat()
if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1 or s.st_uid!=uid or s.st_size>64*1024**2:raise ValueError()
db=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True,timeout=2)
db.execute('PRAGMA query_only=ON');db.execute('PRAGMA trusted_schema=OFF')
if db.execute("SELECT type FROM sqlite_schema WHERE name='configuration'").fetchall()!=[('table',)]:raise ValueError()
def auth(op,a,b,c,d):
 return sqlite3.SQLITE_OK if op==sqlite3.SQLITE_SELECT or op==sqlite3.SQLITE_READ and a=='configuration' else sqlite3.SQLITE_DENY
db.set_authorizer(auth)
rows=db.execute("SELECT value FROM configuration WHERE name=? LIMIT 2",('ingress',)).fetchall();db.close()
if len(rows)!=1 or type(rows[0][0]) is not str or len(rows[0][0])>1024:raise ValueError()
v=json.loads(rows[0][0])
if type(v)is not dict or set(v)!={'publicOrigin'} or type(v['publicOrigin'])is not str or len(v['publicOrigin'])>261:raise ValueError()
print(json.dumps({'publicOrigin':v['publicOrigin']}))
'''


class Host:
    """Fixed production paths; constructor seams are for isolated fixture tests."""
    def __init__(self, root=Path('/'), owner_uid=0):
        self.root = Path(root)
        self.owner_uid = owner_uid

    def path(self, absolute):
        need(absolute.startswith('/') and '..' not in Path(absolute).parts, 'fixed_path_required')
        return self.root / absolute.lstrip('/')

    def trusted(self, path, *, directory=False, maximum=MAX_JSON):
        path = Path(path)
        need(path.is_relative_to(self.root), 'outside_installation')
        for item in (path, *path.parents):
            info = item.lstat()
            need(info.st_uid == self.owner_uid and not info.st_mode & 0o022
                 and not stat.S_ISLNK(info.st_mode), 'root_owned_unlinked_path_required')
            if item == path:
                need(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
                     and info.st_nlink == 1 and info.st_size <= maximum, 'bounded_regular_file_required')
            else:
                need(stat.S_ISDIR(info.st_mode), 'directory_required')
            if item == self.root:
                break
        return info if directory else path.read_bytes()

    def mkdir(self, path, mode=0o700):
        path = Path(path)
        if path.exists() or path.is_symlink():
            self.trusted(path, directory=True)
            need(stat.S_IMODE(path.stat().st_mode) == mode, 'directory_mode_differs')
            return
        self.trusted(path.parent, directory=True)
        path.mkdir(mode=mode)
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)

    def write(self, path, raw, mode=0o600):
        self.trusted(path.parent, directory=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def run(self, arguments, *, user=None, group=None, env=None):
        self.binary(arguments[0])
        # CLI production root is always /. Never invoke this on fixture paths.
        need(self.root == Path('/'), 'native_execution_not_available_in_fixture')
        options = {'user': user, 'group': group, 'extra_groups': []} if user is not None else {}
        result = subprocess.run(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=30, cwd='/',
                                env=env or {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, **options)
        need(len(result.stdout) <= 8192, 'command_output_invalid')
        return result.returncode, result.stdout

    def binary(self, executable):
        need(executable in ('/usr/bin/caddy', '/usr/bin/python3', '/usr/bin/systemctl'), 'fixed_executable_required')
        path = self.path(executable)
        if executable == '/usr/bin/python3' and path.is_symlink():
            # Arch's versioned Python alias is normal. No arbitrary alias or
            # linked parent is allowed, and Caddy/systemctl remain exact files.
            self.trusted(path.parent, directory=True)
            info = path.lstat()
            target = os.readlink(path)
            need(info.st_uid == self.owner_uid and re.fullmatch(r'python3\.\d+', target), 'trusted_python_alias_required')
            path = path.parent / target
        raw = self.trusted(path, maximum=256 * 1024**2)
        need(path.stat().st_mode & 0o111, 'executable_required')
        return raw

    def account(self, name):
        return pwd.getpwnam(name)

    def configured_origin(self, enrollment):
        account = self.account(enrollment['desktopUser'])
        need(account.pw_uid == enrollment['desktopUid'] and account.pw_uid >= 1000
             and account.pw_dir == '/home/' + account.pw_name, 'enrolled_desktop_account_differs')
        db = account.pw_dir + '/.local/state/decrypt-os/trading/service.sqlite'
        code, raw = self.run(['/usr/bin/python3', '-I', '-B', '-c', READ_ORIGIN, db],
                             user=account.pw_uid, group=account.pw_gid)
        need(code == 0, 'configure_public_origin_in_application_first')
        value = strict(raw)
        need(type(value) is dict and set(value) == {'publicOrigin'}, 'configured_origin_invalid')
        return origin(value['publicOrigin'])[0]

    def identity(self):
        raw = self.trusted(self.path('/etc/machine-id'), maximum=64)
        machine = raw.decode().strip()
        need(re.fullmatch(r'[a-f0-9]{32}', machine), 'machine_identity_invalid')
        machine_sha = sha(machine.encode())
        policy=strict(self.trusted(self.path(INSTANCE_POLICY),maximum=16384))
        firstboot=strict(self.trusted(self.path('/var/lib/decrypt-firstboot/state.json'),maximum=16384))
        enrollment_raw = self.trusted(self.path('/etc/decrypt-os/customer-test.json'))
        enrollment = strict(enrollment_raw)
        need(enrollment.get('schema') == 'decrypt.customer-test.v1'
             and enrollment.get('role') == 'clean-customer-test'
             and enrollment.get('machineSha256') == machine_sha
             and re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', enrollment.get('desktopUser', ''))
             and type(enrollment.get('desktopUid')) is int, 'customer_installation_required')
        receipt_raw = self.trusted(self.path(INSTALL + '/INSTALL-RECEIPT.json'))
        receipt = strict(receipt_raw)
        need(receipt.get('schema') == 'decrypt.firstboot-install-receipt.v1' and receipt.get('installed') is True
             and receipt.get('stageOnly') is False and receipt.get('targetMachineSha256') == machine_sha
             and receipt.get('desktopUser') == enrollment['desktopUser'], 'installed_receipt_invalid')
        manifest_raw = self.trusted(self.path(INSTALL + '/SOURCE-MANIFEST.json'))
        manifest_sha = sha(manifest_raw)
        need(receipt.get('sourceManifestSha256') == enrollment.get('sourceManifestSha256') == manifest_sha,
             'source_receipt_differs')
        manifest = strict(manifest_raw)
        rows = [row for row in manifest.get('files', []) if row.get('path') == 'installer/customer_ingress.py']
        helper = self.trusted(self.path(ENTRY))
        need(len(rows) == 1 and rows[0].get('sha256') == sha(helper)
             and rows[0].get('bytes') == len(helper), 'installed_helper_changed')
        control=self.trusted(self.path(INSTALL+'/installer/customer_control.py'))
        control_rows=[row for row in manifest.get('files',[]) if row.get('path')=='installer/customer_control.py']
        need(len(control_rows)==1 and control_rows[0].get('sha256')==sha(control) and control_rows[0].get('bytes')==len(control),'instance_validator_changed')
        namespace={'__name__':'verified_instance_control'}
        exec(compile(control,'verified_instance_control','exec'),namespace)
        try:namespace['validate_instance'](policy,firstboot,enrollment,receipt,machine)
        except (ValueError,KeyError,TypeError):raise Refused('instance_authorization_differs')
        need(policy['sourceManifestSha256']==manifest_sha,'instance_source_differs')
        caddy = self.trusted(self.path('/usr/bin/caddy'), maximum=256 * 1024**2)
        need(self.path('/usr/bin/caddy').stat().st_mode & 0o111, 'caddy_not_executable')
        code, version_raw = self.run(['/usr/bin/caddy', 'version'])
        version = version_raw.decode().strip()
        need(code == 0 and len(version) <= 256 and re.fullmatch(r'v2\.\d+\.\d+(?:[^\r\n]*)', version),
             'caddy_v2_required')
        return {'machineSha256': machine_sha, 'enrollmentSha256': sha(enrollment_raw),
                'installReceiptSha256': sha(receipt_raw), 'sourceManifestSha256': manifest_sha,
                'helperSha256': sha(helper), 'caddySha256': sha(caddy), 'caddyVersion': version,
                'desktopUser': enrollment['desktopUser'], 'desktopUid': enrollment['desktopUid'],
                'publicOrigin': self.configured_origin(enrollment)}

    def listeners(self):
        result = []
        for name, loopback in (('tcp', '0100007F'), ('tcp6', '00000000000000000000000001000000')):
            path = self.path('/proc/net/' + name)
            if name == 'tcp6' and not path.exists():
                continue
            raw = path.read_text()
            need(len(raw) <= 4 * 1024**2, 'socket_inventory_too_large')
            for line in raw.splitlines()[1:]:
                row = line.split()
                need(len(row) >= 10, 'socket_inventory_invalid')
                if row[3] != '0A':
                    continue
                address, port = row[1].split(':')
                result.append({'port': int(port, 16), 'uid': int(row[7]),
                               'ipv4Loopback': name == 'tcp' and address == loopback})
        return result

    def receiver(self, identity, *, require_free_public=False):
        sockets = self.listeners()
        receivers = [row for row in sockets if row['port'] == 8812]
        need(len(receivers) == 1 and receivers[0]['ipv4Loopback']
             and receivers[0]['uid'] == identity['desktopUid'], 'customer_loopback_receiver_required')
        if require_free_public:
            need(not any(row['port'] == 443 for row in sockets), 'public_port_in_use')

    def validate(self, config, validation_home):
        self.trusted(config)
        self.trusted(validation_home, directory=True)
        code, _ = self.run(['/usr/bin/caddy', 'validate', '--config', str(config)], env={
            'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': str(validation_home),
            'XDG_DATA_HOME': str(validation_home / 'data'), 'XDG_CONFIG_HOME': str(validation_home / 'config')})
        need(code == 0, 'caddy_configuration_invalid')

    def systemctl(self, *arguments):
        code, raw = self.run(['/usr/bin/systemctl', *arguments])
        return code, raw.decode().strip()

    def unit_properties(self, name):
        code, raw = self.systemctl('show', name, '--property=LoadState,ActiveState,UnitFileState,FragmentPath,DropInPaths', '--no-pager')
        need(code == 0, 'service_inspection_failed')
        properties = {}
        for line in raw.splitlines():
            key, separator, value = line.partition('=')
            need(separator and key not in properties, 'service_properties_invalid')
            properties[key] = value
        need(set(properties) == {'LoadState', 'ActiveState', 'UnitFileState', 'FragmentPath', 'DropInPaths'},
             'service_properties_invalid')
        return properties


@contextlib.contextmanager
def locked(host):
    folder = host.path(SETUP)
    host.mkdir(folder)
    path = folder / 'operation.lock'
    if path.exists() or path.is_symlink():
        host.trusted(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        need(info.st_uid == host.owner_uid and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
             and not info.st_mode & 0o077, 'private_lock_required')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def planned_files(public_origin):
    return {CONFIG: caddy_config(public_origin), UNIT: service_unit()}


def prepare(host, public_origin):
    public_origin, _ = origin(public_origin)
    identity = host.identity()
    need(identity['publicOrigin'] == public_origin, 'configured_origin_mismatch')
    host.receiver(identity, require_free_public=True)
    with locked(host):
        need(not os.path.lexists(host.path(SETUP + '/ATTEMPT.json'))
             and not os.path.lexists(host.path(CONFIG)) and not os.path.lexists(host.path(UNIT)), 'existing_ingress_preserved')
        need(not any(os.path.lexists(host.path(path)) for path in RETAINED_STATE), 'existing_certificate_state_preserved')
        files = planned_files(public_origin)
        plan = {'schema': 'decrypt.customer-ingress-plan.v1', 'identity': identity,
                'createdAt': int(time.time()), 'preparationId': os.urandom(16).hex(),
                'publicOrigin': public_origin,
                'files': {name: {'bytes': len(raw), 'sha256': sha(raw)} for name, raw in files.items()},
                'publicTlsVerified': False, 'publicReachabilityVerified': False, 'nativeRebootVerified': False,
                'serviceStarted': False, 'credentialValuesIncluded': False}
        raw = canonical(plan)
        plan_sha = sha(raw)
        plans = host.path(SETUP + '/plans')
        host.mkdir(plans)
        folder = plans / plan_sha
        host.mkdir(folder)
        host.write(folder / 'PLAN.json', raw)
        host.write(folder / 'caddy.json', files[CONFIG])
        host.write(folder / 'decrypt-os-ingress.service', files[UNIT])
        validation = folder / 'validation'
        host.mkdir(validation)
        host.validate(folder / 'caddy.json', validation)
        # A changed binary/configuration/host during validation is not accepted.
        need(host.identity() == identity, 'identity_changed_during_prepare')
        host.receiver(identity, require_free_public=True)
        host.write(folder / 'VALIDATED.json', canonical({'schema': 'decrypt.ingress-config-validation.v1',
                   'planSha256': plan_sha, 'caddySha256': identity['caddySha256'], 'configurationAccepted': True,
                   'publicTlsVerified': False, 'publicReachabilityVerified': False}))
        return {'state': 'prepared', 'planSha256': plan_sha, 'planPath': str(folder / 'PLAN.json'),
                'publicOrigin': public_origin, 'caddyConfigurationValidated': True,
                'publicTlsVerified': False, 'publicReachabilityVerified': False, 'nativeRebootVerified': False}


def read_plan(host, plan_sha, *, fresh=False):
    need(type(plan_sha) is str and HEX.fullmatch(plan_sha), 'reviewed_plan_hash_required')
    folder = host.path(SETUP + '/plans/' + plan_sha)
    raw = host.trusted(folder / 'PLAN.json')
    need(sha(raw) == plan_sha, 'reviewed_plan_changed')
    plan = strict(raw)
    need(plan.get('schema') == 'decrypt.customer-ingress-plan.v1', 'plan_schema_invalid')
    need(type(plan.get('preparationId')) is str and re.fullmatch('[a-f0-9]{32}', plan['preparationId']),
         'plan_preparation_identity_required')
    public_origin, _ = origin(plan['publicOrigin'])
    need(plan.get('identity', {}).get('publicOrigin') == public_origin, 'plan_origin_invalid')
    need(type(plan.get('createdAt')) is int and (not fresh or 0 <= time.time() - plan['createdAt'] <= 86400),
         'prepare_a_fresh_plan')
    files = planned_files(public_origin)
    need(plan.get('files') == {name: {'bytes': len(raw), 'sha256': sha(raw)} for name, raw in files.items()},
         'plan_recipe_changed')
    need(host.trusted(folder / 'caddy.json') == files[CONFIG]
         and host.trusted(folder / 'decrypt-os-ingress.service') == files[UNIT], 'prepared_files_changed')
    validation = strict(host.trusted(folder / 'VALIDATED.json'))
    need(validation.get('schema') == 'decrypt.ingress-config-validation.v1'
         and validation.get('planSha256') == plan_sha and validation.get('configurationAccepted') is True
         and validation.get('caddySha256') == plan['identity']['caddySha256'], 'validated_plan_required')
    return plan, folder


def apply(host, plan_sha, *, controls_domain=False, public_service=False):
    need(controls_domain is True and public_service is True, 'explicit_domain_and_public_service_acknowledgments_required')
    with locked(host):
        plan, folder = read_plan(host, plan_sha, fresh=True)
        need(host.identity() == plan['identity'], 'reviewed_installation_changed')
        host.receiver(plan['identity'], require_free_public=True)
        for path in (CONFIG, UNIT, SETUP + '/ATTEMPT.json', SETUP + '/INSTALL-RECEIPT.json', *RETAINED_STATE):
            need(not os.path.lexists(host.path(path)), 'existing_or_partial_ingress_preserved')
        # Do not merge into or replace the distribution's Caddy service.
        for name in ('caddy.service', UNIT_NAME):
            state = host.unit_properties(name)
            need(state['ActiveState'] in ('inactive', 'failed') and state['UnitFileState'] in ('', 'disabled', 'static'),
                 'existing_proxy_service_preserved')
            if name == UNIT_NAME:
                need(state['LoadState'] == 'not-found' and not state['FragmentPath'] and not state['DropInPaths'],
                     'existing_proxy_service_preserved')
        for directory in ('/etc/systemd/system', '/run/systemd/system', '/usr/lib/systemd/system'):
            need(not os.path.lexists(host.path(directory + '/' + UNIT_NAME + '.d')), 'existing_service_overrides_preserved')
        host.validate(folder / 'caddy.json', folder / 'validation')
        need(host.identity() == plan['identity'], 'reviewed_installation_changed')
        host.receiver(plan['identity'], require_free_public=True)
        attempt = {'schema': 'decrypt.customer-ingress-attempt.v1', 'planSha256': plan_sha,
                   'at': int(time.time()), 'noAutomaticRetry': True}
        host.write(host.path(SETUP + '/ATTEMPT.json'), canonical(attempt))
        # From this point a failure is retained and blocks further apply attempts.
        host.mkdir(host.path('/etc/decrypt-os/ingress'), mode=0o755)
        for path, raw in planned_files(plan['publicOrigin']).items():
            host.write(host.path(path), raw, mode=0o644)
        receipt = {'schema': 'decrypt.customer-ingress-install.v1', 'planSha256': plan_sha,
                   'phase': 'installed_before_service_start', 'files': plan['files'],
                   'publicOrigin': plan['publicOrigin'], 'identity': plan['identity'],
                   'publicTlsVerified': False, 'publicReachabilityVerified': False, 'nativeRebootVerified': False}
        host.write(host.path(SETUP + '/INSTALL-RECEIPT.json'), canonical(receipt))
        for arguments in (('daemon-reload',), ('enable', '--now', UNIT_NAME)):
            code, _ = host.systemctl(*arguments)
            need(code == 0, 'service_action_failed_partial_state_preserved')
        result = inspect(host)
        need(result.get('serviceActive') is True and result.get('serviceEnabled') is True, 'service_not_ready_partial_state_preserved')
        host.write(host.path(SETUP + '/SERVICE-STARTED.json'), canonical({
            'schema': 'decrypt.customer-ingress-start.v1', 'planSha256': plan_sha, 'at': int(time.time()),
            'serviceActive': True, 'serviceEnabled': True, 'publicTlsVerified': False,
            'publicReachabilityVerified': False, 'nativeRebootVerified': False}))
        return inspect(host)


def installed(host, *, require_receiver=False):
    receipt = strict(host.trusted(host.path(SETUP + '/INSTALL-RECEIPT.json')))
    need(receipt.get('schema') == 'decrypt.customer-ingress-install.v1', 'installed_ingress_receipt_required')
    plan, _ = read_plan(host, receipt.get('planSha256'))
    need(receipt.get('identity') == plan['identity'] and receipt.get('files') == plan['files']
         and receipt.get('publicOrigin') == plan['publicOrigin'], 'installed_receipt_changed')
    need(host.identity() == plan['identity'], 'installed_origin_or_dependency_changed')
    for path, expected in planned_files(plan['publicOrigin']).items():
        need(host.trusted(host.path(path)) == expected, 'installed_proxy_files_changed')
    state = host.unit_properties(UNIT_NAME)
    need(state['LoadState'] == 'loaded' and state['FragmentPath'] == UNIT and not state['DropInPaths'],
         'installed_service_override_refused')
    if require_receiver:
        host.receiver(plan['identity'])
    return plan


def inspect(host, plan_sha=None):
    if plan_sha is not None:
        plan, _ = read_plan(host, plan_sha)
        need(host.identity() == plan['identity'], 'reviewed_installation_changed')
        return {'state': 'prepared', 'planSha256': plan_sha, 'publicOrigin': plan['publicOrigin'],
                'identity': plan['identity'], 'createdAt': plan['createdAt'],
                'expiresAt': plan['createdAt'] + 86400,
                'files': plan['files'], 'publicTlsVerified': False, 'publicReachabilityVerified': False,
                'nativeRebootVerified': False}
    if not os.path.lexists(host.path(SETUP + '/INSTALL-RECEIPT.json')):
        partial = any(os.path.lexists(host.path(path)) for path in (SETUP + '/ATTEMPT.json', CONFIG, UNIT, *RETAINED_STATE))
        return {'state': 'partial_requires_review' if partial else 'not_installed',
                'publicTlsVerified': False, 'publicReachabilityVerified': False, 'nativeRebootVerified': False}
    plan = installed(host)
    started = host.path(SETUP + '/SERVICE-STARTED.json')
    recorded = os.path.lexists(started)
    if recorded:
        receipt = strict(host.trusted(started))
        need(receipt.get('schema') == 'decrypt.customer-ingress-start.v1'
             and receipt.get('planSha256') == sha(canonical(plan))
             and receipt.get('serviceActive') is True and receipt.get('serviceEnabled') is True,
             'service_start_receipt_changed')
    active_code, active = host.systemctl('is-active', UNIT_NAME)
    enabled_code, enabled = host.systemctl('is-enabled', UNIT_NAME)
    return {'state': 'installed' if recorded else 'installed_service_start_unconfirmed',
            'publicOrigin': plan['publicOrigin'], 'serviceStartRecorded': recorded,
            'serviceActive': active_code == 0 and active == 'active',
            'serviceEnabled': enabled_code == 0 and enabled == 'enabled',
            'publicTlsVerified': False, 'publicReachabilityVerified': False, 'nativeRebootVerified': False,
            'credentialsInConfiguration': False, 'applicationPortExposed': False, 'debuggingPortsExposed': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    prepare_parser = actions.add_parser('prepare')
    prepare_parser.add_argument('--origin', required=True)
    inspect_parser = actions.add_parser('inspect')
    inspect_parser.add_argument('--plan-sha256')
    apply_parser = actions.add_parser('apply')
    apply_parser.add_argument('--plan-sha256', required=True)
    apply_parser.add_argument('--acknowledge-domain-control', action='store_true')
    apply_parser.add_argument('--acknowledge-public-service', action='store_true')
    actions.add_parser('verify-start', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        need(os.geteuid() == 0 and sys.flags.isolated and sys.platform == 'linux', 'installed_isolated_administrator_command_required')
        need(Path(__file__) == Path(ENTRY), 'use_root_owned_installed_entry')
        host = Host()
        host.trusted(Path(__file__))
        if args.action == 'prepare':
            result = prepare(host, args.origin)
        elif args.action == 'apply':
            result = apply(host, args.plan_sha256, controls_domain=args.acknowledge_domain_control,
                           public_service=args.acknowledge_public_service)
        elif args.action == 'verify-start':
            # Origin/dependency checks repeat on every service restart. Receiver
            # readiness is not required here: the desktop app may start later.
            installed(host)
            result = {'configurationVerified': True, 'publicTlsVerified': False}
        else:
            result = inspect(host, args.plan_sha256)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (Refused, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        code = str(exc) if isinstance(exc, Refused) else 'ingress_action_failed_inspect_preserved_state'
        print(json.dumps({'ok': False, 'error': code, 'publicTlsVerified': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
