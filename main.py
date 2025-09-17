import requests
from datetime import datetime, timezone, timedelta

# ==============================
# Fetch Candles from Bybit
# ==============================
def fetch_candles(symbol="TRXUSDT", interval="60", days=30):
    url = "https://api.bybit.com/v5/market/kline"
    limit = days * (24 * 60 // int(interval))  # number of candles
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params)
    data = r.json()["result"]["list"]

    candles = []
    for c in reversed(data):  # reverse so oldest is first
        candles.append({
            "time": datetime.fromtimestamp(int(c[0]) / 1000, tz=timezone.utc),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4])
        })
    return candles

# ==============================
# Heikin Ashi Conversion
# ==============================
def heikin_ashi(candles, initial_open=None):
    ha_candles = []
    if initial_open is None:
        ha_open = candles[0]["open"]
    else:
        ha_open = initial_open

    for c in candles:
        ha_close = (c["open"] + c["high"] + c["low"] + c["close"]) / 4
        ha_open = (ha_open + ha_close) / 2 if ha_candles else ha_open
        ha_high = max(c["high"], ha_open, ha_close)
        ha_low = min(c["low"], ha_open, ha_close)

        ha_candles.append({
            "time": c["time"],
            "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"],
            "HA_open": ha_open, "HA_close": ha_close,
            "HA_high": ha_high, "HA_low": ha_low
        })
    return ha_candles

# ==============================
# Signal Computation
# ==============================
def check_signal(ha_candles):
    signals = []
    for i in range(8, len(ha_candles)):
        recent = ha_candles[i-8:i]
        last_candle = ha_candles[i]
        prev_candle = ha_candles[i-1]

        # SELL
        if last_candle["HA_close"] < last_candle["HA_open"] and prev_candle["HA_close"] > prev_candle["HA_open"]:
            if sum(1 for r in recent if r["HA_close"] < r["HA_open"]) >= 5:
                sl = max(r["high"] for r in recent if r["HA_close"] > r["HA_open"]) + 0.0001
                signals.append(("sell", last_candle["time"], last_candle["close"], sl))

        # BUY
        if last_candle["HA_close"] > last_candle["HA_open"] and prev_candle["HA_close"] < prev_candle["HA_open"]:
            if sum(1 for r in recent if r["HA_close"] > r["HA_open"]) >= 5:
                sl = min(r["low"] for r in recent if r["HA_close"] < r["HA_open"]) - 0.0001
                signals.append(("buy", last_candle["time"], last_candle["close"], sl))
    return signals

# ==============================
# Trade Parameters
# ==============================
def compute_trade_params(entry, sl, trade_type, timeframe):
    rr = 1 if timeframe == "1h" else 2
    risk_pct = 0.1 if timeframe == "1h" else 0.5
    tp = entry + (entry - sl) * rr if trade_type == "buy" else entry - (sl - entry) * rr
    tp += entry * 0.001 if trade_type == "buy" else -entry * 0.001
    return tp, risk_pct

# ==============================
# Backtest
# ==============================
def backtest(candles, signals, timeframe):
    balance = 1000
    trades = []

    for sig in signals:
        trade_type, sig_time, entry, sl = sig
        tp, risk_pct = compute_trade_params(entry, sl, trade_type, timeframe)
        qty = balance * risk_pct / abs(entry - sl)

        start_index = next(i for i, c in enumerate(candles) if c["time"] == sig_time)

        for c in candles[start_index+1:]:
            if trade_type == "buy":
                if c["low"] <= sl:
                    balance -= balance * risk_pct
                    trades.append((trade_type, sig_time, entry, sl, tp, "SL", balance))
                    break
                elif c["high"] >= tp:
                    balance += balance * risk_pct
                    trades.append((trade_type, sig_time, entry, sl, tp, "TP", balance))
                    break

            elif trade_type == "sell":
                if c["high"] >= sl:
                    balance -= balance * risk_pct
                    trades.append((trade_type, sig_time, entry, sl, tp, "SL", balance))
                    break
                elif c["low"] <= tp:
                    balance += balance * risk_pct
                    trades.append((trade_type, sig_time, entry, sl, tp, "TP", balance))
                    break
    return trades, balance

# ==============================
# Run Bot
# ==============================
def run_bot():
    # Fetch both
    c1h = fetch_candles(interval="60")
    c4h = fetch_candles(interval="240")

    # 🔹 Hardcode initial opens
    ha_1h = heikin_ashi(c1h, initial_open=0.33)
    ha_4h = heikin_ashi(c4h, initial_open=0.35)

    signals_1h = check_signal(ha_1h)
    signals_4h = check_signal(ha_4h)

    trades_1h, bal_1h = backtest(c1h, signals_1h, "1h")
    trades_4h, bal_4h = backtest(c4h, signals_4h, "4h")

    print("\n--- RESULTS ---")
    print("1H Final Balance:", bal_1h, "Trades:", len(trades_1h))
    print("4H Final Balance:", bal_4h, "Trades:", len(trades_4h))

if __name__ == "__main__":
    run_bot()
