import os
import time
import logging
from datetime import datetime
from pybit.unified_trading import HTTP

# ================== CONFIG ==================
SYMBOL = "TRXUSDT"
RISK_PER_TRADE = 0.10   # 10% of balance
FALLBACK = 0.95         # fallback = 95% of balance if risk trade not affordable
RR = 2.0                # 2:1 RR
TP_EXTRA = 0.001        # +0.1%

INTERVAL = "3"          # Bybit 3-minute candles
CANDLE_SECONDS = 180    # 3 minutes
PIP = 0.0001            # 1 pip adjustment

# Initial HA open (used only on the first run)
INITIAL_HA_OPEN = 0.33743
ha_open_state = INITIAL_HA_OPEN   # rolling HA open
first_run = True

last_range = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

# ================== SESSION ==================
session = HTTP(
    testnet=False,
    api_key=os.getenv("BYBIT_API_KEY"),
    api_secret=os.getenv("BYBIT_API_SECRET")
)

# ================== HELPERS ==================
def fetch_candles(limit=20):
    """Fetch last candles from Bybit (earliest first)."""
    resp = session.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=limit)
    if "result" not in resp or "list" not in resp["result"]:
        raise Exception(f"Bad kline response: {resp}")
    candles = [
        (float(x[1]), float(x[2]), float(x[3]), float(x[4]), int(x[0]))  # O,H,L,C,TS
        for x in reversed(resp["result"]["list"])
    ]
    return candles

def compute_heikin_ashi(raw, ha_open):
    """Compute HA candles with continuity from last ha_open."""
    ha = []
    for (o, h, l, c, ts) in raw:
        ha_close = (o + h + l + c) / 4
        ha_open = (ha_open + (o + c) / 2) / 2
        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)
        ha.append({"ha_open": ha_open, "ha_high": ha_high, "ha_low": ha_low,
                   "ha_close": ha_close, "ts": ts, "raw": (o, h, l, c)})
    return ha, ha_open

def has_upper_wick(candle):
    return candle["ha_high"] > max(candle["ha_open"], candle["ha_close"])

def has_lower_wick(candle):
    return candle["ha_low"] < min(candle["ha_open"], candle["ha_close"])

def get_balance():
    resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    return float(resp["result"]["list"][0]["coin"][0]["walletBalance"])

def calculate_qty(balance, entry, sl):
    """Calculate position size with fallback (no double leverage issue)."""
    risk_amount = balance * RISK_PER_TRADE
    risk_per_unit = abs(entry - sl)

    if risk_per_unit <= 0:
        return 0

    qty = int(risk_amount / risk_per_unit * 75) - 1  # leverage * minus 1

    if qty < 1 or qty * entry > balance:
        qty = int((balance * FALLBACK) / entry * 75) - 1

    return max(qty, 1)

def place_order(side, entry, sl, tp, qty):
    try:
        logging.info("🚀 %s order | Entry=%.5f SL=%.5f TP=%.5f Qty=%d",
                     side.upper(), entry, sl, tp, qty)

        resp = session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=side.capitalize(),
            orderType="Market",
            qty=str(qty),
            timeInForce="IOC",
            reduceOnly=False,
            stopLoss=str(sl),
            takeProfit=str(tp),
            tpTriggerBy="LastPrice",
            slTriggerBy="LastPrice"
        )
        logging.info("Order response: %s", resp)
    except Exception as e:
        logging.error("Error placing order: %s", e)

# ================== CLOSE ALL POSITIONS ==================
def close_all_positions():
    """Close all open positions for the symbol."""
    positions = session.get_positions(category="linear", symbol=SYMBOL)
    for p in positions["result"]["list"]:
        long_size = float(p.get("longSize", 0))
        short_size = float(p.get("shortSize", 0))

        if long_size > 0:
            try:
                session.place_order(
                    category="linear",
                    symbol=SYMBOL,
                    side="Sell",
                    orderType="Market",
                    qty=str(long_size),
                    timeInForce="IOC",
                    reduceOnly=True
                )
                logging.info("Closed existing long position: %s contracts", long_size)
            except Exception as e:
                logging.error("Error closing long position: %s", e)

        if short_size > 0:
            try:
                session.place_order(
                    category="linear",
                    symbol=SYMBOL,
                    side="Buy",
                    orderType="Market",
                    qty=str(short_size),
                    timeInForce="IOC",
                    reduceOnly=True
                )
                logging.info("Closed existing short position: %s contracts", short_size)
            except Exception as e:
                logging.error("Error closing short position: %s", e)

# ================== CORE LOGIC ==================
def run_once():
    global last_range, ha_open_state, first_run

    logging.info("=== Running at %s ===", datetime.utcnow())

    raw_candles = fetch_candles(limit=8)

    # Log raw candles
    for i, r in enumerate(raw_candles, 1):
        logging.info("Raw %d | O=%.5f H=%.5f L=%.5f C=%.5f", i, r[0], r[1], r[2], r[3])

    # On first run, use INITIAL_HA_OPEN
    if first_run:
        ha_candles, ha_open_state = compute_heikin_ashi(raw_candles, INITIAL_HA_OPEN)
        first_run = False
    else:
        ha_candles, ha_open_state = compute_heikin_ashi(raw_candles, ha_open_state)

    # Log HA candles
    for i, c in enumerate(ha_candles, 1):
        logging.info("HA %d | O=%.5f H=%.5f L=%.5f C=%.5f", i, c["ha_open"], c["ha_high"], c["ha_low"], c["ha_close"])

    # determine trend
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

    # ✅ Close all positions before opening new trade
    close_all_positions()
    last_range = current_range

    balance = get_balance()
    last = ha_candles[-1]

    if current_range == "sell":
        sl = (raw_candles[-2][1] if has_upper_wick(last) else last["raw"][1]) + PIP
        entry = last["ha_close"]
        risk = abs(sl - entry)
        if risk == 0:
            logging.warning("Risk=0, skipping trade.")
            return
        tp = entry - (risk * RR) - (entry * TP_EXTRA)
        qty = calculate_qty(balance, entry, sl)
        place_order("Sell", entry, sl, tp, qty)

    elif current_range == "buy":
        sl = (raw_candles[-2][2] if has_lower_wick(last) else last["raw"][2]) - PIP
        entry = last["ha_close"]
        risk = abs(entry - sl)
        if risk == 0:
            logging.warning("Risk=0, skipping trade.")
            return
        tp = entry + (risk * RR) + (entry * TP_EXTRA)
        qty = calculate_qty(balance, entry, sl)
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
