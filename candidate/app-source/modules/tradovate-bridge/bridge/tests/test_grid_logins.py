"""Grid alerts grouped by Tradovate login (1.1.0 multi-login).

- One login (or a registry whose firing accounts all sit on one login): byte-identical text.
- Two logins: one column header, then per login a header line `<id>: <label>  <STATE>` and its rows.
- An unreachable login: its rows read UNREACHABLE and count for ATTENTION.
"""
import unittest

import harness  # noqa: F401  (puts the repository on sys.path)

from tvbridge import grid

AT = 1615403100000  # 2021-03-10 19:05:00 UTC (14:05 New York)


def acct(key, net, tp1, **kw):
    orders = [{'id': key + '-o', 'type': 'LMT', 'qty': 3, 'tp1': True}] if tp1 else []
    return dict({'key': key, 'available': True, 'logged_in': True, 'entries_disabled': False,
                 'net_position': net, 'orders': orders}, **kw)


def report(main=(5, True), rows=None, logins=None):
    out = {'main': acct('main', *main),
           'followers': rows if rows is not None else [acct('ac', 5, True), acct('ab', 5, True)]}
    if logins is not None:
        out['logins'] = {k: {'state': v} for k, v in logins.items()}
    return out


TWO = [('a', 'Login A', ['ac']), ('b', 'Login B', ['ab'])]
ONE = [('a', 'Login A', ['ac', 'ab'])]


class GroupedText(unittest.TestCase):
    def test_two_logins_table_is_grouped_under_login_headers(self):
        rows = grid.account_rows(dict(report(), main_key='account-a'), ['ac', 'ab'])
        groups = grid.login_groups(rows, ['ac', 'ab'], TWO, {'a': 'CONNECTED', 'b': 'LOGGED OUT'})
        self.assertEqual(grid.render('MNQ', AT, 'LONG 5 @ 12000.00', None, groups),
                         'MNQ  14:05  LONG 5 @ 12000.00\n'
                         'ACCT  POS  ORDERS   STATE\n'
                         'a: Login A  CONNECTED\n'
                         'AA    5    TP1 3    OK\n'
                         'AC    5    TP1 3    OK\n'
                         'b: Login B  LOGGED OUT\n'
                         'AB    5    TP1 3    OK')

    def test_unreachable_login_rows_and_flat_line(self):
        rows = grid.account_rows(dict(report(), main_key='account-a'), ['ac', 'ab'])
        groups = grid.login_groups(rows, ['ac', 'ab'], TWO, {'a': 'CONNECTED'})  # b missing: unreachable
        self.assertEqual(groups[1], ('b: Login B  UNREACHABLE', [('AB', '?', '-', 'UNREACHABLE')]))
        flat = [(h, [(r[0], '0', '-', r[3]) for r in rs]) for h, rs in groups]
        self.assertEqual(grid.render_flat('MNQ', AT, [r for _, rs in flat for r in rs], flat),
                         'MNQ  14:05  ALL FLAT\na: Login A  CONNECTED\nAA 0 | AC 0\n'
                         'b: Login B  UNREACHABLE\nAB 0   UNREACHABLE')

    def test_single_table_helpers_are_unchanged(self):
        rows = [('AA', '5', 'TP1 3', 'OK'), ('AB', '5', 'TP1 3', 'OK')]
        self.assertEqual(grid.table(rows), 'ACCT  POS  ORDERS   STATE\nAA    5    TP1 3    OK\nAB    5    TP1 3    OK')
        self.assertEqual(grid.grouped_table([(None, rows)]), grid.table(rows))
        self.assertEqual(grid.render('MNQ', AT, 'X', rows, None), grid.render('MNQ', AT, 'X', rows))


class TrackerByLogin(unittest.TestCase):
    def sequence(self, tracker, logins=None):
        """The event sequence of GridFormat's tracker test; returns every event text and the `now` texts."""
        trade = {'id': 'T:long', 'side': 'long', 'phase': 'open'}
        cmds = {'e': {'id': 'e', 'kind': 'entry', 'quantity': 5, 'started_at': 1},
                'p': {'id': 'p', 'kind': 'tp1_limit', 'quantity': 3, 'started_at': 2}}
        idle = report(main=(0, False), rows=[acct('ac', 0, False), acct('ab', 0, False)], logins=logins)
        filled = report(logins=logins)
        filled['main']['trade'] = [{'command_id': 'e', 'quantity': 5, 'filled': 5, 'price': 12000.0, 'fill_ms': AT}]
        tp = dict(filled, main=dict(filled['main'], net_position=2, orders=[],
                                    trade=filled['main']['trade'] + [{'command_id': 'p', 'quantity': 3, 'filled': 3,
                                                                      'price': 12006.75, 'fill_ms': AT + 1500}]))
        flat = dict(idle, main=dict(idle['main'], trade=tp['main']['trade']))
        out = []
        for t, r, at in ((None, idle, AT), (trade, filled, AT + 500), (trade, tp, AT + 1500), (trade, tp, AT + 4000),
                         (trade, tp, AT + 10000), (dict(trade, phase='closed'), flat, AT + 420000)):
            out += [ev['text'] for ev in tracker.update(t, cmds if t else {}, r, at)]
            out.append(tracker.status()['now'])
        return out

    def test_one_login_is_byte_identical(self):
        plain = self.sequence(grid.GridTracker('MNQ', 'account-a', ['ac', 'ab'], attention_ms=5000))
        one = self.sequence(grid.GridTracker('MNQ', 'account-a', ['ac', 'ab'], attention_ms=5000, logins=ONE),
                            logins={'a': 'CONNECTED'})
        self.assertEqual(one, plain)
        self.assertIn('MNQ  14:12  ALL FLAT\nAA 0 | AC 0 | AB 0   OK', plain)

    def test_two_logins_events_carry_login_headers(self):
        tr = grid.GridTracker('MNQ', 'account-a', ['ac', 'ab'], attention_ms=5000, logins=TWO)
        texts = self.sequence(tr, logins={'a': 'CONNECTED', 'b': 'CONNECTED'})
        entry = next(t for t in texts if t.startswith('MNQ  14:05  LONG 5'))
        self.assertEqual(entry.split('\n')[1:], ['ACCT  POS  ORDERS   STATE', 'a: Login A  CONNECTED',
                                                 'AA    5    TP1 3    OK', 'AC    5    TP1 3    OK',
                                                 'b: Login B  CONNECTED', 'AB    5    TP1 3    OK'])
        self.assertIn('MNQ  14:12  ALL FLAT\na: Login A  CONNECTED\nAA 0 | AC 0\nb: Login B  CONNECTED\nAB 0   OK',
                      texts)

    def test_unreachable_login_raises_attention_once(self):
        tr = grid.GridTracker('MNQ', 'account-a', ['ac', 'ab'], attention_ms=4000, logins=TWO)
        r = report(main=(0, False), rows=[acct('ac', 0, False)], logins={'a': 'CONNECTED', 'b': 'UNREACHABLE'})
        self.assertEqual(tr.update(None, {}, r, AT), [])
        self.assertIn('b: Login B  UNREACHABLE\nAB    ?    -        UNREACHABLE', tr.status()['now'])
        [att] = tr.update(None, {}, r, AT + 4000)
        self.assertEqual(att['kind'], 'attention')
        self.assertIn('AB=UNREACHABLE', att['key'])
        self.assertEqual(tr.update(None, {}, r, AT + 7500), [])


if __name__ == '__main__':
    unittest.main()
