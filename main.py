#!/usr/bin/env python3
import os
import time
import math
import logging
from datetime import datetime, timezone
from pybit.unified_trading import HTTP

# ================== CONFIG ==================
PAIRS = [
    {"symbol": "BTCUSDT", "threshold": 0.006, "leverage": 100},
    {"symbol": "TRXUSDT", "threshold": 0.006, "leverage": 75}
]

INTERVAL = "3"  # 4H
ROUNDING = 5
FALLBACK = 0.90
RISK_NORMAL = 0.33
RISK_RECOVERY = 0.33
TP_NORMAL = 0.003
TP_RECOVERY = 0.007
SL_PCT = 0.005  # 0.9% used for trade SL
QTY_SL_DIST_PCT = 0.005  # 1% used for qty calculation

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

# no testnet as requested
session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

# ================== GLOBAL STATE ==================
losses_count = 0
last_pnl = 0.0
last_checked_time = {p["symbol"]: 0 for p in PAIRS}

# ================== HELPERS ==================
def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def fetch_last_closed_raw(symbol):
    """Fetch the last closed 4H candle (the second-to-last returned by API)."""
    resp = session.get_kline(category="linear", symbol=symbol, interval=INTERVAL, limit=3)
    if "result" not in resp or "list" not in resp["result"]:
        raise RuntimeError(f"Bad kline response: {resp}")
    raw = resp["result"]["list"][-2]  # second-to-last is last closed
    return {
        "time": int(raw[0]),
        "o": float(raw[1]),
        "h": float(raw[2]),
        "l": float(raw[3]),
        "c": float(raw[4]),
    }

def get_balance_usdt():
    """Return USDT wallet balance (or total equity fallback)."""
    resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    if "result" in resp and "list" in resp["result"] and resp["result"]["list"]:
        try:
            return float(resp["result"]["list"][0]["coin"][0]["walletBalance"])
        except Exception:
            try:
                return float(resp["result"]["list"][0]["totalEquity"])
            except Exception:
                return 0.0
    return 0.0

def calc_qty(balance, entry, leverage, risk_percentage, symbol):
    sl_dist = entry * QTY_SL_DIST_PCT
    if sl_dist <= 0:
        return 0.0
    risk_amount = balance * risk_percentage
    qty_by_risk = risk_amount / sl_dist
    max_affordable = (balance * leverage) / entry * FALLBACK
    qty = min(qty_by_risk, max_affordable)
    if "BTC" in symbol:
        qty = math.floor(qty * 1000) / 1000.0
    elif "TRX" in symbol:
        qty = round(qty)
    return qty

def close_all_positions(symbol):
    """Market close any open positions for symbol (non-blocking)."""
    try:
        pos_resp = session.get_positions(category="linear", symbol=symbol)
        if "result" in pos_resp and "list" in pos_resp["result"]:
            for p in pos_resp["result"]["list"]:
                size = float(p.get("size", 0) or 0)
                side = p.get("side", "")
                if size > 0:
                    close_side = "Sell" if side.lower() == "buy" else "Buy"
                    logging.info(f"🔻 Closing existing {side} position on {symbol} size={size}")
                    session.place_order(
                        category="linear",
                        symbol=symbol,
                        side=close_side,
                        orderType="Market",
                        qty=str(size),
                        reduceOnly=True,
                        timeInForce="IOC"
                    )
                    time.sleep(1)
    except Exception as e:
        logging.error(f"Error closing positions for {symbol}: {e}")

# ================== UPDATED PNL LOGIC ==================
def get_most_recent_pnl():
    """
    Fetch the most recent closed PnL entry among both BTC and TRX.
    Returns tuple: (symbol, pnl) or (None, None) if nothing found.
    """
    global last_pnl
    latest_trade = None
    latest_time = 0
    latest_symbol = None

    for pair in PAIRS:
        symbol = pair["symbol"]
        try:
            resp = session.get_closed_pnl(category="linear", symbol=symbol, limit=1)
            if "result" in resp and "list" in resp["result"] and resp["result"]["list"]:
                trade = resp["result"]["list"][0]
                pnl_val = trade.get("closedPnl") or trade.get("realisedPnl") or trade.get("pnl")
                time_val = int(trade.get("updatedTime") or trade.get("createdTime") or 0)
                if pnl_val is not None and time_val > latest_time:
                    latest_time = time_val
                    latest_trade = float(pnl_val)
                    latest_symbol = symbol
        except Exception as e:
            logging.error(f"Error fetching closed pnl for {symbol}: {e}")

    if latest_symbol:
        last_pnl = latest_trade
        logging.info(f"📊 Most recent closed PnL: {latest_symbol} = {latest_trade:.8f} USDT")
        return latest_symbol, latest_trade

    logging.info("🔎 No closed PnL found for BTC or TRX.")
    return None, None

# ================== UPDATED HANDLE SYMBOL ==================
def handle_symbol(symbol, threshold, leverage):
    global losses_count

    raw = fetch_last_closed_raw(symbol)
    c_time = raw["time"]

    if c_time == last_checked_time[symbol]:
        return False
    last_checked_time[symbol] = c_time

    o, h, l, c = raw["o"], raw["h"], raw["l"], raw["c"]
    logging.info(f"🕒 {symbol} | O:{o:.6f} H:{h:.6f} L:{l:.6f} C:{c:.6f}")

    # Step 1: determine if there's a signal
    is_green = c > o
    is_red = c < o
    signal = None
    if is_green and (h - o) / o > threshold:
        signal = "buy"
    elif is_red and (o - l) / o > threshold:
        signal = "sell"

    if not signal:
        logging.info(f"❌ {symbol}: No signal this candle.")
        return False

    # Step 2: close existing positions
    close_all_positions(symbol)

    # Step 3: check most recent closed PnL (BTC or TRX whichever is latest)
    latest_symbol, pnl = get_most_recent_pnl()
    if pnl is not None:
        if pnl < 0:
            losses_count += 1
            logging.info(f"➕ Increased losses_count to {losses_count} (PnL {pnl:.8f} from {latest_symbol})")
        elif pnl > 0:
            old = losses_count
            losses_count = max(0, losses_count - 1)
            logging.info(f"➖ Decremented losses_count {old} → {losses_count} (PnL {pnl:.8f} from {latest_symbol})")
    else:
        logging.info("ℹ️ No closed PnL available (may be first run or no closed trades yet).")

    # Step 4: risk and TP logic
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

    # Step 5: quantity calculation
    balance = get_balance_usdt()
    qty = calc_qty(balance, entry, leverage, risk_pct, symbol)
    logging.info(f"📐 Qty calc → balance={balance:.8f}, risk_pct={risk_pct}, qty={qty}")

    if qty <= 0:
        logging.warning(f"⚠️ {symbol} computed qty <= 0, skipping order.")
        return "INSUFFICIENT" if "BTC" in symbol else False

    # Step 6: place order
    try:
        resp = place_order(symbol, signal, entry, sl, tp, qty)
        logging.info(f"📊 {symbol} | losses_count={losses_count} | mode={'RECOVERY' if recovery_mode else 'NORMAL'}")
        return True
    except Exception as e:
        msg = str(e).lower()
        logging.error(f"❌ {symbol} order failed: {e}")
        if any(err in msg for err in ["insufficient", "not enough", "ab not enough", "not enough for new order"]):
            return "INSUFFICIENT"
        return False

# ================== ORDER FUNCTION ==================
def place_order(symbol, signal, entry, sl, tp, qty):
    if qty is None or qty <= 0:
        raise ValueError("qty must be > 0")
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
        raise

# ================== MAIN LOOP ==================
def seconds_until_next_candle(interval_minutes):
    now = datetime.now(timezone.utc)
    candle_seconds = int(interval_minutes) * 60
    seconds_into_cycle = (now.hour * 3600 + now.minute * 60 + now.second) % candle_seconds
    wait = candle_seconds - seconds_into_cycle
    if wait <= 0:
        wait += candle_seconds
    return wait

def main():
    logging.info("🤖 Bot started — BTC priority, TRX fallback if insufficient funds")
    while True:
        try:
            wait = seconds_until_next_candle(int(INTERVAL))
            logging.info(f"⏳ Waiting {wait}s for next {INTERVAL}m candle close...")
            time.sleep(wait + 2)

            btc_pair = next(p for p in PAIRS if p["symbol"] == "BTCUSDT")
            trx_pair = next(p for p in PAIRS if p["symbol"] == "TRXUSDT")

            btc_result = handle_symbol(btc_pair["symbol"], btc_pair["threshold"], btc_pair["leverage"])
            if btc_result == "INSUFFICIENT" or btc_result is False:
                logging.info("⚠️ BTC trade not placed or insufficient — trying TRX fallback.")
                trx_result = handle_symbol(trx_pair["symbol"], trx_pair["threshold"], trx_pair["leverage"])
                if trx_result == "INSUFFICIENT":
                    logging.warning("⚠️ TRX fallback also insufficient to open a trade.")
        except KeyboardInterrupt:
            logging.info("🛑 Stopped manually by user.")
            break
        except Exception as e:
            logging.error(f"Unhandled error in main loop: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
        
