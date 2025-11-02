#!/usr/bin/env python3
import os
import time
import math
import logging
import pandas as pd
from datetime import datetime, timezone
from pybit.unified_trading import HTTP

# ================== CONFIG ==================
PAIRS = [
    {"symbol": "BTCUSDT", "threshold": 0.0006, "leverage": 100},
    {"symbol": "TRXUSDT", "threshold": 0.0006, "leverage": 75}
]

INTERVAL = "3"  # 4H
ROUNDING = 5
FALLBACK = 0.90
RISK_NORMAL = 0.1
RISK_RECOVERY = 0.2
TP_NORMAL = 0.004
TP_RECOVERY = 0.004
SL_PCT = 0.005
QTY_SL_DIST_PCT = 0.006

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

# ================== GLOBAL STATE ==================
losses_count = 0
last_pnl = 0.0
last_pnl_order_id = None
last_checked_time = {p["symbol"]: 0 for p in PAIRS}

# ================== HELPERS ==================
def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def fetch_last_closed_raw(symbol):
    """Fetch recent candles and return dataframe for EMA calculation + last closed candle."""
    resp = session.get_kline(category="linear", symbol=symbol, interval=INTERVAL, limit=50)
    if "result" not in resp or "list" not in resp["result"]:
        raise RuntimeError(f"Bad kline response: {resp}")

    candles = resp["result"]["list"]
    data = []
    for c in candles:
        data.append({
            "time": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4])
        })
    df = pd.DataFrame(data)
    return df

def get_balance_usdt():
    """Return USDT wallet balance."""
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
    """Close open positions."""
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

# ================== PNL ==================
def get_most_recent_pnl():
    global last_pnl
    latest_trade = None
    latest_time = 0
    latest_symbol = None
    latest_order_id = None

    for pair in PAIRS:
        symbol = pair["symbol"]
        try:
            resp = session.get_closed_pnl(category="linear", symbol=symbol, limit=1)
            if "result" in resp and "list" in resp["result"] and resp["result"]["list"]:
                trade = resp["result"]["list"][0]
                pnl_val = trade.get("closedPnl") or trade.get("realisedPnl") or trade.get("pnl")
                time_val = int(trade.get("updatedTime") or trade.get("createdTime") or 0)
                order_id = trade.get("orderId")
                if pnl_val is not None and time_val > latest_time:
                    latest_time = time_val
                    latest_trade = float(pnl_val)
                    latest_symbol = symbol
                    latest_order_id = order_id
        except Exception as e:
            logging.error(f"Error fetching closed pnl for {symbol}: {e}")

    if latest_symbol:
        last_pnl = latest_trade
        logging.info(f"📊 Most recent closed PnL: {latest_symbol} = {latest_trade:.8f} USDT (orderId={latest_order_id})")
        return latest_symbol, latest_trade, latest_order_id

    logging.info("🔎 No closed PnL found for BTC or TRX.")
    return None, None, None

# ================== HANDLE SYMBOL ==================
def handle_symbol(symbol, threshold, leverage):
    global losses_count, last_pnl_order_id

    df = fetch_last_closed_raw(symbol)
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()

    raw = df.iloc[-2]  # last closed candle
    c_time = raw["time"]
    o, h, l, c = raw["open"], raw["high"], raw["low"], raw["close"]
    ema9 = df["ema9"].iloc[-2]

    logging.info(f"🕒 {symbol} | O:{o:.6f} H:{h:.6f} L:{l:.6f} C:{c:.6f} | EMA9={ema9:.6f}")

    if c_time == last_checked_time[symbol]:
        return False
    last_checked_time[symbol] = c_time

    # Step 1: Detect signal
    is_green = c > o
    is_red = c < o
    signal = None
    if is_green and (h - o) / o > threshold and (o > ema9 and h > ema9):
        signal = "buy"
    elif is_red and (o - l) / o > threshold and (o < ema9 and l < ema9):
        signal = "sell"

    if not signal:
        logging.info(f"❌ {symbol}: No confirmed EMA-filtered signal — skipping.")
        return False

    logging.info(f"📉 Confirmed {signal.upper()} signal detected — closing all positions.")
    for pair in PAIRS:
        close_all_positions(pair["symbol"])

    latest_symbol, pnl, order_id = get_most_recent_pnl()

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

    if "BTC" in symbol and qty < 0.001:
        logging.warning(f"⚠️ {symbol}: qty {qty:.6f} < 0.001 → skipping trade.")
        return False
    if "TRX" in symbol and qty < 16:
        logging.warning(f"⚠️ {symbol}: qty {qty:.6f} < 16 → skipping trade.")
        return False

    logging.info(f"📐 Qty calc → balance={balance:.8f}, risk_pct={risk_pct}, qty={qty}")

    try:
        resp = place_order(symbol, signal, entry, sl, tp, qty)
        logging.info(f"📊 {symbol} | losses_count={losses_count} | mode={'RECOVERY' if recovery_mode else 'NORMAL'}")
        return True
    except Exception as e:
        msg = str(e).lower()
        logging.error(f"❌ {symbol} order failed: {e}")
        if any(err in msg for err in ["insufficient", "not enough"]):
            return "INSUFFICIENT"
        return False

# ================== ORDER ==================
def place_order(symbol, signal, entry, sl, tp, qty):
    if qty <= 0:
        raise ValueError("qty must be > 0")
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

# ================== MAIN LOOP ==================
def seconds_until_next_candle(interval_minutes):
    now = datetime.now(timezone.utc)
    candle_seconds = int(interval_minutes) * 60
    seconds_into_cycle = (now.hour * 3600 + now.minute * 60 + now.second) % candle_seconds
    wait = candle_seconds - seconds_into_cycle
    return candle_seconds if wait <= 0 else wait

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
                    logging.warning("⚠️ TRX fallback also insufficient.")
        except KeyboardInterrupt:
            logging.info("🛑 Stopped manually.")
            break
        except Exception as e:
            logging.error(f"Unhandled error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
    
