"""Offline desktop preparation inside the already allocated factory target."""
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
from common import child, read, require, write


def omarchy_child(root, relative):
    """Archive names may contain ordinary spaces; signed-media paths may not."""
    require(type(relative) is str and relative and not relative.startswith('/')
            and '\\' not in relative
            and all(part not in ('', '.', '..') for part in relative.split('/'))
            and not any(ord(c) < 32 or ord(c) == 127 or (c.isspace() and c != ' ') for c in relative),
            'omarchy-member-path')
    return Path(root) / relative


def unpack_omarchy(archive, destination):
    """Reject devices, hardlinks, escaping links and duplicate members."""
    destination = Path(destination)
    require(not destination.exists() and not destination.is_symlink(), 'omarchy-target-exists')
    with tarfile.open(archive, 'r:') as source:
        rows = source.getmembers(); names = set(); total = 0
        require(0 < len(rows) <= 30000, 'omarchy-member-count')
        for row in rows:
            name = row.name.rstrip('/'); omarchy_child(destination, name)
            require(name not in names and (row.isfile() or row.isdir() or row.issym()), 'omarchy-member-type')
            names.add(name); total += row.size
            require(0 <= row.size <= 64 * 1024 * 1024 and total <= 512 * 1024 * 1024, 'omarchy-member-size')
            if row.issym():
                link = PurePosixPath(row.linkname)
                require(row.linkname and not link.is_absolute() and '\\' not in row.linkname, 'omarchy-link')
                resolved = destination / PurePosixPath(name).parent / link
                require(os.path.commonpath([os.path.normpath(resolved), destination]) == str(destination), 'omarchy-link-escape')
        require({'bin', 'config', 'default', 'applications'} <= names, 'omarchy-root-layout')
        destination.mkdir(mode=0o755)
        # Links last: no archive link may become an extraction parent.
        for row in sorted(rows, key=lambda r: (r.issym(), r.name.count('/'), r.name)):
            path = omarchy_child(destination, row.name.rstrip('/'))
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            require(not any(p.is_symlink() for p in path.parents if p != destination.parent), 'omarchy-linked-parent')
            if row.isdir():
                path.mkdir(exist_ok=True, mode=0o755)
            elif row.issym():
                path.symlink_to(row.linkname)
            else:
                stream = source.extractfile(row); require(stream is not None, 'omarchy-content')
                write(path, stream.read(), mode=0o755 if row.mode & 0o111 else 0o644)


# Hyprland 0.56 Lua dispatch form only: no `hyprctl dispatch workspace N` strings
# anywhere in this repo. Static placement uses hl.workspace_rule (persistence)
# and o.window/hl.dsp.focus (default workspace); runtime placement after a
# window opens uses hl.dsp.window.move / hl.dsp.focus (see desktop_apps.py).
WORKSPACE_RULES = (b'for _, id in ipairs({"1", "2", "3", "4", "5"}) do\n'
                    b'  hl.workspace_rule({ workspace = id, persistent = true })\n'
                    b'end\n')

# Every assistant terminal (Claude sign-in, the product assistant, the ICT
# assistant, encrypted-storage setup/unlock)
# opens on workspace 4, silently, no matter who raised the window.
ASSISTANT_CLASSES = ('decrypt-os-ict-assistant', 'decrypt-os-assistant', 'decrypt-os-claude-signin',
                      'decrypt-os-claude-verify', 'decrypt-os-claude-stop',
                      'decrypt-os-storage-setup', 'decrypt-os-storage-unlock')
ASSISTANT_WINDOW_RULES = b''.join(
    b'o.window({class = "' + name.encode() + b'"}, {workspace = "4 silent", tile = true, no_initial_focus = true})\n'
    for name in ASSISTANT_CLASSES)

# Chromium on Wayland ignores --class for --app windows and names them after
# the URL (host + first path segment). These are the classes the Decrypt app
# windows really get, so the placement holds no matter who opened the window
# (session start, the restore timer, or the user). WS3/WS5 bridge windows are
# placed by the bridge module's own rules file below.
APP_WINDOW_RULES = (
    b'o.window({class = "chrome-127.0.0.1__-Default"}, {workspace = "1", tile = true})\n'
    b'o.window({class = "chrome-127.0.0.1__pipeline-Default"}, {workspace = "2 silent", tile = true, no_initial_focus = true})\n'
    b'o.window({class = "chrome-www.tradingview.com__chart_-Default"}, {workspace = "2 silent", tile = true, no_initial_focus = true})\n')

# The Tradovate bridge module ships its own window placement (workspaces 3, 5,
# 6-9) in a file installed alongside it, not baked into this image config --
# source it if the module is present; pcall keeps a bridge-less image booting.
BRIDGE_RULES_PATH = b'/usr/local/lib/decrypt-os/modules/tradovate-bridge/desktop/hyprland-bridge-rules.lua'
BRIDGE_RULES_INCLUDE = b'pcall(dofile, "' + BRIDGE_RULES_PATH + b'")\n'

# The ICT assistant launcher only prints the NY session clock and waits for
# Enter; it starts nothing else and asks for sudo only inside its own
# terminal, so autostarting it on WS4 is safe.
ICT_ASSISTANT_COMMAND = (b'/usr/bin/foot --app-id=decrypt-os-ict-assistant '
                          b'--title="ICT Assistant" /usr/local/bin/decrypt-ict-assistant')


# In a VM guest, Chromium app windows under Hyprland's variable frame rate only repaint on
# input (a saved form stays stale until the mouse moves). Continuous frames fix that at the
# cost of idle CPU, so only VM guests get them; real hardware keeps VFR.
VM_FRAMES = (b'do\n'
             b'  local virt = io.popen("/usr/bin/systemd-detect-virt --vm --quiet && echo yes")\n'
             b'  if virt then\n'
             b'    local vm = virt:read("*a") or ""\n'
             b'    virt:close()\n'
             b'    if vm:match("yes") then hl.config({debug={vfr=false}}) end\n'
             b'  end\n'
             b'end\n')


def startup_config(software_rendering):
    """Decrypt additions for ~/.config/hypr/autostart.lua.

    Omarchy's hyprland.lua loads its own default/hypr/autostart.lua (shell,
    environment import, first-run provisioning) and then the user's
    hypr/autostart.lua; this block is appended to the latter, so Omarchy's
    startup always runs. WS1-5 stay open even with no windows on them
    (persistent), and every fresh session starts focused on WS1 (Operations).
    The WS1-3 app windows are opened by decrypt-desktop-layout.timer, which
    also reopens them if closed.
    """
    prefix = b'hl.config({debug={vfr=false}})\n' if software_rendering else b''
    return (prefix + VM_FRAMES + WORKSPACE_RULES + ASSISTANT_WINDOW_RULES + APP_WINDOW_RULES + BRIDGE_RULES_INCLUDE
            + b'hl.on("hyprland.start", function()\n'
              b'  hl.exec_cmd("/usr/bin/python3 -I -B /usr/local/lib/decrypt-os/installer/desktop_apps.py --session-start")\n'
              # Single-quoted Lua string: the command itself contains double quotes.
              b"  hl.exec_cmd('" + ICT_ASSISTANT_COMMAND + b"', {workspace=\"4 silent\", no_initial_focus=true})\n"
              b'  hl.dispatch(hl.dsp.focus({workspace="1"}))\n'
              b'end)\n')


# Lume and UTM both expose the guest display as Virtual-1. Hyprland's "auto"
# scale picks 2 on a 1920x1200 VM screen, which leaves a 960x600 desktop; use
# 1.25 instead. Real monitors keep "auto".
MONITOR_OVERRIDE = (b'\n-- Decrypt OS: VM display at 1.25 (more room than "auto" picks on a VM screen).\n'
                    b'hl.monitor({ output = "Virtual-1", mode = "preferred", position = "auto", scale = 1.25 })\n'
                    b'hl.env("GDK_SCALE", "1")\n')


# Omarchy's first login (omarchy-provision-first-run) posts two toasts, "Update System"
# and "Learn Keybindings", and tries to enable user units this image does not ship.
# That step fails, so first-run retries at every login and re-posts both toasts.
# Decrypt OS manages its own updates and keeps Super+K for the cheatsheet, so these
# three steps become no-ops. Everything else in first-run (default keyring, theme
# seeding, Chromium hosts, GTK settings) still runs as upstream wrote it, and the
# run completes and marks itself done. Keyed by file name: (text that must be in
# the pinned original, replacement).
QUIET_FIRST_RUN = {
    'welcome.sh': (b'omarchy-notification-send',
                   b'# Decrypt OS: no keybinding toast. Super+K opens the cheatsheet.\n'),
    'wifi.sh': (b'omarchy-notification-send',
                b'# Decrypt OS: no Wi-Fi or update toast. Decrypt OS manages its own updates;\n'
                b'# omarchy-update stays a manual command in the Omarchy menu.\n'),
    'enable-user-units.sh': (b'systemctl --user enable',
                             b'#!/bin/bash\n# Decrypt OS ships none of the Omarchy user units, so there is nothing to enable.\nexit 0\n'),
}


def quiet_first_run(source):
    """Replace the three first-run steps above inside the unpacked Omarchy tree."""
    first_run = Path(source) / 'install/user/first-run'
    for name, (marker, replacement) in QUIET_FIRST_RUN.items():
        path = first_run / name
        require(path.is_file() and not path.is_symlink() and marker in path.read_bytes(), 'omarchy-first-run-' + name)
        write(path, replacement, exclusive=False, mode=0o644)


def no_automatic_omarchy_update(target):
    """Nothing in the image may start omarchy-update on its own: no Omarchy unit, timer or
    hook is installed. The shell's update indicator only checks; the user clicks it."""
    target = Path(target)
    for directory in ('usr/lib/systemd/system', 'usr/lib/systemd/user', 'etc/systemd/system', 'etc/systemd/user'):
        require(not list((target / directory).glob('omarchy-*')), 'omarchy-unit-installed')
    for directory in ('usr/share/libalpm/hooks', 'etc/pacman.d/hooks'):
        require(not any(b'omarchy-update' in path.read_bytes() for path in (target / directory).glob('*.hook')),
                'omarchy-update-hook')


# The bar's "Passwordless Sudo" indicator polls `sudo -n omarchy-sudo-passwordless __status`
# every 5 s. Decrypt OS refuses passwordless sudo by policy, so the toggle has no use here and
# its poll only fills the journal with "a password is required". The other indicators stay.
BAR_INDICATORS = ['Dnd', 'Reminder', 'NightLight', 'StayAwake', 'ScreenRecording', 'RemoteSession']


def drop_passwordless_sudo_indicator(shell_json):
    """Set an explicit indicator list on every omarchy.indicators bar widget in shell.json."""
    import json
    path = Path(shell_json)
    config = json.loads(path.read_bytes())
    layout = config['bar']['layout']
    found = 0
    for section in layout.values():
        for widget in section:
            if widget.get('id') == 'omarchy.indicators':
                widget['items'] = list(BAR_INDICATORS)
                found += 1
    require(found >= 1, 'omarchy-shell-indicators')
    write(path, (json.dumps(config, indent=2, ensure_ascii=False) + '\n').encode(), exclusive=False, mode=0o644)


# The idle screensaver runs `ttfx`, which this image does not ship (see arm-limitations.txt).
# Left on, it fills the screen with "command not found" after 150 s idle and keys or the mouse
# cannot dismiss it. 0 turns the idle screensaver off (omarchy-shell idle service); the lock
# timeout is unchanged.
def turn_off_idle_screensaver(shell_json):
    import json
    path = Path(shell_json)
    config = json.loads(path.read_bytes())
    idle = config.get('idle')
    require(isinstance(idle, dict) and 'screensaver' in idle, 'omarchy-shell-idle')
    idle['screensaver'] = 0
    write(path, (json.dumps(config, indent=2, ensure_ascii=False) + '\n').encode(), exclusive=False, mode=0o644)


def install_desktop(factory, install_packages=True):
    """build/guest_build.py installs the packages itself and passes install_packages=False."""
    from driver import TARGET
    if install_packages:
        factory.chroot(['pacman', '--config', '/etc/pacman-factory.conf', '-S', '--needed', '--noconfirm', '--', *factory.desktop])
    record = factory.manifest['omarchy']; archive = child(factory.media, record['path'])
    require(factory.inventory.get(record['path']) == record['sha256'], 'omarchy-hash-binding')
    source = TARGET / 'usr/share/omarchy'; unpack_omarchy(archive, source)
    quiet_first_run(source)
    for executable in sorted((source / 'bin').iterdir()):
        require(executable.is_file() and not executable.is_symlink(), 'omarchy-bin-type')
        if executable.stat().st_mode & 0o111:
            destination = TARGET / 'usr/local/bin' / executable.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            require(not destination.exists() and not destination.is_symlink(), 'desktop-command-collision')
            destination.symlink_to('/usr/share/omarchy/bin/' + executable.name)
    skel = TARGET / 'etc/skel'
    require(not (skel / '.config').exists() and not (skel / '.local').exists(), 'desktop-skeleton-not-fresh')
    shutil.copytree(source / 'config', skel / '.config', symlinks=True)
    drop_passwordless_sudo_indicator(skel / '.config/omarchy/shell.json')
    turn_off_idle_screensaver(skel / '.config/omarchy/shell.json')
    (skel / '.local/share').mkdir(parents=True)
    shutil.copytree(source / 'applications', skel / '.local/share/applications', symlinks=True)
    (skel / '.local/share/omarchy').symlink_to('/usr/share/omarchy')
    # Never shadow Omarchy's default/hypr/autostart.lua (it starts the shell and
    # imports the session environment): extend the user's hypr/autostart.lua.
    require(not (skel / '.config/default').exists(), 'desktop-default-shadow')
    for name, addition in (('autostart.lua', b'\n' + startup_config(factory.profile['graphics']['softwareRendering'])),
                           ('monitors.lua', MONITOR_OVERRIDE)):
        path = skel / '.config/hypr' / name
        require(path.is_file() and not path.is_symlink(), 'desktop-user-hypr-' + name)
        write(path, read(path) + addition, exclusive=False, mode=0o644)
    # The Decrypt app's private state directory (the API refuses to create it).
    state = skel / '.local/state/decrypt-os'; state.mkdir(parents=True, mode=0o700); state.chmod(0o700)
    write(skel / '.config/xdg-terminals.list', b'/disable_cache\n/execarg_default:foot.desktop:--\nfoot.desktop\n', mode=0o644)
    # %h expands per-session, so this is correct for any future account despite
    # being written once into the shared skeleton. Keeps the Node compile cache
    # off /tmp (decrypt-tmp-janitor may clear /tmp hourly) and on disk instead.
    environment_d = skel / '.config/environment.d'; environment_d.mkdir(parents=True, exist_ok=True)
    write(environment_d / '60-node-compile-cache.conf', b'NODE_COMPILE_CACHE=%h/.cache/node-compile-cache\n', mode=0o644)
    visuals = factory.media / 'installer/base/desktop-visuals'
    theme_parent = skel / '.config/omarchy/themes'; theme_parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(visuals / 'theme', theme_parent / 'decrypt')
    greeter = TARGET / 'usr/share/sddm/themes/decrypt'; greeter.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(visuals / 'greeter', greeter)
    require('QtVersion=6' in (greeter / 'metadata.desktop').read_text().splitlines(), 'selected-greeter-qt-version')
    write(greeter / 'theme.conf', b'[General]\n', mode=0o644)
    config = TARGET / 'etc/sddm.conf.d'; config.mkdir(parents=True, exist_ok=True)
    wayland = read(source / 'etc/sddm.conf.d/10-wayland.conf')
    require(b'[Autologin]' not in wayland and b'DisplayServer=wayland' in wayland, 'greeter-policy')
    write(config / '10-decrypt-wayland.conf', wayland, mode=0o644)
    write(config / '10-decrypt-theme.conf', b'[Theme]\nCurrent=decrypt\n', mode=0o644)
    for relative, destination in [('default/sddm/hyprland.lua', 'usr/share/sddm/hyprland.lua'),
                                  ('default/wayland-sessions/omarchy.desktop', 'usr/local/share/wayland-sessions/omarchy.desktop')]:
        path = TARGET / destination; path.parent.mkdir(parents=True, exist_ok=True)
        write(path, read(source / relative), mode=0o644)
    terminal = factory.media / 'installer/base/vendor/xdg-terminal-exec/xdg-terminal-exec'
    write(TARGET / 'usr/local/bin/xdg-terminal-exec', read(terminal), mode=0o755)
    factory.chroot(['env', 'HOME=/etc/skel', 'OMARCHY_PATH=/usr/share/omarchy', 'OMARCHY_THEME_HEADLESS=1',
                    '/usr/share/omarchy/bin/omarchy-theme-set', 'Decrypt'])
    write(TARGET / 'etc/omarchy-source-commit', (record['commit'] + '\n').encode(), mode=0o644)
    if factory.profile['graphics']['softwareRendering']:
        directory = TARGET / 'etc/environment.d'; directory.mkdir(exist_ok=True)
        write(directory / '90-decrypt-software.conf', b'LIBGL_ALWAYS_SOFTWARE=1\nWLR_RENDERER_ALLOW_SOFTWARE=1\nAQ_NO_MODIFIERS=1\nGSK_RENDERER=cairo\n', mode=0o644)
    factory.command(['systemctl', '--root', str(TARGET), 'enable', 'sddm.service', 'NetworkManager.service', 'systemd-timesyncd.service'])
    factory.command(['systemctl', '--root', str(TARGET), 'set-default', 'graphical.target'])
    no_automatic_omarchy_update(TARGET)
    factory.no_customer()
