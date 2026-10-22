"""First-start display and fixed local password-terminal admission; no secret HTTP flow."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import threading
import time
import types
import uuid

POLICY_SHA256='d5ae86d05438497d161eb172d438aa8910513945fcfbc1ed243ea932ebc139b2'
NOTICE_SHA256='2a1903232950f56deba4a8cdd76e79e7b1022366fd6e56510c8c4bbf28ab8055'
CALLBACK_SHA256='235ffa217c78edbe04609b5bee19d003bf45d88b008e0c268ede6e902743b379'
POLICY=Path('/etc/decrypt-firstboot/policy.json')
IMAGE=Path('/etc/decrypt-firstboot/image.json')
REPORT=Path('/run/decrypt-firstboot/report.json')
NOTICE=Path('/usr/share/decrypt-firstboot/NOTICE.txt')
HELPER=Path('/usr/local/lib/decrypt-firstboot/firstboot.py')
CALLBACK=Path('/usr/local/lib/decrypt-os/installer/install_notice.py')
SELF=Path('/usr/local/lib/decrypt-os/app/backend/first_start.py')
UNIT='decrypt-first-start-notice.service'
HEX=re.compile(r'[0-9a-f]{64}')
REPORT_FIELDS=set('schema imageId policySha256 status phase instanceId accountName complete consentAccepted documentSha256 completedAt sequence'.split())

class FirstStartError(ValueError):
    def __init__(self,code,message,status=409):self.code,self.message,self.status=code,message,status;super().__init__(message)

def need(ok,code='first_start_unavailable',message='First-start setup is not ready. Sign in locally after account setup, then refresh.',status=409):
    if not ok:raise FirstStartError(code,message,status)

def digest(raw):return hashlib.sha256(raw).hexdigest()

def strict(raw):
    def pairs(rows):
        value={}
        for key,item in rows:need(key not in value);value[key]=item
        return value
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))

def secure_read(path,maximum=131072,modes=(0o644,)):
    path=Path(path)
    for parent in path.parents:
        info=parent.lstat();need(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022)
    before=path.lstat()
    need(stat.S_ISREG(before.st_mode) and before.st_nlink==1 and before.st_uid==0
         and stat.S_IMODE(before.st_mode) in modes and 0<before.st_size<=maximum)
    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb') as source:
        raw=source.read(maximum+1);after=os.fstat(source.fileno())
    need((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)==
         (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns) and len(raw)==before.st_size)
    return raw

def native_run(argv,env=None,timeout=3):
    result=subprocess.run(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                          env=env or {'PATH':'/usr/bin:/bin','LC_ALL':'C'},timeout=timeout,check=False)
    need(len(result.stdout)<=65536)
    return result.returncode,result.stdout.decode('utf-8','strict')

def public_instance(read=secure_read):
    machine=read(Path('/etc/machine-id'),64,(0o444,0o644)).decode().strip()
    need(re.fullmatch(r'[0-9a-f]{32}',machine) and int(machine,16))
    parts=[machine]
    for algorithm in ('rsa','ecdsa','ed25519'):
        raw=read(Path('/etc/ssh/ssh_host_'+algorithm+'_key.pub'),16384).decode().split()
        need(len(raw)>=2 and raw[0] in ('ssh-rsa','ecdsa-sha2-nistp256','ssh-ed25519'))
        parts.append(' '.join(raw[:2]))
    # Display correlation only: root helper independently verifies private/public pairs.
    return str(uuid.UUID(bytes=hashlib.sha256('\n'.join(parts).encode()).digest()[:16],version=4))

def report_fresh():
    need(sys.platform.startswith('linux'))
    with open('/proc/uptime','r',encoding='ascii') as source:raw=source.read(128)
    uptime=float(raw.split()[0]);now=time.time();stamp=REPORT.stat().st_mtime
    need(uptime>0 and now-uptime-2<=stamp<=now+2)

def process_matches(pid,argv,uid):
    need(type(pid) is int and pid>1)
    directory=Path('/proc')/str(pid);need(directory.stat().st_uid==uid)
    with (directory/'cmdline').open('rb') as source:raw=source.read(8193)
    need(len(raw)<=8192 and raw.rstrip(b'\0').split(b'\0')==[x.encode() for x in argv])

def actor(name,run=native_run):
    uid=os.geteuid();need(sys.platform.startswith('linux') and 1000<=uid<65534 and pwd.getpwuid(uid).pw_name==name)
    need(not os.environ.get('SSH_CONNECTION') and not os.environ.get('SSH_TTY'))
    rc,raw=run(['/usr/bin/loginctl','list-sessions','--no-legend','--no-pager']);need(rc==0)
    eligible=[]
    for line in raw.splitlines():
        columns=line.split()
        if len(columns)>=3 and columns[1]==str(uid):
            need(re.fullmatch(r'[A-Za-z0-9]+',columns[0]))
            rc,properties=run(['/usr/bin/loginctl','show-session',columns[0],'-p','Name','-p','Active','-p','Remote','-p','Type'])
            values=dict(row.split('=',1) for row in properties.splitlines() if '=' in row)
            if rc==0 and values.get('Name')==name and values.get('Active')=='yes' and values.get('Remote')=='no' and values.get('Type')=='wayland':eligible.append(columns[0])
    need(len(eligible)==1,'local_session_required','Use the enrolled account in its local desktop session, then refresh.')
    return uid

def gui_environment(uid):
    runtime=Path('/run/user')/str(uid);info=runtime.lstat()
    need(stat.S_ISDIR(info.st_mode) and info.st_uid==uid and stat.S_IMODE(info.st_mode)==0o700)
    display=os.environ.get('WAYLAND_DISPLAY','');need(re.fullmatch(r'wayland-[0-9]+',display),'terminal_unavailable','Open Decrypt OS from the local desktop, then try again.')
    for path in (runtime/display,runtime/'bus'):
        info=path.lstat();need(stat.S_ISSOCK(info.st_mode) and info.st_uid==uid)
    return {'PATH':'/usr/bin:/bin','LC_ALL':'C.UTF-8','XDG_RUNTIME_DIR':str(runtime),
            'DBUS_SESSION_BUS_ADDRESS':'unix:path='+str(runtime/'bus'),'WAYLAND_DISPLAY':display}

def terminal_argv(binding):
    return ['/usr/bin/foot','--app-id=decrypt-first-start-notice','--title=Decrypt-first-start',
            '/usr/bin/python3','-I','-B',str(SELF),'terminal','--policy-sha256',binding['policySha256'],
            '--document-sha256',binding['documentSha256'],'--instance-id',binding['instanceId']]

def description(binding):
    raw=':'.join(binding[k] for k in ('policySha256','documentSha256','instanceId'))
    return 'Decrypt-first-start-'+digest(raw.encode())

class FirstStart:
    def __init__(self,read=secure_read,run=native_run,actor_check=actor,instance=public_instance,gui=gui_environment,fresh=report_fresh,process_check=process_matches):
        self.read,self.run,self.actor_check,self.instance,self.gui=read,run,actor_check,instance,gui
        self.fresh,self.process_check=fresh,process_check
        self.lock=threading.RLock();self.sequence={}
    def projection(self):
        policy_raw=self.read(POLICY);need(digest(policy_raw)==POLICY_SHA256)
        policy=strict(policy_raw);notice=self.read(NOTICE,16384)
        need(digest(notice)==NOTICE_SHA256 and policy['notice']=={'mode':'interactive','documentSha256':NOTICE_SHA256})
        need(policy['account']=={'creation':'interactive-first-boot','rootLocked':True,'sudo':'password-required'})
        need(policy['service']['path']==str(HELPER) and digest(self.read(HELPER))==policy['service']['sha256'])
        need(digest(self.read(CALLBACK))==CALLBACK_SHA256)
        image=strict(self.read(IMAGE));need(set(image)=={'schema','imageId','policySha256'} and image['schema']=='decrypt.firstboot-image.v1' and image['policySha256']==POLICY_SHA256)
        raw=self.read(REPORT,16384);self.fresh();report=strict(raw)
        need(type(report) is dict and set(report)==REPORT_FIELDS and report['schema']=='decrypt.firstboot-report.v1'
             and report['imageId']==image['imageId'] and report['policySha256']==POLICY_SHA256 and report['documentSha256']==NOTICE_SHA256)
        need(type(report['sequence']) is int and report['sequence']>=1 and report['phase'] in ('notice','complete'))
        need(type(report['accountName']) is str and re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',report['accountName']))
        need(type(report['instanceId']) is str and str(uuid.UUID(report['instanceId']))==report['instanceId'] and report['instanceId']==self.instance())
        complete=report['phase']=='complete'
        need(report['status']==('complete' if complete else 'pending') and report['complete'] is complete and report['consentAccepted'] is complete
             and (type(report['completedAt']) is str if complete else report['completedAt'] is None))
        need(report['sequence']>=self.sequence.get(report['instanceId'],0));self.sequence[report['instanceId']]=report['sequence']
        uid=self.actor_check(report['accountName'])
        return {'schema':'decrypt.first-start-display.v1','status':'complete' if complete else 'pending',
                'policySha256':POLICY_SHA256,'documentSha256':NOTICE_SHA256,'instanceId':report['instanceId'],
                'noticeText':notice.decode('utf-8'),'complete':complete,'source':'root-published-display',
                'nativeAcceptanceEvidence':False,'sequence':report['sequence']},uid
    def unit(self,binding,env):
        properties=['Id','LoadState','ActiveState','SubState','Transient','Description','ExecStart','User','RuntimeMaxUSec','KillMode','MainPID','NoNewPrivileges']
        argv=['/usr/bin/systemctl','--user','show','--no-pager',*['--property='+p for p in properties],UNIT]
        rc,text=self.run(argv,env);values=dict(row.split('=',1) for row in text.splitlines() if '=' in row)
        if values.get('LoadState')=='not-found':return {'active':False}
        need(rc==0 and values.get('Id')==UNIT and values.get('Transient')=='yes' and values.get('Description')==description(binding),
             'terminal_conflict','A different local prompt is using the first-start unit. Close it and refresh.')
        match=re.fullmatch(r'\{ path=/usr/bin/foot ; argv\[\]=(.*?) ; ignore_errors=no ; .*\}',values.get('ExecStart',''))
        need(match and match.group(1)==' '.join(terminal_argv(binding)) and values.get('User') in ('',pwd.getpwuid(os.geteuid()).pw_name)
             and values.get('KillMode')=='control-group' and values.get('NoNewPrivileges')=='no'
             and values.get('RuntimeMaxUSec') in ('3min','3min 0s','180s'),'terminal_conflict','The local prompt does not match this notice. Close it and refresh.')
        active=values.get('ActiveState') in ('active','activating','deactivating')
        if active:self.process_check(int(values.get('MainPID','0')),terminal_argv(binding),os.geteuid())
        return {'active':active}
    def status(self):
        with self.lock:
            try:
                value,uid=self.projection()
                if value['complete']:return {**value,'promptRunning':False}
                env=self.gui(uid);unit=self.unit(value,env)
                return {**value,'promptRunning':unit['active'],'status':'awaiting-password' if unit['active'] else 'pending'}
            except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
                return {'schema':'decrypt.first-start-display.v1','status':'unavailable','complete':False,'promptRunning':False,
                        'nativeAcceptanceEvidence':False,'message':'First-start setup is not ready. Sign in locally after account setup, then refresh.'}
    def perform(self,action,data):
        with self.lock:
            need(type(data) is dict,'invalid_fields','This action accepts only the displayed notice binding.',400)
            value,uid=self.projection()
            if action=='accept':
                need(set(data)=={'accepted','policySha256','documentSha256','instanceId'} and data['accepted'] is True
                     and all(data[k]==value[k] for k in ('policySha256','documentSha256','instanceId')),
                     'notice_changed','Read and explicitly accept the current notice before opening its password prompt.',409)
            elif action=='cancel':
                need(set(data)=={'policySha256','documentSha256','instanceId'} and all(data[k]==value[k] for k in data),
                     'notice_changed','Refresh this notice before closing its prompt.',409)
            else:raise FirstStartError('unknown_action','This first-start action is not available.',404)
            if value['complete']:return {**value,'promptRunning':False}
            env=self.gui(uid);unit=self.unit(value,env)
            if action=='cancel':
                if unit['active']:
                    rc,_=self.run(['/usr/bin/systemctl','--user','stop',UNIT],env,timeout=5);need(rc==0,'prompt_close_failed','Close the password window locally, then refresh.')
                return self.status()
            if unit['active']:return {**value,'status':'awaiting-password','promptRunning':True}
            argv=['/usr/bin/systemd-run','--user','--quiet','--collect','--unit='+UNIT,
                  '--description='+description(value),'--property=RuntimeMaxSec=180','--property=TimeoutStopSec=3',
                  '--property=KillMode=control-group','--property=NoNewPrivileges=no','--property=StandardOutput=null',
                  '--property=StandardError=null',*['--setenv='+k+'='+v for k,v in env.items()],*terminal_argv(value)]
            rc,_=self.run(argv,env,timeout=4);need(rc==0,'terminal_unavailable','The password window could not open. Your notice is still pending; try again from the local desktop.')
            need(self.unit(value,env)['active'],'terminal_unavailable','The password window did not stay open. Your notice is still pending.')
            return {**value,'status':'awaiting-password','promptRunning':True}
    def admitted(self):return self.status().get('complete') is True

def terminal(policy,document,instance):
    need(os.geteuid()!=0 and sys.stdin.isatty() and sys.stdout.isatty(),'local_terminal_required','Use the local password window.')
    service=FirstStart();value,_=service.projection()
    need(not value['complete'] and (policy,document,instance)==tuple(value[k] for k in ('policySha256','documentSha256','instanceId')))
    raw=secure_read(CALLBACK);need(digest(raw)==CALLBACK_SHA256)
    module=types.ModuleType('_fixed_notice_callback');exec(compile(raw,str(CALLBACK),'exec'),module.__dict__)
    print('Confirm the notice you accepted in Decrypt OS. Enter your local account password when sudo asks.\nNo account connection or trade is authorized.',flush=True)
    module.firstboot_accept(policy,document,instance,explicitly_accepted=True)
    print('Notice accepted. You can return to Decrypt OS.',flush=True)

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__,allow_abbrev=False);parser.add_argument('action',choices=('terminal',))
    for name in ('policy-sha256','document-sha256','instance-id'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args(argv)
    try:terminal(args.policy_sha256,args.document_sha256,args.instance_id);return 0
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
        print('Notice acceptance could not be confirmed. Close this window and refresh Decrypt OS; do not assume it was saved.',file=sys.stderr);return 1
if __name__=='__main__':raise SystemExit(main())
