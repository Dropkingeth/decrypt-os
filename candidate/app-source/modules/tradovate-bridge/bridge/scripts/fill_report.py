#!/usr/bin/env python3
"""Fill prices and slippage per trade, from the bridge journals (tvbridge 1.0.9.3). Read-only.

Reads each journal directory's `commands.jsonl` (the command journal) and `fills.jsonl`
(the fill-price sidecar the daemon writes from 1.0.9.3 on). Files are only opened for
reading; nothing is written, and neither the daemon nor Tradovate is contacted.

    python3 scripts/fill_report.py                           # ~/.local/share/decrypt-os/tvbridge: journal, journal-*
    python3 scripts/fill_report.py --trade 'ID:long'         # one trade
    python3 scripts/fill_report.py --last 3 --json           # machine-readable
    python3 scripts/fill_report.py --journal main=DIR --journal account-b=DIR2

Per trade and account it lists each command (kind, outcome, bridge detail), the order-level
price as journaled or as Tradovate's order showed it at the fill (limit, stop, or the entry's
reference quote), every recorded fill (time, qty, price, fill id), the average fill price and
the slippage in points per contract (positive = worse than the order-level price). Market
exits and plain market entries have no order-level price in the bridge journal, so their
slippage is n/a. Soup's exit reasons are not in the bridge journal.

Journals from before 1.0.9.3 have no `fills.jsonl`: the report says "no fill prices recorded".
Python 3.9+, standard library only.
"""
import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = os.environ.get('TVBRIDGE_ROOT') or '~/.local/share/decrypt-os/tvbridge'
COMMANDS = 'commands.jsonl'
FILLS = 'fills.jsonl'
EXIT_ROLES = ('stop', 'tp1', 'exit', 'flatten', 'align_flatten')


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def read_lines(path):
    """(records, skipped lines). Opened read-only; a missing file gives ([], 0)."""
    try:
        with open(path, 'rb') as fh:
            raw = fh.read()
    except FileNotFoundError:
        return [], 0
    records, skipped = [], 0
    for line in raw.split(b'\n'):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(rec, dict):
            records.append(rec)
        else:
            skipped += 1
    return records, skipped


def load_commands(directory):
    """The command journal as tvbridge.journal.Journal reads it (start, then result/reconciled)."""
    records, skipped = read_lines(Path(directory) / COMMANDS)
    commands = {}
    for rec in records:
        cid = rec.get('command_id')
        if not isinstance(cid, str):
            skipped += 1
            continue
        if rec.get('type') == 'start':
            commands[cid] = dict(rec, outcome=None)
        elif rec.get('type') in ('result', 'reconciled') and cid in commands:
            commands[cid].update(outcome=rec.get('outcome'), broker_order_id=rec.get('broker_order_id'),
                                 receipt=rec.get('receipt'))
            if 'legs' in rec:
                commands[cid]['legs'] = rec['legs']
    for c in commands.values():
        if c['outcome'] is None:
            c['outcome'] = 'unknown'
    return commands, skipped


def load_fills(directory):
    """fill id -> fill record (first sighting), with later price and deletion applied. None: no sidecar."""
    path = Path(directory) / FILLS
    if not path.exists():
        return None, 0
    records, skipped = read_lines(path)
    fills = {}
    for rec in records:
        fid = rec.get('fill_id')
        if not isinstance(fid, str):
            skipped += 1
            continue
        kind = rec.get('type')
        if kind == 'fill' and fid not in fills:
            fills[fid] = dict(rec)
        elif kind == 'fill_price' and fid in fills and number(fills[fid].get('price')) is None:
            fills[fid]['price'] = rec.get('price')
            fills[fid]['price_observed_at'] = rec.get('observed_at')
        elif kind == 'fill_deleted' and fid in fills:
            fills[fid]['deleted'] = True
    return fills, skipped


def discover(root):
    root = Path(os.path.expanduser(root))
    found = []
    if (root / 'journal').is_dir():
        found.append(('main', root / 'journal'))
    for d in sorted(root.glob('journal-*')):
        if d.is_dir():
            found.append((d.name[len('journal-'):], d))
    return found


def clock(ms, utc=False):
    if type(ms) is not int:
        return '-'
    dt = datetime.fromtimestamp(ms / 1000, timezone.utc)
    if not utc:
        dt = dt.astimezone()
    return dt.strftime('%Y-%m-%d %H:%M:%S.') + '%03d' % (ms % 1000) + ('Z' if utc else dt.strftime(' %Z'))


def fmt(value):
    value = number(value)
    if value is None:
        return '-'
    text = ('%.6f' % value).rstrip('0').rstrip('.')
    return text


def rows_for(commands, fills):
    """Report rows (commands and their bracket legs) grouped by trade id."""
    by_command = {}
    for f in (fills or {}).values():
        by_command.setdefault(f.get('command_id'), []).append(f)
    modifies = {}
    for c in commands.values():
        if c.get('kind') in ('modify_stop', 'auto_modify_stop') and c.get('outcome') == 'accepted':
            modifies.setdefault(str(c.get('target_order_id')), []).append(c)
    trades = {}

    def row(cid, c, kind, role, journal_price, parent=None):
        own = sorted(by_command.get(cid, []), key=lambda f: (f.get('fill_ms') or 0, f.get('fill_id')))
        receipt = c.get('receipt') if isinstance(c.get('receipt'), dict) else {}
        detail = receipt.get('reason') or receipt.get('mode') or c.get('mode') or receipt.get('bridge')
        r = {'command_id': cid, 'kind': kind, 'role': role, 'outcome': c.get('outcome'), 'side': c.get('side'),
             'at': c.get('at'), 'quantity': c.get('quantity'), 'detail': detail,
             'broker_order_id': c.get('broker_order_id') if parent is None else None,
             'journal_price': number(journal_price), 'fills': own}
        if parent:
            r['parent_command_id'] = parent
        trades.setdefault(c.get('trade_id'), []).append(r)
        return r

    for cid, c in commands.items():
        kind = c.get('kind')
        if kind == 'bracket_entry':
            row(cid, c, kind, 'entry', c.get('reference_price'))
            for role, leg in sorted((c.get('legs') or {}).items()):
                price = c.get('stop_price') if role == 'stop' else c.get('tp1_price')
                r = row(cid + '#' + role, c, kind + ':' + role, role, price, parent=cid)
                r['broker_order_id'] = str(leg)
                if role == 'stop' and modifies.get(str(leg)):
                    r['stop_modified_to'] = number(modifies[str(leg)][-1].get('stop_price'))
        elif kind == 'bracket_tp1':
            parent = commands.get(c.get('parent') or '') or {}
            r = row(cid, c, kind, 'tp1', parent.get('tp1_price'), parent=c.get('parent'))
            r['broker_order_id'] = c.get('broker_order_id')
        elif kind in ('modify_stop', 'auto_modify_stop'):
            row(cid, c, kind, 'modify', c.get('stop_price'))
        else:
            role = {'entry': 'entry', 'tp1_limit': 'tp1', 'exit_market': 'exit', 'flatten_all': 'flatten',
                    'auto_flatten': 'flatten', 'align_flatten': 'align_flatten'}.get(kind, kind)
            row(cid, c, kind, role, None)
    for rows in trades.values():
        for r in rows:
            summarize(r)
    return trades


def summarize(r):
    live = [f for f in r['fills'] if not f.get('deleted')]
    qty = sum(f.get('qty') or 0 for f in live)
    priced = [f for f in live if number(f.get('price')) is not None and type(f.get('qty')) is int]
    r['filled_qty'] = qty
    r['avg_fill_price'] = None
    r['unpriced_fills'] = len(live) - len(priced)
    if priced and len(priced) == len(live):
        r['avg_fill_price'] = round(sum(f['price'] * f['qty'] for f in priced) / qty, 6)
    # Order-level price: what Tradovate's order showed at the fill, else the journal's.
    first = live[0] if live else {}
    if r['role'] == 'stop':
        ref, basis = number(first.get('order_stop_price')), 'stop (order)'
        if ref is None:
            ref = r.get('stop_modified_to') if r.get('stop_modified_to') is not None else r['journal_price']
            basis = 'stop (journal)'
    elif r['role'] == 'tp1':
        ref, basis = number(first.get('order_price')), 'limit (order)'
        if ref is None:
            ref, basis = r['journal_price'], 'limit (journal)'
    elif r['role'] == 'entry' and r['journal_price'] is not None:
        ref, basis = r['journal_price'], 'reference quote (journal)'
    elif r['role'] == 'modify':
        ref, basis = r['journal_price'], 'new stop (journal)'
    else:
        ref, basis = None, None
    r['order_price'], r['order_price_basis'] = ref, basis
    r['slippage_points'] = r['slippage_points_total'] = None
    if ref is not None and r['avg_fill_price'] is not None and r['role'] != 'modify':
        action = first.get('action')
        if action not in ('Buy', 'Sell'):
            action = None
            if r.get('side') in ('long', 'short'):
                buy = (r['role'] == 'entry') == (r['side'] == 'long')
                action = 'Buy' if buy else 'Sell'
        if action:
            # From the fills themselves (exact for tick prices), then per contract.
            paid = sum(f['price'] * f['qty'] for f in priced)
            total = paid - ref * qty if action == 'Buy' else ref * qty - paid
            r['slippage_points'] = round(total / qty, 6)
            r['slippage_points_total'] = round(total, 6)


def build(journals, trade=None, last=10):
    accounts, trades = [], {}
    for label, directory in journals:
        directory = Path(os.path.expanduser(str(directory)))
        commands, bad_commands = load_commands(directory)
        fills, bad_fills = load_fills(directory)
        info = {'label': label, 'journal_dir': str(directory), 'commands_file': (directory / COMMANDS).exists(),
                'fills_file': fills is not None, 'fill_records': len(fills or {}),
                'skipped_lines': {'commands': bad_commands, 'fills': bad_fills}}
        accounts.append(info)
        for tid, rows in rows_for(commands, fills).items():
            if tid is None or (trade is not None and tid != trade):
                continue
            t = trades.setdefault(tid, {'trade_id': tid, 'first_at': None, 'accounts': []})
            ats = [r['at'] for r in rows if type(r.get('at')) is int]
            if ats and (t['first_at'] is None or min(ats) < t['first_at']):
                t['first_at'] = min(ats)
            t['accounts'].append({'label': label, 'fills_file': fills is not None, 'commands': rows})
    ordered = sorted(trades.values(), key=lambda t: (t['first_at'] or 0, t['trade_id']))
    if last and trade is None:
        ordered = ordered[-last:]
    return {'schema': 'tvbridge.fill-report.v1', 'generated_at': int(time.time() * 1000), 'accounts': accounts,
            'trades': ordered}


def render(report, utc=False, out=sys.stdout):
    w = out.write
    for a in report['accounts']:
        w('account %-14s %s  %s\n' % (a['label'], a['journal_dir'],
                                     '%d fill(s) recorded' % a['fill_records'] if a['fills_file']
                                     else 'no fill prices recorded (no fills.jsonl)'))
        if not a['commands_file']:
            w('  (no commands.jsonl in this directory)\n')
    if not report['trades']:
        w('\nno trades found\n')
    for t in report['trades']:
        w('\n=== trade %s  (first command %s)\n' % (t['trade_id'], clock(t['first_at'], utc)))
        for acct in t['accounts']:
            w('  [%s]%s\n' % (acct['label'], '' if acct['fills_file'] else '  no fill prices recorded'))
            for r in acct['commands']:
                head = '    %-15s %-9s %-44s' % (r['kind'], r['outcome'] or '-', r['command_id'])
                bits = []
                if r['order_price'] is not None:
                    bits.append('%s %s' % (r['order_price_basis'], fmt(r['order_price'])))
                if r.get('detail'):
                    bits.append(str(r['detail'])[:60])
                w(head + ('  ' + '; '.join(bits) if bits else '') + '\n')
                if r['role'] == 'modify':
                    continue
                if not r['fills']:
                    if acct['fills_file'] and (r.get('broker_order_id') or r['role'] in EXIT_ROLES + ('entry',)) \
                            and r['outcome'] == 'accepted':
                        w('        no fill recorded\n')
                    continue
                for f in r['fills']:
                    w('        fill %s  %s %s @ %s  id %s%s%s\n' % (
                        clock(f.get('fill_ms'), utc), f.get('action') or '?', f.get('qty'),
                        fmt(f.get('price')) if number(f.get('price')) is not None else 'no price',
                        f.get('fill_id'), '  (deleted by Tradovate)' if f.get('deleted') else '',
                        '  (liquidation matched by structure)' if f.get('attribution') == 'liquidation_match' else ''))
                line = '        filled %s  avg %s' % (r['filled_qty'], fmt(r['avg_fill_price']))
                if r['unpriced_fills']:
                    line += '  (%d fill(s) without a price)' % r['unpriced_fills']
                if r['slippage_points'] is not None:
                    line += '  slippage %+g pts/contract (%+g pts total; + = worse than %s)' % (
                        r['slippage_points'], r['slippage_points_total'], r['order_price_basis'])
                elif r['avg_fill_price'] is not None:
                    line += '  slippage n/a (no order-level price)'
                w(line + '\n')


def main(argv=None):
    ap = argparse.ArgumentParser(description='Fill prices and slippage per trade from the bridge journals '
                                             '(read-only; never writes, never contacts the daemon or Tradovate).')
    ap.add_argument('--root', default=DEFAULT_ROOT,
                    help='install root holding journal/ and journal-<key>/ (default %(default)s)')
    ap.add_argument('--journal', action='append', default=[], metavar='LABEL=DIR',
                    help='journal directory to read (repeatable); replaces the --root discovery')
    ap.add_argument('--trade', help='only this trade id')
    ap.add_argument('--last', type=int, default=10, help='latest N trades (0 = all; default %(default)s)')
    ap.add_argument('--json', action='store_true', help='JSON instead of text')
    ap.add_argument('--utc', action='store_true', help='times in UTC (default: this machine\'s local time)')
    args = ap.parse_args(argv)
    journals = []
    for item in args.journal:
        label, sep, directory = item.partition('=')
        if not sep or not label or not directory:
            ap.error('--journal must be LABEL=DIR')
        journals.append((label, directory))
    if not journals:
        journals = discover(args.root)
        if not journals:
            ap.error('no journal directories under %s (use --journal LABEL=DIR)' % args.root)
    report = build(journals, trade=args.trade, last=max(0, args.last))
    if args.json:
        json.dump(report, sys.stdout, indent=1, sort_keys=True)
        sys.stdout.write('\n')
    else:
        render(report, utc=args.utc)
    return 0


if __name__ == '__main__':
    sys.exit(main())
