import os
import time
import logging
from datetime import datetime, timedelta
from pybit.unified_trading import HTTP

# ================== CONFIG ==================
SYMBOL = "TRXUSDT"
INTERVAL = "3"       # Default 3m, can change to "60" for 1h
LEVERAGE = 75
RISK_PER_TRADE = 0.10
FALLBACK = 0.95

# You provide these before deployment
ha_open = 0.33667  # Example: persisted HA open of last closed candle
colors = list("ggggggrr")  # Last 8 HA candle colors manually entered

# ================== LOGGING ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    handlers=[logging.StreamHandler()]
)

# ================== BYBIT SESSION ==================
session = HTTP(testnet=False)

# ================== FUNCTIONS ==================
def get_last_candle():
    """Fetch latest closed candle from Bybit"""
    data = session.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=2)
    kline = data["result"]["list"][0]  # Most recent closed candle
    ts, o, h, l, c, _, _, _ = kline
    return {
        "time": datetime.utcfromtimestamp(int(ts) / 1000),
        "o": float(o),
        "h": float(h),
        "l": float(l),
        "c": float(c),
    }

def compute_ha(prev_ha_open, raw):
    """Compute HA candle manually using persisted ha_open"""
    ha_close = (raw["o"] + raw["h"] + raw["l"] + raw["c"]) / 4
    ha_open = (prev_ha_open + ha_close) / 2
    ha_high = max(raw["h"], ha_open, ha_close)
    ha_low = min(raw["l"], ha_open, ha_close)
    return {"o": ha_open, "h": ha_high, "l": ha_low, "c": ha_close}

def get_color(ha):
    return "g" if ha["c"] >= ha["o"] else "r"

# ================== MAIN LOOP ==================
def main():
    global ha_open, colors

    logging.info(f"Bot started on {INTERVAL}m timeframe")
    logging.info(f"Initial HA Open = {ha_open}")
    logging.info(f"Initial Colors = {''.join(colors)}")

    while True:
        try:
            now = datetime.utcnow()
            wait = (now + timedelta(minutes=int(INTERVAL))).replace(second=5, microsecond=0) - now
            logging.info(f"⏳ Waiting {wait.total_seconds():.0f}s for next candle close...")
            time.sleep(wait.total_seconds())

            raw = get_last_candle()
            ha = compute_ha(ha_open, raw)
            ha_open = ha["o"]

            color = get_color(ha)
            colors.append(color)
            if len(colors) > 8:
                colors.pop(0)

            logging.info(f"Candle Time={raw['time']} | Raw=O:{raw['o']} H:{raw['h']} L:{raw['l']} C:{raw['c']} "
                         f"| HA=O:{ha['o']:.5f} H:{ha['h']:.5f} L:{ha['l']:.5f} C:{ha['c']:.5f} "
                         f"| Color={color} | Colors Seq={''.join(colors)}")

            # === STRATEGY LOGIC (replace with yours) ===
            if colors[-1] == "g" and colors[-2] == "r":
                logging.info("📈 Potential BUY signal")
            elif colors[-1] == "r" and colors[-2] == "g":
                logging.info("📉 Potential SELL signal")

        except Exception as e:
            logging.error(f"Error: {e}")
            time.sleep(5)

# ================== RUN ==================
if __name__ == "__main__":
    main()
