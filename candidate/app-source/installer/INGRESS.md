# Customer HTTPS alert receiver

This optional administrator tool installs a **dedicated** Caddy proxy for your own alert domain. It does not configure DNS, router/firewall rules, a tunnel or any provider. It does not open the Decrypt OS app/API or browser-debugging ports to the internet.

The only proxied request is `POST /signal/*` for the exact configured host, forwarded to `127.0.0.1:8812`. The receiver still checks the exact instance, secret and message contract. Other paths and methods return 404. A host that disagrees with the TLS server name is rejected. The URL's final segment is private: do not put a real alert URL in shell commands, screenshots, diagnostics, proxy logs or support messages.

## Prerequisites

- Use an installed, enrolled customer OS, with administrator access. This tool refuses hosts listed in the optional protected-host file and untrusted/mutable helper files.
- In Decrypt OS, set your **customer-owned HTTPS alert origin** and start the local receiver. Use only `https://` plus a lowercase public fully qualified domain, without a path, trailing slash, credentials, port, query or fragment. The installer reads only that public-origin setting as the enrolled desktop account and requires an exact match.
- Install the optional distribution **Caddy v2** package through your normal reviewed OS package procedure. Its root-owned executable must be `/usr/bin/caddy`. Caddy is not downloaded or installed by this tool; its version and executable hash are recorded. Python 3, systemd and the existing customer application are also required.
- Dedicate this origin and the guest's TCP port **443** to this service. An existing/enabled Caddy service or occupied port is refused, not replaced. There is no merge into another website's proxy.
- Your DNS and inbound routing must reach this guest. Certificates use ACME **TLS-ALPN on TCP port 443**. HTTP challenge and HTTP-to-HTTPS redirects are disabled. HTTP/3 is disabled, so this service does not listen on UDP 443 or port 80. Public reachability, certificate issuance and provider delivery need separate checks from outside the guest. The tool makes no claim that you own the domain merely because a command was entered.

No credential, token, certificate private key or signing key belongs in the review plan. Caddy's generated certificate/account material is private service state under `/var/lib/decrypt-os-ingress`, with a systemd dynamic user, mode-0700 state directory and umask 0077. Root-owned proxy configuration contains the public origin only. The installer refuses retained certificate directories from an earlier setup instead of adopting or deleting them.

## Prepare, inspect and apply

Run commands in a local terminal. Substitute your own origin for `YOUR-OWN-ALERT-DOMAIN`; there is no default owner domain or hosted service. The administrator command must use the fixed installed script with Python isolation enabled:

```sh
sudo /usr/bin/python3 -I -B /usr/local/lib/decrypt-os/installer/customer_ingress.py prepare \
  --origin https://YOUR-OWN-ALERT-DOMAIN
```

Prepare validates the installed machine/source receipt, configured origin, exact Caddy binary, loopback-only receiver and free public port. It writes a new root-owned plan, generated configuration and unit under `/var/lib/decrypt-os-ingress-setup/plans/`, then runs the exact Caddy binary's `validate` command. It does **not** install, enable or start a service. Review the returned `planPath` locally and retain its `planSha256`. An unsuccessful validation cannot be applied. Repeating preparation creates a separate plan and preserves earlier results.

```sh
sudo /usr/bin/python3 -I -B /usr/local/lib/decrypt-os/installer/customer_ingress.py inspect \
  --plan-sha256 REVIEWED_PLAN_SHA256
```

Inspection reports the domain, machine/source identity, Caddy version/hash, plan expiry and the two proposed file hashes without writing state. Check these before applying. Apply within 24 hours using that exact reviewed hash, only when you control the domain and intend to expose this dedicated signal receiver:

```sh
sudo /usr/bin/python3 -I -B /usr/local/lib/decrypt-os/installer/customer_ingress.py apply \
  --plan-sha256 REVIEWED_PLAN_SHA256 \
  --acknowledge-domain-control --acknowledge-public-service
```

Apply repeats the guards and Caddy validation before installing either file. It creates `/etc/decrypt-os/ingress/caddy.json` and `/etc/systemd/system/decrypt-os-ingress.service`, then enables and starts **only** that unit. A successful start records service state, not TLS or delivery acceptance. The unit persists across reboot, repeats installed origin/source/binary verification on startup, and never loads an admin-API configuration or autosaved replacement. It can start before the desktop receiver; alerts cannot be accepted until the customer application is running with its configured receiver and eligible instance.

```sh
sudo /usr/bin/python3 -I -B /usr/local/lib/decrypt-os/installer/customer_ingress.py inspect
```

All outputs keep `publicTlsVerified`, `publicReachabilityVerified` and `nativeRebootVerified` false. Verify those separately on the actual customer deployment; fixture tests do not qualify them. The service discards access/error/output logs because an upstream error can otherwise reveal the secret URL. The system journal may show service lifecycle events, not application request logs. Avoid adding an access log or enabling Caddy debug logging.

## Changes, failures and removal from service

The first apply writes a durable attempt marker before installing files. A service-start failure or interruption preserves its partial state and blocks automatic apply retries. Inspection reports `partial_requires_review` when files exist without a complete install receipt, or `installed_service_start_unconfirmed` when installation was recorded but completion of service startup was not. A currently active process does not erase that uncertainty. Do not delete the marker, force the installer or replace a customer proxy to make a check pass. Inspect the retained plan/configuration/unit/receipt and review the actual native state before a separate recovery revision.

Changes to the configured origin, helper, original install/source receipt or Caddy executable stop startup verification. An administrator must review and explicitly rebind a future revision; this tool does not silently trust a system-package upgrade or change a live domain. It has no automatic update/reconfigure workflow. Plan retention contains public origin and host/source identifiers, not provider secrets.

To stop accepting internet requests during decommissioning, use the fixed unit directly:

```sh
sudo systemctl disable --now decrypt-os-ingress.service
```

This disables only the dedicated proxy. It does not stop the customer's local trade supervision, remove certificates/state, revoke provider credentials, alter DNS or delete an account. Close/revoke provider access through the provider and review retained local data separately. Decrypt OS offboarding does not automatically remove this root-owned optional service.

**Acceptance still required:** actual Caddy parsing/module support on the installed version, native service hardening and restart behavior, external DNS/TLS/443 reachability, method/host/path rejection, absence of secret request logs, authenticated delivery and token rotation. No source test or service-active response establishes trading readiness.

## Legacy webhook and tunnel path (off, not supported)

Older pre-OS setups are not part of Decrypt OS and are not covered here. Bridge Advanced can show and pause such a setup if it finds one and can register a webhook entry; a registered entry stays off and Bridge Advanced never creates or starts a tunnel or receiver.

Use the receiver on this page instead. Its URL carries a random secret of 43 or more characters, checked in constant time, and contains no user name. The loopback firewall (`installer/loopback_guard.py`) also keeps the ingress service off the bridge, app and browser-debugging ports.
