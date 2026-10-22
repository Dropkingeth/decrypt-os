# Import Decrypt OS (x86_64)

This download is for a Windows, Intel or AMD PC (64-bit). For an Apple Silicon
Mac, use the ARM64 download instead.

1. Check the download. Put the disk next to `SHA256SUMS`, `MANIFEST.json`,
   `MANIFEST.json.sshsig` and `allowed_signers`, then run in that folder.

   On Linux:

       ssh-keygen -Y verify -f allowed_signers -I release@decryptlabs.io \
         -n decrypt-os-release -s MANIFEST.json.sshsig < MANIFEST.json
       while read -r hash name; do grep -q "$hash" MANIFEST.json \
         && echo "listed: $name" || echo "NOT LISTED: $name"; done < SHA256SUMS
       sha256sum -c SHA256SUMS

   - The first command must print a line that starts with
     `Good "decrypt-os-release" signature for release@decryptlabs.io`.
   - The second must print `listed` for every file. The signature covers
     `MANIFEST.json`, and this shows `SHA256SUMS` matches it.
   - The third must print `OK` for each file you downloaded. A file you did
     not download shows `FAILED open or read`; that is fine.

   On Windows, use Command Prompt (`cmd.exe`), not PowerShell, because the
   signature check reads the file with `<`. `ssh-keygen` comes with the
   Windows OpenSSH Client.

       ssh-keygen -Y verify -f allowed_signers -I release@decryptlabs.io -n decrypt-os-release -s MANIFEST.json.sshsig < MANIFEST.json
       certutil -hashfile FILE SHA256

   Replace `FILE` with the disk's file name. Compare the `certutil` hash with
   the line for that file in `SHA256SUMS` and in `MANIFEST.json`. All three
   must match.

   If a check fails, do not use the files. With OpenSSL 3 you can also check the
   Ed25519 signature with `release-public.pem`:

       openssl pkeyutl -verify -pubin -inkey release-public.pem -rawin \
         -in MANIFEST.json -sigfile MANIFEST.json.sig

2. Import one of the two disks. Every option needs **UEFI firmware** and
   **Secure Boot off**. Give the VM at least 4 CPU cores and 8 GB of memory,
   and keep about 45 GB of disk space free.
   - **VirtualBox 7 or VMware Workstation 17:** create a 64-bit Linux VM
     (Arch Linux / Other Linux 6.x), turn on EFI, and attach the `.vmdk` as
     the existing hard disk. In VirtualBox use the VMSVGA graphics controller
     with 3D acceleration on. The `.vmdk` has not been tested in VirtualBox or
     VMware yet. If it does not start, use the `.qcow2` with QEMU/KVM and tell us.
   - **QEMU/KVM (virt-manager, Proxmox, GNOME Boxes):** use the `.qcow2` disk
     with a `q35` machine, OVMF (UEFI) firmware, a VirtIO disk, VirtIO GPU and
     networking. This is the tested option.

3. Start the VM. On first start you create your own account and password. The
   machine gets its own identity, SSH host keys and package keyring at that point.
   Nothing is logged in and no trading or broker connection is active until you
   set it up in the Decrypt app.

Keep the downloaded files unchanged so you can start over from a clean copy.
