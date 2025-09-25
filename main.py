import os
import time
import logging
from datetime import datetime, timedelta
from pybit.unified_trading import HTTP

# ================== CONFIG ==================
SYMBOL = "TRXUSDT"
RISK_PER_TRADE = 0.10   # 10% of balance
FALLBACK = 0.95         # fallback % if qty unaffordable
LEVERAGE = 75
INTERVAL = "60"          # 1h candles
CANDLE_SECONDS = 3600    # 1 hour
WINDOW = 8               # rolling HA candle window
INITIAL_HA_OPEN = 0.33822  # manually set
ROUNDING = 5

# ================== API KEYS ==================
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

# ================== GLOBAL STATE ==================
ha_candles = []
last_signal = None
initial_ha_open_time = None

# ================== FUNCTIONS ==================
def fetch_candles(limit=WINDOW+2):
    """Fetch last N raw candles from Bybit, sorted oldest → newest."""
    resp = session.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=limit)
    if "result" not in resp or "list" not in resp["result"]:
        raise Exception(f"Bad kline response: {resp}")
    candles = []
    for x in reversed(resp["result"]["list"]):
        if None in x[1:5]:
            continue
        candles.append({
            "time": int(x[0]),
            "o": float(x[1]),
            "h": float(x[2]),
            "l": float(x[3]),
            "c": float(x[4])
        })
    return candles

def build_initial_ha():
    """Build initial 8 HA candles using pasted INITIAL_HA_OPEN for oldest candle."""
    global ha_candles, initial_ha_open_time
    raw_candles = fetch_candles(limit=WINDOW)
    ha_candles = []

    for i, c in enumerate(raw_candles):
        ha_close = (c["o"] + c["h"] + c["l"] + c["c"]) / 4
        if i == 0:
            ha_open_candle = INITIAL_HA_OPEN
        else:
            prev = ha_candles[-1]["ha"]
            ha_open_candle = (prev["o"] + prev["c"]) / 2

        ha_high = max(c["h"], ha_open_candle, ha_close)
        ha_low  = min(c["l"], ha_open_candle, ha_close)

        candle = {
            "time": datetime.fromtimestamp(c["time"]/1000),
            "raw": {"o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]},
            "ha": {"o": ha_open_candle, "h": ha_high, "l": ha_low, "c": ha_close},
            "color": "green" if ha_close >= ha_open_candle else "red"
        }
        ha_candles.append(candle)

    initial_ha_open_time = ha_candles[0]["time"]
    logging.info(f"Initial HA Open set to {INITIAL_HA_OPEN} at {initial_ha_open_time}")

def log_candle(candle):
    logging.info(
        f"Candle Time={candle['time']} | "
        f"Raw=O:{candle['raw']['o']} H:{candle['raw']['h']} L:{candle['raw']['l']} C:{candle['raw']['c']} | "
        f"HA=O:{candle['ha']['o']} H:{candle['ha']['h']} L:{candle['ha']['l']} C:{candle['ha']['c']} | "
        f"Color={candle['color']}"
    )

def get_balance():
    resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    return float(resp["result"]["list"][0]["coin"][0]["walletBalance"])

def calc_qty(balance, entry, risk, risk_amount):
    qty = (risk_amount * LEVERAGE) / (risk * entry)
    max_qty = (balance * FALLBACK * LEVERAGE) / entry
    if qty * entry / LEVERAGE > balance:
        qty = max_qty
    return max(0, int(qty) - 1)

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
            takeProfit=str(tp)
        )
        logging.info("Order response: %s", resp)
    except Exception as e:
        logging.error("Error placing order: %s", e)

def process_new_candle_rolling():
    """Process just closed candle, compute signal, SL/TP, execute order, then update rolling HA list."""
    global ha_candles, last_signal

    raw_candles = fetch_candles(limit=2)
    raw_candle = raw_candles[-1]  # last fully closed candle

    ts, raw_o, raw_h, raw_l, raw_c = map(float, [raw_candle["time"], raw_candle["o"], raw_candle["h"], raw_candle["l"], raw_candle["c"]])

    ha_close = (raw_o + raw_h + raw_l + raw_c) / 4
    prev_ha = ha_candles[-1]["ha"]
    ha_open = (prev_ha["o"] + prev_ha["c"]) / 2
    ha_high = max(raw_h, ha_open, ha_close)
    ha_low = min(raw_l, ha_open, ha_close)
    color = "green" if ha_close >= ha_open else "red"

    candle = {
        "time": datetime.fromtimestamp(ts/1000),
        "raw": {"o": raw_o, "h": raw_h, "l": raw_l, "c": raw_c},
        "ha": {"o": ha_open, "h": ha_high, "l": ha_low, "c": ha_close},
        "color": color
    }

    log_candle(candle)

    # Determine signal
    green = sum(1 for c in ha_candles if c["color"] == "green")
    red   = sum(1 for c in ha_candles if c["color"] == "red")

    if green > red:
        signal = "buy"
    elif red > green:
        signal = "sell"
    else:
        signal = "buy" if candle["ha"]["c"] > candle["ha"]["o"] else "sell"

    logging.info(f"Signal={signal} | Last Signal={last_signal}")

    if signal != last_signal:
        last_signal = signal
        balance = get_balance()
        risk_amount = balance * RISK_PER_TRADE
        entry = candle["ha"]["c"]
        prev = ha_candles[-1]

        if signal == "buy":
            sl = min(candle['ha']['l'], prev['ha']['l']) - 0.0001
            risk = entry - sl
            tp = entry + (2 * risk) + (entry * 0.001)
            qty = calc_qty(balance, entry, risk, risk_amount)
            if qty > 0:
                place_order("Buy", entry, sl, tp, qty)

        elif signal == "sell":
            sl = max(candle['ha']['h'], prev['ha']['h']) + 0.0001
            risk = sl - entry
            tp = entry - (2 * risk) - (entry * 0.001)
            qty = calc_qty(balance, entry, risk, risk_amount)
            if qty > 0:
                place_order("Sell", entry, sl, tp, qty)

    # Update rolling HA list
    ha_candles.append(candle)
    if len(ha_candles) > WINDOW:
        ha_candles.pop(0)

def wait_until_next_candle():
    now = datetime.utcnow()
    next_close = (now.replace(minute=0, second=0, microsecond=0) 
                  + timedelta(hours=1))
    wait_sec = (next_close - now).total_seconds()
    logging.info(f"⏳ Waiting {int(wait_sec)}s until next 1h candle close...")
    time.sleep(wait_sec + 1)

# ================== MAIN LOOP ==================
def main():
    logging.info("Building initial HA candles...")
    build_initial_ha()
    for c in ha_candles:
        log_candle(c)

    logging.info("Starting live loop...")
    while True:
        wait_until_next_candle()
        try:
            process_new_candle_rolling()
        except Exception as e:
            logging.error(f"Error: {e}")

if __name__ == "__main__":
    main()
