"""Loopback customer API for the local Decrypt OS app."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hmac
import fcntl
import signal
import stat
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

# The installer and fixture tools also load this entry point by its file path.
# Resolve its sibling modules from this candidate, not from a working directory.
BACKEND_DIRECTORY = str(Path(__file__).resolve().parent)
if BACKEND_DIRECTORY not in sys.path:
    sys.path.insert(0, BACKEND_DIRECTORY)
import strategy_access
from engine_workspace import engine_catalog

CATALOG = json.loads((Path(__file__).resolve().parents[1] / 'strategy-catalog.json').read_text())
# Strategies the user may pick in a strategy-access request: catalog-listed,
# marked requestable, and with a public TradingView page to send people to.
REQUESTABLE_STRATEGY_IDS = {
    item['id'] for item in CATALOG['strategies']
    if item.get('listed', True) and item.get('accessRequestAvailable') and item.get('tradingviewUrl')}

STEPS = ('welcome', 'assistant', 'bot', 'connections', 'access', 'review')
CORE_STEPS = ('welcome', 'assistant', 'connections', 'review')
BODY_LIMIT = 8192
SESSION_TTL = 3600


class ApiError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status


def fields(data, required, optional=()):
    if not isinstance(data, dict) or set(data) - set(required) - set(optional) or set(required) - set(data):
        raise ApiError('invalid_fields', 'Use only the fields required for this action. Credentials are not accepted.')


def plain(value, label, maximum):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ApiError('invalid_text', f'{label} must contain 1 to {maximum} printable characters.')
    return value.strip()


def timestamp():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class Store:
    def __init__(self, state_dir, access_config=None, access_sender=None):
        self.configure_access(access_config, access_sender)
        self.directory = Path(state_dir).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / 'onboarding.sqlite'
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS activity (
              id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
              message TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS strategy_access_requests (
              id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, contact TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL CHECK(status IN ('sending','sent','not_sent')), reason TEXT,
              created_at TEXT NOT NULL, sent_at TEXT, attempted REAL NOT NULL);
            ''')
            # Migration-safe: older rows/databases have no strategies column yet.
            columns = {row['name'] for row in db.execute('PRAGMA table_info(strategy_access_requests)')}
            if 'strategies' not in columns:
                db.execute("ALTER TABLE strategy_access_requests ADD COLUMN strategies TEXT NOT NULL DEFAULT ''")
            db.execute("INSERT OR IGNORE INTO settings VALUES ('step','welcome')")
            db.execute("INSERT OR IGNORE INTO settings VALUES ('assistantName','')")
            # Earlier builds required bot/access screens but never recorded an
            # opt-in. Do not infer one.
            if not db.execute("SELECT 1 FROM settings WHERE key='onboardingVersion'").fetchone():
                db.execute("UPDATE settings SET value='connections' WHERE key='step' AND value='bot'")
                db.execute("UPDATE settings SET value='review' WHERE key='step' AND value='access'")
                db.execute("INSERT OR REPLACE INTO settings VALUES ('tradingPreferences','false')")
                db.execute("INSERT OR REPLACE INTO settings VALUES ('preferencesComplete','false')")
                db.execute("INSERT INTO settings VALUES ('onboardingVersion','2')")
            self._drop_registration(db)
            # A send interrupted by a crash has an unknown outcome; offer to send again.
            db.execute("UPDATE strategy_access_requests SET status='not_sent',reason='interrupted' WHERE status='sending'")
        os.chmod(self.path, 0o600)

    @staticmethod
    def _drop_registration(db):
        """Earlier builds tied requests to a Decrypt Labs system registration. Keep the username only."""
        settings = dict(db.execute('SELECT key,value FROM settings'))
        if 'tradingViewUsername' not in settings:
            old = settings.get('boundTradingViewUsername') or settings.get('enrollmentTradingViewUsername') or ''
            db.execute("INSERT INTO settings VALUES ('tradingViewUsername',?)", (old,))
        db.execute("DELETE FROM settings WHERE key IN ('boundTradingViewUsername','enrollmentTradingViewUsername',"
                   "'usernameBindingVersion','usernameBindingReviewRequired','accessWorkflowVersion')")
        for table in ('access_requests', 'outbox', 'legacy_access_history', 'customer_registry_cache',
                      'customer_sync_state', 'support_outbox'):
            db.execute('DROP TABLE IF EXISTS ' + table)

    def configure_access(self, config=None, sender=None):
        if config is None:
            try:
                config = strategy_access.load_config()
            except strategy_access.ConfigError:
                config = {'requestUrl': '', 'publicSite': strategy_access.PUBLIC_SITE, 'strategyFieldSupported': False}
        self.access_config = config
        self.access_sender = sender or strategy_access.HttpSender(
            config['requestUrl'], strategies_supported=config.get('strategyFieldSupported', False))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def strategy_access(self, db):
        settings = dict(db.execute('SELECT key,value FROM settings'))
        row = db.execute('SELECT * FROM strategy_access_requests ORDER BY id DESC LIMIT 1').fetchone()
        latest = None if row is None else {
            'tradingViewUsername': row['username'], 'contact': row['contact'],
            'status': row['status'], 'reason': row['reason'],
            'strategies': [s for s in row['strategies'].split(',') if s] if row['strategies'] else [],
            'createdAt': row['created_at'], 'sentAt': row['sent_at']}
        return {'tradingViewUsername': settings.get('tradingViewUsername', ''),
                'requestConfigured': self.access_sender.configured,
                # Whether a picked strategy list actually reaches Decrypt Labs on the wire
                # (live-rollout switch; see strategy_access.load_config). While False, a pick
                # is still recorded locally (latest.strategies) and shown back to the user,
                # just not sent -- the live sign-up service rejects unknown JSON keys today.
                'strategyFieldSupported': getattr(self.access_sender, 'strategies_supported', False),
                'requestableStrategies': sorted(REQUESTABLE_STRATEGY_IDS),
                'publicSite': self.access_config['publicSite'], 'latest': latest}

    def state(self):
        with self.connect() as db:
            settings = dict(db.execute('SELECT key,value FROM settings'))
            activity = [dict(r) for r in db.execute('SELECT id,kind,message,created_at AS createdAt FROM activity ORDER BY id DESC LIMIT 50')]
            access = self.strategy_access(db)
        step = settings['step']
        trading = settings['tradingPreferences'] == 'true'
        complete = settings['preferencesComplete'] == 'true'
        path = STEPS if trading else CORE_STEPS
        return {'schema': 'decrypt.os.lab.v1', 'mode': 'lab',
                'onboarding': {'version': 2, 'step': step, 'tradingPreferences': trading,
                               'preferencesComplete': complete,
                               'completed': list(path if complete else path[:path.index(step)])},
                'profile': {'assistantName': settings['assistantName'],
                            'tradingViewUsername': settings.get('tradingViewUsername', '')},
                'connections': {'ai': 'not_connected', 'tradingview': 'not_connected', 'tradovate': 'not_connected'},
                'usage': {'ai': {'status': 'unavailable', 'used': None, 'limit': None},
                          'account': {'status': 'not_connected', 'balance': None, 'currency': None}},
                'bots': [{**strategy, 'status': 'not_connected'} for strategy in CATALOG['strategies']],
                'strategyCatalog': CATALOG,
                'engineWorkspace': engine_catalog(),
                'strategyAccess': access, 'activity': activity}

    def onboarding(self, data):
        fields(data, ('step',), ('assistantName', 'tradingPreferences', 'complete', 'tradingViewUsername'))
        if not isinstance(data['step'], str) or data['step'] not in STEPS:
            raise ApiError('invalid_step', 'Choose a supported setup step.')
        for key in ('tradingPreferences', 'complete'):
            if key in data and type(data[key]) is not bool:
                raise ApiError('invalid_choice', 'Setup choices must be true or false.')
        name = plain(data['assistantName'], 'Assistant name', 80) if 'assistantName' in data else None
        username = None
        if 'tradingViewUsername' in data:
            username = '' if data['tradingViewUsername'] == '' else plain(data['tradingViewUsername'], 'TradingView username', 64)
            if username and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', username):
                raise ApiError('invalid_username', 'Enter a public TradingView username, not an email, URL or credential.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            settings = dict(db.execute('SELECT key,value FROM settings'))
            old = settings['step']
            trading = data.get('tradingPreferences', settings['tradingPreferences'] == 'true')
            path = STEPS if trading else CORE_STEPS
            # Opting out while viewing an optional checkpoint resumes the core
            # path. Opting in never advances a core checkpoint by itself.
            old = {'bot': 'connections', 'access': 'review'}.get(old, old) if not trading else old
            if data['step'] not in path:
                raise ApiError('trading_not_selected', 'Choose optional trading preferences in setup first.', 409)
            if path.index(data['step']) > path.index(old) + 1:
                raise ApiError('invalid_transition', 'Complete the preceding setup step first.', 409)
            saved_name = name if name is not None else settings['assistantName']
            if path.index(data['step']) >= 2 and not saved_name:
                raise ApiError('assistant_name_required', 'Save an assistant name before continuing setup.', 409)
            if data.get('complete') and (old != 'review' or data['step'] != 'review'):
                raise ApiError('review_required', 'Review your saved preferences before finishing setup.', 409)
            changed = (data['step'] != settings['step'] or saved_name != settings['assistantName']
                       or trading != (settings['tradingPreferences'] == 'true')
                       or (username is not None and username != settings.get('tradingViewUsername', '')))
            if data.get('complete') and changed:
                raise ApiError('review_required', 'Save changed preferences and review them before finishing setup.', 409)
            complete = data.get('complete', settings['preferencesComplete'] == 'true' and not changed)
            if name is not None:
                db.execute("UPDATE settings SET value=? WHERE key='assistantName'", (name,))
            if username is not None:
                # Saved on this device only. Nothing is sent from setup.
                db.execute("INSERT OR REPLACE INTO settings VALUES ('tradingViewUsername',?)", (username,))
            db.execute("UPDATE settings SET value=? WHERE key='step'", (data['step'],))
            db.execute("UPDATE settings SET value=? WHERE key='tradingPreferences'", ('true' if trading else 'false',))
            db.execute("UPDATE settings SET value=? WHERE key='preferencesComplete'", ('true' if complete else 'false',))
            if changed or complete != (settings['preferencesComplete'] == 'true'):
                # Fixed event text only. Names, credentials and other free-form
                # setup values do not belong in the activity ledger.
                db.execute('INSERT INTO activity(kind,message,created_at) VALUES (?,?,?)',
                           ('preferences', 'Setup preferences completed.' if complete else 'Setup preferences saved.', timestamp()))
        return self.state()

    def send_access_request(self, data, now=None):
        """Send the TradingView username (and an optional typed contact and
        picked strategies) to Decrypt Labs. The picked strategies are always
        recorded locally and shown back to the user; they only go out on the
        wire when the sender's strategyFieldSupported is True (see
        strategy_access.load_config -- the live service rejects unknown JSON
        keys until Decrypt Labs deploys that change)."""
        fields(data, ('tradingViewUsername',), ('contact', 'strategies'))
        username = strategy_access.clean_username(data['tradingViewUsername'])
        if username is None:
            raise ApiError('invalid_username', 'Enter your TradingView username only: letters, numbers, - or _. Not an email, link or password.')
        contact = strategy_access.clean_contact(data.get('contact'))
        if contact is None:
            raise ApiError('invalid_contact', f'Keep the contact to {strategy_access.CONTACT_LIMIT} printable characters.')
        strategies = strategy_access.clean_strategies(data.get('strategies'), REQUESTABLE_STRATEGY_IDS)
        if strategies is None:
            raise ApiError('invalid_strategies', 'Pick only strategies offered for request here.')
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            recent = db.execute('SELECT username,status,attempted FROM strategy_access_requests WHERE attempted>? ORDER BY id DESC',
                                (now - 3600,)).fetchall()
            if len(recent) >= strategy_access.SENDS_PER_HOUR:
                raise ApiError('too_many_requests', 'Too many requests in the last hour. Try again later.', 429)
            if any(row['status'] == 'sent' and row['username'].casefold() == username.casefold()
                   and now - row['attempted'] < strategy_access.RESEND_SECONDS for row in recent):
                raise ApiError('recently_sent', 'This username was just sent. Decrypt Labs grants access by hand, so allow some time.', 429)
            db.execute("INSERT OR REPLACE INTO settings VALUES ('tradingViewUsername',?)", (username,))
            row_id = db.execute(
                "INSERT INTO strategy_access_requests(username,contact,status,created_at,attempted,strategies) VALUES (?,?,'sending',?,?,?)",
                (username, contact, timestamp(), now, ','.join(strategies))).lastrowid
            db.execute('DELETE FROM strategy_access_requests WHERE id NOT IN (SELECT id FROM strategy_access_requests ORDER BY id DESC LIMIT 20)')
        try:
            self.access_sender.send(username, contact or None, strategies or None)
            status, reason = 'sent', None
        except strategy_access.SendError as error:
            status, reason = 'not_sent', str(error)
        except Exception:
            status, reason = 'not_sent', 'unavailable'
        with self.connect() as db:
            db.execute('UPDATE strategy_access_requests SET status=?,reason=?,sent_at=? WHERE id=?',
                       (status, reason, timestamp() if status == 'sent' else None, row_id))
            # Fixed text only. The username, contact and picked strategies stay out of the activity log.
            db.execute('INSERT INTO activity(kind,message,created_at) VALUES (?,?,?)',
                       ('strategy_access', 'Strategy access request sent to Decrypt Labs.' if status == 'sent'
                        else 'Strategy access request not sent.', timestamp()))
        return self.state()


class LabServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, ui_port=8811, harness_enabled=False, guardian_enabled=False, trading_enabled=False):
        if address[0] != '127.0.0.1':
            raise ValueError('Only IPv4 loopback is supported')
        from maintenance_service import OperationGate
        self.operation_gate = OperationGate()
        self.action_lock = threading.RLock()
        self.guardian_enabled, self.trading_enabled = guardian_enabled, trading_enabled
        self.store = store
        from first_start import FirstStart
        self.first_start = FirstStart()
        self.harness = None
        if harness_enabled:
            from harness_bridge import HarnessBridge
            self.harness = HarnessBridge()
        self.sessions = {}
        self.session_lock = threading.Lock()
        super().__init__(address, Handler)
        ports = {self.server_port, ui_port}
        self.allowed_hosts = {f'{host}:{port}' for host in ('127.0.0.1', 'localhost') for port in ports}
        self.allowed_origins = {'http://' + host for host in self.allowed_hosts}
        self.guardian = None
        self.trading = None
        if trading_enabled and not guardian_enabled:
            super().server_close()
            raise ValueError('The customer runtime requires Guardian')
        if guardian_enabled:
            from guardian_service import GuardianService
            try:
                self.guardian = GuardianService(store.directory)
                self.guardian.start()
            except Exception:
                super().server_close()
                raise
        if trading_enabled:
            from trading_service import TradingService
            try:
                # TradingService selects native Tradovate by immutable provider
                # binding and binds this fresh Guardian to its market browser.
                self.trading = TradingService(store.directory, self.guardian, store)
                self.trading.start()
            except Exception:
                self.guardian.close()
                super().server_close()
                raise
        from maintenance_service import MaintenanceService
        try:
            self.maintenance = MaintenanceService(self, restart_factory=self.restart_held_services)
        except Exception:
            if self.trading is not None:
                self.trading.close()
            if self.guardian is not None:
                self.guardian.close()
            super().server_close()
            raise

    def restart_held_services(self):
        # Only the maintenance boundary calls this after all old writers exited.
        # Constructors load credentials locked and engines held, never replaying
        # prior signals as fresh. Recovery holds remain enforced by TradingService.
        guardian = None
        trading = None
        try:
            if self.guardian_enabled:
                from guardian_service import GuardianService
                guardian = GuardianService(self.store.directory)
                guardian.start()
            if self.trading_enabled:
                from trading_service import TradingService
                # Same provider selection/browser binding as the initial path;
                # no restored instance bypasses the service's global hold.
                trading = TradingService(self.store.directory, guardian, self.store)
                trading.start()
            self.guardian, self.trading = guardian, trading
        except Exception:
            if trading is not None:
                trading.close()
            if guardian is not None:
                guardian.close()
            raise

    def server_close(self):
        if getattr(self, 'trading', None) is not None:
            self.trading.close()
        if getattr(self, 'guardian', None) is not None:
            self.guardian.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = 'DecryptLab'
    sys_version = ''

    def log_message(self, format, *args):
        pass  # Request payloads, identifiers and credentials do not belong in logs.

    def reply(self, status, payload, cookie=None):
        raw = json.dumps(payload, ensure_ascii=True).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'")
        self.send_header('Referrer-Policy', 'no-referrer')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(raw)

    def boundary(self):
        if len(self.headers.get_all('Host', [])) != 1 or self.headers.get('Host') not in self.server.allowed_hosts:
            raise ApiError('invalid_host', 'Use the local Decrypt OS address.', 403)
        origins = self.headers.get_all('Origin', [])
        if len(origins) > 1 or (origins and origins[0] not in self.server.allowed_origins):
            raise ApiError('invalid_origin', 'This request did not come from the local app.', 403)
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise ApiError('invalid_origin', 'Cross-site requests are not allowed.', 403)

    def session_origin(self):
        """A CSRF session is issued only to the app's own page: a same-origin
        fetch() from Chromium (Sec-Fetch-Site/Mode/Dest), and an Origin, if sent,
        equal to the requested host. Plain local clients get no token. Local
        processes of other UIDs are kept off this port by the loopback firewall
        (installer/loopback_guard.py), not by these headers."""
        def one(name):
            values = self.headers.get_all(name, [])
            return values[0] if len(values) == 1 else None
        origin = self.headers.get_all('Origin', [])
        if (one('Sec-Fetch-Site') != 'same-origin' or one('Sec-Fetch-Mode') not in ('cors', 'same-origin')
                or one('Sec-Fetch-Dest') != 'empty'
                or (origin and origin != ['http://' + self.headers.get('Host', '')])):
            raise ApiError('session_origin', 'Open Decrypt OS from its own window to start a session.', 403)

    def session(self, create=False):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            sid = cookie['decrypt_lab_session'].value if 'decrypt_lab_session' in cookie else None
        except Exception:
            sid = None
        now = time.monotonic()
        with self.server.session_lock:
            self.server.sessions = {k: v for k, v in self.server.sessions.items() if v[1] > now}
            current = self.server.sessions.get(sid)
            if current:
                return sid, current[0]
            if not create:
                raise ApiError('session_required', 'Open the local app again before making changes.', 403)
            if len(self.server.sessions) >= 256:
                raise ApiError('session_limit', 'Too many local sessions. Try again later.', 429)
            sid, token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.server.sessions[sid] = (token, now + SESSION_TTL)
            return sid, token

    def read_json(self):
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
            raise ApiError('invalid_body', 'Send a single bounded JSON body.')
        try:
            length = int(self.headers['Content-Length'])
        except ValueError:
            raise ApiError('invalid_body', 'Send a bounded JSON body.')
        if length < 1 or length > BODY_LIMIT:
            raise ApiError('body_limit', 'The request is too large or empty.', 413)
        if self.headers.get_content_type() != 'application/json':
            raise ApiError('invalid_content_type', 'Send application/json.', 415)
        try:
            def unique(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError()
                    value[key] = item
                return value
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError()
            return json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError):
            raise ApiError('invalid_json', 'Send one valid JSON object.')

    def handle_api(self):
        path = urlsplit(self.path).path
        gate = self.server.operation_gate.exclusive if self.command == 'POST' and path.startswith('/api/maintenance/') else self.server.operation_gate.ordinary
        with gate():
            return self._handle_api()

    def _handle_api(self):
        try:
            self.connection.settimeout(5)
            self.boundary()
            path = urlsplit(self.path)
            if path.query or path.fragment or path.scheme or path.netloc:
                raise ApiError('not_found', 'This local API route is not available.', 404)
            if self.command == 'GET':
                if path.path == '/api/session':
                    self.session_origin()
                    sid, token = self.session(create=True)
                    self.reply(200, {'csrfToken': token, 'mode': 'lab'},
                               f'decrypt_lab_session={sid}; Path=/api; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}')
                    return
                if path.path == '/api/first-start/status':
                    self.reply(200, self.server.first_start.status())
                    return
                if not self.server.first_start.admitted():
                    raise ApiError('first_start_pending', 'Complete the first-start notice in this local app before continuing.', 403)
                if path.path == '/api/maintenance':
                    self.session()
                    self.reply(200, {'maintenance': self.server.maintenance.status()})
                    return
                if path.path == '/api/state':
                    self.reply(200, self.server.store.state())
                    return
            elif self.command == 'POST':
                sid, token = self.session()
                provided = self.headers.get('X-CSRF-Token', '')
                if len(self.headers.get_all('X-CSRF-Token', [])) != 1 or not hmac.compare_digest(provided, token):
                    raise ApiError('csrf_required', 'Refresh the app session before making changes.', 403)
                data = self.read_json()
                if path.path in ('/api/first-start/accept', '/api/first-start/cancel'):
                    from first_start import FirstStartError
                    try:
                        result = self.server.first_start.perform(path.path.rsplit('/', 1)[1], data)
                    except FirstStartError as exc:
                        raise ApiError(exc.code, exc.message, exc.status) from None
                    except (OSError, ValueError, KeyError, TypeError):
                        raise ApiError('first_start_unavailable', 'First-start setup is not ready. Refresh from the local desktop.', 409) from None
                    self.reply(200, result)
                    return
                # Revocation remains possible; consent cannot authorize providers or trading.
                if path.path != '/api/guardian/lock' and not self.server.first_start.admitted():
                    raise ApiError('first_start_pending', 'Complete the first-start notice in this local app before continuing.', 403)
                if path.path.startswith('/api/maintenance/'):
                    from maintenance import MaintenanceError
                    try:
                        result = self.server.maintenance.perform(path.path.removeprefix('/api/maintenance/'), data)
                    except MaintenanceError as exc:
                        raise ApiError(exc.code, exc.message, exc.status)
                    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                        code = 'maintenance_failed_restart_required' if self.server.maintenance.restart_required else 'maintenance_refused'
                        raise ApiError(code, 'Maintenance did not complete. Check its status before retrying.', 409) from None
                    self.reply(200, result)
                    return
                if self.server.maintenance.restart_required and not path.path.endswith('/status'):
                    raise ApiError('maintenance_restart_required', 'Restart Decrypt OS before changing settings. Trading remains held.', 409)
                if path.path.startswith('/api/trading/'):
                    from trading_service import TradingError, disabled_status
                    from guardian_auth import GuardianError
                    verb = path.path.removeprefix('/api/trading/')
                    if self.server.trading is None:
                        if verb == 'status':
                            fields(data, ())
                            self.reply(200, disabled_status())
                            return
                        raise ApiError('trading_disabled', 'The customer runtime service is not enabled.', 403)
                    try:
                        result = self.server.trading.action(verb, data)
                    except (TradingError, GuardianError) as exc:
                        raise ApiError(exc.code, exc.message, exc.status)
                    self.reply(200, result)
                    return
                if path.path.startswith('/api/guardian/'):
                    from guardian_auth import GuardianError
                    from guardian_service import disabled_status
                    verb = path.path.removeprefix('/api/guardian/')
                    if self.server.guardian is None:
                        if verb == 'status':
                            fields(data, ())
                            self.reply(200, disabled_status())
                            return
                        raise ApiError('guardian_disabled', 'Guardian is not enabled on this fixture server.', 403)
                    try:
                        if self.server.trading is not None:
                            result = self.server.trading.guardian_action(verb, data)
                        else:
                            result = self.server.guardian.action(verb, data)
                    except GuardianError as exc:
                        raise ApiError(exc.code, exc.message, exc.status)
                    except Exception as exc:
                        from trading_service import TradingError
                        if isinstance(exc, TradingError):
                            raise ApiError(exc.code, exc.message, exc.status)
                        raise
                    self.reply(200, result)
                    return
                if path.path.startswith('/api/harness/'):
                    from harness_bridge import ACTIONS, HarnessError, disabled_status
                    verb = path.path.removeprefix('/api/harness/')
                    if verb not in ACTIONS:
                        raise ApiError('not_found', 'This customer runtime action is not available.', 404)
                    fields(data, ())
                    if self.server.harness is None:
                        if verb == 'status':
                            self.reply(200, disabled_status())
                            return
                        raise ApiError('harness_disabled', 'Customer OpenClaw setup is not enabled on this fixture server.', 403)
                    try:
                        result = self.server.harness.action(verb, data)
                    except HarnessError as exc:
                        raise ApiError(exc.code, exc.message, exc.status)
                    self.reply(200, result)
                    return
                if path.path == '/api/system/scope':
                    fields(data, ())
                    from system_scope import scope_snapshot
                    self.reply(200, scope_snapshot(self.server.store))
                    return
                if path.path == '/api/onboarding':
                    self.reply(200, self.server.store.onboarding(data))
                    return
                if path.path == '/api/strategy-access/request':
                    self.reply(200, self.server.store.send_access_request(data))
                    return
            raise ApiError('not_found', 'This local API route is not available.', 404)
        except ApiError as exc:
            self.reply(exc.status, {'error': {'code': exc.code, 'message': exc.message}})
        except Exception:
            self.reply(500, {'error': {'code': 'local_error', 'message': 'The local request failed. Check status before retrying; an external request may have an unknown outcome.'}})
        finally:
            self.close_connection = True

    do_GET = handle_api
    do_POST = handle_api
    do_PUT = handle_api
    do_DELETE = handle_api
    do_PATCH = handle_api
    do_OPTIONS = handle_api


@contextmanager
def single_backend(state_dir):
    """Hold an exclusive lock so two backends never share one state folder."""
    directory = Path(state_dir).expanduser().absolute()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / 'backend.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError('invalid backend lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('customer_state_already_in_use') from None
        yield
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description='Run the local Decrypt OS app API on loopback.')
    parser.add_argument('--state-dir', required=True, type=Path)
    parser.add_argument('--port', type=int, default=8810)
    parser.add_argument('--ui-port', type=int, default=8811)
    parser.add_argument('--enable-harness', action='store_true', help='Enable the isolated Claude assistant runtime (OpenClaw); never enables trading')
    parser.add_argument('--enable-guardian', action='store_true', help='Enable opt-in local Guardian credentials and read-only session recovery; never enables trading')
    parser.add_argument('--enable-trading', action='store_true', help='Enable customer runtime lifecycle; every instance starts with entries held')
    parser.add_argument('--strategy-access-config', type=Path, help='JSON with requestUrl and publicSite (default: ~/.config/decrypt-os/strategy-access.json, then the shipped file)')
    args = parser.parse_args()
    os.umask(0o077)
    def stop_on_signal(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_on_signal)
    try:
        # Lock before Store migrations or any local API listener.
        with single_backend(args.state_dir):
            config = strategy_access.load_config(args.strategy_access_config) if args.strategy_access_config else None
            server = LabServer(('127.0.0.1', args.port), Store(args.state_dir, config), args.ui_port, args.enable_harness, guardian_enabled=args.enable_guardian, trading_enabled=args.enable_trading)
            try:
                print(f'Decrypt OS API listening on loopback port {server.server_port}. Strategy access requests '
                      + ('go to Decrypt Labs.' if server.store.access_sender.configured else 'are not sent; the app shows the username to send by hand.'), flush=True)
                server.serve_forever()
            finally:
                server.server_close()
    except KeyboardInterrupt:
        pass
    except (strategy_access.ConfigError, OSError, ValueError):
        parser.exit(2, 'Customer backend startup failed. Check the state folder lock and strategy access config.\n')


if __name__ == '__main__':
    main()
