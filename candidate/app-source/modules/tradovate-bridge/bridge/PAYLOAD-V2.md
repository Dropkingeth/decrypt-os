# decrypt-bridge-v2: the shared alert format

Every strategy except Turtle Soup sends its TradingView alerts in this one format. Soup keeps
`decrypt-soup-entry-v3` and `decrypt-soup-management-v3`.

The strategy owns every trading rule. Its entry alert carries the whole plan: size, stop, take-profits,
break-even, runner, end of day, daily loss lock and touch entry. The receiver does exactly what the alert
says. It adds safety checks only:

- strict format and identity;
- entry freshness (a touch entry uses its own `valid_until`);
- size and position conservation;
- broker confirmation, anti-hedge and the kill switch (in the bridge);
- fail closed on anything unknown.

There is no contract cap, filter, time window or trade logic of the receiver's own. A feature that the
alert switches off does nothing.

| Part | File |
|---|---|
| Validator | `app/trading-runtime/decrypt_trading/bridge_v2_contract.py` |
| Receiver and executor | `app/trading-runtime/decrypt_trading/bridge_v2.py` |
| Strategy registry | `app/trading-runtime/decrypt_trading/bridge_v2_registry.py` |
| Bridge adapter lines | `bridge/tvbridge/adapters.py`, `V2_ADAPTERS` (byte-identical runtime copy `decrypt_trading/bridge_adapters.py`) |
| Tests | `app-source/tests/test_bridge_v2_contract.py`, `app-source/tests/test_trading_bridge_v2.py`, `bridge/tests/test_adapters_bridge_v2.py` |

## General rules

- Strict JSON. One object, under 4,096 bytes in compact form. Duplicate keys, `NaN` and `Infinity` are refused.
- Every field listed for an event is required. Any other key is refused, except inside `extras`.
- Prices are numbers on the contract tick (0.25 for MNQ and MES). A price of `0` means "none" where allowed.
- Quantities are whole numbers of 1 or more. There is no upper limit.
- Times are Unix milliseconds, UTC, as integers.
- The symbol is one dated contract, for example `MNQZ2021` or `MESZ2021`. A continuous symbol such as `MNQ1!` is refused.
- When a switch (`enabled`) is off, the block's other fields are type-checked and then ignored. The
  strategy can send its settings unchanged.

## Fields on every alert

| Field | Meaning |
|---|---|
| `protocol` | Always `decrypt-bridge-v2`. |
| `strategy` | The registered strategy id, for example `fvg-r5b`. It must be the binding's strategy. |
| `build` | The strategy's version label, 1-64 characters (letters, digits, `_ . + -`). Kept for the audit trail. |
| `event` | One of `entry`, `cancel_entry`, `modify_stop`, `exit`, `flatten`, `bias_flip`. |
| `alert_id` | Unique id of this alert, 1-160 characters (letters, digits, `_ . : @ + -`). A repeat with the same id is a duplicate. |
| `trade_id` | The strategy's trade id. It must end in `:long` or `:short`, matching `side`. Management events name the trade they act on. |
| `stream_id` | The binding's stream id, the route key the platform uses today. |
| `symbol` | The dated contract. It must be the binding's contract. |
| `side` | `long` or `short`. |
| `ts` | Alert time, Unix ms UTC. More than 5 s ahead of the clock is refused. |
| `extras` | The strategy's own fields, declared by the strategy (see "Extras"). `{}` when it declares none for this event. |

## Entry (`event: "entry"`)

### `entry`

| Field | Meaning |
|---|---|
| `type` | `market`, or `touch` (sent at market when the last price reaches the touch price). |
| `qty` | Contracts. The alert's size is used as sent. |
| `price` | Market: the reference price (signal bar close). Touch: the touch price. |
| `max_age_ms` | Freshness for a market entry, 1000 to 120000. The entry is refused or dropped when it is older. |

### `touch`

| Field | Meaning |
|---|---|
| `enabled` | Must be `true` exactly when `entry.type` is `touch`. |
| `price` | The touch price. It must equal `entry.price`. |
| `valid_until` | Unix ms. The armed entry is disarmed at this time. A touch alert is accepted while `valid_until` is in the future, whatever its age. |
| `cancel_if_stop_touched` | `true`: disarm when the last price reaches `stop.price` before the touch. |

A pending touch entry is disarmed at a restart, at the end of day and by `cancel_entry`. At the touch, the
entry must pass the normal entry checks within 5 s or it is dropped. The bridge sees the touch time as
the entry's signal time.

### `stop`

| Field | Meaning |
|---|---|
| `type` | `resting`: a stop order at the broker, attached to the entry. `bar_close`: a close at or beyond `price` on the `timeframe_s` bar exits everything at market. |
| `price` | The stop level, on the loss side of `entry.price`. |
| `timeframe_s` | `bar_close` only: 5 to 3600 seconds, a multiple of 5 that divides the hour (5, 10, 15, 30, 60, 120, 300, 900, 1800, 3600 and so on). `0` for `resting`. |
| `safety_price` | `bar_close` only: an optional resting broker stop beyond `price` (`0` for none). `0` for `resting`. |

A resting stop (or safety stop) always covers the open position. It is resized after every partial exit.
The bar-close stop is read from the binding's 5-second native bars. The timeframe bar's close is the
close of the 5-second bar that ends on the boundary. If that bar is missing, the last close before the
boundary is used. If the whole timeframe bar has no 5-second bar, that close is skipped and shown as an
alert.

Without a resting stop (bar-close with `safety_price: 0`) the entry is a plain market order. Nothing rests
at the broker for protection, so the strategy decides whether that is acceptable.

### `targets`

A list of 0 to 6 take-profits, nearest first. Their quantities plus `runner.qty` must equal `entry.qty`.

| Field | Meaning |
|---|---|
| `price` | The target price, on the profit side of `entry.price`. |
| `qty` | Contracts closed at this target. |
| `type` | `limit`: a resting limit at the broker. `market`: when the last price reaches `price`, this quantity exits at market. |

- At most 3 targets rest as limits, and no more than the strategy's adapter line allows.
- A `limit` target may not follow a `market` target.
- The first limit is staged with the bracket entry. The others are placed once the entry has filled.
- Before a market partial exit, the resting stop is shrunk to the size that will remain, so it can never over-fill.
- A market target waits until every nearer resting limit has filled. The bridge never sends a market exit while a take-profit limit rests.
- A target that closes the whole remaining position exits with `flatten_all`.

### `runner`

| Field | Meaning |
|---|---|
| `qty` | Contracts left after the targets. |
| `mode` | `hold`: leaves only by stop, break-even stop, end of day, daily loss or a management event. `target`: everything left exits at market when the last price reaches `price`. `trail`: see below. Must be `hold` when `qty` is 0. |
| `price` | `target` mode: beyond every target. |
| `trail_activation_ticks` | `trail` mode: the trail starts once the best price is this many ticks beyond the average entry fill. |
| `trail_distance_ticks` | `trail` mode: after activation, the runner exits at market when the price comes back this many ticks from the best price. At least 1. |

### `break_even`

| Field | Meaning |
|---|---|
| `enabled` | Off: nothing happens for break-even. |
| `trigger_price` | Break-even triggers when the last price reaches this price (`0`: no price trigger). |
| `trigger_target` | Break-even triggers when this target (1-based) has filled (`0`: no target trigger). |
| `trigger_mode` | `any`: one trigger is enough. `all`: every trigger given must be met. |
| `confirm` | `touch`: apply at once. `bar_close`: apply at the close of the `confirm_timeframe_s` bar in which the triggers were met. |
| `confirm_timeframe_s` | Bar timeframe for `bar_close` (same rule as `stop.timeframe_s`); `0` for `touch`. |
| `stop_price` | The break-even stop level. `0`: use the real average entry fill plus `offset_ticks`. |
| `offset_ticks` | Ticks from the average entry fill toward profit (may be negative). Used when `stop_price` is `0`. |
| `stop_type` | `resting`: move the resting stop (needs a resting stop or a safety stop). `bar_close`: the bar-close stop level becomes the break-even level, on `stop_timeframe_s` bars. |
| `stop_timeframe_s` | Bar timeframe for `bar_close`; `0` for `resting`. |
| `if_beyond` | When the last price is already at or beyond the level: `flatten` exits everything; `skip` leaves the stop where it is. |

Break-even never loosens a stop. It is applied once.

### `eod`

| Field | Meaning |
|---|---|
| `enabled` | Off: no end-of-day exit. |
| `time` | `HH:MM`, 24-hour local time. Everything exits at the first such time at or after the entry alert. |
| `tz` | `America/New_York`, `America/Chicago`, `America/Denver`, `America/Los_Angeles`, `Europe/London` or `UTC`. |

At that time an armed touch entry is disarmed and an unsent entry is dropped.

### `daily_loss`

| Field | Meaning |
|---|---|
| `enabled` | Off: no lock and no daily-loss exit. |
| `limit_usd` | The loss that locks the day, in USD (above 0 when enabled). |
| `fee_per_contract_usd` | Round-turn fee per contract, subtracted from each contract closed. |
| `tz` | Time zone of the trading day (same list as `eod.tz`). |
| `day_start` | `HH:MM` when the trading day starts in `tz` (`18:00` for the futures session, `00:00` for the calendar day). |
| `include_open` | `true`: the open position's P&L at the last price counts too (used with `flatten`). |
| `flatten` | `true`: an open trade exits at market when the day reaches the limit. |

The day's P&L is the realized net P&L of this binding's trades: average fill prices from the broker
evidence, the contract's point value (MNQ USD 2, MES USD 5) and the fee. An entry is refused while the
day is at or below `-limit_usd`. If a fill price is missing, the entry is refused (fail closed).

### `bias_flip`

| Field | Meaning |
|---|---|
| `enabled` | `true`: a `bias_flip` event for this trade exits everything. Off: `bias_flip` events are ignored. |

## Management events

Each acts only on the open trade with the same `trade_id` and `side`. An event for any other trade is
recorded and ignored. Management events are never refused for age (an exit only reduces risk). Each has
a `reason` (1-64 of `a-z 0-9 _`) for the audit trail.

| Event | Extra fields | What happens |
|---|---|---|
| `cancel_entry` | `reason` | Disarms an armed touch entry. Ignored once the entry was sent. |
| `modify_stop` | `stop` {`price`, `safety_price`}, `reason` | Resting stop: moves it to `price`. Bar-close stop: the watched level becomes `price`, and a non-zero `safety_price` moves the safety stop. |
| `exit` | `qty`, `reason` | Exits `qty` at market (the stop is shrunk first). `qty` of the whole position or more exits everything. A partial exit while a take-profit limit rests is refused and shown as an alert. |
| `flatten` | `reason` | Exits everything and cancels working orders. |
| `bias_flip` | `reason` | Exits everything when the entry's `bias_flip.enabled` is true. |

## Extras

A strategy declares its own fields once, in its strategy module:

```python
STRATEGY = bridge_v2.Strategy(strategy_id='fvg-r5b', adapter_id='fvg-r5b-bridge-v2', label='FVG/IFVG',
    extras={'entry': {'profile': {'type': 'str', 'pattern': r'[A-Za-z0-9_]{1,24}', 'max_len': 24}}})
```

Field types: `int` and `number` (optional `min`, `max`), `bool`, `str` (`pattern`, `max_len`), `enum`
(`values`), `price` (on tick, or 0) and `ms`. Add `'optional': True` for a field that may be absent. An
undeclared extras field is refused. The receiver does nothing with extras; they are recorded with the
trade. An optional `check(payload)` function may add cross-field format checks. It must not add trading rules.

## Examples

These validate against the test strategy `example-v2` (`tests/bridge_v2_example_strategy.py`), whose entry
extras are `setup` (and optional `risk_usd`) and whose `bias_flip` extras are `new_trend`.

Market entry, resting stop, two resting targets, runner held, break-even on TP1 fill at the 1-minute
close, end of day 15:30 New York:

```json
{"protocol": "decrypt-bridge-v2", "strategy": "example-v2", "build": "example-1.0", "event": "entry",
 "alert_id": "ex1:MNQZ2021:1634135340000:long:entry", "trade_id": "ex1:MNQZ2021:1634135340000:long",
 "stream_id": "fixture-stream", "symbol": "MNQZ2021", "side": "long", "ts": 1634135400000,
 "entry": {"type": "market", "qty": 4, "price": 10000.0, "max_age_ms": 40000},
 "touch": {"enabled": false, "price": 0, "valid_until": 0, "cancel_if_stop_touched": false},
 "stop": {"type": "resting", "price": 9980.0, "timeframe_s": 0, "safety_price": 0},
 "targets": [{"price": 10010.0, "qty": 1, "type": "limit"}, {"price": 10020.0, "qty": 1, "type": "limit"}],
 "runner": {"qty": 2, "mode": "hold", "price": 0, "trail_activation_ticks": 0, "trail_distance_ticks": 0},
 "break_even": {"enabled": true, "trigger_price": 0, "trigger_target": 1, "trigger_mode": "any",
                "confirm": "bar_close", "confirm_timeframe_s": 60, "stop_price": 0, "offset_ticks": 4,
                "stop_type": "resting", "stop_timeframe_s": 0, "if_beyond": "flatten"},
 "eod": {"enabled": true, "time": "15:30", "tz": "America/New_York"},
 "daily_loss": {"enabled": false, "limit_usd": 100, "fee_per_contract_usd": 1.0, "tz": "America/New_York",
                "day_start": "00:00", "include_open": false, "flatten": false},
 "bias_flip": {"enabled": true}, "extras": {"setup": "A"}}
```

Touch entry, valid for one hour, bar-close stop on 1-minute closes with a resting safety stop, three
resting targets, daily loss lock that also exits an open trade:

```json
{"protocol": "decrypt-bridge-v2", "strategy": "example-v2", "build": "example-1.0", "event": "entry",
 "alert_id": "ex1:MESZ2021:1634135340000:short:entry", "trade_id": "ex1:MESZ2021:1634135340000:short",
 "stream_id": "fixture-stream", "symbol": "MESZ2021", "side": "short", "ts": 1634135400000,
 "entry": {"type": "touch", "qty": 4, "price": 5000.0, "max_age_ms": 50000},
 "touch": {"enabled": true, "price": 5000.0, "valid_until": 1634139000000, "cancel_if_stop_touched": true},
 "stop": {"type": "bar_close", "price": 5010.0, "timeframe_s": 60, "safety_price": 5016.0},
 "targets": [{"price": 4990.0, "qty": 1, "type": "limit"}, {"price": 4980.0, "qty": 1, "type": "limit"},
             {"price": 4970.0, "qty": 1, "type": "limit"}],
 "runner": {"qty": 1, "mode": "trail", "price": 0, "trail_activation_ticks": 28, "trail_distance_ticks": 11},
 "break_even": {"enabled": true, "trigger_price": 4990.0, "trigger_target": 1, "trigger_mode": "any",
                "confirm": "touch", "confirm_timeframe_s": 0, "stop_price": 4998.0, "offset_ticks": 0,
                "stop_type": "resting", "stop_timeframe_s": 0, "if_beyond": "flatten"},
 "eod": {"enabled": true, "time": "15:30", "tz": "America/New_York"},
 "daily_loss": {"enabled": true, "limit_usd": 100, "fee_per_contract_usd": 1.0, "tz": "America/New_York",
                "day_start": "00:00", "include_open": true, "flatten": true},
 "bias_flip": {"enabled": false}, "extras": {"setup": "B", "risk_usd": 200}}
```

A 1-minute bar-close stop with a resting safety stop, a resting TP1 and a market TP2, runner to a target,
break-even moves the resting stop on a TP1 touch:

```json
{"protocol": "decrypt-bridge-v2", "strategy": "example-v2", "build": "example-1.0", "event": "entry",
 "alert_id": "ex2:MNQZ2021:1634135370000:long:entry", "trade_id": "ex2:MNQZ2021:1634135370000:long",
 "stream_id": "fixture-stream", "symbol": "MNQZ2021", "side": "long", "ts": 1634135400000,
 "entry": {"type": "market", "qty": 9, "price": 10000.0, "max_age_ms": 70000},
 "touch": {"enabled": false, "price": 0, "valid_until": 0, "cancel_if_stop_touched": false},
 "stop": {"type": "bar_close", "price": 9988.0, "timeframe_s": 60, "safety_price": 9980.0},
 "targets": [{"price": 10006.0, "qty": 3, "type": "limit"}, {"price": 10014.0, "qty": 2, "type": "market"}],
 "runner": {"qty": 4, "mode": "target", "price": 10035.0, "trail_activation_ticks": 0, "trail_distance_ticks": 0},
 "break_even": {"enabled": true, "trigger_price": 0, "trigger_target": 1, "trigger_mode": "any",
                "confirm": "touch", "confirm_timeframe_s": 0, "stop_price": 10003.0, "offset_ticks": 0,
                "stop_type": "resting", "stop_timeframe_s": 0, "if_beyond": "flatten"},
 "eod": {"enabled": true, "time": "15:45", "tz": "America/New_York"},
 "daily_loss": {"enabled": false, "limit_usd": 0, "fee_per_contract_usd": 0, "tz": "America/New_York",
                "day_start": "00:00", "include_open": false, "flatten": false},
 "bias_flip": {"enabled": false}, "extras": {"setup": "B"}}
```

Management events:

```json
{"protocol": "decrypt-bridge-v2", "strategy": "example-v2", "build": "example-1.0", "event": "bias_flip",
 "alert_id": "ex1:MNQZ2021:1634135340000:long:bias_flip:1634136300000",
 "trade_id": "ex1:MNQZ2021:1634135340000:long", "stream_id": "fixture-stream", "symbol": "MNQZ2021",
 "side": "long", "ts": 1634136300000, "reason": "bias_flip", "extras": {"new_trend": "bear"}}
```

```json
{"protocol": "decrypt-bridge-v2", "strategy": "example-v2", "build": "example-1.0", "event": "modify_stop",
 "alert_id": "ex1:MNQZ2021:1634135340000:long:modify_stop:1634136000000",
 "trade_id": "ex1:MNQZ2021:1634135340000:long", "stream_id": "fixture-stream", "symbol": "MNQZ2021",
 "side": "long", "ts": 1634136000000, "stop": {"price": 9990.0, "safety_price": 0}, "reason": "trail_step",
 "extras": {}}
```

```json
{"protocol": "decrypt-bridge-v2", "strategy": "example-v2", "build": "example-1.0", "event": "exit",
 "alert_id": "ex1:MNQZ2021:1634135340000:long:exit:1634136600000",
 "trade_id": "ex1:MNQZ2021:1634135340000:long", "stream_id": "fixture-stream", "symbol": "MNQZ2021",
 "side": "long", "ts": 1634136600000, "qty": 2, "reason": "test_exit", "extras": {}}
```

```json
{"protocol": "decrypt-bridge-v2", "strategy": "example-v2", "build": "example-1.0", "event": "flatten",
 "alert_id": "ex1:MNQZ2021:1634135340000:long:flatten:1634136900000",
 "trade_id": "ex1:MNQZ2021:1634135340000:long", "stream_id": "fixture-stream", "symbol": "MNQZ2021",
 "side": "long", "ts": 1634136900000, "reason": "daily_loss", "extras": {}}
```

```json
{"protocol": "decrypt-bridge-v2", "strategy": "example-v2", "build": "example-1.0", "event": "cancel_entry",
 "alert_id": "ex1:MESZ2021:1634135340000:short:cancel_entry:1634137200000",
 "trade_id": "ex1:MESZ2021:1634135340000:short", "stream_id": "fixture-stream", "symbol": "MESZ2021",
 "side": "short", "ts": 1634137200000, "reason": "strategy_cancel", "extras": {}}
```

## When something goes wrong

These put the trade in `recovery_required`. Nothing more is sent for it and entries stay held until a
person has checked Tradovate:

- a send whose outcome is unknown, or a send in flight at a restart;
- a missing protective stop, or a resting target cancelled outside the receiver;
- fills that do not add up to the position, or an order that is not the receiver's;
- a management order the bridge refuses three times (retried after 2, 4 and 8 s);
- an order the broker evidence does not confirm within 30 s;
- a missing average fill price when break-even, the trail or the daily loss lock needs it.

`Engine.release_recovery()` closes the trade only when fresh broker evidence shows the account flat
with nothing working. It never sends anything.

## Adding a strategy

1. Add `app/trading-runtime/decrypt_trading/bridge_v2_<name>.py`: its `Strategy(...)` (with its extras
   schema) and `Engine, input_identity, payload_schema, signal_time, is_entry_payload, bridge_message =
   bridge_v2.module_api(STRATEGY)`. `tests/bridge_v2_example_strategy.py` is a complete example.
2. Add one line to `bridge_v2_registry.STRATEGIES`: `'<adapter id>': ('bridge_v2_<name>.py', '<module
   SHA-256>', '<strategy pair id>')`. The pair id is opaque: the SHA-256 of the neutral label
   `decrypt-os/strategy/<adapter id>/v1`, never a hash of the TradingView script. Bump the trailing version
   when the paired strategy changes.
3. Add one line to `V2_ADAPTERS` in `bridge/tvbridge/adapters.py`: `('<adapter id>', '<label>', ('MNQ',),
   <resting targets 1-3>)`, then copy the file byte for byte to `decrypt_trading/bridge_adapters.py`.
4. Add the module to `installer/clean_base.py` `TRADING_FILES` and `lab/build_source_bundle.py` `TRADING_SOURCE`.
5. Run `tests/test_bridge_v2_contract.py`. Its registry test prints any hash that does not match.

Bridge config of a binding (the receiver needs the fill prices for break-even, trail and daily loss):

```
"ticker": "MNQZ2021", "tick_size": 0.25,
"adapters": {"<adapter id>": {"enabled": true}},
"bracket_orders_enabled": true, "flatten_all_enabled": true, "snapshot_fill_prices": true
```

Check alert files from the command line:

```sh
cd candidate/app-source/app/trading-runtime
python3 -m decrypt_trading.bridge_v2_contract alert.json
```
