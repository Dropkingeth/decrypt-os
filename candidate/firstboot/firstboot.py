#!/usr/bin/env python3
"""Pinned first-start service. Customer secrets never enter state, reports or argv."""
import argparse
import datetime
import errno
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import stat
import subprocess
import sys
import syslog
import traceback
import uuid
import warnings

SERVICE = '/usr/local/lib/decrypt-firstboot/firstboot.py'
POLICY = Path('/etc/decrypt-firstboot/policy.json')
CONFIG = Path('/etc/decrypt-firstboot/image.json')
NOTICE = Path('/usr/share/decrypt-firstboot/NOTICE.txt')
STATE_DIR = Path('/var/lib/decrypt-firstboot')
REPORT = Path('/run/decrypt-firstboot/report.json')
GATE = Path('/etc/nologin')
GATE_BYTES = b'Decrypt first-start account setup is pending.\n'
GREETER = Path('/usr/share/sddm/themes/decrypt/theme.conf')
GREETER_SELECTION = Path('/etc/sddm.conf.d/10-decrypt-theme.conf')
SUDO_RULE = Path('/etc/sudoers.d/20-decrypt-firstboot')
SUDO_BYTES = b'# Customer operations require their password.\n%wheel ALL=(ALL:ALL) PASSWD: ALL\n'
STATE_FIELDS = set('schema imageId policySha256 instanceId phase status seq accountName notice completedAt failure'.split())
UNIT_NAME = 'decrypt-firstboot.service'
# RuntimeDirectory is created by systemd itself before ExecStart runs, so its mode
# is applied directly and is never masked by this unit's own UMask=0077 (unlike a
# plain mkdir() inside the service, which the process umask would narrow to 0700).
# This keeps /run/decrypt-firstboot at 0755 for publish() without a second unit or
# a stacked .service.d override; there is still exactly one ExecStart.
UNIT_BYTES = b'[Unit]\nDescription=Create the local Decrypt account on first start\nAfter=local-fs.target\nBefore=display-manager.service systemd-user-sessions.service sshd.service\nConflicts=getty@tty1.service\nConditionPathExists=/etc/decrypt-firstboot/policy.json\n\n[Service]\nType=oneshot\nExecStart=/usr/bin/python3 -I -B /usr/local/lib/decrypt-firstboot/firstboot.py setup\nRemainAfterExit=yes\nRuntimeDirectory=decrypt-firstboot\nRuntimeDirectoryMode=0755\nRuntimeDirectoryPreserve=yes\nStandardInput=tty-force\nStandardOutput=tty\nStandardError=tty\nTTYPath=/dev/tty1\nTTYReset=yes\nTTYVHangup=yes\nTimeoutStartSec=infinity\nUMask=0077\n\n[Install]\nWantedBy=multi-user.target\n'
LOGIN_UNIT_BYTES = b'[Unit]\nRequires=decrypt-firstboot.service\nAfter=decrypt-firstboot.service\n'
CHECKS = ['account-setup', 'unique-identity', 'interactive-consent', 'root-locked', 'no-passwordless-rule']
HEX = re.compile(r'[a-f0-9]{64}\Z')
USER = re.compile(r'[a-z_][a-z0-9_-]{0,31}\Z')
RESERVED = {'root', 'nobody', 'bin', 'daemon', 'mail', 'ftp', 'http', 'dbus', 'sddm', 'polkitd', 'systemd-network', 'systemd-resolve'}


class Refusal(ValueError):
    """A stable non-secret failure code; never include input or subprocess output."""


def need(ok, code):
    if not ok:
        raise Refusal(code)


WELCOME=('\033[2J\033[H\n'
         '  DECRYPT OS\n'
         '  ----------\n'
         '  Welcome. Let us create your account on this computer.\n'
         '  You will use this name and password to sign in. Nothing is sent anywhere.\n\n')


TIMEZONES=('America/New_York','America/Chicago','America/Denver','America/Los_Angeles',
           'Europe/London','Europe/Berlin','Asia/Dubai','Asia/Singapore','Australia/Sydney','UTC')


def choose_timezone():
    """Ask once for the clock's time zone. Trading sessions are shown in New York
    time either way; this only sets the desktop clock. Never blocks setup."""
    print('\nWhich time zone should the clock use?')
    for i,zone in enumerate(TIMEZONES,1):print(f'  {i:>2}. {zone}')
    known=set(Path('/usr/share/zoneinfo/tzdata.zi').read_text().split()) if Path('/usr/share/zoneinfo/tzdata.zi').exists() else set()
    for _ in range(5):
        answer=input('Type a number or a zone name like Europe/Paris [1]: ').strip() or '1'
        zone=TIMEZONES[int(answer)-1] if answer.isdigit() and 1<=int(answer)<=len(TIMEZONES) else answer
        if re.fullmatch(r'[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}',zone) and (zone in TIMEZONES or zone in known or Path('/usr/share/zoneinfo',zone).is_file()):
            if subprocess.run(['/usr/bin/timedatectl','set-timezone',zone],check=False).returncode==0:
                print(f'Clock set to {zone}.');return
        print('That time zone was not recognised. Try a number from the list.')
    print('Keeping the current time zone. You can change it later in Settings.')


def masked_password(prompt):
    # getpass otherwise falls back to echoed stdin if terminal echo control fails.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except getpass.GetPassWarning:
        raise Refusal('password-echo-control-failed') from None


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def strict_json(raw):
    def pairs(rows):
        result = {}
        for key, value in rows:
            need(key not in result, 'duplicate-json-key'); result[key] = value
        return result
    def invalid(_):
        raise Refusal('nonfinite-json')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def uid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value and uuid.UUID(value).int != 0
    except (ValueError, AttributeError):
        return False


def username(value):
    return type(value) is str and USER.fullmatch(value) is not None and value not in RESERVED


def policy_check(value):
    need(type(value) is dict and set(value) == set('schema id version account identity notice service completionRequires'.split()), 'policy-shape')
    need(value['schema'] == 'decrypt.firstboot-policy.v1' and value['version'] == 1 and type(value['version']) is int, 'policy-version')
    need(type(value['id']) is str and re.fullmatch(r'[a-z][a-z0-9-]{0,63}', value['id']), 'policy-id')
    need(value['account'] == {'creation':'interactive-first-boot','rootLocked':True,'sudo':'password-required'}
         and type(value['account']['rootLocked']) is bool, 'policy-account')
    need(value['identity'] == {'machineId':'generate-once','sshHostKeys':'generate-once'}, 'policy-identity')
    notice=value['notice']; service=value['service']
    need(type(notice) is dict and set(notice)=={'mode','documentSha256'} and notice['mode']=='interactive'
         and type(notice['documentSha256']) is str and HEX.fullmatch(notice['documentSha256']), 'policy-notice')
    need(type(service) is dict and set(service)=={'path','sha256'} and service['path']==SERVICE
         and type(service['sha256']) is str and HEX.fullmatch(service['sha256']), 'policy-service')
    need(value['completionRequires']==CHECKS, 'policy-completion')
    return value


def secure_read(path, maximum=65536, mode=0o600, owner=0):
    path=Path(path);need(path.is_absolute() and '..' not in path.parts,'unsafe-path')
    for parent in path.parents:
        info=parent.lstat();need(stat.S_ISDIR(info.st_mode) and info.st_uid==owner and not info.st_mode&0o022,'unsafe-parent')
    before=path.lstat()
    need(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_uid==owner
         and stat.S_IMODE(before.st_mode)==mode and 0<before.st_size<=maximum,'unsafe-file')
    key=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as handle:
        need(key(os.fstat(handle.fileno()))==key(before),'changed-file')
        raw=handle.read(maximum+1)
        need(key(os.fstat(handle.fileno()))==key(before),'changed-file')
    need(key(path.lstat())==key(before) and len(raw)==before.st_size,'changed-file')
    return raw


def atomic(path, raw, mode, *, exclusive=False):
    path=Path(path)
    if exclusive:need(not path.exists() and not path.is_symlink(),'already-present')
    temporary=path.parent/('.'+path.name+'.'+uuid.uuid4().hex)
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    with os.fdopen(fd,'wb') as handle:
        os.fchmod(handle.fileno(),mode);handle.write(raw);handle.flush();os.fsync(handle.fileno())
    if exclusive:
        # link provides no-overwrite publication; only our newly created inode is linked.
        os.link(temporary,path,follow_symlinks=False);temporary.unlink()
    else:
        need(not path.is_symlink(),'linked-state');os.replace(temporary,path)
    directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(directory)
    finally:os.close(directory)


def state_check(value, image_id, policy_sha, document_sha):
    need(type(value) is dict and set(value)==STATE_FIELDS and value['schema']=='decrypt.firstboot-state.v1','state-shape')
    need(value['imageId']==image_id and value['policySha256']==policy_sha,'state-binding')
    need(type(value['seq']) is int and value['seq']>=0,'state-sequence')
    need(value['phase'] in ('identity','account','notice','complete') and value['status'] in ('pending','in-progress','failed','complete'),'state-phase')
    need(value['instanceId'] is None or uid(value['instanceId']),'state-instance')
    need(value['accountName'] is None or username(value['accountName']),'state-account')
    notice=value['notice']
    need(type(notice) is dict and set(notice)=={'documentSha256','accepted','acceptedAt'}
         and notice['documentSha256']==document_sha and type(notice['accepted']) is bool,'state-notice')
    need((notice['accepted'] and type(notice['acceptedAt']) is str) or (not notice['accepted'] and notice['acceptedAt'] is None),'state-notice-time')
    need(value['failure'] is None or (type(value['failure']) is dict and set(value['failure'])=={'code'}
         and type(value['failure']['code']) is str and re.fullmatch('[a-z0-9-]+',value['failure']['code'])),'state-failure')
    if value['phase']!='identity':need(uid(value['instanceId']),'state-identity-uncommitted')
    if value['phase'] in ('notice','complete'):need(username(value['accountName']),'state-account-uncommitted')
    if value['phase']=='complete':
        need(value['completedAt']==notice['acceptedAt'],'state-completion-time')
        try:
            stamp=datetime.datetime.fromisoformat(value['completedAt'])
            need(stamp.utcoffset()==datetime.timedelta(0),'state-completion-time')
        except (ValueError,TypeError):raise Refusal('state-completion-time')
        need(value['status']=='complete' and notice['accepted'] and type(value['completedAt']) is str
             and value['failure'] is None,'state-incomplete')
    else:need(value['completedAt'] is None and not notice['accepted'],'state-premature-acceptance')
    return value


class Store:
    """Caller holds lock; atomic commits never serialize password/key material."""
    def __init__(self,directory,image_id,policy_sha,document_sha):
        self.directory=Path(directory);self.path=self.directory/'state.json'
        self.image_id=image_id;self.policy_sha=policy_sha;self.document_sha=document_sha

    def load(self):
        if not self.path.exists() and not self.path.is_symlink():return None
        # Tests use only private temporary roots; native caller additionally validates all ancestors.
        info=self.path.lstat();need(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==os.geteuid()
                                   and stat.S_IMODE(info.st_mode)==0o600 and info.st_size<=16384,'unsafe-state')
        with os.fdopen(os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as handle:raw=handle.read(16385)
        return state_check(strict_json(raw),self.image_id,self.policy_sha,self.document_sha)

    def save(self,state):
        state_check(state,self.image_id,self.policy_sha,self.document_sha)
        previous=self.load()
        need((previous is None and state['seq']==0) or (previous and state['seq']==previous['seq']+1),'state-sequence')
        if previous is None:
            need(state['phase']=='identity' and state['status']=='pending' and state['instanceId'] is None
                 and state['accountName'] is None,'initial-state')
        else:
            old=(previous['phase'],previous['status']);new=(state['phase'],state['status'])
            allowed={('identity','pending'):{('identity','in-progress')},
                     ('identity','in-progress'):{('identity','failed'),('account','pending')},
                     ('identity','failed'):{('account','pending')},
                     ('account','pending'):{('account','in-progress')},
                     ('account','in-progress'):{('account','failed'),('notice','pending')},
                     ('account','failed'):{('account','failed'),('notice','pending')},
                     ('notice','pending'):{('complete','complete')}}
            need(new in allowed.get(old,set()),'state-transition')
            need(previous['instanceId'] is None or previous['instanceId']==state['instanceId'],'state-identity-changed')
            need(previous['accountName'] is None or previous['accountName']==state['accountName'],'state-account-changed')
        atomic(self.path,(json.dumps(state,sort_keys=True,indent=2)+'\n').encode(),0o600,exclusive=previous is None)
        return state

    def initial(self):
        need(self.load() is None,'existing-state')
        return self.save({'schema':'decrypt.firstboot-state.v1','imageId':self.image_id,'policySha256':self.policy_sha,
            'instanceId':None,'phase':'identity','status':'pending','seq':0,'accountName':None,
            'notice':{'documentSha256':self.document_sha,'accepted':False,'acceptedAt':None},'completedAt':None,'failure':None})

    def update(self,state,**changes):
        return self.save({**state,**changes,'seq':state['seq']+1})


class Controller:
    def __init__(self,store,backend):self.store=store;self.backend=backend

    def identity(self,state,recovery=False):
        need(state['phase']=='identity','wrong-recovery-phase')
        need(state['status']=='pending' or recovery,'identity-outcome-unknown')
        if recovery:
            need(state['status'] in ('in-progress','failed'),'identity-recovery-not-needed')
            # Observe a complete existing identity; never regenerate keys during recovery.
            instance=self.backend.identity_status()
        else:
            state=self.store.update(state,status='in-progress')
            try:
                self.backend.generate_identity()
                instance=self.backend.identity_status()
            except Exception:
                self.store.update(state,status='failed',failure={'code':'identity-outcome-unknown'})
                raise Refusal('identity-outcome-unknown')
        need(uid(instance),'identity-not-ready')
        return self.store.update(state,instanceId=instance,phase='account',status='pending',failure=None)

    def account(self,state,name,password,recovery=False):
        need(state['phase']=='account' and username(name),'account-request')
        need(state['instanceId']==self.backend.identity_status(),'identity-changed')
        need(type(password) is str and len(password)>=8 and not any(c in password for c in '\r\n\x00'),'password-invalid')
        if recovery:
            need(state['accountName']==name and state['status'] in ('in-progress','failed'),'account-recovery-binding')
            self.backend.verify_account_identity(name)
        else:
            need(state['status']=='pending' and state['accountName'] is None,'account-outcome-unknown')
            self.backend.account_absent(name)
            state=self.store.update(state,status='in-progress',accountName=name)
        # Password goes only to the backend's anonymous stdin pipe, never a file/argv/state/log.
        try:
            if not recovery:self.backend.create_account(name)
            self.backend.set_password(name,password)
            self.backend.install_sudo_policy()
            self.backend.install_greeter_account(name)
            self.backend.verify_account(name)
        except Exception:
            self.store.update(state,status='failed',failure={'code':'account-outcome-unknown'})
            raise Refusal('account-outcome-unknown')
        state=self.store.update(state,phase='notice',status='pending',failure=None)
        self.backend.install_runtime_authorization(state)
        self.backend.release_login()
        return state

    def accept(self,state,policy_sha,document_sha,instance,actor):
        need(state['phase']=='notice' and state['status']=='pending','notice-not-pending')
        need(policy_sha==state['policySha256'] and document_sha==state['notice']['documentSha256']
             and instance==state['instanceId'] and actor==state['accountName'],'notice-binding')
        self.backend.install_runtime_authorization(state)
        self.backend.verify_consent_actor(actor)
        self.backend.verify_account(actor)
        need(instance==self.backend.identity_status(),'identity-changed')
        # No separate in-progress notice commit: a canceled callback has no effect, and
        # acceptance itself is one durable atomic state commit after all observations.
        now=datetime.datetime.now(datetime.timezone.utc).isoformat()
        return self.store.update(state,phase='complete',status='complete',completedAt=now,
            notice={'documentSha256':document_sha,'accepted':True,'acceptedAt':now},failure=None)

    def report(self,state):
        if state is None:return {'schema':'decrypt.firstboot-report.v1','imageId':self.store.image_id,
            'policySha256':self.store.policy_sha,'status':'not-started','instanceId':None,'complete':False,'consentAccepted':False}
        if state['phase']!='identity':need(state['instanceId']==self.backend.identity_status(),'identity-changed')
        if state['phase'] in ('notice','complete'):
            self.backend.verify_account(state['accountName']);self.backend.verify_runtime_authorization(state)
        return {'schema':'decrypt.firstboot-report.v1','imageId':state['imageId'],'policySha256':state['policySha256'],
            'status':state['status'],'phase':state['phase'],'instanceId':state['instanceId'],'accountName':state['accountName'],
            'complete':state['phase']=='complete' and state['status']=='complete',
            'consentAccepted':state['notice']['accepted'],'documentSha256':state['notice']['documentSha256'],
            'completedAt':state['completedAt'],'sequence':state['seq']}


def command(argv,input_bytes=None):
    result=subprocess.run(argv,input=input_bytes,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120,check=False)
    need(result.returncode==0,'required-command-failed')
    return result.stdout.decode('utf-8',errors='strict').strip()


def greeter_bytes(name):
    need(username(name),'invalid-account')
    return ('[General]\nUsername='+name+'\n').encode()


def check_greeter_source(raw,name):
    need(raw in (b'[General]\n',greeter_bytes(name)),'greeter-account-conflict')


def root_locked():
    rows=[line.split(':') for line in Path('/etc/shadow').read_text().splitlines() if line.startswith('root:')]
    need(len(rows)==1 and len(rows[0])>=2 and rows[0][1].startswith(('!','*')),'root-not-locked')


def validate_sudo_text(text):
    # sudoers joins escaped physical lines before tokenization. Scan that same
    # logical text so split tags/defaults/includes cannot evade the refusal.
    text=re.sub(r'\\\r?\n','',text)
    for line in text.splitlines():
        stripped=line.strip()
        if stripped.startswith(('#include','@include')):
            need(stripped in ('@includedir /etc/sudoers.d','#includedir /etc/sudoers.d'),'unreviewed-sudo-include')
        elif not stripped.startswith('#'):
            need(not re.search(r'NOPASSWD\s*:|!\s*authenticate|exempt_group\s*=',stripped),'passwordless-sudo-policy')


def sudo_check():
    paths=[Path('/etc/sudoers')]+sorted(Path('/etc/sudoers.d').iterdir())
    for path in paths:
        if path.name.startswith('.') or path.name.endswith('~'):continue
        info=path.lstat();need(stat.S_ISREG(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022,'unsafe-sudo-policy')
        text=path.read_text();need(len(text)<=1024*1024,'sudo-policy-too-large')
        validate_sudo_text(text)
    command(['/usr/sbin/visudo','-c'])


def authorization_documents(state,machine,account,source):
    need(state['phase'] in ('notice','complete') and uid(state['instanceId']),'authorization-state')
    need(re.fullmatch('[a-f0-9]{32}',machine) and int(machine,16)!=0,'authorization-machine')
    need(account.pw_name==state['accountName'] and 1000<=account.pw_uid<65534,'authorization-account')
    common={'instanceId':state['instanceId'],'imageId':state['imageId'],'policySha256':state['policySha256'],
        'desktopUser':account.pw_name,'desktopUid':account.pw_uid,'sourceManifestSha256':digest(source)}
    machine_sha=digest(machine.encode())
    policy={'schema':'decrypt.customer-host-policy.v2','machineSha256':machine_sha,
        **{k:v for k,v in common.items() if k!='desktopUser'},'accountName':account.pw_name}
    marker={'schema':'decrypt.customer-test.v1','role':'clean-customer-test','machineSha256':machine_sha,**common,'cleanVmVerified':False}
    receipt={'schema':'decrypt.firstboot-install-receipt.v1','installed':True,'stageOnly':False,
         'targetMachineSha256':machine_sha,**common,'servicesStarted':False}
    return policy,marker,receipt


class Native:
    def generate_identity(self):
        root_locked();sudo_check()
        need(not [a for a in pwd.getpwall() if 1000<=a.pw_uid<65534],'factory-customer-account-present')
        # systemd may have committed a fresh ID earlier in this imported instance's boot.
        command(['/usr/bin/systemd-machine-id-setup'])
        command(['/usr/bin/ssh-keygen','-A'])

    def identity_status(self):
        path=Path('/etc/machine-id');info=path.lstat()
        need(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==0 and not info.st_mode&0o022 and info.st_size<=64,'machine-id-file')
        machine=path.read_text().strip()
        need(re.fullmatch('[a-f0-9]{32}',machine) and int(machine,16)!=0,'machine-id-not-ready')
        identity_parts=[machine]
        for algorithm in ('rsa','ecdsa','ed25519'):
            path=Path('/etc/ssh/ssh_host_'+algorithm+'_key')
            info=path.lstat();need(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_uid==0
                                  and stat.S_IMODE(info.st_mode)==0o600,'ssh-identity-not-ready')
            public=Path(str(path)+'.pub');pub=public.lstat()
            need(stat.S_ISREG(pub.st_mode) and pub.st_uid==0 and not pub.st_mode&0o022,'ssh-public-identity-not-ready')
            derived=command(['/usr/bin/ssh-keygen','-y','-f',str(path)])
            need(public.read_text().split()[:2]==derived.split()[:2],'ssh-identity-pair-mismatch')
            identity_parts.append(' '.join(derived.split()[:2]))
        # Public-key material and machine ID stay out of reports. This UUID changes
        # if any committed identity changes; it is not a secret or reusable credential.
        return str(uuid.UUID(bytes=hashlib.sha256('\n'.join(identity_parts).encode()).digest()[:16],version=4))

    def runtime_authorization_documents(self,state):
        need(state['phase'] in ('notice','complete') and state['instanceId']==self.identity_status(),'authorization-instance-differs')
        self.verify_account(state['accountName'])
        machine=secure_read(Path('/etc/machine-id'),mode=0o444).decode().strip() if Path('/etc/machine-id').stat().st_mode&0o777==0o444 else secure_read(Path('/etc/machine-id'),mode=0o644).decode().strip()
        source=secure_read(Path('/usr/local/lib/decrypt-os/SOURCE-MANIFEST.json'),maximum=2*1024**2,mode=0o644)
        strict_json(source)
        account=pwd.getpwnam(state['accountName'])
        policy,marker,receipt=authorization_documents(state,machine,account,source)
        return [(Path('/etc/decrypt-os/customer-test.json'),marker),
                           (Path('/usr/local/lib/decrypt-os/INSTALL-RECEIPT.json'),receipt),
                           (Path('/etc/decrypt-os/instance-policy.json'),policy)]

    def verify_runtime_authorization(self,state):
        for path,value in self.runtime_authorization_documents(state):
            need(secure_read(path,mode=0o644)==(json.dumps(value,sort_keys=True,indent=2)+'\n').encode(),'authorization-existing-conflict')

    def install_runtime_authorization(self,state):
        # Policy is published last: partial metadata never authorizes a runtime.
        for path,value in self.runtime_authorization_documents(state):
            if not path.parent.exists():
                for parent in path.parent.parents:
                    info=parent.lstat();need(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022,'authorization-parent')
                path.parent.mkdir(mode=0o755)
            raw=(json.dumps(value,sort_keys=True,indent=2)+'\n').encode()
            if path.exists() or path.is_symlink():need(secure_read(path,mode=0o644)==raw,'authorization-existing-conflict')
            else:
                for parent in path.parents:
                    info=parent.lstat();need(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022,'authorization-parent')
                atomic(path,raw,0o644,exclusive=True)

    def account_absent(self,name):
        need(username(name),'invalid-account')
        need(not [a for a in pwd.getpwall() if a.pw_name==name or 1000<=a.pw_uid<65534],'account-already-present')
        need(not (Path('/home')/name).exists() and not (Path('/home')/name).is_symlink(),'home-already-present')

    def create_account(self,name):
        command(['/usr/sbin/useradd','--create-home','--user-group','--groups','wheel','--shell','/bin/bash','--',name])

    def verify_account_identity(self,name):
        root_locked();account=pwd.getpwnam(name)
        need(1000<=account.pw_uid<65534 and account.pw_dir=='/home/'+name and account.pw_shell=='/bin/bash','account-identity-differs')
        home=Path(account.pw_dir);info=home.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid==account.pw_uid and not info.st_mode&0o022,'unsafe-customer-home')
        need([a.pw_name for a in pwd.getpwall() if 1000<=a.pw_uid<65534]==[name],'unexpected-customer-accounts')
        need('wheel' in command(['/usr/bin/id','-nG',name]).split(),'sudo-group-missing')

    def set_password(self,name,password):
        command(['/usr/sbin/chpasswd'],(name+':'+password+'\n').encode())

    def install_sudo_policy(self):
        root_locked();sudo_check()
        if SUDO_RULE.exists():need(SUDO_RULE.read_bytes()==SUDO_BYTES,'sudo-rule-conflict')
        else:atomic(SUDO_RULE,SUDO_BYTES,0o440,exclusive=True)
        sudo_check()

    def install_greeter_account(self,name):
        need(secure_read(GREETER_SELECTION,mode=0o644)==b'[Theme]\nCurrent=decrypt\n','greeter-selection-differs')
        raw=secure_read(GREETER,mode=0o644);check_greeter_source(raw,name)
        if raw!=greeter_bytes(name):atomic(GREETER,greeter_bytes(name),0o644)

    def verify_account(self,name):
        self.verify_account_identity(name);sudo_check()
        fields=command(['/usr/bin/passwd','-S',name]).split()
        need(len(fields)>=2 and fields[:2]==[name,'P'],'password-not-set')
        need(secure_read(SUDO_RULE,mode=0o440)==SUDO_BYTES,'customer-sudo-policy-differs')
        need(secure_read(GREETER_SELECTION,mode=0o644)==b'[Theme]\nCurrent=decrypt\n'
             and secure_read(GREETER,mode=0o644)==greeter_bytes(name),'customer-greeter-binding-differs')

    def release_login(self):
        if GATE.exists():
            need(secure_read(GATE,mode=0o644)==GATE_BYTES,'foreign-login-gate')
            GATE.unlink();fd=os.open('/etc',os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)

    def verify_consent_actor(self,name):
        need(os.environ.get('SUDO_USER')==name and os.environ.get('SUDO_UID')==str(pwd.getpwnam(name).pw_uid),'consent-actor-mismatch')
        need(not os.environ.get('SSH_CONNECTION') and not os.environ.get('SSH_TTY'),'remote-consent-refused')
        # Ask logind, not user-controlled XDG/DISPLAY variables. Exactly one local,
        # active graphical session for the enrolled account must exist.
        sessions=command(['/usr/bin/loginctl','list-sessions','--no-legend','--no-pager']).splitlines()
        eligible=[]
        for line in sessions:
            columns=line.split()
            if len(columns)>=3 and columns[1]==str(pwd.getpwnam(name).pw_uid):
                data=command(['/usr/bin/loginctl','show-session',columns[0],'-p','Name','-p','Active','-p','Remote','-p','Type'])
                values=dict(row.split('=',1) for row in data.splitlines() if '=' in row)
                if values.get('Name')==name and values.get('Active')=='yes' and values.get('Remote')=='no' and values.get('Type') in ('wayland','x11'):eligible.append(columns[0])
        need(len(eligible)==1,'active-local-app-session-required')


def check_service_staging():
    unit=Path('/etc/systemd/system')/UNIT_NAME
    need(secure_read(unit,mode=0o644)==UNIT_BYTES,'service-unit-differs')
    for name in ('display-manager.service','systemd-user-sessions.service','sshd.service'):
        path=Path('/etc/systemd/system')/(name+'.d')/'10-decrypt-firstboot.conf'
        need(secure_read(path,mode=0o644)==LOGIN_UNIT_BYTES,'login-dependency-differs')
    enabled=Path('/etc/systemd/system/multi-user.target.wants')/UNIT_NAME
    info=enabled.lstat()
    need(stat.S_ISLNK(info.st_mode) and info.st_uid==0 and enabled.resolve(strict=True)==unit,'service-not-enabled')


def load_inputs():
    raw=secure_read(POLICY,mode=0o644);policy=policy_check(strict_json(raw))
    need(digest(secure_read(NOTICE,mode=0o644))==policy['notice']['documentSha256'],'installed-notice-differs')
    config=strict_json(secure_read(CONFIG,mode=0o644))
    need(type(config) is dict and set(config)=={'schema','imageId','policySha256'} and config['schema']=='decrypt.firstboot-image.v1','image-config-shape')
    need(type(config['imageId']) is str and re.fullmatch('[a-z0-9][a-z0-9._-]{0,95}',config['imageId']),'image-id')
    need(config['policySha256']==digest(raw) and policy['service']['sha256']==digest(secure_read(Path(SERVICE),maximum=128*1024,mode=0o644)),'source-policy-binding')
    return config,policy


def native_console():
    need(platform.system()=='Linux' and os.geteuid()==0,'linux-root-required')
    need(sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get('SSH_CONNECTION')
         and not os.environ.get('SSH_TTY') and not os.environ.get('SUDO_USER'),'native-first-start-console-required')


def prepare_directory():
    if not STATE_DIR.exists():STATE_DIR.mkdir(mode=0o700)
    info=STATE_DIR.lstat();need(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and stat.S_IMODE(info.st_mode)==0o700,'unsafe-state-directory')
    for parent in STATE_DIR.parents:
        info=parent.lstat();need(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022,'unsafe-state-parent')


def ensure_report_directory(directory):
    """Create the report directory with a mode that survives a restrictive UMask=.

    mkdir()'s mode argument is narrowed by the caller's umask (this unit runs with
    UMask=0077, which alone would silently leave a freshly created directory at
    0700, not the 0755 publish() requires). chmod() is never subject to umask, so
    fix the mode explicitly rather than trusting mkdir() to have produced it.
    RuntimeDirectory= on the unit normally creates this directory before we ever
    reach this function; this remains correct standalone (e.g. under test, or a
    manual run with a different umask).
    """
    if not directory.exists():
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)


def publish(report):
    directory=REPORT.parent
    ensure_report_directory(directory)
    info=directory.lstat();need(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and stat.S_IMODE(info.st_mode)==0o755,'unsafe-report-directory')
    atomic(REPORT,(json.dumps(report,sort_keys=True)+'\n').encode(),0o644)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    parser.add_argument('action',choices=('setup','recover-identity','recover-account','accept-notice','readiness','completion'))
    parser.add_argument('--policy-sha256');parser.add_argument('--document-sha256');parser.add_argument('--instance-id')
    args=parser.parse_args(argv)
    need(platform.system()=='Linux' and os.geteuid()==0,'linux-root-required')
    config,policy=load_inputs()
    if args.action in ('readiness','completion'):
        need(args.policy_sha256==config['policySha256'] and args.document_sha256 is None and args.instance_id is None,'report-policy-binding')
    elif args.action != 'accept-notice':
        need(args.policy_sha256 is None and args.document_sha256 is None and args.instance_id is None,'unexpected-arguments')
    check_service_staging()
    if args.action in ('readiness','completion'):
        store=Store(STATE_DIR,config['imageId'],config['policySha256'],policy['notice']['documentSha256'])
        if not STATE_DIR.exists():state=None
        else:
            info=STATE_DIR.lstat();need(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and stat.S_IMODE(info.st_mode)==0o700,'unsafe-state-directory')
            state=store.load()
        if state is None:
            need(args.action=='readiness','first-start-not-complete')
            root_locked();sudo_check()
            need(not Path('/etc/machine-id').read_text().strip(),'factory-machine-id-present')
            need(not list(Path('/etc/ssh').glob('ssh_host_*_key*')),'factory-ssh-identity-present')
            need(not [a for a in pwd.getpwall() if 1000<=a.pw_uid<65534],'factory-customer-account-present')
            need(secure_read(GATE,mode=0o644)==GATE_BYTES,'login-gate-not-staged')
            report={'schema':'decrypt.firstboot-readiness.v1','imageId':config['imageId'],'policySha256':config['policySha256'],
                    'documentSha256':policy['notice']['documentSha256'],'serviceSha256':policy['service']['sha256'],
                    'ready':True,'customerAccountCreated':False,'consentAccepted':False,'firstStartVerified':False}
        else:
            report=Controller(store,Native()).report(state)
            need(store.load()==state,'state-changed-during-observation')
            need(args.action!='completion' or report['complete'],'first-start-not-complete')
        print(json.dumps(report,sort_keys=True));return 0
    prepare_directory()
    lockfd=os.open(STATE_DIR/'lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(lockfd);need(stat.S_ISREG(info.st_mode) and info.st_uid==0 and info.st_nlink==1 and stat.S_IMODE(info.st_mode)==0o600,'unsafe-state-lock')
        fcntl.flock(lockfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        store=Store(STATE_DIR,config['imageId'],config['policySha256'],policy['notice']['documentSha256'])
        backend=Native();ctl=Controller(store,backend);state=store.load()
        if args.action=='accept-notice':
            need(state is not None,'first-start-not-ready')
            state=ctl.accept(state,args.policy_sha256,args.document_sha256,args.instance_id,os.environ.get('SUDO_USER'))
        else:
            native_console()
            if state is None:
                need(args.action=='setup','recovery-state-missing');state=store.initial()
            if args.action.startswith('recover-'):
                need(input('Type RECOVER to inspect and complete the recorded local step: ')=='RECOVER','recovery-cancelled')
            if state['phase']=='identity':
                state=ctl.identity(state,recovery=args.action=='recover-identity')
            elif args.action=='recover-identity':raise Refusal('identity-already-committed')
            if state['phase']=='account':
                need(state['status']=='pending' or args.action=='recover-account','account-outcome-unknown')
                if args.action=='recover-account':name=state['accountName']
                else:
                    print(WELCOME,end='',flush=True)
                    while True:
                        name=input('Choose a user name (lowercase letters, numbers, - or _): ').strip()
                        if username(name):break
                        if name in RESERVED:print('That name is reserved by the system. Choose another, for example: trader')
                        else:print('That name will not work. Start with a lowercase letter, for example: trader')
                while True:
                    password=masked_password('Choose a password (at least 8 characters): ')
                    if len(password)<8:print('That password is too short. Use at least 8 characters.');continue
                    confirmation=masked_password('Type the password again: ')
                    if password==confirmation:break
                    print('The two passwords did not match. Please try again.')
                try:state=ctl.account(state,name,password,recovery=args.action=='recover-account')
                finally:password=None;confirmation=None
                if args.action!='recover-account':choose_timezone()
            elif args.action=='recover-account':raise Refusal('account-already-committed')
            # After a crash between account commit and removing our own login gate,
            # verify the committed account, then finish only that reversible step.
            if state['phase'] in ('notice','complete'):
                backend.verify_account(state['accountName']);backend.install_runtime_authorization(state);backend.release_login()
        report=ctl.report(state);publish(report)
        if not report['complete']:print('Account setup is ready. Read and accept the notice in the Decrypt app to complete first start.')
        return 0
    finally:os.close(lockfd)


def journal(text):
    """Best-effort error line in the system journal. The unit sends stderr to tty1 only,
    so without this a boot-time failure leaves no record in journalctl."""
    try:
        syslog.openlog('decrypt-firstboot',syslog.LOG_PID,syslog.LOG_DAEMON)
        syslog.syslog(syslog.LOG_ERR,text)
    except Exception:pass


def failure_detail(error):
    """Non-secret diagnosis: exception type, refusal code, OS error number and path, and
    code locations. Other exception messages are left out; they could carry command output."""
    parts=[type(error).__name__]
    if isinstance(error,Refusal) and re.fullmatch('[a-z0-9-]+',str(error)):parts.append(str(error))
    if isinstance(error,OSError):
        if error.errno:parts.append(errno.errorcode.get(error.errno,str(error.errno)))
        if isinstance(error.filename,str):parts.append(error.filename)
    frames=traceback.extract_tb(error.__traceback__)[-8:]
    parts.append('at '+' <- '.join('%s:%s:%s'%(Path(frame.filename).name,frame.lineno,frame.name) for frame in reversed(frames)))
    return ' '.join(parts)


def login_already_released():
    """True once an earlier run committed the account and removed this service's own login
    gate. Both paths are root-controlled. Until then a failed setup keeps every login gated."""
    try:
        return not GATE.exists() and not GATE.is_symlink() and stat.S_ISREG((STATE_DIR/'state.json').lstat().st_mode)
    except OSError:return False


def run(argv=None):
    argv=sys.argv[1:] if argv is None else list(argv)
    try:return main(argv)
    except (KeyboardInterrupt,EOFError):
        print('Setup paused. No consent was accepted.',file=sys.stderr);return 130
    except Exception as error:
        code=str(error) if isinstance(error,Refusal) and re.fullmatch('[a-z0-9-]+',str(error)) else 'verification-or-operation-failed'
        detail=failure_detail(error)
        if argv==['setup'] and login_already_released():
            # The boot-time re-check after a finished first start must not lock the owner
            # out of their own computer (root is locked): sign-in was already allowed on an
            # earlier boot. No report is published, so the Decrypt app stays locked.
            try:REPORT.unlink(missing_ok=True)
            except OSError:pass
            journal('check after completed setup failed: '+code+'; sign-in stays allowed, app stays locked; '+detail)
            print('First-start check failed: '+code+'. You can still sign in. The Decrypt app stays locked until this is fixed. '
                  'Details: journalctl -u decrypt-firstboot',file=sys.stderr)
            return 0
        journal('setup stopped: '+code+'; '+detail)
        print('Setup stopped: '+code+'. Preserve state; use the documented recovery step.',file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(run())
