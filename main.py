#!/usr/bin/env python3
import os
import time
import math
import logging
from datetime import datetime, timezone
from pybit.unified_trading import HTTP
import pandas as pd  # moved import here for clarity

# ================== CONFIG (editable) ==================

PAIRS = [
    {"symbol": "BTCUSDT", "threshold": 0.006, "leverage": 100}
]
INTERVAL = "240"           # timeframe in minutes as string (e.g. "3", "240")
ROUNDING = 5               # decimals for TP/SL display
FALLBACK = 0.90            # fallback percentage for affordability
RISK_NORMAL = 0.2          # risk % of balance in normal mode
RISK_RECOVERY = 0.4        # risk % of balance in recovery mode
TP_NORMAL = 0.004          # normal TP pct (as fraction)
TP_RECOVERY = 0.004        # recovery TP pct (as fraction)
SL_PCT = 0.005             # stop loss percent used when placing trades (0.5% default)
QTY_SL_DIST_PCT = 0.006    # percent used to compute SL distance for qty calculation (0.6%)
EMA_LOOKBACK = 200         # how many closes to request (>=9)

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

# no testnet as requested
session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

# ================== LOGGING ==================

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

# ================== GLOBAL STATE ==================

losses_count = 0
last_pnl = 0.0
last_order_id = None
last_checked_time = {p["symbol"]: 0 for p in PAIRS}

# ================== HELPERS ==================

def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def fetch_candles_and_ema(symbol, interval=INTERVAL, limit=EMA_LOOKBACK):
    resp = session.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
    candles = list(reversed(resp["result"]["list"]))
    closes = [float(c[4]) for c in candles]

    # TradingView-accurate EMA using pandas
    ema_series = pd.Series(closes).ewm(span=9, adjust=False).mean()
    ema9 = ema_series.iloc[-2]  # last closed EMA

    last_closed_raw = candles[-2]
    last_closed = {
        "time": int(last_closed_raw[0]),
        "o": float(last_closed_raw[1]),
        "h": float(last_closed_raw[2]),
        "l": float(last_closed_raw[3]),
        "c": float(last_closed_raw[4]),
    }
    return last_closed, ema9, closes


def get_balance_usdt():
    """Return USDT wallet balance (or total equity fallback)."""
    try:
        resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        if "result" in resp and "list" in resp["result"] and resp["result"]["list"]:
            try:
                bal = float(resp["result"]["list"][0]["coin"][0]["walletBalance"])
                logging.info(f"💰 Wallet balance fetched: {bal:.8f} USDT")
                return bal
            except Exception:
                try:
                    bal2 = float(resp["result"]["list"][0]["totalEquity"])
                    logging.info(f"💰 Wallet total equity fetched: {bal2:.8f} USDT")
                    return bal2
                except Exception:
                    pass
    except Exception as e:
        logging.error(f"Error fetching balance: {e}")

    logging.info("💰 Wallet balance fetched: 0.0 USDT (fallback)")
    return 0.0


def calc_qty(balance, entry, sl, leverage, risk_percentage, symbol):
    """
    QTY is calculated using the REAL stop-loss distance = abs(entry - sl)
    max_affordable = (balance * leverage) / entry * FALLBACK
    Rounds BTC to nearest 0.001 (UP), TRX to whole number.
    """
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return 0.0

    # How much money we are willing to lose
    risk_amount = balance * risk_percentage

    # Quantity based on risk
    qty_by_risk = risk_amount / sl_dist

    # Maximum quantity the account can open
    max_affordable = (balance * leverage) / entry * FALLBACK if entry > 0 else 0.0

    # Choose the smaller of the two
    qty = min(qty_by_risk, max_affordable)

    # Rounding rules
    if "BTC" in symbol:
        # Round UP to nearest 0.001
        qty = math.ceil(qty * 1000) / 1000.0
    elif "TRX" in symbol:
        qty = round(qty)

    return max(qty, 0.001)


def place_order(symbol, signal, entry, sl, tp, qty):
    """
    Place market order and save last_order_id.
    """
    global last_order_id
    if qty is None or qty <= 0:
        raise ValueError("qty must be > 0")
    try:
        logging.info(f"🚀 Placing {signal.upper()} market order → Entry={entry:.8f} SL={sl:.8f} TP={tp:.8f} Qty={qty}")
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
        logging.info(f"✅ Order response: {resp}")
        try:
            if isinstance(resp, dict) and "result" in resp and resp["result"].get("orderId"):
                last_order_id = resp["result"]["orderId"]
                logging.info(f"🆔 Saved last_order_id = {last_order_id}")
        except Exception:
            pass
        return resp
    except Exception as e:
        logging.error(f"Error placing order on {symbol}: {e}")
        raise


# ================== PNL retrieval ==================

def get_pnl_for_order(order_id, symbol, search_limit=50):
    """
    Look up closed pnl entries and search for the provided order_id.
    Returns pnl float if found, else None.
    """
    try:
        resp = session.get_closed_pnl(category="linear", symbol=symbol, limit=search_limit)
        if "result" in resp and "list" in resp["result"] and resp["result"]["list"]:
            for t in resp["result"]["list"]:
                if t.get("orderId") == order_id:
                    pnl_val = t.get("closedPnl") or t.get("realisedPnl") or t.get("pnl")
                    if pnl_val is not None:
                        return float(pnl_val)
    except Exception as e:
        logging.error(f"Error fetching closed pnl for order_id {order_id} on {symbol}: {e}")
    return None


def get_most_recent_pnl_across_pairs():
    """
    If last_order_id exists, try to fetch PnL for that order.
    Otherwise fallback to finding the latest closed trade across pairs and return (symbol, pnl, order_id).
    """
    global last_pnl, last_order_id
    # If we have a saved last_order_id, try to fetch its pnl first (preferred).
    if last_order_id:
        for pair in PAIRS:
            p = pair["symbol"]
            pnl = get_pnl_for_order(last_order_id, p, search_limit=50)
            if pnl is not None:
                last_pnl = pnl
                logging.info(f"📊 Fetched PnL from last_order_id={last_order_id}: {pnl:.8f} USDT (symbol={p})")
                return p, pnl, last_order_id
        logging.info("⚠️ last_order_id present but not found in recent closed pnl lists.")

    # Fallback: find the most recent closed pnl across both pairs
    latest_trade = None
    latest_time = 0
    latest_symbol = None
    latest_order = None
    for pair in PAIRS:
        symbol = pair["symbol"]
        try:
            resp = session.get_closed_pnl(category="linear", symbol=symbol, limit=20)
            if "result" in resp and "list" in resp["result"] and resp["result"]["list"]:
                t = resp["result"]["list"][0]
                pnl_val = t.get("closedPnl") or t.get("realisedPnl") or t.get("pnl")
                time_val = int(t.get("updatedTime") or t.get("createdTime") or 0)
                order_id = t.get("orderId")
                if pnl_val is not None and time_val > latest_time:
                    latest_time = time_val
                    latest_trade = float(pnl_val)
                    latest_symbol = symbol
                    latest_order = order_id
        except Exception as e:
            logging.error(f"Error fetching closed pnl for {symbol}: {e}")

    if latest_symbol:
        last_pnl = latest_trade
        logging.info(f"📊 Most recent closed PnL: {latest_symbol} = {latest_trade:.8f} USDT (orderId={latest_order})")
        return latest_symbol, latest_trade, latest_order

    logging.info("🔎 No closed PnL found for BTC or TRX.")
    return None, None, None


# ================== CORE LOGIC ==================

def handle_symbol(symbol, threshold, leverage):
    """
    1) Fetch last closed candle + EMA9
    2) Determine raw signal (green/red and distance threshold)
    3) EMA9 confirmation
    4) Close positions, fetch PnL, adjust losses_count
    5) Compute qty and enforce min qty
    6) Place market order and log details
    """
    global losses_count

    # 1) candles + ema
    last_closed, ema9, closes = fetch_candles_and_ema(symbol)
    ts = datetime.utcfromtimestamp(last_closed["time"] / 1000).strftime("%Y-%m-%d %H:%M")
    o, h, l, c = last_closed["o"], last_closed["h"], last_closed["l"], last_closed["c"]
    logging.info(f"{symbol} | {ts} | Close={c:.8f} | EMA9={ema9:.8f}")

    # skip if same candle already processed
    if last_closed["time"] == last_checked_time[symbol]:
        return False
    last_checked_time[symbol] = last_closed["time"]

    # 2) raw signal detection
    signal = None
    if c > o and (h - o) / o >= threshold:
        signal = "buy"
    elif c < o and (o - l) / o >= threshold:
        signal = "sell"

    if not signal:
        logging.info(f"❌ {symbol}: No raw signal — skipping.")
        return False

    # 3) EMA confirmation
    if signal == "buy":
        if not (c > ema9):
            logging.info(f"❌ {symbol}: Buy rejected — Close={c:.8f} not above EMA9={ema9:.8f}")
            return False
        logging.info(f"✅ {symbol}: Buy confirmed → Close above EMA9.")
    else:
        if not (c < ema9):
            logging.info(f"❌ {symbol}: Sell rejected — Close={c:.8f} not below EMA9={ema9:.8f}")
            return False
        logging.info(f"✅ {symbol}: Sell confirmed → Close below EMA9.")

    # 4) Close positions and check PnL
    logging.info(f"📉 {symbol}: Confirmed {signal.upper()} signal → closing all positions before new trade.")
    for p in PAIRS:
        try:
            pos_resp = session.get_positions(category="linear", symbol=p["symbol"])
            if "result" in pos_resp and "list" in pos_resp["result"]:
                for pos in pos_resp["result"]["list"]:
                    size = float(pos.get("size", 0) or 0)
                    side = pos.get("side", "")
                    if size > 0:
                        close_side = "Sell" if side.lower() == "buy" else "Buy"
                        logging.info(f"🔻 Closing {side} position on {p['symbol']} size={size}")
                        session.place_order(
                            category="linear",
                            symbol=p["symbol"],
                            side=close_side,
                            orderType="Market",
                            qty=str(size),
                            reduceOnly=True,
                            timeInForce="IOC"
                        )
                        time.sleep(1)
        except Exception as e:
            logging.error(f"Error while closing positions for {p['symbol']}: {e}")

    # fetch pnl
    latest_symbol, pnl, order_id = get_most_recent_pnl_across_pairs()
    if pnl is not None:
        if pnl < 0:
            losses_count += 1
            logging.info(f"➕ Increased losses_count to {losses_count} (PnL loss {pnl:.8f})")
        elif pnl > 0:
            old = losses_count
            losses_count = max(0, losses_count - 1)
            logging.info(f"➖ Decremented losses_count {old} → {losses_count} (PnL gain {pnl:.8f})")
        else:
            logging.info(f"🔁 losses_count unchanged ({losses_count}) PnL={pnl:.8f}")
    else:
        logging.info("🔎 No PnL retrieved (no recent closed trade). losses_count unchanged.")

    # 5) build trade params
    recovery_mode = losses_count > 0
    risk_pct = RISK_RECOVERY if recovery_mode else RISK_NORMAL
    tp_pct = TP_RECOVERY if recovery_mode else TP_NORMAL
    
    SL_BUFFER_PCT = 0.0001
    
    entry = c
    
    if signal == "buy":
    # place SL slightly below the sequence low
        sl = last_closed["l"] * (1 - SL_BUFFER_PCT)
        tp = (entry * 1.0011) + (abs(entry - sl)/2)
    else:
    # place SL slightly above the sequence high
        sl = last_closed["h"] * (1 + SL_BUFFER_PCT)
        tp = (entry * 0.9989) - (abs(entry - sl)/2)
    balance = get_balance_usdt()
    qty =  calc_qty(balance, entry, sl, leverage, risk_pct, symbol)
    # minimum qty enforcement
    if "BTC" in symbol and qty < 0.001:
        logging.warning(f"⚠️ {symbol}: qty {qty:.6f} < 0.001 → skipping trade.")
        return False
    if "TRX" in symbol and qty < 1:
        logging.warning(f"⚠️ {symbol}: qty {qty:.6f} < 1 → skipping trade.")
        return False

    # log trade details
    logging.info(f"📐 Qty calc → balance={balance:.8f}, risk_pct={risk_pct}, qty={qty:.6f}")
    logging.info(f"📊 Preparing order → Entry={entry:.8f} SL={sl:.8f} TP={tp:.8f} (mode={'RECOVERY' if recovery_mode else 'NORMAL'})")

    # 6) place order
    try:
        resp = place_order(symbol, signal, entry, sl, tp, qty)
        return True
    except Exception as e:
        msg = str(e).lower()
        logging.error(f"❌ {symbol} order failed: {e}")
        if any(x in msg for x in ["insufficient", "not enough", "minimum", "exceeds minimum"]):
            logging.warning(f"⚠️ {symbol} trade insufficient or minimum error.")
            return "INSUFFICIENT"
        return False


# ================== SCHEDULER ==================

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
            time.sleep(wait + 1)

            btc_pair = next((p for p in PAIRS if p["symbol"] == "BTCUSDT"), None)
            trx_pair = next((p for p in PAIRS if p["symbol"] == "TRXUSDT"), None)
            if not btc_pair:
                logging.error("BTCUSDT pair missing from PAIRS — cannot continue.")
                return  # stop the bot
            if not trx_pair:
                logging.warning("TRXUSDT pair missing from PAIRS — TRX fallback disabled.")
                
            btc_result = handle_symbol(btc_pair["symbol"], btc_pair["threshold"], btc_pair["leverage"])
            if btc_result == "INSUFFICIENT" or btc_result is False:
                if trx_pair:  # only fallback if trx_pair exists
                    logging.info("⚠️ BTC skipped or insufficient — trying TRX fallback.")
                    trx_result = handle_symbol(trx_pair["symbol"], trx_pair["threshold"], trx_pair["leverage"])
                    if trx_result == "INSUFFICIENT":
                        logging.warning("⚠️ TRX fallback also insufficient.")
                else:
                    logging.warning("⚠️ TRX fallback disabled — TRXUSDT not in PAIRS.")
        except KeyboardInterrupt:
            logging.info("🛑 Stopped manually by user.")
            break
        except Exception as e:
            logging.error(f"Unhandled error in main loop: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
