#!/usr/bin/env bash
# Sign an unsigned release's MANIFEST.json on the release (signing) host.
#
#   build/sign_manifest.sh --dir DIR --signing-key PEM [--public-key PEM]
#
# For builds made elsewhere (build/build-x86_64.sh on the x86_64 builder):
# copy only MANIFEST.json and SHA256SUMS into DIR -- never the disk images --
# run this, then copy MANIFEST.json.sig, MANIFEST.json.sshsig and
# allowed_signers back next to the images. The private key is only read here
# by OpenSSL 3 and never leaves this host. The signing steps are the same as
# build/build.sh's.
set -euo pipefail

dir='' signing_key='' public_key=''
while (($#)); do
  case $1 in
    --dir) dir=$2; shift 2 ;;
    --signing-key) signing_key=$2; shift 2 ;;
    --public-key) public_key=$2; shift 2 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ -n $dir && -n $signing_key ]] || { echo "need --dir and --signing-key" >&2; exit 2; }
[[ -r $signing_key ]] || { echo "signing key not readable: $signing_key" >&2; exit 2; }
[[ -f $dir/MANIFEST.json && -f $dir/SHA256SUMS ]] || { echo "need MANIFEST.json and SHA256SUMS in $dir" >&2; exit 2; }
openssl=${DECRYPT_OPENSSL:-$(command -v openssl)}
"$openssl" version | grep -q '^OpenSSL 3' || { echo "need OpenSSL 3 for Ed25519 signing (put it on PATH or set DECRYPT_OPENSSL)" >&2; exit 2; }

# The manifest must list exactly the artifacts and hashes in SHA256SUMS: what
# gets signed is what the builder checked.
python3 - "$dir" <<'PY'
import json, os, sys
d = sys.argv[1]
manifest = json.load(open(os.path.join(d, 'MANIFEST.json')))
sums = {}
for line in open(os.path.join(d, 'SHA256SUMS')):
    digest, name = line.rstrip('\n').split('  ', 1)
    sums[name] = digest
listed = {a['name']: a['sha256'] for a in manifest['artifacts']}
if manifest.get('schema') != 'decrypt.release-manifest.v1' or listed != sums:
    raise SystemExit('MANIFEST.json does not match SHA256SUMS; refusing to sign')
print(f"manifest ok: {manifest['imageId']} ({manifest['arch']}), {len(listed)} artifacts")
PY

"$openssl" pkeyutl -sign -inkey "$signing_key" -rawin -in "$dir/MANIFEST.json" -out "$dir/MANIFEST.json.sig"
verify_key=$public_key
[[ -n $verify_key ]] || { verify_key=$dir/.verify-public.pem; "$openssl" pkey -in "$signing_key" -pubout -out "$verify_key"; }
"$openssl" pkeyutl -verify -pubin -inkey "$verify_key" -rawin -in "$dir/MANIFEST.json" -sigfile "$dir/MANIFEST.json.sig"
rm -f "$dir/.verify-public.pem"
ssh_key=${signing_key%.pem}.openssh
if [[ -f $ssh_key ]]; then
  ssh-keygen -Y sign -f "$ssh_key" -n decrypt-os-release < "$dir/MANIFEST.json" > "$dir/MANIFEST.json.sshsig"
  printf 'release@decryptlabs.io namespaces="decrypt-os-release" %s\n' "$(ssh-keygen -y -f "$ssh_key")" > "$dir/allowed_signers"
  ssh-keygen -Y verify -f "$dir/allowed_signers" -I release@decryptlabs.io -n decrypt-os-release \
    -s "$dir/MANIFEST.json.sshsig" < "$dir/MANIFEST.json"
fi
echo "signed: $dir/MANIFEST.json.sig"
