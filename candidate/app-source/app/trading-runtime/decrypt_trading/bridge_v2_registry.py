"""decrypt-bridge-v2 strategy registry (beta4): one line per strategy.

A strategy plugs in with its own module `decrypt_trading/bridge_v2_<name>.py` (its Strategy declaration,
extras schema included, see bridge_v2.py `module_api`) and ONE line in STRATEGIES below. source.py and
customer_contract.py read this table: the engine pin (shared files + the strategy module) and the pair
(engine hash, strategy hash) come from here. The strategy's bridge adapter is one line in
bridge_adapters.V2_ADAPTERS (copied byte for byte to the bridge). PAYLOAD-V2.md has the full checklist;
tests/test_bridge_v2_contract.py checks that everything agrees and prints the hashes to paste.

No strategy is registered by default; every v2 adapter is also off until its binding enables it.
"""
import hashlib
from pathlib import Path

# The shared engine and validator, pinned with every v2 strategy. Change them only together with this table.
SHARED = {
    'bridge_v2.py': 'c34ddaa0fc254e7819f5726bc711d654df922630d07d935c9c06e3488497917b',
    'bridge_v2_contract.py': '893f75356189b58f54a32a6dca45a2c6df78b5ec79a852a50e7af7c7bec94e2d',
}

# adapter id: (strategy module file in decrypt_trading, its SHA-256, opaque strategy pair id)
# The strategy pair id is sha256('decrypt-os/strategy/<adapter id>/v1'), a neutral label, never a hash of the
# TradingView script. Bump the trailing version when the paired strategy changes.
STRATEGIES = {
    'fvg-r5b-bridge-v2': ('bridge_v2_fvg_r5b.py',
                          '00c525051a77186c71df2274f6fc254f6904546037d72e6fc15be46b5a1bda54',
                          '9a3c0f238e67786d934afc53ae02e124d038c9aa67c898d4a480f2fe88a91b9b'),
    'lumi-gap-bridge-v2': ('bridge_v2_lumi.py', '273ecaaf873b5240ad5218200e74316db49186a7157f868dff327b35ab5722cf',
                          '86e48d9e61198d85e6dc82c45b346545d5cecd17ce0e1d218f73d408e77c9e6d'),
    'ote-refined-large-bridge-v2': ('bridge_v2_ote_refined_large.py',
                                    '7b575f2adc5b25f156f0977092f140d63fc29d81a837aa1a0945f8fe8712f320',
                                    'a3415b8c4fda114e1cb7ae7c8da9e3ea9e5f170d78227877b91e4c0f65d948be'),
    'ote-refined-small-bridge-v2': ('bridge_v2_ote_refined_small.py',
                                    'e924a367469ad81faf1eae662cc249a636c7ae2e6f46db63197f41efdad3fa09',
                                    'd7ea2d55a3a61bd75980bc7dc7b241ffb6ea6efd85c8c9a6b7bd6a576cb9bfc1'),
    'ote-active-bridge-v2': ('bridge_v2_ote_active.py', '7024392356f90e0ce46ea8225d26a23e36c4f0b197b44239de6c8605650d23d0',
                             'b85a70d04da0e66651d77457a78a2f55816b407e7fd389afa0dc4f407467e519'),
    'order-blocks-bridge-v2': ('bridge_v2_order_blocks.py',
                               '2b7e0493e5c27eb3508529c133f9dad4b62731cf51c75c33d0fe07cf910228fa',
                               'c198f9068fe549bac176159839565e232e817694161bdec25c3ee988aab8df7c'),
}

def files(adapter_id):
    """{file name: SHA-256} pinned for one v2 strategy: the shared engine files plus its own module."""
    module, sha, _ = STRATEGIES[adapter_id]
    return dict(SHARED, **{module: sha})


def module_name(adapter_id):
    return 'decrypt_trading.' + STRATEGIES[adapter_id][0][:-3]


def strategy(strategy_id):
    """The registered Strategy whose alerts carry `strategy: <strategy_id>`. Raises ValueError."""
    import importlib
    for adapter_id in STRATEGIES:
        value = importlib.import_module(module_name(adapter_id)).STRATEGY
        if value.strategy_id == strategy_id:
            return value
    raise ValueError('strategy %r is not a registered decrypt-bridge-v2 strategy' % (strategy_id,))


def sha256(name):
    """SHA-256 of a file next to this module (used by the consistency test and when updating pins)."""
    return hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
