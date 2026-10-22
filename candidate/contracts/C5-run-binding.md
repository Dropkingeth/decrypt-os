# C5: run binding

The host creates one fresh 64 MiB log image per run. Initial filesystem is ext4 with label `DECRYPT_LOG` and a unique UUID. The host writes `/RUN.json` before attachment and never concurrently mounts this writable filesystem while the guest owns it.

Schema `decrypt.install-run.v1` has exactly:

| Field | Rule |
| --- | --- |
| `schema` | `decrypt.install-run.v1` |
| `runId` | Lowercase UUID, generated once per new target |
| `vmName` | Exact name of the newly created intended VM |
| `mediaSha256` | Hash of the complete sealed media image |
| `profileId` | ID of the signed selected profile |
| `profileSha256` | Hash of the exact profile bytes |
| `logUuid` | UUID of this log filesystem |
| `targetBytes` | Exact disk size from C1 |
| `createdAt` | UTC RFC 3339 |

The first four required binding values after the schema are the run ID, VM name, media SHA-256 and profile ID. The additional fields make stale headers, profile drift and swapped disks observable. The host records the header SHA-256 alongside its fresh target creation identity and blank-disk proof. `RUN.json` is immutable throughout the run; the guest appends separate logs and receipts, never overwrites the header.

Before any writes, the guest requires one unambiguous log filesystem and one media filesystem, verifies the exact header schema, validates a nonempty UUID run ID, checks `vmName` against the signed profile, checks profile digest, media binding, log UUID and target size, and separately applies all C1 target guards. A hostname assigned by the installer is not independent VM identity proof. The host must bind its console/boot operation to its fresh creation receipt and attached disk identities. If a required name/device binding is unavailable, refuse instead of assuming device order or trusting a manually assigned hostname.

The guest hashes the read-only media block device with its exact image size when validating `mediaSha256`; the sealed manifest digest and profile digest are checked separately. Reject an ambiguous backing device, unexpected media size, stale complete/failed logs, missing run ID, wrong VM name, wrong profile, changed header, or foreign sequence. Phase B also compares the header digest and run identity with the installer state record. A new run always gets a fresh log disk and fresh target; it cannot adopt another run's log or incomplete target.

A separate qualification step determines how the host can observe logs safely while the guest runs. Never mount the same ext4 filesystem writable on both sides. Until a safe live-read method is proved, only collect a stable copy after the guest is stopped; do not claim live host visibility. A serial/virtiofs replacement needs a revised transport contract and native proof while retaining the same identity checks.
