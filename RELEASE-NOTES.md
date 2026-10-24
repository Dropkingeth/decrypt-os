# Decrypt OS public beta: release notes

Version: **beta4** (release candidate `beta4-rc7`)

**This is beta software.** It is provided as is, without warranty. It is not financial advice. You trade with your own broker and TradingView accounts, and you are responsible for every order they place. Futures trading can lose money. Start with a Tradovate Simulation or demo account. The engines are not qualified for funded or live accounts in this beta.

Get started: [docs/GETTING-STARTED.md](https://github.com/Dropkingeth/decrypt-os/blob/HEAD/docs/GETTING-STARTED.md). Report a problem or send feedback: [GitHub Issues](https://github.com/Dropkingeth/decrypt-os/issues/new/choose).

## What Decrypt OS is

Decrypt OS is a free, open-source Linux desktop for running trading strategies and bots. It runs in a virtual machine (VM) on your own computer, so your trading setup stays separate from your everyday system. It is built on Arch Linux with the Omarchy (Hyprland) desktop. It is not an official Omarchy distribution.

beta4 is the first public beta. It comes in two builds:

- **arm64**, for Apple Silicon Macs with UTM, or other ARM64 hosts with QEMU. Tested in UTM.
- **x86_64**, for Windows, Intel or AMD PCs with VirtualBox, VMware or QEMU/KVM. Tested with QEMU/KVM.

Each release is signed. The download folder has checksums, a signed manifest and the public key. [GETTING-STARTED](https://github.com/Dropkingeth/decrypt-os/blob/HEAD/docs/GETTING-STARTED.md#1-check-the-download) shows how to check them on macOS, Linux and Windows.

## What's included

- **The Decrypt OS app** (workspace 1): setup, connections, strategy access, engines, health notifications, encrypted backups.
- **7 strategy engines.** Each is built to run one invite-only TradingView strategy through the Tradovate bridge. The strategy's alert sets the size and exits. **Only Soup can trade in this release.** The app can't install the other six yet.

  | Engine | Status | In this release |
  | --- | --- | --- |
  | ICT - Turtle Soup Shift | The reference engine | Install it from the app (**Execution runtime**) |
  | Determining Order Flow Engine [FVG+IFVG] | On hold | Can't trade yet |
  | Lumi RTH Gap | In development | Can't trade yet |
  | ICT - OTE Refined L | In development | Can't trade yet |
  | ICT - OTE Refined S | In development | Can't trade yet |
  | ICT - Silver Bullet OTE | Parked | Can't trade yet |
  | Order Blocks Decoded | Research build | Can't trade yet |

  The strategies themselves are not in Decrypt OS. Decrypt Labs grants them to your TradingView username. Send the request from the app's **Strategy access** page; access is added by hand. See [PRIVACY.md](https://github.com/Dropkingeth/decrypt-os/blob/HEAD/PRIVACY.md) for what the request stores.
- **The Tradovate bridge** (workspaces 3 and 5): drives your own Tradovate web session in its own browser window. It checks safety only: the right account and contract, a fresh account reading, a flat account before a new entry, a positive whole-number size, exits that never exceed the open position, and no opposite position in your other accounts. New entries start paused after every setup and restart. You resume them in Bridge Advanced.
- **The 1-second MES feed:** the bridge can read live MES prices every second from your Tradovate session. It is for Lumi RTH Gap, which can't trade in this release.
- **The ICT assistant** (workspace 4): a mentor for Inner Circle Trader concepts on your own Claude account (Claude subscription or Anthropic API key). It can explain, quiz and talk through a chart you describe. It cannot see your screen, place trades or read your accounts, and it does not give financial advice.
- **Guardian:** saves your TradingView sign-in, encrypted, and can recover an expired TradingView session with your permission.
- **Source code** under the GNU AGPL-3.0 at https://github.com/Dropkingeth/decrypt-os.

## Known issues and limits

**Some system changes stop the next boot**

After first start, Decrypt OS checks your login account and this computer's identity at every boot. In this release, if that check fails, the boot stops at a black screen that says "Setup stopped: …" followed by a reason, and you can't sign in. Normal restarts and power loss don't cause this. The next image fixes it.

Until then, don't make these changes after first start:

- set a root password
- add a second login account
- change your login shell
- add a sudo rule that needs no password
- delete or regenerate the SSH host keys in `/etc/ssh`, or change the machine ID

Take a VM snapshot after first start. If a boot stops at "Setup stopped":

- If you set a root password (the reason is `root-not-locked`): press Ctrl+Alt+F2, sign in as `root`, run `passwd -l root`, then run `reboot`.
- Otherwise, restore your snapshot. Without a snapshot, delete the VM and import it again. You'll have to set it up again, including your sign-ins.

**Installing**

- The `.vmdk` disk has not been boot-tested in VirtualBox or VMware. If it does not start, use the x86_64 `.qcow2` with QEMU/KVM and tell us.
- Intel Macs and physical installs (without a VM) have not been tested.
- Growing the system partition onto a disk larger than the default is not boot-tested yet. Keep the disk at its default size.

**Updating**

- There is no in-place update in this beta. A new version comes as a new image: you import it as a new VM and set it up again, including your sign-ins.

**Trading**

- Only Soup can trade. The other six engines can't be installed from the app yet and have not run with live alerts. Don't set up the bridge for them with the bridge's `setup.py --adapter` option: it prepares only the bridge, and that bridge then refuses Soup alerts.
- Lumi RTH Gap's 1-second MES feed has not been tested against a real broker connection yet.
- Lumi RTH Gap and Order Blocks Decoded have no public TradingView page yet, so you cannot request access to them.
- Follower accounts copy Soup trades only.
- Contracts do not roll over by themselves. Near a roll date, set up the engine again for the next dated contract. The trading calendar ends on December 31, 2026.
- From Friday 6 PM to Sunday 6 PM New York time, the bridge does not log in to Tradovate by itself. This is the weekend login hold.
- Captcha and two-factor prompts need you. Finish them yourself in the Tradovate window (workspace 3) or the TradingView window.
- If no Tradovate login is saved yet, the bridge may press Login on the empty form. Sign in yourself on workspace 3 and let the browser save the login.

**Testing status**

- In our automated bridge tests, 2 tests fail because they were written for an older build of the Soup engine. That build reported a close or reduction you make by hand differently. This is a test mismatch, not a known bridge fault, and it is still open.

**Desktop and security**

- The screensaver is turned off. If you start it by hand from the menu, it shows "command not found" and keys don't close it. Press Super+W to close it.
- The VM's disk is not encrypted. The Decrypt app keeps its own encrypted storage for assistant sign-ins and saved logins.

## Feedback

We want to hear what works and what doesn't. Open an issue at https://github.com/Dropkingeth/decrypt-os/issues/new/choose and pick bug report, feedback or strategy access. Issues are public: never post passwords, API keys, setup tokens, account numbers or your private alert address. Report security problems privately, as described in [SECURITY.md](https://github.com/Dropkingeth/decrypt-os/blob/HEAD/SECURITY.md).
