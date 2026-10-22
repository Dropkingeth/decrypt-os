"""Headless Chrome + fake Tradovate + fake trader page, for end-to-end tests.

Browser lifecycle: each Harness starts its own Chrome in a new process group, plus a small
watchdog process holding a pipe to this one. Chrome is killed by close(), by atexit when the
test process exits, and by the watchdog when this process dies without cleanup (SIGKILL,
runner timeout, crash). A failing constructor closes what it started before re-raising.

An installed desktop Chrome is never picked automatically, so a test never shares or disturbs
a user's own browser. Tests use Chromium from the Playwright browser cache
(`npx playwright install chromium`; PLAYWRIGHT_BROWSERS_PATH when set), `chromium` on PATH,
or TVBRIDGE_CHROME.
"""
import atexit
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from fake_tradovate import FakeTradovate, page_html  # noqa: E402

# Playwright's own browser cache: PLAYWRIGHT_BROWSERS_PATH, else its Linux default under the XDG cache.
PLAYWRIGHT_CACHE = Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
                        or Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'ms-playwright')
PLAYWRIGHT_BINARIES = ('chromium-*/chrome-linux*/chrome',)


def _revision(path):
    match = re.search(r'chromium-(\d+)', str(path))
    return int(match.group(1)) if match else 0


def chrome_candidates():
    found = [os.environ.get('TVBRIDGE_CHROME', '')]
    for pattern in PLAYWRIGHT_BINARIES:
        found += [str(p) for p in sorted(PLAYWRIGHT_CACHE.glob(pattern), key=_revision, reverse=True)]
    found += [shutil.which('chromium') or '', shutil.which('chromium-browser') or '']
    return [c for c in found if c]


def chrome_binary():
    for c in chrome_candidates():
        if os.path.exists(c):
            return c
    raise RuntimeError('no test Chrome found: run `npx playwright install chromium` or set TVBRIDGE_CHROME '
                       '(an installed desktop Chrome is deliberately not used)')


def kill_marked(marker, exclude=()):
    """SIGKILL any process whose command line contains marker (the harness's unique temp dir)."""
    try:
        out = subprocess.run(['ps', '-Ao', 'pid=,command='], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return
    for line in out.splitlines():
        pid, _, command = line.strip().partition(' ')
        if marker in command and pid.isdigit() and int(pid) not in (os.getpid(), *exclude):
            try:
                os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass


def stop_group(proc):
    """SIGTERM then SIGKILL Chrome's whole process group (browser, renderers, GPU/network helpers)."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            break
        try:
            proc.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            continue
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # helpers that outlived the browser process
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


# Runs as a separate process with its own session, so terminal Ctrl-C and the death of the
# test process do not reach it. Its stdin is a pipe from the test process: EOF means close()
# finished or the test process is gone, so the Chrome group is killed and the profile removed.
# The temp dir comes in through the environment so this process never matches kill_marked.
WATCHDOG = r'''
import os, shutil, signal, subprocess, sys, time
pgid, tmp = int(sys.argv[1]), os.environ['TVBRIDGE_WATCHDOG_TMP']
try:
    sys.stdin.buffer.read()
except BaseException:
    pass
def alive():
    try:
        os.killpg(pgid, 0)
        return True
    except OSError:
        return False
for sig in (signal.SIGTERM, signal.SIGKILL):
    if not alive():
        break
    try:
        os.killpg(pgid, sig)
    except OSError:
        break
    end = time.time() + 5
    while time.time() < end and alive():
        time.sleep(0.1)
out = subprocess.run(['ps', '-Ao', 'pid=,command='], capture_output=True, text=True).stdout
for line in out.splitlines():
    pid, _, command = line.strip().partition(' ')
    if tmp in command and pid.isdigit() and int(pid) != os.getpid():
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass
shutil.rmtree(tmp, ignore_errors=True)
'''

_live = set()
_live_lock = threading.Lock()


@atexit.register
def close_all():
    with _live_lock:
        live = list(_live)
    for h in live:
        h.close()


# Browser tests run only inside an isolated Linux VM, never on a macOS host.
VM_ONLY_REASON = 'tvbridge browser tests run in an isolated Linux VM, not on macOS'
vm_only = unittest.skipIf(sys.platform == 'darwin', VM_ONLY_REASON)


class Harness:
    def __init__(self, **fake_kwargs):
        if sys.platform == 'darwin':
            raise unittest.SkipTest(VM_ONLY_REASON)
        self._closed = False
        self._close_lock = threading.Lock()
        self.chrome = self.watchdog = self.http = self.fake = None
        self.tmp = Path(tempfile.mkdtemp(prefix='tvbridge-test-'))
        with _live_lock:
            _live.add(self)
        try:
            self._start(fake_kwargs)
        except BaseException:
            self.close()
            raise

    def _start(self, fake_kwargs):
        self.fake = FakeTradovate(**fake_kwargs)
        site = self.tmp / 'site' / 'trader'
        site.mkdir(parents=True)
        (site / 'index.html').write_text(page_html(self.fake.port))
        directory = str(self.tmp / 'site')

        class Quiet(SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=directory, **k)

            def log_message(self, *a):
                pass

        http = ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.http = http
        self.page_url = f'http://127.0.0.1:{self.http.server_address[1]}/trader/index.html'
        profile = self.tmp / 'profile'
        self.chrome = subprocess.Popen(
            [chrome_binary(), '--headless=new', '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
             f'--user-data-dir={profile}', '--no-first-run', '--no-default-browser-check', '--disable-gpu',
             '--use-mock-keychain', self.page_url],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.watchdog = subprocess.Popen(
            [sys.executable, '-c', WATCHDOG, str(self.chrome.pid)], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            env=dict(os.environ, TVBRIDGE_WATCHDOG_TMP=str(self.tmp)))
        port_file = profile / 'DevToolsActivePort'
        deadline = time.time() + 20
        while time.time() < deadline and not port_file.exists():
            if self.chrome.poll() is not None:
                raise RuntimeError('Chrome exited with %s before exposing DevTools' % self.chrome.returncode)
            time.sleep(0.1)
        if not port_file.exists():
            raise RuntimeError('Chrome did not expose DevTools')
        self.devtools_port = int(port_file.read_text().split()[0])
        time.sleep(0.5)

    def config(self, **overrides):
        cfg = {
            'account_name': 'DEMO-1', 'soup_account_id': 'PAPER-TEST', 'ticker': 'MNQZ2021',
            'tick_size': 0.25, 'time_in_force': 'Day',
            'kill_switch_file': str(self.tmp / 'ENTRIES-DISABLED'), 'order_timeout_s': 1.5,
            'journal_dir': str(self.tmp / 'journal'), 'devtools_port': self.devtools_port,
            'page_url_contains': '/trader/', 'trade_url_pattern': r'^ws://127\.0\.0\.1:\d+/v1/websocket$',
        }
        cfg.update(overrides)
        return cfg

    def close(self):
        """Idempotent; safe on a partly started harness. The browser goes first, whatever else fails."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        with _live_lock:
            _live.discard(self)
        try:
            if self.chrome is not None:
                stop_group(self.chrome)
            kill_marked(str(self.tmp), exclude=(self.watchdog.pid,) if self.watchdog else ())
        finally:
            try:
                if self.http is not None:
                    self.http.shutdown()
                    self.http.server_close()
                if self.fake is not None:
                    self.fake.stop()
            finally:
                shutil.rmtree(self.tmp, ignore_errors=True)
                if self.watchdog is not None:
                    self.watchdog.stdin.close()
                    try:
                        self.watchdog.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        self.watchdog.kill()


def seed_market_price(executor, price):
    """A bracket entry needs a known market price for its reference check. A fresh fake exchange
    has no fill yet, so tests save one the way the executor does (tvbridge.executor.MARKET_PRICE_NAME)."""
    import json as _json
    from tvbridge.executor import MARKET_PRICE_NAME
    path = Path(executor.journal.dir) / MARKET_PRICE_NAME
    path.write_text(_json.dumps({'symbol': executor.symbol, 'price': float(price), 'at_ms': executor.clock_ms()}))
