# tvbridge-admin API (v1)

Loopback service behind the **Bridge Advanced** window (workspace 5). It is separate from the read-only Bridge panel (port 8779), which never gains write code.

- Base URL: `http://127.0.0.1:8781`
- Mode: `live` (what `setup.py init` writes) or `sandbox` (tests: a copied tree, simulated restarts or a fake systemctl, dry-run orders).
- Restarter (profile `restarter`): `simulated` records restarts; `systemd` runs the live sequence in "Live restarter" below.
- Soup (profile `soup_service`, optional): `{unit: "<name>.service", port?}` for an install that runs Soup as its own systemd unit. Without it (what `setup.py init` writes) Soup runs inside the Decrypt OS app and the overview shows no Soup unit.

## Security
- `Host` must be `127.0.0.1:8781` or `localhost:8781`. Anything else gets 421.
- Every `/api/*` call needs the header `X-Admin-Token: <token>`. The desktop launcher creates a fresh token for each session and opens `http://127.0.0.1:8781/advanced#t=<token>` (the `/advanced` path gives the window its own class for the workspace-5 rule). The page reads the token from the fragment, keeps it in `sessionStorage` and clears the fragment from the address bar.
- Every POST needs `Content-Type: application/json` and `Origin: http://127.0.0.1:8781` (or `http://localhost:8781`). Missing or other origins get 403.
- `GET /` (the page) and `GET /static/*` need no token.
- Errors look like `{"error": "<code>", "detail": "<plain sentence>"}`, with 400 for bad input, 401 or 403 for auth, 404 when not found, 409 for a conflict or a state that doesn't allow the action, and 421 for a bad host.

## Read endpoints
| Method | Path | Returns |
|---|---|---|
| GET | `/api/overview` | `Overview` (everything the page needs, one call; poll every 3 s) |
| GET | `/api/changes?limit=50` | `{changes: [Change]}`, newest first |
| GET | `/api/changes/<id>` | `Change` |
| GET | `/api/audit?limit=100` | `{entries: [AuditEntry]}`, newest first |
| GET | `/api/export` | `bridge-setup-<UTC>.tar.gz` (`application/gzip`, attachment): "Export whole setup", read only, secrets redacted. See "Export" below. |
| GET | `/api/logins/<login>/visible` | `{login, accounts: [{name, already_configured: bool, key\|null}], error\|null, note\|null}`: account names Tradovate shows on that login. The list is empty while the login is signed out; `note` says so. |

## Write flow: one path for every change
1. `POST /api/changes` with `{type, params}` creates a **preview**: `Change` with `status: "previewed"`, `plan`, `eta` and `preview_hash`. Nothing changes yet. Invalid input returns 400 with a plain-language `detail`. If `plan.blockers` is non-empty, the change can't be confirmed.
2. `POST /api/changes/<id>/confirm` with `{preview_hash, confirm_text?}`:
   - Immediate types (pause, resume, pause_webhook, resume_webhook, flatten) apply at once and return `applied` or `failed`.
   - Structural types return `waiting_flat` when an affected account has a position or working orders, and apply on their own once all are flat. Otherwise they go straight to `applying` and then `applied`.
   - If the config changed after the preview, the call returns 409 `stale_preview`; preview again.
   - `confirm_text` is required when `plan.confirm.required` is true. It must equal `plan.confirm.expect`, which is the account label.
3. `POST /api/changes/<id>/cancel` works only in `previewed` or `waiting_flat`.
4. `POST /api/changes/<id>/rollback` with `{}` returns a **preview** of a new change of type `rollback`. It's the same as `POST /api/changes {type: "rollback", params: {change: <id>}}`. Confirm it like any other change. It works for applied changes (not flatten) whose files haven't changed since; otherwise the preview has a blocker.

`Change.status` values: `previewed`, `waiting_flat`, `applying`, `applied`, `failed` (auto-rolled back; see `result`), `cancelled`, `expired` (a preview not confirmed within 15 minutes), `rolled_back`, `planned` (saved, needs a publish step such as a new lane), `manual` (the plan lists steps you run yourself).

## Change types (`type` and `params`)
| type | params | kind | notes |
|---|---|---|---|
| `pause` | `account`, `reason` | immediate | Writes the account's existing kill-switch file with an admin note. New entries stop; an open trade keeps its stops and exits. Pausing a **leader** pauses its whole webhook, because followers enter only after the leader. |
| `resume` | `account` | immediate | Renames the switch to `*.removed-<UTC>`, which is the existing convention. If a safety event set the switch (an empty or non-admin file), `confirm.required` is true. |
| `pause_webhook` / `resume_webhook` | `webhook`, `reason`? | immediate | Uses the leader's kill switch, the same file as pausing the leader. |
| `flatten` | `account`, `reason`? | immediate, confirmed | Closes the position and cancels working orders for **one** account, then it stays paused. `confirm.required` is always true (type the label). With `order_adapter: "bridge"` it calls the owning login's daemon once (see "Flatten now" below); with `dry-run` it records the call and sends nothing. |
| `set_enabled` | `account`, `enabled` | structural | Turns a follower lane on or off in the firing order (the account stays configured). Not for leaders. |
| `reorder` | `webhook`, `followers` (all follower keys, new order) | structural | Firing order. The leader always fires first. |
| `move_account` | `account`, `to_webhook` (id or `null` for Not assigned), `index`? | structural | Waits until the account is flat. A leader can't be moved. |
| `add_account` | `login`, `key`, `label`, `account_name`, `account_id` (optional; null lets the bridge look it up by name), `prop_profile`, `webhook` | structural | Creates the follower entry, journal dir, firing entry and lane dir. The account **starts paused**. |
| `retire_account` | `account` | structural | Pauses it, waits for flat, then removes it from firing and its login's daemon. Journals are kept. A leader can't be retired. |
| `add_webhook` | `name`, `strategy`, `leader`, `ticker`? | planned | Generates a random stream ID and a short path. Creates lane files. Status is `planned` until the multi-lane publish step. Same name rules as `register_webhook`, so it also refuses the name of an off webhook. |
| `register_webhook` | `name` | registry only | Registers a webhook that is **off**: `state: "off"`, no strategy, leader, followers or stream ID. Reserves the first free port from 8800 and the path `/webhook/<slug of name>-<hash>` (a 12-character hash derived from the change id; no Linux user name in it) (W2 → `/webhook/w2-3f9a0c12ab77`, hash varies). Writes only the admin registry (`admin/webhooks.json`): no restart, no unit, no bridge, Soup or tunnel file. Applies right away after confirm (`applied`, audit entry). `plan.generated` = `{path, url, port}`. Refused when the name is empty or over 40 characters, when its slug is `main`, when the name or slug matches any registered webhook (any state, case-insensitive), and when `live_switches_only` is set. |
| `remove_webhook` | `webhook` | planned/structural | Refused for `main`, or when the webhook still has followers. An off webhook can always be removed; its port and path are freed. |
| `rotate_stream_id` | `webhook` | structural, manual | New random ID. Applies only when flat, together with you changing the TradingView input. `plan.manual_steps` lists both sides. |
| `add_login` | `label` | login setup (no wait) | Guided Add login (see "Add login" below): the id is the next free letter; runs `install.py add-login` (dry run first), writes the Flatten-now token and drop-ins, enables and starts **only** the new login's browser window, restarts the read-only status panel. You sign in by hand. Without `login_setup` in the profile it stays `manual` (steps for you). |
| `start_login` | `login` | immediate | Only a login this page added (its login record says `created`). After your sign-in: enables the login's daemon, trigger and guard timer and starts the trigger and the guard timer, then returns at once with `result.login.state: "starting"` (the engine lock is never held while the login signs in). The **login guard** starts the daemon once it sees the page signed in; this page never starts a daemon. `Login.setup.signin` then reports `connected` / `signed_out` / `starting` / `bad_token`. |
| `remove_login` | `login` | login setup (no wait), confirmed | Only a login this page added (including one whose add did not finish, even with no `logins.json` entry) and that has no accounts. Stops and disables its units, removes it from the registry, peer lists, `peers.env` and `firing.env`, and keeps its folder (with its token file and drop-in) and browser profile as `*.removed-<UTC>`. Type the login's label. Also what `rollback` of an `add_login` does. |

**Size.** The strategy alert in TradingView sets the size. This page sets no contract limit. The bridge only checks that a quantity is a positive whole number and that it fits the account's position (an exit never asks for more than is open).
- `set_max_contracts` is retired. The API refuses it with HTTP 400: "The size cap is retired: the strategy alert sets the size." A `set_max_contracts` change still waiting from an earlier version fails with "No longer possible: …" and writes nothing.
- `add_account` ignores a `max_contracts` param and never writes the key.
- Older configs may still carry `max_contracts`. A positive whole number of any size is accepted and ignored. Any other value (0, a negative number, text, `true`, a decimal) is a malformed legacy value the bridge refuses to start with: structural changes are refused and `resume` is blocked with "max_contracts is retired (the strategy alert sets the size); remove it from the config."

### Off webhooks
A webhook with `state: "off"` exists in the registry and has nothing running. Its Soup copy, tunnel rule and firing config are staged elsewhere as templates; no alert is accepted. These changes refuse it with HTTP 400 and a plain sentence that starts "<name> is off and waiting for a strategy.":
- `move_account` or `add_account` into it: "It can't take accounts yet."
- `reorder`: "It has no accounts to reorder."
- `set_enabled` for an account listed in it (only possible by hand-editing the registry): "Its lanes can't be turned on or off."
- `pause_webhook` / `resume_webhook`: "It can't be paused; nothing runs on it." / "It can't be resumed; nothing runs on it."
- `rotate_stream_id`: "It gets a stream ID when a strategy is approved."

`remove_webhook` works. Any change whose draft lists an account in an off webhook fails validation with the same opening sentence.


## Objects
```
Overview {
  mode: "sandbox"|"live", generated_at: epoch_s,
  market: {state: "open"|"closed", reason, weekend_hold: bool, weekend_hold_text},
  status_source: {ok: bool, age_s, error|null},   // read-only Bridge panel feed used for flat checks
  webhooks: [Webhook], accounts: [Account], logins: [Login], strategies: [Strategy],
  pending: [Change],          // confirmed and not done yet: waiting_flat + applying (open previews are drafts and expire)
  recent: [Change],           // last 10 finished
  needs_person: [{change_id, summary, at, units: [string], firing_stopped: bool, firing_state, backup|null, text}],  // failed changes whose restore restart also failed
  restarter: "simulated"|"systemd",
  coming_soon: {rotate_stream_id|activate_webhook|add_webhook|add_login?: {coming_soon: true, text}},
                              // not live yet: show "Coming soon" + text instead of a button that would be refused
  export: {available: true, url: "/api/export"},
  login_setup: {available: bool, reason|null, next_id: "c"|...|null,   // Add login on/off and the id it would get
                incomplete: [{id, label, state, change_id, error|null, text}]},  // adds that did not finish: offer Remove
  drift: [string]             // plain sentences when live files disagree with each other
}
Webhook {
  id, name, path|null, url|null, url_active: bool,   // url_active: true only for live; the tunnel routes only live paths
  state: "live"|"planned"|"off",
  status_text: "Live"|"Planned — not running yet"|"Off — waiting for a strategy",
  strategy: {id|null, name|null}, stream_id|null, stream_id_strength: "strong"|"weak"|null, ticker|null,
  leader: account key|null, followers: [account keys in firing order],
  paused: bool, pause: Pause|null,
  soup: {unit|null, port|null, enabled: bool|null, locked: bool|null, installed?: false},
                              // unit and port come from the profile's optional soup_service; both null when
                              // Soup runs inside the Decrypt OS app (the default)
  notes: [string],
  coming_soon: {rotate_stream_id: {coming_soon: true, text}, activate_webhook?: {coming_soon: true, text}}
                              // activate_webhook on every webhook that is not live (off, planned)
}
// state "off": strategy {id: null, name: null}, stream_id null, stream_id_strength null (no "easy to guess" note),
//   leader null, followers [], paused false, pause null, url_active false,
//   soup {unit: "<soup_service unit>@<id>.service"|null, port, enabled: false, locked: true, installed: false} (not probed),
//   notes ["Off — waiting for a strategy. Nothing runs and no alert is accepted until you approve a strategy."]
// A registry entry without name or path still renders: name falls back to the id, path and url are null.
Account {
  key, label, login, login_label, role: "leader"|"follower"|"unassigned",
  webhook: id|null, order: int|null, enabled: bool, prop_profile,
  account_name_masked, account_id, native_exits: bool,
  pause: {on: bool, set_by: "admin"|"system"|null, reason|null, at|null},
  position: {net: int|null, flat: bool|null, orders_text, text},
  status: {label, tone: "green"|"amber"|"red"|""}
}
Login { id, label, primary: bool, mode: "main"|"followers_only", state_label, tone, accounts: [keys],
        setup: LoginSetup|null }      // null for the primary login and when login_setup is off
LoginSetup { stage: "awaiting_signin"|"running"|"stopped"|"incomplete"|"unknown",
             origin: "this_page"|"outside",     // only logins this page added can be started or removed here
             units: {browser|daemon|trigger|guard_timer|guard: {unit, active, enabled}}|null,
             devtools_port, bridge_url, added_by: change id|null,
             signin: {state: "connected"|"signed_out"|"starting"|"bad_token", text}|null,   // after Start
             can_start: bool, start_blocker: string|null, can_remove: bool, remove_blocker: string|null,
             account_blockers: [string] }   // why Add account on this login is blocked (peers not restarted)
Strategy { id, name, build, contract_ref, inputs_count, stream_field: "stream_id", notes }
Change {
  id, type, params, status, created_at, updated_at, summary,
  plan: {
    immediate: bool, requires_flat: bool, affected: [keys],
    wait_on: [keys],          // accounts that must be flat first: the edited ones plus every account whose service restarts
                              // (a daemon restart also stops the firing service, so the whole Main webhook waits)
    files: [{path, action: "create"|"modify"|"rename"|"delete"|"mkdir", to|null, diff}],
    restarts: [{unit, why, seconds}],
    warnings: [string], blockers: [string], manual_steps: [string],
    confirm: {required: bool, expect|null, prompt|null},
    generated: {stream_id?, path?, url?, port?, login_id?},
    login?: PlanLogin,         // add_login, start_login, remove_login
    coming_soon?: {coming_soon: true, text}   // rotate_stream_id, add_webhook: the preview still works, but the
                                              // page shows "Coming soon" instead of offering it
  },
  preview_hash, eta: {kind: "now"|"after_flat"|"after_signin"|"cooldown"|"blocked"|"manual"|"planned", seconds|null,
                      text, waiting_on: [keys], waiting_for_signin?: [login labels],
                      reason?: "signin"|"daemon_down"|"cooldown",
                      cooldown?: [{unit, burst, interval_s, used, free_in_s}]},
  result: {ok: bool, steps: [{name, ok, detail, at}], rolled_back: bool,     // rolled_back: files restored
           restart_log?: [{phase: "apply"|"rollback", unit, action, ok: bool|null, detail, at}],  // saved as it happens
           needs_person?: {units: [string], firing_stopped: bool, firing_state, backup?, text},
           refused_lanes_before?: {key: reason}, refused_lanes_after?: {key: reason}}|null,
  error|null
}
AuditEntry { at, change_id, type, status, summary, actor: "operator", files: [path], prev_hash, hash,
             restarts?: [{phase, unit, action, ok}], needs_person?: [unit] }
```

PlanLogin { id, label,
  devtools_port?, bridge_port?, bridge_url?, profile_dir?,            // add_login: what install.py picked
  units: [{unit, action: "run"|"enable"|"enable, start"|"restart"|"stop, disable"|"stop", when: "now"|"Start step", why?}],
  tokens?: [{name, kind: "order"|"read-only"|"flatten", files: [path], note}],   // names only, never values
  units_now?: {role: {unit, active, enabled}} }                      // start_login: the units at preview
// plan.files entries of login changes also carry `by: "install.py"|"this page"` and `detail`.
// Change.result.login after apply: {id, label, stage, text, ...}
//   add_login:    stage "awaiting_signin", devtools_port, bridge_port, browser_unit, next: "start_login"
//   start_login:  stage "running", connected: false, state: "starting" (live state: Login.setup.signin)
//   remove_login: stage "removed", backup, profile_backup|null

## ETA rules
- Immediate: `now`, about 2 s.
- Structural, all affected flat: `now`. Seconds = sum of the restarts, plus a 5 s verify.
- Structural, someone in a trade: `after_flat`, listing `waiting_on`. The text estimates the latest the trade can last: "when Account B's trade closes (its stops and exits stay on); at the latest by the strategy's end-of-day exit". The page polls; the change applies on its own.
- Status feed down: flat is unknown, so `blocked` with the reason. The service never assumes flat.

## Live restarter (`restarter: "systemd"`)
Runs only after every account in `wait_on` is flat **and** every login whose daemon restarts is signed in and synced. Otherwise the change stays `waiting_flat` with `eta.kind: "after_signin"`, `eta.waiting_for_signin: [login labels]` and the text "Waits for Tradovate sign-in (Login B). Applies after the next sign-in, …". The waiter applies it on its own; `POST /api/changes/<id>/cancel` cancels it like any wait. Changes that restart no daemon (lane on/off, reorder, moves and other firing-only edits) apply while logged out. One unit at a time, no retry loops:
1. **Preflight** (before anything is written): every unit is one this page may restart (Soup never), each daemon's read token is available, and every unit is healthy now. A failure ends the change as `failed` with "Not applied: …"; nothing is written or restarted. Step name `preflight`.
2. Stop `tvbridge-firing.service` (when a daemon or the firing order restarts).
3. Restart each affected daemon, primary login first. Within 60 s it must be **synced** (`GET /health/<read token>`: page `authorized`, `synced: true`). A daemon is **never restarted while its login is logged out or not synced**: it serves only after sign-in, so a logged-out restart would end `failed`. Before that, the change waits (below). If the login guard reports the login logged out at the moment of the restart, the daemon is skipped and the change fails and restores. Nothing signs in.
4. Start `tvbridge-firing.service`; within 30 s its `firing/status.json` must be rewritten by the new process (under 5 s old, its pid, the firing order from `firing.json`).
5. Restart `tvbridge-status.service`; `GET /api/status` must answer 200 within 30 s.

On any failure the engine restores every file from the backup and runs the same sequence on the old files (`restart_log` phase `rollback`, step `restart old files`). If that also fails, the change stays `failed` with `result.needs_person` `{units, firing_stopped, text}` listing the units. When the firing service is left stopped, the text says "followers will not fire until this is fixed". The confirm call returns when the sequence ends (up to a few minutes); `result.restart_log` fills in as it runs.

Restart safety:
- **Start limits.** Each unit's `StartLimitBurst`/`StartLimitIntervalUSec` is read and this page's own starts are counted from `admin/restarts.jsonl` (one line per start). A change waits (`eta.kind: "cooldown"`, "Cooling down: restart limit …") unless every unit it touches has 2 starts left (apply + restore). The waiter applies the oldest ready change first and at most one restarting change per unit per pass. Starts made by systemd itself (Restart=on-failure) or by the login guard are not in this count.
- **Daemons.** Apply restarts a daemon only when a reading taken right before its restart says synced; otherwise the change fails without restarting it. The restore run restarts only daemons the apply run restarted, and only when the login guard has a fresh reading that does not say logged out. A down daemon waits with `eta.reason: "daemon_down"` ("… is down: needs a person"); a read token the daemon rejects (HTTP 401/403/404) refuses the change instead of waiting.
- **Restore re-checks flat.** If someone is in a trade at restore time, no daemon is restarted back. `needs_person` names those daemons and says whether the firing service is running.
- **Firing** is started with `--job-mode=ignore-requirements` (its `Wants=tvbridge-daemon.service` never starts daemon A), and preflight requires `tvbridge-daemon.service` to be active whenever firing restarts. An enabled lane the new firing process refuses at start fails verify and restores, except the account an `add_account` just added (it starts paused).
- **Interrupted.** On start, a change left `applying` becomes `failed` with `needs_person` ("stopped while this change was applying"), the backup path for a manual restore and the firing state. Nothing is restored automatically.
- **Profiles.** Live needs `systemd.systemctl` exactly `["/usr/bin/systemctl"]`, `status_api_url` exactly `http://127.0.0.1:8779/api/status` and `command_timeout_s` above 150 (default 180). Sandbox needs the argv to run `tools/fake_systemctl.py`.

## Flatten now (`order_adapter: "bridge"`, tvbridge 1.1.3)
- **Call:** `POST {bridge_url of the account's login}/manual/<manual token>/<key>/flatten` with `{"reason", "change_id"}`. `<key>` is the follower key, or `main` for a login's leader (Account A is `main`). `change_id` is the change's own id (it matches the bridge's `CHANGE_ID_RE`). Loopback only; `bridge_url` comes from `logins.json`. Timeout 60 s (`order_bridge.timeout_s`).
- **Token:** the login's `manual_token_env` (daemon config, else `logins.json`, else `TVBRIDGE_MANUAL_TOKEN_<ID>`), from `~/.local/share/decrypt-os/tvbridge/logins/manual-a.env` / `manual-b.env` through the `manual-tokens.conf` drop-in. It is never stored or shown.
- **Before sending (preview blockers):** the token must be available, and the login's daemon must be synced (same `/health` read as the restarter). Otherwise the preview has a blocker ("…Sign in to Tradovate first; Flatten now never signs in.") and confirm returns 409 `blocked`. Nothing is sent.
- **Leader:** `plan.flatten.also_blocks` lists the accounts its pause also blocks (its webhook's followers and every login whose `master_kill_switch_file` is the leader's switch), and a warning names them. The bridge may also return `also_blocks`.
- **Pause:** the bridge sets the account's switch first with the admin note `{set_by: "tvbridge-admin", reason, change_id, at}`, so the account shows Paused with the flatten reason (read from the switch file as for any pause). The page writes no file for a bridge flatten.
- **Never retried.** An unknown outcome can be sent again only by the person: `POST /api/changes/<id>/resend` with `{confirm_text}` (the label again). It reuses the same change id; the bridge answers a repeated id from its journal (`duplicate: true`) and sends nothing twice. Allowed only when `result.flatten.outcome_class` is `unknown`.
- **Result:** `Change.result.flatten` (and the audit entry's `flatten`):
```
{ok, outcome_class: "flat"|"refused"|"incomplete"|"unknown"|"rejected_request"|"dry_run", detail,
 http_status|null, transport_error|null, key, login, change_id, seconds, sent: bool, unknown: bool,
 kill_switch_after: {on, set_by, reason, at},
 reply: {ok, error, account, account_id, net_position, cancelled: [order ids], still_working: [order ids],
         kill_switch: "set"|"already_set"|null, entries_disabled, outcome, command_id, receipt,
         duplicate?, protective_stop_working?, also_blocks?}|null}
```
  `result.flatten_attempts` counts calls (1, or 2 after a resend). Status: `applied` only for `flat`; every other class is `failed`.
- **Classes:** `flat` = verified flat, nothing working. `refused` (nothing sent; switch usually on) = `logged_out`, `not_synced`, `unhealthy`, `busy`, `daemon_stopping`, `kill_switch_unavailable`. `incomplete` (orders went out; check Tradovate; switch on) = `cancel_not_confirmed`, `liquidation_rejected`, `not_flat`, `orders_still_working`. `unknown` ("Unknown outcome: check the Bridge window") = `liquidation_unknown`, `in_progress`, any other error, no answer or timeout, HTTP 500 or another 5xx. `rejected_request` (nothing sent) = HTTP 400 bad body, 403 bad token, 404 unknown key or route off, 503 account unavailable.
- **The page should show:** `detail` (one plain sentence), `outcome_class` as the tone, `reply.net_position`, `reply.still_working` (count + ids), `reply.cancelled` (count), `reply.kill_switch` with `kill_switch_after` (paused, reason), `reply.duplicate` ("answer to an earlier call"), `reply.protective_stop_working` when present, `reply.also_blocks` / `plan.flatten.also_blocks` (labels), `reply.command_id` (small, for the journal), and a "Send again" action only when `outcome_class` is `unknown`.

## Add login (`login_setup`)
Profile keys (`setup.py init` writes them):
```
"token_files": true,
"login_setup": {"unit_dir": "~/.config/systemd/user",
                "browser_profile_dir": "~/.local/share/decrypt-os/tvbridge-browser/profile",   // new: profile-<id> beside it
                "install_script": "<module>/bridge/scripts/install.py", "python": "/usr/bin/python3",
                "guard_dir": "<module>/login-guard", "template_dirs": ["/etc/systemd/user"]}
```
Needs the systemd restarter. Sandbox profiles may not point `unit_dir` at `~/.config/systemd/user` or `browser_profile_dir` under `~/.local/share/decrypt-os/tvbridge-browser`; live `unit_dir` must be the service user's.

**What a new login gets:**
- `install.py add-login <id> --label <label>`: `logins/<id>/{config.json, login.env, tvbridge.env, journal/, ENTRIES-DISABLED}`, a browser profile `profile-<id>` (marker `.tvbridge-login`), a `logins.json` entry, login A's `peer_logins` gains the login, `peers.env` gains `TVBRIDGE_READ_TOKEN_<ID>`, `firing.env` gains `TVBRIDGE_TOKEN_<ID>`. Ports: next free DevTools from 9557, loopback from 8778 (Login C: 9558 / 8780).
- This page: the login-wide `ENTRIES-DISABLED` is renamed `*.removed-<UTC>` (every account added later starts paused with its own switch), `logins/manual-<id>.env` (0600, `TVBRIDGE_MANUAL_TOKEN_<ID>`), `tvbridge-daemon@<id>.service.d/manual-token.conf`, a line in `tvbridge-admin.service.d/manual-tokens.conf`, `systemctl --user daemon-reload`, enable + start `tvbridge-browser@<id>.service`, restart `tvbridge-status.service` (through the restarter; it loads the new read-only token).
- At Start: enable `tvbridge-daemon@<id>.service`, `tvbridge-login-trigger@<id>.service`, `tvbridge-login-guard@<id>.timer`; start the trigger and the timer.

**Rules:**
- Preview runs `install.py add-login --dry-run` and lists every file (with `by`), unit, port and token **name**. Blockers: leftovers of an earlier login with that id, a picked port that belongs to another service or is listening (install.py does not know 8781, so login D would collide with this page), missing unit templates or guard `logins.py`, a missing read token of an existing login (install.py would make a new one), any install.py warning. The preview hash covers the dry run and the env/registry/config files, so a change between preview and confirm is `stale_preview`.
- Confirm runs the dry run again and must get the same plan, then the real run must report the same actions. Then it checks: the registry gained exactly the dry run's entry; login A's config changed only by the new `peer_logins` entry; every existing token in `peers.env`/`firing.env` is unchanged; the new config is an empty `followers_only` login the bridge validators accept.
- Never restarted: daemons A and B, firing, Soup. Daemon A reads its new peer at its next restart (until then its cross-login hedge check does not see the new login, which has no accounts yet). Daemon B's `peer_logins` is not changed (install.py changes only the primary's). Firing learns the login at its next restart, which the first `add_account` on that login does under the usual wait-for-flat rules (with daemon `<id>` restarted only when it is synced).
- `token_files: true`: this service reads `TVBRIDGE_READ_TOKEN_<ID>` from `logins/peers.env` and `TVBRIDGE_MANUAL_TOKEN_<ID>` from `logins/manual-<login>.env` when its own environment lacks them, so a new login works here without restarting this service. It never uses an order token. To check that install.py and Remove left the other logins' tokens alone, it compares SHA-256 digests of the values in `peers.env` and `firing.env` and drops the bytes; lines are moved as opaque bytes, and originals are kept as hard links (below), so no token value is held, logged or copied.

**Login safeguards:**
- **Only logins this page added.** Add writes a login record `admin/logins/<id>.json` (fsynced) before its first change. Start and Remove refuse any login without one (a login set up by hand: "was not added by this page … by hand").
- **Login record.** It lists every path the add will create, every file it will modify (`logins.json`, login A's and every other login's `config.json`, `peers.env`, `firing.env`, the admin drop-in) with its sha256 and hard-linked original (`<file>.addlogin-<change id>`), the units, the token digests, and each finished step. States: `adding`, `created`, `undone`, `add_failed`, `interrupted`, `removing`, `removed`.
- **Undo.** Each modified file is put back byte for byte from its hard-linked original and verified by sha256. A file that cannot be verified makes the change "needs a person" with the exact paths, and the record turns `add_failed`. An undo that is not complete still restarts the status panel when it was restarted.
- **Interrupted (service killed).** On start, an `applying` login change becomes failed + needs a person: the last step, the record path, every path to check, and "To undo: Remove login <label>". `Remove login` cleans up from the record even when `logins.json` never got the entry; originals left by the interrupted add are kept in `logins/<id>.removed-<UTC>/originals/`.
- **Peers.** Add also adds the new login to every other non-primary login's `peer_logins` (for example login B), as install.py does for A. `add_account` on a login this page added is blocked until every other login lists it and that login's daemon has (re)started since (`ActiveEnterTimestamp`): "Adding accounts to Login C needs a restart of Login A/B while flat and signed in to see Login C." This page has no bare daemon restart; any change that restarts daemon A/B (or a restart by hand while flat and signed in) clears it.
- **Ids.** A letter is not reused while any `logins/<x>`, `<x>.removed-*`, `<x>.failed-*`, `manual-<x>.env*` or a login record (other than `undone`) exists.
- **Race.** install.py's created paths are removed on undo only when install.py got past its own checks and only paths that did not exist before it ran.

**Window placement.** `launch-browser.sh` gives login `<id>` the window class `tvbridge-login-<id>`. `desktop/hyprland-bridge-rules.lua` puts login A on workspace 3 and logins B, C, D, E on workspaces 6 to 9, off the trading screens and off workspace 5 (Advanced). Switch to that workspace to sign in.
- Any failure undoes everything in order (units stopped/disabled, drop-ins and token file removed, daemon-reload, install.py's files removed, the new token lines dropped, registry and login A config restored from the backup) and, if the panel was restarted, restarts it again. An incomplete undo is `needs_person`. Token values are never printed, diffed, logged or copied into backups.

## Export (`GET /api/export`)
Read only. Needs `X-Admin-Token` like every `/api/*` call. Writes nothing except one audit entry (`type: "export"`, `status: "done"`, summary with the file count, name and sha256 prefix). Runs under the engine lock for the second it takes, so it never captures a change half written.

Archive `bridge-setup-<UTC>/`:
| Folder | Contents |
|---|---|
| `admin/` | `webhooks.json` (every `stream_id` redacted), login records `logins/<id>.json`, change records `changes/*.json`, `audit.jsonl`, `profile.json` (the profile in use) |
| `tvbridge/` | `logins.json`, every login's `config.json`, `firing.json`, `mirror.json`, every env file (`tvbridge.env`, `logins/*.env`, `logins/<id>/*.env`) with **every value replaced by `<redacted>`**, names and comments kept |
| `soup/` | `quantity-policy.json`, and `contracts/<strategy>/settings_contract.json` for each profile strategy's `contract_ref` |
| `units/` | systemd user units and drop-ins named `tvbridge-*`, plus the profile's optional `soup_service.unit` and its instances (from `login_setup.unit_dir`, or `~/.config/systemd/user` in live mode; a sandbox without `login_setup` exports none) |
| `tunnel/` | `config.yml` with the `tunnel:` id and `credentials-file:` redacted |
| `MANIFEST.json` | `files: [{path, source, kind, redacted, bytes, sha256}]` (sha256 of the exported bytes), `bridge_release: {current, name, version}`, `versions` (admin build, Python, strategies), `created_at(_utc)`, `mode`, `redaction`, `left_out` |

Redaction: env values always; JSON strings under secret-looking keys (token, secret, password, api key, bearer, credential, auth; `*_env` names are kept) and every `stream_id`; strong stream IDs are also scrubbed from free text (summaries); unit `Environment=` values whose name looks secret or whose value looks like a token (24+ characters of `[A-Za-z0-9_+=-]`, not a path). Left out on purpose: admin backups, Soup `pairing.json` (holds Main's stream ID), browser profiles, journals, databases, cloudflared credentials. A test scans the whole archive for every token value of the sandbox (read, order, manual, Soup drop-in) and finds none.
