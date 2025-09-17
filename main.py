import requests
import pandas as pd
from datetime import datetime, timedelta
import pytz

# ==========================
# Config
# ==========================
symbol = "TRXUSDT"
timezone = pytz.timezone("Africa/Lagos")
initial_balance_1h = 1000
initial_balance_4h = 1000
risk_1h = 0.10
risk_4h = 0.50
fallback = 0.95

# ==========================
# Helpers
# ==========================
def fetch_candles(symbol, interval, days=30):
    url = f"https://api.binance.com/api/v3/klines"
    end_time = int(datetime.now().timestamp() * 1000)
    start_time = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": start_time,
        "endTime": end_time
    }
    data = requests.get(url, params=params).json()
    candles = []
    for d in data:
        candles.append({
            "time": datetime.fromtimestamp(d[0] / 1000, tz=timezone),
            "open": float(d[1]),
            "high": float(d[2]),
            "low": float(d[3]),
            "close": float(d[4])
        })
    return candles

def heikin_ashi(candles, initial_open=None):
    ha_candles = []
    ha_open = initial_open if initial_open else candles[0]["open"]

    for c in candles:
        ha_close = (c["open"] + c["high"] + c["low"] + c["close"]) / 4
        ha_open = (ha_open + ha_close) / 2
        ha_high = max(c["high"], ha_open, ha_close)
        ha_low = min(c["low"], ha_open, ha_close)
        ha_candles.append({
            "time": c["time"],
            "ha_open": ha_open,
            "ha_close": ha_close,
            "ha_high": ha_high,
            "ha_low": ha_low,
            "open": c["open"],
            "high": c["high"],
            "low": c["low"],
            "close": c["close"]
        })
    return ha_candles

def check_signal(candles, i, direction):
    # Buy -> last closed green after red sequence + 5 of last 8 green
    # Sell -> last closed red after green sequence + 5 of last 8 red
    if i < 8: return None

    prev = candles[i-1]
    curr = candles[i]

    if direction == "buy":
        if curr["ha_close"] > curr["ha_open"] and prev["ha_close"] < prev["ha_open"]:
            last8 = candles[i-8:i]
            green_count = sum(1 for x in last8 if x["ha_close"] > x["ha_open"])
            if green_count >= 5:
                sl = min(x["ha_low"] for x in last8) - 0.0001
                return {"type": "buy", "sl": sl, "entry": curr["ha_close"]}
    else:
        if curr["ha_close"] < curr["ha_open"] and prev["ha_close"] > prev["ha_open"]:
            last8 = candles[i-8:i]
            red_count = sum(1 for x in last8 if x["ha_close"] < x["ha_open"])
            if red_count >= 5:
                sl = max(x["ha_high"] for x in last8) + 0.0001
                return {"type": "sell", "sl": sl, "entry": curr["ha_close"]}
    return None

def run_backtest(candles, tf):
    balance = initial_balance_1h if tf == "1h" else initial_balance_4h
    risk = risk_1h if tf == "1h" else risk_4h
    rr = 1 if tf == "1h" else 2
    results = []

    in_trade = False
    trade = None

    for i in range(1, len(candles)):
        c = candles[i]

        # Log raw + HA
        print(f"[{c['time']}] {tf.upper()} Candle")
        print(f"RAW -> O:{c['open']:.5f} H:{c['high']:.5f} L:{c['low']:.5f} C:{c['close']:.5f}")
        print(f"HA  -> O:{c['ha_open']:.5f} H:{c['ha_high']:.5f} L:{c['ha_low']:.5f} C:{c['ha_close']:.5f}")
        print(f"Balance -> {tf.upper()}: {balance:.2f}")

        if in_trade:
            # Check SL or TP hit
            if trade["type"] == "buy":
                if c["low"] <= trade["sl"]:
                    balance -= trade["risk_amt"]
                    print(f"Signal -> BUY stopped out at {trade['sl']:.5f}, New Balance: {balance:.2f}")
                    in_trade = False
                elif c["high"] >= trade["tp"]:
                    balance += trade["risk_amt"]
                    print(f"Signal -> BUY TP hit at {trade['tp']:.5f}, New Balance: {balance:.2f}")
                    in_trade = False
            else:
                if c["high"] >= trade["sl"]:
                    balance -= trade["risk_amt"]
                    print(f"Signal -> SELL stopped out at {trade['sl']:.5f}, New Balance: {balance:.2f}")
                    in_trade = False
                elif c["low"] <= trade["tp"]:
                    balance += trade["risk_amt"]
                    print(f"Signal -> SELL TP hit at {trade['tp']:.5f}, New Balance: {balance:.2f}")
                    in_trade = False
        else:
            sig = check_signal(candles, i, "buy")
            if not sig:
                sig = check_signal(candles, i, "sell")

            if sig:
                # Calculate risk and TP
                risk_amt = balance * risk
                risk_amt = min(risk_amt, balance * fallback)

                stop_size = abs(sig["entry"] - sig["sl"])
                if sig["type"] == "buy":
                    tp = sig["entry"] + (stop_size * rr) + (sig["entry"] * 0.001)
                else:
                    tp = sig["entry"] - (stop_size * rr) - (sig["entry"] * 0.001)

                # Skip if SL > 5%
                if stop_size / sig["entry"] > 0.05:
                    print("Signal -> Skipped (SL > 5%)")
                else:
                    trade = {
                        "type": sig["type"],
                        "entry": sig["entry"],
                        "sl": sig["sl"],
                        "tp": tp,
                        "risk_amt": risk_amt
                    }
                    in_trade = True
                    print(f"Signal -> {sig['type'].upper()} at {sig['entry']:.5f} SL:{sig['sl']:.5f} TP:{tp:.5f}")

        print("-" * 60)

    return balance

# ==========================
# Main Run
# ==========================
c1h = fetch_candles(symbol, "1h")
c4h = fetch_candles(symbol, "4h")

print(f"First 1H candle: {c1h[0]['time']}, open={c1h[0]['open']}")
print(f"First 4H candle: {c4h[0]['time']}, open={c4h[0]['open']}")

# Hardcode initial HA open
ha_1h = heikin_ashi(c1h, initial_open=c1h[0]["open"])
ha_4h = heikin_ashi(c4h, initial_open=c4h[0]["open"])

final_balance_1h = run_backtest(ha_1h, "1h")
final_balance_4h = run_backtest(ha_4h, "4h")

print(f"Final Balance 1H: {final_balance_1h:.2f}")
print(f"Final Balance 4H: {final_balance_4h:.2f}")
