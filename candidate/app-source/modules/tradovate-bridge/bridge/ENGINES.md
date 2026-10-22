# Bridge engines: the shared adapter platform

This is the shared layer that lets strategies other than Soup trade through the Tradovate bridge. Soup is unchanged and stays the reference.

The strategy owns every trading rule. An adapter sends exactly what its strategy's alert says. The bridge and runtime add safety checks only:

- format and identity;
- freshness (an entry alert at most 120 s old);
- size and position checks: every quantity is a positive whole number, exits and take-profits never exceed the open position, and fills must explain the position;
- anti-hedge, the kill switch and broker confirmation.

There are no filters, time windows or trade logic, and no contract cap: the strategy alert sets the size.

## Shipped strategies

Every strategy runs through one adapter. Soup keeps its own format; every other strategy sends `decrypt-bridge-v2` (`PAYLOAD-V2.md`). The bridge refuses any other alert format or adapter id.

| Strategy | Adapter id | Alert format | Contract |
|---|---|---|---|
| ICT - Turtle Soup Shift (r4) | `soup-pine-authority-r4` (reference) | `decrypt-soup-entry-v3`, `decrypt-soup-entry-v4`, `decrypt-soup-management-v3` | MNQ |
| FVG/IFVG | `fvg-r5b-bridge-v2` | `decrypt-bridge-v2` | MNQ |
| Lumi RTH Gap | `lumi-gap-bridge-v2` (`LUMI.md`). Requires the 1-second MES feed (see below). | `decrypt-bridge-v2` | MES |
| OTE Refined Large | `ote-refined-large-bridge-v2` | `decrypt-bridge-v2` | MNQ |
| OTE Refined Small | `ote-refined-small-bridge-v2` | `decrypt-bridge-v2` | MNQ |
| OTE Active (Silver Bullet) | `ote-active-bridge-v2` | `decrypt-bridge-v2` | MNQ |
| Order Blocks Decoded | `order-blocks-bridge-v2` | `decrypt-bridge-v2` | MNQ |

Every adapter but Soup is off until its binding enables it. The TradingView alert setup of each strategy is in `app/strategy-catalog.json` (`bridge.alertSetup`); the app shows it in the engine dialog.

## What changed

| Part | File | What it does |
|---|---|---|
| Adapter registry | `bridge/tvbridge/adapters.py` (byte-identical copy: `app/trading-runtime/decrypt_trading/bridge_adapters.py`) | An explicit list of adapters. Each one declares an id, its alert schemas, the contract roots it trades (MNQ, MES), the order kinds it uses, and how many take-profits may rest at once (default 1 = TP1 only). Soup is registered as the reference. An unknown id, schema, kind or root is refused. |
| Contract table | same file, `CONTRACTS` | MNQ and MES, tick 0.25, quarterly months H M U Z. One dated contract per binding, such as `MESZ2021`. Any other root is refused at start. |
| Bridge orders | `bridge/tvbridge/executor.py` | Adapter commands become the same orders Soup sends (`entry`, `bracket_entry`, `tp1_limit`, `exit_market`, `cancel`, `modify_stop`, `flatten_all`). Every existing guard applies: positive whole-number size, kill switch (fails closed), anti-hedge in this login and across logins, bracket reference-price check, flat before entry, one entry per trade, no exit through flat, unknown outcome blocks entries, journal dedupe across restarts. |
| Start checks | `daemon.py`, `setup.py` | The daemon checks the contract and the `adapters` config before it touches the browser. `setup.py init --adapter <id>` writes an enabled adapter binding. |
| Followers | `followers.py` | A follower takes the main account's adapter and the alert's size. Follower firing (`tvbridge-firing`) still mirrors Soup only. |
| Runtime route | `decrypt_trading/bridge_route.py`, `runtime.py`, `source.py`, `customer_contract.py` | A registered and pinned adapter uses Soup's bridge route. The bridge config must enable the adapter, and `/state` must name it in `adapter_ids`. Entries pass the same gate: fresh flat bridge state, a quote at most 2 s old, and no send more than 120 s after the alert. A command that fails the format check is never sent. |
| MES | `customer_contract.py`, `backend/trading_calendar.py` | A pair may bind MES only when its adapter declares MES (Lumi). Soup and the other strategies trade MNQ. The calendar gate takes the current MES contract from the reviewed MNQ roll dates, which MES shares. |
| Price watching | existing code, now tested per contract | The bridge keeps the last fill price of the binding's own contract (`last-market-price.json`, keyed by symbol). The runtime's native chart feed reads `CME_MINI:<binding contract>`. Prices only feed safety checks. They never open a trade or send an order. |

## Bridge config (per binding)

```json
"ticker": "MESZ2021", "tick_size": 0.25,
"adapters": {"lumi-gap-bridge-v2": {"enabled": true}},
"market_feed": {"enabled": true},
"bracket_orders_enabled": true, "flatten_all_enabled": true, "snapshot_fill_prices": true
```

Lumi needs `market_feed` (the 1-second MES feed); other strategies leave it out.

- No `adapters` key means Soup only, exactly as before.
- `max_contracts` is retired. Older configs may keep a positive whole number; it is ignored. Any other value stops the daemon at start.
- At most one adapter is enabled per binding: one strategy per account and contract.
- With a non-Soup adapter enabled, Soup payloads are refused.
- `bracket_orders_enabled` and `flatten_all_enabled` still gate the extended kinds.

## Adapter command format (POST /order/<token>)

```json
{"ticker": "MESZ2021", "action": "buy", "orderType": "market", "quantity": 3,
 "extras": {"adapter_id": "<id>", "adapter_schema": "decrypt-bridge-v2",
            "command_id": "<unique, no #>", "trade_id": "<id ending :long or :short>",
            "signal_time": 1632147200000,
            "bridge_kind": "bracket_entry", "reference_price": 6800.0, "stop_price": 6790.0,
            "tp1_price": 6815.0, "tp1_quantity": 1}}
```

Each kind has an exact field set, listed in `ADAPTER_FIELDS` in `adapters.py`. A missing or extra key is refused.

- `signal_time` is the alert time in Unix ms. It is required on entries. The bridge refuses an entry more than 120 s old or more than 5 s in the future.
- Exits need an explicit quantity and `cancel: false`.
- Prices must be on the 0.25 tick.

## How to add a strategy

Follow "Adding a strategy" in `PAYLOAD-V2.md`. In short:

1. **Strategy module.** Add `app/trading-runtime/decrypt_trading/bridge_v2_<name>.py`: its `bridge_v2.Strategy` declaration (strategy id, adapter id, extras schema) and the module API from `bridge_v2.module_api`. It adds no trade logic.
2. **Pin it.** One line in `bridge_v2_registry.STRATEGIES` (module file, its SHA-256, an opaque strategy pair id: the SHA-256 of the neutral label `decrypt-os/strategy/<adapter id>/v1`, never a hash of the TradingView script). `source.py` and `customer_contract.py` read it.
3. **Register the adapter.** One line in `V2_ADAPTERS` at the end of `bridge/tvbridge/adapters.py`, then copy the file byte for byte to `app/trading-runtime/decrypt_trading/bridge_adapters.py`. A test checks that the two files match.
4. **Ship it.** Add the module to `installer/clean_base.py` `TRADING_FILES` and `lab/build_source_bundle.py` `TRADING_SOURCE`. Add the strategy's `bridge` block (adapter id, chart, alert setup notes) to `app/strategy-catalog.json` and its engine and source pair to `app/engine-module-catalog.json`.
5. **Set up a binding** with `setup.py init ... --ticker <dated contract> --adapter <id>`. Entries start paused.
6. **Test.** Copy `tests/test_trading_bridge_v2.py` (runtime side, fake bridge) and `bridge/tests/test_adapters_bridge_v2.py` (bridge side, browser-free). Use synthetic ids only.

Broker evidence: with `bracket_orders_enabled`, the bridge's snapshots also list bracket legs (`<command id>#stop`, `#tp1`, with `role`). An engine that sends `bracket_entry` must accept these rows. The opt-in `snapshot_fill_prices: true` adds `avg_fill_price` per order, for engines that size protection from the real fill.

## How to test

```sh
cd candidate/app-source/modules/tradovate-bridge
(cd bridge && python3 -m pytest -q tests/test_adapters_platform.py tests/test_adapters_bridge_v2.py)
python3 -m pytest -q tests/test_setup_adapters.py
cd ../.. && python3 -m pytest -q tests/test_trading_bridge_adapters.py tests/test_trading_calendar_mes.py tests/test_bridge_v2_contract.py tests/test_trading_bridge_v2*.py
```

## Known limits

- There is no contract cap. Each strategy trades the size its alert sends, including the larger account profiles.
- Follower firing is Soup-only. A non-Soup binding trades its own account only.
- Up to 3 take-profits may rest at once, as the alert's `targets[]` declare them (profile `decrypt-bridge-v2-targets`). Together they cannot exceed the position.
- No 1-minute or 2-minute native bar series was added. The native feed still gives 5 s and 30 s bars.
- The app's bridge setup form stays Soup and MNQ. Every other strategy's binding is set up with `setup.py --adapter`.

## decrypt-bridge-v2: the shared format for every non-Soup strategy

New strategy work uses one alert format, `decrypt-bridge-v2`, and one receiver. The format, every field
and the steps to add a strategy are in `bridge/PAYLOAD-V2.md`. In short:

- Validator: `decrypt_trading/bridge_v2_contract.py`. Receiver and executor: `decrypt_trading/bridge_v2.py`.
- A strategy adds its own module `decrypt_trading/bridge_v2_<name>.py` (with its `extras` schema), one line in
  `bridge_v2_registry.STRATEGIES` and one line in `V2_ADAPTERS` in this folder's `tvbridge/adapters.py`.
- The alert carries the stop, targets, break-even, runner, end of day, daily loss lock, touch entry and the
  management events. A feature the alert switches off does nothing.

## 1-second MES feed (required by Lumi RTH Gap)

Lumi's touch entries, break-even and price-hit exits run on a 1-second MES price feed. The feed comes from the
broker's own market data for the binding's dated contract (for example `MESZ2021`, Tradovate `MESZ1`).
Lumi requires it. If the feed is not live and fresh, Lumi does not start and takes no entry.

| Part | File | What it does |
|---|---|---|
| Page side | `bridge/page/mdfeed.js` | Opens its own market-data connection with the trader page's market-data token. The token stays in the page. It subscribes to quotes for one contract only and reports each quote's contract id, time and last trade price. It never places, changes or cancels an order. It stops when the order bridge's page script stops. |
| Bridge side | `bridge/tvbridge/marketfeed.py` | Builds 1-second bars (open, high, low, close of the trade prices) and decides whether the feed is fresh. `GET /feed/<TVBRIDGE_TOKEN>` returns the status and the last 10 bars. Bars are returned only while the feed is fresh. |
| Runtime side | `decrypt_trading/bridge_feed.py` | Reads `GET /feed` every 0.25 s and queues each new bar as one quote, with event id `bridge1s:<contract>:<second close ms>`. When the feed is not fresh, it queues nothing and withdraws the market. |
| Lumi engine | `decrypt_trading/bridge_v2_lumi.py` | Accepts only quotes from this feed. It refuses an entry alert, market or touch, unless the feed is fresh. It does not trigger an armed touch while the feed is not fresh, but a stop touch still disarms it. With an open trade and a stale feed, it records one alert. The resting broker stop keeps protecting the trade. |

Freshness rules (fail closed):

- The newest quote is at most 2 s old, on this machine's clock. Its broker time must also be at most 3 s old
  (2 s plus 1 s of tolerance between the broker's clock and this one). Quotes that arrive late, for example a
  backlog, do not count. A broker time more than 1 s ahead is dropped.
- A second counts only with a trade price seen in it or in the 2 s before its close. An older price is never
  carried forward. With no trade for 2 s, the feed is not fresh.
- Every second has at least one quote. A second without one is a gap. After a gap, a start, a restart or a
  reconnect, the feed needs 3 consecutive live seconds before it is fresh again.
- Own contract only: a quote for any other contract id stops the feed until the daemon restarts. Quotes that
  arrive before the contract id is pinned are ignored. Off-tick prices and quote times more than 5 s ahead are
  dropped.
- Nothing is kept across a restart. The runtime queues only bars at most 2 s old. The Lumi engine clears its last
  price at start.
- Prices never open a trade. The feed only reports prices, and the alert's own plan decides.

Bridge config: `"market_feed": {"enabled": true}`. An optional `md_url_pattern` (default
`^wss://md(-demo)?\.tradovateapi\.com/v1/websocket$`) names the market-data endpoint. `setup.py init --adapter
lumi-gap-bridge-v2` writes the key. With `lumi-gap-bridge-v2` enabled and no feed, the daemon refuses to start. The
runtime does not open Lumi's route unless `GET /feed` shows a fresh feed for the binding's contract. The refusal
says why in plain language. The engine's refusal codes are `refused:mes_1s_feed_no_data`, `_stale` and
`_warming_up`.

Known limit: with the daemon's `--no-reload` option the page is not reloaded. The app's market-data login has then
already happened, so the feed learns the token only at the next token renewal. Until then the feed is not
live and Lumi refuses entries (fail closed). The normal start reloads the page.

Not yet proven: the market-data wire (`md/subscribeQuote` and its quote events) follows Tradovate's
documentation. It has not been checked against a Simulation login. Check it on a test VM before a paper run.
The Bridge window does not show the feed's message yet. The runtime writes it to `status.json` as `price_feed`.

Tests: `bridge/tests/test_marketfeed.py` (feed, `GET /feed`, daemon refusal and the page script under
node) and `app-source/tests/test_bridge_feed_lumi.py` (fresh, stale, gap, wrong contract, restart and start
refusal, with a fake feed).
