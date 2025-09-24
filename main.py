3import time
import logging
from datetime import datetime
from pybit.unified_trading import HTTP

# ================== CONFIG ==================
SYMBOL = "TRXUSDT"
RISK_PER_TRADE = 0.10
LEVERAGE = 75
CANDLE_LIMIT = 200
TIMEFRAME = 1  # minutes
ROUNDING = 5   # price decimals

# API KEYS
MAIN_KEY = "your_main_key"
MAIN_SECRET = "your_main_secret"

# ================== SETUP ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
session = HTTP(testnet=True, api_key=MAIN_KEY, api_secret=MAIN_SECRET)

# ================== GLOBAL ==================
ha_candles = []  # locked HA candles
prev_ha_open = None
prev_ha_close = None

# ================== HELPERS ==================
def fetch_candles():
    """Fetch latest raw OHLC candles from Bybit"""
    data = session.get_kline(category="linear", symbol=SYMBOL, interval=TIMEFRAME, limit=CANDLE_LIMIT)["result"]["list"]
    candles = [
        {
            "time": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
        }
        for c in reversed(data)
    ]
    return candles

def update_heikin_ashi(candles):
    """Update locked HA candles from raw OHLC data"""
    global prev_ha_open, prev_ha_close, ha_candles
    new_ha = []

    for i, c in enumerate(candles):
        raw_o, raw_h, raw_l, raw_c = c["open"], c["high"], c["low"], c["close"]

        # HA close = avg of OHLC
        ha_close = (raw_o + raw_h + raw_l + raw_c) / 4

        # HA open = avg(prev_ha_open, prev_ha_close) OR raw open if first
        if prev_ha_open is None:
            ha_open = raw_o
        else:
            ha_open = (prev_ha_open + prev_ha_close) / 2

        # HA high & low
        ha_high = max(raw_h, ha_open, ha_close)
        ha_low = min(raw_l, ha_open, ha_close)

        # lock values
        ha_candle = {
            "time": c["time"],
            "open": round(ha_open, ROUNDING),
            "high": round(ha_high, ROUNDING),
            "low": round(ha_low, ROUNDING),
            "close": round(ha_close, ROUNDING),
            "color": "green" if ha_close >= ha_open else "red"
        }
        new_ha.append(ha_candle)

        # update prev values for next loop
        prev_ha_open, prev_ha_close = ha_open, ha_close

    ha_candles = new_ha[-8:]  # keep last 8 only
    return ha_candles

def log_candles(raw, ha):
    """Log raw + HA candles with consistency check"""
    logging.info("========= RAW CANDLES =========")
    for c in raw[-8:]:
        logging.info(f"{datetime.utcfromtimestamp(c['time']/1000)} | "
                     f"O:{c['open']} H:{c['high']} L:{c['low']} C:{c['close']}")

    logging.info("========= HEIKIN ASHI =========")
    for c in ha:
        logging.info(f"{datetime.utcfromtimestamp(c['time']/1000)} | "
                     f"O:{c['open']} H:{c['high']} L:{c['low']} C:{c['close']} | {c['color']}")

    # earliest candle time
    earliest = datetime.utcfromtimestamp(ha[0]['time'] / 1000)
    logging.info(f"Earliest of 8 HA candles: {earliest}")

    # color consistency
    colors = [c['color'] for c in ha]
    logging.info(f"Colors last 8: {colors}")

def calculate_quantity(entry, stop):
    """Risk-based qty with leverage, minus 1 adjustment"""
    balance = float(session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]["totalEquity"])
    risk_amount = balance * RISK_PER_TRADE
    risk_per_unit = abs(entry - stop)
    qty = (risk_amount / risk_per_unit) * entry * LEVERAGE
    return max(0, int(qty) - 1)

def place_order(side, entry, sl, tp):
    """Place market order with TP & SL attached"""
    qty = calculate_quantity(entry, sl)
    sl = round(sl + 0.0001 if side == "Buy" else sl - 0.0001, ROUNDING)  # adjust SL

    logging.info(f"Placing {side} order | Entry:{entry} Qty:{qty} SL:{sl} TP:{tp}")

    session.place_order(
        category="linear",
        symbol=SYMBOL,
        side=side,
        orderType="Market",
        qty=qty,
        takeProfit=round(tp, ROUNDING),
        stopLoss=sl,
        tpSlMode="Full",
        reduceOnly=False,
        timeInForce="GoodTillCancel"
    )

def close_all_positions():
    """Close all open positions for SYMBOL"""
    positions = session.get_positions(category="linear", symbol=SYMBOL)["result"]["list"]
    for pos in positions:
        if float(pos["size"]) > 0:
            session.place_order(
                category="linear",
                symbol=SYMBOL,
                side="Sell" if pos["side"] == "Buy" else "Buy",
                orderType="Market",
                qty=pos["size"],
                reduceOnly=True
            )
            logging.info(f"Closed {pos['side']} position of {pos['size']}")

# ================== MAIN LOOP ==================
if __name__ == "__main__":
    while True:
        raw_candles = fetch_candles()
        ha = update_heikin_ashi(raw_candles)
        log_candles(raw_candles, ha)

        # Here: add your strategy signal detection
        # Example dummy signal (for testing):
        if ha[-1]['color'] == "green" and ha[-2]['color'] == "red":
            entry = ha[-1]['close']
            sl = ha[-1]['low']
            tp = entry + (entry - sl)
            place_order("Buy", entry, sl, tp)

        time.sleep(60)
