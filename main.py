#!/usr/bin/env python3
import os
import time
import math
import logging
from datetime import datetime, timezone
from pybit.unified_trading import HTTP

# ================== CONFIG (edit as needed) ==================
SYMBOL = "TRXUSDT"
INTERVAL = "240"                  # timeframe in minutes as string (e.g. "3","60","240")
RISK_PER_TRADE = 0.20           # 20% of balance
FALLBACK = 0.90                 # fallback % if qty unaffordable
LEVERAGE = 75
ROUNDING = 5                    # decimal places for TP/SL
CANDLE_POLL_GRANULARITY = 3     # seconds between retries fetching candles

# Set manually before first run (initial Heikin-Ashi open)
INITIAL_HA_OPEN = 0.33982

# API keys from environment
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

# ================== GLOBAL STATE ==================
range_signal = None            # "buy" or "sell" when raw & HA match
ha_open_prev = INITIAL_HA_OPEN
ha_close_prev = INITIAL_HA_OPEN
last_pnl = 0.0
last_order_id = None


# ================== HELPERS ==================
def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fetch_last_closed_raw():
    try:
        resp = session.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=3)
        if "result" not in resp or "list" not in resp["result"]:
            raise RuntimeError(f"Bad kline response: {resp}")

        raw = resp["result"]["list"][-2]
        parsed = {
            "time": int(raw[0]),
            "o": float(raw[1]),
            "h": float(raw[2]),
            "l": float(raw[3]),
            "c": float(raw[4])
        }
        logging.info(f"Parsed candle → O:{parsed['o']:.8f} H:{parsed['h']:.8f} "
                     f"L:{parsed['l']:.8f} C:{parsed['c']:.8f}")
        return parsed
    except Exception as e:
        logging.error(f"Error fetching kline: {e}")
        raise


def calc_heikin_ashi(raw, first_candle=False):
    global ha_open_prev, ha_close_prev
    ha_close = (raw["o"] + raw["h"] + raw["l"] + raw["c"]) / 4.0

    if first_candle:
        ha_open = INITIAL_HA_OPEN
    else:
        ha_open = (ha_open_prev + ha_close_prev) / 2.0

    ha_high = max(raw["h"], ha_open, ha_close)
    ha_low = min(raw["l"], ha_open, ha_close)

    ha_open_prev = ha_open
    ha_close_prev = ha_close
    return {"o": ha_open, "h": ha_high, "l": ha_low, "c": ha_close}


def get_balance_usdt():
    try:
        resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        balance = 0.0

        if "result" in resp and "list" in resp["result"] and resp["result"]["list"]:
            try:
                balance = float(resp["result"]["list"][0]["coin"][0]["walletBalance"])
            except Exception:
                try:
                    balance = float(resp["result"]["list"][0]["totalEquity"])
                except Exception:
                    balance = 0.0

        logging.info(f"💰 Wallet balance fetched: {balance:.8f} USDT")
        return balance
    except Exception as e:
        logging.error(f"Error fetching balance: {e}")
        return 0.0


def calc_qtys(balance, entry, sl):
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return 0.0, 0.0

    risk_amount = balance * RISK_PER_TRADE
    qty_by_risk = (risk_amount / sl_dist) * LEVERAGE
    max_affordable = (balance * LEVERAGE) / entry * FALLBACK

    logging.info(f"📐 Qty calc → RiskAmt={risk_amount:.8f}, SL Dist={sl_dist:.8f}, "
                 f"QtyByRisk={qty_by_risk:.4f}, MaxAffordable={max_affordable:.4f}")
    return qty_by_risk, max_affordable


def close_all_positions_and_get_last_pnl():
    global last_pnl, last_order_id
    try:
        pos_resp = session.get_positions(category="linear", symbol=SYMBOL)
        if "result" in pos_resp and "list" in pos_resp["result"] and pos_resp["result"]["list"]:
            for p in pos_resp["result"]["list"]:
                size = float(p.get("size", 0) or 0)
                side = p.get("side", "")
                if size > 0:
                    close_side = "Sell" if side.lower() == "buy" else "Buy"
                    logging.info(f"🔻 Closing existing {side} pos size={size}")
                    session.place_order(
                        category="linear",
                        symbol=SYMBOL,
                        side=close_side,
                        orderType="Market",
                        qty=str(size),
                        reduceOnly=True,
                        timeInForce="IOC"
                    )
                    time.sleep(2)

        resp = session.get_closed_pnl(category="linear", symbol=SYMBOL, limit=3)
        pnl = 0.0
        if "result" in resp and "list" in resp["result"] and resp["result"]["list"]:
            last_trade = resp["result"]["list"][0]
            pnl_val = last_trade.get("closedPnl") or last_trade.get("realisedPnl") or last_trade.get("pnl")
            order_id = last_trade.get("orderId")

            if pnl_val is not None:
                pnl = float(pnl_val)
                last_order_id = order_id
                last_pnl = pnl
                logging.info(f"📉 Latest closed trade → orderId={order_id} | PnL={pnl:.8f} USDT")
            else:
                logging.info("⚠️ No closedPnl found for recent trade.")
        else:
            logging.info("⚠️ No closed trades yet.")

        return last_pnl
    except Exception as e:
        logging.error(f"Error closing positions or fetching pnl: {e}")
        return last_pnl


def place_order_market(signal, entry, sl, tp, qty_int):
    global last_order_id
    try:
        sl_str = f"{round(sl, ROUNDING)}"
        tp_str = f"{round(tp, ROUNDING)}"
        logging.info(f"🚀 Placing {signal.upper()} market order → Entry={entry:.8f} SL={sl_str} "
                     f"TP={tp_str} Qty={qty_int}")

        resp = session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=signal.capitalize(),
            orderType="Market",
            qty=str(int(qty_int)),
            timeInForce="IOC",
            reduceOnly=False,
            takeProfit=tp_str,
            stopLoss=sl_str,
            positionIdx=0
        )
        logging.info(f"✅ Order response: {resp}")

        try:
            if "result" in resp and "orderId" in resp["result"]:
                last_order_id = resp["result"]["orderId"]
        except Exception:
            pass

        return resp
    except Exception as e:
        logging.error(f"Error placing order: {e}")
        return None


# 🧩 NEW: Fetch PnL for last saved order
def get_pnl_from_last_order():
    global last_order_id, last_pnl
    if not last_order_id:
        logging.info("⚠️ No last_order_id saved yet — skipping PnL fetch.")
        return last_pnl

    try:
        resp = session.get_closed_pnl(category="linear", symbol=SYMBOL, limit=10)
        if "result" in resp and "list" in resp["result"] and resp["result"]["list"]:
            for trade in resp["result"]["list"]:
                if trade.get("orderId") == last_order_id:
                    pnl_val = trade.get("closedPnl") or trade.get("realisedPnl") or trade.get("pnl")
                    if pnl_val is not None:
                        pnl = float(pnl_val)
                        last_pnl = pnl
                        logging.info(f"📊 Fetched PnL from last_order_id={last_order_id}: {pnl:.8f} USDT")
                        return pnl
            logging.info("⚠️ Last orderId not found in recent closed PnL list.")
        return last_pnl
    except Exception as e:
        logging.error(f"Error fetching pnl for last_order_id: {e}")
        return last_pnl


# ================== CORE LOGIC ==================
def handle_closed_candle():
    global range_signal, ha_open_prev, ha_close_prev, last_pnl

    raw = fetch_last_closed_raw()
    first_candle = (ha_open_prev == INITIAL_HA_OPEN and ha_close_prev == INITIAL_HA_OPEN and range_signal is None)
    ha = calc_heikin_ashi(raw, first_candle)
    raw_color = "buy" if raw["c"] > raw["o"] else "sell"
    ha_color = "buy" if ha["c"] > ha["o"] else "sell"

    logging.info(
        f"Candle {datetime.fromtimestamp(raw['time']/1000)} | Raw({raw_color}) "
        f"O:{raw['o']:.8f} H:{raw['h']:.8f} L:{raw['l']:.8f} C:{raw['c']:.8f} "
        f"| HA({ha_color}) O:{ha['o']:.8f} H:{ha['h']:.8f} L:{ha['l']:.8f} C:{ha['c']:.8f}"
    )

    # 🔹 Detect new range when raw & HA match
    if raw_color == ha_color:
        if range_signal != raw_color:
            logging.info(f"🔁 Range signal changed → {raw_color.upper()} (raw & HA matched). Resetting range.")
            range_signal = raw_color
    else:
        logging.info("↔ Raw and HA color do not match — range unchanged.")

    if range_signal is None:
        logging.info("No active range_signal yet — waiting for a matching raw & HA close.")
        return

    if raw_color == range_signal:
        logging.info(f"➡ Raw matches active range ({range_signal.upper()}) — preparing trade for this candle.")

        # 🧩 New rule: skip trade if open-high/low distance < 0.5%
        e = abs(raw["h"] - raw["o"]) if range_signal == "buy" else abs(raw["o"] - raw["l"])
        threshold = raw["o"] * 0.005

        if e <= threshold:
            logging.info(f"❌ Skipping trade: candle range below 0.5% "
                         f"(distance={e:.8f}, threshold={threshold:.8f}) — keeping current positions open.")
            return

        # ✅ Close existing trades if candle passes the 0.5% rule
        close_all_positions_and_get_last_pnl()
        last_pnl_local = get_pnl_from_last_order()

        recovery_flag = (last_pnl_local < 0)
        entry = raw["c"]
        sl = raw["l"] if range_signal == "buy" else raw["h"]

        balance = get_balance_usdt()
        qty_by_risk, max_affordable = calc_qtys(balance, entry, sl)

        if qty_by_risk <= 0 or max_affordable <= 0:
            logging.warning("⚠️ qty_by_risk or max_affordable <= 0, skipping trade.")
            return

        qty_int = int(min(qty_by_risk, max_affordable))
        if qty_int <= 0:
            logging.warning("⚠️ Final integer qty <= 0, skipping trade.")
            return

        if recovery_flag:
            pnl_abs = abs(last_pnl_local)
            price_move = pnl_abs / qty_int
            if range_signal == "buy":
                tp = entry + price_move + (entry * 0.0011)
            else:
                tp = entry - price_move - (entry * 0.0011)
            logging.info(f"⚡ Recovery trade → last_pnl={last_pnl_local:.8f}, qty={qty_int}, "
                         f"price_move={price_move:.8f}, TP={tp:.8f}")
        else:
            tp = entry * (1 + 0.0021) if range_signal == "buy" else entry * (1 - 0.0021)
            logging.info(f"✅ Normal trade → TP={tp:.8f} (±0.21%)")

        place_order_market(range_signal, entry, sl, tp, qty_int)
    else:
        logging.info(f"Raw color {raw_color.upper()} does not match active range {range_signal.upper()} — skipping trade this candle.")
        return last_pnl


# ================== MAIN LOOP ==================
def main():
    logging.info(f"🤖 Bot started | Symbol={SYMBOL} | TF={INTERVAL}m | "
                 f"Leverage={LEVERAGE}x | Risk={RISK_PER_TRADE*100:.0f}%")

    candle_seconds = int(INTERVAL) * 60

    while True:
        try:
            now = datetime.now(timezone.utc)
            seconds_into_cycle = (now.hour * 3600 + now.minute * 60 + now.second) % candle_seconds
            wait = candle_seconds - seconds_into_cycle
            if wait <= 0:
                wait += candle_seconds

            logging.info(f"⏳ Waiting {wait}s for next candle close...")
            time.sleep(wait + 2)

            for i in range(3):
                try:
                    handle_closed_candle()
                    break
                except Exception as e:
                    logging.warning(f"Attempt {i+1}/3 failed processing candle: {e}")
                    time.sleep(CANDLE_POLL_GRANULARITY)

        except KeyboardInterrupt:
            logging.info("Interrupted by user, exiting.")
            break
        except Exception as e:
            logging.error(f"Unhandled error in main loop: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
    
