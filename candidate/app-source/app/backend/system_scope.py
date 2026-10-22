"""Customer component inventory and fixed, read-only setup projection.

No probe, arbitrary path, executable, network, secret or mutation interface.
Implementation inventory is not runtime health or customer commissioning.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

SCHEMA = 'decrypt.system-scope.v1'
MAX_INSPECTED_ROWS = 1000
STEPS = frozenset(('welcome', 'assistant', 'bot', 'connections', 'access', 'review'))
# Code-owned IDs/dependencies ensure missing or edited catalog entries cannot
# silently shrink the known scope or add arbitrary identifiers to diagnostics.
DEPENDENCIES = {
    'setup_api': (), 'setup_ui': ('setup_api',),
    'credential_vault': ('setup_api',),
    'openclaw_runtime': ('setup_api',), 'claude_auth': ('openclaw_runtime',),
    'ict_assistant': ('openclaw_runtime', 'claude_auth'),
    'strategy_catalog': ('setup_api',),
    'access_queue': ('setup_api', 'strategy_catalog'),
    'tradingview': ('access_queue',),
    'guardian_login': ('setup_api',),
    'broker_adapter': ('setup_api',), 'market_data': ('tradingview',),
    'signal_ingress': ('tradingview', 'strategy_catalog'),
    'calendar_reference': ('setup_ui',),
    'rollover_coordinator': ('calendar_reference', 'broker_adapter', 'market_data'),
    'execution_engine': ('strategy_catalog', 'broker_adapter', 'market_data',
                         'signal_ingress', 'rollover_coordinator'),
    'desktop_layout': ('setup_ui',), 'recovery_notifications': ('setup_api',),
    'encrypted_backups': ('credential_vault',), 'signed_updates': ('encrypted_backups',),
}
IMPLEMENTATIONS = frozenset(('implemented_local', 'implemented_opt_in',
                            'reference_only', 'not_implemented'))


def _enum(value, allowed):
    return value if type(value) is str and value in allowed else 'unknown'


def validate_catalog(catalog):
    """Validate packaged metadata; never accept a path or a caller-supplied URL."""
    if type(catalog) is not dict or catalog.get('schema') != SCHEMA:
        raise ValueError('invalid_catalog')
    rows = catalog.get('components')
    if type(rows) is not list or len(rows) != len(DEPENDENCIES):
        raise ValueError('invalid_catalog')
    seen = set()
    for row in rows:
        if type(row) is not dict:
            raise ValueError('invalid_component')
        cid = row.get('id')
        if type(cid) is not str or cid not in DEPENDENCIES or cid in seen:
            raise ValueError('invalid_component')
        seen.add(cid)
        deps = row.get('dependsOn')
        if type(deps) is not list or deps != list(DEPENDENCIES[cid]):
            raise ValueError('invalid_dependencies')
        if (row.get('owner') != 'customer_product'
                or _enum(row.get('implementation'), IMPLEMENTATIONS) == 'unknown'
                or row.get('verification') != 'not_checked_by_snapshot'
                or row.get('runtimeStatus') != 'unknown'
                or row.get('repair') != 'not_available'):
            raise ValueError('invalid_component_status')
    return catalog


def _load_catalog():
    # Fixed source artifact only; never read another agent's workspace/config.
    path = Path(__file__).resolve().parents[1] / 'system-scope.json'
    if path.stat().st_size > 131072:
        raise ValueError('catalog_too_large')
    return validate_catalog(json.loads(path.read_text(encoding='utf-8')))


def _component_snapshot():
    try:
        rows = {row['id']: row for row in _load_catalog()['components']}
        available = True
    except Exception:
        rows, available = {}, False
    return available, [{
        'id': cid, 'owner': 'customer_product',
        'implementation': _enum(rows.get(cid, {}).get('implementation'), IMPLEMENTATIONS),
        'verification': 'not_checked_by_snapshot', 'runtimeStatus': 'unknown',
        'dependsOn': list(deps), 'repair': 'not_available',
    } for cid, deps in DEPENDENCIES.items()]


def _counts(rows, allowed):
    counts = {status: 0 for status in allowed}
    counts['unknown'] = 0
    if type(rows) is not list:
        return {'available': False, 'totalRows': None, 'inspectedRows': 0,
                'allRowsInspected': False, 'statusCountsForInspectedRows': counts}
    inspected = rows[:MAX_INSPECTED_ROWS]
    for row in inspected:
        status = row.get('status') if type(row) is dict else None
        counts[_enum(status, allowed)] += 1
    return {'available': True, 'totalRows': len(rows), 'inspectedRows': len(inspected),
            'allRowsInspected': len(rows) <= MAX_INSPECTED_ROWS,
            'statusCountsForInspectedRows': counts}


def scope_snapshot(store):
    """Return inventory plus sanitized Store.state fields; caller owns permission.

    Never use state['connections']['ai'] for assistant authentication. Never copy
    free text, names, handles, balances, activity, IDs, URLs or exception text.
    State and catalog failures are independent, explicit unavailable evidence.
    """
    catalog_available, components = _component_snapshot()
    try:
        state = store.state()
        if type(state) is not dict:
            raise ValueError('state_unavailable')
        available = state.get('schema') == 'decrypt.os.lab.v1'
        if not available:
            state = {}
    except Exception:
        state, available = {}, False
    onboarding = state.get('onboarding')
    onboarding = onboarding if type(onboarding) is dict else {}
    connections = state.get('connections')
    connections = connections if type(connections) is dict else {}
    return {
        'schema': SCHEMA, 'source': 'customer_inventory_and_local_setup',
        'createdAt': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'catalogAvailable': catalog_available, 'stateAvailable': available,
        'mode': _enum(state.get('mode'), ('lab',)),
        'components': components,
        'setup': {
            'savedStep': _enum(onboarding.get('step'), STEPS),
            'localConnections': {name: _enum(connections.get(name), ('not_connected',))
                                 for name in ('tradingview', 'tradovate')},
            'aiConnectionScope': 'separate_assistant_session',
            'botCatalog': _counts(state.get('bots'), ('not_connected',)),
            'botCatalogScope': 'packaged_strategy_catalog_only',
            'strategyAccessRequest': _enum(((state.get('strategyAccess') or {}).get('latest') or {}).get('status', 'none'),
                                           ('none', 'sending', 'sent', 'not_sent')),
        },
        'botCommissioning': 'not_inspected_by_setup_tool', 'tradingAvailable': False,
        'tradingAvailabilityScope': 'not_inspected_by_setup_tool',
        'externalVerification': 'not_performed', 'settingsChanged': False,
    }
