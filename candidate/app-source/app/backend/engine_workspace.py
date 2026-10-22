"""The packaged engine and module catalogue, validated against the included code.

Read only: the app shows which engines exist, which are installable and which are coming soon.
Installing and running an engine is trading_service.py; there are no saved drafts.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import sys

SCHEMA = 'decrypt.engine-workspace.v1'
CATALOG_SCHEMA = 'decrypt.engine-module-catalog.v1'
ID = re.compile(r'[a-z][a-z0-9_-]{0,95}')
SHA = re.compile(r'[a-f0-9]{64}')


def encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def validate_catalog(catalog):
    """Only metadata and bounded scalar configuration fields are accepted."""
    if type(catalog) is not dict or catalog.get('schema') != CATALOG_SCHEMA:
        raise ValueError('invalid_engine_module_catalog')
    for key in ('engines', 'modules', 'sourcePairs'):
        rows = catalog.get(key)
        if type(rows) is not list or len(rows) > 100:
            raise ValueError('invalid_catalog_rows')
        seen = set()
        for row in rows:
            if type(row) is not dict or type(row.get('id')) is not str or not ID.fullmatch(row['id']) or row['id'] in seen:
                raise ValueError('invalid_catalog_id')
            seen.add(row['id'])
    pairs = {p['id']: p for p in catalog['sourcePairs']}
    engines = {e['id']: e for e in catalog['engines']}
    for pair in pairs.values():
        if pair.get('engineId') not in engines or pair.get('publicationVerified') is not False or not pair.get('version'):
            raise ValueError('invalid_source_pair')
        artifacts = pair.get('artifacts')
        if type(artifacts) is not list or not artifacts:
            raise ValueError('missing_source_pins')
        for artifact in artifacts:
            if type(artifact) is not dict or type(artifact.get('sha256')) is not str or not SHA.fullmatch(artifact['sha256']):
                raise ValueError('invalid_source_hash')
            ref = artifact.get('evidenceRef')
            if type(ref) is not str or not ref or ref.startswith('/') or '..' in ref.split('/') or '://' in ref:
                raise ValueError('invalid_evidence_reference')
    for kind in ('engines', 'modules'):
        for item in catalog[kind]:
            if (type(item.get('label')) is not str or not item['label']
                    or type(item.get('description')) is not str or 'version' not in item
                    or (kind == 'engines' and 'sourcePairId' not in item)
                    or (kind == 'modules' and (type(item['version']) is not str or not item['version']))):
                raise ValueError('invalid_catalog_metadata')
            if item.get('packageStatus') not in (('not_packaged', 'source_packaged') if kind == 'engines' else ('not_packaged', 'library_included')):
                raise ValueError('unverified_package_status')
            if kind == 'engines':
                pair_id = item.get('sourcePairId')
                if pair_id is not None and (pair_id not in pairs or pairs[pair_id]['engineId'] != item['id']):
                    raise ValueError('invalid_engine_pair')
                if item.get('pairingStatus') != ('source_verified' if item.get('packageStatus') == 'source_packaged' else 'reference_only' if pair_id else 'unverified'):
                    raise ValueError('invalid_pair_status')
            else:
                artifacts = item.get('sourceArtifacts')
                if type(artifacts) is not list or not artifacts:
                    raise ValueError('missing_module_source_pins')
                for artifact in artifacts:
                    ref = artifact.get('evidenceRef')
                    if (type(artifact.get('sha256')) is not str or not SHA.fullmatch(artifact['sha256'])
                            or type(ref) is not str or not ref or ref.startswith('/') or '..' in ref.split('/') or '://' in ref):
                        raise ValueError('invalid_module_source_pin')
                if item.get('scope') not in ('engine', 'system') or type(item.get('compatibility')) is not list:
                    raise ValueError('invalid_module_scope')
                seen = set()
                for match in item['compatibility']:
                    pair_id = match.get('sourcePairId')
                    expected = 'trading-system' if item['scope'] == 'system' else pair_id
                    if (pair_id != expected or (item['scope'] == 'engine' and pair_id not in pairs)
                            or pair_id in seen or match.get('status') not in ('supported', 'reference_only', 'unsupported')):
                        raise ValueError('invalid_module_compatibility')
                    if item['scope'] == 'engine':
                        pins = match.get('sourcePairPins')
                        role_pins = [[a['sha256'] for a in pairs[pair_id]['artifacts'] if a.get('role') == role]
                                     for role in ('engine', 'strategy')]
                        if (type(pins) is not dict or set(pins) != {'engineSha256', 'strategySha256'}
                                or any(len(values) != 1 for values in role_pins)
                                or (pins['engineSha256'], pins['strategySha256']) != tuple(values[0] for values in role_pins)):
                            raise ValueError('invalid_module_pair_pin')
                    seen.add(pair_id)
            definitions = item.get('configurationFields')
            if type(definitions) is not list or len(definitions) > 20 or type(item.get('defaultConfiguration')) is not dict:
                raise ValueError('invalid_configuration_schema')
            seen = set()
            for field in definitions:
                key = field.get('key')
                if type(key) is not str or not re.fullmatch(r'[a-z][A-Za-z0-9]{0,47}', key) or key in seen:
                    raise ValueError('invalid_configuration_field')
                seen.add(key)
                if type(field.get('required')) is not bool or field.get('type') not in ('enum', 'integer', 'number', 'boolean'):
                    raise ValueError('invalid_configuration_type')
                if field['type'] == 'enum':
                    choices = field.get('options')
                    if type(choices) is not list or not 1 <= len(choices) <= 30 or any(type(v.get('value')) is not str for v in choices):
                        raise ValueError('invalid_configuration_options')
            if set(item['defaultConfiguration']) - seen:
                raise ValueError('invalid_configuration_defaults')
    return catalog


def load_catalog():
    path = Path(__file__).resolve().parents[1] / 'engine-module-catalog.json'
    if path.stat().st_size > 262144:
        raise ValueError('engine_module_catalog_too_large')
    catalog = validate_catalog(json.loads(path.read_text(encoding='utf-8')))
    directory = path.parent / 'trading-runtime'
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    from decrypt_trading.source import package_catalog
    packages = {item['pairId']: item for item in package_catalog()}
    for engine in catalog['engines']:
        if engine['packageStatus'] != 'source_packaged':
            continue
        pair = next(item for item in catalog['sourcePairs'] if item['id'] == engine['sourcePairId'])
        package = packages.get(pair['id'])
        pins = {item['role']: item['sha256'] for item in pair['artifacts'] if item['role'] in ('engine', 'strategy')}
        expected_engine = 'soup' if package and package['engineId'] == 'turtle-soup' else package['engineId'] if package else None
        if (not package or engine['id'] != expected_engine or engine['version'] != package['version']
                or pins != {'engine':package['engineSha256'], 'strategy':package['strategySha256']}):
            raise ValueError('included_engine_integrity_failed')
    # Included code is checked at its fixed package path, never at evidenceRef
    # or another caller-supplied path. Historical archives are not read.
    packaged_libraries = {}
    for module in catalog['modules']:
        if module['packageStatus'] != 'library_included':
            continue
        library = packaged_libraries.get(module['id'])
        pins = [a['sha256'] for a in module['sourceArtifacts'] if a['role'] == 'implementation']
        if (library is None or library.is_symlink() or not library.is_file()
                or library.stat().st_size > 131072 or len(pins) != 1):
            raise ValueError('included_library_integrity_failed')
        source = library.read_bytes()
        if hashlib.sha256(source).hexdigest() != pins[0]:
            raise ValueError('included_library_integrity_failed')
        # Read literal contract metadata from these same verified bytes. Do not
        # execute the observer or import historical engine code to check a pin.
        metadata = {}
        for statement in ast.parse(source).body:
            if (isinstance(statement, ast.Assign) and len(statement.targets) == 1
                    and isinstance(statement.targets[0], ast.Name)
                    and statement.targets[0].id in ('MODULE_ID', 'VERSION', 'PAIR_REFERENCES')):
                key = statement.targets[0].id
                if key in metadata:
                    raise ValueError('included_library_contract_invalid')
                metadata[key] = ast.literal_eval(statement.value)
        if (metadata.get('MODULE_ID') != module['id'] or metadata.get('VERSION') != module['version']
                or type(metadata.get('PAIR_REFERENCES')) is not dict or module['scope'] != 'engine'):
            raise ValueError('included_library_contract_invalid')
        pairs = {pair['id']: pair for pair in catalog['sourcePairs']}
        for match in module['compatibility']:
            if match['status'] == 'unsupported':
                continue
            pair = pairs[match['sourcePairId']]
            role_pins = [[a['sha256'] for a in pair['artifacts'] if a.get('role') == role]
                         for role in ('engine', 'strategy')]
            if (any(len(values) != 1 for values in role_pins)
                    or metadata['PAIR_REFERENCES'].get(pair['id']) != tuple(values[0] for values in role_pins)):
                raise ValueError('included_library_pair_mismatch')
    return catalog


def engine_catalog():
    return {'schema': SCHEMA, 'setupOnly': True, 'catalog': load_catalog()}
