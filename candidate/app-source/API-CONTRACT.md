# Engine catalog and bridge setup API contract

The app shows the packaged engine catalog and sets up the Tradovate bridge for the user. It does not send orders. The existing onboarding, access-request and account APIs keep their present meanings.

## Catalog (read)

`GET /api/state` includes `engineWorkspace`:

```ts
type Engine = { id: string; strategyId: string; label: string; version: string|null; description: string; customerAvailability?: 'available'|'coming_soon'; packageStatus: 'not_packaged'|'source_packaged' };
type Module = { id: string; label: string; version: string; description: string; scope: 'engine'|'system'; runtimeSupported?: boolean };
type EngineWorkspace = { schema: 'decrypt.engine-workspace.v1'; setupOnly: true; catalog: {schema: 'decrypt.engine-module-catalog.v1'; engines: Engine[]; modules: Module[]; sourcePairs: unknown[]} };
```

Catalog objects may include extra fields (source pins the backend validates against the included code). There are no saved drafts: `POST /api/engine-workspace/actions` is gone and answers `404 not_found`. Installing an engine is `POST /api/trading/install`; Size Lock is `POST /api/trading/configure-size-lock`.

## Tradovate bridge (write, Trading service)

All four use the existing session cookie, same-origin boundary and `X-CSRF-Token`, and need `--enable-trading`. Each returns the trading status plus one extra key. They run the bridge module's own `setup.py` with fixed arguments (no shell) and never return a token.

| Route | Body | Extra key | What it does |
|---|---|---|---|
| `POST /api/trading/bridge-setup` | `{accountName, accountNumber, contract}` | `bridgeSetup: {created, paused: true, firingEngineFound}` | `setup.py init`: login A for this account, entries paused. Idempotent; refuses another account (`409 bridge_setup_refused`). |
| `POST /api/trading/bridge-enable` | `{}` | `bridgeUnits: {enabled: string[], paused}` | `setup.py enable`. Linux only (`409 bridge_linux_only`). |
| `POST /api/trading/bridge-disable` | `{}` | `bridgeUnits: {disabled: string[]}` | `setup.py disable`. Keeps every file; never cancels orders. |
| `POST /api/trading/bridge-login` | `{}` | `bridgeLogin` (below) | Read only: the daemon's `GET /login` with its read token. |

```ts
type BridgeLogin = { configured: boolean; account: {name: string; number: string; contract: string|null}|null; daemon: 'reachable'|'unreachable'; signIn: 'connected'|'logged_out'|'needs_human'|'unknown'; detail: string; accounts: {id: number; name: string; configured: boolean}[]; nameMatch: boolean|null; numberMatch: boolean|null; guard: {state: string|null; reason: string|null}|null };
```

Validation: `accountName` 1–128 printable characters, no placeholder, no leading dash; `accountNumber` digits (`[1-9][0-9]{0,14}`); `contract` a dated MNQ contract (`MNQ[HMUZ]20YY`). Errors: `400 invalid_fields | invalid_text | invalid_account_number | invalid_contract`, `503 bridge_module_missing | bridge_setup_failed`.
