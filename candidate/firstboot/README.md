# First start: customer account and consent

This is the first-start source implementation, not native acceptance. The publishable image
contains no customer account/password, SSH host keys, machine identity or firstboot
state. The image build stages the bundle, sealing is verified separately, and native
testing uses a separate imported copy. Never redistribute the personalized test copy.

## Factory staging

Use the exact signed inputs with:

```python
bundle = stage.build_bundle(policy_path, expected_sha256, image_id)
```

`bundle` maps relative POSIX paths to `{bytes: bytes, mode: int}`. The function is
pure: it selects no disk and writes nothing. The image build must validate the allocated target
and all source pins, preflight every destination, then create these files exclusively
within that target. Do not overwrite existing files or follow symlinks. Create
parents root-owned mode 0755. Files are root-owned mode 0644; the service runs under
Python, not a file-execute permission. The image build then enables `stage.UNIT` with offline
`systemctl --root TARGET enable decrypt-firstboot.service`, never starts it while
building, and checks the resulting unit link. No factory sudo authorization or
customer account comes from this bundle.

The service runs before display-manager, systemd-user-sessions and sshd, with the
three exact Requires/After drop-ins. `/etc/nologin` prevents ordinary logins until
local identity/account checks pass. The selected greeter is then personalized with
the chosen instance account; its factory configuration contains no Username. Root is already locked at sealing and must
remain locked. The service does not unlock root or ship a password.

The approved notice is `NOTICE.txt`, SHA-256
`2a1903232950f56deba4a8cdd76e79e7b1022366fd6e56510c8c4bbf28ab8055`.
Its exact bytes match the in-app first-start notice. It is not the installation disk
warning or qualification-only profile acknowledgement.

## Commit and recovery protocol

State is `/var/lib/decrypt-firstboot/state.json` (root 0600, directory 0700).
A root-only advisory lock serializes setup and consent. Each state update validates
its exact keys, binding, sequence and allowed transition, then fsyncs a temporary
file, atomically publishes it and fsyncs the directory. No password, shadow hash,
private/public host key, machine ID or raw input is serialized. The account name
exists only in the personalized instance's state and reports.

| Boundary | Durable outcome | Interruption behavior |
| --- | --- | --- |
| Initial state | `identity/pending` | No identity operation attempted. |
| Before generating identity | `identity/in-progress` | Unknown outcome; do not replay. |
| Identity verified | `account/pending` + instance UUID | Machine ID and all SSH public identities are bound; no regeneration on restart. |
| Before creating account | `account/in-progress` + chosen account | Unknown useradd/password outcome; do not replay. |
| Password, password-required sudo and exact greeter account verified | `notice/pending` | Ordinary login can start. If removal of our login gate was interrupted, only that reversible step is completed after revalidation. |
| In-app notice accepted and password authentication succeeds | `complete/complete` | Consent and completion time are a single atomic commit. Cancellation changes nothing. |

A command failure records a stable `*-outcome-unknown` code. A killed process can
leave `in-progress`; that has the same no-replay meaning. Partial/native account
operations are never rolled back or erased automatically.

Recovery is explicit, from a trusted local recovery/root console for this imported
instance, not SSH or an app:

- `firstboot.py recover-identity`: type `RECOVER`; verify a complete existing
  machine ID and all SSH key pairs, then commit them. It never generates or
  replaces keys. An incomplete identity remains blocked for manual diagnosis.
- `firstboot.py recover-account`: type `RECOVER`; verify the exact recorded
  account, home directory and group, then enter and confirm its password again. It cannot choose
  another account or repeat useradd. If the account is absent or inconsistent,
  preserve this failed copy and use a separately imported clean image after review.

These actions cannot restart a factory install, partition a disk, alter package
locks, or reopen the factory's root authorization step. Never reset state to repeat a step.

The instance UUID is derived from a hash of the imported instance's machine ID and
all verified SSH public identities, with UUID version bits set. It is not a secret.
A changed machine ID or SSH identity causes subsequent verification to refuse.
Actual uniqueness across imported copies is a native acceptance check;
local fixtures cannot establish it.

## In-app consent callback

The app displays the exact notice and obtains an explicit acceptance first. It
then invokes only this fixed action with password-required sudo (no NOPASSWD):

```text
sudo -k -- /usr/bin/python3 -I -B /usr/local/lib/decrypt-firstboot/firstboot.py accept-notice --policy-sha256 POLICY_SHA --document-sha256 NOTICE_SHA --instance-id INSTANCE_UUID
```

The root helper accepts no custom file path, source, command or generic argument
list. It binds the policy, notice, instance and enrolled `SUDO_USER`/`SUDO_UID`,
requires one active local graphical logind session, and rechecks the account,
root lock and sudo policy. It does not display a terminal substitute for the
in-app notice. A canceled notice/password request or wrong binding leaves consent
pending. The callback authorizes no provider login, account connection or trade.

## Read-only reports

Run the installed pinned helper as root:

```text
python3 -I -B /usr/local/lib/decrypt-firstboot/firstboot.py readiness --policy-sha256 EXPECTED_SHA
python3 -I -B /usr/local/lib/decrypt-firstboot/firstboot.py completion --policy-sha256 EXPECTED_SHA
```

These commands create no state, lock, key, account or report file. The expected
policy digest comes from the signed input or external receipt, not the policy's
own contents. Both verify exact source/unit/drop-in staging and the enable link.

Before customer setup, successful readiness returns `decrypt.firstboot-readiness.v1`:
`imageId`, `policySha256`, `documentSha256`, `serviceSha256`, `ready:true`,
`customerAccountCreated:false`, `consentAccepted:false`, `firstStartVerified:false`.
It requires blank machine-id, no host keys/account, locked root, no passwordless
sudo and the staged login gate. This is factory readiness only.

After setup begins, the active observation is `decrypt.firstboot-report.v1`:
`imageId`, `policySha256`, `status`, `phase`, `instanceId`, `accountName`, `complete`,
`consentAccepted`, `documentSha256`, `completedAt`, `sequence`. Completion exits
successfully only for a complete state and matching live identity/account/policy.
Native testing captures the actual report and binds its exact hash to imported-copy
acceptance. A copied fixture or cached report cannot prove native success.

The running service also publishes a nonsecret root-owned 0644 cached copy at
`/run/decrypt-firstboot/report.json` for the app. Treat it as display state, not
fresh acceptance evidence. Private state stays root-only.

## Local verification

`python3 selftest.py` exercises only temporary fixture state and mocked native
operations. It covers complete and interrupted flows, controlled recovery,
consent cancellation/binding, immutable identity, no replay, secret-free state,
policy/bundle pins and read-only reports. The only subprocess entry points used
are help and an unsupported-host refusal. No host accounts, keys, services or
VMs are modified. Native first start remains unverified.

## Per-instance runtime authorization

After fresh identity and account setup, firstboot writes the root-controlled customer marker and firstboot install receipt, then publishes the positive `decrypt.customer-host-policy.v2` policy last. These records bind only this machine/instance, source manifest and local account. No vendor protected-machine list is required by the public image runtime. Partial publication keeps login gated; retry completes only byte-identical records without recreating identity. Read-only reports refuse missing/conflicting records. The distributable master must contain none of these personalized records.
