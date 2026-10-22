"""Validate observed main-series updates, not exchange trades or fills.

The supplied sequence is a persistent local collector counter. callback_at is
the browser dataUpdated callback clock, never a relabeled exchange event time.
Keep the last fresh output as previous. A HOLD must not advance that cursor.
"""

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math


TIMEFRAME = "1S"
SOURCE = "tradingview_chart_series"
SCHEMA = "decrypt.chart-price.v1"


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("invalid_utc_timestamp")
    try:
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid_utc_timestamp") from None
    if value.tzinfo is None or value.utcoffset().total_seconds() != 0:
        raise ValueError("invalid_utc_timestamp")
    return value


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _age(stamp, now, max_age, label):
    age = (now - stamp).total_seconds()
    if age < 0:
        raise ValueError(label + "_in_future")
    if age > max_age:
        raise ValueError(label + "_stale")


def validate_chart_event(packet, *, sequence, chart_url, symbol, now=None, previous=None, max_age=3):
    """Return a fresh/hold chart observation with explicit timestamp semantics.

    Required input fields are page_url, event_kind, callback_at, history,
    series_symbol, timeframe, is_delayed, replay_status, chart_ready,
    chart_failed, series_status, symbol_info and bar. bar has the exact shape
    [bar_open_unix_seconds, open, high, low, close, volume]. source_status may
    appear as a diagnostic, but its null value is not a readiness gate.
    """
    CHART_URL, SYMBOL, SERIES_SYMBOL = chart_url, symbol, 'CME_MINI:'+symbol
    receipt = now if now is not None else datetime.now(timezone.utc).isoformat()
    result = {
        "schema": SCHEMA,
        "status": "hold",
        "source": SOURCE,
        "symbol": SYMBOL,
        "series_symbol": SERIES_SYMBOL,
        "timeframe": TIMEFRAME,
        "chart_url": CHART_URL,
        "sequence": sequence if type(sequence) is int else None,
        "sequence_semantics": "persistent_local_chart_update",
        "timestamp_semantics": "browser_chart_update",
        "observed_at": None,
        "received_at": receipt if isinstance(receipt, str) else None,
        "bar_open_at": None,
        "provenance_verified": False,
        "execution_eligible": False,
        "reasons": [],
    }
    try:
        clock = _stamp(receipt)
        if not _number(max_age) or max_age <= 0:
            raise ValueError("invalid_age_limit")
        if not isinstance(packet, dict):
            raise ValueError("packet_not_object")
        if type(sequence) is not int or sequence < 1:
            raise ValueError("invalid_local_sequence")
        if packet.get("page_url") != CHART_URL:
            raise ValueError("wrong_chart_url")
        if packet.get("event_kind") != "data_updated" or packet.get("history") is not False:
            raise ValueError("not_a_live_chart_update")
        if packet.get("series_symbol") != SERIES_SYMBOL or packet.get("timeframe") != TIMEFRAME:
            raise ValueError("wrong_series_or_timeframe")
        info = packet.get("symbol_info")
        if not isinstance(info, dict) or info.get("full_name") != SERIES_SYMBOL:
            raise ValueError("wrong_resolved_symbol")
        if type(info.get("minmov")) is not int or type(info.get("pricescale")) is not int:
            raise ValueError("invalid_tick_definition")
        if info["minmov"] != 25 or info["pricescale"] != 100:
            raise ValueError("unexpected_tick_definition")
        if packet.get("chart_ready") is not True or packet.get("chart_failed") is not False:
            raise ValueError("chart_not_ready")
        if type(packet.get("series_status")) is not int or packet["series_status"] != 3:
            raise ValueError("series_not_ready")
        if packet.get("is_delayed") is not False:
            raise ValueError("delayed_or_unknown_feed")
        if type(packet.get("replay_status")) is not int or packet["replay_status"] != 0:
            raise ValueError("replay_or_unknown_mode")
        observed = _stamp(packet.get("callback_at"))
        result["observed_at"] = packet["callback_at"]
        _age(observed, clock, max_age, "callback")
        bar = packet.get("bar")
        if not isinstance(bar, list) or len(bar) != 6 or not all(_number(v) for v in bar):
            raise ValueError("invalid_bar")
        if bar[0] <= 0 or float(bar[0]).is_integer() is False:
            raise ValueError("invalid_bar_time")
        try:
            bar_time = datetime.fromtimestamp(bar[0], timezone.utc)
        except (ValueError, OverflowError, OSError):
            raise ValueError("invalid_bar_time") from None
        result["bar_open_at"] = bar_time.isoformat()
        _age(bar_time, clock, max_age, "bar")
        if bar_time > observed:
            raise ValueError("bar_after_callback")
        open_price, high, low, close, volume = bar[1:]
        if min(open_price, high, low, close) <= 0 or volume < 0:
            raise ValueError("invalid_ohlcv")
        if high < max(open_price, low, close) or low > min(open_price, high, close):
            raise ValueError("inconsistent_ohlc")
        if Decimal(str(close)) % Decimal("0.25"):
            raise ValueError("close_off_tick")
        identity = {
            "series_symbol": SERIES_SYMBOL,
            "timeframe": TIMEFRAME,
            "symbol_info": {"full_name": SERIES_SYMBOL, "minmov": 25, "pricescale": 100},
            "bar": [float(v) for v in bar],
        }
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        if previous is not None:
            if not isinstance(previous, dict) or any((
                previous.get("schema") != SCHEMA,
                previous.get("status") != "fresh",
                previous.get("source") != SOURCE,
                previous.get("series_symbol") != SERIES_SYMBOL,
                previous.get("timeframe") != TIMEFRAME,
                previous.get("chart_url") != CHART_URL,
                previous.get("timestamp_semantics") != "browser_chart_update",
                type(previous.get("sequence")) is not int,
                not isinstance(previous.get("fingerprint"), str),
            )):
                raise ValueError("invalid_previous_cursor")
            if sequence <= previous["sequence"]:
                raise ValueError("local_sequence_not_advancing")
            if observed < _stamp(previous.get("observed_at")):
                raise ValueError("callback_time_regressed")
            if bar_time < _stamp(previous.get("bar_open_at")):
                raise ValueError("bar_time_regressed")
            if fingerprint == previous["fingerprint"]:
                raise ValueError("duplicate_chart_state")
        result.update(
            status="fresh",
            price=close,
            bar=list(bar),
            fingerprint=fingerprint,
            symbol_info=identity["symbol_info"],
            callback_age_seconds=(clock - observed).total_seconds(),
            bar_age_seconds=(clock - bar_time).total_seconds(),
        )
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        result["reasons"].append(str(exc))
    return result
