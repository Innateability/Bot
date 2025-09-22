import os
import time
import math
import logging
from datetime import datetime, timezone
from pybit.unified_trading import HTTP

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger(__name__)

# ---------------- CONFIG ----------------
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
SYMBOL = "TRXUSDT"
RISK_PER_TRADE = 0.10  # 10%
FALLBACK = 0.95
TP_RR = 2.0
TP_EXTRA = 0.001  # +0.1%
INITIAL_HA_OPEN = 0.33961  # 👈 set this manually for consistency with TradingView

session = HTTP(api_key=API_KEY, api_secret=API_SECRET)

last_range = None
first_logged = False  # ensures first candle log only once

# ---------------- HEIKIN ASHI ----------------
def heikin_ashi_transform(raw_candles, initial_open):
    ha = []
    ha_open = initial_open
    for o, h, l, c, ts in raw_candles:
        ha_close = (o + h + l + c) / 4
        ha_open = (ha_open + ha_close) / 2
        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)
        ha.append({
            "ts": datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
            "raw": (o, h, l, c),
            "ha": (ha_open, ha_high, ha_low, ha_close)
        })
    return ha

# ---------------- SIGNAL DETECTION ----------------
def detect_signal(ha_candles):
    global last_range

    last8 = ha_candles[-8:]
    colors = ["g" if c["ha"][3] >= c["ha"][0] else "r" for c in last8]
    greens = colors.count("g")
    reds = colors.count("r")
    last_color = colors[-1]

    if greens > reds:
        current = "buy"
    elif reds > greens:
        current = "sell"
    else:
        current = "buy" if last_color == "g" else "sell"

    logger.info("Current Range=%s | Last Range=%s", current, last_range)

    if current != last_range:
        last_range = current
        return current
    return None

# ---------------- SL & TP LOGIC ----------------
def calc_sl_tp(signal, ha_candles):
    last_raw = ha_candles[-1]["raw"]
    prev_raw = ha_candles[-2]["raw"]
    o, h, l, c = last_raw
    po, ph, pl, pc = prev_raw

    if signal == "sell":
        upper_wick = h > max(o, c)
        sl = ph if upper_wick else h
        rr = abs(c - sl)
        tp = c - rr * TP_RR - (c * TP_EXTRA)
        return c, sl, tp

    elif signal == "buy":
        lower_wick = l < min(o, c)
        sl = pl if lower_wick else l
        rr = abs(sl - c)
        tp = c + rr * TP_RR + (c * TP_EXTRA)
        return c, sl, tp

# ---------------- ORDER ----------------
def place_trade(signal, ha_candles):
    balance = get_balance()
    if not balance:
        return
    risk_amount = balance * RISK_PER_TRADE
    entry, sl, tp = calc_sl_tp(signal, ha_candles)

    # position size
    qty = risk_amount / abs(entry - sl)
    qty = math.floor(qty * FALLBACK * 100) / 100  # round down

    logger.info("🚀 %s order | Entry=%s SL=%s TP=%s Qty=%s", signal.upper(), entry, sl, tp, qty)

    side = "Buy" if signal == "buy" else "Sell"
    try:
        session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=side,
            orderType="Market",
            qty=qty,
            takeProfit=tp,
            stopLoss=sl,
            tpTriggerBy="LastPrice",
            slTriggerBy="LastPrice",
            reduceOnly=False
        )
    except Exception as e:
        logger.error("Order error: %s", e)

# ---------------- BALANCE ----------------
def get_balance():
    try:
        resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        return float(resp["result"]["list"][0]["coin"][0]["equity"])
    except Exception as e:
        logger.error("Balance fetch error: %s", e)
        return None

# ---------------- MAIN LOOP ----------------
def run_once():
    global first_logged
    now = datetime.now(timezone.utc)
    logger.info("=== Running at %s ===", now.strftime("%Y-%m-%d %H:%M:%S"))

    # fetch candles
    resp = session.get_kline(category="linear", symbol=SYMBOL, interval="60", limit=20)
    raw_candles = [
        (float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]), int(x["start"]))
        for x in reversed(resp["result"]["list"])
    ]

    # log the first candle once for HA seeding
    if not first_logged:
        first_candle = raw_candles[0]
        logger.info("📌 First raw candle for HA seed: %s", first_candle)
        first_logged = True

    # build HA
    ha_candles = heikin_ashi_transform(raw_candles, INITIAL_HA_OPEN)

    # log last 8
    for c in ha_candles[-8:]:
        logger.info("RAW %s | %s | HA %s", c["ts"], c["raw"], c["ha"])

    signal = detect_signal(ha_candles)
    if signal:
        place_trade(signal, ha_candles)
    else:
        logger.info("No trend change — no trade")

def main():
    while True:
        now = datetime.now(timezone.utc)
        wait = 3600 - (now.minute * 60 + now.second)
        logger.info("⏳ Waiting %ss until next full hour...", wait)
        time.sleep(wait + 1)
        run_once()

if __name__ == "__main__":
    main()
