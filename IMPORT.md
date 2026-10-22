# Importing a future image release

This is a source-only package. No image or native hypervisor acceptance is included.
The image release must provide its own IMPORT.md, signed manifest, detached
signature, checksums and separately obtained release trust anchor.

1. Verify the signature over the exact canonical manifest using the independent
   Ed25519 trust document, then verify every artifact checksum and expected size.
   Use the key and release verification steps in `SECURITY.md` and the "Check a
   release" steps in `build/README.md`. Do not trust a public key merely because
   it accompanies the download.
2. Keep downloaded artifacts immutable. Import a separate writable customer copy,
   never a personal VM, a builder disk or a previously personalized acceptance disk.
3. For a UTM release, authenticate the entire tar and exact expected bundle members
   before safe extraction. Import the produced .utm bundle into a new VM. A tar
   structure check alone is not evidence that that UTM version boots it.
4. For Lume, convert the authenticated exported qcow2 to a new RAW disk, using a
   bounded qemu-img conversion at20MiB/s. Verify the RAW size and SHA256 against
   the signed sourceImage fields before its first boot. Register a new Linux VM,
   use the new RAW and fresh NVRAM, and do not reuse an existing machine identity.
   Use only flags supported by the installed Lume version; a successful Lume run
   does not qualify UTM, or vice versa.
5. On first boot, create your account locally and accept the notice through the
   actual desktop flow. Identity and instance authorization are generated for this
   machine only. No Decrypt Labs registration is needed. For strategy access, send
   your TradingView username from the app's Strategy access page.

Record actual hypervisor/version/architecture, imported artifact digest and native
first-start/application evidence separately. An empty validatedImports list means
no native import claim. Adding later acceptance to signed metadata requires a new
reviewed manifest/signature and explicit supersession, never silent replacement.
Do not redistribute your personalized disk.
