"""Resolve an `unknown` command after checking Tradovate by hand.

    python3 -m tvbridge.reconcile --journal-dir DIR --list
    python3 -m tvbridge.reconcile --journal-dir DIR --command ID --placed ORDER_ID --note "seen in Orders tab"
    python3 -m tvbridge.reconcile --journal-dir DIR --command ID --not-placed --note "no order in Orders tab"
    python3 -m tvbridge.reconcile --journal-dir DIR --command ID --cancel-settled --note "no working orders left"

Bracket kinds (opt-in, see tvbridge/executor.py):

    bracket_entry: --placed ENTRY_ORDER_ID --leg stop=ORDER_ID [--leg tp1=ORDER_ID]   (tp1 only for an OSO TP leg)
    modify_stop / auto_modify_stop: --settled (the stop shows the change) or --not-placed (it does not)
    auto_cancel: --settled (the listed orders are no longer working) or --not-placed
    flatten_all: --placed LIQUIDATION_ORDER_ID, --settled (flat, nothing working), or --not-placed
    manual_flatten (1.1.3, operator Flatten now): the same three choices as flatten_all

Stop the daemon first; it reads the journal at startup.
"""
import argparse
import json

from .journal import NO_ORDER_KINDS, Journal


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--journal-dir', required=True)
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--command')
    group = ap.add_mutually_exclusive_group()
    group.add_argument('--placed', metavar='ORDER_ID')
    group.add_argument('--not-placed', action='store_true')
    group.add_argument('--cancel-settled', action='store_true',
                       help='for an unknown cancel: Tradovate shows no working order left on the contract')
    group.add_argument('--settled', action='store_true',
                       help='for an unknown modify/auto-cancel: Tradovate shows its effect; for flatten_all: '
                            'the contract is flat with nothing working')
    ap.add_argument('--leg', action='append', default=[], metavar='ROLE=ORDER_ID',
                    help='bracket_entry with --placed: stop=ID (required) and tp1=ID (OSO TP leg)')
    ap.add_argument('--note', default='')
    args = ap.parse_args(argv)
    journal = Journal(args.journal_dir)
    if args.list or not args.command:
        print(json.dumps(journal.unresolved(), indent=2, sort_keys=True))
        return
    if not (args.placed or args.not_placed or args.cancel_settled or args.settled):
        raise SystemExit('choose --placed ORDER_ID, --not-placed, --cancel-settled or --settled')
    command = journal.get(args.command)
    kind = (command or {}).get('kind')
    is_cancel = kind == 'cancel'
    if args.cancel_settled and not is_cancel:
        raise SystemExit('--cancel-settled is only for cancel commands')
    if args.settled and kind not in (NO_ORDER_KINDS - {'cancel'}) | {'flatten_all', 'manual_flatten'}:
        raise SystemExit('--settled is for modify_stop, auto_modify_stop, auto_cancel, flatten_all and '
                         'manual_flatten commands')
    if args.placed and kind in NO_ORDER_KINDS:
        raise SystemExit('a %s has no order id; use --%s or --not-placed'
                         % (kind, 'cancel-settled' if is_cancel else 'settled'))
    legs = {}
    for item in args.leg:
        role, _, oid = item.partition('=')
        if role not in ('stop', 'tp1') or not oid.isdigit() or role in legs:
            raise SystemExit('--leg must be stop=ORDER_ID or tp1=ORDER_ID, each at most once')
        legs[role] = oid
    if legs and not (args.placed and kind == 'bracket_entry'):
        raise SystemExit('--leg is only for --placed on a bracket_entry')
    try:
        if args.cancel_settled or args.settled:
            journal.reconcile(args.command, 'accepted', note=args.note)
        elif args.placed:
            journal.reconcile(args.command, 'accepted', broker_order_id=args.placed, note=args.note,
                              legs=legs or None)
        else:
            journal.reconcile(args.command, 'rejected', note=args.note)
    except ValueError as exc:
        raise SystemExit(str(exc))
    print('reconciled', args.command)


if __name__ == '__main__':
    main()
