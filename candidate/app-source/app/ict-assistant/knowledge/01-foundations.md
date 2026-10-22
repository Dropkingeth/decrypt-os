## 1. Foundations: time, price and the dealing range

**Core idea.** ICT treats price as being delivered by an algorithm he calls IPDA (Interbank Price Delivery Algorithm). Whether or not one accepts that story, the working method is: price moves from one pool of liquidity to another, and it does so at predictable times, rebalancing inefficiencies (gaps) on the way. Study *where* price is likely to go (liquidity), *when* it is likely to move (time), and *from where* (PD arrays).

**New York time.** Every ICT time window is in New York local time and follows daylight saving. A window like 08:30-11:00 is 08:30-11:00 in New York in summer and in winter. Never convert with a fixed UTC-5.

**Swing points.** A swing high is a candle with a lower high on each side; a swing low has a higher low on each side. Short-term, intermediate-term and long-term highs/lows are nested: an intermediate-term high is a short-term high with lower short-term highs on both sides, and so on. ICT reads structure through these, not through indicator pivots.

**Dealing range.** A range defined by a meaningful swing high and swing low (often the most recent leg that took liquidity). Its 50% level is **equilibrium**.
- Above equilibrium is **premium**: ICT prefers to sell there in a bearish context.
- Below equilibrium is **discount**: ICT prefers to buy there in a bullish context.
- Always say which range and timeframe you are using; price can be premium in one range and discount in another.
- Calculation example: high 120, low 100, equilibrium 110. The range anchors are interpretation; the midpoint is arithmetic.

**Higher timeframe first.** Bias and draw come from higher timeframes (monthly, weekly, daily, 4h); setups form on 1h/15m; execution on 5m/1m. A common pairing: daily or 4h for the narrative, 15m for structure, 1m-5m for entry. One lower-timeframe move does not reverse a higher-timeframe draw.

**IPDA data ranges.** ICT looks back 20, 40 and 60 trading days (not calendar days) to find the highs and lows the market is likely to reach for, and forward 20/40/60 days as a projection window. The 20-day high and low are the nearest "external" references; 40 and 60 widen the context. Quarterly shifts (roughly every three to four months) often reset which range matters. Count trading sessions, not calendar days.

**Market efficiency paradigm.** Price spends its time either seeking liquidity (taking out old highs/lows) or rebalancing inefficiency (filling gaps). If you cannot say which one price is doing, you do not have a narrative yet.

**Fair price vs. delivery.** Fast one-directional moves leave imbalances. Price often returns to them before continuing. Slow, overlapping price action is "efficient" and offers little.
