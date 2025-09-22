import os
import time
import logging
import math
from datetime import datetime, timezone
from pybit.unified_trading import HTTP

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ---------------- API KEYS ----------------
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

if not API_KEY or not API_SECRET:
    raise ValueError("❌ API keys not found in environment variables.")

session = HTTP(
    testnet=False,
    api_key=API_KEY,
    api_secret=API_SECRET
)

# ---------------- SETTINGS ----------------
SYMBOL = "TRXUSDT"
RISK = 0.10           # 10% of balance
FALLBACK = 0.95       # fallback if not enough balance
INITIAL_HA_OPEN = 0.3460 # <- you must set manually for consistency
LAST_7_COLORS = "rrggrrg"  # <- example input, update before running
TIMEFRAME = 60        # 1 hour (minutes)
LAST_RANGE = None     # persisted trend state

# ---------------- HA CALCULATION ----------------
def heikin_ashi_transform(candles, initial_ha_open):
    """Convert raw candles to HA candles forward from initial HA open."""
    ha_candles = []
    ha_open = initial_ha_open

    for o, h, l, c, ts in candles:
        ha_close = (o + h + l + c) / 4
        ha_open = (ha_open + ha_close) / 2
        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)
        ha_candles.append({
            "ts": ts,
            "raw": (o, h, l, c),
            "ha": (ha_open, ha_high, ha_low, ha_close)
        })

    return ha_candles

# ---------------- SIGNAL LOGIC ----------------
def detect_signal(ha_candles):
    global LAST_RANGE
    last_8 = ha_candles[-8:]

    # count red vs green
    red = sum(1 for c in last_8 if c["ha"][3] < c["ha"][0])
    green = len(last_8) - red

    if red > green:
        new_range = "sell"
    elif green > red:
        new_range = "buy"
    else:
        last_close = last_8[-1]["ha"][3]
        last_open = last_8[-1]["ha"][0]
        new_range = "buy" if last_close > last_open else "sell"

    if LAST_RANGE != new_range:
        LAST_RANGE = new_range
        return new_range
    return None  # no new signal

# ---------------- WICK-BASED SL ----------------
def get_stoploss(signal, ha_candles):
    last = ha_candles[-1]["ha"]
    prev = ha_candles[-2]["ha"]

    ha_open, ha_high, ha_low, ha_close = last
    prev_high, prev_low = ha_candles[-2]["raw"][1], ha_candles[-2]["raw"][2]
    last_high, last_low = ha_candles[-1]["raw"][1], ha_candles[-1]["raw"][2]

    if signal == "sell":
        has_upper_wick = ha_high > max(ha_open, ha_close)
        return prev_high if has_upper_wick else last_high

    elif signal == "buy":
        has_lower_wick = ha_low < min(ha_open, ha_close)
        return prev_low if has_lower_wick else last_low

# ---------------- ACCOUNT + ORDER ----------------
def get_balance():
    resp = session.get_wallet_balance(accountType="UNIFIED")
    balance = float(resp["result"]["list"][0]["coin"][0]["equity"])
    return balance

def place_trade(signal, ha_candles):
    balance = get_balance()
    entry = ha_candles[-1]["raw"][3]  # close price
    sl = get_stoploss(signal, ha_candles)
    risk_amt = balance * RISK

    # qty = risk / |entry - sl|
    stop_distance = abs(entry - sl)
    qty = risk_amt / stop_distance if stop_distance else 0
    qty = max(0, math.floor(qty * entry))  # approximate qty

    if qty <= 0:
        logger.warning("⚠️ Qty too small, skipping trade.")
        return

    # TP = 2 * RR + 0.1%
    rr = 2
    tp = entry + (entry - sl) * rr if signal == "buy" else entry - (sl - entry) * rr
    tp *= 1.001  # add +0.1%

    side = "Buy" if signal == "buy" else "Sell"
    logger.info("Placing %s trade | entry=%.5f sl=%.5f tp=%.5f qty=%.2f balance=%.2f",
                side, entry, sl, tp, qty, balance)

    try:
        resp = session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=side,
            orderType="Market",
            qty=str(qty),
            timeInForce="IOC",
            reduceOnly=False
        )
        logger.info("✅ Order response: %s", resp)
    except Exception as e:
        logger.exception("❌ Order placement failed")

# ---------------- MAIN LOOP ----------------
def run_once():
    now = datetime.now(timezone.utc)
    logger.info("=== Running at %s ===", now.strftime("%Y-%m-%d %H:%M:%S"))

    # fetch candles
    resp = session.get_kline(
        category="linear",
        symbol=SYMBOL,
        interval="60",
        limit=20
    )
    raw_candles = [
        (float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]), int(x["start"]))
        for x in reversed(resp["result"]["list"])
    ]

    # 👉 Log the raw OHLC of the FIRST candle (for INITIAL_HA_OPEN reference)
    first_candle = raw_candles[0]
    logger.info("First raw candle used for HA computation: %s", first_candle)

    # build HA candles
    ha_candles = heikin_ashi_transform(raw_candles, INITIAL_HA_OPEN)

    # log candles
    for c in ha_candles[-8:]:
        logger.info("RAW %s | %s | HA %s", c["ts"], c["raw"], c["ha"])

    # detect signal
    signal = detect_signal(ha_candles)
    if signal:
        place_trade(signal, ha_candles)
    else:
        logger.info("No trend change — no trade")

def wait_until_next_hour():
    now = datetime.now(timezone.utc)
    to_wait = 3600 - (now.minute * 60 + now.second)
    logger.info("Sleeping %d seconds until next hour", to_wait)
    time.sleep(to_wait)

if __name__ == "__main__":
    while True:
        try:
            run_once()
        except Exception:
            logger.exception("Error in run_once")
        wait_until_next_hour()
