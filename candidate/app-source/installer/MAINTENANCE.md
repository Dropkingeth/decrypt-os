# Local recovery and application maintenance

These tools are candidate source. Native install, crash/reboot recovery and signed
update/rollback acceptance must be recorded for the exact shipped build before
those features are called qualified.

## Back up

In Decrypt OS, create a recovery backup with a new archive passphrase of at least
12 characters. Keep that passphrase separately; it is not saved or recoverable.
The encrypted archive covers a consistent snapshot of application settings and
trading ledgers. It excludes provider credentials by default. Explicit opt-in adds
encrypted vaults and their matching encrypted route/history files. Browser cookies
and profiles are never included.

Use the archive ID shown by the application to copy an encrypted archive into an
existing private folder you own (mode 0700). The destination must not exist:

```sh
/usr/local/lib/decrypt-os-runtime/python/bin/python -E -s -B \
  /usr/local/lib/decrypt-os/installer/maintenance_cli.py export \
  --backup-id ARCHIVE_ID --destination /absolute/private/folder/recovery.json
```

Keep a second copy away from the installation disk. An archive present only on the
same disk is not a disaster-recovery backup. An export command does not prove that
the archive can be restored; complete the bounded import and native restore checks.

## Restore

On the customer installation, import the encrypted archive using an explicit local
path. The command prompts for its passphrase without echoing or storing it:

```sh
/usr/local/lib/decrypt-os-runtime/python/bin/python -E -s -B \
  /usr/local/lib/decrypt-os/installer/maintenance_cli.py import \
  --source /absolute/private/folder/recovery.json
```

Import only decrypts, validates and stages the archive. Apply the returned restore
ID through the local application. State swaps require its private maintenance lock,
stopped workers, held entries and a fresh, complete native-flat reading. A failed
or stale broker reading cannot authorize a restore. Previous state is retained in
a local recovery transaction; an interrupted transaction can only be rolled back
if every current file still matches its recorded before/after state. Newer state
is preserved instead of being overwritten.

**Restored trading remains held.** The durable `maintenance/hold.json` is not
cleared by restart, successful login or successful database restoration. No orders
are replayed automatically. After restarting, use **Reconcile restored records**
under System care. Idle or closed ledgers must still match the retained restore
transaction and the current customer installation. Guardian must supply two
separate fresh, complete, flat readings for the exact account and route; clicking
twice on the same reading does not count. If the first reading expires, repeat the
check. Any active trade, unknown command, unmatched route, working order or position
keeps the hold in place. A setup with no configured account or engines can complete
an explicit empty-scope check without asserting anything about a broker.

A successful check saves its receipt before consuming the hold, then requires
another app restart. Engines reopen held; enabling trading is a separate action
with its existing qualification checks. Offboard and invalid-release-fallback
holds cannot be cleared through this action. Provider sign-ins not included in the
archive must be re-established. Browser cookies must be
restored or reconfigured separately through their normal customer flows.

## Signed update and rollback

Obtain an authenticated release directory containing `RELEASE.json` and `payload/`.
Its Ed25519 signer must match a root-owned key installed with Decrypt OS. The
installer never trusts a signing key supplied by an update, API request or browser.
No development/test signing key is included in shipping defaults.

```sh
/usr/local/lib/decrypt-os-runtime/python/bin/python -E -s -B \
  /usr/local/lib/decrypt-os/installer/maintenance_cli.py stage-release \
  --source /absolute/private/folder/signed-release
```

Staging checks the signed canonical manifest, exact bounded file inventory, every
hash, schema compatibility and acceptance expiry. Payloads are stored as immutable
version directories. Activate through the local application only while held,
native-flat and stopped. Activation requires a monotonically increasing sequence;
rollback retains that high-water mark. Rollback changes application code, not
trading ledgers. Only the current compatible state schema is supported. First-update
rollback returns to the verified installed package; later rollback returns to the
previous verified release. Restart is required after activation or rollback.
Then explicitly reconcile the held account from System care. This additionally
requires the running API's root-verified adoption record and loaded source to match
the exact selected release; changing a pointer alone is insufficient. Completion
keeps entries held and requires a restart before opening workers.

The installed bootstrap verifies the selected payload again. The UI follows the
running API's verified release, bound to its boot ID, process ID and process start
time, rather than independently choosing a new pointer. Invalid update state falls
back to a separately verified installed package with trading held. A previously
accepted release does not expire while running; expiry limits new acceptance.

## Remove saved sign-ins

The application can remove local encrypted sign-in files while held, native-flat
and stopped. It disables Guardian monitoring and leaves ledgers, recovery copies
and encrypted backups intact. It does **not** claim to revoke provider tokens,
or sign out browser sessions. Complete those actions through the relevant
provider or browser. Whole-system
uninstall and irreversible data erasure are separate operations, not performed by
this module.
