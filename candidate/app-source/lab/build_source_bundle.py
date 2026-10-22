#!/usr/bin/env python3
"""Build a deterministic allowlisted app-source bundle, never an OS image."""
from __future__ import annotations
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile

SCHEMA = 'decrypt.os.source-bundle.v1'
EXACT = {'app/backend/requirements.txt', 'app/contract-calendar.json', 'app/strategy-catalog.json', 'app/strategy-access.json', 'app/engine-module-catalog.json', 'app/CONTRACT.md', 'app/backend/README.md', 'app/ui/package.json',
         'app/ui/package-lock.json', 'app/ui/index.html', 'lab/run.py', 'lab/README.md',
         'app/ui/BRAND-ASSETS.json', 'app/ui/licenses/spacegrotesk-OFL.txt',
         'app/ui/licenses/jetbrainsmono-OFL.txt', 'app/ui/licenses/UI-THIRD-PARTY-NOTICES.txt'}
# Customer runtime source is explicit. Never scan the workspace or skill trees
# recursively: generated profiles, credentials and arbitrary instructions stay out.
CUSTOMER_RUNTIME_SOURCE = {
    'app/system-scope.json',
    'app/harness/runtime.py',
    'app/harness/bootstrap.py',
    'app/harness/namespace_entry.py',
    'app/harness/vault_storage.py',
    'app/harness/vault_control.py',
    'app/harness/README.md',
    'app/harness/RUNTIME-MANIFEST.json',
    'app/customer-workspace/AGENTS.md',
    'app/customer-workspace/SYSTEM.md',
    'app/customer-workspace/SYSTEM-SCOPE.md',
    'app/customer-workspace/skills/decrypt-frontend-design/SKILL.md',
    'app/customer-workspace/skills/decrypt-frontend-design/references/provenance.md',
    'app/customer-workspace/skills/decrypt-frontend-design/references/LICENSE.txt',
    'app/customer-workspace/skills/decrypt-unslop/SKILL.md',
    'app/customer-workspace/skills/decrypt-unslop/references/provenance.md',
    'app/customer-workspace/skills/decrypt-unslop/references/LICENSE.txt',
    'app/customer-workspace/skills/review-skill-imports/SKILL.md',
    # Packaged ICT assistant (agent "ict"): explicit files only.
    'app/ict-assistant/README.md', 'app/ict-assistant/openclaw-agent.json',
    'app/ict-assistant/ict_clock.py', 'app/ict-assistant/launcher.py', 'app/ict-assistant/web_reader_proxy.py',
    'app/ict-assistant/desktop/decrypt-ict-assistant.desktop',
    'app/ict-assistant/workspace/AGENTS.md', 'app/ict-assistant/workspace/SOUL.md',
    'app/ict-assistant/workspace/IDENTITY.md',
    'app/ict-assistant/knowledge/01-foundations.md', 'app/ict-assistant/knowledge/02-liquidity-and-structure.md',
    'app/ict-assistant/knowledge/03-pd-arrays.md', 'app/ict-assistant/knowledge/04-time-and-sessions.md',
    'app/ict-assistant/knowledge/05-models.md', 'app/ict-assistant/knowledge/06-bias-and-smt.md',
    'app/ict-assistant/knowledge/07-glossary.md', 'app/ict-assistant/knowledge/08-sources.md',
}
CUSTOMER_BASE_SOURCE = {
    'installer/clean_base.py', 'installer/frontend.py',
    'installer/customer_control.py', 'installer/desktop_apps.py', 'installer/desktop_layout_restore.py', 'installer/market_browser.py', 'installer/install_notice.py', 'installer/version_bootstrap.py', 'installer/maintenance_cli.py', 'installer/customer_ingress.py', 'installer/loopback_guard.py', 'installer/tmp_janitor.py', 'installer/INGRESS.md', 'installer/release-trust.json', 'installer/MAINTENANCE.md', 'installer/README.md',
    'customer-docs/GETTING-STARTED.md', 'customer-docs/TEST-CHECKLIST.md', 'customer-docs/UI-THIRD-PARTY-NOTICES.txt',
    'lab/build_source_bundle.py',
}
CUSTOMER_BASE_REQUIRED = CUSTOMER_BASE_SOURCE | CUSTOMER_RUNTIME_SOURCE | {
    'app/ui/package-lock.json', 'app/backend/requirements.txt',
    'app/strategy-catalog.json', 'app/contract-calendar.json',
    'app/backend/strategy_access.py', 'app/strategy-access.json',
    'app/backend/engine_workspace.py', 'app/engine-module-catalog.json',
    'app/backend/guardian_auth.py', 'app/backend/guardian_browser.py', 'app/backend/guardian_service.py',
    'app/backend/maintenance.py', 'app/backend/maintenance_service.py', 'app/backend/trading_service.py',
    'app/backend/trading_routes.py', 'app/backend/trading_ingress_secrets.py',
    'app/backend/trading_browser.py', 'app/backend/trading_feed.py', 'app/backend/trading_calendar.py', 'app/backend/customer_health.py',
    'app/ui/BRAND-ASSETS.json', 'app/ui/licenses/spacegrotesk-OFL.txt',
    'app/ui/licenses/jetbrainsmono-OFL.txt', 'app/ui/licenses/UI-THIRD-PARTY-NOTICES.txt',
}
TRADING_SOURCE = {'app/trading-runtime/decrypt_trading/runtime.py', 'app/trading-runtime/decrypt_trading/vendor/soup_r4/transport.py', 'app/trading-runtime/MANIFEST.json', 'app/trading-runtime/decrypt_trading/native_chart_hook.js', 'app/trading-runtime/decrypt_trading/native_chart_contract.py', 'app/trading-runtime/README.md', 'app/trading-runtime/decrypt_trading/__init__.py', 'app/trading-runtime/decrypt_trading/vendor/soup_r4/settings_contract.json', 'app/trading-runtime/decrypt_trading/native_market_store.py', 'app/trading-runtime/decrypt_trading/source.py', 'app/trading-runtime/decrypt_trading/native_market.py', 'app/trading-runtime/decrypt_trading/vendor/soup_r4/core.py', 'app/trading-runtime/decrypt_trading/http_ingress.py', 'app/trading-runtime/decrypt_trading/vendor/soup_r4/storage.py', 'app/trading-runtime/Caddyfile.example', 'app/trading-runtime/decrypt_trading/ingress.py'}
TRADING_SOURCE |= {'app/trading-runtime/decrypt_trading/retirement.py'}
TRADING_SOURCE |= {'app/trading-runtime/decrypt_trading/customer_contract.py',
                   'app/trading-runtime/decrypt_trading/paths.py',
                   'app/trading-runtime/decrypt_trading/bridge_route.py',
                   'app/trading-runtime/decrypt_trading/bridge_transport.py',
                   'app/trading-runtime/decrypt_trading/bridge_adapters.py'}
# decrypt-bridge-v2: shared receiver/executor, validator and strategy registry.
TRADING_SOURCE |= {'app/trading-runtime/decrypt_trading/bridge_v2.py',
                   'app/trading-runtime/decrypt_trading/bridge_v2_contract.py',
                   'app/trading-runtime/decrypt_trading/bridge_v2_registry.py'}
TRADING_SOURCE |= {'app/trading-runtime/decrypt_trading/bridge_v2_fvg_r5b.py'}   # FVG/IFVG v2 strategy
TRADING_SOURCE |= {'app/trading-runtime/decrypt_trading/bridge_v2_lumi.py'}   # Lumi RTH Gap v2
TRADING_SOURCE |= {'app/trading-runtime/decrypt_trading/bridge_feed.py'}   # the bridge's 1-second feed reader (Lumi)
# OTE Refined Large + Small: decrypt-bridge-v2 strategy modules.
TRADING_SOURCE |= {'app/trading-runtime/decrypt_trading/bridge_v2_ote_refined_large.py',
                   'app/trading-runtime/decrypt_trading/bridge_v2_ote_refined_small.py'}
TRADING_SOURCE |= {'app/trading-runtime/decrypt_trading/bridge_v2_ote_active.py'}   # OTE Active (Silver Bullet) v2
TRADING_SOURCE |= {'app/trading-runtime/decrypt_trading/bridge_v2_order_blocks.py'}   # Order Blocks Decoded
TRADOVATE_LIST = 'modules/tradovate-bridge/runtime-files.txt'


def tradovate_source(root):
    """The bridge module's shipped files, exactly as its runtime-files.txt lists them."""
    listing = Path(root) / TRADOVATE_LIST
    names = listing.read_text().split() if listing.is_file() else []
    return {'modules/tradovate-bridge/' + n for n in names} | {TRADOVATE_LIST}


TRADOVATE_SOURCE = tradovate_source(Path(__file__).resolve().parents[1])
EXACT |= TRADOVATE_SOURCE
CUSTOMER_BASE_REQUIRED |= TRADOVATE_SOURCE
CUSTOMER_BASE_REQUIRED |= {'app/backend/guardian_keyring.py', 'app/backend/guardian_login_browser.py',
                           'app/backend/tradovate_service.py'}
EXACT |= TRADING_SOURCE
CUSTOMER_BASE_REQUIRED |= TRADING_SOURCE
EXACT |= CUSTOMER_RUNTIME_SOURCE | CUSTOMER_BASE_SOURCE
ASSET_EXTENSIONS = {'.ts', '.tsx', '.js', '.jsx', '.css', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.woff', '.woff2'}
FORBIDDEN_PARTS = {'node_modules', '__pycache__', '__tests__', 'tests', 'test', 'state', 'evidence', '.git', 'credentials', 'secrets'}
FORBIDDEN_SUFFIXES = {'.pem', '.key', '.p12', '.pfx', '.sqlite', '.db', '.log', '.zip', '.tar', '.gz'}
SECRET_CONTENT = re.compile(
    rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|'
    rb'\bgh[pousr]_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{30,}\b|'
    rb'\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{32,}\b|\bxox[baprs]-[A-Za-z0-9-]{24,}\b|'
    rb'\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b'
)
REQUIRED = {'app/CONTRACT.md', 'app/backend/server.py', 'app/ui/package.json', 'app/ui/index.html', 'lab/run.py'}


def validate_path(name: str) -> PurePosixPath:
    if not name or '\\' in name or '\x00' in name or re.match(r'^[A-Za-z]:', name):
        raise ValueError('Invalid bundle path')
    p = PurePosixPath(name)
    if p.is_absolute() or any(part in ('..', '.') for part in name.split('/')) or any(not part for part in name.split('/')):
        raise ValueError('Absolute or traversal bundle path rejected')
    return p


def excluded(p: PurePosixPath) -> bool:
    parts = [part.lower() for part in p.parts]
    if any(part in FORBIDDEN_PARTS or part.startswith('.') for part in parts):
        return True
    if p.suffix.lower() in FORBIDDEN_SUFFIXES:
        return True
    return bool(re.search(r'(^|[._-])(secret|secrets|credential|credentials|token|tokens|test|spec)([._-]|$)', p.name.lower()))


def allowed(name: str) -> bool:
    p = validate_path(name)
    # TEST-CHECKLIST.md is a reviewed customer document, not an executable test or fixture.
    # trading_ingress_secrets.py only generates fresh tokens at runtime and never hardcodes a
    # secret value, so it is let through the generic secret-named-file filter below.
    if name in {'customer-docs/TEST-CHECKLIST.md', 'app/backend/trading_ingress_secrets.py'}: return True
    if excluded(p): return False
    if name in EXACT: return True
    if len(p.parts) == 3 and p.parts[:2] == ('app', 'backend'):
        return p.suffix == '.py' and not p.name.startswith('test_')
    if p.parts[:3] == ('app', 'ui', 'src') and len(p.parts) > 3:
        return p.suffix.lower() in ASSET_EXTENSIONS
    if len(p.parts) == 3 and p.parts[:2] == ('app', 'ui'):
        return bool(re.fullmatch(r'tsconfig(?:\.[a-zA-Z0-9_-]+)?\.json|vite\.config\.(ts|js|mts|mjs)', p.name))
    return False


def gather(root: Path) -> dict[str, bytes]:
    root = root.resolve(strict=True)
    found = {}
    # Deliberately visit only source locations, never a developer home or non-source directories.
    for directory in ('app/backend', 'app/ui/src'):
        base = root / directory
        if any(part.is_symlink() for part in [base, *base.parents] if part != root and root in part.parents):
            raise ValueError(f'Symlink source directory rejected: {directory}')
        if not base.exists(): continue
        for path in sorted(base.rglob('*')):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                # Reject even excluded symlinks within scanned source subtrees.
                raise ValueError(f'Symlink source rejected: {relative}')
            if path.is_file() and allowed(relative): found[relative] = path.read_bytes()
    candidates = set(EXACT)
    ui = root / 'app/ui'
    if ui.is_symlink(): raise ValueError('Symlink UI directory rejected')
    if ui.is_dir():
        candidates.update(path.relative_to(root).as_posix() for path in ui.iterdir() if allowed(path.relative_to(root).as_posix()))
    for relative in sorted(candidates):
        path = root / relative
        # Check every parent: resolving first would hide redirected source directories.
        if any(part.is_symlink() for part in [path, *path.parents] if part != root and root in part.parents):
            raise ValueError(f'Symlink source rejected: {relative}')
        if path.is_file() and allowed(relative): found[relative] = path.read_bytes()
    missing = REQUIRED - found.keys()
    if missing: raise ValueError('Required source files missing: ' + ', '.join(sorted(missing)))
    for name, content in found.items():
        if SECRET_CONTENT.search(content): raise ValueError(f'Private credential marker found in {name}; content withheld')
    return dict(sorted(found.items()))


def dependency_inventory(files: dict[str, bytes]) -> list[dict]:
    package = json.loads(files['app/ui/package.json'])
    lock = json.loads(files.get('app/ui/package-lock.json', b'{}'))
    inventory = []
    for scope in ('dependencies', 'devDependencies'):
        for name, version in sorted(package.get(scope, {}).items()):
            if not re.fullmatch(r'(?:@[a-zA-Z0-9._-]+/)?[a-zA-Z0-9._-]+', name): raise ValueError('Invalid declared npm package name')
            pinned = lock.get('packages', {}).get('node_modules/' + name, {})
            inventory.append({'name': name, 'scope': scope, 'declared_version': version,
                'locked_version': pinned.get('version'), 'integrity': pinned.get('integrity'),
                'license_declared_in_lock': pinned.get('license'),
                'license_review_pointer': f'https://www.npmjs.com/package/{name}',
                'review_status': 'not_reviewed'})
    return inventory


def build(root: Path, destination: Path, *, customer_base=False) -> dict:
    files = gather(root)
    if customer_base:
        missing = CUSTOMER_BASE_REQUIRED - files.keys()
        if missing:
            raise ValueError("Customer base inputs missing: " + ", ".join(sorted(missing)))
    manifest = {'schema': SCHEMA, 'artifact_type': 'app_source_not_os_image',
        'customer_base_recipe': {'included': CUSTOMER_BASE_SOURCE <= files.keys(), 'required_by_build': customer_base, 'clean_vm_verified': False},
        'files': [{'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for name, data in files.items()],
        'npm_declared_dependencies': dependency_inventory(files),
        'limitations': ['Source with optional installation recipe; not a bootable image or verified installed OS', 'No generated secrets, binaries, node_modules or production state',
                       'Dependency list covers declared npm dependencies only; not a full SBOM or license/security audit',
                       'Private-key/token marker scan is limited and does not prove absence of all secrets',
                       'Customer runtime source and a manifest, when present, are distribution recipe inputs, not bundled OpenClaw or Node binaries',
                       'No signing, clean-VM installation, broker commissioning or hardware qualification claimed'],
        'determinism': 'Sorted paths; uid/gid 0; file mode0644; tar and gzip timestamps0; no host paths'}
    manifest_data = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode()
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode='wb', filename='', mtime=0, compresslevel=9) as compressed:
        with tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT) as tar:
            for name, data in sorted({**files, 'SOURCE-MANIFEST.json': manifest_data}.items()):
                validate_path(name)
                info = tarfile.TarInfo('decrypt-os-source/' + name)
                info.size = len(data); info.mode = 0o644; info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ''
                tar.addfile(info, io.BytesIO(data))
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / 'decrypt-os-source.tar.gz'
    archive.write_bytes(buffer.getvalue())
    (destination / 'SOURCE-MANIFEST.json').write_bytes(manifest_data)
    receipt = {'schema': SCHEMA, 'artifact': archive.name, 'sha256': hashlib.sha256(buffer.getvalue()).hexdigest(),
               'files': len(files), 'bytes': len(buffer.getvalue()), 'artifact_type': 'app_source_not_os_image'}
    (destination / 'BUNDLE-RECEIPT.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--customer-base', action='store_true', help='Require the complete customer installation recipe and runtime sources')
    args = parser.parse_args()
    print(json.dumps(build(args.source_root, args.out_dir, customer_base=args.customer_base), indent=2))

if __name__ == '__main__': main()
