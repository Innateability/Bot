import requests
from datetime import datetime, timezone, timedelta

# ==============================
# Fetch Candles (Bybit API)
# ==============================
def fetch_candles(symbol="TRXUSDT", interval="60", days=30):
    url = "https://api.bybit.com/v5/market/kline"
    limit = days * (24 * 60 // int(interval))
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params)
    data = r.json()["result"]["list"]

    candles = []
    for d in reversed(data):  # oldest first
        t = datetime.fromtimestamp(int(d[0]) / 1000, tz=timezone.utc).astimezone(
            timezone(timedelta(hours=1))  # Nigeria timezone UTC+1
        )
        candles.append({
            "time": t,
            "open": float(d[1]),
            "high": float(d[2]),
            "low": float(d[3]),
            "close": float(d[4]),
        })
    return candles

# ==============================
# Heikin Ashi Conversion
# ==============================
def heikin_ashi(candles, initial_open=None):
    ha_candles = []
    ha_open = initial_open if initial_open is not None else candles[0]["open"]

    for i, c in enumerate(candles):
        ha_close = (c["open"] + c["high"] + c["low"] + c["close"]) / 4
        if i == 0 and initial_open is not None:
            ha_open = initial_open
        elif i > 0:
            ha_open = (ha_open + ha_candles[-1]["ha_close"]) / 2

        ha_high = max(c["high"], ha_open, ha_close)
        ha_low = min(c["low"], ha_open, ha_close)

        ha_candles.append({
            "time": c["time"],
            "open": c["open"],
            "high": c["high"],
            "low": c["low"],
            "close": c["close"],
            "ha_open": ha_open,
            "ha_close": ha_close,
            "ha_high": ha_high,
            "ha_low": ha_low,
        })
    return ha_candles

# ==============================
# Signal Computation
# ==============================
def check_signal(ha_candles):
    signals = []
    for i in range(8, len(ha_candles)):
        last = ha_candles[i]
        prev = ha_candles[i - 1]
        recent = ha_candles[i - 8:i]

        # BUY condition
        if last["ha_close"] > last["ha_open"] and prev["ha_close"] < prev["ha_open"]:
            if sum(1 for r in recent if r["ha_close"] > r["ha_open"]) >= 5:
                sl = min(r["low"] for r in recent if r["ha_close"] < r["ha_open"]) - 0.0001
                signals.append(("buy", last, sl))

        # SELL condition
        if last["ha_close"] < last["ha_open"] and prev["ha_close"] > prev["ha_open"]:
            if sum(1 for r in recent if r["ha_close"] < r["ha_open"]) >= 5:
                sl = max(r["high"] for r in recent if r["ha_close"] > r["ha_open"]) + 0.0001
                signals.append(("sell", last, sl))

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
# Backtest Trades
# ==============================
def backtest(ha_candles, signals, timeframe):
    balance = 1000
    trades = []

    for trade_type, last, sl in signals:
        entry = last["close"]
        tp, risk_pct = compute_trade_params(entry, sl, trade_type, timeframe)

        # Skip if SL is too wide (>5%)
        if abs((entry - sl) / entry) > 0.05:
            print(f"{last['time']} | Signal {trade_type.upper()} skipped (SL > 5%)")
            continue

        qty = balance * risk_pct / abs(entry - sl)
        print(f"\n{last['time']} | Balance before trade: {balance:.2f}")
        print(f"Signal -> {trade_type.upper()} | Entry={entry:.4f}, SL={sl:.4f}, TP={tp:.4f}")

        # check candles after signal
        for j in range(ha_candles.index(last) + 1, len(ha_candles)):
            c = ha_candles[j]
            if trade_type == "buy":
                if c["low"] <= sl:
                    balance -= balance * risk_pct
                    print(f" -> SL hit at {c['time']} | New balance: {balance:.2f}")
                    trades.append((trade_type, entry, sl, tp, "SL", balance))
                    break
                elif c["high"] >= tp:
                    balance += balance * risk_pct
                    print(f" -> TP hit at {c['time']} | New balance: {balance:.2f}")
                    trades.append((trade_type, entry, sl, tp, "TP", balance))
                    break
            if trade_type == "sell":
                if c["high"] >= sl:
                    balance -= balance * risk_pct
                    print(f" -> SL hit at {c['time']} | New balance: {balance:.2f}")
                    trades.append((trade_type, entry, sl, tp, "SL", balance))
                    break
                elif c["low"] <= tp:
                    balance += balance * risk_pct
                    print(f" -> TP hit at {c['time']} | New balance: {balance:.2f}")
                    trades.append((trade_type, entry, sl, tp, "TP", balance))
                    break

    return trades, balance

# ==============================
# Run Bot
# ==============================
def run_bot():
    c1h = fetch_candles(interval="60")
    c4h = fetch_candles(interval="240")

    ha_1h = heikin_ashi(c1h, initial_open=0.34779)  # you can hardcode here
    ha_4h = heikin_ashi(c4h, initial_open=0.34747)  # and here

    print(f"First 1H candle: {ha_1h[0]['time']}, HA_open={ha_1h[0]['ha_open']}")
    print(f"First 4H candle: {ha_4h[0]['time']}, HA_open={ha_4h[0]['ha_open']}")

    signals_1h = check_signal(ha_1h)
    signals_4h = check_signal(ha_4h)

    trades_1h, bal_1h = backtest(ha_1h, signals_1h, "1h")
    trades_4h, bal_4h = backtest(ha_4h, signals_4h, "4h")

    print("\n--- RESULTS ---")
    print("1H Final Balance:", bal_1h)
    print("4H Final Balance:", bal_4h)

if __name__ == "__main__":
    run_bot()
