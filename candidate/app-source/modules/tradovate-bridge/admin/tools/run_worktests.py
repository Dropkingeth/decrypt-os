#!/usr/bin/env python3
"""Run the admin tests against this checkout's app/ using an installed admin's sandbox.

A source checkout has no sandbox of its own, and the sandbox configs hold absolute paths to the
installed tvbridge-admin/sandbox (TVBRIDGE_ADMIN_ROOT). This runner imports app modules from this
checkout first, then loads each test file as if it sat in the installed tvbridge-admin/tests (so
ADMIN/sandbox resolves to that sandbox). Modules already imported are never re-imported, so the tests
exercise this checkout's code; that is asserted.

    TMPDIR=$HOME/.cache PYTHONDONTWRITEBYTECODE=1 python3 tools/run_worktests.py [-v] [test_file ...]
"""
import importlib
import importlib.util
import os
import sys
import unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
COPY = os.path.dirname(HERE)
APP = os.path.join(COPY, 'app')
REAL_ADMIN = os.environ.get('TVBRIDGE_ADMIN_ROOT', os.path.expanduser('~/.local/share/decrypt-os/tvbridge-admin'))


def main(argv):
    verbose = '-v' in argv
    files = [a for a in argv if a != '-v'] or ['tests/test_admin.py', 'tests/test_restarter.py', 'tests/test_flatten.py',
                                               'tests/test_addlogin.py', 'tests/test_webhook2.py',
                                               'tests/test_export.py']
    files = [f for f in files if os.path.exists(os.path.join(COPY, f))]
    sys.path.insert(0, APP)
    sys.path.insert(1, os.path.join(COPY, 'tests'))
    names = ['tree', 'model', 'plans', 'adapters', 'restarter', 'engine', 'server']
    for extra in ('loginops', 'export'):
        if os.path.exists(os.path.join(APP, extra + '.py')):
            names.insert(5, extra)
    mods = {name: importlib.import_module(name) for name in names}
    for name, mod in mods.items():
        assert os.path.realpath(mod.__file__).startswith(os.path.realpath(APP) + os.sep), (name, mod.__file__)
    suite = unittest.TestSuite()

    def load(f):
        path = os.path.join(COPY, f)
        name = os.path.splitext(os.path.basename(path))[0]
        if name in sys.modules:
            return sys.modules[name]
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        mod.__file__ = os.path.join(REAL_ADMIN, 'tests', os.path.basename(path))  # ADMIN = the real admin dir
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    # Shared test bases first, the same way, so `from test_admin import Base` in any file gets this module
    # (its sandbox paths match sandbox/profile.json) and never a second copy that would miss the rewrite.
    for dep in ('tests/test_admin.py', 'tests/test_restarter.py', 'tests/test_addlogin.py'):
        if os.path.exists(os.path.join(COPY, dep)) and (dep in files or any(f not in (dep,) for f in files)):
            load(dep)
    for f in files:
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(load(f)))
    for name, mod in mods.items():  # the test files must not have swapped in the live app/ modules
        assert sys.modules[name] is mod, name
    print(f'app under test: {APP}', flush=True)
    result = unittest.TextTestRunner(verbosity=2 if verbose else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
