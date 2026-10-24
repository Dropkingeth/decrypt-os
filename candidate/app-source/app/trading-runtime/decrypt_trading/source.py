"""Load only the exact reviewed, customer-neutral core bytes.

The original modules use a flat ``storage`` import. A module-local import
binding preserves their bytes without changing sys.path or global modules.
"""
import builtins
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
PINS = {
    'core.py': '0a7803755e3e84c02f79252ef8938e093cc6a452715b49f7a2e29c1b86281d2a',
    'storage.py': 'e281d9315a494556c03ec5fcffa9bb53ab5e538898423db0d0e32313ca6c2075',
    'transport.py': '84164390049c65c9b52b5f4e61f772fc20f311712f0fbe5e744ea38b9f08b744',
    'settings_contract.json': '73299fd428eaa33887b96ec1d23ab6daec0cb920c5165a13fc82b3e835b61aa5',
}
STRATEGY_PIN = '32d09c04e6edec49ccb9c5d5f107d9ed43669c3bd60b89efebdf9b08f89b730f'
PAIR_ID = 'soup-pine-authority-r4'


def manifest():
    value = json.loads((ROOT / 'MANIFEST.json').read_text())
    if (value.get('supportedPairId') != PAIR_ID or value.get('vendorFiles') != PINS
            or value.get('sourcePins') != {'engine': PINS['core.py'], 'strategy': STRATEGY_PIN}
            or value.get('mode') != 'customer_demo_only'
            or value.get('publicationVerified') is not False
            or value.get('providerCommissioned') is not False):
        raise ValueError('runtime manifest mismatch')
    return value


def load_sources():
    manifest()
    directory = Path(__file__).parent / 'vendor' / 'soup_r4'
    source = {}
    for name, pin in PINS.items():
        path = directory / name
        if path.is_symlink():
            raise ValueError('runtime source symlink')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != pin:
            raise ValueError('runtime source differs from reviewed bytes')
        source[name] = raw
    modules = {}
    def module_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'storage' and level == 0:
            return modules['storage']
        return builtins.__import__(name, globals, locals, fromlist, level)
    for name in ('storage', 'core', 'transport'):
        module = ModuleType('decrypt_trading.reviewed_' + name)
        module.__file__ = str(directory / (name + '.py'))
        module.__dict__['__builtins__'] = dict(vars(builtins), __import__=module_import)
        exec(compile(source[name + '.py'], module.__file__, 'exec'), module.__dict__)
        modules[name] = module
    return modules


# beta4 engines platform: engines of strategy adapters that trade through the Tradovate bridge
# (decrypt_trading.bridge_adapters). An adapter plugs in with register_bridge_engine(); it must also be
# registered in bridge_adapters and pinned in customer_contract.PAIRS with the same two hashes.
# Nothing is registered here by default. See modules/tradovate-bridge/bridge/ENGINES.md.
BRIDGE_ENGINES = {}
BRIDGE_ENGINE_FUNCTIONS = ('input_identity', 'payload_schema', 'bridge_message', 'signal_time')


def register_bridge_engine(pair_id, *, module, files, strategy_sha256):
    """Pin one adapter engine. `module` is its dotted import name; `files` maps each file name in the
    module's folder to its SHA-256 (the module's own file is the engine hash). Loaded only after every
    file matches its pin. Raises ValueError."""
    from . import bridge_adapters
    adapter = bridge_adapters.lookup(pair_id)
    if adapter.reference:
        raise ValueError('the reference adapter has its own loader')
    entry = module.rsplit('.', 1)[-1] + '.py'
    if (pair_id in BRIDGE_ENGINES or type(module) is not str or type(files) is not dict or entry not in files
            or not all(type(k) is str and '/' not in k and type(v) is str and len(v) == 64 for k, v in files.items())
            or type(strategy_sha256) is not str or len(strategy_sha256) != 64):
        raise ValueError('bridge engine pin is invalid or already registered')
    BRIDGE_ENGINES[pair_id] = {'module': module, 'files': dict(files), 'engineSha256': files[entry],
                               'strategySha256': strategy_sha256}
    return BRIDGE_ENGINES[pair_id]


def _load_bridge_engine(pair_id):
    pin = BRIDGE_ENGINES[pair_id]
    spec = importlib.util.find_spec(pin['module'])
    if spec is None or not spec.origin:
        raise ValueError('bridge engine module not found')
    folder = Path(spec.origin).parent
    for name, sha in pin['files'].items():
        path = folder / name
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise ValueError('bridge engine differs from its pinned files')
    module = importlib.import_module(pin['module'])
    if not hasattr(module, 'Engine') or not all(callable(getattr(module, f, None)) for f in BRIDGE_ENGINE_FUNCTIONS):
        raise ValueError('bridge engine lacks Engine, ' + ', '.join(BRIDGE_ENGINE_FUNCTIONS))
    return module


def pair_spec(pair_id):
    if pair_id in BRIDGE_ENGINES:
        pin = BRIDGE_ENGINES[pair_id]
        return {'pairId': pair_id, 'engineSha256': pin['engineSha256'], 'strategySha256': pin['strategySha256']}
    if pair_id == PAIR_ID:
        return {'pairId': PAIR_ID, 'engineSha256': PINS['core.py'], 'strategySha256': STRATEGY_PIN}
    raise ValueError('source pair has no supported runtime adapter')


def load_pair_sources(pair_id):
    modules = load_sources()
    if pair_id == PAIR_ID:
        return modules
    if pair_id in BRIDGE_ENGINES:
        # Soup's storage helpers stay (writer lock, strict JSON); engine and order messages are the adapter's.
        engine = _load_bridge_engine(pair_id)
        modules['core'] = engine
        modules['transport'] = engine
        return modules
    raise ValueError('source pair has no supported runtime adapter')


def package_catalog():
    """Engines installable from the app (Execution runtime): hash-verified, no activation or owner paths.
    Only Soup in this release. The decrypt-bridge-v2 strategies (below) are pinned here, but the app service
    (app/backend/trading_service.py) neither installs nor routes them yet. The bridge side of a binding is
    `setup.py init --adapter <id>`; see modules/tradovate-bridge/bridge/ENGINES.md."""
    load_pair_sources(PAIR_ID)
    return [{**pair_spec(PAIR_ID), 'label': 'Soup — native TP1 retry', 'engineId': 'turtle-soup',
             'version': 'soup-r4-os-1', 'runtimeSupported': True, 'publicationVerified': False,
             'providerCommissioned': False, 'tp1OffsetMode': 'explicit'}]


# --- decrypt-bridge-v2 strategies (beta4): one line each in bridge_v2_registry.STRATEGIES. Each pin holds the
# shared engine and validator (bridge_v2.py, bridge_v2_contract.py) and the strategy's own module.
from . import bridge_v2_registry as _bridge_v2
for _adapter_id, (_module_file, _engine_pin, _strategy_pin) in _bridge_v2.STRATEGIES.items():
    register_bridge_engine(_adapter_id, module=_bridge_v2.module_name(_adapter_id), files=_bridge_v2.files(_adapter_id),
                           strategy_sha256=_strategy_pin)
