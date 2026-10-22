"""Opt-in HTTP boundary for the isolated customer OpenClaw harness.

Fixed commands only. No HTTP credentials, shell text, provider URLs or raw
runtime output. Constructors do not start services. GUI login is visible and
workspace-scoped; it is never a captured/background sign-in subprocess.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import threading

ACTIONS = frozenset(('status', 'start', 'login', 'console', 'ict', 'stop', 'storage_setup', 'storage_unlock', 'verify'))
STATUS_VALUES = frozenset(('not_started', 'starting', 'ready', 'needs_login',
                           'connected', 'error', 'unavailable'))
# Claude only: the configured primary and its fallback.
MODELS = frozenset(('anthropic/claude-opus-5-5', 'anthropic/claude-sonnet-5'))
HELPER = Path(__file__).resolve().parents[1] / 'harness' / 'runtime.py'
PYTHON = '/usr/bin/python3'
HYPRCTL = '/usr/bin/hyprctl'
FOOT = '/usr/bin/foot'
SYSTEMD_RUN = '/usr/bin/systemd-run'
SYSTEMCTL = '/usr/bin/systemctl'
GUI_ENV = ('WAYLAND_DISPLAY', 'HYPRLAND_INSTANCE_SIGNATURE', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS')
APP_IDS = {'login': 'decrypt-os-claude-signin', 'console': 'decrypt-os-assistant', 'ict': 'decrypt-os-ict-assistant',
           'storage_setup': 'decrypt-os-storage-setup', 'storage_unlock': 'decrypt-os-storage-unlock'}
TITLES = {'login': 'Claude sign-in', 'console': 'assistant', 'ict': 'ICT assistant',
          'storage_setup': 'encrypted storage setup', 'storage_unlock': 'storage unlock'}
# Installed system (the fixed helper exists): every privileged runtime action goes through the
# customer's sudoers entry, which needs a password. The app therefore never runs sudo itself. It
# reads the status the helper publishes after each action (runtime.py STATUS_FILE) plus the unit
# state, and opens start/login/verify/stop in a terminal on workspace 4 where sudo asks. Start and
# login share the guided connect terminal (launcher.py --connect).
INSTALLED_CONTROL = Path('/usr/local/lib/decrypt-os/installer/customer_control.py')
STATUS_FILE = Path('/run/decrypt-os/harness-status.json')
STATUS_SCHEMA = 'decrypt.harness-status.v1'
UNIT = 'decrypt-os-customer-pilot.service'
LAUNCHER = Path(__file__).resolve().parents[1] / 'ict-assistant' / 'launcher.py'
INSTALLED_TERMINALS = {'start': 'login', 'login': 'login', 'verify': 'verify', 'stop': 'stop'}
INSTALLED_APP_IDS = {'verify': 'decrypt-os-claude-verify', 'stop': 'decrypt-os-claude-stop'}
INSTALLED_TITLES = {'verify': 'connection check', 'stop': 'stop assistant'}
ALL_APP_IDS = {**APP_IDS, **INSTALLED_APP_IDS}
ALL_TITLES = {**TITLES, **INSTALLED_TITLES}
# Assistant terminals (sign-in, product assistant, ICT assistant) live on workspace 4.
WORKSPACE = 4
MAX_OUTPUT = 65536
ERROR_TEXT = {
    'runtime_busy': 'Another customer runtime action is in progress. Check status shortly.',
    'disabled': 'Customer OpenClaw setup is not enabled on this fixture server.',
    'runtime_unavailable': 'The customer runtime helper is unavailable.',
    'runtime_failed': 'The customer runtime request failed. Check its local status before retrying.',
    'runtime_timeout': 'The customer runtime request timed out. Check status before retrying.',
    'gui_unavailable': 'Open the app in the Omarchy desktop session to use native sign-in or the assistant console.',
    'workspace_unavailable': 'Workspace 4 could not be verified. No sign-in or console window was opened.',
    'terminal_unavailable': 'The native terminal could not be opened. No background sign-in was started.',
    'login_not_available': 'Native sign-in is available only when the customer runtime needs login.',
    'console_not_available': 'Connect your Claude account before opening the assistant. A saved sign-in is not a verified model response.',
    'verify_not_available': 'Sign in and unlock saved storage before checking the connection.',
    'verification_failed': 'The model response was not verified. Refresh status before retrying.',
    'runtime_not_started': 'Start the customer runtime before opening sign-in or the assistant console.',
    'status_after_terminal': 'The assistant runtime is on, but its sign-in state is not known yet. Finish or reopen the terminal on workspace 4; the status refreshes after each step there.',
    'stop_not_available': 'Nothing to stop: the assistant runtime is off.',
    'password_terminal': 'This step needs your password. Open it from this page; it runs in a terminal on workspace 4.',
}


def installed():
    return INSTALLED_CONTROL.is_file()


class HarnessError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status


def _base(enabled=True):
    return {'enabled': enabled, 'status': 'unavailable', 'runtime': 'OpenClaw',
            'provider': 'Claude', 'credentialPersistence': 'session_only',
            'storage': {'configured': False, 'unlocked': False, 'error': None, 'busy': False, 'operation': None},
            'gatewayPort': 19789, 'model': None, 'loginRunning': False,
            'consoleRunning': False, 'ictRunning': False, 'scopeUpdatedAt': None, 'error': None,
            'passwordInTerminal': False}


def disabled_status():
    return {**_base(False), 'error': ERROR_TEXT['disabled']}


def _timestamp(value):
    if type(value) is not str or len(value) > 32:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        return parsed.isoformat(timespec='seconds')
    except ValueError:
        return None


def _sanitize(raw):
    result = _base()
    if type(raw) is not dict:
        return {**result, 'error': ERROR_TEXT['runtime_unavailable']}
    storage = raw.get('storage') if type(raw.get('storage')) is dict else {}
    result['storage'] = {'configured': storage.get('configured') is True,
                         'unlocked': storage.get('unlocked') is True,
                         'error': 'Saved sign-in storage needs attention.' if storage.get('error') else None,
                         'busy': False, 'operation': None}
    result['credentialPersistence'] = 'encrypted' if raw.get('credentialPersistence') == 'encrypted' else 'session_only'
    status = raw.get('status')
    result['status'] = status if type(status) is str and status in STATUS_VALUES else 'unavailable'
    model = raw.get('model')
    result['model'] = model if type(model) is str and model in MODELS else None
    result['scopeUpdatedAt'] = _timestamp(raw.get('scopeUpdatedAt'))
    result['loginRunning'] = raw.get('loginRunning') is True
    result['consoleRunning'] = raw.get('consoleRunning') is True
    # Never reflect runtime error strings, account identity, auth data or paths.
    if result['status'] in ('error', 'unavailable'):
        result['error'] = ERROR_TEXT['runtime_unavailable']
    return result


class HarnessBridge:
    def __init__(self):
        self.lock = threading.Lock()

    @staticmethod
    def _command(verb):
        if verb not in ACTIONS:
            raise HarnessError('unknown_action', 'This customer runtime action is not available.', 404)
        if installed():
            # The exact sudoers command; sudo asks for the password on the terminal it runs in.
            return ['/usr/bin/sudo', PYTHON, '-B', '-I', str(INSTALLED_CONTROL), verb]
        # Development path: the helper escalates itself (fixed, non-interactive); an outer sudo would lose the caller identity.
        command = [PYTHON, str(HELPER), verb]
        if verb in ('status', 'start', 'stop', 'verify'):
            command.append('--json')
        return command

    @classmethod
    def _terminal_command(cls, verb):
        """What runs inside the workspace 4 terminal for a verb."""
        key = INSTALLED_TERMINALS.get(verb) if installed() else None
        if key == 'login':
            return [PYTHON, '-I', '-B', str(LAUNCHER), '--connect']
        if key in ('verify', 'stop'):
            return [PYTHON, '-I', '-B', str(LAUNCHER), '--' + key]
        return cls._command(verb)

    @staticmethod
    def _run(command, timeout):
        return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, timeout=timeout,
                              check=False, shell=False)

    @staticmethod
    def _published():
        """The helper's last published status, or None. Bounded, schema-checked, never raises."""
        try:
            if not STATUS_FILE.is_file() or STATUS_FILE.stat().st_size > MAX_OUTPUT:
                return None
            value = json.loads(STATUS_FILE.read_text())
        except (OSError, ValueError, TypeError):
            return None
        return value if type(value) is dict and value.get('schema') == STATUS_SCHEMA else None

    def _installed_status(self):
        """Status without sudo: the unit state (readable by anyone) plus the published file when it
        belongs to the current unit start."""
        try:
            reply = self._run([SYSTEMCTL, 'show', UNIT, '--no-pager',
                               '--property=LoadState,ActiveState,ActiveEnterTimestampMonotonic'], 1.5)
            props = dict(line.split('=', 1) for line in reply.stdout.splitlines() if '=' in line) \
                if len(reply.stdout) <= MAX_OUTPUT else {}
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
            props = {}
        published = self._published()
        result = _sanitize(published) if published else _base()
        result.update({'status': 'not_started', 'error': None, 'passwordInTerminal': True,
                       'loginRunning': False, 'consoleRunning': False})
        state = props.get('ActiveState')
        if state == 'active':
            if published and published.get('unitStart') and published.get('unitStart') == props.get('ActiveEnterTimestampMonotonic'):
                fresh = _sanitize(published)
                result.update({k: fresh[k] for k in ('status', 'model', 'scopeUpdatedAt', 'loginRunning',
                                                      'consoleRunning', 'error')})
            else:
                result.update({'status': 'unavailable', 'error': ERROR_TEXT['status_after_terminal']})
        elif state == 'activating':
            result['status'] = 'starting'
        elif state == 'failed':
            result.update({'status': 'error', 'error': ERROR_TEXT['runtime_failed']})
        return result

    def _runtime(self, verb):
        if installed():
            if verb == 'status':
                return self._installed_status()
            return {**self._installed_status(), 'error': ERROR_TEXT['password_terminal']}
        try:
            reply = self._run(
                [SYSTEMD_RUN, '--user', '--quiet', '--wait', '--pipe', '--collect',
                 '--property=RuntimeMaxSec=' + ('125' if verb == 'verify' else '35'), '--property=TimeoutStopSec=3',
                 *self._command(verb)], 4 if verb == 'status' else 130 if verb == 'verify' else 30)
            if reply.returncode != 0:
                return {**_base(), 'status': 'error', 'error': ERROR_TEXT['runtime_failed']}
            if verb == 'verify':
                if len(reply.stdout) > MAX_OUTPUT:
                    raise ValueError('bounded_output')
                proof = json.loads(reply.stdout)
                if (type(proof) is not dict or proof.get('verified') is not True
                        or proof.get('model') not in MODELS
                        or _timestamp(proof.get('verifiedAt')) is None):
                    return {**_base(), 'status': 'error', 'error': ERROR_TEXT['verification_failed']}
                # Only the runtime's fresh status may claim connected. Its
                # supervisor may need one poll to consume the new proof.
                return self._runtime('status')
            if verb not in APP_IDS and verb != 'status':
                return self._runtime('status')
            if len(reply.stdout) > MAX_OUTPUT:
                raise ValueError('bounded_output')
            return _sanitize(json.loads(reply.stdout))
        except subprocess.TimeoutExpired:
            return {**_base(), 'error': ERROR_TEXT['runtime_timeout']}
        except (OSError, ValueError, TypeError):
            return {**_base(), 'error': ERROR_TEXT['runtime_unavailable']}

    def _unit_states(self):
        states = {verb: False for verb in ALL_APP_IDS}
        try:
            reply = self._run([SYSTEMCTL, '--user', 'show', '--no-pager',
                               '--property=Id', '--property=ActiveState',
                               *[ALL_APP_IDS[verb] + '.service' for verb in ALL_APP_IDS]], 1.5)
            if len(reply.stdout) > MAX_OUTPUT:
                return states
            for block in reply.stdout.strip().split('\n\n'):
                values = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
                for verb, app_id in ALL_APP_IDS.items():
                    if values.get('Id') == app_id + '.service':
                        states[verb] = values.get('ActiveState') in ('active', 'activating', 'deactivating')
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
            pass
        return states

    def _with_terminals(self, result, units=None):
        units = self._unit_states() if units is None else units
        operation = 'setup' if units.get('storage_setup') else 'unlock' if units.get('storage_unlock') else None
        return {**result, 'loginRunning': result.get('loginRunning') is True or units.get('login', False),
                'consoleRunning': result.get('consoleRunning') is True or units.get('console', False),
                'ictRunning': units.get('ict', False),
                'verifyRunning': units.get('verify', False), 'stopRunning': units.get('stop', False),
                'storage': {**result.get('storage', {}), 'busy': operation is not None, 'operation': operation}}

    def _gui_json(self, selector):
        reply = self._run([HYPRCTL, '-j', selector], 3)
        if reply.returncode or len(reply.stdout) > MAX_OUTPUT:
            raise ValueError('gui_unavailable')
        return json.loads(reply.stdout)

    def _open_terminal(self, verb):
        result = self._runtime('status')
        units = self._unit_states()
        result = self._with_terminals(result, units)
        if result.get('storage', {}).get('busy'):
            return result
        native_storage = verb in ('storage_setup', 'storage_unlock')
        saved = result.get('storage', {})
        # Installed: start and login share the connect terminal (any state); verify and stop are
        # their own one-step terminals. The terminal asks for the password.
        key = INSTALLED_TERMINALS.get(verb) if installed() else None
        if native_storage:
            if result.get('loginRunning'):
                return {**result, 'error': ERROR_TEXT['runtime_busy']}
            if verb == 'storage_setup' and saved.get('configured'):
                return {**result, 'error': 'Saved sign-in storage already exists. Unlock it instead.'}
            if verb == 'storage_unlock' and not saved.get('configured'):
                return {**result, 'error': 'Set up saved sign-in storage first.'}
        elif key == 'login':
            if saved.get('error'):
                return {**result, 'error': result.get('storage', {}).get('error')}
        elif key == 'verify':
            if result['status'] not in ('ready', 'connected') or result.get('loginRunning'):
                return {**result, 'error': ERROR_TEXT['verify_not_available']}
        elif key == 'stop':
            if result['status'] == 'not_started' and not saved.get('unlocked'):
                return {**result, 'error': ERROR_TEXT['stop_not_available']}
        else:
            if result['status'] in ('not_started', 'starting', 'error', 'unavailable'):
                return {**result, 'error': result['error'] or ERROR_TEXT['runtime_not_started']}
            allowed = ('needs_login',) if verb == 'login' else ('ready', 'connected')
            if result['status'] not in allowed:
                return {**result, 'error': ERROR_TEXT[
                    'login_not_available' if verb == 'login' else 'console_not_available']}
        verb = key or verb
        if units.get(verb):
            return result
        if not os.environ.get('WAYLAND_DISPLAY') or not os.environ.get('HYPRLAND_INSTANCE_SIGNATURE'):
            return {**result, 'status': 'unavailable', 'error': ERROR_TEXT['gui_unavailable']}
        try:
            # Read back the exact target workspace before spawning. Never use
            # user-supplied shell, app-id, title, workspace or command arguments.
            focus = self._run([HYPRCTL, 'dispatch', 'hl.dsp.focus({workspace="4"})'], 3)
            active = self._gui_json('activeworkspace')
            if (focus.returncode or type(active) is not dict
                    or type(active.get('id')) is not int or active['id'] != WORKSPACE):
                return {**result, 'status': 'unavailable', 'error': ERROR_TEXT['workspace_unavailable']}
            clients = self._gui_json('clients')
            if type(clients) is not list:
                raise ValueError('gui_clients_unavailable')
            for client in clients:
                if type(client) is not dict or client.get('class') != ALL_APP_IDS[verb]:
                    continue
                workspace = client.get('workspace')
                if type(workspace) is not dict or workspace.get('id') != WORKSPACE:
                    return {**result, 'status': 'unavailable', 'error': ERROR_TEXT['workspace_unavailable']}
                # Survives backend restarts without opening duplicate terminals.
                if native_storage:
                    return {**result, 'storage': {**result.get('storage', {}), 'busy': True,
                            'operation': 'setup' if verb == 'storage_setup' else 'unlock'}}
                return {**result, verb + 'Running': True}
            command = [FOOT, '--app-id=' + ALL_APP_IDS[verb], '--title=Decrypt OS ' + ALL_TITLES[verb],
                       *self._terminal_command(verb)]
            # User manager starts this fixed unit independently of the API's
            # NoNewPrivileges sandbox. Keep that sandbox intact; sudo escalation
            # still belongs solely to the fixed runtime helper inside this TTY.
            environment = []
            for name in GUI_ENV:
                value = os.environ.get(name)
                if value and len(value) <= 4096 and not any(ord(char) < 32 for char in value):
                    environment.append('--setenv=' + name + '=' + value)
            launched = self._run([SYSTEMD_RUN, '--user', '--quiet', '--collect',
                                  '--unit=' + ALL_APP_IDS[verb], *environment, *command], 4)
            if launched.returncode:
                return {**result, 'status': 'unavailable', 'error': ERROR_TEXT['terminal_unavailable']}
            result = self._with_terminals(result)
            running = result.get('storage', {}).get('busy') if native_storage else result.get(verb + 'Running')
            if running is not True:
                return {**result, 'status': 'unavailable', 'error': ERROR_TEXT['terminal_unavailable']}
            return result
        except subprocess.TimeoutExpired:
            return {**result, 'status': 'unavailable', 'error': ERROR_TEXT['workspace_unavailable']}
        except (OSError, ValueError, TypeError):
            return {**result, 'status': 'unavailable', 'error': ERROR_TEXT['terminal_unavailable']}

    def action(self, verb, data):
        if type(verb) is not str or verb not in ACTIONS:
            raise HarnessError('unknown_action', 'This customer runtime action is not available.', 404)
        if type(data) is not dict or data:
            raise HarnessError('invalid_fields', 'This action accepts an empty object only. Enter credentials in the native sign-in window.')
        if verb == 'status':
            # Read-only, so it never takes the launch lock: a status poll in flight must not
            # turn the user's first Start click into runtime_busy.
            return self._with_terminals(self._runtime('status'))
        # Serialize launches, preventing simultaneous HTTP requests duplicating
        # terminals or racing start/stop. No lock means no wait on GUI input.
        if not self.lock.acquire(blocking=False):
            return {**_base(), 'error': ERROR_TEXT['runtime_busy']}
        try:
            if verb not in APP_IDS and verb != 'status':
                units = self._unit_states()
                if units.get('storage_setup') or units.get('storage_unlock'):
                    return {**self._with_terminals(self._runtime('status')), 'error': ERROR_TEXT['runtime_busy']}
            if verb == 'verify':
                current = self._with_terminals(self._runtime('status'), units)
                saved = current.get('storage', {})
                locked = (saved.get('configured') or current.get('credentialPersistence') == 'encrypted') and not saved.get('unlocked')
                if (current.get('status') not in ('ready', 'connected') or locked
                        or saved.get('error') or current.get('loginRunning')):
                    return {**current, 'error': ERROR_TEXT['verify_not_available']}
            if verb in APP_IDS or (installed() and verb in INSTALLED_TERMINALS):
                return self._open_terminal(verb)
            result = self._runtime(verb)
            if verb == 'verify' and result.get('status') in ('error', 'unavailable'):
                # A failed check must not relabel encrypted state as session-only.
                result = {**current, 'status': result['status'], 'error': result.get('error')}
            return self._with_terminals(result)
        finally:
            self.lock.release()
