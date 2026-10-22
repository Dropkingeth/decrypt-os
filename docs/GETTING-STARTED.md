# Get started with the Decrypt OS public beta

Decrypt OS is a Linux trading desktop that runs in a virtual machine (VM) on your computer. It brings the Decrypt app, 7 strategy engines, the Tradovate bridge and the ICT assistant together in one place.

**This is beta software.** It is provided as is, without warranty. It is not financial advice. You trade with your own broker and TradingView accounts, and you are responsible for every order they place. Futures trading can lose money. Start with a Tradovate Simulation or demo account. The engines are not qualified for funded or live accounts in this beta.

- Release notes and known issues: [RELEASE-NOTES.md](https://github.com/Dropkingeth/decrypt-os/blob/HEAD/RELEASE-NOTES.md)
- Privacy note for strategy access: [PRIVACY.md](https://github.com/Dropkingeth/decrypt-os/blob/HEAD/PRIVACY.md)
- Report a problem or send feedback: [GitHub Issues](https://github.com/Dropkingeth/decrypt-os/issues/new/choose)

## What you need

- **A computer that can run a VM.**
  - Apple Silicon Mac (M1 or later) with [UTM](https://mac.getutm.app).
  - Windows, Intel or AMD PC (64-bit) with VirtualBox 7, VMware Workstation 17, or QEMU/KVM on Linux. Turn on hardware virtualization (Intel VT-x or AMD-V) in the PC's firmware settings if it is off.
- **For the VM:** 4 CPU cores, 8 GB of memory and about 45 GB of free disk space. The computer itself needs more memory than the 8 GB the VM uses.
- **An internet connection** for the download and for the services you connect.
- **Your own accounts, used only when you choose:**
  - a Claude subscription (Pro or Max) or an Anthropic API key, for the assistants;
  - a TradingView account, for charts, strategies and alerts;
  - a Tradovate account, for trading. Use Simulation or demo.

Intel Macs and physical installs (without a VM) have not been tested.

## Download

Pick the folder for your computer. Download the disk file you need and the check files next to it.

| Your computer | Folder | Disk file | Size |
| --- | --- | --- | --- |
| Apple Silicon Mac, UTM | [arm64](https://downloads.decryptlabs.io/decrypt-os/beta4-rc7/arm64/) | `decrypt-os-beta4-rc7-arm64.utm.tar` | `~3.1 GB` |
| ARM64 host, QEMU | [arm64](https://downloads.decryptlabs.io/decrypt-os/beta4-rc7/arm64/) | `decrypt-os-beta4-rc7-arm64.qcow2` | `~3.1 GB` |
| PC, VirtualBox or VMware | [x86_64](https://downloads.decryptlabs.io/decrypt-os/beta4-rc7/x86_64/) | `decrypt-os-beta4-rc7-x86_64.vmdk` | `~7.0 GB` |
| PC, QEMU/KVM on Linux | [x86_64](https://downloads.decryptlabs.io/decrypt-os/beta4-rc7/x86_64/) | `decrypt-os-beta4-rc7-x86_64.qcow2` | `~3.4 GB` |

Sizes are approximate and rounded; see `MANIFEST.json` in each folder for the exact byte count.

Every folder also has these files:

| File | What it is |
| --- | --- |
| `SHA256SUMS` | SHA-256 checksum of each file |
| `MANIFEST.json` | The release record, with every file's checksum and size |
| `MANIFEST.json.sshsig`, `allowed_signers` | The release signature, for `ssh-keygen` |
| `MANIFEST.json.sig`, `release-public.pem` | The same signature, for OpenSSL |
| `IMPORT.md` | Short import steps for that architecture |
| `GETTING-STARTED.md`, `RELEASE-NOTES.md` | This guide and the release notes |

The `.vmdk` has not been boot-tested in VirtualBox or VMware yet. The x86_64 `.qcow2` was tested with QEMU/KVM, and the ARM64 image was tested in UTM. If the `.vmdk` does not start, use the `.qcow2` with QEMU/KVM and [tell us](https://github.com/Dropkingeth/decrypt-os/issues/new/choose).

The source code is on GitHub: https://github.com/Dropkingeth/decrypt-os (AGPL-3.0).

## 1. Check the download

Put the disk file, `SHA256SUMS`, `MANIFEST.json`, `MANIFEST.json.sshsig` and `allowed_signers` in one folder. If any check below fails, do not use the files. Download them again, and tell us if it still fails.

### Check the signing key

Do this once. Compare the output with the values in [SECURITY.md](https://github.com/Dropkingeth/decrypt-os/blob/HEAD/SECURITY.md). Do not trust a key only because it came with the download.

```sh
shasum -a 256 release-public.pem
awk '{print $3, $4}' allowed_signers | ssh-keygen -lf -
```

On Linux, use `sha256sum release-public.pem` for the first line. The key's OpenSSH fingerprint is `SHA256:fTPlBq3LRYwNA8swEKYocZx/culH9OS2SV+MIWr2QHc`.

### macOS and Linux

Open a terminal in the folder and run:

```sh
ssh-keygen -Y verify -f allowed_signers -I release@decryptlabs.io -n decrypt-os-release \
  -s MANIFEST.json.sshsig < MANIFEST.json
while read -r hash name; do grep -q "$hash" MANIFEST.json \
  && echo "listed: $name" || echo "NOT LISTED: $name"; done < SHA256SUMS
shasum -a 256 -c SHA256SUMS      # macOS
sha256sum -c SHA256SUMS          # Linux
```

- The first command must print a line that starts with `Good "decrypt-os-release" signature for release@decryptlabs.io`.
- The second must print `listed` for every file. The signature covers `MANIFEST.json`, and this shows `SHA256SUMS` matches it.
- The last must print `OK` for each file you downloaded. A file you did not download shows `FAILED open or read`. That is fine.

With OpenSSL 3 you can also check the same signature with `release-public.pem`:

```sh
openssl pkeyutl -verify -pubin -inkey release-public.pem -rawin -in MANIFEST.json -sigfile MANIFEST.json.sig
```

It must print `Signature Verified Successfully`.

### Windows

`ssh-keygen` comes with the Windows OpenSSH Client. Run the signature check in **Command Prompt** (`cmd.exe`), not PowerShell, because it reads the file with `<`:

```bat
ssh-keygen -Y verify -f allowed_signers -I release@decryptlabs.io -n decrypt-os-release -s MANIFEST.json.sshsig < MANIFEST.json
```

It must print `Good "decrypt-os-release" signature for release@decryptlabs.io`.

Then check the disk's checksum in **PowerShell**. Replace the file name if you downloaded a different disk:

```powershell
$file = 'decrypt-os-beta4-rc7-x86_64.vmdk'
$actual = (Get-FileHash -Algorithm SHA256 $file).Hash.ToLower()
$expected = (Select-String -Path SHA256SUMS -Pattern $file -SimpleMatch).Line.Split(' ')[0]
if ($actual -eq $expected) { 'OK: matches SHA256SUMS' } else { 'MISMATCH: do not use this file' }
if (Select-String -Path MANIFEST.json -Pattern $actual -SimpleMatch -Quiet) { 'OK: listed in MANIFEST.json' } else { 'NOT LISTED: do not use this file' }
```

Both lines must say `OK`.

## 2. Import and start the VM

Keep the downloaded files unchanged. Import a copy, so you can start over from a clean download at any time.

### Apple Silicon Mac: UTM

1. Install UTM from https://mac.getutm.app or the Mac App Store.
2. Double-click `decrypt-os-beta4-rc7-arm64.utm.tar` in Finder, or run `tar -xf decrypt-os-beta4-rc7-arm64.utm.tar`. You get `Decrypt-OS.utm`.
3. Double-click `Decrypt-OS.utm`, or choose **File → Open** in UTM and select it.
4. The VM is already set up: 4 CPU cores, 8 GB of memory, UEFI boot, VirtIO disk, display and network. You can check this under the VM's **Edit → System**.
5. Press **Play**.

### ARM64 host: QEMU

Copy the `.qcow2` and use the copy with an `aarch64` `virt` machine, UEFI firmware (AAVMF), 4 CPU cores, 8 GB of memory, a VirtIO disk, VirtIO GPU and VirtIO networking.

### Windows, Intel or AMD PC: VirtualBox 7

Not boot-tested yet. Every option needs **UEFI (EFI) firmware** and **Secure Boot off**.

1. Copy `decrypt-os-beta4-rc7-x86_64.vmdk` to the folder where you keep VMs. VirtualBox writes to the disk you attach, so attach the copy.
2. **Machine → New.** Name: `Decrypt OS`. Type: **Linux**. Version: **Arch Linux (64-bit)**. Do not choose an ISO.
3. **Hardware:** Base Memory **8192 MB**, Processors **4**, and tick **Enable EFI**.
4. **Hard Disk:** choose **Use an Existing Virtual Hard Disk File**, press the folder icon, **Add**, and select your copied `.vmdk`.
5. **Finish.** Then open **Settings**:
   - **System → Motherboard:** EFI on. If you see **Enable Secure Boot**, turn it off.
   - **Display → Screen:** Graphics Controller **VMSVGA**, Video Memory **128 MB**, tick **Enable 3D Acceleration**.
   - **Network → Adapter 1:** **NAT** (the default).
6. Press **Start**.

### Windows, Intel or AMD PC: VMware Workstation 17

Not boot-tested yet.

1. Copy `decrypt-os-beta4-rc7-x86_64.vmdk` to the folder where you keep VMs.
2. **File → New Virtual Machine → Custom (advanced)**, hardware compatibility **Workstation 17.x**.
3. Choose **I will install the operating system later**. Guest: **Linux**, version **Other Linux 6.x kernel 64-bit**.
4. Name it `Decrypt OS`. Processors: 1 processor with **4 cores**. Memory: **8192 MB**. Network: **NAT**.
5. Disk type: **SATA**. Choose **Use an existing virtual disk** and select your copied `.vmdk`. If VMware offers to convert the disk, choose **Keep Existing Format**.
6. Before the first start, open **Edit virtual machine settings → Options → Advanced**. Set Firmware type to **UEFI** and leave **Enable secure boot** off. Under **Hardware → Display**, tick **Accelerate 3D graphics**.
7. Press **Power on**.

### Linux PC: QEMU/KVM

This is the tested x86_64 option.

**With virt-manager:**

1. Copy `decrypt-os-beta4-rc7-x86_64.qcow2` to your images folder (for example `/var/lib/libvirt/images/`).
2. **File → New Virtual Machine → Import existing disk image.** Select the copy. Operating system: **Arch Linux** (or **Generic Linux**).
3. Memory **8192 MB**, CPUs **4**.
4. Name it `Decrypt OS` and tick **Customize configuration before install**.
5. In **Overview**: Chipset **Q35**, Firmware **UEFI x86_64** with an OVMF file that is not a Secure Boot (`secboot`) variant. Check that the disk bus is **VirtIO**, the video model **Virtio** and the network device **virtio**.
6. **Apply**, then **Begin Installation**. Nothing is installed; the VM starts from the disk.

**With plain QEMU:** firmware file names differ by distribution. These are the Debian and Ubuntu names from the `ovmf` package.

```sh
cp decrypt-os-beta4-rc7-x86_64.qcow2 decrypt-os.qcow2
cp /usr/share/OVMF/OVMF_VARS_4M.fd decrypt-os-vars.fd
qemu-system-x86_64 -machine q35,accel=kvm -cpu host -smp 4 -m 8192 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd \
  -drive if=pflash,format=raw,file=decrypt-os-vars.fd \
  -drive file=decrypt-os.qcow2,if=virtio \
  -device virtio-vga -nic user,model=virtio-net-pci
```

Proxmox and GNOME Boxes work the same way: q35 machine, OVMF (UEFI) without Secure Boot, VirtIO disk, display and network.

## 3. First start (about 2 minutes)

1. Choose a user name and a password. A typing mistake just asks again.
2. Pick your time zone from the list.
3. Sign in on the Decrypt OS screen.
4. Read the notice and accept it. A small password window opens. Type your password there.

The VM gets its own identity, SSH host keys and package keyring at this point. Nothing is signed in, and no trading or broker connection is active until you set it up. Decrypt OS needs no registration.

## 4. Your desktop

The bar at the top shows workspaces 1 to 5. Click a number, or press Super+1 to Super+5. Super is the Windows key; on a Mac it is usually Command. If a shortcut doesn't work, click the number in the bar.

| Workspace | What's there |
| --- | --- |
| 1 | Decrypt OS app: setup, status, connections, strategy access, engines, system care |
| 2 | Trading: your engine and your TradingView chart |
| 3 | Tradovate bridge: the Tradovate login window and bridge status |
| 4 | ICT assistant: a mentor for Inner Circle Trader concepts, on your own Claude account |
| 5 | Bridge Advanced: pause, resume, flatten, add logins |

Super+Enter opens a terminal.

## 5. Set it up, in order

The app's Home page shows your next step. The full order:

1. **Setup:** name your assistant and choose whether this computer trades.
2. **Connections → Claude:** press **Start and connect**. A terminal opens on workspace 4. Choose 1 for a Claude setup token (run `claude setup-token` on any computer with Claude Code) or 2 for an Anthropic API key, and paste it there. Never paste it into a chat.
3. **Strategy access:** the strategies are invite-only TradingView scripts. Enter your TradingView username and press send. The app sends it to access.decryptlabs.io. Decrypt Labs adds access to that username **by hand**, so allow some time. Then look in TradingView under **Indicators → Invite-only scripts**. See [PRIVACY.md](https://github.com/Dropkingeth/decrypt-os/blob/HEAD/PRIVACY.md) for what the request stores.
4. **Tradovate bridge:** for Soup, fill in the form in **Connections → Tradovate bridge**, then turn the bridge on. For any other engine, use the setup command under "Engines" below instead of the form. Then sign in to Tradovate on workspace 3 and choose **Access Simulation**. Let the browser save the login. Decrypt OS never sees your Tradovate password.
5. **Engine:** for Soup, install the Soup engine in **Execution runtime** for the same account. For any other engine, the bridge setup in step 4 is the engine setup.
6. **TradingView alert:** add the strategy to your chart and create the alert. The app shows the alert settings for each engine. Keep your private alert address out of messages, logs and screenshots.
7. **Bridge Advanced (workspace 5):** open it from the app launcher. Resume entries only when you are ready. Entries stay paused after every setup and every restart.

Nothing trades until you finish step 7 and your strategy alert is set up in TradingView.

## Engines

Decrypt OS ships 7 engines. Each one runs one TradingView strategy through the Tradovate bridge. The strategy's alert sets the size and the exits. The bridge sends exactly that and adds safety checks only.

| Engine | Contract and chart | Status | How to set it up |
| --- | --- | --- | --- |
| ICT - Turtle Soup Shift | MNQ, dated contract | The reference engine | From the app (**Execution runtime**) |
| Determining Order Flow Engine [FVG+IFVG] | MNQ, 1-minute | On hold, off by default | Setup command, adapter `fvg-r5b-bridge-v2` |
| Lumi RTH Gap | MES, 2-minute | In development; needs the 1-second MES feed | Setup command, adapter `lumi-gap-bridge-v2` |
| ICT - OTE Refined L | MNQ, dated contract | In development | Setup command, adapter `ote-refined-large-bridge-v2` |
| ICT - OTE Refined S | MNQ, dated contract | In development | Setup command, adapter `ote-refined-small-bridge-v2` |
| ICT - Silver Bullet OTE | MNQ, 30-second | Parked; paper or demo only | Setup command, adapter `ote-active-bridge-v2` |
| Order Blocks Decoded | MNQ, 1-minute | Research build; paper only | Setup command, adapter `order-blocks-bridge-v2` |

Always chart the dated contract (for example `MNQZ2026`), not a continuous one such as `MNQ1!`.

The six engines other than Soup read the `decrypt-bridge-v2` alert format and are set up with the bridge's setup command. Run it in a terminal on a VM where the bridge is not set up yet. It never changes an existing bridge setup.

```sh
M=/usr/local/lib/decrypt-os/modules/tradovate-bridge
python3 $M/setup.py init --account-name 'ACCOUNT NAME' --soup-account-id 'ACCOUNT NUMBER' --ticker MNQZ2026 --adapter ADAPTER-ID
python3 $M/setup.py enable
```

- `ACCOUNT NAME` and `ACCOUNT NUMBER` are your Tradovate Simulation account's name and number. The `--soup-account-id` option keeps its old name for every engine.
- `--ticker` is the current dated contract. For Lumi, use the MES contract, for example `MESZ2026`.
- `ADAPTER-ID` is the adapter from the table.
- `python3 $M/setup.py status` shows what is set up and running.

One bridge account runs one engine on one contract. Run either OTE Refined L or OTE Refined S on an account, never both.

**Lumi RTH Gap needs the bridge's 1-second MES feed.** The bridge reads live MES prices for your dated contract every second from your Tradovate session. The setup command turns the feed on for Lumi. If the feed is not live and fresh, Lumi does not start and takes no entry.

Lumi RTH Gap and Order Blocks Decoded have no public TradingView page yet, so you cannot request access to them.

## Updates and starting over

There is no in-place update in this beta. A new version comes as a new image: check it and import it as a new VM, then set it up again. Running a VM never changes the downloaded files, so you can also delete the VM and import the same files again to start over.

## Get help and send feedback

Open an issue on GitHub: https://github.com/Dropkingeth/decrypt-os/issues/new/choose. There are forms for bug reports, feedback and strategy access problems. Tell us what you did, what you expected and what happened. Add a screenshot if you can.

Issues are public. **Never post passwords, API keys, setup tokens, account numbers or your private alert address** in an issue or a screenshot. Report security problems privately, as described in [SECURITY.md](https://github.com/Dropkingeth/decrypt-os/blob/HEAD/SECURITY.md).

Decrypt OS is free software under the [GNU AGPL-3.0](https://github.com/Dropkingeth/decrypt-os/blob/HEAD/LICENSE). Third-party components keep their own licenses; see [NOTICES.md](https://github.com/Dropkingeth/decrypt-os/blob/HEAD/NOTICES.md).
