# Import Decrypt OS (ARM64)

This download is for an Apple Silicon Mac with UTM, or another ARM64 host with
QEMU. For a Windows, Intel or AMD PC, use the x86_64 download instead.

1. Check the download. Put the image next to `SHA256SUMS`, `MANIFEST.json`,
   `MANIFEST.json.sshsig` and `allowed_signers`, then run in that folder:

       ssh-keygen -Y verify -f allowed_signers -I release@decryptlabs.io \
         -n decrypt-os-release -s MANIFEST.json.sshsig < MANIFEST.json
       while read -r hash name; do grep -q "$hash" MANIFEST.json \
         && echo "listed: $name" || echo "NOT LISTED: $name"; done < SHA256SUMS
       shasum -a 256 -c SHA256SUMS

   - The first command must print a line that starts with
     `Good "decrypt-os-release" signature for release@decryptlabs.io`.
   - The second must print `listed` for every file. The signature covers
     `MANIFEST.json`, and this shows `SHA256SUMS` matches it.
   - The third must print `OK` for each file you downloaded. A file you did
     not download shows `FAILED open or read`; that is fine.

   If a check fails, do not use the files. On Linux with OpenSSL 3 you can also
   check the Ed25519 signature with `release-public.pem`:

       openssl pkeyutl -verify -pubin -inkey release-public.pem -rawin \
         -in MANIFEST.json -sigfile MANIFEST.json.sig

2. Import one of the two files. Give the VM at least 4 CPU cores and 8 GB of
   memory, and keep about 45 GB of disk space free.
   - **UTM on an Apple Silicon Mac:** extract the `.utm.tar` file and open
     `Decrypt-OS.utm` in UTM.
   - **QEMU on another ARM64 host:** use the `.qcow2` disk with an `aarch64`
     `virt` machine, UEFI firmware, a VirtIO disk, VirtIO GPU and networking.

3. Start the VM. On first start you create your own account and password. The
   machine gets its own identity, SSH host keys and package keyring at that point.
   Nothing is logged in and no trading or broker connection is active until you
   set it up in the Decrypt app.

Keep the downloaded files unchanged so you can start over from a clean copy.
