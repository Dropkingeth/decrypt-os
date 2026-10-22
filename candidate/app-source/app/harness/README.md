# Customer Claude runtime

## What it is
The user's own assistant runs on a pinned OpenClaw package inside an isolated service. It is Claude only. The user connects their own account; nothing from the developer or owner is imported.

Two agents run on one Gateway and share one sign-in:
- `main`: the Decrypt OS product helper. No model tools.
- `ict`: the personal ICT assistant from `app/ict-assistant`. Tools: `session_status` (time) and `web_fetch` (public pages) only.

Models: `anthropic/claude-opus-5-5`, fallback `anthropic/claude-sonnet-5`. Heartbeat and compaction use `anthropic/claude-haiku-4-5`. Heartbeat is off (`every: 0m`) and set to `isolatedSession: true`, `lightContext: true`.

## Lifecycle
1. Connections > Start assistant starts only the isolated customer service.
2. Connect Claude opens a terminal on workspace 4. The user picks one method:
   - Claude subscription (Pro or Max): paste a setup-token made with `claude setup-token` on any computer with Claude Code.
   - Anthropic API key: paste a key from the Anthropic Console.
   OpenClaw stores it in the customer profile `anthropic:customer`. The app never sees it.
3. After sign-in the Gateway child restarts once to pick up the new auth store. Status then shows Signed in / response unverified.
4. Verify connection sends one fixed prompt and records only success, model and time.
5. Open assistant / Open ICT assistant open the OpenClaw terminal UI on workspace 4.
6. Stop ends the service. Session-only mode removes sign-in and history; encrypted mode locks them.

## Boundary
The service runs under a separate user with no access to the owner home, read-only package and context mounts, private temp/devices, no host control sockets, no capabilities and bounded memory/CPU. The Gateway listens on loopback port 19789 with a token. A per-UID nftables table (`installer/loopback_guard.py`, reloaded at every start) blocks the service from the bridge, app and browser-debugging ports on loopback. The HTTP API accepts only empty objects for fixed actions.

The global tool profile is `minimal` with shell, file, UI, automation, device, messaging, media and memory tools denied. `main` denies all tools. `ict` adds only `web_fetch`. Neither agent can trade, read files, run commands or change settings.

## Encrypted storage (optional)
Keep sign-in after restart creates a 1 GiB LUKS2 volume (Argon2id) at `/var/lib/decrypt-os/customer-storage/customer.luks`. The passphrase is typed in a native terminal and is never stored. Stop and lock closes the volume. Unlock never formats an existing image. Encryption protects the closed volume, not an unlocked session or a compromised root account.

## Known limits
- The pinned OpenClaw is 2026.9.9. It sends `claude-opus-5-5` requests, but a live Opus 5.5 response has not been checked on this pin. If it fails, the fallback model answers. Bumping the pin is a build task.
- A setup-token needs Claude Code on some computer to create it. The OS does not ship Claude Code.
  The sign-in terminal can optionally install one pinned release (Claude Code 2.1.287, native Linux ARM64 package, sha512 from the npm registry) into the assistant's own runtime folder. It uses `npm ci` with a lockfile, `--ignore-scripts`, empty npm configs and no proxy, then runs `claude setup-token` with auto-update off. Bumping the pin means a new version, tarball URL and integrity value in `bootstrap.py`.
- Token refresh/revocation, whole-VM reboot and encrypted backup/restore are not qualified.
