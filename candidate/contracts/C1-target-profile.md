# C1: target profile

Schema identifier: `decrypt.target-profile.v1`.

All fields in this table are required. Objects reject unlisted fields. The profile is a signed media member and contains no host filesystem paths or personal data.

| Field | Type and rule |
| --- | --- |
| `schema` | Exact schema identifier above |
| `id` | Identifier, unique within the media |
| `kind` | `qualification`, `dryrun-loop` or `image-build` |
| `arch` | `aarch64` or `x86_64`; an implementation must reject an unsupported architecture |
| `hypervisors` | Nonempty, unique array of allowed `systemd-detect-virt` identifiers |
| `vmName` | Expected target identifier, distinct from any host-protected name |
| `hostname` | Expected installed hostname, a valid single DNS label |
| `user` | Generic local username matching `[a-z_][a-z0-9_-]{0,31}`; not root or a system/reserved account |
| `disk` | `{device, bytes}`; explicit absolute Linux block-device path and positive exact integer byte size |
| `partitionNaming` | `suffix` for `/dev/vda1`, or `p-suffix` for `/dev/loop0p1` and NVMe |
| `liveRoot` | `{allowedFsTypes, requireRamBacked}`; nonempty string array and boolean |
| `kernelPackage` | Package name; `linux-aarch64` for the initial ARM qualification profile |
| `basePackages` | Nonempty unique package-name array, including `kernelPackage` |
| `desktopPackageList` | Relative signed-media path to the exact desktop root list |
| `network` | `{phaseA, phaseB, policyId}`; phase A must be `offline`; phase B is `offline` or `pinned-runtime-only`; policy ID binds the reviewed isolation policy |
| `logChannel` | `{kind, label, uuid}`; initial kind `log-disk`, label `DECRYPT_LOG`, `uuid` must be null in the reusable signed profile; the actual per-run UUID is bound exclusively through C5 and the installer state record; other transports require a contract revision after a native spike |
| `graphics` | `{policy, softwareRendering}`; policy identifier and boolean, no shell command or arbitrary environment map |
| `password` | `native-prompt`, `locked` or `first-boot`; `locked` only for `dryrun-loop`; qualification requires `native-prompt`; image-build requires `first-boot` |
| `notice` | `interactive` or `profile-accepted`; latter only for `qualification` and never general customer acceptance |

The first qualification profile is `lume-arm64-qualification`, for a new disposable qualification VM, `aarch64`, `apple`, and an exact 40 GiB target. It must not name any existing VM. The loop profile must use `dryrun-loop` and a guarded allocated loop device, never a host or guest system disk. Its implementation resolves the actual loop device through an explicitly bound allocation; it must not guess `/dev/loop0`.

Before writing a disk, the guard verifies C5, architecture, hypervisor, live-root backing, native-console policy, exact device identity and byte size, no existing partitions/signatures/mounts, and a complete zero-byte scan immediately before partitioning. Media and log devices must be identified independently by label, UUID and backing-device ancestry and excluded. More than one match, overlapping devices, a device rename, or an ambiguous live-root backing refuses the install. Profile values do not bypass any existing refusal.

Phase A runs in a network namespace with no external network device or default route. Isolation is proved before package resolution and disk writes. Phase B retains the test isolation policy before NetworkManager starts. `pinned-runtime-only` permits only the reviewed runtime downloads with pinned hashes; it does not authorize provider login, trading connectivity, enrollment or other network changes.

The existing base and desktop package roots are the package-set inputs. Package closure, versions and signatures belong in C2, not an unpinned live mirror lookup at install time. Customer image generalization and x86_64 support remain separate qualification work.
