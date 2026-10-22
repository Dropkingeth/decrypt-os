# Security

## Report a vulnerability

Report it privately through GitHub: open this repository's **Security** tab and choose **Report a vulnerability**. Only the maintainers can read the report. Please do not open a public issue or pull request for a vulnerability.

Include the affected version, steps to reproduce, what you expected and the impact. Use synthetic data only. Never send tokens, passwords, account numbers, private keys or a personalized disk image. Keep exploit details private until we agree on disclosure.

## Supported versions

| Version | Supported |
| --- | --- |
| beta4 | Yes |
| Older betas | No. Install beta4. |

## Release signing key

Every release `MANIFEST.json` is signed with one Ed25519 key. Check the key against these values before you trust it. Do not trust a key only because it came with the download.

| | |
| --- | --- |
| OpenSSH fingerprint | `SHA256:fTPlBq3LRYwNA8swEKYocZx/culH9OS2SV+MIWr2QHc` |
| SHA-256 of `release-public.pem` | `bf1793ae3e8e5e4988fb505aff106e4e87efba6c211721446baf462567c9abbe` |
| Raw public key (base64) | `sYfkN8WNgNSaMMKiyMfParrSYtVdRCBI6rWcfNIWE0s=` (`decrypt-beta-20260924` in `candidate/app-source/installer/release-trust.json`) |

Check the key, then the release:

```sh
shasum -a 256 release-public.pem
awk '{print $3, $4}' allowed_signers | ssh-keygen -lf -
ssh-keygen -Y verify -f allowed_signers -I release@decryptlabs.io -n decrypt-os-release \
  -s MANIFEST.json.sshsig < MANIFEST.json
# or, with OpenSSL 3:
openssl pkeyutl -verify -pubin -inkey release-public.pem -rawin -in MANIFEST.json -sigfile MANIFEST.json.sig
shasum -a 256 -c SHA256SUMS
```

A checksum file next to an image proves only that the download is complete, not who published it. Import a separate writable copy and create your own account. A beta workspace is never approval to activate a live trading account.

## Not supported

Older pre-OS setups are not part of Decrypt OS and are not covered here. See `candidate/app-source/installer/INGRESS.md`.
