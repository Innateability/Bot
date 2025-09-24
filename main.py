import os
import time
import logging
from datetime import datetime
from pybit.unified_trading import HTTP

# ================== CONFIG ==================
SYMBOL = "TRXUSDT"
RISK_PER_TRADE = 0.10    # 10% of balance
FALLBACK = 0.95          # fallback if qty unaffordable
RR = 2.0                 # risk:reward
TP_EXTRA = 0.001         # +0.1% on TP
SL_PIP = 0.0001          # extra buffer on SL
ROUNDING = 5
LEVERAGE = 75
INTERVAL = 3             # 3m timeframe
WINDOW = 8               # number of HA candles to keep rolling

# ====== MANUAL INITIAL HA OPEN ======
INITIAL_HA_OPEN = 0.33611

# ================== API KEYS ==================
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

# ================== SESSION ==================
session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

# ================== LOGGING ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s"
)

# ================== GLOBAL STATE ==================
ha_candles = []        # rolling list of last 8 HA candles
ha_open_state = INITIAL_HA_OPEN
last_signal = None

# ================== FUNCTIONS ==================
def fetch_candles(limit=WINDOW):
    """Fetch the last N raw candles from Bybit"""
    resp = session.get_kline(category="linear", symbol=SYMBOL, interval=str(INTERVAL), limit=limit)
    if "result" not in resp or "list" not in resp["result"]:
        raise Exception(f"Bad kline response: {resp}")
    # Return oldest → newest
    return [
        {"time": int(c[0]), "o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": float(c[4])}
        for c in reversed(resp["result"]["list"])
    ]

def build_initial_ha():
    """Compute initial 8 HA candles using manual INITIAL_HA_OPEN"""
    global ha_candles, ha_open_state
    raw = fetch_candles(limit=WINDOW)
    ha_open = INITIAL_HA_OPEN
    ha_candles.clear()

    for c in raw:
        ha_close = (c["o"] + c["h"] + c["l"] + c["c"]) / 4
        ha_open = (ha_open + ha_close) / 2
        ha_high = max(c["h"], ha_open, ha_close)
        ha_low = min(c["l"], ha_open, ha_close)
        color = "green" if ha_close >= ha_open else "red"
        ha_candles.append({
            "time": c["time"],
            "raw": c,
            "ha": {"o": ha_open, "h": ha_high, "l": ha_low, "c": ha_close},
            "color": color
        })
    ha_open_state = ha_candles[-1]["ha"]["o"]

def process_new_candle():
    """Compute HA for the newly closed candle and update rolling list"""
    global ha_candles, ha_open_state, last_signal
    raw_candle = fetch_candles(limit=1)[0]  # last closed candle
    ha_close = (raw_candle["o"] + raw_candle["h"] + raw_candle["l"] + raw_candle["c"]) / 4
    ha_open_state = (ha_open_state + ha_close) / 2
    ha_high = max(raw_candle["h"], ha_open_state, ha_close)
    ha_low = min(raw_candle["l"], ha_open_state, ha_close)
    color = "green" if ha_close >= ha_open_state else "red"

    candle = {
        "time": raw_candle["time"],
        "raw": raw_candle,
        "ha": {"o": ha_open_state, "h": ha_high, "l": ha_low, "c": ha_close},
        "color": color
    }

    ha_candles.append(candle)
    if len(ha_candles) > WINDOW:
        ha_candles.pop(0)

    log_candle(candle)
    compute_signal_and_trade(candle)

def log_candle(candle):
    logging.info(
        f"Candle Time={datetime.utcfromtimestamp(candle['time']/1000)} | "
        f"Raw=O:{candle['raw']['o']} H:{candle['raw']['h']} L:{candle['raw']['l']} C:{candle['raw']['c']} | "
        f"HA=O:{round(candle['ha']['o'], ROUNDING)} H:{round(candle['ha']['h'], ROUNDING)} "
        f"L:{round(candle['ha']['l'], ROUNDING)} C:{round(candle['ha']['c'], ROUNDING)} | "
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
        logging.info(f"🚀 {side.upper()} order | Entry={entry:.5f} SL={sl:.5f} TP={tp:.5f} Qty={qty}")
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

def compute_signal_and_trade(last_candle):
    global last_signal
    green = sum(1 for c in ha_candles if c["color"] == "green")
    red = sum(1 for c in ha_candles if c["color"] == "red")
    signal = None
    if green > red:
        signal = "buy"
    elif red > green:
        signal = "sell"
    else:
        signal = "buy" if last_candle["ha"]["c"] > last_candle["ha"]["o"] else "sell"

    logging.info(f"Signal={signal} | Last={last_signal}")
    if signal == last_signal:
        return
    last_signal = signal

    balance = get_balance()
    risk_amount = balance * RISK_PER_TRADE
    entry = last_candle["ha"]["c"]

    if signal == "sell":
        sl = last_candle["ha"]["h"] + SL_PIP
        risk = abs(sl - entry)
        tp = entry - (risk * RR) - (entry * TP_EXTRA)
        qty = calc_qty(balance, entry, risk, risk_amount)
        if qty > 0:
            place_order("Sell", entry, sl, tp, qty)

    elif signal == "buy":
        sl = last_candle["ha"]["l"] - SL_PIP
        risk = abs(entry - sl)
        tp = entry + (risk * RR) + (entry * TP_EXTRA)
        qty = calc_qty(balance, entry, risk, risk_amount)
        if qty > 0:
            place_order("Buy", entry, sl, tp, qty)

# ================== MAIN LOOP ==================
def main():
    logging.info("Building initial HA candles...")
    build_initial_ha()
    for c in ha_candles:
        log_candle(c)

    logging.info("Starting live loop...")
    while True:
        now = datetime.utcnow()
        sec_into_cycle = (now.minute % INTERVAL) * 60 + now.second
        wait = INTERVAL * 60 - sec_into_cycle
        if wait <= 0:
            wait += INTERVAL * 60
        logging.info(f"⏳ Waiting {wait}s until next {INTERVAL}m candle close...")
        time.sleep(wait)
        try:
            process_new_candle()
        except Exception as e:
            logging.error(f"Error: {e}")

if __name__ == "__main__":
    main()
