## 4. Time: sessions, killzones, macros and opening prices

All times New York local (America/New_York), 24-hour clock. These tables match the Decrypt OS ICT clock (`ict_clock.py`). ICT has shifted some windows between mentorship years and between forex and index futures; when a user quotes a different window, explain the variant instead of arguing.

### Killzones and sessions

| Window | NY time | Notes |
| --- | --- | --- |
| Asian killzone | 20:00-00:00 | Asian range forms; its high/low become liquidity for London. Some versions use 19:00-22:00. |
| London killzone | 02:00-05:00 | Often makes the high or low of the day in forex; frequently raids the Asian range. |
| New York killzone (forex) | 07:00-10:00 | Forex version of the NY open window. |
| New York AM killzone (indices) | 08:30-11:00 | Index futures window; 08:30 news and the 09:30 equities open sit inside it. |
| London close killzone | 10:00-12:00 | London traders close positions; retracements and reversals of the morning move. |
| New York lunch | 12:00-13:30 | Usually avoid; low-quality, choppy price. |
| New York PM session | 13:30-16:00 | Afternoon delivery; 14:00 often a turning point (FOMC days especially). |

### Silver Bullet windows

| Window | NY time |
| --- | --- |
| London Silver Bullet | 03:00-04:00 |
| AM Silver Bullet | 10:00-11:00 |
| PM Silver Bullet | 14:00-15:00 |

### Algorithmic macros (short windows where price often seeks liquidity or rebalances)

| Window | NY time |
| --- | --- |
| London macro 1 | 02:33-03:00 |
| London macro 2 | 04:03-04:30 |
| NY AM macro 1 | 08:50-09:10 |
| NY AM macro 2 | 09:50-10:10 |
| NY AM macro 3 | 10:50-11:10 |
| NY lunch macro | 11:50-12:10 |
| NY PM macro | 13:10-13:40 |
| Last-hour macro | 15:15-15:45 |

### Opening prices and gaps

- **Midnight open (00:00)**: ICT's "true day" opening price. Below it in a bullish day is discount; above it in a bearish day is premium.
- **08:30 open**: US news time and the start of the NY AM index window.
- **09:30 open**: equities cash open; the opening range and the **opening range gap** (prior 16:00 cash close to 09:30 open) matter for indices.
- **18:00 open**: CME Globex reopen; the futures trading day starts.
- **NDOG (new day opening gap)**: futures, from the 17:00 close to the 18:00 reopen. Can be tiny.
- **NWOG (new week opening gap)**: futures, from Friday's 17:00 close to Sunday's 18:00 open. Track the last five as references. Some lessons also use Friday close to Monday cash open; keep the two conventions apart.
- **CBDR (central bank dealers range)**: forex, 14:00-20:00. Small CBDR often precedes a larger next-day range; its standard deviations project highs/lows.
- **Asian range**: 20:00-00:00 high and low; standard deviations of it are used as projections too.

### Market hours (CME index futures)

Sunday 18:00 to Friday 17:00, with a daily pause 17:00-18:00. No futures trading Saturday. US holidays and early closes change this; tell the user to check the exchange calendar.

### Daily and weekly profiles

- Typical bullish day: open, an early move lower (often in London or the first NY hour) that takes sell-side liquidity, then expansion higher, close near the high. Bearish is the mirror. This is the **Power of Three** at daily scale.
- Weekly: the low (bullish week) or high (bearish week) often forms Monday to Wednesday; Thursday/Friday can reverse around major news. Classic profiles: "classic Tuesday low/high", "Wednesday reversal", "consolidation then Thursday expansion".
- High-impact news (CPI, NFP, FOMC) can override any time model. Without a calendar, say the news context is unknown.
