import os
import time
import hmac
import hashlib
import requests
from datetime import datetime, timedelta

# =========================
# CONFIG
# =========================
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
BASE_URL = "https://api.bybit.com"

SYMBOL = "TRXUSDT"
RISK_PERCENT = 0.1
INITIAL_HA_OPEN = 0.3551  # 👈 Hardcode your initial HA open here

# =========================
# BYBIT API HELPERS
# =========================
def send_signed_request(method, endpoint, params=None):
    if params is None:
        params = {}
    timestamp = str(int(time.time() * 1000))
    params["api_key"] = API_KEY
    params["timestamp"] = timestamp
    sorted_params = sorted(params.items())
    query = "&".join([f"{k}={v}" for k, v in sorted_params])
    signature = hmac.new(
        API_SECRET.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    params["sign"] = signature

    url = BASE_URL + endpoint
    if method == "POST":
        response = requests.post(url, data=params)
    else:
        response = requests.get(url, params=params)
    return response.json()

def fetch_candles():
    endpoint = "/v5/market/kline"
    params = {"symbol": SYMBOL, "interval": "60", "limit": 200}
    r = send_signed_request("GET", endpoint, params)
    candles = []
    for c in r["result"]["list"][::-1]:  # reverse to oldest → newest
        candles.append({
            "time": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
        })
    return candles

def fetch_balance():
    endpoint = "/v5/account/wallet-balance"
    params = {"accountType": "UNIFIED", "coin": "USDT"}
    r = send_signed_request("GET", endpoint, params)
    balance = float(r["result"]["list"][0]["coin"][0]["walletBalance"])
    return balance

# =========================
# HEIKIN ASHI CONVERSION
# =========================
def convert_to_heikin_ashi(candles):
    ha_candles = []
    for i, c in enumerate(candles):
        ha_close = (c["open"] + c["high"] + c["low"] + c["close"]) / 4
        if i == 0:
            ha_open = INITIAL_HA_OPEN   # 👈 uses hardcoded starting value
        else:
            ha_open = (ha_candles[-1]["open"] + ha_candles[-1]["close"]) / 2
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
# STRATEGY LOGIC
# =========================
def compute_range(ha_candles, lookback=8):
    recent = ha_candles[-lookback:]
    greens = sum(1 for c in recent if c["close"] > c["open"])
    reds = lookback - greens
    if greens > reds:
        return "buy"
    elif reds > greens:
        return "sell"
    else:
        return "buy" if ha_candles[-1]["close"] > ha_candles[-1]["open"] else "sell"

def compute_sl_tp(direction, ha_candles):
    last = ha_candles[-1]
    prev = ha_candles[-2]
    if direction == "buy":
        if last["low"] == last["close"]:  # no wick
            sl = last["low"]
        else:
            sl = prev["low"]
        rr = (last["close"] - sl)
        tp = last["close"] + (2 * rr) + (0.001 * last["close"])
    else:
        if last["high"] == last["close"]:  # no wick
            sl = last["high"]
        else:
            sl = prev["high"]
        rr = (sl - last["close"])
        tp = last["close"] - (2 * rr) - (0.001 * last["close"])
    return sl, tp

def compute_qty(entry, sl, balance):
    risk_amount = balance * RISK_PERCENT
    stop_distance = abs(entry - sl)
    if stop_distance == 0:
        return 1
    qty = risk_amount / stop_distance
    return max(1, int(qty))  # ✅ always at least 1 whole contract

def place_trade(direction, entry, sl, tp, qty, raw_last, ha_last):
    print(f"🚀 {direction.upper()} order | Entry={entry} SL={sl} TP={tp} Qty={qty}")
    # Live trading would be placed here – for now we simulate

# =========================
# LOGGING
# =========================
def log_ohlc(raw_candles, ha_candles, tag="CANDLES"):
    with open("ohlc.log", "a") as f:
        f.write(f"{datetime.now()} | {tag} | {len(raw_candles)} candles logged\n")
        for i in range(len(raw_candles)):
            rc = raw_candles[i]
            hc = ha_candles[i]
            f.write(
                f"RAW {i}: O={rc['open']:.5f} H={rc['high']:.5f} "
                f"L={rc['low']:.5f} C={rc['close']:.5f} | "
                f"HA: O={hc['open']:.5f} H={hc['high']:.5f} "
                f"L={hc['low']:.5f} C={hc['close']:.5f}\n"
            )
        f.write("---\n")

# =========================
# BOT LOOP
# =========================
def wait_for_next_hour():
    now = datetime.now()
    next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    wait_time = (next_hour - now).total_seconds()
    print(f"⏳ Waiting {int(wait_time)}s until next full hour...")
    time.sleep(wait_time)

def bot_loop():
    last_range = None
    first_test_done = False

    while True:
        wait_for_next_hour()
        candles = fetch_candles()
        ha_candles = convert_to_heikin_ashi(candles)
        current_range = compute_range(ha_candles)

        log_ohlc(candles, ha_candles, tag="HOURLY_LOG")

        print(f"{datetime.now()} | Current Range={current_range} | Last Range={last_range}")

        # ✅ Run the one-time 16 contracts test trade
        if not first_test_done:
            entry = ha_candles[-1]["close"]
            sl, tp = compute_sl_tp("sell", ha_candles)
            qty = 16  # fixed for test
            place_trade("sell", entry, sl, tp, qty, candles[-1], ha_candles[-1])
            first_test_done = True

        # ✅ Live trading starts after test
        elif current_range != last_range:
            balance = fetch_balance()
            entry = ha_candles[-1]["close"]
            sl, tp = compute_sl_tp(current_range, ha_candles)
            qty = compute_qty(entry, sl, balance)
            place_trade(current_range, entry, sl, tp, qty, candles[-1], ha_candles[-1])
            last_range = current_range

# =========================
# START
# =========================
if __name__ == "__main__":
    bot_loop()
