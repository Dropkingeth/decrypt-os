#!/usr/bin/env python3
"""Verify source, render a clean customer rootfs recipe, or inspect a target VM.

Apply installs only on an explicitly identified fresh target. Services never start here.
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import pwd
import re
import socket
import stat
import tempfile
import subprocess
import sys

INSTALL = '/usr/local/lib/decrypt-os'
RUNTIME = '/usr/local/lib/decrypt-os-runtime'
# Image architecture. build/target_app.py renders this recipe inside the new
# image, so platform.machine() is the image's own; anything other than x86_64
# keeps the original ARM64 recipe byte for byte.
TARGET_ARCH = 'x86_64' if platform.machine() == 'x86_64' else 'aarch64'
NODE_PLATFORM = {'aarch64': 'linux-arm64', 'x86_64': 'linux-x64'}[TARGET_ARCH]
NODE = RUNTIME + '/node-v24.19.0-' + NODE_PLATFORM
# The optional Claude Code sign-in helper (app/harness/bootstrap.py) pins the
# native Linux ARM64 package; an x86_64 image gets the same release's x64
# package (registry.npmjs.org metadata for 2.1.287, sha512 "integrity").
CLAUDE_CODE_X64 = (
    ('CLAUDE_CODE_PACKAGE = "@anthropic-ai/claude-code-linux-arm64"',
     'CLAUDE_CODE_PACKAGE = "@anthropic-ai/claude-code-linux-x64"'),
    ('CLAUDE_CODE_TARBALL = ("https://registry.npmjs.org/@anthropic-ai/claude-code-linux-arm64/-/"\n'
     '                       "claude-code-linux-arm64-2.1.287.tgz")',
     'CLAUDE_CODE_TARBALL = ("https://registry.npmjs.org/@anthropic-ai/claude-code-linux-x64/-/"\n'
     '                       "claude-code-linux-x64-2.1.287.tgz")'),
    ('CLAUDE_CODE_INTEGRITY = ("sha512-MU3ReK1kjstDmjEOzhFZaphh2AVUKnPaik6lOV0WLs3/"\n'
     '                         "mOM7mE/mqkdcQ/Sv0KwHH5MnOhSkIjCEl0/8tr+vRg==")',
     'CLAUDE_CODE_INTEGRITY = ("sha512-Q31ekN28viVjgfDuZKXUR5vxIBgUYoLGe+cRmjx/"\n'
     '                         "f01omqFDMi6IrgmcK5pWMnRbu/cjsTA/zO+bVES5oY3XUg==")'),
    ('"cpu": ["arm64"]', '"cpu": ["x64"]'),
)
PYTHON = RUNTIME + '/python/bin/python'
PRIVATE_HOST_BINDING = Path('/etc/decrypt-host/protected-machines.json')
# Existing assistant/runtime state that a fresh customer account never has.
ASSISTANT_STATE = ('.openclaw', '.local/state/decrypt-os')
ACTIONS = ('prepare', 'status', 'start', 'stop', 'verify', 'login', 'console', 'ict', 'storage_setup', 'storage_unlock')
HARNESS = ('runtime.py', 'bootstrap.py', 'namespace_entry.py', 'vault_control.py', 'vault_storage.py', 'README.md', 'RUNTIME-MANIFEST.json')
BACKEND = ('server.py', 'first_start.py', 'harness_bridge.py', 'system_scope.py', 'strategy_access.py', 'engine_workspace.py', 'guardian_auth.py', 'guardian_browser.py', 'guardian_service.py', 'guardian_keyring.py', 'guardian_login_browser.py', 'tradovate_service.py', 'maintenance.py', 'maintenance_service.py', 'trading_service.py', 'trading_routes.py', 'trading_ingress_secrets.py', 'trading_browser.py', 'trading_feed.py', 'trading_calendar.py', 'customer_health.py', 'requirements.txt')
CONTEXT = ('AGENTS.md', 'SYSTEM.md', 'SYSTEM-SCOPE.md',
    'skills/decrypt-unslop/SKILL.md', 'skills/decrypt-unslop/references/LICENSE.txt', 'skills/decrypt-unslop/references/provenance.md',
    'skills/decrypt-frontend-design/SKILL.md', 'skills/decrypt-frontend-design/references/LICENSE.txt', 'skills/decrypt-frontend-design/references/provenance.md',
    'skills/review-skill-imports/SKILL.md')
ICT_ASSISTANT = ('README.md', 'openclaw-agent.json', 'ict_clock.py', 'launcher.py', 'web_reader_proxy.py', 'desktop/decrypt-ict-assistant.desktop',
    'workspace/AGENTS.md', 'workspace/SOUL.md', 'workspace/IDENTITY.md',
    'knowledge/01-foundations.md', 'knowledge/02-liquidity-and-structure.md', 'knowledge/03-pd-arrays.md',
    'knowledge/04-time-and-sessions.md', 'knowledge/05-models.md', 'knowledge/06-bias-and-smt.md',
    'knowledge/07-glossary.md', 'knowledge/08-sources.md')
COPY_FILES = tuple('app/ict-assistant/' + n for n in ICT_ASSISTANT) + tuple('app/backend/' + n for n in BACKEND) + tuple('app/harness/' + n for n in HARNESS) + tuple('app/customer-workspace/' + n for n in CONTEXT) + (
    'app/system-scope.json', 'app/strategy-catalog.json', 'app/strategy-access.json', 'app/contract-calendar.json', 'app/engine-module-catalog.json',
    'installer/customer_control.py', 'installer/frontend.py', 'installer/desktop_apps.py', 'installer/desktop_layout_restore.py', 'installer/market_browser.py', 'installer/install_notice.py', 'installer/version_bootstrap.py', 'installer/maintenance_cli.py', 'installer/customer_ingress.py', 'installer/loopback_guard.py', 'installer/tmp_janitor.py', 'installer/INGRESS.md', 'installer/release-trust.json', 'installer/MAINTENANCE.md',
    'customer-docs/GETTING-STARTED.md', 'customer-docs/TEST-CHECKLIST.md', 'customer-docs/UI-THIRD-PARTY-NOTICES.txt', 'app/ui/licenses/UI-THIRD-PARTY-NOTICES.txt')
TRADING_FILES = ('app/trading-runtime/Caddyfile.example', 'app/trading-runtime/MANIFEST.json', 'app/trading-runtime/README.md', 'app/trading-runtime/decrypt_trading/__init__.py', 'app/trading-runtime/decrypt_trading/http_ingress.py', 'app/trading-runtime/decrypt_trading/ingress.py', 'app/trading-runtime/decrypt_trading/native_chart_contract.py', 'app/trading-runtime/decrypt_trading/native_chart_hook.js', 'app/trading-runtime/decrypt_trading/native_market.py', 'app/trading-runtime/decrypt_trading/native_market_store.py', 'app/trading-runtime/decrypt_trading/retirement.py', 'app/trading-runtime/decrypt_trading/runtime.py', 'app/trading-runtime/decrypt_trading/source.py', 'app/trading-runtime/decrypt_trading/vendor/soup_r4/core.py', 'app/trading-runtime/decrypt_trading/vendor/soup_r4/settings_contract.json', 'app/trading-runtime/decrypt_trading/vendor/soup_r4/storage.py', 'app/trading-runtime/decrypt_trading/vendor/soup_r4/transport.py')
TRADING_FILES += ('app/trading-runtime/decrypt_trading/customer_contract.py',
                  'app/trading-runtime/decrypt_trading/paths.py',
                  'app/trading-runtime/decrypt_trading/bridge_route.py',
                  'app/trading-runtime/decrypt_trading/bridge_transport.py',
                  'app/trading-runtime/decrypt_trading/bridge_adapters.py')
# decrypt-bridge-v2 (beta4): shared receiver/executor, validator and strategy registry. Strategy modules
# (bridge_v2_<name>.py) are added here by their strategy, one line each.
TRADING_FILES += ('app/trading-runtime/decrypt_trading/bridge_v2.py',
                  'app/trading-runtime/decrypt_trading/bridge_v2_contract.py',
                  'app/trading-runtime/decrypt_trading/bridge_v2_registry.py')
TRADING_FILES += ('app/trading-runtime/decrypt_trading/bridge_v2_fvg_r5b.py',)   # FVG/IFVG v2 strategy (beta4)
TRADING_FILES += ('app/trading-runtime/decrypt_trading/bridge_v2_lumi.py',)   # Lumi RTH Gap v2 (beta4)
TRADING_FILES += ('app/trading-runtime/decrypt_trading/bridge_feed.py',)   # the bridge's 1-second feed reader (Lumi, beta4)
# OTE Refined Large + Small (beta4): decrypt-bridge-v2 strategy modules.
TRADING_FILES += ('app/trading-runtime/decrypt_trading/bridge_v2_ote_refined_large.py',
                  'app/trading-runtime/decrypt_trading/bridge_v2_ote_refined_small.py')
TRADING_FILES += ('app/trading-runtime/decrypt_trading/bridge_v2_ote_active.py',)   # OTE Active (Silver Bullet) v2, beta4
TRADING_FILES += ('app/trading-runtime/decrypt_trading/bridge_v2_order_blocks.py',)   # Order Blocks Decoded (beta4)
# The Tradovate bridge module ships exactly what its runtime-files.txt lists (see tradovate_files()).
TRADOVATE_LIST = 'modules/tradovate-bridge/runtime-files.txt'
COPY_FILES += TRADING_FILES
DEPENDENCIES = ('python3', 'systemd-run', 'systemctl', 'sudo', 'nsenter', 'setpriv', 'findmnt', 'foot', 'hyprctl',
                'cryptsetup', 'losetup', 'mount', 'umount', 'mkfs.ext4', 'blkid', 'useradd', 'visudo', 'xdg-open', 'chromium', 'openssl', 'notify-send')

# Same bytes as app/ict-assistant/desktop/decrypt-ict-assistant.desktop (checked by tests).
ICT_DESKTOP = b'[Desktop Entry]\nType=Application\nName=ICT Assistant\nComment=Your personal ICT mentor, powered by your own Claude account. Education only; it cannot trade.\nExec=/usr/bin/foot --app-id=decrypt-os-ict-assistant --title=ICT\\sAssistant /usr/local/bin/decrypt-ict-assistant\nTerminal=false\nCategories=Office;Finance;Education;\nStartupNotify=true\nStartupWMClass=decrypt-os-ict-assistant\n'


class RecipeError(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise RecipeError('Invalid source path.')
    parts = value.split('/')
    if any(not p or p in ('.', '..') for p in parts) or PurePosixPath(value).is_absolute():
        raise RecipeError('Invalid source path.')
    return value


def regular(path, root, maximum=32 * 1024 * 1024):
    """Check parents before opening. Never resolve a source symlink into trust."""
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink():
            raise RecipeError('Linked source files are not allowed.')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
        raise RecipeError('Source must be a bounded regular file with one link.')
    return path.read_bytes()


def verify_bundle(bundle):
    bundle = Path(bundle).absolute()
    if bundle.is_symlink() or not bundle.is_dir():
        raise RecipeError('Use a real extracted source directory.')
    manifest_data = regular(bundle / 'SOURCE-MANIFEST.json', bundle, 2 * 1024 * 1024)
    manifest = json.loads(manifest_data)
    if manifest.get('schema') != 'decrypt.os.source-bundle.v1' or manifest.get('artifact_type') != 'app_source_not_os_image':
        raise RecipeError('This is not a supported source bundle.')
    rows = manifest.get('files')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2000:
        raise RecipeError('Source manifest is incomplete.')
    files = {}
    for row in rows:
        name = safe_name(row.get('path'))
        if name in files or not re.fullmatch('[0-9a-f]{64}', str(row.get('sha256', ''))):
            raise RecipeError('Duplicate or invalid source record.')
        data = regular(bundle / name, bundle)
        if type(row.get('bytes')) is not int or len(data) != row['bytes'] or digest(data) != row['sha256']:
            raise RecipeError('Source does not match its manifest.')
        files[name] = data
    missing = set(COPY_FILES) - files.keys()
    if missing:
        raise RecipeError('Customer recipe inputs are missing: ' + ', '.join(sorted(missing)))
    return files, digest(manifest_data)


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise RecipeError('Runtime source changed; review the installation transform.')
    return text.replace(old, new, 1)


def adapt_runtime(data):
    text = data.decode()
    text = replace_once(text, 'pathlib.Path("/home/customer/.local/lib/node_modules/openclaw")',
                        'pathlib.Path("' + RUNTIME + '/openclaw")')
    text = replace_once(text, 'pathlib.Path("/home/customer/.local/opt/node-v24.19.0-linux-arm64")',
                        'pathlib.Path("' + NODE + '")')
    # Keep the tested runtime itself intact; root ownership replaces developer paths.
    ast.parse(text)
    return text.encode()


def adapt_bridge(data):
    """harness_bridge.py ships unchanged: on an installed system it finds the fixed helper
    (installer/customer_control.py) itself, reads the status the helper publishes and opens every
    privileged step in a terminal where sudo asks for the password. The UI never acquires root
    authority silently. The checks below pin what the installed copy relies on."""
    text = data.decode()
    for needed in ("INSTALLED_CONTROL = Path('" + INSTALL + "/installer/customer_control.py')",
                   "['/usr/bin/sudo', PYTHON, '-B', '-I', str(INSTALLED_CONTROL), verb]",
                   # The pinned Omarchy guest uses Lua dispatch: the fixed focus command and its
                   # active-workspace readback stay as tested.
                   "[HYPRCTL, 'dispatch', 'hl.dsp.focus({workspace=\"4\"})']"):
        if needed not in text:
            raise RecipeError('harness_bridge.py no longer matches the installed helper contract.')
    if 'sudo -n' in text or "'-n'" in text:
        raise RecipeError('The app must not run sudo non-interactively.')
    ast.parse(text)
    return text.encode()


def adapt_bootstrap(data, arch=None):
    text = replace_once(data.decode(), 'if pathlib.Path("/home/customer/.openclaw").exists():',
                        'if os.access("/home", os.R_OK | os.X_OK) or os.access("/root", os.R_OK | os.X_OK):')
    if (arch or TARGET_ARCH) == 'x86_64':
        for old, new in CLAUDE_CODE_X64:
            text = replace_once(text, old, new)
    ast.parse(text)
    return text.encode()


def user_name(user):
    if not isinstance(user, str) or not re.fullmatch('[a-z_][a-z0-9_-]{0,31}', user) or user == 'root':
        raise RecipeError('Use a non-root local desktop account name.')
    return user


def generated_files(user=None):
    if user is not None:
        user = user_name(user)
    control = INSTALL + '/installer/customer_control.py'
    sudoers = '# Fixed customer operations only. No wildcards or arbitrary commands.\n'
    if user is not None:
        sudoers += '\n'.join(user + ' ALL=(root) PASSWD: /usr/bin/python3 -B -I ' + control + ' ' + action for action in ACTIONS) + '\n'
    # User units must not use mount-namespace settings (ProtectSystem, ProtectHome,
    # PrivateTmp, ReadWritePaths...). In a user manager they imply a private user
    # namespace where root-owned files appear as nobody, which fails every
    # installed-root trust check. Installed files stay root-owned and read-only.
    api = f'''[Unit]
Description=Decrypt OS local API
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=exec
ExecStart={PYTHON} -E -s -B {INSTALL}/installer/version_bootstrap.py --component api --state-dir %h/.local/state/decrypt-os --enable-harness --enable-guardian --enable-trading
WorkingDirectory={INSTALL}
Environment=PATH=/usr/bin:/bin
UMask=0077
LimitCORE=0
NoNewPrivileges=yes
RestrictSUIDSGID=yes
Restart=on-failure
RestartSec=3
StandardOutput=null
StandardError=journal

[Install]
WantedBy=graphical-session.target
'''
    ui = f'''[Unit]
Description=Decrypt OS local interface
PartOf=graphical-session.target
After=decrypt-os-api.service
Requires=decrypt-os-api.service

[Service]
Type=exec
ExecStart={PYTHON} -E -s -B {INSTALL}/installer/version_bootstrap.py --component ui --state-dir %h/.local/state/decrypt-os
WorkingDirectory={INSTALL}
Environment=PATH=/usr/bin:/bin
UMask=0077
LimitCORE=0
NoNewPrivileges=yes
RestrictSUIDSGID=yes
Restart=on-failure
RestartSec=3
StandardOutput=null
StandardError=journal

[Install]
WantedBy=graphical-session.target
'''
    market = f'''[Unit]
Description=Decrypt OS dedicated market browser
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=exec
ExecStart=/usr/bin/python3 -I -B {INSTALL}/installer/market_browser.py
Environment=PATH=/usr/bin:/bin
UMask=0077
LimitCORE=0
NoNewPrivileges=yes
RestrictSUIDSGID=yes
Restart=on-failure
RestartSec=5
StandardOutput=null
StandardError=null
'''
    janitor_service = f'''[Unit]
Description=Keep /tmp from filling up for this account

[Service]
Type=oneshot
Nice=10
IOSchedulingClass=idle
Environment=TMPDIR=%h/.cache
ExecStart=/usr/bin/python3 -I -B {INSTALL}/installer/tmp_janitor.py --apply
NoNewPrivileges=yes
RestrictSUIDSGID=yes
UMask=0077
LimitCORE=0
'''
    janitor_timer = '''[Unit]
Description=Hourly /tmp janitor

[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
RandomizedDelaySec=2min
Persistent=true

[Install]
WantedBy=timers.target
'''
    layout_service = f'''[Unit]
Description=Restore missing Decrypt desktop views without stealing focus
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I -B {INSTALL}/installer/desktop_layout_restore.py
TimeoutStartSec=30
NoNewPrivileges=yes
RestrictSUIDSGID=yes
UMask=0077
LimitCORE=0
'''
    layout_timer = '''[Unit]
Description=Check for missing Decrypt desktop views
PartOf=graphical-session.target

[Timer]
OnActiveSec=10s
OnUnitActiveSec=20s
AccuracySec=2s

[Install]
WantedBy=graphical-session.target
'''
    # System unit: per-UID nftables rules keep the assistant sandbox and the HTTPS
    # ingress off trading, bridge, app and DevTools ports (installer/loopback_guard.py).
    # Enabled through a multi-user.target drop-in so the rootfs needs no symlinks.
    loopback_guard = f'''[Unit]
Description=Decrypt OS loopback guard for trading and browser-debugging ports
DefaultDependencies=no
After=local-fs.target
Wants=network-pre.target
Before=network-pre.target systemd-user-sessions.service display-manager.service shutdown.target
Conflicts=shutdown.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 -I -B {INSTALL}/installer/loopback_guard.py apply
ExecReload=/usr/bin/python3 -I -B {INSTALL}/installer/loopback_guard.py apply
NoNewPrivileges=yes
StandardOutput=null
StandardError=journal
'''
    loopback_wants = '[Unit]\nWants=decrypt-os-loopback-guard.service\n'
    # This launcher only uses user services. A later privileged install must create
    # the empty owner state directory before these units can start.
    def app_launcher(app):
        return ('#!/usr/bin/python3 -I\nimport runpy, sys\n'
                + 'sys.argv = [' + repr(INSTALL + '/installer/desktop_apps.py') + ', ' + repr(app) + ']\n'
                + 'runpy.run_path(sys.argv[0], run_name="__main__")\n').encode()
    desktop = '''[Desktop Entry]
Type=Application
Name=Decrypt OS
Comment=Set up your personal assistant
Exec=/usr/local/bin/decrypt-os
Terminal=false
Categories=Utility;
StartupNotify=true
StartupWMClass=decrypt-os
MimeType=x-scheme-handler/decrypt-os;
'''
    # decrypt-os:// (the Bridge window's "Open Decrypt OS" link) and decrypt-advanced:// (its gear)
    # open the app windows on workspaces 1 and 5. A system-wide default so no desktop database
    # refresh is needed; the user's own mimeapps.list can still override it.
    mimeapps = ('[Default Applications]\nx-scheme-handler/decrypt-os=decrypt-os.desktop\n'
                'x-scheme-handler/decrypt-advanced=decrypt-bridge-advanced.desktop\n')
    # One generic .desktop/launcher pair per workspace app; the WS1-WS5 layout
    # (desktop_apps.py) matches windows by these StartupWMClass values.
    launchers = {
        'trading-pipeline': ('decrypt-trading-pipeline', 'Trading Pipeline',
            'View your trading pipeline status. Opening this window does not place or change any order.',
            'Office;Finance;', 'decrypt-trading-pipeline'),
        'tradingview': ('decrypt-tradingview', 'TradingView (web app)',
            'Open your own TradingView chart. You sign in yourself; Decrypt OS never stores or automates this login.',
            'Office;Finance;', 'decrypt-tradingview-chart'),
        'tradovate-bridge': ('decrypt-tradovate-bridge', 'Tradovate Bridge',
            'View bridge status. Opening this window does not enable trading.',
            'Office;Finance;', 'decrypt-tradovate-status'),
    }
    result = {
        'etc/systemd/user/decrypt-os-api.service': (api.encode(), 0o644),
        'etc/systemd/user/decrypt-os-ui.service': (ui.encode(), 0o644),
        'etc/systemd/user/decrypt-os-market-browser.service': (market.encode(), 0o644),
        'etc/systemd/user/decrypt-tmp-janitor.service': (janitor_service.encode(), 0o644),
        'etc/systemd/user/decrypt-tmp-janitor.timer': (janitor_timer.encode(), 0o644),
        'etc/systemd/user/decrypt-desktop-layout.service': (layout_service.encode(), 0o644),
        'etc/systemd/user/decrypt-desktop-layout.timer': (layout_timer.encode(), 0o644),
        'usr/local/bin/decrypt-os': (app_launcher('decrypt'), 0o755),
        # ICT assistant launcher for workspace 4 (desktop layout places the window).
        'usr/local/bin/decrypt-ict-assistant': (('#!/usr/bin/python3 -I\nimport runpy, sys\n'
            + 'sys.argv = [' + repr(INSTALL + '/app/ict-assistant/launcher.py') + ']\n'
            + 'runpy.run_path(sys.argv[0], run_name="__main__")\n').encode(), 0o755),
        'usr/share/applications/decrypt-ict-assistant.desktop': (ICT_DESKTOP, 0o644),
        'etc/systemd/user-preset/00-decrypt-tradovate-default-off.preset': (b'# The Tradovate bridge ships off. Its setup.py enable turns it on for your own account.\ndisable tvbridge-*.service\ndisable tvbridge-*.timer\ndisable tvbridge-*@*.service\ndisable tvbridge-*@*.timer\n', 0o644),
        'usr/share/applications/decrypt-os.desktop': (desktop.encode(), 0o644),
        'etc/xdg/mimeapps.list': (mimeapps.encode(), 0o644),
        'etc/systemd/system/decrypt-os-loopback-guard.service': (loopback_guard.encode(), 0o644),
        'etc/systemd/system/multi-user.target.d/decrypt-os-loopback-guard.conf': (loopback_wants.encode(), 0o644),
    }
    for app, (binary, name, comment, categories, wmclass) in launchers.items():
        result['usr/local/bin/' + binary] = (app_launcher(app), 0o755)
        entry = (f'[Desktop Entry]\nType=Application\nName={name}\nComment={comment}\n'
                 f'Exec=/usr/local/bin/{binary}\nTerminal=false\nCategories={categories}\n'
                 f'StartupNotify=true\nStartupWMClass={wmclass}\n')
        result['usr/share/applications/' + binary + '.desktop'] = (entry.encode(), 0o644)
    if user is not None:
        result['etc/sudoers.d/decrypt-os-customer'] = (sudoers.encode(), 0o440)
    return result


def tradovate_files(files):
    """The bridge module, its user units (default off: no wants-links, no state, no account) and its
    desktop entry. Scripts in bin/ are executable; everything else is read-only data."""
    if TRADOVATE_LIST not in files:
        raise RecipeError('The Tradovate bridge file list is missing from the bundle.')
    writes = {}
    for rel in files[TRADOVATE_LIST].decode().split():
        name = 'modules/tradovate-bridge/' + rel
        safe_name(name)
        if name not in files:
            raise RecipeError('A listed Tradovate bridge file is missing from the bundle.')
        data = files[name]
        writes[INSTALL.lstrip('/') + '/' + name] = (data, 0o755 if rel.startswith('bin/') else 0o644)
        if rel.startswith('systemd/'):
            if b'/opt/decrypt-os' in data or b'ExecStartPre' in data:
                raise RecipeError('Bridge units must use the installed root and one ExecStart.')
            writes['etc/systemd/user/' + rel.split('/', 1)[1]] = (data, 0o644)
        elif rel.startswith('desktop/') and rel.endswith('.desktop'):
            writes['usr/share/applications/' + rel.split('/', 1)[1]] = (data, 0o644)
    return writes


def ui_files(bundle):
    root = Path(bundle).absolute() / 'app/ui/dist'
    if root.is_symlink() or not (root / 'index.html').is_file():
        raise RecipeError('Build app/ui from the verified lockfile as a non-root user before rendering.')
    result = {}
    for item in sorted(root.rglob('*')):
        if item.is_symlink():
            raise RecipeError('Linked UI output is not allowed.')
        if item.is_dir():
            continue
        name = item.relative_to(root).as_posix()
        safe_name(name)
        if item.suffix.lower() not in ('.html', '.js', '.css', '.png', '.jpg', '.jpeg', '.svg', '.webp', '.woff', '.woff2', '.txt'):
            raise RecipeError('Unexpected UI build output.')
        result[name] = regular(item, root)
    if len(result) > 500 or sum(map(len, result.values())) > 64 * 1024 * 1024:
        raise RecipeError('UI output exceeded the recipe limits.')
    return result


def render(bundle, output, user=None):
    files, source_hash = verify_bundle(bundle)
    if user is not None:
        user_name(user)
    built_ui = ui_files(bundle)
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise RecipeError('Choose a new staging directory. Existing files are never overwritten.')
    if not output.parent.is_dir() or any(p.is_symlink() for p in [output.parent, *output.parent.parents]):
        raise RecipeError('Use an existing real staging parent directory.')
    writes = {INSTALL.lstrip('/') + '/' + name: (files[name], 0o644) for name in COPY_FILES}
    writes[INSTALL.lstrip('/') + '/release-trust.json'] = (files['installer/release-trust.json'], 0o644)
    writes[INSTALL.lstrip('/') + '/app/harness/runtime.py'] = (adapt_runtime(files['app/harness/runtime.py']), 0o644)
    writes[INSTALL.lstrip('/') + '/app/backend/harness_bridge.py'] = (adapt_bridge(files['app/backend/harness_bridge.py']), 0o644)
    writes[INSTALL.lstrip('/') + '/app/harness/bootstrap.py'] = (adapt_bootstrap(files['app/harness/bootstrap.py']), 0o644)
    for name, data in built_ui.items():
        writes[INSTALL.lstrip('/') + '/app/ui/dist/' + name] = (data, 0o644)
    writes[INSTALL.lstrip('/') + '/SOURCE-MANIFEST.json'] = ((Path(bundle) / 'SOURCE-MANIFEST.json').read_bytes(), 0o644)
    runtime_manifest_path = INSTALL.lstrip('/') + '/app/harness/RUNTIME-MANIFEST.json'
    runtime_manifest = json.loads(files['app/harness/RUNTIME-MANIFEST.json'])
    runtime_manifest['node']['architecture'] = NODE_PLATFORM
    runtime_manifest['sourceFiles'] = {name: digest(writes[INSTALL.lstrip('/') + '/app/harness/' + name][0]) for name in HARNESS if name.endswith('.py')}
    runtime_manifest['qualificationScope'] = 'source-tests-only; not this customer installation'
    runtime_manifest['installerQualified'] = False
    runtime_manifest['installedFromSourceManifestSha256'] = source_hash
    runtime_manifest['limitations'] = ['Customer clean installation and reboot remain unverified', 'No complete dependency/license/security audit', 'Source transformations are recorded in RECIPE-RECEIPT.json']
    writes[runtime_manifest_path] = ((json.dumps(runtime_manifest, sort_keys=True, indent=2) + '\n').encode(), 0o644)
    writes.update(generated_files(user))
    writes.update(tradovate_files(files))
    for name, (data, _) in writes.items():
        if name.endswith('.py') and (b'/home/customer' in data or b'/Users/' in data):
            raise RecipeError('Developer paths remain in installed code.')
        if name.endswith('.py'):
            ast.parse(data)
    output.mkdir(mode=0o700)
    for name, (data, mode) in sorted(writes.items()):
        dest = output / 'rootfs' / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        dest.chmod(mode)
    (output / 'rootfs').chmod(0o755)
    for directory in (output / 'rootfs').rglob('*'):
        if directory.is_dir():
            directory.chmod(0o755)
    receipt = {'schema': 'decrypt.clean-base-recipe.v1', 'stageOnly': True, 'installed': False,
        'sourceManifestSha256': source_hash, 'desktopUser': user,
        'platform': 'arch-linux-' + TARGET_ARCH + '-omarchy-compatibility', 'cleanVmVerified': False,
        'runtimeDependenciesBundled': False, 'sourceAuthenticityVerified': False,
        'files': [{'path': name, 'bytes': len(data), 'mode': oct(mode), 'sha256': digest(data)} for name, (data, mode) in sorted(writes.items())],
        'pending': ['Authenticate the source release and dependency artifacts', 'Provision the pinned public dependencies',
                    'Qualify guarded installation and target enrollment on a fresh guest', 'Test a clean guest and cold boot',
                    'Qualify customer OAuth and encrypted restore on that guest']}
    (output / 'RECIPE-RECEIPT.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


def load_private_host_binding(path=PRIVATE_HOST_BINDING):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        info = item.lstat()
        if item.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise RecipeError('Protected host bindings must be private root-controlled configuration.')
    value = json.loads(regular(path, Path('/'), 16384))
    if set(value) != {'schema', 'protectedMachineSha256'} or value['schema'] != 'decrypt.protected-hosts.v1':
        raise RecipeError('Invalid private host binding.')
    hashes = value['protectedMachineSha256']
    if not isinstance(hashes, list) or not hashes or len(set(hashes)) != len(hashes) or any(not isinstance(h, str) or not re.fullmatch('[0-9a-f]{64}', h) for h in hashes):
        raise RecipeError('Explicit protected machine bindings are required.')
    return tuple(hashes)


def inspect_host(user, host_binding=PRIVATE_HOST_BINDING):
    """Read metadata only. Never inspect account credentials or ask for a password."""
    user = user_name(user)
    facts = {'system': platform.system(), 'architecture': platform.machine(), 'failures': []}
    failed = facts['failures']
    try:
        protected = load_private_host_binding(host_binding)
    except (OSError, ValueError) as error:
        failed.append(str(error))
        protected = ()
    machine = Path('/etc/machine-id')
    if not machine.is_file() or not re.fullmatch('[0-9a-f]{32}', machine.read_text().strip()):
        failed.append('A valid, unique machine identity is required.')
    elif digest(machine.read_text().strip().encode()) in protected:
        failed.append('This machine is protected by its host binding. Use a fresh customer-test VM.')
    if facts['system'] != 'Linux' or facts['architecture'] not in ('aarch64', 'x86_64'):
        failed.append('Only the ARM64 and x86_64 Arch/Omarchy compatibility targets are in this recipe.')
    release = Path('/etc/os-release')
    if not release.is_file() or not re.search(r'^ID=(?:arch|archarm|"arch"|"archarm")$', release.read_text(), re.M):
        failed.append('An Arch Linux base is required.')
    try:
        account = pwd.getpwnam(user)
        home = Path(account.pw_dir)
        if account.pw_uid < 1000 or not home.is_dir() or home.is_symlink() or home.stat().st_uid != account.pw_uid:
            failed.append('A normal local desktop account with its own home is required.')
        if not (home / '.local/share/omarchy').is_dir():
            failed.append('The Omarchy desktop is not installed for this account.')
        for name in ASSISTANT_STATE:
            if (home / name).exists() or (home / name).is_symlink():
                failed.append('Existing assistant or development state was found. Use a fresh account.')
                break
    except KeyError:
        failed.append('The local desktop account does not exist.')
    for name in (INSTALL, '/etc/decrypt-os', '/var/lib/decrypt-os/customer-storage', '/run/decrypt-os-customer-pilot'):
        if Path(name).exists() or Path(name).is_symlink():
            failed.append('An existing customer installation or runtime is present.')
            break
    swaps = Path('/proc/swaps')
    if not swaps.is_file() or len(swaps.read_text().splitlines()) != 1:
        failed.append('Disable swap before testing temporary customer credentials.')
    for name in DEPENDENCIES:
        path = Path('/usr/bin') / name
        if not path.is_file() or not os.access(path, os.X_OK):
            failed.append('Required local utility is missing: ' + name)
    for port in (8810, 8811, 19789):
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', port))
            except OSError:
                failed.append('A required loopback port is occupied: ' + str(port))
    facts['eligibleForInstall'] = not failed
    facts['installationAvailable'] = True
    return facts


def trusted_tree(root):
    root = Path(root)
    for path in (root, *root.parents):
        info = path.lstat()
        if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise RecipeError('Runtime dependencies must have trusted system ownership.')
    for path in root.rglob('*'):
        info = path.lstat()
        if info.st_uid != 0 or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022):
            raise RecipeError('Runtime dependencies must not be user-writable.')
        if path.is_symlink():
            if os.path.isabs(os.readlink(path)) or not path.resolve().is_relative_to(root):
                raise RecipeError('Runtime dependency links must stay inside the package.')
        elif not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise RecipeError('Unexpected runtime dependency file.')


def dependency_status():
    failures = []
    try:
        trusted_tree(RUNTIME + '/openclaw')
        package = json.loads(Path(RUNTIME + '/openclaw/package.json').read_text())
        if package.get('name') != 'openclaw' or package.get('version') != '2026.9.9':
            raise RecipeError('Pinned OpenClaw2026.9.9 is missing.')
        if not Path(RUNTIME + '/openclaw/openclaw.mjs').is_file():
            raise RecipeError('The complete OpenClaw entry point is missing.')
        trusted_tree(NODE)
        result = subprocess.run([NODE + '/bin/node', '--version'], capture_output=True, text=True, check=True, timeout=5,
                                env={'PATH': '/usr/bin:/bin'})
        if result.stdout.strip() != 'v24.19.0':
            raise RecipeError('Pinned Node24.19.0 is missing.')
        # venv must be made with --copies; absolute interpreter symlinks are not accepted.
        trusted_tree(RUNTIME + '/python')
        code = 'import importlib.metadata as m,json;print(json.dumps({n:m.version(n) for n in ("cryptography","cffi","pycparser","SecretStorage","jeepney")}))'
        result = subprocess.run([PYTHON, '-I', '-B', '-c', code], capture_output=True, text=True, check=True, timeout=5,
                                env={'PATH': '/usr/bin:/bin'})
        if json.loads(result.stdout) != {'cryptography': '46.0.5', 'cffi': '2.1.1', 'pycparser': '3.0', 'SecretStorage': '3.3.3', 'jeepney': '0.9.0'}:
            raise RecipeError('Pinned Python dependencies are missing.')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        failures.append(str(exc) if isinstance(exc, RecipeError) else 'Complete pinned runtime dependencies are not installed.')
    return {'ready': not failures, 'failures': failures, 'artifactSignaturesVerified': False}



def check_parent_ownership(path):
    for parent in path.parents:
        if not parent.exists() and not parent.is_symlink():
            continue
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RecipeError('Installation parents must be root-owned real directories.')



def create_customer_state(home, uid, gid):
    """Keep directory handles while creating state; a user cannot redirect root writes."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    handles, created = [], []
    try:
        current = os.open(home, flags)
        handles.append(current)
        info = os.fstat(current)
        if info.st_uid != uid or info.st_mode & 0o022:
            raise RecipeError('The desktop home has unsafe ownership.')
        for name in ('.local', 'state', 'decrypt-os'):
            try:
                os.mkdir(name, mode=0o700, dir_fd=current)
                made = True
                created.append((current, name))
            except FileExistsError:
                made = False
                if name == 'decrypt-os':
                    raise RecipeError('Customer state appeared during installation; nothing was overwritten.')
            child = os.open(name, flags, dir_fd=current)
            handles.append(child)
            if made:
                os.fchown(child, uid, gid)
                os.fchmod(child, 0o700)
            info = os.fstat(child)
            if info.st_uid != uid or info.st_mode & 0o022:
                raise RecipeError('Customer state has unsafe ownership.')
            current = child
        return handles, created
    except BaseException:
        finish_customer_state(handles, created, rollback=True)
        raise


def finish_customer_state(handles, created, rollback=False):
    try:
        if rollback:
            for parent, name in reversed(created):
                # Normal rmdir only: never delete content or follow a replacement link.
                try:
                    os.rmdir(name, dir_fd=parent)
                except OSError:
                    pass
    finally:
        for fd in reversed(handles):
            os.close(fd)


def verify_reviewed_recipe(stage, expected_recipe):
    if not re.fullmatch('[0-9a-f]{64}', expected_recipe or '') or digest((stage / 'RECIPE-RECEIPT.json').read_bytes()) != expected_recipe:
        raise RecipeError('Rendered files do not match the reviewed recipe hash.')


def application_receipt(recipe, machine, acknowledgement):
    """The exact producer contract consumed by guided installed verification."""
    return {**recipe, 'installed': True, 'stageOnly': False,
            'targetMachineSha256': machine, 'servicesStarted': False, 'acknowledgement': acknowledgement}


def apply(bundle, user, expected_source, expected_machine, expected_recipe=None, acknowledgement=None, host_binding=PRIVATE_HOST_BINDING):
    """No shell, overwrite, downloads, service changes, or live account import."""
    if os.geteuid() != 0:
        raise RecipeError('Apply requires root on the fresh customer-test VM.')
    files, source_hash = verify_bundle(bundle)
    if not re.fullmatch('[0-9a-f]{64}', expected_source or '') or source_hash != expected_source:
        raise RecipeError('The reviewed source-manifest hash does not match.')
    # Host guard runs before dependency execution, staging, or any filesystem mutation.
    host = inspect_host(user, host_binding)
    if not host['eligibleForInstall']:
        raise RecipeError('Target preflight failed: ' + ' '.join(host['failures']))
    actual_machine = digest(Path('/etc/machine-id').read_text().strip().encode())
    if not re.fullmatch('[0-9a-f]{64}', expected_machine or '') or actual_machine != expected_machine:
        raise RecipeError('The explicitly selected target machine does not match.')
    if acknowledgement is None:
        raise RecipeError('Accept the versioned installation notice through guided setup before apply.')
    account = pwd.getpwnam(user)
    notice = {'__name__': 'verified_install_notice'}
    exec(compile(files['installer/install_notice.py'], 'verified_install_notice', 'exec'), notice)
    accepted = notice['verify'](acknowledgement, 'runtime-setup',
                               notice['runtime_binding'](expected_machine, expected_recipe), user, {account.pw_uid, 0})
    dependencies = dependency_status()
    if not dependencies['ready']:
        raise RecipeError('Target dependencies failed: ' + ' '.join(dependencies['failures']))
    account = pwd.getpwnam(user)
    state_dir = Path(account.pw_dir) / '.local/state/decrypt-os'
    # State parents are private to the user; reject redirections and shared directories.
    for parent in [state_dir.parent, state_dir.parent.parent, Path(account.pw_dir)]:
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != account.pw_uid or info.st_mode & 0o022:
                raise RecipeError('The customer state directory has unsafe parents.')
    with tempfile.TemporaryDirectory(prefix='decrypt-base-', dir='/var/tmp') as work:
        stage = Path(work) / 'recipe'
        receipt = render(bundle, stage, user)
        verify_reviewed_recipe(stage, expected_recipe)
        enrollment = {'schema': 'decrypt.customer-test.v1', 'role': 'clean-customer-test',
                      'machineSha256': actual_machine, 'desktopUser': user, 'desktopUid': account.pw_uid,
                      'sourceManifestSha256': source_hash, 'cleanVmVerified': False}
        entries = {Path('/') / row['path']: (stage / 'rootfs' / row['path'], int(row['mode'], 8)) for row in receipt['files']}
        marker = stage / 'enrollment.json'
        marker.write_text(json.dumps(enrollment, indent=2) + '\n')
        entries[Path('/etc/decrypt-os/customer-test.json')] = (marker, 0o644)
        install_receipt = stage / 'install-receipt.json'
        install_receipt.write_text(json.dumps(application_receipt(receipt, actual_machine, accepted), indent=2) + '\n')
        entries[Path(INSTALL) / 'INSTALL-RECEIPT.json'] = (install_receipt, 0o644)
        for target in entries:
            check_parent_ownership(target)
            if target.exists() or target.is_symlink():
                raise RecipeError('Existing installation files are never overwritten: ' + str(target))
        subprocess.run(['/usr/bin/visudo', '-c', '-f', str(stage / 'rootfs/etc/sudoers.d/decrypt-os-customer')],
                       capture_output=True, check=True, timeout=10, env={'PATH': '/usr/bin:/bin'})
        created_files, created_dirs = [], []
        state_handles, state_created = [], []
        def mkdirs(path, uid=0, gid=0, mode=0o755):
            missing = []
            item = path
            while not item.exists():
                missing.append(item)
                item = item.parent
            for item in reversed(missing):
                item.mkdir(mode=mode)
                created_dirs.append(item)
                os.chown(item, uid, gid)
                item.chmod(mode)
        try:
            state_handles, state_created = create_customer_state(account.pw_dir, account.pw_uid, account.pw_gid)
            # Install the privilege rule last. Until then no runtime action is authorized.
            ordered = sorted(entries, key=lambda p: (str(p).startswith('/etc/sudoers.d/'), str(p)))
            for target in ordered:
                source, mode = entries[target]
                mkdirs(target.parent)
                check_parent_ownership(target)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
                created_files.append(target)
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(source.read_bytes())
                    handle.flush()
                    os.fsync(handle.fileno())
                    os.fchmod(handle.fileno(), mode)
            finish_customer_state(state_handles, state_created)
            state_handles = []
            return {'installed': True, 'servicesStarted': False, 'cleanVmVerified': False,
                    'sourceManifestSha256': source_hash, 'targetMachineSha256': actual_machine,
                    'next': 'From the customer desktop, reload user units and open Decrypt OS. Complete the clean-VM test checklist.'}
        except BaseException:
            # Remove only files created by this attempt. No customer data has been opened.
            if state_handles:
                finish_customer_state(state_handles, state_created, rollback=True)
            for path in reversed(created_files):
                path.unlink()
            for path in reversed(created_dirs):
                path.rmdir()
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    render_parser = commands.add_parser('render', help='Create a reviewable rootfs tree only')
    render_parser.add_argument('--bundle', required=True, type=Path)
    render_parser.add_argument('--output', required=True, type=Path)
    render_parser.add_argument('--user', help='Omit only for a generic factory rootfs; creates no account or sudoers rule.')
    check = commands.add_parser('preflight', help='Read-only target checks; rejects protected machines')
    check.add_argument('--bundle', required=True, type=Path)
    check.add_argument('--user', required=True)
    check.add_argument('--host-binding', type=Path, default=PRIVATE_HOST_BINDING)
    install = commands.add_parser('apply', help='Install on an explicitly identified fresh target only; never starts services')
    install.add_argument('--bundle', required=True, type=Path)
    install.add_argument('--host-binding', type=Path, default=PRIVATE_HOST_BINDING)
    install.add_argument('--user', required=True)
    install.add_argument('--source-manifest-sha256', required=True)
    install.add_argument('--target-machine-sha256', required=True)
    install.add_argument('--recipe-sha256', required=True, help='Hash of the reviewed RECIPE-RECEIPT.json, including generated UI')
    install.add_argument('--acknowledgement', required=True, type=Path, help='Versioned local acceptance bound to this target and recipe')
    args = parser.parse_args(argv)
    try:
        if args.command == 'apply':
            print(json.dumps(apply(args.bundle, args.user, args.source_manifest_sha256, args.target_machine_sha256, args.recipe_sha256, args.acknowledgement, args.host_binding), indent=2))
            return 0
        if args.command == 'render':
            result = render(args.bundle, args.output, args.user)
            print(json.dumps({k: v for k, v in result.items() if k != 'files'}, indent=2))
            return 0
        _, manifest_hash = verify_bundle(args.bundle)
        result = inspect_host(args.user, args.host_binding)
        # Do not execute candidate dependencies on a rejected host.
        result['dependencies'] = dependency_status() if result['eligibleForInstall'] else {'ready': False, 'checked': False}
        result['sourceManifestSha256'] = manifest_hash
        result['sourceAuthenticityVerified'] = False
        print(json.dumps(result, indent=2))
        return 0 if result['eligibleForInstall'] and result['dependencies']['ready'] else 1
    except (OSError, ValueError, KeyError, TypeError, SyntaxError, subprocess.SubprocessError) as exc:
        print(json.dumps({'error': str(exc), 'installed': False}), file=sys.stderr)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
