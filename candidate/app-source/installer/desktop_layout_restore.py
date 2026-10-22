#!/usr/bin/env python3
"""Reopen missing always-on views without stealing focus or touching trading.

Runs periodically as a user timer. Only ever acts
on RESTORABLE: the operations home, the trading pipeline view, and the bridge
status panel, plus the ICT assistant terminal below. tradingview, bridge-login
and bridge-advanced are never opened, moved, or otherwise touched here --
those windows are user-initiated only.
"""
import os
import pwd
import shlex
import sys

# The unit runs `python3 -I`, which leaves the script's own directory off sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import desktop_apps as apps  # noqa: E402

RESTORABLE = ('decrypt', 'trading-pipeline', 'tradovate-bridge')

# The ICT assistant (workspace 4) is a foot terminal launched by
# phase_b.startup_config's autostart, not a Chromium APPS entry -- it has no
# dedicated profile directory for apps.windows()/apps.resolve_app() to key on.
# It gets its own narrow restore path, keyed on window class instead.
ICT_ASSISTANT_CLASS = 'decrypt-os-ict-assistant'
ICT_ASSISTANT_WORKSPACE = 4
ICT_ASSISTANT_COMMAND = ('/usr/bin/foot --app-id=decrypt-os-ict-assistant '
                          '--title="ICT Assistant" /usr/local/bin/decrypt-ict-assistant')


def lua_string(text):
    """A double-quoted Lua string literal for a fixed launch command."""
    if not text.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError('Launch commands must be printable ASCII.')
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


def exec_silent(command, workspace):
    """hl.dsp.exec_cmd with exec rules: the window opens on its workspace without focus."""
    apps.run(['/usr/bin/hyprctl', 'dispatch',
              f'hl.dsp.exec_cmd({lua_string(command)},{{workspace="{workspace} silent",no_initial_focus=true}})'])


def reposition_silent(config, window):
    """Move an already-open window back onto its workspace without raising it."""
    selector = 'address:' + window['address']
    apps.run(['/usr/bin/hyprctl', 'dispatch',
              f'hl.dsp.window.move({{workspace="{config["workspace"]}",window="{selector}",follow=false}})'])


def launch_silent(config, profile):
    """Launch directly onto the target workspace, never focusing it. Unlike
    desktop_apps.open_window(), this never calls hl.dsp.focus and never raises
    the new window above whatever the user is already looking at."""
    exec_silent(shlex.join(apps.browser_command(config, profile)), config['workspace'])


def restore_one(app, home, uid):
    config = apps.resolve_app(app)
    profile = apps.profile_path(config, home, uid)
    try:
        with apps.launch_lock(profile):
            existing = apps.windows(app, profile, config)
            if existing:
                window = existing[0]
                if window.get('workspace', {}).get('id') != config['workspace']:
                    reposition_silent(config, window)
                    return {'app': app, 'action': 'repositioned'}
                return {'app': app, 'action': 'none'}
            apps.ensure_backend_ready(app)
            launch_silent(config, profile)
            return {'app': app, 'action': 'launched'}
    except ValueError:
        # Another launch is already in flight, or the window list could not be
        # trusted this cycle. The next timer tick tries again; nothing here is
        # worth surfacing to the user.
        return {'app': app, 'action': 'skipped'}


def restore_ict_assistant():
    """Same silent reposition-or-relaunch policy as restore_one(), but keyed on
    the terminal's window class since it has no Chromium profile to key on."""
    try:
        clients = apps.query('clients')
        if not isinstance(clients, list):
            return {'app': 'ict-assistant', 'action': 'skipped'}
        matches = [client for client in clients if isinstance(client, dict)
                   and client.get('class') == ICT_ASSISTANT_CLASS and client.get('mapped') is True]
        if len(matches) > 1:
            # An untrusted/ambiguous window list is skipped, not acted on; the
            # next tick retries, same as a busy lock in restore_one().
            return {'app': 'ict-assistant', 'action': 'skipped'}
        if matches:
            window = matches[0]
            if window.get('workspace', {}).get('id') != ICT_ASSISTANT_WORKSPACE:
                selector = 'address:' + window['address']
                apps.run(['/usr/bin/hyprctl', 'dispatch',
                          f'hl.dsp.window.move({{workspace="{ICT_ASSISTANT_WORKSPACE}",'
                          f'window="{selector}",follow=false}})'])
                return {'app': 'ict-assistant', 'action': 'repositioned'}
            return {'app': 'ict-assistant', 'action': 'none'}
        exec_silent(ICT_ASSISTANT_COMMAND, ICT_ASSISTANT_WORKSPACE)
        return {'app': 'ict-assistant', 'action': 'launched'}
    except (ValueError, KeyError, OSError, apps.subprocess.SubprocessError):
        return {'app': 'ict-assistant', 'action': 'skipped'}


def main():
    uid = os.getuid()
    if uid < 1000 or os.geteuid() != uid:
        print('decrypt-desktop-layout: run this as the local desktop account.', file=sys.stderr)
        return 1
    home = pwd.getpwuid(uid).pw_dir
    for app in RESTORABLE:
        restore_one(app, home, uid)
    restore_ict_assistant()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
