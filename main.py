import os
import time
import logging
from datetime import datetime, timedelta
from pybit.unified_trading import HTTP

# ================== CONFIG ==================
SYMBOL = "TRXUSDT"
RISK_PER_TRADE = 0.10   # 10%
FALLBACK = 0.95         # 95% fallback if not enough balance
RR = 2.0                # Risk-reward ratio
TP_EXTRA = 0.001        # +0.1%

INTERVAL = "3"          # Bybit 3-minute
CANDLE_SECONDS = 180    # 3 minutes in seconds

# Initial HA open (hardcode from TradingView if needed)
INITIAL_HA_OPEN = 0.3398

# State for trend tracking
last_range = None

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

# ================== SESSION ==================
session = HTTP(
    testnet=False,
    api_key=os.getenv("BYBIT_API_KEY"),
    api_secret=os.getenv("BYBIT_API_SECRET")
)


# ================== HELPERS ==================
def fetch_candles(limit=20):
    """Fetch last candles from Bybit."""
    resp = session.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=limit)
    if "result" not in resp or "list" not in resp["result"]:
        raise Exception(f"Bad kline response: {resp}")
    candles = [
        (float(x[1]), float(x[2]), float(x[3]), float(x[4]), int(x[0]))  # O, H, L, C, TS
        for x in reversed(resp["result"]["list"])
    ]
    return candles


def compute_heikin_ashi(raw, initial_ha_open):
    """Compute HA candles using persisted HA open."""
    ha = []
    ha_open = initial_ha_open
    for (o, h, l, c, ts) in raw:
        ha_close = (o + h + l + c) / 4
        ha_open = (ha_open + (o + c) / 2) / 2
        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)
        ha.append({"ha_open": ha_open, "ha_high": ha_high, "ha_low": ha_low,
                   "ha_close": ha_close, "ts": ts, "raw": (o, h, l, c)})
    return ha


def has_upper_wick(candle):
    return candle["ha_high"] > max(candle["ha_open"], candle["ha_close"])


def has_lower_wick(candle):
    return candle["ha_low"] < min(candle["ha_open"], candle["ha_close"])


def get_balance():
    resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    return float(resp["result"]["list"][0]["coin"][0]["walletBalance"])


def place_order(side, entry, sl, tp, qty):
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
        logging.info("%s order placed | Entry=%.5f SL=%.5f TP=%.5f Qty=%.2f", side, entry, sl, tp, qty)
        logging.info("Order response: %s", resp)
    except Exception as e:
        logging.error("Error placing order: %s", e)


# ================== CORE LOGIC ==================
def run_once():
    global last_range, INITIAL_HA_OPEN

    logging.info("=== Running at %s ===", datetime.utcnow())

    raw_candles = fetch_candles(limit=8)
    logging.info("Raw first candle (for HA init): O=%.5f H=%.5f L=%.5f C=%.5f",
                 *raw_candles[0][:4])

    ha_candles = compute_heikin_ashi(raw_candles, INITIAL_HA_OPEN)

    # Update HA open for next round
    INITIAL_HA_OPEN = ha_candles[-1]["ha_open"]

    for i, c in enumerate(ha_candles, 1):
        logging.info("HA %d | O=%.5f H=%.5f L=%.5f C=%.5f | Raw=%s",
                     i, c["ha_open"], c["ha_high"], c["ha_low"], c["ha_close"], c["raw"])

    green = sum(1 for c in ha_candles if c["ha_close"] > c["ha_open"])
    red = sum(1 for c in ha_candles if c["ha_close"] < c["ha_open"])

    if green > red:
        current_range = "buy"
    elif red > green:
        current_range = "sell"
    else:
        current_range = "buy" if ha_candles[-1]["ha_close"] > ha_candles[-1]["ha_open"] else "sell"

    logging.info("Current Range=%s | Last Range=%s", current_range, last_range)

    if current_range == last_range:
        return
    last_range = current_range

    balance = get_balance()
    risk_amount = balance * RISK_PER_TRADE
    last = ha_candles[-1]

    if current_range == "sell":
        sl = last["raw"][1] if has_upper_wick(last) else last["ha_high"]
        entry = last["ha_close"]
        risk = abs(sl - entry)
        tp = entry - (risk * RR) - (entry * TP_EXTRA)
        qty = max(1, (risk_amount / risk) * FALLBACK)
        place_order("Sell", entry, sl, tp, qty)

    elif current_range == "buy":
        sl = last["raw"][2] if has_lower_wick(last) else last["ha_low"]
        entry = last["ha_close"]
        risk = abs(entry - sl)
        tp = entry + (risk * RR) + (entry * TP_EXTRA)
        qty = max(1, (risk_amount / risk) * FALLBACK)
        place_order("Buy", entry, sl, tp, qty)


# ================== MAIN LOOP ==================
def main():
    while True:
        now = datetime.utcnow()
        sec_into_cycle = (now.minute % 3) * 60 + now.second
        wait = CANDLE_SECONDS - sec_into_cycle
        if wait <= 0:
            wait += CANDLE_SECONDS
        logging.info("⏳ Waiting %ds until next 3m candle close...", wait)
        time.sleep(wait)
        run_once()


if __name__ == "__main__":
    main()
