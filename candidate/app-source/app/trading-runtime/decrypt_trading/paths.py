"""Where Decrypt OS keeps trading data. One root, one environment override per part.

  ~/.local/share/decrypt-os/          DECRYPT_OS_DATA
    tvbridge/                         TVBRIDGE_ROOT        bridge config, tokens, journals, firing
    tvbridge-admin/                   TVBRIDGE_ADMIN_ROOT  Bridge Advanced profile and state
    tvbridge-state/                   TVBRIDGE_STATE       bridge broker events, login-guard state
    soup/                             DECRYPT_SOUP_ROOT    Soup engine files the bridge reads

The bridge module's setup.py and units use the same defaults (stdlib copies, no import).
"""
import os
from pathlib import Path


def _env_path(name, default):
    value = os.environ.get(name)
    return Path(os.path.expanduser(value)) if value else default


def data_root():
    return _env_path('DECRYPT_OS_DATA', Path.home() / '.local/share/decrypt-os')


def tvbridge_root():
    return _env_path('TVBRIDGE_ROOT', data_root() / 'tvbridge')


def bridge_state_root():
    return _env_path('TVBRIDGE_STATE', data_root() / 'tvbridge-state')


def soup_root():
    return _env_path('DECRYPT_SOUP_ROOT', data_root() / 'soup')
