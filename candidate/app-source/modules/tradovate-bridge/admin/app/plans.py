"""Turn a requested change into a plan: file operations, restarts, who must be flat, and checks.

Nothing here writes a file. `make(state, type, params)` returns a Plan; the engine applies it.
Rules kept from the bridge:
- the strategy alert sets the size; there is no contract limit here (the retired `max_contracts` key is never
  written, and a legacy positive value is ignored); size_policy stays "payload"
- leaders are fixed: never moved, retired or turned off here
- one webhook per account; the Main leader stays first and on the primary login
- structural changes wait until every account whose service restarts is flat
- this page never places or cancels orders, except the confirmed per-account Flatten now
"""
import glob
import os
import re
import time

from model import (PROP_PROFILES, Draft, Invalid, KEY_RE, LANE_PORT_BASE, legacy_max_contracts_problem, new_stream_id,
                   new_webhook_path, slug, stream_problem, stream_strength, validate)
import loginops
from tree import admin_note, read_switch, utc_stamp

IMMEDIATE = {'pause', 'resume', 'pause_webhook', 'resume_webhook', 'flatten'}
SWITCH_TYPES = {'pause', 'resume', 'pause_webhook', 'resume_webhook'}
SECONDS = {'daemon': 15, 'firing': 5, 'status': 3, 'soup': 20}
VERIFY_S = 5


class Plan(dict):
    """summary, immediate, requires_flat, affected, wait_on, ops, pre_ops, restarts, warnings, blockers,
    manual_steps, confirm, generated, final_status, action"""


def _plan(**kw):
    p = Plan(immediate=False, requires_flat=False, affected=[], wait_on=[], ops=[], pre_ops=[], restarts=[],
             warnings=[], blockers=[], manual_steps=[], confirm={'required': False, 'expect': None, 'prompt': None},
             generated={}, final_status='applied', action=None)
    p.update(kw)
    return p


def _text(params, name, limit=80, required=True):
    v = params.get(name)
    if v is None and not required:
        return None
    if not isinstance(v, str) or not v.strip():
        raise Invalid(f'Please fill in {name.replace("_", " ")}.')
    v = v.strip()
    if len(v) > limit:
        raise Invalid(f'{name.replace("_", " ").capitalize()} is too long (max {limit} characters).')
    return v


def _restarts_for(state, ops):
    """Units whose config changed, in a safe order: daemons (login registry order, primary first), then
    firing, then the read-only panel. The firing service is in the list whenever a daemon restarts: the
    live restarter stops it first so no follower fires mid-restart, so every account it fires for must be
    flat too (see _wait_on)."""
    units = []
    touched = {op['path'] for op in ops if op['action'] == 'write'}
    for lg in state.logins:
        if state.login_cfg[lg['id']][0] in touched:
            units.append({'unit': lg.get('daemon_unit', f'tvbridge-daemon@{lg["id"]}.service'),
                          'why': f'{lg.get("label", lg["id"])} reads its account list at start',
                          'seconds': SECONDS['daemon'], 'login': lg['id']})
    if state.firing_path in touched or units:
        why = 'reads the firing order at start' if state.firing_path in touched else \
            'stopped while a bridge daemon restarts, so no follower fires mid-restart'
        units.append({'unit': 'tvbridge-firing.service', 'why': why, 'seconds': SECONDS['firing'], 'login': None})
    if state.p.soup_unit() and any(state.p.soup_state in p for p in touched):
        units.append({'unit': state.p.soup_unit(), 'why': 'reads its pairing at start',
                      'seconds': SECONDS['soup'], 'login': None})
    if units:
        units.append({'unit': 'tvbridge-status.service', 'why': 'refreshes the Bridge window\'s account list',
                      'seconds': SECONDS['status'], 'login': None})
    return units


def _wait_on(state, restarts, affected):
    """Every account a restart can touch must be flat: all accounts on a restarted daemon, and the whole
    Main webhook when the firing service or Soup restarts. Plus the accounts the change itself edits."""
    keys = set(affected)
    for u in restarts:
        if u['login']:
            keys |= {k for k, a in state.accounts.items() if a['login'] == u['login']}
        elif u['unit'] == 'tvbridge-firing.service' or state.p.is_soup_unit(u['unit']):
            main = state.webhook('main')
            keys |= {main['leader'], *main['followers']}
    return sorted(k for k in keys if k in state.accounts)


def _finish(state, draft, plan, validate_draft=True):
    plan['ops'] = draft.ops() if draft else plan['ops']
    if validate_draft and draft:
        plan['warnings'] += validate(draft)
    if not plan['immediate'] and plan['final_status'] == 'applied':
        plan['restarts'] = _restarts_for(state, plan['ops'])
        plan['requires_flat'] = bool(plan['restarts'])  # registry-only edits touch nothing live
        plan['wait_on'] = _wait_on(state, plan['restarts'], plan['affected']) if plan['restarts'] else []
    return plan


def _is_off(w):
    """A registered webhook that is off: no strategy, no accounts, no stream ID; nothing runs on it."""
    return bool(w) and w.get('state') == 'off'


def _off_refusal(w, what):
    return Invalid(f'{w.get("name") or w["id"]} is off and waiting for a strategy. {what}')


def _follower_in(state, key):
    role, w = state.role(key)
    if role == 'leader':
        raise Invalid(f'{state.accounts[key]["label"]} leads {w["name"]}. Leaders are fixed.')
    return role, w


def _remove_member(draft, key, wid):
    if wid == 'main':
        f = draft.firing
        f['firing_order'] = [k for k in f['firing_order'] if k != key]
        f['accounts'].pop(key, None)
    elif wid:
        w = draft.reg_webhook(wid)
        w['followers'] = [k for k in w.get('followers', []) if k != key]
        w['disabled'] = [k for k in w.get('disabled', []) if k != key]


def _add_member(draft, state, key, wid, index=None, prop_profile=None, label=None, login=None):
    if wid is None:
        return
    a = state.accounts.get(key, {})
    login = login or a.get('login')
    if wid == 'main':
        f = draft.firing
        entry = {'role': 'follower', 'label': label or a.get('label', key), 'enabled': True,
                 'prop_profile': prop_profile or a.get('prop_profile'), 'size_policy': 'payload'}
        if login != state.primary_login():
            entry['login'] = login
        f['accounts'][key] = entry
        order = f['firing_order']
        pos = len(order) if index is None else max(1, min(len(order), 1 + int(index)))
        order.insert(pos, key)
    else:
        w = draft.reg_webhook(wid)
        if _is_off(w):
            raise _off_refusal(w, 'It can\'t take accounts yet.')
        if w.get('state') != 'planned':
            raise Invalid('That webhook cannot take accounts.')
        fl = w.setdefault('followers', [])
        fl.insert(len(fl) if index is None else max(0, min(len(fl), int(index))), key)


# ---------------- change types ----------------

def p_pause(state, params, change_id):
    key = _text(params, 'account', 40)
    a = state.account(key)
    reason = _text(params, 'reason', 200)
    sw = read_switch(a['kill_switch_file'])
    plan = _plan(immediate=True, affected=[key], summary=f'Pause {a["label"]}: {reason}')
    if not sw.get('path_ok', True):
        plan['blockers'].append(f'{a["label"]} is already paused: {sw["reason"]}')
    elif sw['on']:
        plan['blockers'].append(f'{a["label"]} is already paused.')
    plan['ops'] = [{'path': a['kill_switch_file'], 'action': 'write', 'content': admin_note(reason, change_id)}]
    role, w = state.role(key)
    if role == 'leader':
        plan['warnings'].append(f'{a["label"]} leads {w["name"]}. Followers enter only after the leader, so '
                                f'this pauses the whole webhook.')
    plan['warnings'].append('Stops new entries. An open trade finishes with its stops and exits.')
    return plan


def p_resume(state, params, change_id):
    key = _text(params, 'account', 40)
    a = state.account(key)
    sw = read_switch(a['kill_switch_file'])
    plan = _plan(immediate=True, affected=[key], summary=f'Resume {a["label"]}')
    if legacy_max_contracts_problem(a.get('legacy_max_contracts')):
        # Never arm an account whose config the bridge refuses to start with (a malformed retired value).
        plan['blockers'].append(f'{a["label"]}: its config has a malformed max_contracts value, so the bridge does '
                                f'not start with it. max_contracts is retired (the strategy alert sets the size); '
                                f'remove it from the config first.')
    if not sw['on']:
        plan['blockers'].append(f'{a["label"]} is not paused.')
    elif not sw.get('path_ok', True):
        plan['blockers'].append(f'{a["label"]} stays paused: {sw["reason"]}')
    else:
        plan['ops'] = [{'path': a['kill_switch_file'], 'action': 'rename',
                        'to': f'{a["kill_switch_file"]}.removed-{utc_stamp()}'}]
    if sw['set_by'] == 'system':
        plan['confirm'] = {'required': True, 'expect': a['label'],
                           'prompt': f'A safety event (for example a native-exit failure or a liquidation) or a '
                                     f'manual step set this pause, not this page. Check {a["label"]} in Tradovate '
                                     f'first. Type {a["label"]} to resume.'}
    if state.member.get(key) is None:
        plan['warnings'].append(f'{a["label"]} is not in a webhook, so it will not trade yet.')
    elif state.webhook(state.member[key]).get('state') == 'planned':
        plan['warnings'].append('Its webhook is planned and not running yet.')
    return plan


def p_pause_webhook(state, params, change_id):
    w = state.webhook(_text(params, 'webhook', 40))
    if not w:
        raise Invalid('There is no such webhook.')
    if _is_off(w):
        raise _off_refusal(w, 'It can\'t be paused; nothing runs on it.')
    reason = _text(params, 'reason', 200, required=False) or 'Webhook paused'
    if w.get('state') == 'planned':
        d = Draft(state)
        d.reg_webhook(w['id'])['paused'] = {'set_by': 'admin', 'reason': reason, 'at': time.time()}
        plan = _plan(immediate=True, summary=f'Pause webhook {w["name"]}', affected=[])
        return _finish(state, d, plan, validate_draft=False)
    plan = p_pause(state, {'account': w['leader'], 'reason': reason}, change_id)
    plan['summary'] = f'Pause webhook {w["name"]} (leader {state.accounts[w["leader"]]["label"]})'
    return plan


def p_resume_webhook(state, params, change_id):
    w = state.webhook(_text(params, 'webhook', 40))
    if not w:
        raise Invalid('There is no such webhook.')
    if _is_off(w):
        raise _off_refusal(w, 'It can\'t be resumed; nothing runs on it.')
    if w.get('state') == 'planned':
        d = Draft(state)
        d.reg_webhook(w['id'])['paused'] = None
        plan = _plan(immediate=True, summary=f'Resume webhook {w["name"]}')
        return _finish(state, d, plan, validate_draft=False)
    plan = p_resume(state, {'account': w['leader']}, change_id)
    plan['summary'] = f'Resume webhook {w["name"]} (leader {state.accounts[w["leader"]]["label"]})'
    return plan


def p_flatten(state, params, change_id):
    key = _text(params, 'account', 40)
    a = state.account(key)
    reason = _text(params, 'reason', 200, required=False) or 'Flatten now (Bridge Advanced)'
    plan = _plan(immediate=True, affected=[key], action='flatten',
                 summary=f'Flatten now: {a["label"]} (close position, cancel working orders, then pause)')
    plan['flatten'] = {'reason': ' '.join(reason.split()), 'key': 'main' if a.get('daemon_role') == 'main' else key,
                       'login': a['login'], 'also_blocks': []}
    plan['confirm'] = {'required': True, 'expect': a['label'],
                       'prompt': f'Closes the position and cancels working orders for {a["label"]} now, then '
                                 f'pauses it. Type {a["label"]} to confirm.'}
    bridge = state.p.order_adapter == 'bridge'
    if not bridge and not read_switch(a['kill_switch_file'])['on']:
        # Dry run: the page sets the pause itself. Through the bridge, the bridge sets it first (admin note).
        plan['ops'] = [{'path': a['kill_switch_file'], 'action': 'write',
                        'content': admin_note('Paused after Flatten now', change_id)}]
    role, w = state.role(key)
    if a.get('daemon_role') == 'main' or role == 'leader':
        also = [k for k in (w['followers'] if w else []) if k in state.accounts]
        # followers on other logins also stop when their master_kill_switch_file is this leader's switch
        for lg in state.logins:
            cfg = state.login_cfg[lg['id']][1]
            if cfg.get('master_kill_switch_file') == a['kill_switch_file']:
                also += [f.get('key') for f in cfg.get('followers') or [] if f.get('key') not in also]
        plan['flatten']['also_blocks'] = also
        names = ', '.join(state.accounts[k]['label'] for k in also if k in state.accounts) or 'none'
        plan['warnings'].append(f'This is a leader. Its pause also blocks new entries on: {names}. Their open '
                                f'trades keep their own stops and exits; flatten them separately if you want them '
                                f'out too. Soup sees the close as an outside close and stops managing that trade.')
    if not bridge:
        plan['warnings'].append('Build preview: the close is recorded as a dry run and nothing is sent.')
    else:
        plan['warnings'].append('Sends one flatten to the bridge for this account only. It is never retried on its '
                                'own; an unknown outcome means: check the Bridge window.')
    return plan


def p_set_enabled(state, params, change_id):
    key = _text(params, 'account', 40)
    a = state.account(key)
    enabled = params.get('enabled')
    if not isinstance(enabled, bool):
        raise Invalid('enabled must be true or false.')
    role, w = _follower_in(state, key)
    if role == 'unassigned':
        raise Invalid(f'{a["label"]} is not in a webhook.')
    if _is_off(w):
        raise _off_refusal(w, 'Its lanes can\'t be turned on or off.')
    d = Draft(state)
    if w['id'] == 'main':
        d.firing['accounts'][key]['enabled'] = enabled
    else:
        rw = d.reg_webhook(w['id'])
        dis = [k for k in rw.get('disabled', []) if k != key]
        rw['disabled'] = dis if enabled else dis + [key]
    plan = _plan(affected=[key], summary=f'{a["label"]}: lane {"on" if enabled else "off"} in {w["name"]}')
    return _finish(state, d, plan)


def p_reorder(state, params, change_id):
    w = state.webhook(_text(params, 'webhook', 40))
    if not w:
        raise Invalid('There is no such webhook.')
    if _is_off(w):
        raise _off_refusal(w, 'It has no accounts to reorder.')
    new = params.get('followers')
    if not isinstance(new, list) or sorted(new) != sorted(w['followers']) or len(set(new)) != len(new):
        raise Invalid('The new order must list each follower of this webhook once.')
    if new == w['followers']:
        raise Invalid('That is already the firing order.')
    d = Draft(state)
    if w['id'] == 'main':
        d.firing['firing_order'] = [w['leader']] + new
    else:
        d.reg_webhook(w['id'])['followers'] = list(new)
    labels = ' → '.join(state.accounts[k]['label'] for k in new)
    plan = _plan(affected=list(new), summary=f'{w["name"]} firing order: leader → {labels}')
    return _finish(state, d, plan)


def p_move_account(state, params, change_id):
    key = _text(params, 'account', 40)
    a = state.account(key)
    to = params.get('to_webhook')
    role, src = _follower_in(state, key)
    dest = state.webhook(to) if to else None
    if to and not dest:
        raise Invalid('There is no such webhook.')
    if _is_off(dest):
        raise _off_refusal(dest, 'It can\'t take accounts yet.')
    if (src and src['id']) == to:
        raise Invalid('It is already there. Use reorder to change its place.')
    d = Draft(state)
    _remove_member(d, key, src['id'] if src else None)
    _add_member(d, state, key, to, params.get('index'))
    plan = _plan(affected=[key], summary=f'Move {a["label"]}: {src["name"] if src else "Not assigned"} → '
                                         f'{dest["name"] if dest else "Not assigned"}')
    if dest and dest.get('state') == 'planned':
        plan['warnings'].append(f'{dest["name"]} is planned and not running yet, so {a["label"]} will not trade '
                                f'until it is activated.')
    if not dest:
        plan['warnings'].append(f'{a["label"]} will not trade while it is not assigned.')
    return _finish(state, d, plan)


def _account_paths(state, login, key):
    cfg_path = state.login_cfg[login][0]
    base = state.p.tvbridge_root if login == state.primary_login() else os.path.dirname(cfg_path)
    return os.path.join(base, f'journal-{key}'), os.path.join(base, f'ENTRIES-DISABLED-{key}')


def p_add_account(state, params, change_id):
    login = _text(params, 'login', 16)
    if login not in state.login_cfg:
        raise Invalid('There is no such login.')
    key = _text(params, 'key', 32)
    if not KEY_RE.match(key):
        raise Invalid('Key must start with a letter and use lowercase letters, digits and hyphens.')
    if key in state.accounts or key.startswith('main-'):
        raise Invalid(f'Key {key} is already used.')
    label = _text(params, 'label', 40)
    if any(a['label'] == label for a in state.accounts.values()):
        raise Invalid(f'Label {label} is already used.')
    name = _text(params, 'account_name', 40)
    acct_id = params.get('account_id')
    if acct_id is not None and (type(acct_id) is not int or acct_id <= 0):
        raise Invalid('Account id must be the number Tradovate shows, or left empty for the bridge to look up.')
    profile = params.get('prop_profile')
    if profile not in PROP_PROFILES:
        raise Invalid('Prop profile must be one of ' + ', '.join(PROP_PROFILES) + '.')
    # A `max_contracts` an older caller still sends is ignored and never written: the strategy alert sets the size.
    wid = params.get('webhook')
    w = state.webhook(wid) if wid else None
    if wid and not w:
        raise Invalid('There is no such webhook.')
    journal, kill = _account_paths(state, login, key)
    d = Draft(state)
    d.login_cfg(login).setdefault('followers', []).append({
        'key': key, 'label': label, 'account_name': name, 'account_id': acct_id, 'journal_dir': journal,
        'kill_switch_file': kill, 'enabled': True, 'prop_profile': profile,
        'native_exits_enabled': True})
    _add_member(d, state, key, wid, prop_profile=profile, label=label, login=login)
    firing_dir = state.firing.get('firing_dir')
    d.extra += [{'path': journal, 'action': 'mkdir'},
                {'path': kill, 'action': 'write', 'content': admin_note('New account starts paused', change_id)}]
    if firing_dir and wid == 'main':
        d.extra.append({'path': os.path.join(firing_dir, key), 'action': 'mkdir'})
    plan = _plan(affected=[key], summary=f'Add {label} (login {login}) to {w["name"] if w else "Not assigned"}, paused')
    plan['warnings'].append('New accounts start paused. Resume it when you are ready.')
    return _finish(state, d, plan)


def p_retire_account(state, params, change_id):
    key = _text(params, 'account', 40)
    a = state.account(key)
    role, w = _follower_in(state, key)
    d = Draft(state)
    _remove_member(d, key, w['id'] if w else None)
    cfg = d.login_cfg(a['login'])
    cfg['followers'] = [f for f in cfg.get('followers') or [] if f.get('key') != key]
    plan = _plan(affected=[key], summary=f'Retire {a["label"]}: pause, wait for flat, remove from '
                                         f'{w["name"] if w else "its login"}; journals kept')
    if not read_switch(a['kill_switch_file'])['on']:
        plan['pre_ops'] = [{'path': a['kill_switch_file'], 'action': 'write',
                            'content': admin_note('Retiring', change_id)}]
        plan['warnings'].append('It is paused as soon as you confirm, and stays paused if you cancel.')
    plan['warnings'].append(f'Its journal folder and pause switch file stay in place for the records.')
    return _finish(state, d, plan)


def _new_port(state):
    used = {w.get('port') for w in state.registry.get('webhooks', [])}
    port = LANE_PORT_BASE
    while port in used:
        port += 1
    return port


def _new_webhook_id(state, params):
    """(name, id) for a new webhook. Refuses `main` and a name or id already in the registry (any state)."""
    name = _text(params, 'name', 40)
    wid = slug(name)
    if wid == 'main' or state.webhook(wid) or \
            any((w.get('name') or '').lower() == name.lower() for w in state.webhooks):
        raise Invalid(f'A webhook named {name} already exists.')
    return name, wid


def p_register_webhook(state, params, change_id):
    """Register a webhook that is off: a reserved port and path, no strategy, no accounts, no stream ID.
    Writes only the admin registry. Nothing runs and no alert is accepted until a strategy is approved."""
    name, wid = _new_webhook_id(state, params)
    path = new_webhook_path(wid, change_id)
    port = _new_port(state)
    d = Draft(state)
    d.registry['webhooks'].append({
        'id': wid, 'name': name, 'strategy': None, 'path': path, 'state': 'off', 'stream_id': None,
        'ticker': None, 'leader': None, 'followers': [], 'disabled': [], 'paused': None,
        'port': port, 'created_at': time.time(), 'change_id': change_id})
    plan = _plan(summary=f'Register webhook {name} (off, waiting for a strategy)')
    plan['generated'] = {'path': path, 'url': f'https://{state.p.public_host}{path}', 'port': port}
    plan['warnings'].append(f'{name} stays off. Nothing runs and no alert is accepted until you approve a '
                            f'strategy for it.')
    return _finish(state, d, plan)


def p_add_webhook(state, params, change_id):
    name, wid = _new_webhook_id(state, params)
    strategies = {s['id']: s for s in state.p.strategies}
    sid = params.get('strategy')
    if sid not in strategies:
        raise Invalid('Choose a strategy from the library.')
    leader = _text(params, 'leader', 40)
    a = state.account(leader)
    role, src = state.role(leader)
    if role == 'leader':
        raise Invalid(f'{a["label"]} already leads {src["name"]}.')
    ticker = params.get('ticker') or state.pairing.get('ticker')
    stream = params.get('stream_id') or params.get('_previewed_stream_id') or new_stream_id()
    if stream_problem(stream):
        raise Invalid(stream_problem(stream))
    path = new_webhook_path(wid, change_id)
    url = f'https://{state.p.public_host}{path}'
    d = Draft(state)
    if src:
        _remove_member(d, leader, src['id'])
    d.registry['webhooks'].append({
        'id': wid, 'name': name, 'strategy': sid, 'path': path, 'state': 'planned', 'stream_id': stream,
        'ticker': ticker, 'leader': leader, 'followers': [], 'disabled': [], 'paused': None,
        'port': _new_port(state), 'created_at': time.time(), 'change_id': change_id})
    plan = _plan(affected=[leader], summary=f'Add webhook {name} ({strategies[sid]["name"]}), leader {a["label"]}',
                 final_status='planned')
    plan['generated'] = {'stream_id': stream, 'path': path, 'url': url}
    plan['manual_steps'] = [
        f'In TradingView, open {strategies[sid]["name"]} settings → "🔌 Soup v3 connection" and paste '
        f'{stream} into "📡 Alert stream ID".',
        f'Set the alert\'s webhook URL to {url}.',
        'Activation needs the multi-lane publish step (its own Soup instance and firing lane). You approve it.']
    if a['login'] != state.primary_login():
        plan['warnings'].append(f'{a["label"]} is on {a["login_label"]}, which runs followers only today. '
                                f'Activating it as a leader needs that login switched to main mode.')
    if src:
        plan['warnings'].append(f'{a["label"]} leaves {src["name"]} to lead the new webhook.')
    plan['ops'] = d.ops()
    plan['warnings'] += validate(d)
    if src and src['id'] == 'main':
        plan['restarts'] = _restarts_for(state, plan['ops'])
        plan['requires_flat'] = True
        plan['wait_on'] = _wait_on(state, plan['restarts'], [leader])
    return plan


def p_remove_webhook(state, params, change_id):
    w = state.webhook(_text(params, 'webhook', 40))
    if not w:
        raise Invalid('There is no such webhook.')
    plan = _plan(summary=f'Remove webhook {w.get("name") or w["id"]}', affected=[w['leader']] if w['leader'] else [])
    if w['id'] == 'main':
        plan['blockers'].append('The Main webhook cannot be removed.')
        return plan
    if w['followers']:
        plan['blockers'].append('Move its followers to another webhook first.')
    d = Draft(state)
    d.registry['webhooks'] = [x for x in d.registry['webhooks'] if x['id'] != w['id']]
    if _is_off(w):
        plan['warnings'].append('It is off, so nothing else changes. Its port and path are freed.')
    else:
        plan['warnings'].append(f'Its leader moves to Not assigned. Its stream ID stops being accepted.')
    plan['ops'] = d.ops()
    return plan


def p_rotate_stream_id(state, params, change_id):
    w = state.webhook(_text(params, 'webhook', 40))
    if not w:
        raise Invalid('There is no such webhook.')
    if _is_off(w):
        raise _off_refusal(w, 'It gets a stream ID when a strategy is approved.')
    new = params.get('_previewed_stream_id') or new_stream_id()
    if w.get('state') == 'planned':
        d = Draft(state)
        d.reg_webhook(w['id'])['stream_id'] = new
        plan = _plan(immediate=True, summary=f'{w["name"]}: new stream ID')
        plan['generated'] = {'stream_id': new}
        plan['manual_steps'] = [f'Paste {new} into the strategy\'s "📡 Alert stream ID" in TradingView.']
        plan['ops'] = d.ops()
        return plan
    members = [w['leader']] + w['followers']
    plan = _plan(summary=f'{w["name"]}: change stream ID {w["stream_id"]} → {new}', final_status='manual',
                 affected=members, wait_on=sorted(members), requires_flat=True)
    plan['generated'] = {'stream_id': new}
    plan['manual_steps'] = [
        'Wait until every account in this webhook is flat with no working orders.',
        f'In TradingView, set "📡 Alert stream ID" to {new} on the strategy and re-create its alert.',
        f'Soup pairing change (offline, needs your approval): stop Soup, back up its state folder, '
        f'set stream_id to {new} in pairing.json, start it, check /health, then send no test orders.',
        'Until both sides match, Soup rejects the alerts as a stream mismatch, which is the safe failure.']
    plan['warnings'].append(f'Stream ID {w["stream_id"]} is {stream_strength(w["stream_id"])}. Soup checks it '
                            f'on every alert, so a random ID keeps other people\'s alerts out.')
    return plan


LOGIN_LABEL_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,39}\Z')
LOGIN_LETTERS = 'bcdefghijklmnopqrstuvwxyz'


def next_login_id(state):
    """Next free one-letter login id. A letter is never reused while anything of an earlier login with that
    id is left (its folder, a removed-* backup, its Flatten-now token file)."""
    taken = {lg['id'] for lg in state.logins}
    for c in LOGIN_LETTERS:
        if c in taken:
            continue
        if glob.glob(state.p.tb('logins', c)) + glob.glob(state.p.tb('logins', c + '.removed-*')) + \
                glob.glob(state.p.tb('logins', c + '.failed-*')) + glob.glob(state.p.tb('logins', f'manual-{c}.env*')):
            continue
        rec = loginops.read_record(state.p, c)
        if rec and rec.get('state') not in ('undone',):
            continue          # a login record of this page (created, removed or not finished) keeps its letter
        return c
    raise Invalid('No free login letter is left (b to z).')


def _login_entry(state, params, record_ok=False):
    lid = _text(params, 'login', 16)
    entry = next((lg for lg in state.logins if lg['id'] == lid), None)
    if not entry and record_ok:
        # Remove may clean up an add that stopped before logins.json had the entry (its login record).
        rec = loginops.read_record(state.p, lid)
        if rec and rec.get('state') in loginops.INCOMPLETE:
            return lid, {'id': lid, 'label': rec.get('label')}, rec.get('label') or f'Login {lid.upper()}'
    if not entry:
        raise Invalid('There is no such login.')
    if entry.get('primary') or lid == state.primary_login():
        raise Invalid(f'{entry.get("label", lid)} is the primary login. It is never added, started or removed here.')
    return lid, entry, entry.get('label') or f'Login {lid.upper()}'


def _p_add_login_manual(state, params, change_id):
    """Installs without `login_setup` in their profile keep the earlier manual plan (steps for the operator)."""
    label = _text(params, 'label', 40)
    lid = slug(label, 16).replace('login-', '') or 'c'
    taken = set(state.login_cfg)
    if lid in taken:
        for c in 'cdefghijklmnopqrstuvwxyz':
            if c not in taken:
                lid = c
                break
    plan = _plan(summary=f'Add login {label} (id {lid})', final_status='manual')
    script = os.path.join(state.p.tvbridge_root, 'current', 'scripts', 'install.py')
    cli = f'python3 {script} add-login {lid} --label "{label}"'
    plan['manual_steps'] = [
        f'Preview: {cli} --dry-run',
        f'Apply: {cli}',
        'Start its browser, then sign in to Tradovate yourself in the new window (never during the weekend '
        'login hold).',
        'Back here, use Add account to pick accounts from the new login.']
    plan['generated'] = {'login_id': lid}
    return plan


def p_add_login(state, params, change_id):
    """Guided Add login: the engine's login step runs install.py add-login (dry run first) and the unit
    pieces login B has (loginops.py). Nothing is restarted but the read-only status panel."""
    if not state.p.data.get('login_setup'):
        return _p_add_login_manual(state, params, change_id)
    label = _text(params, 'label', 40)
    if not LOGIN_LABEL_RE.match(label):
        raise Invalid('Use letters, digits, spaces and . _ ( ) + - for the login name.')
    if any((lg.get('label') or '').strip().lower() == label.lower() for lg in state.logins):
        raise Invalid(f'A login named {label} already exists.')
    lid = next_login_id(state)
    plan = _plan(summary=f'Add {label} (login {lid}): its files, units and its own Tradovate window', affected=[],
                 action='add_login')
    plan['login'] = {'id': lid, 'label': label}
    plan['generated'] = {'login_id': lid}
    plan['restarts'] = [{'unit': 'tvbridge-status.service', 'login': None, 'seconds': SECONDS['status'],
                         'why': f'the Bridge window loads {label}\'s read-only token at start, so it can show it'}]
    plan['requires_flat'] = False      # no account or trading service is touched
    plan['eta'] = {'kind': 'now', 'seconds': 15, 'waiting_on': [],
                   'text': f'About 15 s once you confirm. Then sign in to Tradovate yourself in the new {label} window.'}
    plan['warnings'] += [
        f'Opens {label}\'s own Tradovate window. You sign in there by hand; nothing here signs in.',
        f'Its bridge starts only after your sign-in (the next step, Start). Daemons A and B, the firing service '
        f'and Soup are not restarted.',
        f'{label} starts with no accounts. Accounts you add to it later start paused.',
        f'Login A\'s config lists {label} as a peer for the cross-login hedge check. Daemon A reads that at its next '
        f'restart; until then its check does not include {label}, which has no accounts yet.',
        'The Bridge window\'s data service restarts once (about 3 s) so it can read the new login.']
    plan['manual_steps'] = [f'Sign in to Tradovate in the {label} window (never during the weekend login hold).',
                            f'Back here, press Start for {label}.',
                            f'Then use Add account to pick accounts from {label} (they start paused).']
    return plan


def p_start_login(state, params, change_id):
    lid, entry, label = _login_entry(state, params)
    plan = _plan(immediate=True, action='start_login', affected=[],
                 summary=f'Start {label}\'s bridge after your sign-in')
    plan['login'] = {'id': lid, 'label': label}
    plan['eta'] = {'kind': 'now', 'seconds': 60, 'waiting_on': [],
                   'text': 'Usually under a minute once you are signed in.'}
    plan['warnings'] += [
        f'Starts {label}\'s login trigger and login guard. The guard starts its bridge daemon only when it sees '
        f'{label} signed in, so a signed-out daemon is never started.',
        f'Finish your sign-in first. If {label} is still signed out, the guard may click into the empty sign-in '
        f'form once (it looks for a saved login) and then waits for you.']
    return plan


def p_remove_login(state, params, change_id):
    lid, entry, label = _login_entry(state, params, record_ok=True)
    plan = _plan(action='remove_login', affected=[], summary=f'Remove {label} (login {lid}); a backup is kept')
    on_it = [a['label'] for a in state.accounts.values() if a['login'] == lid]
    if on_it:
        plan['blockers'].append(f'{label} still has accounts ({", ".join(on_it)}). Retire them first.')
    in_firing = [k for k, a in state.firing.get('accounts', {}).items() if a.get('login') == lid]
    if in_firing:
        plan['blockers'].append(f'firing.json still puts {", ".join(in_firing)} on {label}.')
    plan['login'] = {'id': lid, 'label': label}
    plan['confirm'] = {'required': True, 'expect': label,
                       'prompt': f'Stops {label}\'s window and bridge and removes it from the login registry. Its '
                                 f'files and browser profile are kept as a backup. Type {label} to confirm.'}
    plan['eta'] = {'kind': 'now', 'seconds': 10, 'waiting_on': [], 'text': 'About 10 s once you confirm.'}
    plan['warnings'] += [
        f'Stops and disables {label}\'s browser window, bridge daemon, login trigger and login guard.',
        f'No other service is restarted. Daemon A (and the firing service, if it knows {label}) keep asking '
        f'{label} until their next restart; a login that does not answer is skipped, never blocking.']
    return plan


def p_rollback(state, params, change_id, store=None):
    """Plan to undo an applied change from its backup. The engine passes the change store."""
    cid = _text(params, 'change', 64)
    old = store.get(cid) if store else None
    if not old:
        raise Invalid('There is no such change.')
    if old['status'] != 'applied' and old['status'] != 'planned':
        raise Invalid('Only applied changes can be rolled back.')
    if old['type'] in ('flatten', 'rollback', 'start_login', 'remove_login'):
        raise Invalid('This change cannot be rolled back.')
    if old['type'] == 'add_login':
        # Undo = Remove login (only while it has no accounts), with its own backup.
        lid = ((old.get('plan') or {}).get('login') or {}).get('id')
        plan = p_remove_login(state, {'login': lid}, change_id)
        plan['summary'] = f'Roll back: {old["summary"]}'
        plan['rollback_of'] = cid
        return plan
    manifest, expected = old.get('backup_manifest'), old.get('expected_after') or {}
    if not manifest:
        raise Invalid('This change has no backup to restore.')
    from tree import file_sha
    plan = _plan(summary=f'Roll back: {old["summary"]}', affected=list(old['plan'].get('affected', [])))
    for path, want in expected.items():
        if want is not None and file_sha(path) != want:
            plan['blockers'].append(f'{state.p.rel(path)} changed after that change. Roll back newer changes first.')
    ops = []
    for e in manifest:
        if e['sha'] == 'dir':
            continue
        if e['copy']:
            with open(e['copy']) as f:
                ops.append({'path': e['path'], 'action': 'write', 'content': f.read()})
        elif e['sha'] == 'absent' and os.path.isfile(e['path']):
            ops.append({'path': e['path'], 'action': 'delete'})
    plan['ops'] = ops
    plan['immediate'] = old['plan'].get('immediate', False)
    if not plan['immediate']:
        plan['restarts'] = _restarts_for(state, ops)
        plan['requires_flat'] = bool(plan['restarts'])
        plan['wait_on'] = _wait_on(state, plan['restarts'], plan['affected'])
    return plan


BUILDERS = {
    'pause': p_pause, 'resume': p_resume, 'pause_webhook': p_pause_webhook, 'resume_webhook': p_resume_webhook,
    'flatten': p_flatten, 'set_enabled': p_set_enabled,
    'reorder': p_reorder, 'move_account': p_move_account, 'add_account': p_add_account,
    'retire_account': p_retire_account, 'add_webhook': p_add_webhook, 'remove_webhook': p_remove_webhook,
    'register_webhook': p_register_webhook,
    'rotate_stream_id': p_rotate_stream_id, 'add_login': p_add_login, 'start_login': p_start_login,
    'remove_login': p_remove_login, 'rollback': p_rollback,
}


# Change types that no longer exist. The API refuses them with this sentence instead of "unknown".
RETIRED = {
    'set_max_contracts': 'The size cap is retired: the strategy alert sets the size.',
}


def make(state, ctype, params, change_id, store=None):
    if ctype in RETIRED:
        raise Invalid(RETIRED[ctype])
    if ctype not in BUILDERS:
        raise Invalid(f'Unknown change type "{ctype}".')
    if not isinstance(params, dict):
        raise Invalid('params must be an object.')
    if state.p.data.get('live_switches_only') and ctype not in SWITCH_TYPES:
        raise Invalid('This install allows only pause and resume for now. Other changes come with the next '
                      'publish step.')
    if ctype == 'rollback':
        return p_rollback(state, params, change_id, store)
    if ctype in ('add_webhook', 'rotate_stream_id') and store is not None:
        # Confirm rebuilds the plan: keep the random stream ID the preview showed (and the person copied).
        prev = (((store.get(change_id) or {}).get('plan') or {}).get('generated') or {}).get('stream_id')
        if isinstance(prev, str) and not stream_problem(prev):
            params = dict(params, _previewed_stream_id=prev)
    return BUILDERS[ctype](state, params, change_id)
