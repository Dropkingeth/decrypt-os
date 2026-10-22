"""Change engine: preview → confirm → (wait for flat) → backup → write → restart → verify → audit.

Any failure after the backup restores every touched file and re-runs the restarts. One change applies
at a time (process lock + file lock), so two windows can't interleave writes.
"""
import fcntl
import json
import os
import secrets
import threading
import time

import adapters
import loginops
import plans
from model import Invalid, State, mask, stream_strength
from tree import FileOps, dump_json, file_sha, read_switch, sha

ACTIVE = ('previewed', 'waiting_flat', 'applying')
FINISHED = ('applied', 'failed', 'cancelled', 'rolled_back', 'planned', 'manual')
PREVIEW_TTL_S = 900  # an unconfirmed preview expires; confirm needs a fresh one
# Things the page refers to that are not live yet: the page shows "Coming soon" with this sentence instead of
# offering a button that would only be refused.
COMING_SOON = {
    'rotate_stream_id': 'Changing a stream ID is coming soon: a guided, step-by-step rotation together with '
                        'TradingView. It is on hold until then.',
    'activate_webhook': 'Turning a webhook on is coming soon. It needs a second strategy that has passed '
                        'backtesting, and your approval.',
    'add_webhook': 'Webhooks with their own strategy and leader are coming soon. For now, Register webhook adds '
                   'one that stays off.',
}
WEBHOOK_STATUS_TEXT = {'live': 'Live', 'planned': 'Planned — not running yet', 'off': 'Off — waiting for a strategy'}
OFF_NOTE = 'Off — waiting for a strategy. Nothing runs and no alert is accepted until you approve a strategy.'


def hold_text(guard):
    """'Weekend pause: automatic login is off until the Sun 6:00 PM open (America/New_York).'"""
    text = ((guard or {}).get('weekend_hold') or {}).get('text')
    if isinstance(text, str) and ' to ' in text:
        return 'Weekend pause: automatic login is off until the %s.' % text.split(' to ', 1)[1].replace(' (', ' open (', 1)
    return 'Weekend pause: automatic login is off until the Sunday 6:00 PM ET open.'


class Conflict(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code, self.detail = code, detail


class Store:
    def __init__(self, root):
        self.root = root
        os.makedirs(root, mode=0o700, exist_ok=True)

    def path(self, cid):
        if not cid or '/' in cid or cid.startswith('.'):
            raise Invalid('bad change id')
        return os.path.join(self.root, cid + '.json')

    def get(self, cid):
        try:
            with open(self.path(cid)) as f:
                return json.load(f)
        except (FileNotFoundError, Invalid):
            return None

    def put(self, ch):
        ch['updated_at'] = time.time()
        FileOps.atomic_write(self.path(ch['id']), dump_json(ch))

    def all(self):
        out = []
        for name in os.listdir(self.root):
            if name.endswith('.json'):
                try:
                    with open(os.path.join(self.root, name)) as f:
                        out.append(json.load(f))
                except ValueError:
                    continue
        return sorted(out, key=lambda c: c['created_at'], reverse=True)


class Audit:
    """Append-only JSONL with a hash chain, so a missing or edited line shows up."""

    def __init__(self, path):
        self.path = path

    def last_hash(self):
        try:
            with open(self.path, 'rb') as f:
                lines = f.read().splitlines()
            return json.loads(lines[-1])['hash'] if lines else 'genesis'
        except FileNotFoundError:
            return 'genesis'

    def add(self, ch, profile):
        entry = {'at': time.time(), 'change_id': ch['id'], 'type': ch['type'], 'status': ch['status'],
                 'summary': ch['summary'], 'actor': 'operator', 'mode': profile.mode,
                 'files': [f['path'] for f in ch['plan'].get('files', [])],
                 'prev_hash': self.last_hash()}
        res = ch.get('result') or {}
        if res.get('restart_log'):
            # Both runs (apply and the restore re-run) are on the record, unit by unit.
            entry['restarts'] = [{k: r.get(k) for k in ('phase', 'unit', 'action', 'ok')} for r in res['restart_log']]
        if res.get('needs_person'):
            entry['needs_person'] = res['needs_person']['units']
        if res.get('flatten'):
            entry['flatten'] = res['flatten']   # the bridge's full reply and how it was read
        entry['hash'] = sha(json.dumps(entry, sort_keys=True))
        with open(self.path, 'a') as f:
            f.write(json.dumps(entry) + '\n')
        return entry

    def read(self, limit=100):
        try:
            with open(self.path) as f:
                rows = [json.loads(x) for x in f if x.strip()]
        except FileNotFoundError:
            return []
        return list(reversed(rows))[:limit]

    def verify_chain(self):
        prev = 'genesis'
        for row in reversed(self.read(10 ** 9)):
            h = row.pop('hash')
            if row['prev_hash'] != prev or sha(json.dumps(row, sort_keys=True)) != h:
                return False
            prev = h
        return True


def _diff(profile, op):
    import difflib
    if op['action'] != 'write':
        return ''
    try:
        with open(op['path']) as f:
            before = f.read().splitlines()
    except FileNotFoundError:
        before = []
    after = op['content'].splitlines()
    return '\n'.join(list(difflib.unified_diff(before, after, lineterm='', n=1))[2:])[:6000]


class Engine:
    def __init__(self, profile, status=None, restarter=None, orders=None, clock=time.time):
        self.p = profile
        s, r, o = adapters.build(profile)
        self.status, self.restarter, self.orders = status or s, restarter or r, orders or o
        self.files = FileOps(profile)
        self.loginops = loginops.LoginSetup(profile, self.restarter)
        os.makedirs(profile.admin_state, mode=0o700, exist_ok=True)
        self.store = Store(profile.st('changes'))
        self.audit = Audit(profile.st('audit.jsonl'))
        self.lock = threading.RLock()
        self._flock = open(profile.st('.engine.lock'), 'w')
        try:
            fcntl.flock(self._flock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('another tvbridge-admin is already running on this admin state')
        self.clock = clock
        self._stop = threading.Event()
        st = State(profile)
        if not os.path.exists(st.registry_path):  # admin state, not a live config
            FileOps.atomic_write(st.registry_path, dump_json(st.registry))
        self.recover_interrupted()

    # ----- helpers -----
    def state(self):
        return State(self.p)

    def _hash(self, state, ctype, params, plan):
        paths = sorted({op['path'] for op in plan['ops'] + plan['pre_ops']})
        doc = {'type': ctype, 'params': params, 'docs': state.fingerprint(), 'paths': {p: file_sha(p) for p in paths}}
        if plan.get('login'):   # Add/Start/Remove login: what install.py and the units looked like at preview
            doc['login'] = {k: v for k, v in plan['login'].items() if k != 'units_now'}
            doc['watch'] = {p: file_sha(p) for p in plan.get('watch', [])}
        return sha(json.dumps(doc, sort_keys=True))[:32]

    def _public_plan(self, plan):
        return {
            'immediate': plan['immediate'], 'requires_flat': plan['requires_flat'], 'affected': plan['affected'],
            'wait_on': plan['wait_on'],
            'files': [{'path': self.p.rel(op['path']), 'action': {'write': 'create' if not os.path.exists(op['path'])
                                                                 else 'modify'}.get(op['action'], op['action']),
                       'to': self.p.rel(op['to']) if op.get('to') else None, 'diff': _diff(self.p, op)}
                      for op in plan['pre_ops'] + plan['ops']] + list(plan.get('public_files', [])),
            'restarts': [{k: u[k] for k in ('unit', 'why', 'seconds')} for u in plan['restarts']],
            'warnings': plan['warnings'], 'blockers': plan['blockers'], 'manual_steps': plan['manual_steps'],
            'confirm': plan['confirm'], 'generated': plan['generated'],
            **({'flatten': plan['flatten']} if plan.get('flatten') else {}),
            **({'login': {k: v for k, v in plan['login'].items() if k != 'dry_run'}} if plan.get('login') else {}),
            **({'coming_soon': plan['coming_soon']} if plan.get('coming_soon') else {}),
        }

    def _make_plan(self, state, ctype, params, cid):
        """plans.make plus the order adapter's checks for Flatten now (token here, login synced)."""
        plan = plans.make(state, ctype, params, cid, self.store)
        if ctype in COMING_SOON:
            plan['coming_soon'] = {'coming_soon': True, 'text': COMING_SOON[ctype]}
        if plan.get('action') == 'flatten' and hasattr(self.orders, 'readiness'):
            try:
                blockers, warnings = self.orders.readiness(state.account(params['account']))
            except Exception as exc:  # noqa: BLE001 - a broken check never lets a flatten through
                blockers, warnings = [f'Flatten check failed ({type(exc).__name__}).'], []
            plan['blockers'] += blockers
            plan['warnings'] += warnings
        if ctype == 'add_account' and isinstance(params, dict) and isinstance(params.get('login'), str):
            try:   # a login this page added: every other daemon must see it as a peer first
                plan['blockers'] += self.loginops.peer_blockers(state, params['login'])
            except Exception as exc:  # noqa: BLE001
                plan['blockers'].append(f'Peer check failed ({type(exc).__name__}).')
        if plan.get('action') in loginops.ACTIONS:
            try:
                data, _, _ = self.status.read()
                self.loginops.inspect(state, plan, self.store, data)
            except Exception as exc:  # noqa: BLE001 - a broken check never lets a login change through
                plan['blockers'].append(f'Login check failed ({type(exc).__name__}: {str(exc)[:160]}).')
        return plan

    def flat_report(self, keys, fresh=False):
        """({key: True/False/None}, error). None = unknown, which never counts as flat."""
        cards, error, _ = self.status.cards(fresh)
        return {k: (None if error else adapters.card_flat(cards.get(k))) for k in keys}, error

    def signin_wait(self, plan):
        """Logins a structural change must wait for: a daemon in its plan is logged out or not synced.
        A daemon is never restarted then (it could not finish starting). Read-only; never signs in."""
        fn = getattr(self.restarter, 'signin_wait', None)
        if not plan['restarts'] or fn is None:
            return []
        try:
            return fn(plan['restarts'])
        except Exception as exc:  # noqa: BLE001 - unknown means wait, never restart blind
            return [{'unit': None, 'login': None, 'label': 'a login', 'verdict': 'unknown',
                     'detail': f'sign-in check failed ({type(exc).__name__})'}]

    def cooldown(self, plan):
        """Units of this change that do not have 2 systemd starts left (apply + restore)."""
        fn = getattr(self.restarter, 'start_budget', None)
        if not plan['restarts'] or fn is None:
            return []
        try:
            return fn(plan['restarts'])
        except Exception as exc:  # noqa: BLE001 - unknown means wait
            return [{'unit': 'unknown', 'burst': 0, 'interval_s': 0, 'used': 0, 'free_in_s': None,
                     'error': f'{type(exc).__name__}'}]

    def ready(self, plan, fresh=True):
        """(apply_now, flat, error, signin, cooldown) for a structural change."""
        flat, error = self.flat_report(plan['wait_on'], fresh=fresh)
        signin = self.signin_wait(plan)
        cool = [] if signin else self.cooldown(plan)
        go = not error and all(v is True for v in flat.values()) and not signin and not cool
        return go, flat, error, signin, cool

    def eta(self, state, plan, flat=None, error=None, signin=None, cool=None):
        if plan['blockers']:
            return {'kind': 'blocked', 'seconds': None, 'text': 'Blocked: ' + plan['blockers'][0], 'waiting_on': []}
        if plan['final_status'] == 'manual':
            return {'kind': 'manual', 'seconds': None, 'text': 'You do the listed steps; nothing changes here.',
                    'waiting_on': []}
        if plan.get('eta'):
            return dict(plan['eta'])
        if plan['immediate'] or not plan['requires_flat']:
            text = 'Right away.' if plan['final_status'] != 'planned' else 'Saved right away as planned.'
            return {'kind': 'now' if plan['final_status'] != 'planned' else 'planned', 'seconds': 2, 'text': text,
                    'waiting_on': []}
        secs = sum(u['seconds'] for u in plan['restarts']) + plans.VERIFY_S
        if flat is None:
            flat, error = self.flat_report(plan['wait_on'])
        if signin is None:
            signin = self.signin_wait(plan)
        not_flat = [k for k, v in flat.items() if v is not True] if not error else list(plan['wait_on'])
        down = [x for x in signin if x['verdict'] == 'down']
        if down:
            units = ', '.join(x['unit'] for x in down)
            return {'kind': 'blocked', 'seconds': None, 'waiting_on': not_flat, 'reason': 'daemon_down',
                    'waiting_for_signin': [],
                    'text': f'{units} is down: needs a person. The change waits and applies on its own once the '
                            f'daemon runs and is synced.'}
        if signin:
            labels = ', '.join(dict.fromkeys(x['label'] for x in signin))
            flat_text = ' and every affected account is flat' if not_flat else ''
            return {'kind': 'after_signin', 'seconds': None, 'waiting_on': not_flat, 'reason': 'signin',
                    'waiting_for_signin': [x['label'] for x in signin],
                    'text': f'Waits for Tradovate sign-in ({labels}). Applies after the next sign-in, once the '
                            f'bridge is synced{flat_text}, then about {secs} s. Nothing signs in from here.'}
        if error:
            return {'kind': 'blocked', 'seconds': None, 'waiting_on': plan['wait_on'],
                    'text': 'Can\'t confirm the accounts are flat: the Bridge status feed is unavailable. '
                            'The change waits and never assumes flat.'}
        unknown = [k for k, v in flat.items() if v is None]
        busy = [k for k, v in flat.items() if v is False]
        if not busy and not unknown:
            if cool is None:
                cool = self.cooldown(plan)
            if cool:
                wait = max((c['free_in_s'] or 0) for c in cool)
                detail = '; '.join(f'{c["unit"]} started {c["used"]} of {c["burst"]} times allowed in '
                                   f'{c["interval_s"] / 60:.0f} min' for c in cool if c.get('burst'))
                return {'kind': 'cooldown', 'seconds': round(wait) + secs or None, 'waiting_on': [],
                        'reason': 'cooldown', 'cooldown': cool,
                        'text': f'Cooling down: restart limit ({detail or "limit unknown"}). Applies on its own in '
                                f'about {round(wait / 60) or 1} min, so a failed restart can still be undone.'}
            return {'kind': 'now', 'seconds': secs, 'waiting_on': [],
                    'text': f'Everyone affected is flat: about {secs} s once you confirm.'}
        names = ', '.join(state.accounts[k]['label'] for k in busy + unknown if k in state.accounts)
        if busy:
            text = (f'When the trade on {names} closes (stops and exits stay on), then about {secs} s. '
                    f'At the latest after the strategy\'s end-of-day exit.')
        else:
            text = f'Waiting to see {names} flat in the Bridge status (no reading yet), then about {secs} s.'
        return {'kind': 'after_flat', 'seconds': None, 'text': text, 'waiting_on': busy + unknown}

    # ----- API operations -----
    def preview(self, ctype, params):
        with self.lock:
            cid = time.strftime('c-%Y%m%d-%H%M%S-', time.gmtime(self.clock())) + secrets.token_hex(2)
            state = self.state()
            plan = self._make_plan(state, ctype, params, cid)
            ch = {'id': cid, 'type': ctype, 'params': params, 'status': 'previewed', 'created_at': self.clock(),
                  'updated_at': self.clock(), 'summary': plan['summary'], 'plan': self._public_plan(plan),
                  'preview_hash': self._hash(state, ctype, params, plan), 'eta': self.eta(state, plan),
                  'result': None, 'error': None, 'final_status': plan['final_status']}
            self.store.put(ch)
            return ch

    def get(self, cid):
        ch = self.store.get(cid)
        if not ch:
            raise KeyError(cid)
        return ch

    def confirm(self, cid, preview_hash, confirm_text=None):
        with self.lock:
            ch = self.get(cid)
            if ch['status'] == 'previewed' and self.clock() - ch['created_at'] > PREVIEW_TTL_S:
                ch['status'] = 'expired'
                self.store.put(ch)
            if ch['status'] != 'previewed':
                raise Conflict('not_previewed', f'This change is {ch["status"]}. Preview it again.')
            state = self.state()
            try:
                plan = self._make_plan(state, ch['type'], ch['params'], cid)
            except Invalid as exc:
                raise Conflict('invalid_now', str(exc))
            if self._hash(state, ch['type'], ch['params'], plan) != preview_hash or preview_hash != ch['preview_hash']:
                raise Conflict('stale_preview', 'Something changed since this preview. Preview it again.')
            if plan['blockers']:
                raise Conflict('blocked', plan['blockers'][0])
            if plan['confirm']['required'] and (confirm_text or '').strip() != plan['confirm']['expect']:
                raise Conflict('confirm_text', f'Type {plan["confirm"]["expect"]} exactly to confirm.')
            steps = []
            if plan['pre_ops']:
                self.files.apply(plan['pre_ops'])
                steps.append(self._step('pause first', True, 'paused before waiting for flat'))
                ch['pre_applied'] = True
            ch['result'] = {'ok': None, 'steps': steps, 'rolled_back': False}
            if plan['final_status'] == 'manual':
                ch['status'] = 'manual'
                ch['result']['ok'] = True
                self.store.put(ch)
                self.audit.add(ch, self.p)
                return ch
            if plan['immediate'] or not plan['requires_flat']:
                return self._apply(ch, state, plan)
            go, flat, error, signin, cool = self.ready(plan)
            if go:
                return self._apply(ch, state, plan)
            ch['status'] = 'waiting_flat'   # also sign-in/cooldown: same waiter, same cancel; eta.kind says which
            ch['eta'] = self.eta(state, plan, flat, error, signin, cool)
            self.store.put(ch)
            self.audit.add(ch, self.p)
            return ch

    def cancel(self, cid):
        with self.lock:
            ch = self.get(cid)
            if ch['status'] not in ('previewed', 'waiting_flat'):
                raise Conflict('not_cancellable', f'This change is {ch["status"]}.')
            was_waiting = ch['status'] == 'waiting_flat'
            ch['status'] = 'cancelled'
            self.store.put(ch)
            if was_waiting:
                self.audit.add(ch, self.p)
            return ch

    def rollback_preview(self, cid):
        return self.preview('rollback', {'change': cid})

    def _step(self, name, ok, detail):
        return {'name': name, 'ok': ok, 'detail': detail, 'at': self.clock()}

    def _apply(self, ch, state, plan):
        """Preflight → backup → write → restart → verify. Any failure after the backup restores every file
        and runs the same restart sequence on the old files. If that fails too, the change is failed and
        marked "needs a person" with the units. Nothing retries in a loop."""
        if plan.get('action') in loginops.ACTIONS:
            return self._apply_login(ch, state, plan)
        ch['status'] = 'applying'
        self.store.put(ch)
        res = ch['result']
        steps = res['steps']
        ops = plan['ops']
        manifest = None
        services_touched = False
        if plan['restarts']:
            try:
                problems, detail = self.restarter.preflight(plan['restarts'])
            except Exception as exc:  # noqa: BLE001 - a broken check never lets the change through
                problems, detail = [f'The service check itself failed ({type(exc).__name__}: {str(exc)[:120]}).'], None
            if problems or detail:
                steps.append(self._step('preflight', not problems, '; '.join(problems) if problems else detail))
            if problems:
                ch['status'] = 'failed'
                ch['error'] = ('Not applied: ' + problems[0] + ' Nothing was written and no service was restarted.')[:300]
                res['ok'] = False
                ch['eta'] = {'kind': 'now', 'seconds': None, 'text': 'Not applied: services were not ready.',
                             'waiting_on': []}
                self.store.put(ch)
                self.audit.add(ch, self.p)
                return ch
        refused_before = None
        if plan['restarts'] and hasattr(self.restarter, 'refused_lanes'):
            refused_before = self.restarter.refused_lanes()
            res['refused_lanes_before'] = refused_before
        try:
            self.files.check(ops)
            manifest = self.files.backup(ops, self.p.st('backups', ch['id']))
            steps.append(self._step('backup', True, f'{len(manifest)} path(s) saved'))
            if plan['action'] == 'flatten':
                acct = dict(state.account(ch['params']['account']))
                r = self._send_flatten(ch, plan, acct)
                if not r['ok']:
                    raise RuntimeError(r['detail'])
            self.files.apply(ops)
            steps.append(self._step('write', True, f'{len(ops)} change(s) written'))
            if plan['restarts']:
                services_touched = True
                results = self._restart(ch, plan, 'apply')
                bad = [r['unit'] for r in results if r['ok'] is False]
                steps.append(self._step('restart', not bad, '; '.join(f'{r["unit"]}: {r["detail"]}' for r in results)))
                if bad:
                    raise RuntimeError('restart failed: ' + ', '.join(dict.fromkeys(bad)))
            expected = self.files.expected(ops)
            wrong = [self.p.rel(p) for p, want in expected.items() if want is not None and file_sha(p) != want]
            health = self.restarter.health(plan['restarts']) if plan['restarts'] else {}
            unhealthy = [u for u, ok in health.items() if not ok]
            newly_refused = []
            if refused_before is not None:
                # A lane the firing service refused at its new start fails the change, unless it is the
                # account this change just added (it starts paused and may not be pinned yet).
                exempt = {ch['params'].get('key')} if ch['type'] == 'add_account' else set()
                after = self.restarter.refused_lanes()
                res['refused_lanes_after'] = after
                newly_refused = sorted(k for k in after if k not in refused_before and k not in exempt)
            ok = not wrong and not unhealthy and not newly_refused
            steps.append(self._step('verify', ok, 'files and services check out' if ok else
                                    'mismatch: ' + ', '.join(wrong + unhealthy +
                                                             [f'lane {k} refused at start ({after[k]})'
                                                              for k in newly_refused])))
            if not ok:
                raise RuntimeError('verify failed')
            ch['status'] = ch.get('final_status') if ch.get('final_status') in ('planned',) else 'applied'
            res['ok'] = True
            ch['backup_manifest'] = manifest
            ch['expected_after'] = expected
            if ch['type'] == 'rollback':
                old = self.store.get(ch['params']['change'])
                if old:
                    old['status'] = 'rolled_back'
                    self.store.put(old)
        except Exception as exc:  # noqa: BLE001 - every failure path restores the backup
            ch['status'] = 'failed'
            ch['error'] = str(exc)[:300]
            res['ok'] = False
            if manifest is not None:
                self._restore(ch, plan, manifest, services_touched)
        if res['ok']:
            text = 'Done.'
        elif res.get('flatten'):
            text = res['flatten']['detail']
        elif res.get('needs_person'):
            text = res['needs_person']['text']
        else:
            text = 'Failed and rolled back.'
        ch['eta'] = {'kind': 'now', 'seconds': None, 'text': text, 'waiting_on': []}
        self.store.put(ch)
        self.audit.add(ch, self.p)
        return ch

    def _apply_login(self, ch, state, plan):
        """Add / Start / Remove login (loginops.py). Same record as every change: preflight of the restarts,
        steps saved as they happen, an exact undo on any failure, audit. "Needs a person" when the undo or
        the restore restart did not finish."""
        ch['status'] = 'applying'
        self.store.put(ch)
        res = ch['result']
        steps = res['steps']
        if plan['restarts']:
            try:
                problems, detail = self.restarter.preflight(plan['restarts'])
            except Exception as exc:  # noqa: BLE001 - a broken check never lets the change through
                problems, detail = [f'The service check itself failed ({type(exc).__name__}: {str(exc)[:120]}).'], None
            if problems or detail:
                steps.append(self._step('preflight', not problems, '; '.join(problems) if problems else detail))
            if problems:
                ch['status'] = 'failed'
                ch['error'] = ('Not applied: ' + problems[0] + ' Nothing was written and no service was touched.')[:300]
                res['ok'] = False
                ch['eta'] = {'kind': 'now', 'seconds': None, 'text': 'Not applied: services were not ready.',
                             'waiting_on': []}
                self.store.put(ch)
                self.audit.add(ch, self.p)
                return ch

        def step(name, ok, detail):
            steps.append(self._step(name, ok, detail))
            self.store.put(ch)
        try:
            res['login'] = self.loginops.apply(ch, state, plan, step,
                                               lambda units, phase: self._restart(ch, plan, phase))
            ch['status'] = 'applied'
            res['ok'] = True
            text = res['login']['text']
            if ch['type'] == 'rollback':
                old = self.store.get(ch['params']['change'])
                if old:
                    old['status'] = 'rolled_back'
                    self.store.put(old)
        except Exception as exc:  # noqa: BLE001 - loginops undid what it could; record it
            ch['status'] = 'failed'
            ch['error'] = str(exc)[:300]
            res['ok'] = False
            res['rolled_back'] = any(s['name'] == 'rollback' and s['ok'] for s in steps) and \
                not isinstance(exc, loginops.UndoIncomplete)
            bad = [s for s in steps if s['name'] in ('rollback', 'restart old files') and s['ok'] is False]
            undone = any(s['name'] == 'rollback' for s in steps)
            text = f'Failed and undone: {ch["error"]}' if undone else f'Failed; nothing was changed: {ch["error"]}'
            if bad or isinstance(exc, loginops.UndoIncomplete):
                lid = plan['login']['id']
                paths = getattr(exc, 'paths', []) or []
                units = (getattr(exc, 'units', []) or list(loginops.units_of(lid).values())) + \
                    [s['detail'].split(':')[0] for s in bad if s['name'] == 'restart old files']
                notes = ([f'Not restored: {", ".join(paths)}.'] if paths else []) + \
                    [f'Login record: {self.p.rel(loginops.record_path(self.p, lid))}; Remove login {plan["login"]["label"]} '
                     f'cleans up what it lists.']
                self._needs_person(res, units, f'the {plan["action"].replace("_", " ")} change failed and was not '
                                               f'fully undone.', notes)
                text = res['needs_person']['text']
        ch['eta'] = {'kind': 'now', 'seconds': None, 'text': text, 'waiting_on': []}
        self.store.put(ch)
        self.audit.add(ch, self.p)
        return ch

    def _send_flatten(self, ch, plan, acct):
        """One flatten call; the full reply is kept on the change (and in the audit). Never retried here."""
        res = ch['result']
        reason = (plan.get('flatten') or {}).get('reason') or 'Flatten now (Bridge Advanced)'
        res['flatten_attempts'] = res.get('flatten_attempts', 0) + 1
        try:
            r = self.orders.flatten(acct, ch['id'], reason=reason)
        except Exception as exc:  # noqa: BLE001 - never success
            r = {'ok': False, 'outcome_class': 'unknown', 'unknown': True, 'reply': None, 'dry_run': False,
                 'detail': f'Unknown outcome: the call failed here ({type(exc).__name__}). Check the Bridge window.'}
        r['kill_switch_after'] = read_switch(acct['kill_switch_file'])   # the pause, as the page shows it
        res['flatten'] = r
        self._step_into(res, 'flatten', r['ok'], r['detail'])
        return r

    def _step_into(self, res, name, ok, detail):
        res['steps'].append(self._step(name, ok, detail))

    def resend_flatten(self, cid, confirm_text=None):
        """Send a flatten again with the SAME change id, only after an unknown outcome. The bridge answers a
        repeated change id from its journal (duplicate) and sends nothing twice. Typed confirmation again."""
        with self.lock:
            ch = self.get(cid)
            fl = (ch.get('result') or {}).get('flatten') or {}
            if ch['type'] != 'flatten' or ch['status'] != 'failed' or fl.get('outcome_class') != 'unknown':
                raise Conflict('not_resendable', 'Only a Flatten now with an unknown outcome can be sent again.')
            state = self.state()
            acct = dict(state.account(ch['params']['account']))
            if (confirm_text or '').strip() != acct['label']:
                raise Conflict('confirm_text', f'Type {acct["label"]} exactly to confirm.')
            plan = self._make_plan(state, 'flatten', ch['params'], cid)
            if plan['blockers']:
                raise Conflict('blocked', plan['blockers'][0])
            r = self._send_flatten(ch, plan, acct)
            ch['status'] = 'applied' if r['ok'] else 'failed'
            ch['result']['ok'] = r['ok']
            ch['error'] = None if r['ok'] else r['detail'][:300]
            ch['eta'] = {'kind': 'now', 'seconds': None, 'text': 'Done.' if r['ok'] else r['detail'],
                         'waiting_on': []}
            self.store.put(ch)
            self.audit.add(ch, self.p)
            return ch

    def _restart(self, ch, plan, phase, **kw):
        """Run the restarter, saving each unit's result on the change as it happens (the page polls it)."""
        res = ch['result']
        log = res.setdefault('restart_log', [])

        def progress(r):
            log.append(dict(r, phase=phase))
            self.store.put(ch)
        n = len(log)
        results = self.restarter.restart(plan['restarts'], ch['id'] + ('' if phase == 'apply' else '-' + phase),
                                         progress=progress, phase=phase, **kw)
        seen = len(log) - n
        for r in results[seen:]:  # results the restarter did not report through progress (e.g. not reached)
            log.append(dict(r, phase=phase))
        return results

    def _firing_state(self):
        fn = getattr(self.restarter, 'unit_state', None)
        try:
            return fn('tvbridge-firing.service') if fn else 'active'
        except Exception:  # noqa: BLE001
            return 'unknown'

    def _restore(self, ch, plan, manifest, services_touched):
        res, steps = ch['result'], ch['result']['steps']
        bad_units, notes = [], []
        try:
            self.files.restore(manifest)
            res['rolled_back'] = True
            steps.append(self._step('rollback', True, 'every file restored from the backup'))
        except Exception as exc:  # noqa: BLE001
            steps.append(self._step('rollback', False, f'restore failed: {exc}'))
            bad_units.append('files: ' + ', '.join(self.p.rel(e['path']) for e in manifest))
        if services_touched and plan['restarts']:
            attempted = {r['unit'] for r in res.get('restart_log', [])
                         if r.get('phase') == 'apply' and r.get('action') == 'restart' and r.get('ok') is not None
                         and r['unit'].startswith('tvbridge-daemon')}
            # Re-check flat before any daemon restart of the restore run.
            flat, error = self.flat_report(plan['wait_on'], fresh=True)
            not_flat = [k for k, v in flat.items() if v is not True] if not error else list(plan['wait_on'])
            keep = None
            if not_flat:
                keep = ('not flat at restore time (' + (', '.join(not_flat) if not error else 'status feed down')
                        + ')')
            try:
                results = self._restart(ch, plan, 'rollback', attempted=attempted, keep_daemons=keep)
                bad = list(dict.fromkeys(r['unit'] for r in results if r['ok'] is False))
                notes += [f'{r["unit"]} {r["detail"]}.' for r in results
                          if r.get('action') == 'skipped' and r['ok'] is False]
                if keep:
                    for u in sorted(attempted):
                        bad.append(u)
                        notes.append(f'{u} was restarted with the new files and was not restarted back: {keep}. '
                                     f'It runs the new files until a person restarts it when flat.')
                if not bad:
                    health = self.restarter.health(plan['restarts'])
                    skipped = {r['unit'] for r in results if r.get('action') == 'skipped'}
                    bad = [u for u, ok in health.items() if not ok and u not in skipped]
                steps.append(self._step('restart old files', not bad,
                                        '; '.join(f'{r["unit"]}: {r["detail"]}' for r in results)))
            except Exception as exc:  # noqa: BLE001
                bad = [u['unit'] for u in plan['restarts']]
                steps.append(self._step('restart old files', False, f'{type(exc).__name__}: {exc}'))
            bad_units += list(dict.fromkeys(bad))
        if bad_units:
            self._needs_person(res, bad_units, 'the change failed and the services did not all come back on the '
                                               'old files.', notes)

    def _needs_person(self, res, units, why, notes=(), backup=None):
        firing = self._firing_state()
        firing_down = firing != 'active'
        units = list(dict.fromkeys(list(units) + (['tvbridge-firing.service'] if firing_down else [])))
        text = f'Needs a person: {why} Check ' + ', '.join(units) + '.'
        text += (f' The firing service is {firing}: followers will not fire until this is fixed.' if firing_down
                 else ' The firing service is running.')
        if backup:
            text += f' Backup for a manual restore: {backup}.'
        text = ' '.join([text] + list(notes) + ['Nothing will retry on its own.'])
        res['needs_person'] = {'units': units, 'firing_stopped': firing_down, 'firing_state': firing, 'text': text}
        if backup:
            res['needs_person']['backup'] = backup

    def recover_interrupted(self):
        """At start: a change left `applying` means the service died mid-sequence. Mark it failed and
        "needs a person" with the backup path. Nothing is restored or restarted automatically."""
        for ch in [c for c in self.store.all() if c['status'] == 'applying']:
            res = ch.get('result') or {'ok': None, 'steps': [], 'rolled_back': False}
            ch['result'] = res
            res['ok'] = False
            backup = self.p.st('backups', ch['id'])
            units = [u['unit'] for u in (ch.get('plan') or {}).get('restarts', [])]
            login = (ch.get('plan') or {}).get('login') or {}
            rec = loginops.read_record(self.p, login.get('id')) if login else None
            if rec:
                # Add/Remove login: its login record lists every path and unit; Remove login cleans it up.
                if rec.get('state') in ('adding', 'removing'):
                    rec['state'] = 'interrupted' if rec['state'] == 'adding' else rec['state']
                    loginops.write_record(self.p, rec, self.clock)
                originals = [m['original'] for m in rec.get('modifies') or [] if m.get('original')]
                self._needs_person(
                    res, list(dict.fromkeys(rec.get('units', []) + units)),
                    f'the admin service stopped while this {ch["type"].replace("_", " ")} change was applying '
                    f'(last step: {(rec.get("steps") or ["none"])[-1]}). Nothing was undone automatically.',
                    [f'Login record {self.p.rel(loginops.record_path(self.p, rec["id"]))} lists every path it may have '
                     f'created ({", ".join(self.p.rel(x) for x in rec.get("creates", []))}) and modified '
                     f'({", ".join(self.p.rel(m["path"]) for m in rec.get("modifies", []))}); originals: '
                     f'{", ".join(self.p.rel(x) for x in originals) or "none"}.',
                     f'To undo: Remove login {rec.get("label")} (id {rec["id"]}).'])
                res['steps'].append(self._step('interrupted', False, 'service restarted mid-change; see the login record'))
                ch['status'] = 'failed'
                ch['error'] = 'Interrupted: the admin service stopped mid-change.'
                ch['eta'] = {'kind': 'now', 'seconds': None, 'text': res['needs_person']['text'], 'waiting_on': []}
                self.store.put(ch)
                self.audit.add(ch, self.p)
                continue
            self._needs_person(res, units, 'the admin service stopped while this change was applying, so files and '
                                           'services may be half changed. It was not restored automatically.',
                               backup=backup if os.path.isdir(backup) else 'none (stopped before the backup)')
            res['steps'].append(self._step('interrupted', False, 'service restarted mid-change; marked for a person'))
            ch['status'] = 'failed'
            ch['error'] = 'Interrupted: the admin service stopped mid-change.'
            ch['eta'] = {'kind': 'now', 'seconds': None, 'text': res['needs_person']['text'], 'waiting_on': []}
            self.store.put(ch)
            self.audit.add(ch, self.p)

    # ----- waiting for flat -----
    def tick(self):
        """Apply waiting changes whose accounts are all flat now. Re-plans on the current config."""
        with self.lock:
            for ch in [c for c in self.store.all() if c['status'] == 'previewed']:
                if self.clock() - ch['created_at'] > PREVIEW_TTL_S:
                    ch['status'] = 'expired'
                    self.store.put(ch)
            used = set()   # units restarted in this pass: at most one restarting change per unit per pass
            for ch in reversed([c for c in self.store.all() if c['status'] == 'waiting_flat']):  # oldest first
                state = self.state()
                try:
                    plan = plans.make(state, ch['type'], ch['params'], ch['id'], self.store)
                except Invalid as exc:
                    ch['status'], ch['error'] = 'failed', 'No longer possible: ' + str(exc)
                    ch['result'] = ch.get('result') or {'ok': False, 'steps': [], 'rolled_back': False}
                    ch['result']['ok'] = False
                    self.store.put(ch)
                    self.audit.add(ch, self.p)
                    continue
                plan['pre_ops'] = []  # already applied at confirm
                if plan['blockers']:
                    ch['eta'] = self.eta(state, plan)
                    self.store.put(ch)
                    continue
                units = {u['unit'] for u in plan['restarts']}
                if units & used:
                    continue   # next pass, after its start budget is re-checked
                go, flat, error, signin, cool = self.ready(plan)
                if go:
                    used |= units
                    new_plan = self._public_plan(plan)
                    if new_plan['files'] != ch['plan']['files']:
                        ch['result']['steps'].append(self._step('re-planned', True,
                                                                'config changed while waiting; same request '
                                                                'applied to the current config'))
                        ch['plan'] = new_plan
                    self._apply(ch, state, plan)
                else:
                    ch['eta'] = self.eta(state, plan, flat, error, signin, cool)
                    self.store.put(ch)

    def run_waiter(self, every=5.0):
        def loop():
            while not self._stop.wait(every):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 - keep waiting; next tick retries
                    pass
        t = threading.Thread(target=loop, name='wait-for-flat', daemon=True)
        t.start()
        return t

    def stop(self):
        self._stop.set()

    # ----- overview -----
    def overview(self):
        state = self.state()
        data, error, at = self.status.read()
        cards, _, _ = self.status.cards()
        market = ((data or {}).get('soup') or {}).get('market_session') or {}
        guard = (data or {}).get('guard') or {}
        hold = guard.get('reason') == 'weekend_maintenance_hold'
        status_logins = {lg.get('id'): lg for lg in (data or {}).get('logins') or []}
        accounts = []
        for key, a in state.accounts.items():
            role, w = state.role(key)
            order = None
            if w:
                order = 0 if role == 'leader' else w['followers'].index(key) + 1
            sw = read_switch(a['kill_switch_file'])
            card = cards.get(key)
            flat = None if error else adapters.card_flat(card)
            pos = (card or {}).get('position') or {}
            st = (card or {}).get('status') or {}
            # Same rule as the Bridge window: a pause outranks "needs a look"/linked/off, never a fault.
            fault = st.get('state') in ('blocked', 'not_linked', 'unknown')
            if sw['on'] and not fault:
                status = {'label': 'Paused by safety' if sw['set_by'] == 'system' else 'Paused',
                          'tone': 'red' if sw['set_by'] == 'system' else 'amber'}
            else:
                status = {'label': st.get('label') or ('No reading' if not card else '—'), 'tone': st.get('tone', '')}
            enabled = a['daemon_enabled']
            if w and w['id'] == 'main' and role == 'follower':
                enabled = state.firing['accounts'].get(key, {}).get('enabled', True)
            elif w and role == 'follower':
                enabled = key not in w.get('disabled', [])
            accounts.append({
                'key': key, 'label': a['label'], 'login': a['login'], 'login_label': a['login_label'],
                'role': role, 'webhook': w['id'] if w else None, 'order': order, 'enabled': enabled,
                'prop_profile': a['prop_profile'],
                'account_name_masked': mask(a['account_name']), 'account_id': a['account_id'],
                'native_exits': a['native_exits'], 'pause': sw,
                'position': {'net': pos.get('net'), 'flat': flat, 'text': pos.get('text') or '—',
                             'orders_text': ((card or {}).get('orders') or {}).get('text') or '—'},
                'status': status})
        webhooks = []
        strategies = {s['id']: s for s in self.p.strategies}
        for w in state.webhooks:
            leader = state.accounts.get(w['leader'])
            wstate = w.get('state', 'live')
            off = wstate == 'off'
            if w['id'] == 'main':
                pause = read_switch(leader['kill_switch_file']) if leader else None
                soup = {'unit': self.p.soup_unit(), 'port': self.p.soup_port(),
                        'enabled': state.control.get('enabled'), 'locked': state.control.get('locked')}
            elif off:   # staged elsewhere as templates; never installed or probed from here
                pause = None
                soup = {'unit': self.p.soup_unit(w['id']), 'port': w.get('port'),
                        'enabled': False, 'locked': True, 'installed': False}
            else:
                pause = w.get('paused')
                soup = {'unit': self.p.soup_unit(w['id']), 'port': w.get('port'),
                        'enabled': None, 'locked': None}
            notes = []
            if off:
                notes.append(OFF_NOTE)
            elif stream_strength(w['stream_id']) == 'weak':
                notes.append(f'Stream ID {w["stream_id"]} is easy to guess. Rotate it when the accounts are flat.')
            if wstate == 'planned':
                notes.append('Planned: it starts working after the multi-lane publish step.')
            st = strategies.get(w.get('strategy')) or {}
            path = w.get('path')   # .get: one bad registry entry must never break the whole overview
            webhooks.append({
                'id': w['id'], 'name': w.get('name') or w['id'], 'path': path,
                'url': f'https://{self.p.public_host}{path}' if path else None, 'url_active': wstate == 'live',
                'state': wstate, 'status_text': WEBHOOK_STATUS_TEXT.get(wstate, wstate),
                'strategy': {'id': w.get('strategy'), 'name': st.get('name')},
                'stream_id': w['stream_id'], 'stream_id_strength': None if off else stream_strength(w['stream_id']),
                'ticker': w['ticker'], 'leader': w['leader'], 'followers': w['followers'],
                'paused': bool(pause and pause.get('on', True)), 'pause': pause if pause and pause.get('on', True) else None,
                'soup': soup, 'notes': notes,
                'coming_soon': {k: {'coming_soon': True, 'text': COMING_SOON[k]}
                                for k in (['rotate_stream_id'] + (['activate_webhook'] if wstate != 'live' else []))}})
        changes = self.store.all()
        try:
            setup = self.loginops.view(state, changes)
        except Exception as exc:  # noqa: BLE001 - the page still loads
            setup = {'available': False, 'reason': f'Login check failed ({type(exc).__name__}).', 'next_id': None,
                     'logins': {}}
        logins = []
        for lg in state.logins:
            sl = status_logins.get(lg['id']) or {}
            cfg = state.login_cfg[lg['id']][1]
            logins.append({'id': lg['id'], 'label': lg.get('label', lg['id']), 'primary': bool(lg.get('primary')),
                           'mode': cfg.get('mode', 'main'), 'state_label': sl.get('state_label') or 'No reading',
                           'tone': sl.get('tone', ''),
                           'accounts': [k for k, a in state.accounts.items() if a['login'] == lg['id']],
                           'setup': setup['logins'].get(lg['id'])})
        return {
            'mode': self.p.mode, 'generated_at': time.time(),
            'market': {'state': market.get('state', 'unknown'), 'reason': market.get('reason'),
                       'weekend_hold': hold,
                       'weekend_hold_text': hold_text(guard) if hold else None},
            'status_source': {'ok': error is None, 'age_s': round(time.time() - at, 1), 'error': error},
            'webhooks': webhooks, 'accounts': accounts, 'logins': logins,
            'strategies': [{k: s.get(k) for k in ('id', 'name', 'build', 'contract_ref', 'inputs_count',
                                                   'stream_field', 'notes')} for s in self.p.strategies],
            'pending': [c for c in changes if c['status'] in ('waiting_flat', 'applying')],
            # failed changes whose restore restart also failed: red "needs a person" with the units
            'needs_person': [{'change_id': c['id'], 'summary': c['summary'], 'at': c['updated_at'],
                              'units': c['result']['needs_person']['units'],
                              'firing_stopped': bool(c['result']['needs_person'].get('firing_stopped')),
                              'firing_state': c['result']['needs_person'].get('firing_state'),
                              'backup': c['result']['needs_person'].get('backup'),
                              'text': c['result']['needs_person']['text']}
                             for c in changes[:50] if c['status'] == 'failed'
                             and (c.get('result') or {}).get('needs_person')],
            'restarter': self.restarter.name,
            'login_setup': {k: setup.get(k) for k in ('available', 'reason', 'next_id', 'incomplete')},
            'coming_soon': dict({k: {'coming_soon': True, 'text': t} for k, t in COMING_SOON.items()},
                                **({} if setup.get('available') else {'add_login': {
                                    'coming_soon': True,
                                    'text': 'Add login is coming soon on this install. Until then the Add login '
                                            'preview lists the steps to do by hand.'}})),
            'export': {'available': True, 'url': '/api/export'},
            'recent': [c for c in changes if c['status'] in FINISHED and c['status'] != 'cancelled'][:10],
            'drift': state.drift,
        }

    def export(self):
        """Export whole setup (export.py): read only, one audit entry. Returns (bytes, filename, manifest)."""
        import export as exporter
        with self.lock:
            data, name, manifest = exporter.Exporter(self).build(self.clock)
            self.audit.add({'id': 'x-' + time.strftime('%Y%m%d-%H%M%S', time.gmtime(self.clock())), 'type': 'export',
                            'status': 'done', 'summary': f'Export whole setup: {len(manifest["files"])} files, {name} '
                                                         f'(sha256 {sha(data)[:16]}…)',
                            'plan': {'files': []}, 'result': None}, self.p)
        return data, name, manifest

    def visible(self, login):
        """Accounts Tradovate shows on a login (names come from the read-only panel's /login reading)."""
        state = self.state()
        if login not in state.login_cfg:
            raise KeyError(login)
        data, error, _ = self.status.read()
        known = {a['account_name']: k for k, a in state.accounts.items()}
        out = []
        for lg in (data or {}).get('logins') or []:
            if lg.get('id') == login:
                for v in lg.get('visible') or []:
                    name = v.get('name')
                    out.append({'name': name, 'already_configured': name in known, 'key': known.get(name)})
        return {'login': login, 'accounts': out, 'error': error,
                'note': None if out else 'No reading: the login must be signed in for Tradovate to list accounts.'}
