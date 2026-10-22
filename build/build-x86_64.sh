#!/usr/bin/env bash
# Build a Decrypt OS x86_64 release natively on an Arch Linux x86_64 builder.
#
# This is the x86_64 counterpart of build/build.sh (which drives an ARM64 Lima
# VM from macOS). It runs ON the builder, as root, from a repo checkout, and
# leaves the release UNSIGNED: the release signing key never comes here. Copy
# only MANIFEST.json and SHA256SUMS to the release host, sign them there with
# build/sign_manifest.sh, and copy the signatures back next to the images.
#
#   sudo build/build-x86_64.sh [--version V] [--out DIR] [--online] [--keep-work] [--keep-raw]
#                              [--stop-after disk|base|desktop|app|finalize|export] [--unsigned-dev]
#
# An export (the default --stop-after) needs --unsigned-dev, the same explicit
# opt-in build.sh asks for, because this script never signs.
#
# Builder packages (Arch): arch-install-scripts btrfs-progs dosfstools
# e2fsprogs util-linux qemu-img lua54 python git libarchive gnupg curl.
# Cache: see factory/builder/prepare_x86_64_cache.sh and build/README.md.
set -euo pipefail

repo=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
# The checkout usually belongs to the builder's normal user while this runs as
# root; trust exactly this repo for every git call (also privacy_check.py's).
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="$repo"
state=${DECRYPT_BUILDER_STATE:-/var/lib/decrypt-builder}
cache=${DECRYPT_BUILDER_CACHE:-$state/cache}
out_root=${DECRYPT_BUILD_OUT:-$state/out}
version=$(git -C "$repo" describe --tags --always)
stop_after=export unsigned_dev=0 keep_work=0 online=0
extra=()

while (($#)); do
  case $1 in
    --version) version=$2; shift 2 ;;
    --out) out_root=$2; shift 2 ;;
    --stop-after) stop_after=$2; shift 2 ;;
    --online) extra+=(--online); online=1; shift ;;
    --keep-raw) extra+=(--keep-raw); shift ;;
    --keep-work) keep_work=1; shift ;;
    --unsigned-dev) unsigned_dev=1; shift ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ $stop_after == export && $unsigned_dev == 0 ]]; then
  echo "this script never signs: pass --unsigned-dev to export, then sign MANIFEST.json on the release host (build/sign_manifest.sh)" >&2
  exit 2
fi
[[ $(id -u) == 0 && $(uname -m) == x86_64 && $(uname -s) == Linux ]] || { echo "run as root on an x86_64 Linux builder" >&2; exit 2; }
for tool in losetup sfdisk partx udevadm mkfs.fat mkfs.btrfs blkid fstrim qemu-img unshare systemd-run git python3; do
  command -v "$tool" >/dev/null || { echo "missing builder tool: $tool (see the package list in this script's header)" >&2; exit 2; }
done
command -v luac5.4 >/dev/null || command -v luac >/dev/null || { echo "missing builder tool: luac (pacman -S lua54)" >&2; exit 2; }

commit=$(git -C "$repo" rev-parse HEAD)
tree=$(git -C "$repo" rev-parse "$commit^{tree}")
epoch=$(git -C "$repo" log -1 --format=%ct HEAD)
image_id=$(printf 'decrypt-os-%s-x86_64' "$version" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9._\n-' '-')
build_id=$image_id-$(date -u +%Y%m%dT%H%M%SZ)
work=$state/work/$build_id
out=$out_root/$build_id
unit=decrypt-build-$build_id

if [[ -n $(git -C "$repo" status --porcelain) ]]; then
  echo "note: uncommitted changes are NOT built; building commit $commit" >&2
fi
# Same privacy gate as build.sh, before any archive/build work.
python3 "$repo/tools/privacy_check.py" --root "$repo"

test -d "$cache/toolroot" || { echo "builder cache missing: $cache/toolroot (factory/builder/prepare_x86_64_cache.sh toolroot)" >&2; exit 1; }
if (( online )); then
  install -d -m 755 "$cache/pkg/x86_64" "$cache/npm-archives"
else
  test -f "$cache/pkg/x86_64/decrypt-offline.db.tar.gz" || { echo "no offline package repo yet in $cache/pkg/x86_64: run once with --online" >&2; exit 1; }
fi

mkdir -p "$out"
log=$out/build.log
echo "build $build_id (source tree $tree) -> $out" | tee "$log"
started=$(date +%s)
install -d -m 700 "$work/src"
# Archiving the tree (not the commit) leaves out git's pax "comment=<commit id>"
# header; --mtime keeps entry times at the commit time, as build.sh does.
git -C "$repo" -c tar.umask=0022 archive --format=tar --mtime="@$epoch" "$tree" | (umask 022; tar -x --no-same-owner --no-same-permissions -C "$work/src")

# Own systemd unit, so a dropped SSH session does not kill the build.
systemd-run --quiet --collect --unit="$unit" -p StandardOutput=append:"$work/build.log" -p StandardError=append:"$work/build.log" \
  bash -c "unshare --mount --propagation private python3 -u $work/src/build/guest_build.py --arch x86_64 --source $work/src --cache $cache \
  --work $work --image-id $image_id --stop-after $stop_after --source-date-epoch $epoch ${extra[*]:-}; echo \$? > $work/EXIT"
until test -e "$work/build.log"; do sleep 1; done
tail -n +1 -F "$work/build.log" & t=$!
until test -e "$work/EXIT"; do sleep 5; done
sleep 1; kill $t 2>/dev/null || true
cat "$work/build.log" >> "$log"
status=$(cat "$work/EXIT")
cp "$work/TIMINGS.json" "$out/TIMINGS.json" 2>/dev/null || true
if [[ $status != 0 ]]; then
  echo "BUILD FAILED (exit $status). Work dir kept: $work" | tee -a "$log"; exit 1
fi
if [[ $stop_after != export ]]; then
  echo "stopped after stage '$stop_after'. Work dir: $work" | tee -a "$log"; exit 0
fi

cp "$work/release/"* "$out/"
(cd "$out" && sha256sum -c SHA256SUMS) | tee -a "$log"
python3 "$repo/build/write_manifest.py" "$out" "$image_id" "$version" "$tree" "$epoch" x86_64
echo "MANIFEST.json written UNSIGNED. Sign on the release host:" | tee -a "$log"
echo "  copy $out/{MANIFEST.json,SHA256SUMS} there, run build/sign_manifest.sh --dir <dir> --signing-key <pem>," | tee -a "$log"
echo "  then copy MANIFEST.json.sig, MANIFEST.json.sshsig and allowed_signers back into $out" | tee -a "$log"

(( keep_work )) || rm -rf "$work"
if [[ -n ${SUDO_USER:-} ]]; then chown -R "$SUDO_USER": "$out"; fi
echo "done in $(( $(date +%s) - started ))s: $out" | tee -a "$log"
