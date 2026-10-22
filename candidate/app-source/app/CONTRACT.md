> This is an engineering contract and changelog for contributors
> working on the app backend/API, not end-user documentation. See `README.md`,
> `RELEASE-NOTES.md` and `docs/GETTING-STARTED.md` for the public-facing
> description of Decrypt OS.

# Current candidate integration — beta4

This section supersedes older absence claims below. It records source functionality, not native/provider acceptance or permission for funded trading.

- `--enable-trading` opens the customer execution service only alongside Guardian. All mutation routes retain the app's loopback Host/Origin/session/CSRF protections. They accept reviewed lifecycle/configuration fields, never arbitrary code, order payloads or user-declared broker fills.
- `POST /api/trading/status {}` reports actual installed runtime states, hash-verified package catalog, native/feed freshness, entry blockers, reviewed calendar status and health receipts. A draft, registration, accepted signal or fresh page is not a trading-readiness assertion.
- The distinct Soup, FVG/IFVG and Silver Bullet adapters preserve their own versioned policies. `publicationVerified` and `providerCommissioned` remain false for this development candidate. Refined adapters and protected-child native evidence are still under review; no generic substitute is authorized.
- The dedicated market browser uses port 9567 and its own profile. Guardian's TradingView sign-in recovery reuses this same browser's login tab rather than a separate browser. Native post-start callbacks and exact dated-contract chart identity are required. A page load or screenshot cannot serve as a quote.
- Customer alert intake uses a private instance-specific token through the customer's own HTTPS origin and loopback8812 receiver. Authentication, bounded payload validation, durable identity/deduplication and restart holds apply. No shared owner relay or arbitrary destination is inherited.
- Trades remain engine-owned. Guardian provides identity-bound observations through one qualified adapter, not a second order writer. Entry-only calendar holds preserve existing-trade management. The separate offline Guardian Observer library and draft-only module assignments do not gain operational authority.
- Saved route and ingress secrets are encrypted under separate vault domains. Ordinary status excludes their values. A private alert address is returned only by explicit local creation/retrieval while unlocked and held; it is never logged or persisted in browser storage.
- Optional desktop health notifications persist transitions and bounded attempts. Notification-daemon acceptance is not user-read confirmation; uncertain delivery is not blindly retried. No self-repair action can gain order authority.
- Encrypted recovery and signed code lifecycle have separate internal quiescence/native-flat requirements. A restore creates a durable hold; the candidate does not yet reopen restored trading automatically. Code rollback cannot rewind ledgers.
- The assistant's diagnostic permissions remain read-only and independent. New runtime controls do not become model tools. Customer provider and native acceptance still require recorded real-environment tests.

## Earlier contracts (historical; overridden above where stated)

# Decrypt OS integration contract

## Engine catalog and bridge setup

GET `/api/state` includes `engineWorkspace`: the packaged engine and module catalog, read only. The earlier draft/module-planning route (`POST /api/engine-workspace/actions`) is removed. Installing an engine is the trading service. The Tradovate bridge is set up from the app through `POST /api/trading/bridge-setup|bridge-enable|bridge-disable|bridge-login` (see `API-CONTRACT.md`): the module's own `setup.py` with fixed arguments, entries paused, no token returned.

Engines and trading modules are separate from AI connections and from strategy-access approval. Historical source identity does not establish current TradingView publication compatibility.

The separately versioned Guardian Observer is a pure offline snapshot comparison library. It never sends an order or rewrites engine/broker state. Snapshot acquisition and a commissioned runtime adapter are not included. Missing or untrusted evidence must remain unknown.

The sections below record the earlier app features and their own limitations. Historical fixture descriptions do not override the current engine/module distinction.

## Original vertical slice
Bot-only Decrypt OS. All data fixture/lab until supported credentials and paper commissioning are implemented. Never attach production browser ports, keys or state.

UI: React + TypeScript + Vite in ui/. Backend: Python standard library HTTP/SQLite in backend/. Default local server bind127.0.0.1 port8810; UI dev8811 proxies /api to8810. No new systemd service or public port. All app state via explicit --state-dir under lab/. No credentials in this slice.

GET /api/state returns {schema:'decrypt.os.lab.v1',mode:'lab',onboarding:{step:string,completed:string[]},profile:{assistantName:string},connections:{ai:string,tradingview:string,tradovate:string},usage:{ai:{status:'unavailable',used:null,limit:null},account:{status:'not_connected',balance:null,currency:null}},bots:[{id:'soup',name:'ICT - Turtle Soup Shift',version:'V1.1',label:'ICT - Turtle Soup Shift (V1.1)',technicalRevision:'r4',status:'not_connected'}],strategyAccess:{...},activity:[]} (see Strategy access below).
POST /api/onboarding {step:string,assistantName?:string,tradingPreferences?:boolean,complete?:boolean} saves validated progression only. The default path is welcome,assistant,connections,review. Explicit tradingPreferences:true adds bot before connections and access before review. No access request is required to finish. complete:true is accepted only at the saved review checkpoint and means preferences complete, not verified accounts or release readiness. No input can declare provider verification, tradingready or enable orders. Name bound80chars. GET state keeps schema decrypt.os.lab.v1 and adds onboarding.version:2, tradingPreferences:boolean and preferencesComplete:boolean. Its completed list uses only the selected path and includes review only after explicit completion.

The version2 app requires a matching version2 local service. Legacy SQLite stores migrate transactionally and idempotently: bot resumes at connections; access resumes at review. Prior required screens do not establish opt-in, so tradingPreferences defaults false and preferencesComplete defaults false. Names and activity remain intact. Opting out later also retains history. Editing saved setup choices clears completion until reviewed again. Provider/vault state is neither inspected nor changed by this migration.
All mutations require exact allowed Origin or same-origin token. Backend supplies per-session CSRF token via GET /api/session; no broad CORS. Loopback Host validation. UI stores token in memory only. Error {error:{code,message}}. Provider/API keys not accepted by any endpoint. Trading endpoint absent, not simulated enablement. Fixtures clearly labeled.

Display names and versions come from `app/strategy-catalog.json`. Stable strategyId `soup` and technical revision `r4` remain unchanged. Turtle Soup is an invite-only TradingView strategy that is being published (`invite_only_publishing`); it is open to access requests and has no publication URL or ID yet. The fvg-ifvg entry identifies the published invite-only script Determining Order Flow Engine [FVG+IFVG], publication BhpDI6NS. Publisher attribution is optional catalog metadata; the public image does not configure a publisher account name. Its published version is not established. No engine is installed or commissioned by a catalog selection or grant.


## Claude assistant harness (beta4)

The assistant is Claude only and runs on the isolated customer OpenClaw runtime. The earlier in-app API-key assistant (`/api/assistant/*`, `--enable-assistant`) is removed; an Anthropic API key now goes through the same native sign-in as a Claude subscription.

`--enable-harness` enables fixed `POST /api/harness/{status,start,login,console,ict,stop,verify,storage_setup,storage_unlock}` routes. Each accepts only `{}` behind the existing session/CSRF/Host/Origin checks. No token, key, code, URL or command reaches HTTP. Sign-in, the product assistant (`console`) and the ICT assistant (`ict`) open as native terminals on workspace 4.

- `login`: the terminal asks for one method: a Claude subscription setup-token (made with `claude setup-token`) or an Anthropic API key. Both are stored by OpenClaw in the customer profile `anthropic:customer`.
- Status values: `needs_login` = Gateway ready, no usable Claude profile; `ready` = profile usable, response unverified; `connected` = a fixed test response passed. Status fails closed when stale.
- Models: `anthropic/claude-opus-5-5`, fallback `anthropic/claude-sonnet-5`. Heartbeat and compaction use `anthropic/claude-haiku-4-5`; heartbeat is off by default and set to `isolatedSession: true`.
- Tools: the product helper (`main`) has none. The ICT agent (`ict`) has `session_status` (time) and `web_fetch` (public pages) only.
- Stop in session-only mode discards sign-in and history. With encrypted storage, stop locks them.

`POST /api/system/scope {}` returns a redacted source-inventory/local-setup snapshot. It is not a live service probe or permission grant.

## Encrypted storage actions (beta4)
The opt-in fixed harness actions also include storage_setup and storage_unlock; both accept only {} behind the same cookie/CSRF/Host/Origin checks. Passphrases are collected only in native masked terminals in workspace 4. Status adds credentialPersistence=session_only|encrypted and storage={configured,unlocked,error,busy,operation}, where operation is setup|unlock|null. Busy lifecycle controls are serialized; status remains readable. An encrypted volume is not a verified provider response.

In encrypted mode, stop means stop the customer service and lock its stored sign-in/history without deleting them. Session-only stop retains its prior discard behavior. Configured-but-locked storage forbids temporary fallback. No provider password, setup-token, API key, arbitrary path, command or passphrase is accepted or returned by the new HTTP routes. No trading or maintenance authority is added. Native setup verifies a customer-only migration and a stop/lock/unlock restore before accepting a model response. See app/harness/README.md for storage boundaries and unreleased feature flags.

## Strategy access (beta4)

Decrypt OS needs no Decrypt Labs registration. Engines install on any system. The only gated part is the strategies: Decrypt Labs grants its invite-only TradingView scripts to a TradingView username.

GET /api/state includes strategyAccess: {tradingViewUsername, requestConfigured, publicSite, latest}. latest is null or {tradingViewUsername, contact, status:'sent'|'not_sent'|'sending', reason, createdAt, sentAt}.

POST /api/strategy-access/request {tradingViewUsername, contact?} sends exactly that JSON (contact only when typed) to requestUrl from app/strategy-access.json, or from ~/.config/decrypt-os/strategy-access.json when present. No machine ID, account data or other field is sent. requestUrl must be plain https; an empty value turns sending off. Local limits: the same username once per 10 minutes after a successful send, 5 attempts per hour. A failed or disabled send stores status not_sent with a reason code, and the app shows the username with a copy button and the publicSite contact route. The setup step's tradingViewUsername is saved locally only.

Sending a username is not a grant. The app does not check TradingView access; the user confirms the strategy under Indicators → Invite-only scripts and creates the alert. Earlier registration tables and settings are removed on upgrade; the saved username is kept.

## Strategy catalog v2 and per-strategy access requests (beta4)

`app/strategy-catalog.json` is schema `decrypt.strategy-catalog.v3` and lists all seven tracked strategies: `soup` (engine live; test status, open to requests), `fvg-ifvg` (engine on hold), `lumi-rth-gap` (new; engine in development, no TradingView page yet), `ote-refined-large`/`ote-refined-small` (ids unchanged on purpose; display names now "ICT - OTE Refined L"/"S"; engine in development, unverified), `ote-silver-bullet` (parked), `order-blocks-decoded` (new; in development, no payload or link yet). Every strategy carries `tradingviewUrl` (the public script page; empty string when none exists) and `listed` (shown in the catalog; default true; a strategy can be hidden later by setting this false, with no schema change) and `engineStatus` (`live`|`hold`|`in_development`|`parked`). A strategy with an empty `tradingviewUrl` has no request button anywhere in the UI (Lumi, Order Blocks Decoded) since there is nothing to send people to yet. Every strategy also carries `bridge` (v3): `adapterId` (the bridge adapter that runs it, also `runtimePairId`: `soup-pine-authority-r4` for Soup, a `*-bridge-v2` adapter for every other strategy), `alertFormat`, `contractRoot`, `chart` and `alertSetup` (the TradingView alert setup notes). The engine dialog shows these notes from the catalog; the UI holds no strategy-specific setup text of its own.

The strategy-access form lets the user pick which of the requestable strategies (catalog-`listed`, `accessRequestAvailable`, non-empty `tradingviewUrl`) they want, as a plain checkbox group, alongside the existing username/contact fields. `POST /api/strategy-access/request` now also accepts an optional `strategies: string[]` (catalog ids only, deduped, capped, unknown id is a 400). The picked list is always recorded locally and returned in `latest.strategies`, so the UI can tell the user which strategies the request covers — but it is only put on the outgoing POST to `requestUrl` when `app/strategy-access.json`'s `strategyFieldSupported` is `true` (default `false`). The sign-up service currently accepts only `tradingViewUsername`/`contact`, so while the flag is `false` the wire body stays exactly that legacy shape and the app shows the picked strategies back to the user instead (and, for a sent request, a manual copy block listing them). Setting `strategyFieldSupported` to `true` is the only step needed here once a matching server-side update accepts the new field. `GET /api/state`'s `strategyAccess` also gains `strategyFieldSupported` and `requestableStrategies` (the current catalog-derived id list).

## Guardian recovery addition (beta4)

Guardian supplies customer-owned provider login recovery for Tradovate and TradingView. It does not install an execution engine, publish engine confirmations or place, cancel, flatten or replay orders. The direct Tradovate bridge is the only execution path.

Passwords belong only in the explicit local Guardian form. Separate provider credentials live in an authenticated encrypted vault, not the onboarding database, assistant context or browser storage. Unlock is required after backend restart. The browser profile uses Chromium storage independently. Readiness requires fresh, complete, identity-bound native evidence, not a stored login or an HTTP login acceptance. MFA/CAPTCHA need a person. Native customer provider acceptance is not implied by local tests.
