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

INTERVAL = "240"  # 4H
ROUNDING = 5
FALLBACK = 0.90
RISK_NORMAL = 0.33
RISK_RECOVERY = 0.33
TP_NORMAL = 0.003
TP_RECOVERY = 0.007
SL_PCT = 0.005 # 0.9% used for trade SL
QTY_SL_DIST_PCT = 0.005 # 1% used for qty calculation

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

# no testnet as requested
session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

# ================== GLOBAL STATE ==================
losses_count = 0
last_pnl = 0.0
last_order_id = {p["symbol"]: None for p in PAIRS}   # store last order id per symbol
last_checked_time = {p["symbol"]: 0 for p in PAIRS}  # avoid reprocessing a closed candle

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
    """
    Calculate qty using:
      sl_dist = entry * 1%   (for quantity calculation)
      risk_amount = balance * risk_percentage
      qty_by_risk = risk_amount / sl_dist
      max_affordable = (balance * leverage) / entry * FALLBACK
      qty = min(qty_by_risk, max_affordable)
    Rounding:
      BTC -> floor to 0.001
      TRX -> round to nearest whole
    """
    sl_dist = entry * QTY_SL_DIST_PCT
    if sl_dist <= 0:
        return 0.0
    risk_amount = balance * risk_percentage
    qty_by_risk = risk_amount / sl_dist
    max_affordable = (balance * leverage) / entry * FALLBACK
    qty = min(qty_by_risk, max_affordable)

    # rounding
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

def get_pnl_for_order(order_id, symbol):
    """
    Fetch closed PnL for a given orderId. Returns None if not found.
    This function searches recent closed pnl entries once.
    """
    global last_pnl
    if not order_id:
        return None
    try:
        resp = session.get_closed_pnl(category="linear", symbol=symbol, limit=50)
        if "result" in resp and "list" in resp["result"] and resp["result"]["list"]:
            for trade in resp["result"]["list"]:
                # Some responses use 'orderId' or 'order_id'
                oid = trade.get("orderId") or trade.get("order_id")
                if oid == order_id:
                    pnl_val = trade.get("closedPnl") or trade.get("realisedPnl") or trade.get("pnl")
                    if pnl_val is not None:
                        pnl = float(pnl_val)
                        last_pnl = pnl
                        logging.info(f"📊 Retrieved PnL for order_id={order_id} on {symbol}: {pnl:.8f} USDT")
                        return pnl
        logging.info(f"🔎 No closed PnL found for order_id={order_id} on {symbol}")
        return None
    except Exception as e:
        logging.error(f"Error fetching closed pnl for {symbol}: {e}")
        return None

def place_order(symbol, signal, entry, sl, tp, qty):
    """
    Place a market order with TP/SL in single call (Bybit v5 supports takeProfit/stopLoss params).
    Returns the API response dict or raises.
    """
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
        # bubble up so caller can evaluate message
        raise

# ================== CORE LOGIC ==================
def handle_symbol(symbol, threshold, leverage):
    """
    Process 1 closed candle for the symbol. Uses last_checked_time to avoid reprocessing.
    Returns:
      - "INSUFFICIENT" if order failed for insufficient funds
      - True on success
      - False if no signal or skipped
    """
    global losses_count, last_order_id

    raw = fetch_last_closed_raw(symbol)
    c_time = raw["time"]

    # process each closed candle only once
    if c_time == last_checked_time[symbol]:
        return False
    last_checked_time[symbol] = c_time

    o, h, l, c = raw["o"], raw["h"], raw["l"], raw["c"]
    logging.info(f"🕒 {symbol} | O:{o:.6f} H:{h:.6f} L:{l:.6f} C:{c:.6f}")

    # signal logic using raw candle values
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

    # Before opening, close all positions for this symbol (auto-close regardless of direction)
    close_all_positions(symbol)

    # Fetch last order PnL using stored last_order_id for this symbol (check once)
    prev_order_id = last_order_id.get(symbol)
    pnl = None
    if prev_order_id:
        pnl = get_pnl_for_order(prev_order_id, symbol)
        # update losses_count based on that pnl (only if pnl is available)
        if pnl is not None:
            if pnl < 0:
                losses_count += 1
                logging.info(f"➕ Increased losses_count to {losses_count} (previous PnL {pnl:.8f})")
            elif pnl > 0:
                old = losses_count
                losses_count = max(0, losses_count - 1)
                logging.info(f"➖ Decremented losses_count {old} → {losses_count} (previous PnL {pnl:.8f})")
    else:
        logging.info("ℹ️ No previous order_id for PnL check (skipping PnL retrieval).")

    # Determine mode
    recovery_mode = losses_count > 0
    risk_pct = RISK_RECOVERY if recovery_mode else RISK_NORMAL
    tp_pct = TP_RECOVERY if recovery_mode else TP_NORMAL

    # prepare entry, sl, tp (SL always 0.9%)
    entry = c
    if signal == "buy":
        sl = entry * (1 - SL_PCT)
        tp = entry * (1 + tp_pct)
    else:
        sl = entry * (1 + SL_PCT)
        tp = entry * (1 - tp_pct)

    balance = get_balance_usdt()
    qty = calc_qty(balance, entry, leverage, risk_pct, symbol)

    logging.info(f"📐 Qty calc → balance={balance:.8f}, risk_pct={risk_pct}, qty={qty}")

    # if qty invalid or zero -> treat as insufficient/skip and fallback
    if qty <= 0:
        logging.warning(f"⚠️ {symbol} computed qty <= 0, skipping order (will fallback if BTC priority).")
        return "INSUFFICIENT" if "BTC" in symbol else False

    # Try to place order
    try:
        resp = place_order(symbol, signal, entry, sl, tp, qty)
        # save order id for later PnL lookup
        oid = None
        try:
            if "result" in resp and isinstance(resp["result"], dict):
                oid = resp["result"].get("orderId") or resp["result"].get("order_id")
        except Exception:
            pass
        if oid:
            last_order_id[symbol] = oid
            logging.info(f"🆔 Saved last_order_id for {symbol} = {oid}")
        else:
            logging.info(f"ℹ️ No orderId returned in response for {symbol}, cannot check PnL later for this trade.")
        logging.info(f"📊 {symbol} | losses_count={losses_count} | mode={'RECOVERY' if recovery_mode else 'NORMAL'}")
        return True
    except Exception as e:
        msg = str(e).lower()
        logging.error(f"❌ {symbol} order failed: {e}")
        # classify insufficient margin / not enough
        if "insufficient" in msg or "not enough" in msg or "ab not enough" in msg or "not enough for new order" in msg:
            return "INSUFFICIENT"
        # any other error => False (skip)
        return False

# ================== MAIN LOOP ==================
def seconds_until_next_candle(interval_minutes):
    """Return seconds until next candle close aligned to UTC 00:00 multiples."""
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
            # Wait for next 4H candle close
            wait = seconds_until_next_candle(int(INTERVAL))
            logging.info(f"⏳ Waiting {wait}s for next {INTERVAL}m candle close...")
            time.sleep(wait + 2)  # small buffer

            # Process BTC first, then TRX as fallback if BTC returns INSUFFICIENT or fails
            btc_pair = next(p for p in PAIRS if p["symbol"] == "BTCUSDT")
            trx_pair = next(p for p in PAIRS if p["symbol"] == "TRXUSDT")

            btc_result = handle_symbol(btc_pair["symbol"], btc_pair["threshold"], btc_pair["leverage"])
            if btc_result == "INSUFFICIENT" or btc_result is False:
                logging.info("⚠️ BTC trade not placed or insufficient — trying TRX fallback.")
                trx_result = handle_symbol(trx_pair["symbol"], trx_pair["threshold"], trx_pair["leverage"])
                # if TRX returns INSUFFICIENT also, log and continue to next candle
                if trx_result == "INSUFFICIENT":
                    logging.warning("⚠️ TRX fallback also insufficient to open a trade.")
            # else BTC placed or no signal; continue to next candle
        except KeyboardInterrupt:
            logging.info("🛑 Stopped manually by user.")
            break
        except Exception as e:
            logging.error(f"Unhandled error in main loop: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
    
