# Visual setup progress

Open the approved setup view beside the native installation terminal. This stage installs the runtime and app after the separate base-disk, desktop, reboot and local-login checks. It is not a bootable or whole-OS installer.

## Open guided setup

The installed **Decrypt Setup** shortcut is the preferred entry point. It checks the desktop handoff and asks for the read-only media location and separately reviewed checksum-list hash. It never restarts disk installation.

The equivalent post-desktop launcher contract is:

```sh
python3 -I -B /path/to/verified-media/setup/launch_setup.py \
  --media /path/to/verified-media \
  --manifest-sha256 REVIEWED_CHECKSUM_LIST_HASH \
  --work "$HOME/.local/state/decrypt-setup"
```

The default work directory matches the guided installer: `$HOME/.local/state/decrypt-setup`. Its parent must already exist, as required by the guide. Use an explicit `--work` consistently when selecting another state location.

Use `--in-terminal` when an existing native terminal should run the guide, including the installed handoff. Otherwise the launcher opens a native terminal through the installed `xdg-terminal-exec`, with Foot, kitty and Alacritty as explicit fallbacks. Chromium or Firefox opens the read-only progress page. The existing candidate desktop supplies Foot and Chromium. Chromium receives only the base recipe's existing `--no-first-run` and `--no-default-browser-check` flags, using the regular customer profile and unchanged credential/keyring policy. Python's standard library provides the monitor; it needs no server package or internet endpoint.

The launcher rejects root, unsupported hosts, a missing graphical session, changed media, missing visual files and mismatched bundled reader code before opening the guide. It checks exact media membership and file hashes against the separately reviewed checksum-list hash. It accepts neither shell commands through `BROWSER`/`TERMINAL` nor browser/terminal programs from a user-writable `PATH`.

The native terminal runs the existing guided `start` command. Builds stay under the ordinary customer account. The existing review and grouped administrator operation remain unchanged. Enter the local password only in sudo's native masked terminal prompt. The launcher never captures terminal input/output or writes a password log.

## Lifecycle

- The progress server binds only to `127.0.0.1`, on an available port. Its address appears in the terminal and opens in the browser.
- Closing or reopening the browser page has no effect on installation. Keep the installation terminal open.
- The monitor is a separate process. Stopping it does not signal, restart or mark the installer successful. An unavailable monitor means progress is unavailable, not that installation succeeded or failed.
- After the guided command finishes, the terminal retains its result and the monitor stays available for review. Press Return there to stop that monitor and close the setup command.
- The launcher cleans up only the monitor it created. It does not kill a browser process or an installer process group. Closing the terminal during setup is an interruption; inspect saved status and installed state before continuing. The existing guide handles interrupted/partial installation and refuses blind replay.
- There is no installed startup job, background service or external listener. An uncatchable process kill or host failure can interrupt cleanup; a native interruption/reboot trial is still required.

## Monitor only

To inspect existing saved progress without starting installation:

```sh
python3 -I -B /path/to/verified-media/setup/monitor.py \
  --work "$HOME/.local/state/decrypt-setup"
```

Open the printed loopback URL on that same computer. This command does not create the working directory. Stop this standalone monitor with Ctrl-C. The reader is bundled at `guided/guided_setup.py` and checked against an exact SHA-256 before loading.

The reader uses the guide's actual read-only state and lock logic. A stale `installing` record becomes `interrupted` when the operation lock is no longer held. Malformed and unknown records show status unavailable. An `installed` record means the guide recorded successful file checks. It does not prove native first launch, sign-in, a real response or recovery.

The HTTP response contains only an allowlisted status, activity flag and observation time. It omits paths, accounts, machine identity, logs and verification details. There is no write endpoint, cross-origin access, password form or command runner. Only named UI assets are served.

## Review the design

```sh
python3 -B monitor.py --preview
```

Open the loopback URL it prints. The preview banner labels sample states and allows their selection. Preview mode never reads or writes installation state.

## Qualification

Source, read-only HTTP, separate-process lifecycle and browser-fixture checks run on a developer workstation. They do not establish native ARM64 launch, a real administrator prompt, Linux terminal/browser handoff, installation, reboot or restore. Those remain fresh-guest gates.

## Assets

The logo and fonts retain the existing Decrypt app assets. Their provenance and font notices remain in `app-source/app/ui/BRAND-ASSETS.json` and `app-source/app/ui/licenses/` in the source candidate, and inside the packaged app archive. The approved visual style is unchanged.
