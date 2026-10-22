"""Synthetic Soup strategy constants for the test fixtures.

The shipped settings contract (vendor/soup_r4/settings_contract.json) pins the strategy's hidden constants by
digest only (`constants_sha256`, SHA-256 of the canonical JSON); an alert is accepted only when the SHA-256 of its
`strategy_snapshot.constants` matches. The fixtures in this folder carry FIXTURE_CONSTANTS instead, so the tests
point every Soup engine they open at the digest of this synthetic block. install() is idempotent.

The contract names only the inputs the engine reads; the others are checked only by name form, scalar value and
an upper bound (`unread_inputs`), so the fixtures carry a few under synthetic names (fixture_bool_01,
fixture_int_01, fixture_number_01, fixture_text_01, ...) with arbitrary synthetic settings. No patch is needed for those.
"""
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app/trading-runtime'))
from decrypt_trading import source  # noqa: E402

FIXTURE_CONSTANTS = {'fixture_constant_a': 1, 'fixture_flag_b': True, 'fixture_window_c': '0000-0001'}


def constants_digest(value):
    """The engine's digest: SHA-256 of the canonical JSON (sorted keys, no spaces)."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


FIXTURE_DIGEST = constants_digest(FIXTURE_CONSTANTS)
_REAL_LOAD = source.load_sources


def _load_sources():
    modules = _REAL_LOAD()                       # pin-checked bytes, unchanged
    engine = modules['core'].Engine              # a fresh class per load: the change stays in these modules
    real_init = engine.__init__

    def __init__(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        self.rules['constants_sha256'] = FIXTURE_DIGEST
    engine.__init__ = __init__
    return modules


def install():
    if source.load_sources is not _load_sources:
        source.load_sources = _load_sources


install()
