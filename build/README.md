# Building a Decrypt OS image

Two architectures share one build (`build/guest_build.py --arch`): **aarch64**
(default; macOS host + Lima ARM64 builder, `build/build.sh`) and **x86_64**
(native Arch Linux x86_64 builder, `build/build-x86_64.sh`, see
[x86_64 builds](#x86_64-builds) below). The architecture selects one entry of
`TARGETS` in `guest_build.py`: profile, package lock, mirrors, kernel and
bootloader file names, export formats.

One command builds an ARM64 release from a repo checkout:

```sh
build/build.sh --version beta4 \
  --signing-key /path/outside/repo/release-signing-ed25519.pem \
  --public-key  /path/outside/repo/release-public.pem
```

It builds the **committed** `HEAD` (uncommitted changes are ignored) and writes to
`$DECRYPT_BUILD_OUT/<image-id>-<time>/` (default `~/decrypt-os-build/out`, or `--out`):

| File | What it is |
| --- | --- |
| `decrypt-os-<version>-arm64.qcow2` | the disk, zstd-compressed qcow2 (40 GiB virtual) |
| `decrypt-os-<version>-arm64.utm.tar` | `Decrypt-OS.utm` bundle for UTM (same disk) |
| `IMPORT.md` | short import instructions |
| `SHA256SUMS` | hashes of the three files above |
| `MANIFEST.json` | image id, source tree id (`sourceTree`), artifact names, sizes and hashes |
| `MANIFEST.json.sig` | Ed25519 signature over the exact bytes of `MANIFEST.json` |
| `build.log`, `TIMINGS.json` | build log and per-stage seconds (not part of the release) |

Without `--signing-key` the manifest is written but not signed. The private key is
only read on the host by OpenSSL 3 (`openssl` on PATH, or set `DECRYPT_OPENSSL`); it never enters the
VM or the repo.

Check a release:

```sh
openssl pkeyutl -verify -pubin -inkey release-public.pem -rawin -in MANIFEST.json -sigfile MANIFEST.json.sig
shasum -a 256 -c SHA256SUMS
```

`sourceTree` is `git rev-parse <tag>^{tree}`, which is the same in the public
repo as in any other clone that has the tag, so it identifies the exact source
without naming a development commit. A matching source tarball (no commit-id
header) is `git archive --format=tar.gz -o decrypt-os-<tag>-source.tar.gz <tag>^{tree}`.

## What happens

`build.sh` (host) streams `git archive` of `HEAD`'s tree into the builder VM and starts
`build/guest_build.py` there as root, as a systemd unit in a private mount
namespace. Closing the host terminal does not stop the build. The stages:

| Stage | Work |
| --- | --- |
| `disk` | 40 GiB sparse raw disk, GPT: 1 GiB FAT32 EFI + btrfs root |
| `base` | `pacstrap` of the base packages (`candidate/profiles/arm64-image-factory.json`) and the desktop packages (`candidate/base/desktop-packages.txt`) from the cached package repo, signatures checked |
| `desktop` | fstab, hostname, locale, locked root, `%wheel` sudo (password required), systemd-boot, then the Omarchy desktop, skeleton, theme and SDDM greeter via `candidate/base/unattended/phase_b.py` |
| `app` | `build/target_app.py` runs **inside the image**: Node + OpenClaw + Python venv into `/usr/local/lib/decrypt-os-runtime`, the UI built offline with npm, and the app files rendered by `installer/clean_base.py` |
| `finalize` | first-start service (`candidate/firstboot/stage.py`), checks, cleanup, unmount. The raw disk is now bootable |
| `export` | qcow2, UTM tar, `SHA256SUMS` |

Stop early with `--stop-after <stage>`; the work dir stays in the VM under
`/var/lib/decrypt-builder/work/`. Add `--keep-work` or `--keep-raw` to keep it after a
full build. A failed build always keeps its work dir; delete it when done.

## Account-free image

The image has no user account, no password, no machine id, no SSH host keys, no
pacman keyring and no customer configuration. Decrypt user services are installed
but not enabled. On first start `decrypt-firstboot.service` creates the account,
identities and the login, and `decrypt-pacman-keyring.service` creates the
machine's own package keyring. `guest_build.py` refuses to finish if any user
account, unlocked root, `NOPASSWD` sudo rule, machine identity or enabled
Decrypt user service is found.

## Builder VM and cache

The builder is a Lima VM (Ubuntu 24.04 ARM64, Apple VZ, 4 CPU, 8 GiB) created from
`factory/builder/lima-builder.yaml`, default name `decrypt-builder`. Set
`LIMA_HOME`, `DECRYPT_BUILDER_VM`, `DECRYPT_BUILDER_CACHE` and `DECRYPT_BUILD_OUT`
in the environment or in an uncommitted `build/local.env`. Builder state lives in
the VM under `/var/lib/decrypt-<name>`, where `<name>` is the VM name without a
leading `decrypt-` (so `decrypt-builder` uses `/var/lib/decrypt-builder`);
`DECRYPT_BUILDER_STATE` overrides it. The VM needs
`qemu-utils`, `btrfs-progs`, `dosfstools` and `python3`.

Expensive inputs are cached in the VM under `/var/lib/decrypt-builder/cache`:

| Path | Content | Refill |
| --- | --- | --- |
| `toolroot/` | Arch Linux ARM root with `pacman`, `pacstrap`, `arch-chroot`, keyring | extract `alarm/` with `factory/builder/bootstrap_alarm_root.sh --inputs-json build/inputs.json ...`, then `pacman -S arch-install-scripts python` |
| `alarm/` | signed `ArchLinuxARM-aarch64-latest.tar.gz` + signing key | archlinuxarm.org |

`bootstrap_alarm_root.sh` takes the downloaded archive/signature/keyring as
file paths (those can't live in the repo), but reads their expected sha256
and the ALARM signing-key fingerprint from `build/inputs.json`'s `"alarm"`
section when `--inputs-json` is given, instead of the operator retyping
them. ALARM's "latest" rootfs changes periodically; when a fresh download's
hashes no longer match, recompute them (`sha256sum` the new archive,
signature and `archlinuxarm-build-key.gpg`) and update `build/inputs.json`.
The signing-key fingerprint itself is stable and does not need refreshing.
| `pkg/aarch64/` | offline package repo `decrypt-offline` (packages, `.sig`, db) | `build.sh --online` |
| `omarchy/<commit>/omarchy.tar` | Omarchy source at the commit pinned in `build/inputs.json` | `factory/builder/fetch_omarchy.py` |
| `runtime-inputs/` | Node tarball, OpenClaw lock, Python wheels + metadata | `factory/builder/fetch_runtime_inputs.py` |
| `npm-archives/` | npm tarballs for the UI and OpenClaw locks, named `sha256(url).tgz` | `build.sh --online` fetches missing ones |

`--online` installs packages from the Arch Linux ARM mirrors instead of the cached
repo, keeps the downloaded packages in `pkg/aarch64/` and rebuilds the
`decrypt-offline` database from exactly what was installed, so the next offline
build reproduces it. Use it after changing a package list.

All mirrors in `build/guest_build.py`'s `ALARM_MIRRORS` are HTTPS (Arch Linux
ARM's own default mirrorlist is plain HTTP; a network-path attacker on HTTP can
still serve older signed-but-vulnerable packages). The built image's own
`/etc/pacman.d/mirrorlist` is overwritten with the same HTTPS list, so a later
`pacman -Syu` on the customer's machine does not fall back to HTTP.

## Package lock

`build/arch-package-lock.json` pins every package in the offline repo cache
(name, version, sha256) as of the last refresh. `check_image` (the `finalize`
stage) fails the build if the image's actual installed package set
(`pacman -Q`) has anything missing from the lock, at the wrong version, or
whose cached file no longer matches the pinned hash.

Refresh it after `build/build.sh --online` (or any other change to the
offline repo cache):

```sh
limactl shell --workdir / decrypt-builder -- sudo python3 \
  "$(pwd)/build/generate_package_lock.py" \
  --repo /var/lib/decrypt-builder/cache/pkg/aarch64 \
  --output "$(pwd)/build/arch-package-lock.json"
```

Commit the result.

## x86_64 builds

The x86_64 image builds **natively on an Arch Linux x86_64 machine or VM**
(KVM on an Intel/AMD host), not through Lima. `build/build-x86_64.sh` runs on
that builder as root, archives the committed tree like `build.sh`, runs
`guest_build.py --arch x86_64` in its own systemd unit and writes the release
**unsigned** (the signing key never goes to the builder).

| | aarch64 | x86_64 |
| --- | --- | --- |
| Profile | `candidate/profiles/arm64-image-factory.json` | `candidate/profiles/x86-64-image-factory.json` |
| Tool root | ALARM rootfs, `build/inputs.json` `"alarm"` | Arch bootstrap tarball from a dated archive snapshot, `build/inputs-x86_64.json` `"archlinux"` (sha256 + release-key fingerprint; key in `build/keys/`) |
| Build-time packages | ALARM HTTPS mirrors | `archive.archlinux.org/repos/<date>` snapshot (`packageSnapshot`), so `--online` is reproducible |
| Package lock | `build/arch-package-lock.json` | `build/arch-package-lock-x86_64.json` |
| Kernel / initramfs | `Image`, `linux-aarch64` | `vmlinuz-linux`, `linux`, early `intel-ucode`/`amd-ucode`; initramfs without `autodetect` so it boots on any PC/hypervisor |
| UEFI loader | `systemd-bootaa64.efi` -> `EFI/BOOT/BOOTAA64.EFI` | `systemd-bootx64.efi` -> `EFI/BOOT/BOOTX64.EFI` |
| Node runtime | `node-v24.19.0-linux-arm64` | `node-v24.19.0-linux-x64` |
| Outputs | `*-arm64.qcow2`, `*-arm64.utm.tar` | `*-x86_64.qcow2`, `*-x86_64.vmdk` (VirtualBox/VMware), `IMPORT.md` from `build/IMPORT-x86_64.md` |

Builder packages (Arch): `arch-install-scripts btrfs-progs dosfstools e2fsprogs
util-linux qemu-img lua54 python git libarchive gnupg curl`. Builder state:
`/var/lib/decrypt-builder` (`DECRYPT_BUILDER_STATE`), output
`/var/lib/decrypt-builder/out` (`DECRYPT_BUILD_OUT`).

Fill the cache once, as root, from the checkout:

```sh
sudo factory/builder/prepare_x86_64_cache.sh toolroot   # download + verify + extract the pinned Arch bootstrap
sudo factory/builder/prepare_x86_64_cache.sh omarchy    # Omarchy at the pinned commit
sudo factory/builder/prepare_x86_64_cache.sh runtime DIR
#   DIR holds the reviewed openclaw.json, npm/package.json and npm/package-lock.json
#   (architecture independent; copy them from an existing builder cache's runtime-inputs/).
#   Node x64, OpenClaw and the x86_64 wheels are downloaded and checked against the pins.
```

First build (no package lock yet): install from the snapshot, then generate and
commit the lock, then build the release:

```sh
sudo build/build-x86_64.sh --online --stop-after base
sudo python3 build/generate_package_lock.py --arch x86_64 \
  --repo /var/lib/decrypt-builder/cache/pkg/x86_64 --output build/arch-package-lock-x86_64.json
git add build/arch-package-lock-x86_64.json && git commit -m "x86_64 package lock"
sudo build/build-x86_64.sh --online --unsigned-dev
```

Until the lock is generated, `build/arch-package-lock-x86_64.json` is an empty
placeholder and the `finalize` stage refuses to finish.

Signing happens on the release host. Copy only `MANIFEST.json` and
`SHA256SUMS` from the output directory (never the disk images), then:

```sh
build/sign_manifest.sh --dir <dir> --signing-key /path/outside/repo/release-signing-ed25519.pem
```

and copy `MANIFEST.json.sig`, `MANIFEST.json.sshsig` and `allowed_signers`
back next to the images. `sign_manifest.sh` refuses a manifest whose artifact
list differs from `SHA256SUMS`.

## Tests

`python3 -m pytest build/test_build.py build/test_build_x86_64.py` checks the
pure helpers (pacman config, fstab, boot entry, UTM bundle layout and
determinism, checksums, npm cache lookup, the x86_64 target: profiles, pins,
bootloader/kernel names, lock arch, export formats, kit naming). The real build
only runs in the builder VM.
