import os
import time
import logging
from datetime import datetime, timezone
from pybit.unified_trading import HTTP

# ================== CONFIG ==================
SYMBOL = "TRXUSDT"
RISK_PER_TRADE = 0.10   # 10% of balance
LEVERAGE = 75
INTERVAL = "3"          # 3m candles
CANDLE_SECONDS = 180
WINDOW = 8              # rolling HA window
INITIAL_HA_OPEN = 0.33304  # only HA open is seeded
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
last_ha_open = None
first_build = True   # flag for initial HA open usage

# ================== FUNCTIONS ==================
def fetch_last_closed():
    """Fetch the last fully closed raw candle."""
    resp = session.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=3)
    raw = resp["result"]["list"][-2]  # last closed
    return {
        "time": int(raw[0]),
        "o": float(raw[1]),
        "h": float(raw[2]),
        "l": float(raw[3]),
        "c": float(raw[4])
    }

def build_ha(raw, prev_ha_open):
    """Build a new HA candle from raw candle and previous HA open."""
    ha_close = (raw["o"] + raw["h"] + raw["l"] + raw["c"]) / 4
    ha_open = (prev_ha_open + ha_close) / 2
    ha_high = max(raw["h"], ha_open, ha_close)
    ha_low = min(raw["l"], ha_open, ha_close)
    color = "green" if ha_close >= ha_open else "red"
    return {
        "time": datetime.fromtimestamp(raw["time"]/1000),
        "raw": raw,
        "ha": {"o": ha_open, "h": ha_high, "l": ha_low, "c": ha_close},
        "color": color
    }

def log_candle(c):
    logging.info(
        f"Candle {c['time']} | Raw O:{c['raw']['o']} H:{c['raw']['h']} "
        f"L:{c['raw']['l']} C:{c['raw']['c']} | "
        f"HA O:{c['ha']['o']} H:{c['ha']['h']} "
        f"L:{c['ha']['l']} C:{c['ha']['c']} | Color={c['color']}"
    )

def get_balance():
    resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    return float(resp["result"]["list"][0]["coin"][0]["walletBalance"])

def calc_qty(balance, entry, sl, risk_amount):
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return 0
    qty_by_risk = (risk_amount / sl_dist) * LEVERAGE
    max_affordable = (balance * LEVERAGE) / entry * 0.9
    return max(0, int(min(qty_by_risk, max_affordable)))

def place_order(side, entry, sl, tp, qty):
    logging.info(f"🚀 {side.upper()} ORDER | Entry={entry:.5f} SL={sl:.5f} TP={tp:.5f} Qty={qty}")
    try:
        resp = session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=side.capitalize(),
            orderType="Market",
            qty=str(qty),
            timeInForce="IOC",
            reduceOnly=False,
            stopLoss=str(round(sl, ROUNDING)),
            takeProfit=str(round(tp, ROUNDING))
        )
        logging.info(f"Order response: {resp}")
    except Exception as e:
        logging.error(f"❌ Error placing order: {e}")

def process_new_candle():
    global last_signal, last_ha_open, ha_candles, first_build

    raw = fetch_last_closed()

    if first_build:
        prev_ha_open = INITIAL_HA_OPEN
        first_build = False
        logging.info(f"🔑 Using INITIAL_HA_OPEN={INITIAL_HA_OPEN} for first HA candle...")
    else:
        prev_ha_open = last_ha_open

    candle = build_ha(raw, prev_ha_open)
    last_ha_open = candle["ha"]["o"]
    log_candle(candle)

    # Store into rolling window
    ha_candles.append(candle)
    if len(ha_candles) > WINDOW:
        ha_candles.pop(0)

    # Only start trading after first WINDOW candles
    if len(ha_candles) < WINDOW:
        logging.info(f"📉 Accumulating candles ({len(ha_candles)}/{WINDOW})... not trading yet.")
        return

    # Signal logic: majority color in last WINDOW
    greens = sum(1 for c in ha_candles if c["color"] == "green")
    reds = WINDOW - greens
    signal = "buy" if greens > reds else "sell"

    logging.info(f"Signal={signal.upper()} | Last Signal={last_signal}")

    if signal != last_signal:
        last_signal = signal
        balance = get_balance()
        risk_amount = balance * RISK_PER_TRADE
        entry = candle["ha"]["c"]

        if signal == "buy":
            sl = candle["ha"]["l"]
            tp = entry + (2 * (entry - sl)) + (entry * 0.001)
            qty = calc_qty(balance, entry, sl, risk_amount)
            logging.info(f"🟢 BUY setup → Entry={entry:.5f} SL={sl:.5f} TP={tp:.5f} Qty={qty}")
            if qty > 0:
                place_order("Buy", entry, sl, tp, qty)

        elif signal == "sell":
            sl = candle["ha"]["h"]
            tp = entry - (2 * (sl - entry)) - (entry * 0.001)
            qty = calc_qty(balance, entry, sl, risk_amount)
            logging.info(f"🔴 SELL setup → Entry={entry:.5f} SL={sl:.5f} TP={tp:.5f} Qty={qty}")
            if qty > 0:
                place_order("Sell", entry, sl, tp, qty)

# ================== MAIN LOOP ==================
def main():
    logging.info(
        f"🤖 Bot starting on {INTERVAL}m candles with Initial HA Open={INITIAL_HA_OPEN}"
    )
    while True:
        now = datetime.now(timezone.utc)
        sec_into_cycle = (now.minute * 60 + now.second) % CANDLE_SECONDS
        wait = CANDLE_SECONDS - sec_into_cycle
        if wait <= 0:
            wait += CANDLE_SECONDS
        logging.info(f"⏳ Waiting {wait}s until next candle close...")
        time.sleep(wait + 2)
        try:
            process_new_candle()
        except Exception as e:
            logging.error(f"❌ Error: {e}")

if __name__ == "__main__":
    main()
    
