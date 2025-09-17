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
        if i == 0:
            ha_open_i = ha_open
        else:
            ha_open_i = (ha_open_i + ha_candles[-1]["ha_close"]) / 2
        ha_high = max(c["high"], ha_open_i, ha_close)
        ha_low = min(c["low"], ha_open_i, ha_close)

        ha_candles.append({
            "time": c["time"],
            "open": c["open"],
            "high": c["high"],
            "low": c["low"],
            "close": c["close"],
            "ha_open": ha_open_i,
            "ha_high": ha_high,
            "ha_low": ha_low,
            "ha_close": ha_close
        })
    return ha_candles

# ==============================
# Fetch Candles from Bybit
# ==============================
def fetch_candles(symbol="TRXUSDT", interval="60", days=30):
    url = "https://api.bybit.com/v5/market/kline"
    limit = days * (24 * 60 // int(interval))  # approximate candle count
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params)
    data = r.json()["result"]["list"]

    candles = []
    for entry in reversed(data):  # oldest → newest
        candles.append({
            "time": datetime.fromtimestamp(int(entry[0]) / 1000, tz=timezone.utc)
                        .astimezone(timezone(timedelta(hours=1))),  # Lagos time
            "open": float(entry[1]),
            "high": float(entry[2]),
            "low": float(entry[3]),
            "close": float(entry[4]),
        })
    return candles

# ==============================
# Run Backtest
# ==============================
def run_backtest(ha_candles, timeframe, balance):
    trades = []

    for i in range(8, len(ha_candles)):
        last = ha_candles[i]
        prev = ha_candles[i - 1]
        recent = ha_candles[i - 8:i]

        print(f"\n[{timeframe}] Balance before trade check: {balance:.2f}")
        print(f"[{timeframe}] Candle {last['time']} | RAW O:{last['open']} H:{last['high']} L:{last['low']} C:{last['close']} | HA O:{last['ha_open']} H:{last['ha_high']} L:{last['ha_low']} C:{last['ha_close']}")

        signal = None
        sl = None

        # SELL condition
        if last["ha_close"] < last["ha_open"] and prev["ha_close"] > prev["ha_open"]:
            if sum(1 for c in recent if c["ha_close"] < c["ha_open"]) >= 5:
                sl = max(c["high"] for c in recent if c["ha_close"] > c["ha_open"]) + 0.0001
                signal = "sell"

        # BUY condition
        elif last["ha_close"] > last["ha_open"] and prev["ha_close"] < prev["ha_open"]:
            if sum(1 for c in recent if c["ha_close"] > c["ha_open"]) >= 5:
                sl = min(c["low"] for c in recent if c["ha_close"] < c["ha_open"]) - 0.0001
                signal = "buy"

        if signal and sl:
            risk_pct = 0.1 if timeframe == "1h" else 0.5
            if abs((last["close"] - sl) / last["close"]) > 0.05:
                print(f"[{timeframe}] Signal skipped (SL > 5%) at {last['time']}")
                continue

            rr = 1 if timeframe == "1h" else 2
            if signal == "buy":
                tp = last["close"] + (last["close"] - sl) * rr
                tp += last["close"] * 0.001
            else:
                tp = last["close"] - (sl - last["close"]) * rr
                tp -= last["close"] * 0.001

            print(f"[{timeframe}] SIGNAL: {signal.upper()} at {last['time']} | Entry={last['close']} SL={sl} TP={tp}")

            entry = last["close"]
            risk_amount = balance * risk_pct
            qty = risk_amount / abs(entry - sl)

            # simulate forward until TP/SL
            outcome = None
            for j in range(i + 1, len(ha_candles)):
                c = ha_candles[j]
                # buy
                if signal == "buy":
                    if c["low"] <= sl:
                        actual_loss = min(risk_amount, risk_amount * ((entry - sl) / abs(entry - sl)))
                        balance -= actual_loss
                        outcome = "SL"
                        print(f"[{timeframe}] SL HIT at {c['time']} | Balance now {balance:.2f}")
                        break
                    elif c["high"] >= tp:
                        actual_gain = risk_amount
                        balance += actual_gain
                        outcome = "TP"
                        print(f"[{timeframe}] TP HIT at {c['time']} | Balance now {balance:.2f}")
                        break
                # sell
                else:
                    if c["high"] >= sl:
                        actual_loss = risk_amount
                        balance -= actual_loss
                        outcome = "SL"
                        print(f"[{timeframe}] SL HIT at {c['time']} | Balance now {balance:.2f}")
                        break
                    elif c["low"] <= tp:
                        actual_gain = risk_amount
                        balance += actual_gain
                        outcome = "TP"
                        print(f"[{timeframe}] TP HIT at {c['time']} | Balance now {balance:.2f}")
                        break

            if outcome:
                trades.append((signal, last["time"], entry, sl, tp, outcome, balance))
        else:
            print(f"[{timeframe}] No signal at {last['time']}")

    return trades, balance

# ==============================
# Run Bot
# ==============================
def run_bot():
    # fetch both timeframes
    c1h = fetch_candles(interval="60")
    c4h = fetch_candles(interval="240")

    # hardcode HA opens
    ha_1h = heikin_ashi(c1h, initial_open=0.34695)
    ha_4h = heikin_ashi(c4h, initial_open=0.34779)

    print(f"First 1H candle time: {ha_1h[0]['time']}, HA open: {ha_1h[0]['ha_open']}")
    print(f"First 4H candle time: {ha_4h[0]['time']}, HA open: {ha_4h[0]['ha_open']}")

    balance_1h = 1000
    balance_4h = 1000

    trades_1h, balance_1h = run_backtest(ha_1h, "1h", balance_1h)
    trades_4h, balance_4h = run_backtest(ha_4h, "4h", balance_4h)

    print("\n--- FINAL RESULTS ---")
    print(f"1H Final Balance: {balance_1h:.2f} | Trades: {len(trades_1h)}")
    print(f"4H Final Balance: {balance_4h:.2f} | Trades: {len(trades_4h)}")

if __name__ == "__main__":
    run_bot()
