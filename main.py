import os
import time
import requests
import schedule
from datetime import datetime, timezone
from pybit.unified_trading import HTTP

# =========================
# Config
# =========================
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
SYMBOL = "TRXUSDT"
INTERVAL = 60   # 1h candles
LEVERAGE = 75

# Risk settings
RISK_PERCENT = 0.10
AFFORDABILITY = 0.95

# Initial HA open (set once, can be adjusted manually if needed)
INITIAL_OPEN = 0.34537

# =========================
# Bybit session
# =========================
session = HTTP(
    testnet=False,  # LIVE not testnet
    api_key=API_KEY,
    api_secret=API_SECRET
)

try:
    session.set_position_mode(symbol=SYMBOL, mode="MergedSingle")
except Exception as e:
    print("Position mode setup failed:", e)

# =========================
# Candle Fetch
# =========================
def fetch_candles(limit=100):
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "linear", "symbol": SYMBOL, "interval": str(INTERVAL), "limit": limit}
    r = requests.get(url, params=params).json()
    data = r["result"]["list"]
    candles = []
    for entry in reversed(data):
        candles.append({
            "time": datetime.fromtimestamp(int(entry[0]) / 1000, tz=timezone.utc),
            "open": float(entry[1]),
            "high": float(entry[2]),
            "low": float(entry[3]),
            "close": float(entry[4])
        })
    return candles

# =========================
# Heikin Ashi Conversion
# =========================
def heikin_ashi(candles):
    ha_candles = []
    for i, c in enumerate(candles):
        if i == 0:
            ha_open = INITIAL_OPEN
            ha_close = (c["open"] + c["high"] + c["low"] + c["close"]) / 4
        else:
            prev_ha = ha_candles[-1]
            ha_open = (prev_ha["open"] + prev_ha["close"]) / 2
            ha_close = (c["open"] + c["high"] + c["low"] + c["close"]) / 4

        ha_high = max(c["high"], ha_open, ha_close)
        ha_low = min(c["low"], ha_open, ha_close)

        ha_candles.append({
            "time": c["time"],
            "open": ha_open,
            "high": ha_high,
            "low": ha_low,
            "close": ha_close
        })
    return ha_candles

# =========================
# SL & TP Rules
# =========================
def compute_sl_tp(signal, candles):
    signal_candle = candles[-1]
    prev_candle = candles[-2]

    if signal == "buy":
        sl = min(signal_candle["low"], prev_candle["low"]) - 0.0001
        tp = signal_candle["close"] + (signal_candle["close"] - sl) * 2
        tp += signal_candle["close"] * 0.001
    else:
        sl = max(signal_candle["high"], prev_candle["high"]) + 0.0001
        tp = signal_candle["close"] - (sl - signal_candle["close"]) * 2
        tp -= signal_candle["close"] * 0.001

    return sl, tp

# =========================
# Position Sizing
# =========================
def compute_qty(entry, sl, balance):
    risk_amount = balance * RISK_PERCENT
    pip_risk = abs(entry - sl)
    qty = risk_amount / pip_risk
    max_qty = balance * AFFORDABILITY / entry
    return min(qty, max_qty)

# =========================
# Trade Execution
# =========================
def place_trade(signal, entry, sl, tp, qty, raw_candle, ha_candle):
    side = "Buy" if signal == "buy" else "Sell"
    try:
        session.cancel_all_orders(category="linear", symbol=SYMBOL)
        session.set_leverage(symbol=SYMBOL, buyLeverage=LEVERAGE, sellLeverage=LEVERAGE)
        session.set_trading_stop(symbol=SYMBOL, stopLoss=str(sl), takeProfit=str(tp), category="linear")
        session.place_order(category="linear", symbol=SYMBOL, side=side,
                            orderType="Market", qty=round(qty, 0), timeInForce="GTC", reduceOnly=False)

        log_trade(signal, entry, sl, tp, qty, raw_candle, ha_candle)
    except Exception as e:
        print("Trade error:", e)

# =========================
# Logging
# =========================
def log_trade(signal, entry, sl, tp, qty, raw_candle, ha_candle):
    with open("trades.log", "a") as f:
        f.write(
            f"{datetime.now()} | TRADE | {signal.upper()} | Entry={entry} SL={sl} TP={tp} QTY={qty}\n"
            f"RAW: O={raw_candle['open']} H={raw_candle['high']} L={raw_candle['low']} C={raw_candle['close']}\n"
            f"HA : O={ha_candle['open']} H={ha_candle['high']} L={ha_candle['low']} C={ha_candle['close']}\n"
            f"---\n"
        )

def log_status(range_dir, raw_candle, ha_candle):
    with open("status.log", "a") as f:
        f.write(
            f"{datetime.now()} | STATUS | Range={range_dir}\n"
            f"RAW: O={raw_candle['open']} H={raw_candle['high']} L={raw_candle['low']} C={raw_candle['close']}\n"
            f"HA : O={ha_candle['open']} H={ha_candle['high']} L={ha_candle['low']} C={ha_candle['close']}\n"
            f"---\n"
        )

# =========================
# Helper: Compute Range
# =========================
def compute_range(ha_candles):
    recent = ha_candles[-8:]
    greens = sum(1 for c in recent if c["close"] > c["open"])
    reds = 8 - greens

    if greens > reds:
        return "buy"
    elif reds > greens:
        return "sell"
    else:
        return "buy" if ha_candles[-1]["close"] > ha_candles[-1]["open"] else "sell"

# =========================
# Main Bot Loop
# =========================
def bot_loop():
    last_range = None
    balance = 1000  # TODO: fetch actual account balance via API

    def hourly_log():
        candles = fetch_candles()
        ha_candles = heikin_ashi(candles)
        current_range = compute_range(ha_candles)
        log_status(current_range, candles[-1], ha_candles[-1])

    # Schedule hourly logging
    schedule.every().hour.at(":00").do(hourly_log)

    while True:
        candles = fetch_candles()
        ha_candles = heikin_ashi(candles)
        current_range = compute_range(ha_candles)

        print(f"{datetime.now()} | Current Range={current_range} | Last Range={last_range}")

        if current_range != last_range:
            entry = ha_candles[-1]["close"]
            sl, tp = compute_sl_tp(current_range, ha_candles)
            qty = compute_qty(entry, sl, balance)
            print(f"🔄 New Range → {current_range.upper()} | Entry={entry} | SL={sl} | TP={tp} | Qty={qty}")
            place_trade(current_range, entry, sl, tp, qty, candles[-1], ha_candles[-1])
            last_range = current_range

        schedule.run_pending()
        time.sleep(60)

# =========================
# Run Bot
# =========================
if __name__ == "__main__":
    bot_loop()
    
