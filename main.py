import time
import hmac
import hashlib
import requests
import logging
from datetime import datetime

# ================== CONFIG ==================
API_KEY = "your_api_key"
API_SECRET = "your_api_secret"
SYMBOL = "TRXUSDT"
BASE_URL = "https://api.bybit.com"
RISK_PERCENT = 0.045   # 4.5% risk per trade
CAP_PERCENT = 0.45     # 45% available balance cap
SL_BUFFER = 0.0001
TP_EXTRA = 0.001       # 0.1% of entry price

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# ================ UTILITIES =================
def sign_request(params):
    """Signs request with HMAC SHA256."""
    query = "&".join([f"{k}={params[k]}" for k in sorted(params)])
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    params["sign"] = signature
    return params

def get_server_time():
    resp = requests.get(f"{BASE_URL}/v5/market/time")
    return int(resp.json()["result"]["timeSecond"])

def api_get(endpoint, params=None):
    if params is None: params = {}
    params["api_key"] = API_KEY
    params["timestamp"] = int(time.time() * 1000)
    signed = sign_request(params)
    url = f"{BASE_URL}{endpoint}"
    resp = requests.get(url, params=signed)
    return resp.json()

def api_post(endpoint, params):
    params["api_key"] = API_KEY
    params["timestamp"] = int(time.time() * 1000)
    signed = sign_request(params)
    url = f"{BASE_URL}{endpoint}"
    resp = requests.post(url, data=signed)
    return resp.json()

# ================ HA CANDLES =================
def fetch_candles(limit=50, interval="60"):
    """Fetch raw candles and compute HA candles."""
    url = f"{BASE_URL}/v5/market/kline"
    params = {"category": "linear", "symbol": SYMBOL, "interval": interval, "limit": limit}
    resp = requests.get(url, params=params).json()
    raw = resp["result"]["list"]
    raw = raw[::-1]  # reverse to chronological

    ha = []
    ha_open = (float(raw[0][1]) + float(raw[0][4])) / 2  # seed = avg(open, close)
    ha_close = (float(raw[0][1]) + float(raw[0][2]) + float(raw[0][3]) + float(raw[0][4])) / 4
    for c in raw:
        o, h, l, cl = map(float, c[1:5])
        ha_close = (o + h + l + cl) / 4
        ha_open = (ha_open + ha_close) / 2
        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)
        ha.append({"open": ha_open, "high": ha_high, "low": ha_low, "close": ha_close})
    return ha

# ================ TRADING LOGIC ================
def get_balance():
    resp = api_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    return float(resp["result"]["list"][0]["totalEquity"])

def has_open_position(side):
    resp = api_get("/v5/position/list", {"category": "linear", "symbol": SYMBOL})
    for pos in resp["result"]["list"]:
        if pos["side"].lower() == side.lower() and float(pos["size"]) > 0:
            return True
    return False

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
    if qty * entry > cap:  # fallback
        qty = cap / entry
    return round(qty, 0)

# ================ SIGNAL CHECK =================
def check_signals():
    candles = fetch_candles()
    if len(candles) < 3: return

    last = candles[-1]
    prev = candles[-2]

    balance = get_balance()

    # BUY SIGNAL: green after red sequence + higher low
    if last["close"] > last["open"] and prev["close"] < prev["open"] and last["low"] > prev["low"]:
        if not has_open_position("Buy"):
            entry = last["close"]
            sl = prev["low"] - SL_BUFFER
            tp = entry + (entry - sl) * 2 + entry * TP_EXTRA
            qty = compute_qty(balance, entry, sl)
            if qty > 0:
                logging.info("Buy signal detected ✅")
                place_order("Buy", qty, sl, tp)
            else:
                logging.info("Buy skipped (qty=0)")

    # SELL SIGNAL: red after green sequence + lower high
    if last["close"] < last["open"] and prev["close"] > prev["open"] and last["high"] < prev["high"]:
        if not has_open_position("Sell"):
            entry = last["close"]
            sl = prev["high"] + SL_BUFFER
            tp = entry - (sl - entry) * 2 - entry * TP_EXTRA
            qty = compute_qty(balance, entry, sl)
            if qty > 0:
                logging.info("Sell signal detected ✅")
                place_order("Sell", qty, sl, tp)
            else:
                logging.info("Sell skipped (qty=0)")

# ================ MAIN LOOP =================
if __name__ == "__main__":
    logging.info("Bot started ✅")
    while True:
        try:
            check_signals()
        except Exception as e:
            logging.error(f"Error: {e}")
        time.sleep(60)  # run every 1 min
