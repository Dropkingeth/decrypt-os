#!/usr/bin/env python3
"""Manual-management display tests for the bridge status panel (2.1). Offline, no network.
Run: python3 -m unittest test_manual -v"""
import unittest

import server

TRADE = 'fixture-stream:MNQM2021:1615925100000:short'
INITIAL, BE, HAND = 5031.25, 4992.5, 5017.75


def context(manual_price=None):
    native = {'manual': {'stop': {'price': manual_price, 'previous': INITIAL, 'keep': False}}} if manual_price else {}
    return {'engine': {'trade': {'id': TRADE, 'payload': {'break_even_price': BE, 'initial_hard_stop_price': INITIAL},
                                 'native': native}, 'commands': {}}}


class StopKind(unittest.TestCase):
    def test_hand_moved_stop_is_labelled_moved_by_hand(self):
        self.assertEqual(server.stop_kind(HAND, TRADE, {'stop_price': INITIAL}, [], context(HAND)),
                         ('manual', 'soup_manual_stop'))
        view = server.stop_view({'trade_id': TRADE, 'open': True, 'resting': True, 'quantity': 3, 'price': HAND,
                                 'price_text': '5017.75', 'kind': 'manual', 'confirmed_text': '20:09:00 UTC'})
        self.assertEqual((view['pill'], view['tone']), ('Moved by hand', 'teal'))
        self.assertIn('moved by hand', view['summary'])

    def test_initial_and_break_even_are_checked_first_and_nothing_is_guessed(self):
        self.assertEqual(server.stop_kind(BE, TRADE, {}, [], context(BE))[0], 'break_even')
        self.assertEqual(server.stop_kind(INITIAL, TRADE, {}, [], context(INITIAL))[0], 'initial')
        self.assertEqual(server.stop_kind(HAND, TRADE, {}, [], context()), ('unknown', None))
        self.assertEqual(server.stop_kind(HAND, 'other-trade', {}, [], context(HAND)), ('unknown', None))


class SoupAlert(unittest.TestCase):
    def soup(self, *alerts, **native):
        return {'native_exits': dict({'alerts': list(alerts)}, **native)}

    def test_info_alerts_are_calm_plain_words(self):
        a = server.soup_alert(self.soup({'code': 'external_close_detected', 'level': 'info'}))
        self.assertEqual(a['tone'], 'info')
        self.assertIn('entries are not blocked', a['text'])
        a = server.soup_alert(self.soup({'code': 'external_reduction', 'level': 'info', 'quantity': 2, 'position': 5}))
        self.assertEqual((a['text'], a['tone']), ('2 contract(s) closed by hand: Soup keeps managing the 5 left', 'info'))
        a = server.soup_alert(self.soup({'code': 'manual_stop_adopted', 'level': 'info', 'price': HAND, 'keep': True}))
        self.assertEqual(a['text'], 'Stop moved by hand to 5017.75: Soup keeps it')

    def test_faults_are_unchanged(self):
        ec = {'trade_id': TRADE, 'entry_qty': 7}
        a = server.soup_alert(self.soup({'code': 'external_close_detected', 'at': 1}, external_close=ec))
        self.assertEqual(a['tone'], 'coral')
        a = server.soup_alert(self.soup({'code': 'external_close_detected', 'level': 'info'},
                                        failed={'reason': 'external_flat_unreconciled'}))
        self.assertEqual(a['tone'], 'coral')
        a = server.soup_alert(self.soup({'code': 'stop_modify_rejected', 'at': 1}))
        self.assertEqual((a['text'], a['tone']), ('stop modify rejected', 'amber'))

    def test_tp1_fill_race_wait_and_resend_are_info(self):
        a = server.soup_alert(self.soup({'code': 'tp1_refused_awaiting_fill', 'level': 'info', 'entry_status': 'PendingNew',
                                         'attempt': 1}))
        self.assertEqual((a['text'], a['tone']), (
            'TP1 waiting for the entry fill: the bridge still showed the entry as not filled (PendingNew). '
            'Soup sends the same TP1 again once the fill shows (up to 30 s)', 'info'))
        a = server.soup_alert(self.soup({'code': 'tp1_refused_awaiting_fill', 'level': 'info', 'entry_status': None}))
        self.assertIn('as not filled. Soup sends', a['text'])
        a = server.soup_alert(self.soup({'code': 'tp1_resent_after_fill', 'level': 'info', 'attempt': 1}))
        self.assertEqual((a['text'], a['tone']), (
            'TP1 sent again once the entry fill showed. The bridge had refused it before it saw the fill', 'info'))
        a = server.soup_alert(self.soup({'code': 'tp1_resent_after_fill', 'level': 'info', 'attempt': 2}))
        self.assertTrue(a['text'].startswith('TP1 sent a third time'))

    def test_tp1_wait_ending_without_tp1_is_amber(self):
        a = server.soup_alert(self.soup({'code': 'tp1_fill_evidence_timeout', 'at': 1}))
        self.assertEqual((a['text'], a['tone']), (
            'TP1 not placed: the entry fill did not show within 30 s, so Soup exited the trade', 'amber'))
        a = server.soup_alert(self.soup({'code': 'tp1_retry_budget_exhausted', 'at': 1}))
        self.assertEqual((a['text'], a['tone']), (
            'TP1 not placed: the bridge refused it 3 times before it saw the entry fill, so Soup exited the trade', 'amber'))
        a = server.soup_alert(self.soup({'code': 'tp1_fill_evidence_timeout', 'at': 1}, entries_blocked=True))
        self.assertEqual(a['tone'], 'coral')  # a blocked exit still outranks it


if __name__ == '__main__':
    unittest.main()
