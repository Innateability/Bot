import os
import time
import logging
from datetime import datetime, timezone
from pybit.unified_trading import HTTP

# ================== CONFIG ==================
SYMBOL = "TRXUSDT"
RISK_PER_TRADE = 0.50   # 50% of balance
FALLBACK = 0.90         # fallback % if qty unaffordable
LEVERAGE = 75
INTERVAL = "4"        # 4h candles (can change to "60" or "3")
CANDLE_SECONDS = 4 * 60 # 4h = 14400 sec
ROUNDING = 5

# Set manually before first run
INITIAL_HA_OPEN = 0.33615

# ================== API KEYS ==================
API_KEY = os.getenv("BYBIT_SUB_API_KEY")
API_SECRET = os.getenv("BYBIT_SUB_API_SECRET")
session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

# ================== GLOBAL STATE ==================
last_signal = None
last_ha_open = INITIAL_HA_OPEN

# ================== FUNCTIONS ==================
def fetch_last_closed():
    """Fetch last fully closed raw candle from Bybit."""
    resp = session.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=2)
    if "result" not in resp or "list" not in resp["result"]:
        raise Exception(f"Bad kline response: {resp}")
    raw = resp["result"]["list"][-2]  # second-to-last = closed candle
    return {
        "time": int(raw[0]),
        "o": float(raw[1]),
        "h": float(raw[2]),
        "l": float(raw[3]),
        "c": float(raw[4])
    }

def calc_ha(raw, prev_ha_open):
    """Calculate HA values using persisted HA open."""
    ha_close = (raw["o"] + raw["h"] + raw["l"] + raw["c"]) / 4
    ha_open = (prev_ha_open + ha_close) / 2
    ha_high = max(raw["h"], ha_open, ha_close)
    ha_low = min(raw["l"], ha_open, ha_close)
    return {"o": ha_open, "h": ha_high, "l": ha_low, "c": ha_close}

def get_balance():
    resp = session.get_wallet_balance(accountType="CONTRACT", coin="USDT")
    return float(resp["result"]["list"][0]["coin"][0]["walletBalance"])

def calc_qty(balance, entry, sl, risk_amount):
    """Calculate position size with leverage, fallback if needed."""
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return 0
    qty_by_risk = (risk_amount / sl_dist) * LEVERAGE
    max_affordable = (balance * LEVERAGE) / entry * FALLBACK
    qty = min(qty_by_risk, max_affordable)
    return max(0, int(qty))

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
            takeProfit=str(round(tp, ROUNDING)),
            stopLoss=str(round(sl, ROUNDING)),
            positionIdx=0  # one-way mode
        )
        logging.info("Order response: %s", resp)
    except Exception as e:
        logging.error("Error placing order: %s", e)

def process_new_candle():
    global last_signal, last_ha_open

    raw = fetch_last_closed()
    ha = calc_ha(raw, last_ha_open)
    last_ha_open = ha["o"]

    color_raw = "green" if raw["c"] >= raw["o"] else "red"
    color_ha = "green" if ha["c"] >= ha["o"] else "red"

    logging.info(f"Candle {datetime.fromtimestamp(raw['time']/1000)} "
                 f"| Raw O:{raw['o']} H:{raw['h']} L:{raw['l']} C:{raw['c']} ({color_raw}) "
                 f"| HA O:{ha['o']} H:{ha['h']} L:{ha['l']} C:{ha['c']} ({color_ha})")

    # Check for signal
    if color_raw == "red" and color_ha == "red":
        signal = "sell"
    elif color_raw == "green" and color_ha == "green":
        signal = "buy"
    else:
        signal = None

    if signal and signal != last_signal:
        last_signal = signal
        balance = get_balance()
        risk_amount = balance * RISK_PER_TRADE
        entry = raw["c"]

        if signal == "buy":
            sl = ha["l"]
            tp = entry * (1 + 0.0021)
            qty = calc_qty(balance, entry, sl, risk_amount)
            if qty > 0:
                place_order("Buy", entry, sl, tp, qty)

        elif signal == "sell":
            sl = ha["h"]
            tp = entry * (1 - 0.0021)
            qty = calc_qty(balance, entry, sl, risk_amount)
            if qty > 0:
                place_order("Sell", entry, sl, tp, qty)

# ================== MAIN LOOP ==================
def main():
    logging.info(f"Bot started on {INTERVAL}m timeframe")
    logging.info(f"Initial HA Open = {INITIAL_HA_OPEN}")

    while True:
        now = datetime.now(timezone.utc)
        sec_into_cycle = (now.hour * 3600 + now.minute * 60 + now.second) % CANDLE_SECONDS
        wait = CANDLE_SECONDS - sec_into_cycle
        if wait <= 0:
            wait += CANDLE_SECONDS
        logging.info(f"⏳ Waiting {wait}s for next candle close...")
        time.sleep(wait + 2)
        try:
            process_new_candle()
        except Exception as e:
            logging.error(f"Error: {e}")

if __name__ == "__main__":
    main()
    
