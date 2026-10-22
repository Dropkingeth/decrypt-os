#!/usr/bin/env python3
"""Create fresh-user desktop links without replacing existing user data.

The Hyprland startup additions live in /etc/skel (base/unattended/phase_b.py);
this script never writes a startup file."""
import argparse
import configparser
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from guard import Refusal, check_installed, require, valid_user

SDDM_MAIN_SHA256 = 'aa578ec8a6269079e2141842073821fa24940fa4495a13815fa0754652e6027f'
SDDM_USER_PROPERTY = '  property string currentUser: userModel.lastUser'
CHROMIUM_FLAGS_SHA256 = '75cfb592ab71abf38f99a1c48ee00ad8c772705c4e9a2b66be3ed6d70e7888fd'
TEST_CHROMIUM_FLAGS_SHA256 = '376c7e4eda393b3b2adaa097d76525f5d6df2f6ed2b497c9a9031cf137dd238f'
TEST_CHROMIUM_FLAGS = b'--no-first-run\n--no-default-browser-check\n'



def link_source(home, source):
    home, source = Path(home), Path(source)
    require(home.is_dir() and not home.is_symlink(), 'Expected a real new-user home directory.')
    require(source.is_dir() and not source.is_symlink(), 'Expected the pinned source directory.')
    local = home / '.local'
    share = local / 'share'
    for parent in (local, share):
        require(not parent.is_symlink(), 'User share parents must not be symbolic links.')
        require(not parent.exists() or parent.is_dir(), 'User share parent is not a directory.')
    destination = share / 'omarchy'
    require(not destination.exists() and not destination.is_symlink(),
            'Omarchy user source target already exists. Nothing was replaced.')
    share.mkdir(parents=True, exist_ok=True)
    destination.symlink_to(source, target_is_directory=True)
    return destination


def install_terminal_preference(home):
    config = Path(home) / '.config'
    require(config.is_dir() and not config.is_symlink(), 'Expected the new-user config directory.')
    destination = config / 'xdg-terminals.list'
    require(not destination.exists() and not destination.is_symlink(), 'Terminal preference already exists.')
    destination.write_text('/disable_cache\n/execarg_default:foot.desktop:--\nfoot.desktop\n')


def configure_test_chromium(home):
    """Append two flags only to the freshly copied, exact pinned ARM test config."""
    home = Path(home)
    config = home / '.config'
    destination = config / 'chromium-flags.conf'
    for parent in (home, config):
        require(parent.is_dir() and not parent.is_symlink(),
                'Expected real fresh-user Chromium configuration parents.')
    with os.fdopen(os.open(destination, os.O_RDWR | os.O_NOFOLLOW), 'r+b') as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size == 330,
                'Expected the freshly copied regular Chromium flags file.')
        original = handle.read(331)
        require(hashlib.sha256(original).hexdigest() == CHROMIUM_FLAGS_SHA256,
                'Chromium flags differ from the pinned source. Nothing was changed.')
        updated = original + TEST_CHROMIUM_FLAGS
        require(hashlib.sha256(updated).hexdigest() == TEST_CHROMIUM_FLAGS_SHA256,
                'Unexpected clean-test Chromium flags transformation.')
        # The original source remains unchanged in /usr/share/omarchy. Preserve
        # its copied flags and mode; append only these first-launch preferences.
        handle.seek(0, os.SEEK_END)
        handle.write(TEST_CHROMIUM_FLAGS)
        handle.flush()
        os.fsync(handle.fileno())
        handle.seek(0)
        require(handle.read(373) == updated,
                'Chromium flags readback failed. Inspect the partial installation.')
    return destination


def verify_theme(home, expected_theme='tokyo-night'):
    current = Path(home) / '.local/state/omarchy/current'
    for name in ('colors.toml', 'foot.ini', 'hyprland.lua', 'gum_env.lua', 'btop.theme'):
        file = current / 'theme' / name
        require(file.is_file() and file.stat().st_size > 0, 'Missing generated theme file: ' + name)
        require('{{' not in file.read_text(), 'Unresolved theme template: ' + name)
    require((current / 'theme.name').read_text().strip() == expected_theme, 'Unexpected initial theme.')
    require((current / 'background').is_symlink() and (current / 'background').is_file(),
            'Initial background link is missing or broken.')


def check_sddm_template(path):
    config = configparser.ConfigParser()
    config.read_string(Path(path).read_text())
    require(config.get('General', 'DisplayServer', fallback='') == 'wayland',
            'The pinned SDDM template must select Wayland.')
    require(config.get('Wayland', 'CompositorCommand', fallback='') ==
            'start-hyprland -- --config /usr/share/sddm/hyprland.lua',
            'The pinned SDDM template must use the installed greeter compositor.')
    require(not config.has_section('Autologin'), 'An automatic-login template is not allowed.')


def configure_first_login(path, user):
    """Give the pinned password-only greeter an account before its first login."""
    require(valid_user(user), 'Expected the verified new local account name.')
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), 'Expected the copied regular greeter file.')
    original = path.read_bytes()
    require(hashlib.sha256(original).hexdigest() == SDDM_MAIN_SHA256,
            'The copied greeter does not match the pinned source. Nothing was changed.')
    source = original.decode('utf-8')
    require(source.splitlines().count(SDDM_USER_PROPERTY) == 1,
            'The pinned greeter user property is ambiguous.')
    replacement = SDDM_USER_PROPERTY + ' || ' + json.dumps(user)
    path.write_text(source.replace(SDDM_USER_PROPERTY + '\n', replacement + '\n'), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--apply-new-user-links', action='store_true')
    actions.add_argument('--verify-theme', action='store_true')
    actions.add_argument('--verify-sddm-source', action='store_true')
    actions.add_argument('--configure-first-login', action='store_true')
    args = parser.parse_args()
    context = check_installed()
    home = Path('/home') / context['user']
    if args.configure_first_login:
        configure_first_login('/usr/share/sddm/themes/omarchy/Main.qml', context['user'])
        print('The first login has a local account selected. Its password is still required.')
    elif args.verify_sddm_source:
        check_sddm_template('/usr/share/omarchy/etc/sddm.conf.d/10-wayland.conf')
        print('Pinned SDDM Wayland template checked. Login behavior remains unverified.')
    elif args.verify_theme:
        verify_theme(home, 'decrypt')
        print('Initial theme files and background link are present. Desktop boot remains unverified.')
    else:
        configure_test_chromium(home)
        link_source(home, '/usr/share/omarchy')
        install_terminal_preference(home)
        print('Pinned source linked and terminal preference set for the new local user.')


if __name__ == '__main__':
    try:
        main()
    except (Refusal, OSError, ValueError) as error:
        print('Stopped: ' + str(error), file=sys.stderr)
        sys.exit(1)
