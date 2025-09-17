import requests
from datetime import datetime, timezone, timedelta

# ==============================
# Heikin Ashi Conversion
# ==============================
def heikin_ashi(candles, initial_open=None):
    ha_candles = []
    ha_open = initial_open if initial_open is not None else candles[0]["open"]

    for i, c in enumerate(candles):
        ha_close = (c["open"] + c["high"] + c["low"] + c["close"]) / 4
        if i == 0 and initial_open is None:
            ha_open = c["open"]
        else:
            ha_open = (ha_open + ha_candles[-1]["ha_close"]) / 2

        ha_high = max(c["high"], ha_open, ha_close)
        ha_low = min(c["low"], ha_open, ha_close)

        ha_candles.append({
            "time": c["time"],
            "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"],
            "ha_open": ha_open, "ha_close": ha_close, "ha_high": ha_high, "ha_low": ha_low
        })

    return ha_candles

# ==============================
# Fetch Candles from Bybit
# ==============================
def fetch_candles(symbol="TRXUSDT", interval="60", days=30):
    url = "https://api.bybit.com/v5/market/kline"
    limit = days * (24 * 60 // int(interval))  # candles count
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params)
    data = r.json()["result"]["list"]

    candles = []
    for row in reversed(data):  # reverse into chronological order
        ts = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc).astimezone(
            timezone(timedelta(hours=1))  # Nigeria time UTC+1
        )
        candles.append({
            "time": ts,
            "open": float(row[1]), "high": float(row[2]),
            "low": float(row[3]), "close": float(row[4])
        })
    return candles

# ==============================
# Signal Computation
# ==============================
def check_signal(ha_candles):
    signals = []
    for i in range(8, len(ha_candles)):
        last = ha_candles[i]
        prev = ha_candles[i - 1]
        recent = ha_candles[i - 8:i]

        # Count green/red
        greens = sum(1 for c in recent if c["ha_close"] > c["ha_open"])
        reds = 8 - greens

        # SELL condition
        if last["ha_close"] < last["ha_open"] and prev["ha_close"] > prev["ha_open"]:
            if reds >= 5:
                sl = max(c["high"] for c in recent if c["ha_close"] > c["ha_open"]) + 0.0001
                signals.append(("sell", last, prev, sl))

        # BUY condition
        if last["ha_close"] > last["ha_open"] and prev["ha_close"] < prev["ha_open"]:
            if greens >= 5:
                sl = min(c["low"] for c in recent if c["ha_close"] < c["ha_open"]) - 0.0001
                signals.append(("buy", last, prev, sl))

    return signals

# ==============================
# Trade Parameters (TP, Risk %)
# ==============================
def compute_trade_params(entry, sl, trade_type, timeframe):
    rr = 1 if timeframe == "1h" else 2
    risk_pct = 0.1 if timeframe == "1h" else 0.5
    tp = entry + (entry - sl) * rr if trade_type == "buy" else entry - (sl - entry) * rr
    tp += entry * 0.001 if trade_type == "buy" else -entry * 0.001
    return tp, risk_pct

# ==============================
# Backtest Trades (TP/SL check)
# ==============================
def backtest(candles, ha_candles, signals, timeframe):
    balance = 10
    trades = []
    trade_open = False

    for sig in signals:
        trade_type, last, prev, sl = sig
        entry = last["close"]
        tp, risk_pct = compute_trade_params(entry, sl, trade_type, timeframe)

        # LOG the signal candles
        print(f"\n[{timeframe.upper()} SIGNAL] {trade_type.upper()} at {last['time']}")
        print(f"Prev RAW o/h/l/c = {prev['open']}/{prev['high']}/{prev['low']}/{prev['close']}")
        print(f"Prev HA  o/h/l/c = {prev['ha_open']}/{prev['ha_high']}/{prev['ha_low']}/{prev['ha_close']}")
        print(f"Last RAW o/h/l/c = {last['open']}/{last['high']}/{last['low']}/{last['close']}")
        print(f"Last HA  o/h/l/c = {last['ha_open']}/{last['ha_high']}/{last['ha_low']}/{last['ha_close']}")
        print(f"Candidate SL = {sl}, Candidate TP = {tp}, Entry = {entry}")
        print(f"Balance before trade = {balance}")

        if trade_open:
            print("⚠️ Trade ignored (already in trade).")
            continue

        # Open trade
        qty = balance * risk_pct / abs(entry - sl)
        trade_open = True

        # Now check future candles
        start_index = ha_candles.index(last) + 1
        for j in range(start_index, len(candles)):
            high, low, tstamp = candles[j]["high"], candles[j]["low"], candles[j]["time"]

            if trade_type == "buy":
                if low <= sl:
                    balance -= balance * risk_pct
                    trades.append((trade_type, last["time"], entry, sl, tp, "SL", balance))
                    trade_open = False
                    break
                elif high >= tp:
                    balance += balance * risk_pct
                    trades.append((trade_type, last["time"], entry, sl, tp, "TP", balance))
                    trade_open = False
                    break

            if trade_type == "sell":
                if high >= sl:
                    balance -= balance * risk_pct
                    trades.append((trade_type, last["time"], entry, sl, tp, "SL", balance))
                    trade_open = False
                    break
                elif low <= tp:
                    balance += balance * risk_pct
                    trades.append((trade_type, last["time"], entry, sl, tp, "TP", balance))
                    trade_open = False
                    break

    return trades, balance

# ==============================
# Run Bot
# ==============================
def run_bot():
    # Fetch
    c1h = fetch_candles(interval="60")
    c4h = fetch_candles(interval="240")

    # Hardcode initial HA opens if needed
    ha_1h = heikin_ashi(c1h, initial_open=c1h[0]["open"])
    ha_4h = heikin_ashi(c4h, initial_open=c4h[0]["open"])

    print(f"First 1H candle -> time: {ha_1h[0]['time']}, HA_open: {ha_1h[0]['ha_open']}")
    print(f"First 4H candle -> time: {ha_4h[0]['time']}, HA_open: {ha_4h[0]['ha_open']}")

    signals_1h = check_signal(ha_1h)
    signals_4h = check_signal(ha_4h)

    trades_1h, bal_1h = backtest(c1h, ha_1h, signals_1h, "1h")
    trades_4h, bal_4h = backtest(c4h, ha_4h, signals_4h, "4h")

    print("\n--- RESULTS ---")
    print("1H Final Balance:", bal_1h, "Trades:", len(trades_1h))
    print("4H Final Balance:", bal_4h, "Trades:", len(trades_4h))

if __name__ == "__main__":
    run_bot()
