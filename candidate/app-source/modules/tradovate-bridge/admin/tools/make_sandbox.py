#!/usr/bin/env python3
"""Build tvbridge-admin/sandbox/ from the installed config tree, read-only on the installed side.

Copies configs only: no env files, tokens, journals, databases or browser profiles. Absolute live paths
inside the copies are rewritten to the sandbox. Active kill-switch files are copied so pause states
match. The bridge package is copied too, so validators are imported from the sandbox, never the live
release. Writes sandbox/profile.json (mode sandbox, simulated restarts, dry-run orders).

    python3 make_sandbox.py [--reset] [--tunnel-config PATH]
"""
import argparse
import glob
import json
import os
import re
import shutil
import sys

HOME = os.path.expanduser('~')
LIVE_TB = os.path.join(HOME, '.local/share/decrypt-os/tvbridge')
LIVE_SOUP = os.path.join(HOME, '.local/share/decrypt-os/soup')
ADMIN = os.path.join(HOME, '.local/share/decrypt-os/tvbridge-admin')
SB = os.path.join(ADMIN, 'sandbox')
SOUP_CONTRACT = '/usr/local/lib/decrypt-os/app/trading-runtime/decrypt_trading/vendor/soup_r4/settings_contract.json'
TREE_TB = os.path.join(SB, 'tree/tvbridge')
TREE_SOUP = os.path.join(SB, 'tree/soup-state')
CONFIGS = ['config.json', 'firing.json', 'logins.json']
SOUP_FILES = ['pairing.json', 'quantity-policy.json', 'bridge-binding.json', 'control.json']
SECRET_RE = re.compile(r'(?i)"[^"]*(secret|password|passwd|bearer)[^"]*"\s*:\s*"[^"]+"')


def rewrite(text):
    return text.replace(LIVE_TB, TREE_TB).replace(LIVE_SOUP, TREE_SOUP)


def copy_json(src, dst):
    with open(src) as f:
        text = f.read()
    json.loads(text)
    if SECRET_RE.search(text):
        sys.exit(f'refusing: {src} looks like it holds a secret value')
    os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
    with open(dst, 'w') as f:
        f.write(rewrite(text))
    os.chmod(dst, 0o600)


def soup_contract():
    """The settings contract of the Soup engine the app ships, or None when it is not installed."""
    return SOUP_CONTRACT if os.path.exists(SOUP_CONTRACT) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--reset', action='store_true', help='delete and rebuild sandbox/ (admin state included)')
    ap.add_argument('--tunnel-config', default=None,
                    help='optional tunnel config.yml to copy (credentials lines dropped); default host webhooks.example.com')
    args = ap.parse_args()
    if os.path.exists(SB):
        if not args.reset:
            sys.exit('sandbox exists; use --reset to rebuild it')
        assert os.path.realpath(SB).startswith(os.path.realpath(ADMIN) + os.sep)
        shutil.rmtree(SB)
    os.makedirs(SB, mode=0o700)
    for name in CONFIGS:
        copy_json(os.path.join(LIVE_TB, name), os.path.join(TREE_TB, name))
    with open(os.path.join(LIVE_TB, 'logins.json')) as f:
        logins = json.load(f)['logins']
    configs = []
    for lg in logins:
        src = lg['config']
        if not src.startswith(LIVE_TB + '/'):
            sys.exit('login config outside the tvbridge tree: ' + src)
        dst = rewrite(src)
        if src != os.path.join(LIVE_TB, 'config.json'):
            copy_json(src, dst)
        with open(dst) as f:
            configs.append(json.load(f))
    for name in SOUP_FILES:
        if os.path.exists(os.path.join(LIVE_SOUP, name)):
            copy_json(os.path.join(LIVE_SOUP, name), os.path.join(TREE_SOUP, name))
    # journal dirs, firing lanes and active kill switches the configs point at
    with open(os.path.join(TREE_TB, 'firing.json')) as f:
        firing = json.load(f)
    dirs, kills = [], []
    for cfg in configs:
        for e in [cfg] + (cfg.get('followers') or []):
            if e.get('journal_dir'):
                dirs.append(e['journal_dir'])
            if e.get('kill_switch_file'):
                kills.append(e['kill_switch_file'])
        if cfg.get('master_kill_switch_file'):
            kills.append(cfg['master_kill_switch_file'])
    dirs += [os.path.join(firing['firing_dir'], k) for k in firing['firing_order'][1:]]
    for d in dirs:
        assert d.startswith(SB + '/'), d
        os.makedirs(d, mode=0o700, exist_ok=True)
    copied = []
    for k in set(kills):
        live = k.replace(TREE_TB, LIVE_TB)
        if os.path.isfile(live):
            shutil.copy2(live, k)
            copied.append(os.path.basename(k))
    for live in glob.glob(os.path.join(LIVE_SOUP, 'ENTRIES-BLOCKED*')):
        if '.removed-' not in live:
            shutil.copy2(live, os.path.join(TREE_SOUP, os.path.basename(live)))
            copied.append(os.path.basename(live))
    # tunnel ingress (no credentials line)
    os.makedirs(os.path.join(SB, 'tree/tunnel'), mode=0o700, exist_ok=True)
    host = 'webhooks.example.com'
    if args.tunnel_config:
        with open(args.tunnel_config) as f:
            lines = [ln for ln in f if not ln.strip().startswith(('credentials-file', 'tunnel:'))]
        with open(os.path.join(SB, 'tree/tunnel/config.yml'), 'w') as f:
            f.writelines(lines)
        m = re.search(r'hostname:\s*(\S+)', ''.join(lines))
        host = m.group(1) if m else host
    # bridge validators, copied so nothing is imported (or byte-compiled) from the live release
    live_pkg = os.path.join(os.path.realpath(os.path.join(LIVE_TB, 'current')), 'tvbridge')
    shutil.copytree(live_pkg, os.path.join(SB, 'bridge-release/tvbridge'),
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    # untouched snapshot of the sandbox tree, so a sandbox session can be reset to its starting state.
    # (The unit tests do not use it: they run on the hand-written template in tests/fixture/pristine-tree.)
    shutil.copytree(os.path.join(SB, 'tree'), os.path.join(SB, 'pristine-tree'), symlinks=True)
    contract = soup_contract()
    build = 'soup-r4' if contract else None
    inputs = 0
    if contract:
        with open(contract) as f:
            inputs = len(json.load(f).get('inputs', {}))
    profile = {
        'mode': 'sandbox', 'port': 8781, 'user': os.environ.get('USER', 'trader'), 'public_host': host,
        'tvbridge_root': TREE_TB, 'soup_state': TREE_SOUP, 'admin_state': os.path.join(SB, 'admin-state'),
        'tunnel_config': os.path.join(SB, 'tree/tunnel/config.yml'),
        'bridge_release': os.path.join(SB, 'bridge-release'),
        'status_url': 'http://127.0.0.1:8779/api/status', 'status_ttl_s': 2.0, 'wait_poll_s': 5,
        'restarter': 'simulated', 'order_adapter': 'dry-run',
        'strategies': [{'id': 'soup-pine-r4', 'name': 'Soup Pine r4', 'build': build, 'contract_ref': contract,
                        'inputs_count': inputs, 'stream_field': 'stream_id',
                        'notes': 'In TradingView, paste the stream ID into "📡 Alert stream ID" '
                                 '(group "🔌 Soup v3 connection").'}],
    }
    with open(os.path.join(SB, 'profile.json'), 'w') as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)
    os.chmod(os.path.join(SB, 'profile.json'), 0o600)
    print(json.dumps({'sandbox': SB, 'configs': len(configs), 'dirs': len(dirs), 'switches_copied': copied,
                      'soup_build': build, 'inputs': inputs, 'host': host}, indent=1))


if __name__ == '__main__':
    main()
