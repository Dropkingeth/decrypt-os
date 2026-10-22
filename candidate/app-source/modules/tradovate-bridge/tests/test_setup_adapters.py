"""`setup.py init` checks the contract (MNQ or MES, tick 0.25) and the binding's adapter like the daemon."""
import argparse
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE / 'bridge'))

from tvbridge import adapters  # noqa: E402


def load_setup():
    spec = importlib.util.spec_from_file_location('decrypt_bridge_setup_adapters', MODULE / 'setup.py')
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    return setup


SETUP = load_setup()


def args(**kw):
    base = dict(account_name='SIM-ACCOUNT-1', soup_account_id='30000001', ticker='MNQZ2021', tick_size=.25,
                max_contracts=10, publish_mode='adapter-db', soup_state='/tmp/decrypt-os-test/soup', adapter=None)
    base.update(kw)
    return argparse.Namespace(**base)


class SetupContracts(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(adapters.REGISTRY, {})
        patcher.start()
        self.addCleanup(patcher.stop)
        adapters.register(adapters.Adapter('test-mes-adapter-v1', 'Synthetic MES adapter', schemas=('test-entry-v1',),
                                           roots=('MES',), kinds=('entry', 'exit_market', 'cancel')))

    def test_soup_default_is_unchanged(self):
        self.assertIsNone(SETUP.contract_problem('MNQZ2021', .25))
        self.assertNotIn('adapters', SETUP.daemon_config(args()))

    def test_unknown_root_tick_or_adapter_is_refused(self):
        for ticker, tick, adapter, fragment in (('ESZ2021', .25, None, 'not supported'),
                                                ('MNQZ2021', .5, None, 'tick_size'),
                                                ('MESZ2021', .25, None, 'does not trade MES'),
                                                ('MESZ2021', .25, 'nope-adapter-v1', 'not a registered adapter'),
                                                ('MNQZ2021', .25, 'test-mes-adapter-v1', 'does not trade MNQ')):
            with self.subTest(ticker=ticker, adapter=adapter):
                self.assertIn(fragment, SETUP.contract_problem(ticker, tick, adapter))

    def test_mes_adapter_binding_is_written_enabled(self):
        self.assertIsNone(SETUP.contract_problem('MESZ2021', .25, 'test-mes-adapter-v1'))
        cfg = SETUP.daemon_config(args(ticker='MESZ2021', adapter='test-mes-adapter-v1'))
        self.assertEqual(cfg['adapters'], {'test-mes-adapter-v1': {'enabled': True}})
        self.assertEqual((cfg['ticker'], cfg['tick_size']), ('MESZ2021', .25))
        self.assertNotIn('max_contracts', cfg)  # no size cap: the strategy alert sets the size
        self.assertEqual(list(adapters.binding_adapters(cfg)), ['test-mes-adapter-v1'])


if __name__ == '__main__':
    unittest.main()
