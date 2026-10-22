# Clean customer-base recipe

Built on Omarchy. Assistant powered by OpenClaw.

This is a pre-release ARM64 customer-test candidate, not a bootable image or release installer. It installs the local application into an existing, fresh Arch/Omarchy compatibility guest. It does not install Omarchy, create a VM, download dependencies, import accounts, enable trading, or start services.

This beta source carries reviewed browser-ownership corrections. Source regressions do not qualify this combined package: a fresh native install, reboot and credential-bearing restore remain unverified. Existing development and customer installations are rejected.

## Prepare the candidate

Use the allowlisted source archive and retain its `SOURCE-MANIFEST.json`. Verify the archive and manifest hashes against the reviewed build receipt. The manifest detects changed files; it is not a release signature.

On an isolated build account, extract the source archive and build its UI as a non-root user:

```sh
cd decrypt-os-source/app/ui
decrypt_node=/usr/local/lib/decrypt-os-runtime/node-v24.19.0-linux-arm64
decrypt_npm_config=$(mktemp -d)
touch "$decrypt_npm_config/user.npmrc" "$decrypt_npm_config/global.npmrc"
env -i HOME="$HOME" PATH="$decrypt_node/bin:/usr/bin:/bin" \
  "$decrypt_node/bin/npm" --userconfig="$decrypt_npm_config/user.npmrc" \
  --globalconfig="$decrypt_npm_config/global.npmrc" ci --ignore-scripts --no-audit --no-fund && \
env -i HOME="$HOME" PATH="$decrypt_node/bin:/usr/bin:/bin" \
  "$decrypt_node/bin/npm" --userconfig="$decrypt_npm_config/user.npmrc" \
  --globalconfig="$decrypt_npm_config/global.npmrc" run build
```

Run these commands after the pinned runtime is installed. Stop if dependency installation fails. npm needs two different configuration files; using the same path for both causes a configuration conflict. These empty files exclude inherited registry credentials and user settings.

The locked install skips package lifecycle scripts. The build executes the reviewed local build script and pinned build tools. Do not build as root. Keep the generated `app/ui/dist` beside the verified source. Runtime accounts, API vaults and history must never be added to the archive or build output.

From the extracted `decrypt-os-source` directory (not `app/ui`), render a reviewable filesystem tree without installing it:

```sh
python3 installer/clean_base.py render --bundle . --output /absolute/new/staging-dir --user customer
```

Replace `customer` with the fresh guest's desktop account. The output directory must not exist. The command verifies every source record, checks the UI output, replaces development-only runtime paths, and writes `rootfs/` plus `RECIPE-RECEIPT.json`. It does not read credentials or change services.

## Target dependencies

The target is an ARM64 Arch Linux guest with Omarchy installed for its own desktop account. This compatibility target does not establish standard x86 PC support.

Before installation, provision these complete public dependencies through a separately reviewed, pinned dependency procedure:

| Location | Required content |
| --- | --- |
| `/usr/local/lib/decrypt-os-runtime/openclaw` | Complete OpenClaw 2026.9.9 package, dependencies and license notices |
| `/usr/local/lib/decrypt-os-runtime/node-v24.19.0-linux-arm64` | Node 24.19.0 ARM64 distribution with its notices |
| `/usr/local/lib/decrypt-os-runtime/python` | Python virtual environment made with `--copies`, containing cryptography 46.0.5, cffi 2.1.1 and pycparser 3.0 |

Dependencies must be root-owned and not writable by group or other users. Package symlinks must be relative and stay inside that package. Do not copy the operator's installed runtime, home, `.openclaw`, browser profile or package cache. The recipe checks installed versions and ownership; artifact authenticity and complete license/security review remain separate requirements.

System utilities include systemd, sudo, Python 3, util-linux, cryptsetup, e2fsprogs, Foot, Hyprland, Chromium and xdg-utils. Temporary credentials require swap to be disabled. Application ports 8810, 8811 and 19789 must be free; the dedicated market browser also requires its loopback port 9567 before opening. The target account must have no existing assistant/development state. Existing application files or encrypted customer storage are never overwritten.

## Check and install on the fresh guest

Run the read-only check before applying anything:

```sh
python3 installer/clean_base.py preflight --bundle . --user customer
```

Read its result. A rejected host is not eligible for installation. The current development guest is rejected even if all dependencies are present.

Record the selected fresh guest's machine identity hash and the reviewed source-manifest hash. These are identifiers, not credentials:

```sh
python3 -c 'import hashlib,pathlib; print(hashlib.sha256(pathlib.Path("/etc/machine-id").read_text().strip().encode()).hexdigest())'
sha256sum SOURCE-MANIFEST.json
sha256sum /absolute/new/staging-dir/RECIPE-RECEIPT.json
```

Use the complete verified transfer media's guide to install. It prepares the runtime, binds the rendered app to the exact source and target, and collects the versioned local acknowledgement before the native administrator prompt:

```sh
python3 /path/to/verified-media/guided/guided_setup.py start \
  --media /path/to/verified-media \
  --manifest-sha256 REVIEWED_CHECKSUM_LIST_HASH \
  --work "$HOME/.local/state/decrypt-setup"
```

The guide invokes `clean_base.py apply` with its required `--acknowledgement` record. Do not create that record by hand or bypass the guide. Apply repeats all checks and re-renders the candidate before installing. The recipe hash binds the generated UI and transformed code as well as the source. A changed UI build is rejected. It uses exclusive file creation, adds a machine-bound customer-test marker, installs exact fixed sudo permissions, and creates an empty private application-state directory. It removes files created by the attempt if a handled error interrupts installation. Sudden power loss can leave a partial install; automatic replay after power loss is not supported. A partial or existing installation needs read-only verification and review, not a force option.

The installer does not start or enable services. From the customer desktop, reload user units and open **Decrypt OS** in the application launcher:

```sh
systemctl --user daemon-reload
```

The launcher imports the current desktop environment, selects workspace 5 using the pinned Lua compositor command, verifies that workspace, starts the local application services, and hands `http://127.0.0.1:8811` to the default browser. A long-running browser opener is no longer treated as a timeout failure. The handoff is not proof that the page is visible. Check the actual window, including when a browser is already running elsewhere.

The paired desktop recipe must include Chromium's `--no-first-run` and `--no-default-browser-check` flags. The app installer does not rewrite browser preferences or disable the browser sandbox. Native sign-in and storage terminals target workspace 5 and verify that workspace before opening.

Follow [Get started](../customer-docs/GETTING-STARTED.md) and [the test checklist](../customer-docs/TEST-CHECKLIST.md). A completed install is not a verified AI connection. Login, conversation, encrypted persistence and cold-boot recovery each need evidence from the new guest.

## Installed boundaries

- Application and customer context live under `/usr/local/lib/decrypt-os` and remain root-owned.
- The desktop API stores new customer state under `~/.local/state/decrypt-os` with mode 0700. The existing API-key encryption flow is unchanged.
- System-wide user units are `decrypt-os-api.service`, `decrypt-os-ui.service` and `decrypt-os-market-browser.service` under `/etc/systemd/user/`. They are not enabled automatically. The market browser (used for TradingView charting and Guardian's TradingView sign-in recovery) starts only through its explicit connection action.
- The root wrapper permits only its fixed customer actions, checks machine enrollment, drops inherited environment, and loads root-owned runtime code. The API retains `NoNewPrivileges` and dispatches fixed native operations through the desktop user-service manager.
- The customer runtime uses an isolated identity with home/control-socket restrictions. It has no model-executed maintenance or trading tools. Encrypted sign-in is optional and uses the existing masked terminal flow.

The current customer base has no update mechanism, signed release trust chain, encrypted backup/restore, broker commissioning or automatic repairs. Do not describe render tests or a successful file copy as a qualified OS installation.
