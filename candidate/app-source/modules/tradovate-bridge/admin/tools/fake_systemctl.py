#!/usr/bin/env python3
"""Fake `systemctl --user` for tvbridge-admin sandbox tests. Never runs the real systemctl.

    fake_systemctl.py --state STATE.json --user show|is-active|start|stop|restart UNIT [-p PROP ...]
    fake_systemctl.py --state STATE.json --user enable|disable|is-enabled UNIT | daemon-reload

STATE.json (the test writes it; this script updates it under a lock):
  units:      {unit: {active, sub, pid, nrestarts, result, mode}}   mode: synced | unsynced | waiting
  on_restart: {unit: [outcome, ...]}  consumed one per restart; the last one sticks. Outcomes:
              synced | unsynced (never syncs) | waiting (running, no /health answer: waiting for sign-in)
              | fail (restart exits 1, unit failed) | crash (runs, exits 0.3 s later; systemd restarts it)
  on_start:   {unit: [ok | bad_config | stale | refuse:<key>,...]}  for start (firing): bad_config = the
              service rejects its config and exits; stale = runs but never writes status.json;
              refuse:<keys> = runs but refuses those enabled lanes at start (status lanes[k].unavailable)
  limits:     {unit: [burst, seconds]} systemd start limits (defaults: daemons 3/600 s, firing 5/300 s,
              others 5/10 s); a start beyond the limit fails with result start-limit-hit, like systemd
  starts:     {unit: [t, ...]} every start, kept for the limit
  firing:     {config: firing.json path, status: firing/status.json path}
  calls:      [[verb, unit, at], ...] appended by every call
  unit_files: {unit: enabled | disabled} for enable/disable/is-enabled and show -p UnitFileState (default
              disabled); fail_verbs: {"verb unit": true} makes that one call exit 1 (tests of the undo paths)

While tvbridge-firing.service is active (outcome ok) every call rewrites the firing status file the way the
running service does (pid, updated_at, firing_order from firing.json), so freshness checks see a live
service. Soup units (soup_units, default ["example-soup"]: that name and its @instances) are refused
(exit 99) and recorded, so tests can assert they were never touched.
"""
import fcntl
import json
import os
import sys
import time

FIRING = 'tvbridge-firing.service'


def load(path):
    with open(path) as f:
        return json.load(f)


def save(path, st):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, path)


def pop(queue_map, unit, default):
    q = queue_map.get(unit)
    if not q:
        return default
    return q.pop(0) if len(q) > 1 else q[0]


def limit(st, unit):
    if unit in (st.get('limits') or {}):
        return tuple(st['limits'][unit])
    if unit.startswith('tvbridge-daemon'):
        return 3, 600
    return (5, 300) if unit == FIRING else (5, 10)


def over_limit(st, unit):
    burst, secs = limit(st, unit)
    now = time.time()
    starts = [t for t in st.setdefault('starts', {}).get(unit, []) if t > now - secs]
    if len(starts) >= burst:
        return True
    st['starts'][unit] = starts + [now]
    return False


def write_firing_status(st, unit_state):
    f = st.get('firing') or {}
    if not f.get('status') or unit_state.get('mode') != 'ok' or unit_state.get('active') != 'active':
        return
    try:
        cfg = load(f['config'])
    except (OSError, ValueError):
        return
    refused = set(unit_state.get('refused') or ())
    lanes = {}
    for k in cfg.get('firing_order', [])[1:]:
        enabled = cfg.get('accounts', {}).get(k, {}).get('enabled', True)
        lanes[k] = {'enabled': enabled, 'running': not (enabled and k in refused)}
        if enabled and k in refused:
            lanes[k]['unavailable'] = 'unavailable:configured Tradovate account is not visible in this login'
    status = {'schema': 'tvbridge.firing.v2', 'pid': unit_state['pid'], 'updated_at': int(time.time() * 1000),
              'firing_order': cfg.get('firing_order'), 'main': (cfg.get('firing_order') or [None])[0],
              'lanes': lanes}
    os.makedirs(os.path.dirname(f['status']), exist_ok=True)
    tmp = f['status'] + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(status, fh)
    os.replace(tmp, f['status'])


def main(argv):
    if len(argv) < 4 or argv[0] != '--state' or argv[2] != '--user':
        print('usage: fake_systemctl.py --state STATE.json --user VERB UNIT', file=sys.stderr)
        return 2
    path, verb = argv[1], argv[3]
    opts = [a for a in argv[4:] if a.startswith('--')]
    rest = [a for a in argv[4:] if not a.startswith('--')]
    unit = rest[0] if rest else ''
    with open(path + '.lock', 'w') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        st = load(path)
        st.setdefault('calls', []).append([verb, unit, time.time()] + ([opts] if opts else []))
        name = unit.split('@')[0]
        name = name[:-len('.service')] if name.endswith('.service') else name
        if unit and name in st.get('soup_units', ['example-soup']):
            st['soup_touched'] = True
            save(path, st)
            print('fake systemctl: Soup is off limits', file=sys.stderr)
            return 99
        if (st.get('fail_verbs') or {}).get(f'{verb} {unit}'.strip()):
            save(path, st)
            print(f'fake systemctl: {verb} {unit} failed (test)', file=sys.stderr)
            return 1
        if verb == 'daemon-reload':
            st['reloads'] = st.get('reloads', 0) + 1
            save(path, st)
            return 0
        files = st.setdefault('unit_files', {})
        if verb in ('enable', 'disable'):
            files[unit] = verb + 'd'
            save(path, st)
            return 0
        if verb == 'is-enabled':
            save(path, st)
            sys.stdout.write(files.get(unit, 'disabled') + '\n')
            return 0 if files.get(unit) == 'enabled' else 1
        units = st.setdefault('units', {})
        u = units.setdefault(unit, {'active': 'inactive', 'sub': 'dead', 'pid': 0, 'nrestarts': 0,
                                    'result': 'success', 'mode': 'synced'})
        code, out = 0, ''

        def run_new(mode):
            st['next_pid'] = st.get('next_pid', 5000) + 1
            u.pop('refused', None)
            u.update(active='active', sub='running', pid=st['next_pid'], result='success', mode=mode,
                     active_enter=time.time())

        if u.get('mode') == 'crash' and time.time() >= u.get('crash_at', 0):
            st['next_pid'] = st.get('next_pid', 5000) + 1
            u.update(mode='crash-looping', active='activating', sub='auto-restart', pid=st['next_pid'],
                     nrestarts=u['nrestarts'] + 1)
        if verb == 'show':
            props = [rest[i + 1] for i in range(len(rest) - 1) if rest[i] == '-p']
            burst, secs = limit(st, unit)
            vals = {'ActiveState': u['active'], 'SubState': u['sub'], 'MainPID': u['pid'],
                    'NRestarts': u['nrestarts'], 'Result': u['result'], 'StartLimitBurst': burst,
                    'StartLimitIntervalUSec': f'{secs // 60}min' if secs % 60 == 0 else f'{secs}s',
                    'UnitFileState': files.get(unit, 'disabled'),
                    'ActiveEnterTimestamp': (f'@{int(u["active_enter"])}' if u.get('active_enter') else '')
                    if '--timestamp=unix' in opts else ''}
            out = ''.join(f'{p}={vals.get(p, "")}\n' for p in props)
        elif verb == 'is-active':
            out = u['active'] + '\n'
            code = 0 if u['active'] == 'active' else 3
        elif verb == 'stop':
            u.update(active='inactive', sub='dead', pid=0)
        elif verb in ('start', 'restart') and over_limit(st, unit):
            u.update(active='failed', sub='failed', pid=0, result='start-limit-hit')
            code = 1
            print(f'Job for {unit} failed: start request repeated too quickly (start-limit-hit).', file=sys.stderr)
        elif verb in ('start', 'restart'):
            if unit == FIRING and verb == 'start' and '--job-mode=ignore-requirements' not in opts:
                st['firing_started_with_requirements'] = True
            if unit == FIRING or verb == 'start':
                outcome = pop(st.setdefault('on_start', {}), unit, 'ok')
            else:
                outcome = pop(st.setdefault('on_restart', {}), unit, 'synced')
            if outcome == 'fail':
                u.update(active='failed', sub='failed', pid=0, result='exit-code')
                code, out = 1, ''
                print(f'Job for {unit} failed because the control process exited with error code.',
                      file=sys.stderr)
            elif outcome == 'bad_config':
                u.update(active='failed', sub='failed', pid=0, result='exit-code', mode='bad_config')
            elif outcome == 'crash':
                run_new('crash')       # starts, then exits a moment later and systemd restarts it
                u['crash_at'] = time.time() + 0.3
            elif outcome == 'stale':
                run_new('stale')
            elif outcome.startswith('refuse:'):
                run_new('ok')
                u['refused'] = outcome[len('refuse:'):].split(',')
            else:
                run_new(outcome)
        else:
            code = 1
            print(f'fake systemctl: unsupported verb {verb}', file=sys.stderr)
        write_firing_status(st, units.get(FIRING, {}))
        save(path, st)
    sys.stdout.write(out)
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
