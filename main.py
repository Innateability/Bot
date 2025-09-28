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
INTERVAL = "3"          # timeframe in minutes ("3" = 3m, "60" = 1h, "240" = 4h)
CANDLE_SECONDS = 3 * 60 # adjust with INTERVAL * 60
ROUNDING = 5

# Set manually before first run
INITIAL_HA_OPEN = 0.33511

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
    resp = session.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=3)

    logging.info(f"Raw kline response: {resp}")

    if "result" not in resp or "list" not in resp["result"]:
        raise Exception(f"Bad kline response: {resp}")

    raw = resp["result"]["list"][-2]  # last fully closed
    parsed = {
        "time": int(raw[0]),
        "o": float(raw[1]),
        "h": float(raw[2]),
        "l": float(raw[3]),
        "c": float(raw[4])
    }
    logging.info(f"Parsed candle → O:{parsed['o']} H:{parsed['h']} L:{parsed['l']} C:{parsed['c']}")
    return parsed

def calc_ha(raw, prev_ha_open):
    """Calculate HA values using persisted HA open."""
    ha_close = (raw["o"] + raw["h"] + raw["l"] + raw["c"]) / 4
    ha_open = (prev_ha_open + ha_close) / 2
    ha_high = max(raw["h"], ha_open, ha_close)
    ha_low = min(raw["l"], ha_open, ha_close)
    return {"o": ha_open, "h": ha_high, "l": ha_low, "c": ha_close}

def get_balance():
    resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    balance = float(resp["result"]["list"][0]["coin"][0]["walletBalance"])
    logging.info(f"💰 Wallet balance fetched: {balance:.4f} USDT")
    return balance

def calc_qty(balance, entry, sl, risk_amount):
    """Calculate position size with leverage, fallback if needed."""
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return 0
    qty_by_risk = (risk_amount / sl_dist) * LEVERAGE
    max_affordable = (balance * LEVERAGE) / entry * FALLBACK
    qty = min(qty_by_risk, max_affordable)
    logging.info(f"📐 Qty calc → Risk={risk_amount:.4f}, SL Dist={sl_dist:.6f}, "
                 f"QtyByRisk={qty_by_risk:.2f}, MaxAffordable={max_affordable:.2f}, Final={qty:.2f}")
    return max(0, int(qty))

def place_order(side, entry, sl, tp, qty):
    try:
        logging.info("🚀 Placing %s order | Entry=%.5f SL=%.5f TP=%.5f Qty=%d",
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
        logging.info("✅ Order response: %s", resp)
    except Exception as e:
        logging.error("❌ Error placing order: %s", e)

def process_new_candle():
    global last_signal, last_ha_open

    raw = fetch_last_closed()
    ha = calc_ha(raw, last_ha_open)
    last_ha_open = ha["o"]

    color_raw = "green" if raw["c"] >= raw["o"] else "red"
    color_ha = "green" if ha["c"] >= ha["o"] else "red"

    logging.info(f"Candle {datetime.fromtimestamp(raw['time']/1000)} "
                 f"| Raw ({color_raw}) O:{raw['o']} H:{raw['h']} L:{raw['l']} C:{raw['c']} "
                 f"| HA ({color_ha}) O:{ha['o']} H:{ha['h']} L:{ha['l']} C:{ha['c']}")

    # Signal check
    if color_raw == "red" and color_ha == "red":
        signal = "sell"
    elif color_raw == "green" and color_ha == "green":
        signal = "buy"
    else:
        signal = None

    if signal and signal != last_signal:
        logging.info(f"📊 New signal detected: {signal.upper()} (previous={last_signal})")
        balance = get_balance()
        risk_amount = balance * RISK_PER_TRADE
        entry = raw["c"]

        if signal == "buy":
            sl = ha["l"]
            tp = entry * (1 + 0.0021)
            qty = calc_qty(balance, entry, sl, risk_amount)
            logging.info(f"🟢 BUY setup → Entry={entry:.5f} SL={sl:.5f} TP={tp:.5f} Qty={qty}")
            if qty > 0:
                place_order("Buy", entry, sl, tp, qty)
                last_signal = "buy"
            else:
                logging.warning("⚠️ BUY ignored (qty=0)")

        elif signal == "sell":
            sl = ha["h"]
            tp = entry * (1 - 0.0021)
            qty = calc_qty(balance, entry, sl, risk_amount)
            logging.info(f"🔴 SELL setup → Entry={entry:.5f} SL={sl:.5f} TP={tp:.5f} Qty={qty}")
            if qty > 0:
                place_order("Sell", entry, sl, tp, qty)
                last_signal = "sell"
            else:
                logging.warning("⚠️ SELL ignored (qty=0)")

# ================== MAIN LOOP ==================
def main():
    logging.info(f"🤖 Bot started on {INTERVAL}m timeframe")
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
    
