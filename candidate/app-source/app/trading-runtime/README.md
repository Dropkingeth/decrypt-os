# Decrypt OS trading runtime

The engines run inside the Decrypt app's backend. Soup sends its orders to the Tradovate bridge
module on this machine through the Soup engine's bridge transport (`--transport bridge`).
No third-party relay or webhook route exists. Engines install held and start held after every
open; new entries need an explicit release.

| Engine | Package | Orders |
| --- | --- | --- |
| Soup r4 | Vendored Soup core, storage, message mapping and settings (`vendor/soup_r4`); alerts `decrypt-soup-entry-v3`/`-v4`, `decrypt-soup-management-v3` | Tradovate bridge `/order` |
| FVG/IFVG, Lumi RTH Gap, OTE Refined Large and Small, OTE Active, Order Blocks Decoded | One shared engine (`bridge_v2.py`) reading the `decrypt-bridge-v2` alert (`modules/tradovate-bridge/bridge/PAYLOAD-V2.md`); one small module per strategy (`bridge_v2_<name>.py`), pinned in `bridge_v2_registry.py` | Tradovate bridge `/order` (adapter commands), off until the binding enables the adapter |

Older alert formats are refused.

Strategy quantities and prices are never replaced. The only checks added outside the strategy
are safety checks: format, account identity, freshness, position conservation and broker state.
There is no contract cap: the strategy alert sets the size.

## Integration API

Add `app/trading-runtime` to the import path and import `decrypt_trading`. Importing starts no
process and opens no connection.

```python
from decrypt_trading import RuntimeManager
from decrypt_trading.bridge_route import open_route

manager = RuntimeManager(private_state / 'trading')
manager.install(binding)                 # immutable identities; no credentials
transport = open_route(binding)          # Soup only; raises BindingError(reason)
runtime = manager.open(binding['instanceId'], transport=transport)
runtime.start()                          # worker; still holds new entries
runtime.set_controls(enabled=True, locked=False)   # explicit release
runtime.hold()                           # hold new entries; management continues
runtime.close()
```

If the bridge is not set up yet, open the engine without a transport and attach it later with
`runtime.attach_bridge(open_route(binding))`. The backend's trading service does this every few
seconds.

One account per instance: Soup's cancel and flatten messages act on the whole account. Accounts
cannot be reused by another active instance or rebound to a used ledger.

## The bridge route (`decrypt_trading/bridge_route.py`)

The Soup engine's bridge transport, as a route inside the Decrypt app's backend.

- **Account check before every send.** The bridge's `GET /state/<token>` must report the binding's
  Tradovate account id, the SHA-256 of its account name, the Soup account (`accountId`), the
  contract and `publish_mode: adapter-db`. A mismatch refuses the send; nothing is sent.
- **First entry.** The entry waits until a fresh bridge state (at most 3 s old, queried for this
  trade) shows the account synced, healthy, flat, with nothing working, unseen or unresolved,
  entries not paused at the bridge and no entry yet for the trade. An entry still waiting 120 s
  after its decision time is dropped, never sent late.
- **Orders.** Soup's exact `message()` payload goes to `POST /order/<TVBRIDGE_TOKEN>`.
  `bridge_transport.py` is the bridge module's `soup_transport.py`, copied unchanged (a test keeps
  them equal).
- **Other strategy adapters (beta4).** A registered and pinned adapter (`bridge_adapters.py`, a
  byte-identical copy of the bridge module's `tvbridge/adapters.py`; `source.register_bridge_engine`)
  uses the same route with the same gates. Its engine module builds the adapter command format. See
  `modules/tradovate-bridge/bridge/ENGINES.md`.
- **Broker events.** The bridge appends broker events to its adapter database
  (`tvbridge-state/events.sqlite`). The runtime reads it read-only and queues each event for the
  engine; the cursor lives in the engine database. Events older than 15 s are skipped, never
  replayed as fresh. A sequence gap stops reading and shows a fault.
- **Failed native exit.** If Soup cannot verify an exit-all, the runtime holds entries, writes
  `soup/ENTRIES-BLOCKED-NATIVE-EXIT` and pauses the bridge. Release both by hand after Tradovate
  shows flat.

The token comes from the bridge's private `tvbridge.env` (mode 0600, owned by this user).

## Data

All runtime data lives under `~/.local/share/decrypt-os` (`DECRYPT_OS_DATA`):

| Folder | Override | Contents |
| --- | --- | --- |
| `tvbridge/` | `TVBRIDGE_ROOT` | Bridge config, tokens, journals, firing order |
| `tvbridge-admin/` | | Bridge Advanced profile and state |
| `tvbridge-state/` | `TVBRIDGE_STATE` | Broker events (`events.sqlite`), login-guard state |
| `tvbridge-browser/` | | The bridge's Chromium profiles (saved Tradovate login) |
| `soup/` | `DECRYPT_SOUP_ROOT` | Files the bridge reads from Soup |

The runtime writes these files into `soup/`, with the file names the bridge module expects:
`broker-context.json` (the bridge continues Soup's broker sequence from it), `pairing.json`,
`quantity-policy.json`, `status.json` (Bridge window), `control.json` (shown in Bridge Advanced;
the engine does not read it back) and the link `current` to the engine folder. The firing order
reads `soup/current/engine.sqlite` read-only with the vendored Soup engine code.

The engine ledgers themselves stay in the backend state folder
(`~/.local/state/decrypt-os/trading/<instance>/engine.sqlite`), where backups and retirement
expect them.

## Immutable binding

`customer_contract.validate_binding` requires exactly these keys:

| Field | Contract |
| --- | --- |
| `schema`, `provider` | `decrypt.customer-tradovate-binding.v2`, `tradovate` |
| `instanceId`, `customerId`, `installationId` | Local identities |
| `pairId`, `engineSha256`, `strategySha256` | One supported engine/strategy pair |
| `accountId`, `nativeAccountId`, `nativeAccountName`, `accountMode` | The Tradovate account (the bridge setup uses the same number and name) |
| `loginGroupId`, `routeId`, `routeSha256` | Local route identity |
| `feedId`, `streamId`, `symbol`, `root`, `tickSize` | Feed, Pine stream and the exact dated contract (tick 0.25): MNQ, or MES for an adapter that declares it |
| `tp1LimitOffsetHandles` | Soup: explicit tick-aligned offset. decrypt-bridge-v2 strategies: zero (the alert carries its own targets) |
| `signalProducerId`, `feedProducerId`, `brokerProducerId` | Three separate authorities |

## Signal and feed adapter contract

After independently authenticating the correct producer and mapping its secret
route to this installation/instance, call:

```python
runtime.ingest('pine', raw_pine_bytes, producer_id=bound_signal_producer)
runtime.ingest('quote', raw_quote_bytes, producer_id=bound_feed_producer)
runtime.ingest('bar', raw_completed_bar_bytes, producer_id=bound_feed_producer)
```

The actual wire fields are the preserved Soup core contract. Ingest checks the
dated symbol and Pine stream, bounds JSON and rejects duplicate JSON keys. Each
input is durable before its receipt is returned. The worker consumes in inbox
sequence and dispatches immediately after each accepted strategy event. The
unchanged core validates exact settings, source revision, timestamps, intervals,
signal identity and payload-owned sizing. Do not translate another strategy into
this schema. Receiver authentication and customer native chart acquisition are implemented
below. Public DNS/TLS deployment remains unverified; no source fixture is a
trading-readiness grant.

The binding's feed identity must be established by the authenticated adapter;
changing a label in the browser does not prove an actual feed source. Quotes and
completed bars retain their source clocks. A fresh chart window is not feed proof.

## Limits and tests

This package has no live provider test. The decrypt-bridge-v2 strategies are tested offline only
(synthetic alerts, fake bridge). Real demo-account checks are a release step.

```sh
python3 -m pytest tests/test_trading_runtime*.py tests/test_trading_bridge_route.py
```

## Authenticated ingress

`SignalIngress(runtime, lease).accept(raw_bytes, presented_secret=token)` receives
the unchanged Pine message. `lease(instance_id, "pine")` yields a customer-generated
URL-safe token (43–128 characters) from the encrypted store. The payload's actual
trade/signal identity and exact schema are checked. Credential fields cannot be
appended to Pine bodies. Body digest, replay identity, producer sequence (where
present) and inbox insertion commit atomically. Identical retry returns the same
receipt; changed identity/body is rejected. A held entry at admission is consumed
permanently, not saved to enter later when controls are released.

`SignedIngress(runtime, signed_lease).accept(channel, raw_bytes, metadata=...,
signature=...)` supports customer-controlled authenticated relays. The lease is
selected internally from `(instance_id, channel)` and yields 32–128 key bytes;
keys are channel-separated. Metadata has exactly `schema` (`decrypt.ingress.v1`),
`instanceId`, `channel`, `feedId` (null for Pine), `eventId`, `sequence`, `sentAt`.
The HMAC-SHA256 input is the UTF-8 concatenation of `decrypt.ingress.v1\n`, canonical
sorted compact metadata JSON, `\n`, and raw-body SHA-256 hex. Signatures are lowercase
hex; server time must be within 15 seconds after `sentAt`. Quote/bar timestamps
remain their source clocks and have tighter native freshness bounds. TradingView
cannot compute this HMAC; its direct alerts use the token receiver instead.

`IngressListener(runtimes_callback, signal_lease, public_origin,
host="127.0.0.1", port=8812).start()/close()` provides a dedicated receiver,
separate from app control/CSRF routes. Its only accepted route is
`POST /signal/<instance>/<token>`. It requires the exact configured HTTPS public
Host and forwarded host/protocol from the loopback reverse proxy, a bounded
40-KiB JSON body, no browser Origin/Referer, no chunked transfer or repeated
security headers. Sixteen concurrent connections and three-second reads are
bounded. Neither success nor failure logs a URL, header, token or request body.

`Caddyfile.example` is a standalone customer-domain proxy template. Replace the
example domain consistently and arrange customer DNS/TLS/boot ownership before
use. The template discards **all default logs**, because upstream errors can
otherwise include secret request paths. Do not merge it into a proxy with access
logging or third-party request analytics. Rotate the token if a webhook URL is
exposed. No proxy, public listener, certificate or tunnel was installed or run.

## Native market acquisition

`NativeMarketFeed(runtime, owned_browser_transport, chart_url=..., target_id=...)`
ports the reviewed main-series callback and independent native-five-second candle
reader. The browser object must expose `targets()` and `evaluate(target_id, js)`
from the app-owned customer profile. It must not accept a browser endpoint from
request JSON or attach to another running installation.

Explicit `install()` selects exactly one already-open customer chart target; it
never opens, navigates, resizes or switches a page. `tick()` at 250–500 ms drains
native callback events, preserving browser callback timestamps (not exchange
trade timestamps). A main-series polling snapshot or initial historical batch
never becomes a fresh quote. Only a later native candle proves a completed bar;
missing rows never become synthesized candles. Delayed feed, replay, changed
chart/style/contract, callback overflow, changed completed candle or lost target
holds readiness. Native five-second candles come from the same chart API through
an independent silent session, never by reconnecting the primary visible chart.

The adapter stores a bounded durable market-event journal and forwards only
current inputs into the instance's deduplicated inbox. Inactive bars are retained
in the feed journal but not delivered as strategy events; active bars must match
the Pine instance's snapshotted interval. Reopening starts at the journal tail,
never replays old prices. `close()` stops only its own hook/session; a five-second
hook lease also stops orphaned acquisition. Methods serialize their own SQLite
access; install/tick/close can be called by the API's single owned worker.

Native chart APIs are private implementation surfaces. The actual shipped hook
is exercised in an isolated API fixture, and Python checks use native-shaped
fixtures with real SQLite. Customer browser login/entitlement/feed latency,
source publication and real order lifecycle remain release checks, not claims
established by these tests.


## Supported pairs

`source.package_catalog()` verifies installed code hashes and returns the engines the app installs
(Execution runtime): Soup r4 only, with its explicit TP1 offset. `source.BRIDGE_ENGINES` holds the
pinned `decrypt-bridge-v2` strategies (`bridge_v2_registry.STRATEGIES`); each is set up per binding with
the bridge's `setup.py init --adapter <id>`. All carry `publicationVerified:false` and
`providerCommissioned:false`.

`dispatch_guard` is an optional internal synchronous callback checked before
all outbound commands (for durable restore/maintenance hold). False or exception
holds entries and preserves known-unsent management without sending. Separate
`entry_guard` is checked at authenticated entry admission and immediately before
entry dispatch (for calendar/source qualification). It never disables existing
management. Neither callback is a browser/API assertion or may acquire the
service/vault lock. All credential access follows runtime-mutex → vault-lease
order, including authenticated input admission; a coordinated two-thread test
checks that this cannot deadlock with dispatch.

## Reversible execution-instance retirement

`RuntimeManager.retirement_precheck(runtime)` checks eligibility while normal
supervision is still running and entries are held. Pending inputs or unresolved
state refuse retirement before any worker is stopped. This check never drains
inputs, steps the engine or sends a command; any pending work must finish through
ordinary supervision. The service then stops its feed/bridge and closes the
runtime. `RuntimeManager.retire(runtime)` commits the archival registry operation,
not an order action, and rechecks the exact latest closed runtime generation.
A stale object from an earlier open is refused.

Never-dispatched idle instances can retire with `nativeFlatClaimed:false`.
Anything with a trade/command history needs a fresh flat report from the
Tradovate bridge (basis `closed_bridge_flat`): the bound account synced, healthy,
flat, with nothing working, unseen or unresolved; plus a closed engine and terminal historical commands. Ready/started/unknown or
unresolved accepted placements block retirement. Accepted/confirmed cancellation
is allowed only when it belongs to that same closed trade. The bridge report is
fetched again after file hashing and immediately before the registry transaction.

Existing active `instances` rows and unique account/route/endpoint constraints are
unchanged. Retirement atomically moves the immutable row to `retired_instances`
inside `instances.sqlite`, including its original created time, retirement basis,
accepted native snapshot/hash and exact preserved per-instance file hashes. The
registry also retains lifecycle events. No engine/market/history/receipt/inbox
bytes are rewritten or deleted by retirement. Owned SQLite close settles WAL
**before** preservation begins. On Darwin, when SQLite leaves sidecars after all
owned connections close, a bounded fixed SQLite CLI checkpoint/journal operation
settles that same private database under its writer lock. No sidecar is manually
deleted or renamed. Remaining WAL/SHM/journal files still cause refusal.

`manager.list()` / `get(id)` / `open(id)` remain active-only. An active instance whose engine is retired (an older release's
pre-v2 strategy format) is listed with `registryState:'retired_engine'` and the blocker
`retired_engine`; `get`/`open` refuse it, and `retire_retired_engine(id)` archives it from its
read-only ledger (no active trade or unresolved command, no broker claim) without loading the engine.
`list(include_retired=True)` adds explicit `registryState:'retired'` rows;
`get(id, include_retired=True)` retrieves the original binding for archival review.
A retired ID is never reused by `install`. The total of active and retained
instances is bounded to 32, not 32 active plus unlimited archives.

`manager.reactivate(id)` is explicit and reversible: verify exact archived file
hashes, current supported source pins and no conflicting active account/route/
endpoint, then transactionally restore the immutable active row. It does not open
or enable anything. A later deliberate open starts held, without reused native
proof; old authenticated Pine receipts remain consumed. A failed reactivation
leaves the archive intact. Restoring files preserves retired status and cannot
implicitly reactivate an instance.

Backups must include retired engine/market bytes and registry
records even though the default active list omits them. Archived SQLite files are
copied byte-for-byte, not vacuumed/re-serialized into different archive hashes.
