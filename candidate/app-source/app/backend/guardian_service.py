"""Opt-in encrypted sign-in vault and bounded login recovery. Never an execution authority.

Only the exact customer-owned browser is used. Recovery reservations are durable
before any login submission. A recovered sign-in is never evidence of broker or
engine readiness; it only keeps the TradingView session usable for the
customer's own chart work. Tradovate sign-in belongs to the bridge module.
"""
from __future__ import annotations

import math
import re
import threading
import time

from guardian_auth import GuardianError, GuardianVault, PrivateFiles, PROVIDERS, provider_id, require
from guardian_login_browser import LoginBrowsers
from guardian_keyring import VaultUnlock

ATTEMPT_WINDOW = 86400
MAX_ATTEMPTS = 3
PAUSES = {'mfa', 'captcha', 'bad_password', 'ambiguous_form', 'untrusted_page', 'untrusted_form'}


def disabled_status():
    return {'enabled': False, 'vault': {'configured': False, 'unlocked': False}, 'providers': [],
            'engineReady': False, 'executionAuthority': False}


class GuardianService:
    def __init__(self, state_dir, browser=None, clock=time.time):
        self.files = PrivateFiles(state_dir)
        self.vault = GuardianVault(self.files, clock)
        self.unlocker = VaultUnlock(self.vault)
        self.browser = browser or LoginBrowsers(state_dir, clock)
        self.login_checked = {}
        self.login_verified = {}
        self.clock = clock
        self.mutex = threading.RLock()
        self.stop_event, self.wake_event = threading.Event(), threading.Event()
        self.worker = None
        self.closed = False
        self.provider_status = {}
        try:
            missing = object()
            self.retry = self.files.read('recovery.json', missing=missing)
            if self.retry is missing:
                self.retry = {p: {'attempts': [], 'nextAttemptAt': 0, 'pause': None} for p in PROVIDERS}
            elif type(self.retry) is dict:
                # Drop retired providers and add newly supported ones; never
                # invent a fresh retry budget for a provider that already has one.
                self.retry = {p: self.retry[p] for p in PROVIDERS if p in self.retry}
                for p in PROVIDERS:
                    if p not in self.retry:
                        self.retry[p] = {'attempts': [], 'nextAttemptAt': 0, 'pause': None}
            self._validate_retry()
            self.vault.metadata()  # Fail startup on unsafe storage, without unlocking.
        except Exception:
            self.files.close()
            raise

    def _validate_retry(self):
        if type(self.retry) is not dict or self.retry.keys() != PROVIDERS.keys():
            raise GuardianError('unsafe_storage')
        for data in self.retry.values():
            require(data, ('attempts', 'nextAttemptAt', 'pause'))
            if (type(data['attempts']) is not list or len(data['attempts']) > MAX_ATTEMPTS
                    or any(type(t) not in (int, float) or not math.isfinite(t) or t <= 0 for t in data['attempts'])
                    or type(data['nextAttemptAt']) not in (int, float) or not math.isfinite(data['nextAttemptAt'])
                    or data['nextAttemptAt'] < 0 or data['pause'] not in PAUSES | {None, 'attempt_limit', 'submission_unknown'}):
                raise GuardianError('unsafe_storage')

    def start(self):
        with self.mutex:
            if self.closed or self.worker is not None:
                return False
            self.worker = threading.Thread(target=self._run, name='guardian-observer', daemon=True)
            self.worker.start()
            return True

    def _run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    self.check()
                except Exception:
                    pass
                self.wake_event.wait(5)
                self.wake_event.clear()
        finally:
            with self.mutex:
                self.vault.lock()
                self.files.close()

    def close(self):
        # Signal before waiting for a bounded CDP read; no further phase starts.
        self.stop_event.set()
        self.wake_event.set()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=45)
        with self.mutex:
            self.closed = True
            self.vault.lock()
            if self.worker is None or not self.worker.is_alive():
                self.files.close()

    def status(self):
        with self.mutex:
            metadata = self.vault.metadata()
            providers = []
            for provider, label in PROVIDERS.items():
                saved = metadata['providers'].get(provider, {})
                live = dict(self.provider_status.get(provider, {}))
                stamp = self.login_verified.get(provider)
                if live.get('state') == 'authenticated' and (stamp is None or not 0 <= self.clock() - stamp <= 15):
                    live = {'state': 'available', 'reason': 'session_stale'}
                retry = self.retry[provider]
                state = live.get('state', 'locked' if saved and not metadata['unlocked'] else 'available' if saved else 'not_saved')
                reason = live.get('reason', 'vault_locked' if saved and not metadata['unlocked'] else 'not_checked')
                if saved and not metadata['unlocked'] and state not in ('requires_user', 'cooldown'):
                    state, reason = 'locked', 'vault_locked'
                providers.append({'id': provider, 'label': label, 'saved': bool(saved),
                                  'autoLogin': saved.get('autoLogin', False), 'state': state, 'reason': reason,
                                  'nextAttemptAt': retry['nextAttemptAt'] if retry['nextAttemptAt'] > self.clock() else None,
                                  'requiresUser': bool(retry['pause']) or state in ('locked', 'not_saved', 'requires_user')})
            return {'enabled': True, 'vault': {**{k: metadata[k] for k in ('configured', 'unlocked')}, 'unlockMode': self.unlocker.mode(), 'unlockError': self.unlocker.last_error},
                    'providers': providers, 'engineReady': False, 'executionAuthority': False}

    def action(self, action, data):
        with self.mutex:
            if self.closed:
                raise GuardianError('guardian_busy', status=409)
            if action in ('status', 'lock', 'check'):
                require(data)
                if action == 'lock':
                    try:
                        self.unlocker.lock()
                    finally:
                        self.provider_status.clear()
                        self.login_verified.clear()
                elif action == 'check':
                    self.check()
            elif action == 'initialize':
                require(data, (), ('passphrase', 'unlockMode'))
                # Old callers supplying only a passphrase retain old semantics.
                mode = data.get('unlockMode', 'passphrase' if 'passphrase' in data else 'keyring')
                self.unlocker.initialize(mode, data.get('passphrase'))
            elif action == 'unlock':
                require(data, (), ('passphrase',))
                self.unlocker.unlock(data.get('passphrase'))
            elif action == 'use-login-keyring':
                require(data, ('passphrase', 'acknowledgeLoginUnlock'))
                if data['acknowledgeLoginUnlock'] is not True:
                    raise GuardianError('invalid_input')
                self.unlocker.use_keyring(data['passphrase'])
            elif action == 'save-provider':
                require(data, ('provider', 'username', 'password', 'autoLogin'))
                self.vault.save_provider(data['provider'], data['username'], data['password'], data['autoLogin'])
                # Credential edits can acknowledge a pause but never reset the
                # durable daily budget or cooldown.
                self._persist_pause(data['provider'], None)
                self.provider_status.pop(data['provider'], None)
                self.login_verified.pop(data['provider'], None)
            elif action == 'delete-provider':
                require(data, ('provider',))
                self.vault.delete_provider(data['provider'])
                self.provider_status.pop(data['provider'], None)
                self.login_verified.pop(data['provider'], None)
            elif action == 'resume-provider':
                require(data, ('provider', 'nativeAccountId', 'acknowledgeManualSignIn'))
                provider = provider_id(data['provider'])
                account = data['nativeAccountId']
                from guardian_auth import bounded_text
                account = bounded_text(account, 1, 64)
                if data['acknowledgeManualSignIn'] is not True or (provider != 'tradingview' and not re.fullmatch(r'[1-9]\d{0,19}|[0-9a-f-]{36}', account)):
                    raise GuardianError('invalid_input')
                if not self.vault.metadata()['providers'].get(provider):
                    raise GuardianError('provider_not_saved', status=409)
                proof = self.browser.verify_session(provider, account, None)
                stamp = proof.get('observedAt') if type(proof) is dict else None
                if (type(proof) is not dict or proof.get('valid') is not True
                        or proof.get('identityVerified') is not True or proof.get('sessionVerified') is not True
                        or proof.get('provider') != provider or proof.get('nativeAccountId') != account
                        or type(stamp) not in (int, float) or not math.isfinite(stamp)
                        or not 0 <= self.clock() * 1000 - stamp <= 10000):
                    raise GuardianError('session_unverified', 'Complete sign-in on the exact account in the Guardian browser, then verify again.', 409)
                # No retry history, cooldown, credential or monitor is reset.
                self._persist_pause(provider, None)
                self._provider(provider, 'available', 'native_session_verified')
            elif action == 'open-browser':
                require(data, ('provider',))
                self.browser.open(provider_id(data['provider']))
            else:
                raise GuardianError('action_unknown', status=404)
            if action not in ('status', 'lock'):
                self.wake_event.set()
            return self.status()

    def _provider(self, provider, state, reason):
        self.provider_status[provider] = {'state': state, 'reason': reason}

    def _persist_pause(self, provider, pause):
        # Do not release an in-memory pause before its durable transition.
        # A failed outcome/acknowledgement write retains the old inhibit.
        retry = self.retry[provider]
        self.files.write('recovery.json', {**self.retry, provider: {**retry, 'pause': pause}})
        retry['pause'] = pause

    def _recover(self, provider):
        if self.stop_event.is_set():
            return False
        now = self.clock()
        if now - self.login_checked.get(provider, 0) < 5:
            stamp = self.login_verified.get(provider)
            return stamp is not None and 0 <= now - stamp <= 15
        self.login_checked[provider] = now
        self.login_verified.pop(provider, None)
        view = self.browser.inspect(provider)
        state = view.get('state')
        retry = self.retry[provider]
        if state == 'authenticated_unverified':
            # A loading/redirect page without a password input is not proof of
            # successful login, and cannot clear MFA/bad-password/unknown pauses.
            metadata = self.vault.metadata()
            if not metadata['unlocked']:
                self._provider(provider, 'locked', 'vault_locked')
                return False
            with self.vault.credential(provider) as (username, _, auto_login):
                proof = self.browser.verify_saved(provider, username, None) if auto_login else None
            stamp = proof.get('observedAt') if type(proof) is dict else None
            if (proof and proof.get('valid') is True and type(stamp) in (int, float)
                    and math.isfinite(stamp) and 0 <= self.clock() * 1000 - stamp <= 15000):
                self.login_verified[provider] = stamp / 1000
                self._provider(provider, 'authenticated', 'fresh_provider_request')
                return True
            self._provider(provider, 'available', 'session_unverified')
            return False
        if state in PAUSES:
            retry['pause'] = state
            self.files.write('recovery.json', self.retry)
            self._provider(provider, 'requires_user', state)
            return False
        if state != 'logged_out':
            self._provider(provider, 'requires_user', 'login_page_missing')
            return False
        metadata = self.vault.metadata()
        saved = metadata['providers'].get(provider)
        if not saved or not saved['autoLogin']:
            self._provider(provider, 'logged_out', 'automatic_login_disabled')
            return False
        if not metadata['unlocked']:
            self._provider(provider, 'locked', 'vault_locked')
            return False
        now = self.clock()
        if retry['pause']:
            self._provider(provider, 'requires_user', retry['pause'])
            return False
        recent = [t for t in retry['attempts'] if now - t < ATTEMPT_WINDOW]
        if len(recent) >= MAX_ATTEMPTS:
            retry['nextAttemptAt'] = max(retry['nextAttemptAt'], min(recent) + ATTEMPT_WINDOW)
            self.files.write('recovery.json', self.retry)
            self._provider(provider, 'cooldown', 'attempt_limit')
            return False
        if retry['nextAttemptAt'] > now:
            self._provider(provider, 'cooldown', 'retry_cooldown')
            return False
        if self.stop_event.is_set():
            return False
        with self.vault.credential(provider) as (username, password, auto_login):
            if not auto_login:
                return False
            # A crash/network loss after this reservation cannot cause an
            # uncounted retry. This is persisted BEFORE credential submission.
            retry['attempts'] = recent + [now]
            retry['nextAttemptAt'] = now + (60, 300, 900)[len(recent)]
            retry['pause'] = 'submission_unknown'
            self.files.write('recovery.json', self.retry)
            try:
                outcome = self.browser.submit(provider, view['targetId'], username, password)
            except Exception:
                self._provider(provider, 'requires_user', 'submission_unknown')
                return False
        outcome = outcome.get('state')
        self._persist_pause(provider, None if outcome == 'login_submitted' else outcome if outcome in PAUSES else 'submission_unknown')
        self._provider(provider, 'login_submitted' if outcome == 'login_submitted' else 'requires_user',
                       'awaiting_native_verification' if outcome == 'login_submitted' else retry['pause'])
        return False

    def login_request(self, provider, check=False):
        provider_id(provider)
        with self.mutex:
            if self.closed or self.stop_event.is_set():
                raise GuardianError('guardian_busy', status=409)
            metadata = self.vault.metadata()
            saved = metadata['providers'].get(provider, {})
            if check and saved.get('autoLogin'):
                self.unlocker.try_at_login()
                try:
                    self._recover(provider)
                except Exception:
                    self._provider(provider, 'requires_user', 'browser_unavailable')
            metadata = self.vault.metadata()
            retry = self.retry[provider]
            live = self.provider_status.get(provider, {})
            verified = self.login_verified.get(provider)
            state = live.get('state', 'available' if saved else 'disabled')
            reason = live.get('reason', 'not_checked')
            if not saved.get('autoLogin'):
                state, reason, verified = 'disabled', 'automatic_login_disabled', None
            elif not metadata['unlocked']:
                state, reason, verified = 'locked', 'vault_locked', None
            elif verified is not None and not 0 <= self.clock() - verified <= 15:
                state, reason, verified = 'available', 'session_stale', None
            return {'schema': 'decrypt.guardian-login.v1', 'provider': provider,
                    'state': state, 'reason': reason, 'checkedAt': self.login_checked.get(provider),
                    'verifiedAt': verified, 'nextAttemptAt': retry['nextAttemptAt'] or None,
                    'requiresUser': bool(retry['pause']) or state in ('locked', 'requires_user'),
                    'executionAuthority': False}

    def check(self):
        with self.mutex:
            if self.closed or self.stop_event.is_set():
                return
            self.unlocker.try_at_login()
            metadata = self.vault.metadata()
            maintain = [p for p in PROVIDERS if metadata['providers'].get(p, {}).get('autoLogin')]
            for provider in maintain:
                if self.stop_event.is_set():
                    return
                try:
                    self._recover(provider)
                except GuardianError:
                    self._provider(provider, 'requires_user', 'browser_unavailable')
