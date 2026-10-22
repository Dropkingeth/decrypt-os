#!/usr/bin/env python3
"""Copy encrypted recovery archives using explicit local paths, never API paths.

Create a backup in Decrypt OS first. Import only decrypts/stages; it cannot swap
state, clear a trading hold or execute an order. Native application requires the
held/flat/stopped-worker checks in the authenticated local application.
"""
import argparse
import getpass
from pathlib import Path
import sys
import warnings

INSTALL=Path('/usr/local/lib/decrypt-os')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state-dir',type=Path,default=Path.home()/'.local/state/decrypt-os')
    sub=p.add_subparsers(dest='action',required=True)
    export=sub.add_parser('export');export.add_argument('--backup-id',required=True);export.add_argument('--destination',type=Path,required=True)
    imp=sub.add_parser('import');imp.add_argument('--source',type=Path,required=True)
    update=sub.add_parser('stage-release');update.add_argument('--source',type=Path,required=True)
    args=p.parse_args()
    sys.path.insert(0,str(INSTALL/'app/backend'))
    import maintenance
    from maintenance import Maintenance,MaintenanceError,regular,write_new,MAX_TOTAL,MAX_FILES,need,strict_json
    import version_bootstrap
    trust=version_bootstrap.load_trust(version_bootstrap.TRUST,maintenance)
    manager=Maintenance(args.state_dir,lambda:None,trusted_release_keys=trust)
    try:
        if args.action=='export':
            raw=manager.export_backup(args.backup_id)
            write_new(args.destination.absolute(),raw)
            print('Encrypted archive copied. Existing files are never overwritten.')
        elif args.action=='stage-release':
            source=args.source.absolute()
            envelope=strict_json(regular(source/'RELEASE.json'))
            need(type(envelope) is dict and set(envelope)=={'manifest','signature','keyId'},'invalid_release_manifest')
            files={};total=0
            for path in (source/'payload').rglob('*'):
                need(not path.is_symlink(),'linked_release_path')
                if path.is_file():
                    need(len(files)<MAX_FILES,'release_too_large')
                    raw=regular(path);total+=len(raw);need(total<=MAX_TOTAL,'release_too_large')
                    files[path.relative_to(source/'payload').as_posix()]=raw
            result=manager.stage_release(envelope['manifest'],envelope['signature'],envelope['keyId'],files)
            print('Verified signed release staged: '+result['version'])
            print('Activate through Decrypt OS after held, native-flat and stopped-worker checks.')
        else:
            need(sys.stdin.isatty() and sys.stderr.isatty(),'passphrase_tty_required')
            raw=regular(args.source.absolute(),MAX_TOTAL*2)
            result=manager.import_backup(raw)
            with warnings.catch_warnings():
                warnings.simplefilter('error',getpass.GetPassWarning)
                passphrase=getpass.getpass('Archive passphrase (not stored): ')
            try:stage=manager.prepare_restore(result['backupId'],passphrase)
            finally:passphrase=None
            print('Restore staged: '+stage['restoreId'])
            print('Apply through Decrypt OS after fresh native-flat and stopped-worker checks. Trading remains held.')
    except (MaintenanceError,OSError,ValueError,EOFError,getpass.GetPassWarning):
        raise SystemExit('Recovery file action refused. Check the selected file, private folder and passphrase.') from None

if __name__=='__main__':main()
