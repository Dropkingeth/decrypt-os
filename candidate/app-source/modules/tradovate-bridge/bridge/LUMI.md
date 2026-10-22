# Lumi RTH Gap adapter (`lumi-gap-bridge-v2`)

The bridge engine for the Lumi RTH Gap strategy. It reads the shared `decrypt-bridge-v2` alert (`PAYLOAD-V2.md`) and runs on the shared engine like every non-Soup strategy (`ENGINES.md`).

**Lumi requires the 1-second MES feed.** It is the bridge's own live MES prices, one bar per second, for the binding's dated contract. Turn it on with `"market_feed": {"enabled": true}` in the bridge config. `setup.py init --adapter lumi-gap-bridge-v2` writes it. If the feed is not live and fresh, Lumi does not start and takes no entry. Known limit: after a daemon start with `--no-reload`, the feed waits for the next token renewal, so Lumi refuses entries until then. See "1-second MES feed" in `ENGINES.md`.

The strategy owns every trading rule. Its alert carries the whole plan: size, stop, TP1 and TP2, runner, break-even, end of day, daily loss lock and touch entry. The adapter executes that plan and adds safety checks only: format, identity, freshness, size and position conservation (no contract cap), anti-hedge, the kill switch and broker confirmation.

## Where it lives

| Part | File |
|---|---|
| Strategy module (declaration and extras) | `app/trading-runtime/decrypt_trading/bridge_v2_lumi.py` |
| Shared engine and validator | `decrypt_trading/bridge_v2.py`, `decrypt_trading/bridge_v2_contract.py` |
| Pin | one line in `decrypt_trading/bridge_v2_registry.py` (`STRATEGIES`) |
| Bridge adapter (MES only, off by default) | one line in `V2_ADAPTERS`, `bridge/tvbridge/adapters.py` |
| Tests | `tests/test_trading_bridge_v2_lumi.py` (runtime, fake bridge), `bridge/tests/test_adapters_bridge_v2.py` (bridge side) |

## Alerts it accepts

`protocol: decrypt-bridge-v2`, `strategy: lumi-gap`, on one dated MES contract such as `MESZ2026`. Events: `entry`, `cancel_entry`, `modify_stop` and `flatten`. Anything else is refused.

Lumi's own `extras` on the entry: `setup` (the strategy's own short setup label, 1-12 of `A-Z a-z 0-9 _ -`), `chart_symbol` (must equal `symbol`, so an alert from a continuous chart such as `MES1!` is refused), `bar_seconds`, `decision_time`, `account_profile` and `risk_usd`. They are recorded with the trade; only `chart_symbol` is checked.

Touch entries are off unless the strategy's "Touch entries" setting is on. When on, the alert's `touch` block arms the entry and its own `valid_until` sets how long it stays armed.

## Setup

Bridge config of the binding (MES only):

```json
"ticker": "MESZ2026", "tick_size": 0.25,
"adapters": {"lumi-gap-bridge-v2": {"enabled": true}},
"bracket_orders_enabled": true, "flatten_all_enabled": true, "snapshot_fill_prices": true
```

`setup.py init ... --ticker MESZ2026 --adapter lumi-gap-bridge-v2` writes the binding. Entries start paused. Use a login that trades nothing else: the anti-hedge guard spans ES, MES, NQ and MNQ per login. The TradingView alert setup is in the strategy catalog (`app/strategy-catalog.json`, `lumi-rth-gap`), shown in the app's engine dialog.

## Known limits

- **Size.** There is no contract cap. The alert sets the size.
- **Followers.** Not built for non-Soup adapters. A Lumi binding trades its own account only.
- **Contract roll.** A roll is a new binding, and the strategy's contract input must change with it.
