#!/usr/bin/env python3
import os
import time
import math
import logging
from datetime import datetime, timezone
import pandas as pd
from pybit.unified_trading import HTTP

# ================== CONFIG (editable) ==================
PAIRS = [
    {"symbol": "BTCUSDT", "threshold": 0.0006, "leverage": 100},
    {"symbol": "TRXUSDT", "threshold": 0.0006, "leverage": 75}
]

INTERVAL = "3"                # timeframe in minutes as string (e.g. "3", "240")
ROUNDING = 5
FALLBACK = 0.90
RISK_NORMAL = 0.1
RISK_RECOVERY = 0.2
TP_NORMAL = 0.004
TP_RECOVERY = 0.004
SL_PCT = 0.005
QTY_SL_DIST_PCT = 0.006
EMA_LOOKBACK = 20

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

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


def fetch_candles_and_ema(symbol, interval=INTERVAL, limit=EMA_LOOKBACK):
    """
    Fetch recent candles and compute EMA9 from closes using pandas.
    Returns (last_closed, ema9, closes_list)
    """
    resp = session.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
    if "result" not in resp or "list" not in resp["result"]:
        raise RuntimeError(f"Bad kline response: {resp}")
    candles = resp["result"]["list"]

    if len(candles) < 9:
        raise RuntimeError(f"Not enough candles for EMA9: got {len(candles)} for {symbol}")

    candles = list(reversed(candles))
    data = [
        {
            "time": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
        }
        for c in candles
    ]
    df = pd.DataFrame(data)
    ema_series = df["close"].ewm(span=9, adjust=False).mean()
    last_closed = df.iloc[-2]
    ema_value = ema_series.iloc[-2]

    last_closed_dict = {
        "time": int(last_closed["time"]),
        "o": last_closed["open"],
        "h": last_closed["high"],
        "l": last_closed["low"],
        "c": last_closed["close"],
    }

    return last_closed_dict, ema_value, df["close"].tolist()


def calc_qty(balance, entry, leverage, risk_percentage, symbol):
    sl_dist = entry * QTY_SL_DIST_PCT
    if sl_dist <= 0:
        return 0.0
    risk_amount = balance * risk_percentage
    qty_by_risk = risk_amount / sl_dist
    max_affordable = (balance * leverage) / entry * FALLBACK if entry > 0 else 0.0
    qty = min(qty_by_risk, max_affordable)
    if "BTC" in symbol:
        qty = math.floor(qty * 1000) / 1000.0
    elif "TRX" in symbol:
        qty = round(qty)
    return qty


def place_order(symbol, signal, entry, sl, tp, qty):
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
        if isinstance(resp, dict) and "result" in resp and resp["result"].get("orderId"):
            last_order_id = resp["result"]["orderId"]
            logging.info(f"🆔 Saved last_order_id = {last_order_id}")
        return resp
    except Exception as e:
        logging.error(f"Error placing order on {symbol}: {e}")
        raise


# ================== PNL retrieval ==================
def get_pnl_for_order(order_id, symbol, search_limit=50):
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
    global last_pnl, last_order_id
    if last_order_id:
        for pair in PAIRS:
            p = pair["symbol"]
            pnl = get_pnl_for_order(last_order_id, p, search_limit=50)
            if pnl is not None:
                last_pnl = pnl
                logging.info(f"📊 Fetched PnL from last_order_id={last_order_id}: {pnl:.8f} USDT (symbol={p})")
                return p, pnl, last_order_id
        logging.info("⚠️ last_order_id present but not found in recent closed pnl lists.")

    latest_trade, latest_time, latest_symbol, latest_order = None, 0, None, None
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
    global losses_count

    last_closed, ema9, closes = fetch_candles_and_ema(symbol)
    ts = datetime.utcfromtimestamp(last_closed["time"]/1000).strftime("%Y-%m-%d %H:%M")
    o, h, l, c = last_closed["o"], last_closed["h"], last_closed["l"], last_closed["c"]
    logging.info(f"{symbol} | {ts} | Close={c:.8f} | EMA9={ema9:.8f}")

    if last_closed["time"] == last_checked_time[symbol]:
        return False
    last_checked_time[symbol] = last_closed["time"]

    signal = None
    if c > o and (h - o) / o >= threshold:
        signal = "buy"
    elif c < o and (o - l) / o >= threshold:
        signal = "sell"

    if not signal:
        logging.info(f"❌ {symbol}: No raw signal — skipping.")
        return False

    if signal == "buy":
        if not (o > ema9 and h > ema9):
            logging.info(f"❌ {symbol}: Buy rejected by EMA9.")
            return False
        logging.info(f"✅ {symbol}: Buy confirmed by EMA9.")
    else:
        if not (o < ema9 and l < ema9):
            logging.info(f"❌ {symbol}: Sell rejected by EMA9.")
            return False
        logging.info(f"✅ {symbol}: Sell confirmed by EMA9.")

    logging.info(f"📉 {symbol}: Confirmed {signal.upper()} signal → closing all positions.")
    for p in PAIRS:
        try:
            pos_resp = session.get_positions(category="linear", symbol=p["symbol"])
            if "result" in pos_resp and "list" in pos_resp["result"]:
                for pos in pos_resp["result"]["list"]:
                    size = float(pos.get("size", 0) or 0)
                    side = pos.get("side", "")
                    if size > 0:
                        close_side = "Sell" if side.lower() == "buy" else "Buy"
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
            logging.error(f"Error closing positions for {p['symbol']}: {e}")

    latest_symbol, pnl, order_id = get_most_recent_pnl_across_pairs()
    if pnl is not None:
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

    if "BTC" in symbol and qty < 0.001:
        return False
    if "TRX" in symbol and qty < 1:
        return False

    logging.info(f"📊 Preparing order → Entry={entry:.8f} SL={sl:.8f} TP={tp:.8f}")
    return place_order(symbol, signal, entry, sl, tp, qty)


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

            btc_pair = next(p for p in PAIRS if p["symbol"] == "BTCUSDT")
            trx_pair = next(p for p in PAIRS if p["symbol"] == "TRXUSDT")

            btc_result = handle_symbol(btc_pair["symbol"], btc_pair["threshold"], btc_pair["leverage"])
            if btc_result == "INSUFFICIENT" or btc_result is False:
                trx_result = handle_symbol(trx_pair["symbol"], trx_pair["threshold"], trx_pair["leverage"])
                if trx_result == "INSUFFICIENT":
                    logging.warning("⚠️ TRX fallback also insufficient.")
        except KeyboardInterrupt:
            logging.info("🛑 Stopped manually by user.")
            break
        except Exception as e:
            logging.error(f"Unhandled error in main loop: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
