#!/usr/bin/env python3
"""Open post-desktop setup with a native terminal and read-only local progress."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import selectors
import signal
import subprocess
import sys
import types
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
GUIDED_SHA256 = '327a0d8c8bf01f1ad33dfbb7655892644f2f6c794e23aeaf1288cf872d04e38d'
REQUIRED = ('guided/guided_setup.py', 'setup/launch_setup.py', 'setup/monitor.py',
            'setup/index.html', 'setup/styles.css', 'setup/app.js',
            'setup/assets/logo.svg', 'setup/assets/space-grotesk.woff2',
            'setup/assets/jetbrains-mono.woff2')
PYTHON = '/usr/bin/python3'


class Refused(ValueError):
    pass


def require(test, message):
    if not test:
        raise Refused(message)


def load_guided(root=ROOT):
    """Load only the exact bundled reader/installer; never a previous report."""
    path = Path(root) / 'guided/guided_setup.py'
    require(not any(p.is_symlink() for p in (path, *path.parents)),
            'Use the original verified media path, not a linked path.')
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == GUIDED_SHA256,
            'The bundled setup program does not match this launcher.')
    module = types.ModuleType('bundled_guided_setup')
    module.__file__ = str(path)
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    return module


def verify_inputs(media, manifest_sha256, root=ROOT):
    guided = load_guided(root)
    media = guided.real_chain(media)
    require(media == Path(root), 'Run the launcher from the verified installation media.')
    records = guided.verify_media(media, manifest_sha256)
    require(set(REQUIRED) <= records.keys(), 'This media is missing the visual setup files.')
    # verify_media checked membership and every hash, including this launcher and assets.
    require(records['guided/guided_setup.py'] == GUIDED_SHA256,
            'The manifest selects another guided installer.')
    return guided


def desktop_guard():
    require(os.geteuid() == os.getuid() and os.getuid() >= 1000,
            'Open setup as the fresh guest desktop account, never through sudo.')
    require(platform.system() == 'Linux' and platform.machine() == 'aarch64',
            'Visual setup is for the fresh Linux ARM64 guest. This host was not changed.')
    require(bool(os.environ.get('WAYLAND_DISPLAY') or os.environ.get('DISPLAY')),
            'Sign in to the Omarchy desktop before opening setup.')


def executable(*paths):
    # Do not interpret BROWSER, TERMINAL, shell fragments or user PATH commands.
    return next((path for path in paths if Path(path).is_file() and os.access(path, os.X_OK)), None)


def terminal_command(command):
    resolver = executable('/usr/local/bin/xdg-terminal-exec', '/usr/bin/xdg-terminal-exec')
    if resolver:
        return [resolver, '--title=Decrypt setup', '--', *command]
    foot = executable('/usr/bin/foot')
    if foot:
        return [foot, '--title=Decrypt setup', '--', *command]
    kitty = executable('/usr/bin/kitty')
    if kitty:
        return [kitty, '--title', 'Decrypt setup', '--', *command]
    alacritty = executable('/usr/bin/alacritty')
    require(alacritty, 'No supported terminal was found. Open a native terminal and use --in-terminal.')
    return [alacritty, '--title', 'Decrypt setup', '-e', *command]


def browser_command(url):
    require(re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}/', url), 'Invalid local progress address.')
    browser = executable('/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome')
    if browser:
        # These are the same first-run flags installed by base/desktop_layout.py.
        # Use the regular customer profile and keep its credential/keyring policy.
        return [browser, '--no-first-run', '--no-default-browser-check', '--new-window', url]
    browser = executable('/usr/bin/firefox')
    require(browser, 'No supported browser was found. Install the reviewed desktop packages first.')
    return [browser, '--new-window', url]


def child_arguments(media, manifest_sha256, work):
    return [PYTHON, '-I', '-B', str(Path(media) / 'setup/launch_setup.py'),
            '--in-terminal', '--media', str(media), '--manifest-sha256', manifest_sha256,
            '--work', str(work)]


def local_snapshot(url):
    # Environment proxy settings never receive a local setup request.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url + 'api/status', timeout=3) as response:
        require(response.status == 200, 'The local progress reader did not respond.')
        state = json.loads(response.read(8193))
    require(state.get('mode') == 'live' and state.get('source') == 'guided-runtime-app',
            'The progress reader did not return live guided status.')


@contextlib.contextmanager
def running_monitor(work, root=ROOT, python=PYTHON):
    """Own only this monitor. Browser or monitor shutdown never signals installer."""
    child = subprocess.Popen([python, '-I', '-B', str(Path(root) / 'setup/monitor.py'),
                              '--work', str(work)], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(child.stdout, selectors.EVENT_READ)
            require(ready.select(10), 'The local progress reader did not start. Nothing was installed.')
            line = child.stdout.readline(256).strip()
        match = re.fullmatch(r'Read-only setup view: (http://127\.0\.0\.1:([0-9]{1,5})/)', line)
        require(match and 0 < int(match[2]) <= 65535,
                'The local progress reader did not start. Nothing was installed.')
        url = match[1]
        local_snapshot(url)
        yield url
    finally:
        # No process-group kill, browser termination, or installer operation here.
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        if child.stdout:
            child.stdout.close()


def open_browser(url):
    child = subprocess.Popen(browser_command(url), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    try:
        code = child.wait(timeout=0.3)
    except subprocess.TimeoutExpired:
        return
    require(code == 0, 'The browser could not open. Nothing was installed; retry from the desktop.')


def terminal_session(media, manifest_sha256, work):
    require(sys.stdin.isatty() and sys.stdout.isatty(),
            'Setup needs a native terminal for review and the masked password prompt.')
    verify_inputs(media, manifest_sha256)
    # Discover before any build or authentication starts.
    browser_command('http://127.0.0.1:1/')
    print('Decrypt setup: runtime and app', flush=True)
    print('Base installation and desktop login must already be complete.', flush=True)
    with running_monitor(work) as url:
        print('Progress: ' + url, flush=True)
        print('You can close or reopen the progress page. Keep this terminal open.', flush=True)
        print('Enter your password only at the masked administrator prompt below.', flush=True)
        open_browser(url)
        # Inherit terminal I/O. No password, terminal output or installer log is captured.
        command = [PYTHON, '-I', '-B', str(Path(media) / 'guided/guided_setup.py'), 'start',
                   '--media', str(media), '--manifest-sha256', manifest_sha256, '--work', str(work)]
        child = subprocess.Popen(command)
        try:
            code = child.wait()
        except KeyboardInterrupt:
            # The terminal sends SIGINT to the foreground group, including guided setup.
            # Wait for its own interruption journal before cleaning only the monitor.
            print('\nWaiting for guided setup to record its result. Keep its files.', flush=True)
            code = child.wait()
        print('\nSetup command finished. Read its result above; this is not an end-to-end pass.', flush=True)
        try:
            input('Press Return to close the progress service and this setup window: ')
        except (EOFError, KeyboardInterrupt):
            pass
        return code


def interrupted_monitor_owner(signum, _frame):
    # Unwind only this worker's monitor context. Never signal the guided process.
    raise SystemExit(128 + signum)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--media', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--work', type=Path, default=Path.home() / '.local/state/decrypt-setup')
    parser.add_argument('--in-terminal', action='store_true', help='Use the current native terminal.')
    args = parser.parse_args(argv)
    try:
        desktop_guard()
        require(args.work.is_absolute(), 'Choose an absolute path for saved setup state.')
        verify_inputs(args.media, args.manifest_sha256)
        if args.in_terminal:
            signal.signal(signal.SIGTERM, interrupted_monitor_owner)
            signal.signal(signal.SIGHUP, interrupted_monitor_owner)
            return terminal_session(args.media, args.manifest_sha256, args.work)
        browser_command('http://127.0.0.1:1/')
        command = terminal_command(child_arguments(args.media, args.manifest_sha256, args.work))
        # A native terminal may hand off through IPC and exit. Its lifetime is not setup state.
        subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True)
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print('Setup could not open: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
