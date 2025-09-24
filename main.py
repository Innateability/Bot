import os
import time
import logging
from datetime import datetime
from pybit.unified_trading import HTTP

# ================== CONFIG ==================
SYMBOL = "TRXUSDT"
RISK_PER_TRADE = 0.10  # 10% of balance
FALLBACK = 0.95        # fallback if balance insufficient
RR = 2.0                # risk:reward
TP_EXTRA = 0.001        # extra TP (+0.1%)
LEVERAGE = 75
INTERVAL = 3            # 3-minute candles
CANDLE_SECONDS = INTERVAL * 60
WINDOW = 8              # rolling HA candles

# Manual initial HA open
INITIAL_HA_OPEN = 0.33715

# ================== API ==================
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

# ================== LOGGING ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.StreamHandler()]
)

# ================== GLOBAL STATE ==================
ha_candles = []      # rolling HA candles
ha_open_state = INITIAL_HA_OPEN
last_signal = None
initial_ha_open_time = None

# ================== HELPERS ==================
def fetch_candles(limit=WINDOW):
    """Fetch last N raw candles from Bybit."""
    resp = session.get_kline(category="linear", symbol=SYMBOL, interval=str(INTERVAL), limit=limit)
    raw = resp["result"]["list"][::-1]  # oldest → newest
    return raw

def log_candle(candle):
    logging.info(
        f"Candle Time={candle['time']} | "
        f"Raw O:{candle['raw']['o']} H:{candle['raw']['h']} L:{candle['raw']['l']} C:{candle['raw']['c']} | "
        f"HA  O:{candle['ha']['o']} H:{candle['ha']['h']} L:{candle['ha']['l']} C:{candle['ha']['c']} | "
        f"Color={candle['color']}"
    )

def get_balance():
    resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    return float(resp["result"]["list"][0]["coin"][0]["walletBalance"])

def calc_qty(balance, entry, risk, risk_amount):
    """Calculate position size with leverage and fallback, subtract 1 for safety."""
    qty = (risk_amount * LEVERAGE) / (risk * entry)
    max_qty = (balance * FALLBACK * LEVERAGE) / entry
    if qty * entry / LEVERAGE > balance:
        qty = max_qty
    return max(0, int(qty) - 1)

def place_order(side, entry, sl, tp, qty):
    try:
        logging.info(f"🚀 {side.upper()} | Entry={entry:.5f} SL={sl:.5f} TP={tp:.5f} Qty={qty}")
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

# ================== INITIAL HA BUILD ==================
def build_initial_ha():
    global ha_candles, ha_open_state, initial_ha_open_time
    raw_candles = fetch_candles(WINDOW)
    ha_open = INITIAL_HA_OPEN
    ha_candles = []

    for i, c in enumerate(raw_candles):
        ts, open_, high_, low_, close_ = map(float, [c[0], c[1], c[2], c[3], c[4]])
        ha_close = (open_ + high_ + low_ + close_) / 4
        ha_open = (ha_open + ha_close) / 2
        ha_high = max(high_, ha_open, ha_close)
        ha_low = min(low_, ha_open, ha_close)
        color = "green" if ha_close >= ha_open else "red"

        candle = {
            "time": datetime.fromtimestamp(ts/1000),
            "raw": {"o": open_, "h": high_, "l": low_, "c": close_},
            "ha": {"o": ha_open, "h": ha_high, "l": ha_low, "c": ha_close},
            "color": color
        }
        ha_candles.append(candle)

        # Log the initial HA open candle
        if i == 0:
            initial_ha_open_time = candle["time"]
            logging.info(f"Initial HA Open: {INITIAL_HA_OPEN} at {initial_ha_open_time}")

    ha_open_state = ha_candles[-1]["ha"]["o"]

# ================== PROCESS NEW CANDLE ==================
def process_new_candle_rolling():
    global ha_candles, ha_open_state, last_signal

    raw_candle = fetch_candles(limit=1)[0]
    ts, raw_o, raw_h, raw_l, raw_c = map(float, [raw_candle[0], raw_candle[1], raw_candle[2], raw_candle[3], raw_candle[4]])

    ha_close = (raw_o + raw_h + raw_l + raw_c) / 4
    ha_open = (ha_open_state + ha_candles[-1]["ha"]["c"]) / 2 if ha_candles else INITIAL_HA_OPEN
    ha_high = max(raw_h, ha_open, ha_close)
    ha_low  = min(raw_l, ha_open, ha_close)
    color = "green" if ha_close >= ha_open else "red"

    candle = {
        "time": datetime.fromtimestamp(ts/1000),
        "raw": {"o": raw_o, "h": raw_h, "l": raw_l, "c": raw_c},
        "ha": {"o": ha_open, "h": ha_high, "l": ha_low, "c": ha_close},
        "color": color
    }

    # Update rolling list
    ha_candles.append(candle)
    if len(ha_candles) > WINDOW:
        ha_candles.pop(0)

    ha_open_state = ha_open
    log_candle(candle)

    # Compute signal
    green = sum(1 for c in ha_candles if c["color"] == "green")
    red   = sum(1 for c in ha_candles if c["color"] == "red")

    if green > red:
        signal = "buy"
    elif red > green:
        signal = "sell"
    else:
        signal = "buy" if ha_candles[-1]["ha"]["c"] > ha_candles[-1]["ha"]["o"] else "sell"

    logging.info(f"Signal={signal} | Last Signal={last_signal}")

    if signal != last_signal:
        last_signal = signal
        balance = get_balance()
        risk_amount = balance * RISK_PER_TRADE
        entry = ha_candles[-1]["ha"]["c"]

        if signal == "buy":
            sl = ha_candles[-1]["ha"]["l"] - 0.0001
            risk = entry - sl
            tp = entry + (risk * RR) + (entry * TP_EXTRA)
            qty = calc_qty(balance, entry, risk, risk_amount)
            if qty > 0:
                place_order("Buy", entry, sl, tp, qty)

        elif signal == "sell":
            sl = ha_candles[-1]["ha"]["h"] + 0.0001
            risk = sl - entry
            tp = entry - (risk * RR) - (entry * TP_EXTRA)
            qty = calc_qty(balance, entry, risk, risk_amount)
            if qty > 0:
                place_order("Sell", entry, sl, tp, qty)

# ================== MAIN LOOP ==================
def main():
    logging.info("Building initial HA candles...")
    build_initial_ha()

    while True:
        # Calculate wait time until next 3-min candle close
        now = datetime.utcnow()
        sec_into_cycle = (now.minute % INTERVAL) * 60 + now.second
        wait = CANDLE_SECONDS - sec_into_cycle
        if wait <= 0:
            wait += CANDLE_SECONDS
        logging.info(f"⏳ Waiting {wait}s until next candle close...")
        time.sleep(wait)

        try:
            process_new_candle_rolling()
        except Exception as e:
            logging.error(f"Error processing new candle: {e}")

if __name__ == "__main__":
    main()
