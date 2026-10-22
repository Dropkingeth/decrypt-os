# C2: media and package lock

The ARM media is a read-only ext4 filesystem, label `DECRYPT_MEDIA`. The stock live ISO is a separate hash-pinned boot input. The media contains no accounts, login state, logs, private keys, host configuration or internal evidence.

## Membership and trust

Only the following roots are allowed. Builders produce an exact regular-file inventory beneath each directory. A directory prefix alone is not permission to copy arbitrary files.

```
SHA256SUMS
MANIFEST.json
MANIFEST.sig
install
installer/**
verify/**
profiles/*.json
repo/aarch64/*.pkg.tar.*
repo/aarch64/*.pkg.tar.*.sig
repo/aarch64/decrypt-offline.db
repo/aarch64/decrypt-offline.files
repo/aarch64/package-lock.json
omarchy/omarchy-<commit>.tar
omarchy/source.json
app/**
```

`app/**` must match the separately reviewed signed transfer-tree inventory. Package suffixes must be recognized archive suffixes, not an unrestricted glob. Repo database aliases must be materialized regular files, not symlinks. No special files, hardlink aliases, bytecode, caches, dotfiles or unlisted files may enter the payload. The ext4 root may also contain the empty filesystem-generated `lost+found` directory.

`SHA256SUMS` is bytewise path-sorted, UTF-8, one line per regular file other than itself, with a final LF. Format is `<64 lowercase hex><two spaces><relative path>`. It covers `MANIFEST.json`, `MANIFEST.sig` and every payload file. Duplicate paths and escaped checksum rows are forbidden. Every regular file must appear exactly once; hash verification alone without membership verification is insufficient.

The manifest describes all payload files excluding the three metadata files `SHA256SUMS`, `MANIFEST.json` and `MANIFEST.sig`. This avoids a self-reference. Its Ed25519 detached signature covers the exact manifest bytes. The trusted release public key comes from the pinned verifier or a separately trusted host input, never solely from the media being checked. The typed SHA-256 of `SHA256SUMS` anchors the live bootstrap. The image SHA-256 is recorded externally after filesystem construction and cannot be embedded in its own manifest.

Manifest schema `decrypt.install-media.v1` has exactly these fields:

- `schema`, `version`, `arch`, `sourceDateEpoch`, `releaseKeyId`.
- `filesystem`: `{type, label, uuid, hashSeed, blockSize, inodeSize, features}`.
- `files`: path-sorted array of `{path, bytes, mode, sha256}` for every payload file. Mode is an octal string, either `0644` or `0755`.
- `profiles`: sorted array of `{id, path, sha256}`.
- `packageLock`: `{path, sha256}`.
- `omarchy`: `{commit, path, sha256}`; commit is a full 40-character hexadecimal object ID.
- `app`: `{manifestPath, manifestSha256}` for the existing signed transfer tree.
- `liveIso`: `{filename, sha256}`; filename only, never a host path.

## Reproducible ext4 build parameters

Use ext4, 4096-byte blocks, 256-byte inodes, label `DECRYPT_MEDIA`, filesystem UUID `6c5e8a44-28c0-4d2d-a6c8-9f5f49de0301`, directory hash seed `7957c165-fcb4-4484-925d-06c146d0d319`, and `SOURCE_DATE_EPOCH=1790467200`. Set `E2FSPROGS_FAKE_TIME` to the same epoch, disable lazy inode-table and journal initialization, set zero reserved-block percentage, and use an explicit fixed feature list. Initial feature list: `extent,filetype,sparse_super,large_file,huge_file,dir_nlink,extra_isize,metadata_csum,64bit,flex_bg`. Do not enable the journal.

Pin and record the builder's e2fsprogs version. Normalize payload ownership to root, modes to the inventory, timestamps to the epoch, directory traversal order and image size. Choose image bytes deterministically from allocated payload bytes with an explicitly recorded margin and MiB rounding. Mount with `ro,noload`. Independently extract/read every file, validate membership, signature, hashes and filesystem parameters. L1 requires two independent builds with identical `SHA256SUMS`; report image byte reproducibility separately and do not claim it from matching payload sums alone.

## Package lock

Schema `decrypt.package-lock.v1`:

- `schema`, `arch`, `resolvedAt` and `repositorySnapshots`.
- `roots`: `{base: [package names], desktop: [package names]}` preserving the reviewed root sets.
- `repositorySnapshots`: sorted array of `{name, url, databaseSha256}` for downloaded upstream repository metadata. URLs must be public and credential-free.
- `packages`: name-sorted array of `{name, version, arch, filename, bytes, sha256, signatureFilename, signatureSha256, signerFingerprint, primaryFingerprint, sourceUrl, depends, provides, conflicts, replaces}`. Dependencies and relationship fields are string arrays from the exact package metadata. Fingerprints are full uppercase hex OpenPGP fingerprints, not short IDs. Include every transitive dependency exactly once at its locked version.

Resolve the full base and desktop closure together before downloading. Verify each downloaded archive's upstream signature against the trusted Arch Linux ARM keyring and record the actual signer and primary-key fingerprints. Record all ABI conflicts without hiding failed resolution. Use `SigLevel = PackageRequired DatabaseNever` for the local repo, `file://` only, with no remote repository or downloader fallback. The signed media manifest and sums authenticate the local database. Missing signatures, invalid signatures, untrusted/expired/revoked keys, missing dependencies, or mismatched lock metadata refuse installation before target writes. Never use `--nodeps`, `--assume-installed`, an unsigned compatibility library, or a disabled signature check to force closure.

The package-set build retains the downloaded repo as an artifact even if the package set fails. A compatibility package signed with a project key or a changed lock date is a separate decision, not an automatic recovery.
