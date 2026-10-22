#!/usr/bin/env bash
# Prepare the build cache on a native Arch Linux x86_64 builder for
# build/build-x86_64.sh. Run as root from a repo checkout:
#
#   sudo factory/builder/prepare_x86_64_cache.sh toolroot   # pinned, signed Arch bootstrap -> cache/toolroot
#   sudo factory/builder/prepare_x86_64_cache.sh omarchy    # Omarchy at the commit pinned in build/inputs.json
#   sudo factory/builder/prepare_x86_64_cache.sh runtime METADATA_DIR
#        # runtime-inputs/ (Node x64, OpenClaw, x86_64 wheels); METADATA_DIR holds the
#        # reviewed openclaw.json and npm/package{,-lock}.json from an existing cache
#
# Each step refuses to overwrite an existing cache entry. Packages
# (cache/pkg/x86_64) and npm archives are filled by the first
# `build/build-x86_64.sh --online` run. State dir: DECRYPT_BUILDER_STATE
# (default /var/lib/decrypt-builder).
set -euo pipefail
umask 022
repo=$(cd -- "$(dirname -- "$0")/../.." && pwd -P)
state=${DECRYPT_BUILDER_STATE:-/var/lib/decrypt-builder}
cache=${DECRYPT_BUILDER_CACHE:-$state/cache}
pins=$repo/build/inputs-x86_64.json
pin() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]][sys.argv[3]])' "$@"; }

[[ $(id -u) == 0 && $(uname -m) == x86_64 ]] || { echo "run as root on the x86_64 builder" >&2; exit 2; }
install -d -m 755 "$state" "$cache"

case ${1:-} in
  toolroot)
    [[ ! -e $cache/toolroot ]] || { echo "$cache/toolroot already exists" >&2; exit 1; }
    dl=$state/downloads; install -d -m 755 "$dl"
    url=$(pin "$pins" archlinux source); sig=$(pin "$pins" archlinux signature)
    archive=$dl/${url##*/}
    [[ -f $archive ]] || curl --fail --proto =https --max-redirs 0 -o "$archive" "$url"
    [[ -f $archive.sig ]] || curl --fail --proto =https --max-redirs 0 -o "$archive.sig" "$sig"
    # Verifies sha256 pins + the Arch release signature (gpgv, pinned primary
    # fingerprint), then extracts root.x86_64/ into cache/toolroot.
    bash "$repo/factory/builder/bootstrap_alarm_root.sh" --section archlinux --inputs-json "$pins" \
      --archive "$archive" --signature "$archive.sig" --keyring "$repo/$(pin "$pins" archlinux keyring)" \
      --build-root "$state" --output "$cache/toolroot" --receipt "$state/toolroot-receipt-$(date -u +%Y%m%dT%H%M%SZ).json"
    t=$cache/toolroot
    snapshot=$(pin "$pins" archlinux packageSnapshot)
    printf 'Server = %s/$repo/os/$arch\n' "${snapshot%/}" > "$t/etc/pacman.d/mirrorlist"
    # pacman needs its root to be a mount point (disk-space check), so bind the
    # tool root onto itself while arch-chroot runs.
    mount --bind "$t" "$t"; trap 'umount -R "$t" 2>/dev/null || true' EXIT
    # Tool-root keyring (never shipped in the image) + the tools guest_build.py
    # runs inside it: pacstrap/arch-chroot (arch-install-scripts), repo-add
    # (pacman) and python.
    arch-chroot "$t" pacman-key --init
    arch-chroot "$t" pacman-key --populate archlinux
    arch-chroot "$t" pacman -Sy --noconfirm --needed archlinux-keyring
    arch-chroot "$t" pacman -S --noconfirm --needed arch-install-scripts python
    rm -f "$t/var/cache/pacman/pkg/"*
    umount -R "$t"; trap - EXIT
    echo "toolroot ready: $t"
    ;;
  omarchy)
    commit=$(pin "$repo/build/inputs.json" omarchy commit)
    repository=$(pin "$repo/build/inputs.json" omarchy repository); repository=${repository#https://github.com/}
    install -d -m 755 "$cache/omarchy"
    python3 -B "$repo/factory/builder/fetch_omarchy.py" --repository "$repository" --commit "$commit" \
      --destination "$cache/omarchy/$commit"
    want=$(pin "$repo/build/inputs.json" omarchy archiveSha256)
    got=$(sha256sum "$cache/omarchy/$commit/omarchy.tar" | cut -d' ' -f1)
    [[ $got == "$want" ]] || { echo "omarchy.tar sha256 $got != pinned $want" >&2; exit 1; }
    echo "omarchy ready: $cache/omarchy/$commit/omarchy.tar"
    ;;
  runtime)
    [[ -n ${2:-} ]] || { echo "usage: $0 runtime METADATA_DIR" >&2; exit 2; }
    python3 -B "$repo/factory/builder/seed_runtime_inputs.py" --metadata-from "$2" --output "$cache/runtime-inputs"
    ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
