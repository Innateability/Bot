import time
import hmac
import hashlib
import requests
import logging
import json
import os
from datetime import datetime

# ================== CONFIG ==================
API_KEY = "your_testnet_api_key"
API_SECRET = "your_testnet_api_secret"
SYMBOL = "TRXUSDT"
BASE_URL = "https://api-testnet.bybit.com"   # <-- TESTNET URL

RISK_PERCENT = 0.045   # 4.5% risk per trade
CAP_PERCENT = 0.45     # 45% of balance cap
SL_BUFFER = 0.0001
TP_EXTRA = 0.001       # 0.1% of entry price

STATE_FILE = "ha_state.json"

# Manual balance override for testing
TEST_BALANCE = 5.0
USE_TEST_BALANCE = True   # set False to fetch real Testnet balance

# Manual initial HA open (None = auto compute)
INITIAL_HA_OPEN = 0.33798 # e.g., 0.33097

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", mode="a")
    ]
)

# ================ UTILITIES =================
def sign_request(params):
    query = "&".join([f"{k}={params[k]}" for k in sorted(params)])
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    params["sign"] = signature
    return params

def api_get(endpoint, params=None):
    if params is None: params = {}
    params["api_key"] = API_KEY
    params["timestamp"] = int(time.time() * 1000)
    signed = sign_request(params)
    url = f"{BASE_URL}{endpoint}"
    resp = requests.get(url, params=signed).json()
    return resp

def api_post(endpoint, params):
    params["api_key"] = API_KEY
    params["timestamp"] = int(time.time() * 1000)
    signed = sign_request(params)
    url = f"{BASE_URL}{endpoint}"
    resp = requests.post(url, data=signed).json()
    return resp

# ================ STATE =================
def load_ha_open():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
            return state.get("ha_open")
    return None

def save_ha_open(ha_open):
    with open(STATE_FILE, "w") as f:
        json.dump({"ha_open": ha_open}, f)

# ================ HA CANDLES =================
def fetch_candles(limit=50, interval="60"):
    """Fetch raw candles and compute HA candles with persistence."""
    url = f"{BASE_URL}/v5/market/kline"
    params = {"category": "linear", "symbol": SYMBOL, "interval": interval, "limit": limit}
    resp = requests.get(url, params=params).json()
    raw = resp["result"]["list"]
    raw = raw[::-1]  # chronological order

    ha = []
    last_ha_open = load_ha_open()

    if last_ha_open is None:
        if INITIAL_HA_OPEN is not None:
            last_ha_open = INITIAL_HA_OPEN
            logging.info(f"Using manual INITIAL_HA_OPEN = {INITIAL_HA_OPEN}")
        else:
            last_ha_open = (float(raw[0][1]) + float(raw[0][4])) / 2
            logging.info(f"Auto-computed initial ha_open = {last_ha_open}")

    for c in raw:
        o, h, l, cl = map(float, c[1:5])
        ha_close = (o + h + l + cl) / 4
        ha_open = (last_ha_open + ha_close) / 2
        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)
        ha.append({"open": ha_open, "high": ha_high, "low": ha_low, "close": ha_close})
        last_ha_open = ha_open

    save_ha_open(last_ha_open)
    return ha

# ================ ACCOUNT =================
def get_balance():
    if USE_TEST_BALANCE:
        return TEST_BALANCE
    resp = api_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    return float(resp["result"]["list"][0]["totalEquity"])

def has_open_position(side):
    resp = api_get("/v5/position/list", {"category": "linear", "symbol": SYMBOL})
    for pos in resp["result"]["list"]:
        if pos["side"].lower() == side.lower() and float(pos["size"]) > 0:
            return True
    return False

# ================ ORDERS =================
def place_order(side, qty, sl, tp):
    params = {
        "category": "linear",
        "symbol": SYMBOL,
        "side": side,
        "orderType": "Market",
        "qty": str(qty),
        "timeInForce": "GoodTillCancel",
        "reduceOnly": False,
        "positionIdx": 0,   # hedge mode
        "stopLoss": str(sl),
        "takeProfit": str(tp)
    }
    resp = api_post("/v5/order/create", params)
    logging.info(f"Placed {side} order | Qty={qty} | SL={sl} | TP={tp}")
    return resp

def compute_qty(balance, entry, sl):
    risk_capital = balance * RISK_PERCENT
    cap = balance * CAP_PERCENT
    risk_per_unit = abs(entry - sl)
    if risk_per_unit == 0: return 0
    qty = risk_capital / risk_per_unit
    if qty * entry > cap:
        qty = cap / entry
    return round(qty, 0)

# ================ SIGNAL CHECK =================
def check_signals():
    candles = fetch_candles()
    if len(candles) < 3: return

    last = candles[-1]
    prev = candles[-2]

    balance = get_balance()

    # BUY SIGNAL
    if last["close"] > last["open"] and prev["close"] < prev["open"] and last["low"] > prev["low"]:
        if not has_open_position("Buy"):
            entry = last["close"]
            sl = prev["low"] - SL_BUFFER
            tp = entry + (entry - sl) * 2 + entry * TP_EXTRA
            qty = compute_qty(balance, entry, sl)
            if qty > 0:
                logging.info(f"Buy signal ✅ | Entry={entry} | SL={sl} | TP={tp} | Balance={balance}")
                place_order("Buy", qty, sl, tp)
            else:
                logging.info("Buy skipped (qty=0)")

    # SELL SIGNAL
    if last["close"] < last["open"] and prev["close"] > prev["open"] and last["high"] < prev["high"]:
        if not has_open_position("Sell"):
            entry = last["close"]
            sl = prev["high"] + SL_BUFFER
            tp = entry - (sl - entry) * 2 - entry * TP_EXTRA
            qty = compute_qty(balance, entry, sl)
            if qty > 0:
                logging.info(f"Sell signal ✅ | Entry={entry} | SL={sl} | TP={tp} | Balance={balance}")
                place_order("Sell", qty, sl, tp)
            else:
                logging.info("Sell skipped (qty=0)")

# ================ MAIN LOOP =================
if __name__ == "__main__":
    logging.info("Bot started in TESTNET mode ✅")
    while True:
        try:
            check_signals()
        except Exception as e:
            logging.error(f"Error: {e}")
        time.sleep(60)  # run every 1 min
