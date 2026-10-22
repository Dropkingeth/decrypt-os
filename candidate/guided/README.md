# Guided Decrypt setup

Install the runtime and app after the fresh guest has passed desktop and local-login checks. This terminal guide combines the reviewed installation steps and normally asks for the local administrator password once.

**This is a pre-release ARM64 test candidate.** Local tests pass; this guided sequence has not run on a new VM. It does not create a VM, partition a disk, install the base desktop, connect accounts, start services or establish backup/reboot recovery. Use the separate base guide for the earlier stages. Do not run it on a machine you use for other work or on an already-installed test VM.

## Start

For the matching visual progress page, open the installed **Decrypt Setup** shortcut after the separate desktop and reboot checks. Its verified-media handoff uses `setup/launch_setup.py --in-terminal`. The terminal guide below remains available to operators. The browser only reads progress; it never starts installation or receives a password.

Use the normal desktop account in a fresh Arch/Omarchy ARM64 guest. Keep the verified media mounted read-only. Obtain its checksum-list hash from the separate package receipt, not from the media alone.

Run this command from the guest terminal, replacing the media path and hash with the verified values:

```sh
python3 /path/to/verified-media/guided/guided_setup.py start \
  --media /path/to/verified-media \
  --manifest-sha256 REVIEWED_CHECKSUM_LIST_HASH \
  --work "$HOME/decrypt-setup"
```

The script on the media must itself match that media's reviewed manifest. The working directory is created privately if absent; an existing directory must be owned by the account and have mode 0700. The guide rejects root builds, unsupported hosts, the protected development machine and existing installations. Public npm/PyPI access is required by the pinned runtime recipe.

The six steps are:

1. Verify every media file and the desktop prerequisites.
2. Build the pinned runtime in a new attempt directory as the normal user.
3. Build the interface with the staged Node runtime and empty npm configuration files.
4. Show the target, changed files and rendered installation tree for review.
5. Read the versioned notice, type `I AGREE`, then `INSTALL`. One native sudo request installs the runtime, verifies it, checks app dependencies and installs Decrypt.
6. Verify installed hashes, real file modes, root ownership, machine enrollment, the matching prepared/root/installed acknowledgement and the empty customer-state directory.

Enter the existing local password only in the masked sudo prompt. The guide does not read, save or forward it. The guide adds no installation sudo bypass or background sudo renewal, and never builds packages as root. The app's existing narrowly scoped customer-operation policy is installed unchanged by the reviewed recipe.

If `start` finds a prepared build, it rechecks that build and shows its review instead of rebuilding. The ordinary setup flow does not require copying plan hashes between commands.

## After installation

A successful result means **runtime and app files were verified**, not that first launch or an AI connection passed. Services remain stopped. Follow the operator guide's native launcher, workspace-5, empty-state and responsiveness checks. The software-rendered guest's log-containment requirement remains separate; this tool does not silently install a guest-specific workaround on another identity.

Keep the verified, unconnected baseline before provider sign-in. Sign-in, a real response, conversations, encrypted storage, reboot recovery and a separately booted restore each need their own evidence. The planned heavy app is not included.

## Status and recovery

Running the program without an action, or with `status`, reads its saved state without writing files:

```sh
python3 /path/to/verified-media/guided/guided_setup.py status --work "$HOME/decrypt-setup"
```

- **Build failed or interrupted:** Keep the attempt and logs. Run `start` again; it creates a new attempt and does not reuse partially built packages.
- **Cancelled at the local `INSTALL` confirmation:** Nothing was installed. The verified build remains ready.
- **Sudo exited before installation began:** The guide only reopens the path to installation if no root journal exists and the original fresh-target checks still pass. Run `start` again when ready.
- **Privileged work failed, was interrupted or has an unknown outcome:** The attempt is blocked from replay. Keep the build, source, state and root journal. Do not delete partial files, reset the state or repeat the base-disk installer. Use explicit verification, then review the actual incomplete stage.

The operator can run read-only installed-file verification with the plan hash shown in `STATE.json`:

```sh
python3 /path/to/verified-media/guided/guided_setup.py verify \
  --work "$HOME/decrypt-setup" --plan-sha256 REVIEWED_PLAN_HASH
```

This may need a local sudo password because the installed sudoers file must remain root-readable only. Verification changes no install files or saved state. A complete readback can prove that an interrupted installation left the entire reviewed file set installed. It cannot prove native launch, a clean reboot or onboarding. If any file, ownership, mode, receipt, enrollment or empty-state check fails, the tool stops for recovery review. An installed runtime with an incomplete app is **not** automatically replayed.

## Operator commands and evidence

`prepare` builds and renders without opening sudo. `install --plan-sha256 …` applies an already reviewed plan. These actions use the same checks as `start`; they are not bypass options.

The private work directory retains each unique attempt, build logs, `DEPENDENCY-BUILD-RECEIPT.json`, `review/RECIPE-RECEIPT.json`, rendered `review/rootfs`, `PLAN.json` and `STATE.json`. The approved plan binds machine/account identity, verified input records, guided/helper code, source manifest, runtime artifact inventory and rendered recipe. It contains no credentials. The root runner rechecks the plan and source, re-renders the recipe, then uses the unchanged reviewed runtime/app apply functions. Each privileged boundary is recorded in root-owned `/var/lib/decrypt-os-guided-setup/<plan-hash>/RESULT.json` before proceeding. An existing journal prevents a second installation attempt.

Hash verification establishes consistency with the separately reviewed input receipt; it is not an upstream signature, a complete dependency audit or a supported-hardware qualification.

## Local verification evidence

The source repository carries the synthetic tests (`test_guided_setup.py`). They use temporary local fixtures and mocked privileged operations; they do not invoke sudo, install packages, access a VM or alter a disk. The installation media excludes those test and evidence files.

Local source checks do not establish a native installation. The new guided sequence still needs its own fresh-guest trial.
