# Target profiles

These public profiles implement frozen C1. `schema.json` describes their JSON shape;
`base/target_profile.py` additionally enforces reserved names, architecture/kernel,
partition naming, package roots and canonical paths. Supported architecture/kernel
pairs: `aarch64`/`linux-aarch64` and `x86_64`/`linux`. Validation is not native
qualification or permission to write a disk.

| Profile | Use | Account handling |
| --- | --- | --- |
| `arm64-image-factory` | Initial automated ARM64 image factory | `user` is a non-credential build placeholder; never create it. First boot creates the customer account. |
| `loop-arm64-dryrun` | Isolated RAM-root loop fixture | Locked dry-run only; never distribute as a customer image. |
| `lume-arm64-qualification` | New disposable ARM64 qualification VM | Native password prompt; profile acceptance applies only to qualification. |
| `x86-64-image-factory` | Automated x86_64 image factory (`build/guest_build.py --arch x86_64`) | Same as `arm64-image-factory`. |
| `loop-x86-64-dryrun` | x86_64 RAM-root loop fixture | Locked dry-run only; never distribute as a customer image. |
| `kvm-x86-64-qualification` | New disposable x86_64 KVM/QEMU qualification VM | Native password prompt; profile acceptance applies only to qualification. |

The factory and dry-run `/dev/loop0` values are device-family templates, not selected
devices. Pass the actual new allocation to `device_for` and `partition_device`.
No caller may guess a loop number, reuse a nonzero image, or substitute another
qualification disk. All profiles retain the original base and desktop package roots.

`image-build/password:first-boot` does not extend the factory's root authorization step. Customer account creation,
password collection, machine-ID/SSH-key generalization and notice consent belong
to first-boot implementation and native acceptance, not this validator.

Profiles are authenticated signed-media members. The caller supplies their exact
verified raw SHA-256 and C5's independently verified full media SHA-256. Loading a
JSON file or copying a digest from its own header is not signature verification.
