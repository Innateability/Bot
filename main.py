#!/usr/bin/env python3
import os
import time
import math
import logging
from datetime import datetime
from pybit.unified_trading import HTTP

# ================== CONFIG ==================
PAIRS = [
    {"symbol": "BTCUSDT", "threshold": 0.00009, "leverage": 100},
    {"symbol": "TRXUSDT", "threshold": 0.00008, "leverage": 75}
]

INTERVAL = "3"  # 4-hour candles
ROUNDING = 5
FALLBACK = 0.90
RISK_NORMAL = 0.33
RISK_RECOVERY = 0.50
TP_NORMAL = 0.003
TP_RECOVERY = 0.01
SL_PCT = 0.009

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

# ================== STATE ==================
losses_count = 0
last_pnl = 0.0
has_opened = {p["symbol"]: False for p in PAIRS}
last_checked_time = {p["symbol"]: 0 for p in PAIRS}

# ================== HELPERS ==================
def fetch_last_closed_raw(symbol):
    """Fetch the most recently closed candle."""
    resp = session.get_kline(category="linear", symbol=symbol, interval=INTERVAL, limit=3)
    raw = resp["result"]["list"][-2]
    return {
        "time": int(raw[0]),
        "o": float(raw[1]),
        "h": float(raw[2]),
        "l": float(raw[3]),
        "c": float(raw[4]),
    }

def get_balance_usdt():
    """Get wallet USDT balance from unified account."""
    resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    if "result" in resp and "list" in resp["result"] and resp["result"]["list"]:
        try:
            return float(resp["result"]["list"][0]["coin"][0]["walletBalance"])
        except Exception:
            return float(resp["result"]["list"][0]["totalEquity"])
    return 0.0

def calc_qty(balance, entry, leverage, risk_percentage, symbol):
    """Calculate position size using risk-based formula."""
    sl_dist = entry * 0.01
    risk_amount = balance * risk_percentage
    qty_by_risk = risk_amount / sl_dist
    max_affordable = (balance * leverage) / entry * FALLBACK
    qty = min(qty_by_risk, max_affordable)

    # Precision rounding by pair type
    if "BTC" in symbol:
        qty = math.floor(qty * 1000) / 1000.0
    elif "TRX" in symbol:
        qty = round(qty)

    return qty

def close_all_positions_and_get_last_pnl(symbol):
    """Close all open positions for a symbol and return the last realized PnL."""
    global last_pnl
    pos_resp = session.get_positions(category="linear", symbol=symbol)
    if "result" in pos_resp and "list" in pos_resp["result"]:
        for p in pos_resp["result"]["list"]:
            size = float(p.get("size", 0) or 0)
            side = p.get("side", "")
            if size > 0:
                close_side = "Sell" if side.lower() == "buy" else "Buy"
                logging.info(f"🔻 Closing {side} position on {symbol} (size={size})")
                session.place_order(
                    category="linear",
                    symbol=symbol,
                    side=close_side,
                    orderType="Market",
                    qty=str(size),
                    reduceOnly=True,
                    timeInForce="IOC"
                )
                time.sleep(2)

    resp = session.get_closed_pnl(category="linear", symbol=symbol, limit=5)
    if "result" in resp and "list" in resp["result"] and resp["result"]["list"]:
        pnl_val = resp["result"]["list"][0].get("closedPnl") or resp["result"]["list"][0].get("realisedPnl")
        if pnl_val is not None:
            pnl = float(pnl_val)
            last_pnl = pnl
            return pnl
    return last_pnl

def place_order(symbol, signal, entry, sl, tp, qty):
    """Execute a market order with TP and SL."""
    try:
        logging.info(f"🚀 {symbol} | {signal.upper()} | Entry={entry:.6f} SL={sl:.6f} TP={tp:.6f} Qty={qty}")
        resp = session.place_order(
            category="linear",
            symbol=symbol,
            side=signal.capitalize(),
            orderType="Market",
            qty=str(qty),
            reduceOnly=False,
            timeInForce="IOC",
            takeProfit=f"{round(tp, ROUNDING)}",
            stopLoss=f"{round(sl, ROUNDING)}",
            positionIdx=0
        )
        logging.info(f"✅ {symbol} Order response: {resp}")
        return resp
    except Exception as e:
        logging.error(f"Error placing order on {symbol}: {e}")
        raise

# ================== CORE LOGIC ==================
def handle_symbol(symbol, threshold, leverage):
    """Main trading logic per symbol."""
    global losses_count, last_pnl, has_opened, last_checked_time

    raw = fetch_last_closed_raw(symbol)
    c_time = raw["time"]

    if c_time == last_checked_time[symbol]:
        return False  # no new candle yet

    last_checked_time[symbol] = c_time
    o, h, l, c = raw["o"], raw["h"], raw["l"], raw["c"]
    logging.info(f"🕒 {symbol} | O:{o:.6f} H:{h:.6f} L:{l:.6f} C:{c:.6f}")

    is_green = c > o
    is_red = c < o
    signal = None

    if is_green and (h - o) / o > threshold:
        signal = "buy"
    elif is_red and (o - l) / o > threshold:
        signal = "sell"

    if not signal:
        logging.info(f"❌ {symbol}: No trade signal.")
        return False

    if has_opened[symbol]:
        logging.info(f"⏸ {symbol}: Trade already opened this candle.")
        return False

    pnl = close_all_positions_and_get_last_pnl(symbol)
    if pnl < 0:
        losses_count += 1
    elif pnl > 0:
        losses_count = max(0, losses_count - 1)

    recovery_mode = losses_count > 0
    risk_pct = RISK_RECOVERY if recovery_mode else RISK_NORMAL
    tp_pct = TP_RECOVERY if recovery_mode else TP_NORMAL

    entry = c
    if signal == "buy":
        sl = entry * (1 - SL_PCT)
        tp = entry * (1 + tp_pct)
    else:
        sl = entry * (1 + SL_PCT)
        tp = entry * (1 - tp_pct)

    balance = get_balance_usdt()
    qty = calc_qty(balance, entry, leverage, risk_pct, symbol)

    try:
        resp = place_order(symbol, signal, entry, sl, tp, qty)
        if "retMsg" in resp and "insufficient" in resp["retMsg"].lower():
            logging.warning(f"⚠️ Insufficient balance for {symbol}")
            return "INSUFFICIENT"
        has_opened[symbol] = True
        logging.info(f"📊 {symbol} | losses={losses_count} | mode={'RECOVERY' if recovery_mode else 'NORMAL'}")
        return True
    except Exception as e:
        if "insufficient" in str(e).lower():
            logging.warning(f"⚠️ Insufficient balance for {symbol}")
            return "INSUFFICIENT"
        logging.error(f"❌ {symbol} order failed: {e}")
        return False

# ================== MAIN LOOP ==================
def main():
    logging.info("🤖 Bot started — BTC priority, TRX fallback on insufficient funds")
    while True:
        try:
            btc = next(p for p in PAIRS if p["symbol"] == "BTCUSDT")
            trx = next(p for p in PAIRS if p["symbol"] == "TRXUSDT")

            btc_result = handle_symbol(btc["symbol"], btc["threshold"], btc["leverage"])
            if btc_result == "INSUFFICIENT" or not btc_result:
                trx_result = handle_symbol(trx["symbol"], trx["threshold"], trx["leverage"])
                if trx_result == "INSUFFICIENT":
                    logging.warning("⚠️ Both BTC and TRX insufficient — skipping this cycle")

            time.sleep(60)
        except KeyboardInterrupt:
            logging.info("🛑 Bot stopped manually.")
            break
        except Exception as e:
            logging.error(f"⚠️ Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
