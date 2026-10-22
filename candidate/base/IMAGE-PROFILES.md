# Profile-bound image factory interfaces

## Scope

This layer validates C1/C5 inputs and preserves fail-closed target checks. It never creates
an account, collects a password, partitions a device, applies host network policy,
or claims a native installation passed. Image export and first-start setup are
separate workstreams. Any privileged install-time escalation tooling is internal
qualification-only and is not part of this repository.

The default product path is `profiles/arm64-image-factory.json`. Its generic `user`
is a placeholder required by C1, not an account to create, preload or distribute.

## Driver sequence

1. Authenticate signed media and its profile; retain both exact raw SHA-256 hashes.
2. Read the C5 run header from the independently identified `DECRYPT_LOG` filesystem.
3. Exclusively create a new root-owned mode-0600 image with the exact profile byte
   size. Bind its actual device/inode and an explicitly allocated loop device to
   the current C5 run. Do not reuse partial files. Hold the backing/loop allocation
   until cleanup; reject a kernel offset or size limit.
4. Enter a new network namespace without external devices or default routes.
   Run `network_policy.py --apply-live-offline` there. This verifies and records
   the existing isolation; it does not disable the builder's uplink or change
   firewall rules. It refuses the initial PID-1 network namespace.
5. Immediately before the first target write, repeat the guard, perform the full
   zero-byte scan, and repeat collection/validation. Keep the driver offline for
   package resolution and installation. The build driver owns this ordering and phase receipts.

Live hostname must equal the profile's `vmName` plus `-live`; changing a hostname
alone never authorizes an install. C5, boot ID, selected block identity, exact bytes,
blank state and independent media/log ancestry must also match.

### Python API

- `target_profile.load(path, expected_sha256)` reads and validates exact profile bytes.
- `target_profile.validate_run(header, profile, profile_sha256, media_sha256, log_uuid)`
  checks C5 against independently authenticated media and observed evidence UUID.
- `target_profile.device_for(profile, allocation)` resolves the explicit target.
- `target_profile.partition_device(profile, index, allocation)` applies suffix or
  p-suffix naming to the actual allocated device.
- `guard.collect_live(profile, allocation)` only collects native observations.
- `guard.validate_live(snapshot, context=None, profile=..., run_binding=...,
  profile_sha256=..., media_sha256=..., allocation=...)` checks those observations.
- `guard.verify_blank_disk(snapshot, profile, allocation)` scans the entire exact
  target size. It is a read, not proof of future device stability.

Allocation JSON fields: `device`, `bytes`, `createdExclusive:true`, `runId`,
`backingFile`, `backingDevice`, `backingInode`. The loop path, backing path and
stat identities are native observations, not reusable profile values. A private
allocation receipt is read through `guard.read_private_context`.

## Installed binding

Install the exact authenticated profile at `/etc/decrypt-target-profile.json`
(root, 0644), and a root-only `/etc/decrypt-clean-base-context.json` (0600):

```
schema: decrypt.installed-context.v2
profile_sha256: exact raw profile hash
vm_name: profile.vmName
user: profile.user (placeholder only for image-build)
password: profile.password
run_id: C5.runId
disk_bytes: profile.disk.bytes
root_uuid: actual installed filesystem UUID
customer_account_created: false for image-build
```

The context grants no account provisioning permission. `guard.check_installed()`
checks the installed profile, Linux architecture/hostname and actual root UUID,
then returns the context with a `profile` field. Existing desktop helpers that
assume an already-created user require factory-root integration; do not call
those helpers with the image placeholder account.

Image builds install no hand-driven setup launcher; customers finish setup at
first boot. Old v1 records and implicit target defaults refuse.
