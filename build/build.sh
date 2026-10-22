#!/usr/bin/env bash
# Build a Decrypt OS ARM64 release from this repo checkout.
#
# Runs on the host (macOS). The heavy work runs as root inside the Linux builder
# VM (a Lima VM, default name "decrypt-builder"; see build/README.md). Signing
# runs here on the host: the private key never enters the VM or the repo.
#
#   build/build.sh [--version V] [--out DIR] [--signing-key PEM] [--public-key PEM]
#                  [--stop-after disk|base|desktop|app|finalize|export]
#                  [--online] [--keep-work] [--keep-raw] [--unsigned-dev]
#
# An export (the default --stop-after) without --signing-key is refused
# unless --unsigned-dev is also given; that flag is for local test builds
# only, never for anything distributed.
set -euo pipefail

repo=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
# Machine-specific settings (LIMA_HOME, DECRYPT_BUILDER_VM, DECRYPT_BUILD_OUT, ...)
# can live in build/local.env, which is not committed.
[[ -f $repo/build/local.env ]] && source "$repo/build/local.env"
[[ -n ${LIMA_HOME:-} ]] && export LIMA_HOME
vm=${DECRYPT_BUILDER_VM:-decrypt-builder}
# Builder state (cache + work dirs) inside the VM: /var/lib/decrypt-<name>, where
# <name> is the VM name without a leading "decrypt-" (decrypt-builder ->
# /var/lib/decrypt-builder).
state=${DECRYPT_BUILDER_STATE:-/var/lib/decrypt-${vm#decrypt-}}
cache=${DECRYPT_BUILDER_CACHE:-$state/cache}
out_root=${DECRYPT_BUILD_OUT:-$HOME/decrypt-os-build/out}
version=$(git -C "$repo" describe --tags --always)
signing_key='' public_key='' stop_after=export unsigned_dev=0
extra=() keep_work=0

while (($#)); do
  case $1 in
    --version) version=$2; shift 2 ;;
    --out) out_root=$2; shift 2 ;;
    --signing-key) signing_key=$2; shift 2 ;;
    --public-key) public_key=$2; shift 2 ;;
    --stop-after) stop_after=$2; shift 2 ;;
    --online) extra+=(--online); shift ;;
    --keep-raw) extra+=(--keep-raw); shift ;;
    --keep-work) keep_work=1; shift ;;
    --unsigned-dev) unsigned_dev=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

# An unsigned release has no publisher-identity proof at all (MANIFEST.json.sig
# is how a downloader tells "built by Decrypt Labs" from "built by anyone with
# this repo"). Require an explicit, named opt-in to export one anyway, so a
# forgotten --signing-key cannot silently ship an unsigned release.
if [[ $stop_after == export && -z $signing_key && $unsigned_dev == 0 ]]; then
  echo "refusing to export an unsigned release: pass --signing-key, or --unsigned-dev for a local-only test build" >&2
  exit 2
fi

commit=$(git -C "$repo" rev-parse HEAD)
# The release records the source *tree* id, not the commit id: the tree id is
# identical in the squashed public repo (tools/make_public_repo.sh), while the
# commit id belongs to the private development history.
tree=$(git -C "$repo" rev-parse "$commit^{tree}")
epoch=$(git -C "$repo" log -1 --format=%ct HEAD)
image_id=$(printf 'decrypt-os-%s-arm64' "$version" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9._\n-' '-')
build_id=$image_id-$(date -u +%Y%m%dT%H%M%SZ)
work=$state/work/$build_id
out=$out_root/$build_id
unit=decrypt-build-$build_id

guest() { limactl shell --workdir / "$vm" -- sudo bash -c "$1"; }

if [[ -n $(git -C "$repo" status --porcelain) ]]; then
  echo "note: uncommitted changes are NOT built; building commit $commit" >&2
fi
if [[ -n $signing_key && ! -r $signing_key ]]; then
  echo "signing key not readable: $signing_key" >&2; exit 2
fi
openssl=${DECRYPT_OPENSSL:-$(command -v openssl)}
"$openssl" version | grep -q '^OpenSSL 3' || { echo "need OpenSSL 3 for Ed25519 signing (put it on PATH or set DECRYPT_OPENSSL)" >&2; exit 2; }

# Privacy gate: fail before any archive/VM/export work if the committed tree
# (exactly what git archive "$tree" below would ship) carries owner-
# identifying data, host paths or key material. DECRYPT_PRIVATE_PATTERNS
# (optionally set in build/local.env, which is gitignored) adds a maintainer's
# own private patterns without putting them in this repo.
python3 "$repo/tools/privacy_check.py" --root "$repo"

mkdir -p "$out"
log=$out/build.log
echo "build $build_id (source tree $tree) -> $out" | tee "$log"

limactl list --format '{{.Status}}' "$vm" | grep -qx Running || {
  echo "starting $vm" | tee -a "$log"; limactl start --tty=false "$vm" >>"$log" 2>&1; }

started=$(date +%s)
guest "test -d '$cache/toolroot' && test -d '$cache/pkg/aarch64' || { echo 'builder cache missing: $cache (see build/README.md)'; exit 1; }"
guest "install -d -m 700 '$work/src'"
# Archiving the tree (not the commit) leaves out git's pax "comment=<commit id>"
# header; --mtime keeps entry times at the commit time, as a commit archive would.
git -C "$repo" -c tar.umask=0022 archive --format=tar --mtime="@$epoch" "$tree" | guest "umask 022; tar -x --no-same-owner --no-same-permissions -C '$work/src'"

# The build runs as its own systemd unit so a dropped host session does not kill it.
guest "systemd-run --quiet --collect --unit='$unit' -p StandardOutput=append:'$work/build.log' -p StandardError=append:'$work/build.log' \
  bash -c 'unshare --mount --propagation private python3 -u $work/src/build/guest_build.py --source $work/src --cache $cache \
  --work $work --image-id $image_id --stop-after $stop_after --source-date-epoch $epoch ${extra[*]:-}; echo \$? > $work/EXIT'"
guest "until test -e '$work/build.log'; do sleep 1; done; tail -n +1 -F '$work/build.log' & t=\$!; until test -e '$work/EXIT'; do sleep 5; done; sleep 1; kill \$t" | tee -a "$log"
status=$(guest "cat '$work/EXIT'")
guest "cat '$work/TIMINGS.json' 2>/dev/null" > "$out/TIMINGS.json" || true
if [[ $status != 0 ]]; then
  echo "BUILD FAILED (exit $status). Work dir kept in $vm: $work" | tee -a "$log"; exit 1
fi
if [[ $stop_after != export ]]; then
  echo "stopped after stage '$stop_after'. Work dir in $vm: $work" | tee -a "$log"; exit 0
fi

# Copy release files out of the VM and check them on the host.
for f in $(guest "ls '$work/release'"); do
  guest "cat '$work/release/$f'" > "$out/$f"
done
(cd "$out" && shasum -a 256 -c SHA256SUMS) | tee -a "$log"

python3 - "$out" "$image_id" "$version" "$tree" "$epoch" <<'PY'
import json, os, sys, time
out, image_id, version, tree, epoch = sys.argv[1:]
artifacts = []
for line in open(os.path.join(out, 'SHA256SUMS')):
    digest, name = line.rstrip('\n').split('  ', 1)
    artifacts.append({'name': name, 'bytes': os.path.getsize(os.path.join(out, name)), 'sha256': digest})
manifest = {'schema': 'decrypt.release-manifest.v1', 'imageId': image_id, 'version': version, 'arch': 'aarch64',
            'sourceTree': tree, 'sourceDateEpoch': int(epoch),
            'builtAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'artifacts': artifacts,
            'signature': {'algorithm': 'Ed25519', 'file': 'MANIFEST.json.sig', 'signs': 'exact bytes of MANIFEST.json'}}
with open(os.path.join(out, 'MANIFEST.json'), 'w') as f:
    json.dump(manifest, f, indent=2, sort_keys=True); f.write('\n')
PY

if [[ -n $signing_key ]]; then
  "$openssl" pkeyutl -sign -inkey "$signing_key" -rawin -in "$out/MANIFEST.json" -out "$out/MANIFEST.json.sig"
  verify_key=$public_key
  [[ -n $verify_key ]] || { verify_key=$out/.verify-public.pem; "$openssl" pkey -in "$signing_key" -pubout -out "$verify_key"; }
  "$openssl" pkeyutl -verify -pubin -inkey "$verify_key" -rawin -in "$out/MANIFEST.json" -sigfile "$out/MANIFEST.json.sig" | tee -a "$log"
  rm -f "$out/.verify-public.pem"
  # Same key in OpenSSH format (kept beside the PEM, outside the repo) lets stock
  # macOS check the release with ssh-keygen, whose LibreSSL openssl cannot do Ed25519.
  ssh_key=${signing_key%.pem}.openssh
  if [[ -f $ssh_key ]]; then
    ssh-keygen -Y sign -f "$ssh_key" -n decrypt-os-release < "$out/MANIFEST.json" > "$out/MANIFEST.json.sshsig"
    printf 'release@decryptlabs.io namespaces="decrypt-os-release" %s\n' "$(ssh-keygen -y -f "$ssh_key")" > "$out/allowed_signers"
    ssh-keygen -Y verify -f "$out/allowed_signers" -I release@decryptlabs.io -n decrypt-os-release \
      -s "$out/MANIFEST.json.sshsig" < "$out/MANIFEST.json" | tee -a "$log"
  fi
else
  echo "no --signing-key: MANIFEST.json left unsigned" | tee -a "$log"
fi

(( keep_work )) || guest "rm -rf '$work'"
echo "done in $(( $(date +%s) - started ))s: $out" | tee -a "$log"
