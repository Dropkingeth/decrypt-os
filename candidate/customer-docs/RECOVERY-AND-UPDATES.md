# Keep your workspace recoverable

System care provides encrypted application backups, explicit restored-record reconciliation and signed application updates. Source and local integration checks have passed; native restore/update acceptance remains open for this candidate.

## Application backup

Hold new entries first. The service requires complete fresh native flatness, no working/pending or uncertain orders and stopped writers. Active trades retain supervision when maintenance refuses. Choose a new archive passphrase of at least 12 characters and keep it separately. Decrypt cannot recover it.

Backups contain a consistent snapshot of known application settings and retained trading ledgers. Saved encrypted credentials are opt-in. Browser cookies and browser profiles are excluded. Signed commissioning receipts are retained as data and revalidated against the exact binding, current trust and expiry after restore.

Export through the installed maintenance command into a private folder and keep a copy off the installation disk. Follow `/usr/local/lib/decrypt-os/installer/MAINTENANCE.md` for exact commands. A backup file on the same physical disk is not an independent disaster-recovery copy.

## Restore

Import decrypts and stages a selected archive. Applying it preserves the replaced state in a journal and creates a persistent trading hold. Restart, then use **Reconcile restored records**. Idle/closed ledgers need two distinct current Guardian acquisitions proving their exact account and route are flat. Active or uncertain ledgers remain blocked. An empty installation has an explicit empty-scope check, not a broker-flat assertion.

Successful reconciliation saves evidence before consuming the hold, then requires another restart. Engines still reopen held. No old orders or signals are replayed. Restore does not recover excluded browser sessions/enrollment; use their normal customer sign-in flows.

## Complete VM recovery

Before provider sign-in, shut down the new target normally and retain a private complete cold baseline. Never boot that baseline itself. Verify a separate restored copy while the original is stopped. Check destination identity, available space and volume quota before each copy. Even an account-empty VM contains its local password hash. Keep it private.

Assistant saved-login LUKS storage is separate from application backup. Use **Stop and lock** before a full stopped-VM copy. Never copy an unlocked mount as if it were an encrypted recovery image. Test boot, login, app identity, account-empty state and vault behavior on the restore.

## Application updates and rollback

Stage an authenticated signed release using the installed maintenance CLI. The signer must match the root-owned trust installed with the OS. Activation requires held/flat/stopped state and increasing release sequence. The API and UI must adopt the same verified release after restart. Explicit reconciliation is still required.

Rollback changes compatible application code, preserving newer ledgers and sequence history. It does not downgrade database schemas or the operating system. Invalid-release and offboarding holds cannot be cleared by the restore reconciliation action.

Do not rerun the blank-disk installer to update an existing system. Interrupted setup requires inspected state and the stage-specific recovery instructions; a partial disk is not blank. Preserve failed receipts.

## Support and removal

The installed maintenance guide describes export/import/update commands and local sign-in removal. Local removal retains ledgers and recovery copies; provider token revocation and browser sign-out are separate actions.

`tools/diagnostics.py` on verified media produces a small read-only support summary. It does not read credentials or conversation history, contact providers, change services or send its result. Review it before sharing.
