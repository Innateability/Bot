#!/usr/bin/env python3
"""
Forward Heikin-Ashi Backtester for Bybit USDT Perp
"""

import requests
import math
import logging
from datetime import datetime

# -------- CONFIG --------

SYMBOL = "TRXUSDT"
INTERVAL = "60"       # 1h
LIMIT = 200           # number of candles to fetch
INITIAL_HA_OPEN = 0.3 # starting HA open to match TradingView
TICK_SIZE = 0.00001
LEVERAGE = 75
RISK_PERCENT = 0.10
FALLBACK_PERCENT = 0.90
QTY_STEP = 1
MIN_NEW_ORDER_QTY = 16

# -------- LOGGING --------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("backtest")

# -------- HELPERS --------

def floor_to_step(x, step):
    if step <= 0:
        return x
    return math.floor(x / step) * step

def round_price(p, tick=TICK_SIZE):
    ticks = round(p / tick)
    return round(ticks * tick, 8)

def ts_to_str(ts):
    return datetime.utcfromtimestamp(ts/1000).strftime("%Y-%m-%d %H:%M:%S")

# -------- DATA FETCH --------

def fetch_bybit_klines(symbol, interval, limit=200):
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params)
    r.raise_for_status()
    data = r.json()
    rows = data["result"]["list"]
    candles = []
    for r in rows:
        candles.append({
            "ts": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4])
        })
    candles.sort(key=lambda x: x["ts"])
    return candles

# -------- FORWARD HEIKIN-ASHI --------

def compute_ha_candle_forward(candle, prev_ha_open, prev_ha_close):
    ro, rh, rl, rc = candle["open"], candle["high"], candle["low"], candle["close"]
    ha_close = (ro + rh + rl + rc) / 4.0
    ha_open = prev_ha_open if prev_ha_close is None else (prev_ha_open + prev_ha_close) / 2.0
    ha_high = max(rh, ha_open, ha_close)
    ha_low = min(rl, ha_open, ha_close)
    return {
        "ts": candle["ts"],
        "raw_open": ro, "raw_high": rh, "raw_low": rl, "raw_close": rc,
        "ha_open": ha_open, "ha_high": ha_high, "ha_low": ha_low, "ha_close": ha_close
    }, ha_open, ha_close

# -------- SIGNAL --------

def evaluate_signal(last):
    green = last["ha_close"] > last["ha_open"]
    red = last["ha_close"] < last["ha_open"]
    if green and abs(last["ha_low"] - last["ha_open"]) <= TICK_SIZE:
        return "Buy"
    if red and abs(last["ha_high"] - last["ha_open"]) <= TICK_SIZE:
        return "Sell"
    return None

# -------- QTY --------

def compute_qty(entry, sl, balance):
    risk_usd = balance * RISK_PERCENT
    per_contract_risk = abs(entry - sl)
    if per_contract_risk <= 0:
        return 0.0
    qty = risk_usd / per_contract_risk
    est_margin = (qty * entry) / LEVERAGE
    if est_margin > balance:
        qty = (balance * FALLBACK_PERCENT * LEVERAGE) / entry
    return floor_to_step(qty, QTY_STEP)

# -------- BACKTEST --------

def backtest(balance=100):
    raw = fetch_bybit_klines(SYMBOL, INTERVAL, LIMIT)
    logger.info("Fetched %d candles. First candle UTC time = %s", len(raw), ts_to_str(raw[0]['ts']))
    logger.info("⚠️ Use this time to cross-check HA open in TradingView!")

    trades = []
    prev_ha_open = INITIAL_HA_OPEN
    prev_ha_close = None
    pos = {"Buy": None, "Sell": None}

    for candle in raw:
        ha_candle, prev_ha_open, prev_ha_close = compute_ha_candle_forward(candle, prev_ha_open, prev_ha_close)
        timestamp_str = ts_to_str(ha_candle["ts"])

        sig = evaluate_signal(ha_candle)
        if not sig:
            continue

        entry = ha_candle["raw_close"]
        sl = ha_candle["ha_open"]
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        tp = entry + risk + 0.001*entry if sig=="Buy" else entry - (risk + 0.001*entry)
        sl, tp = round_price(sl), round_price(tp)
        qty = max(compute_qty(entry, sl, balance), MIN_NEW_ORDER_QTY)

        current_trade = pos[sig]

        if not current_trade:
            # Open new trade
            pos[sig] = {"side": sig, "entry": entry, "sl": sl, "tp": tp, "qty": qty, "open_time": candle["ts"]}
            trades.append(pos[sig])
            logger.info("[%s] New %s trade | Entry=%.6f | SL=%.6f | TP=%.6f | qty=%.2f | Balance=%.2f",
                        timestamp_str, sig, entry, sl, tp, qty, balance)
            logger.info("    RAW O/H/L/C = %.6f / %.6f / %.6f / %.6f | HA O/H/L/C = %.6f / %.6f / %.6f / %.6f",
                        ha_candle["raw_open"], ha_candle["raw_high"], ha_candle["raw_low"], ha_candle["raw_close"],
                        ha_candle["ha_open"], ha_candle["ha_high"], ha_candle["ha_low"], ha_candle["ha_close"])
        else:
            # Update TP/SL if changed
            if current_trade["sl"] != sl or current_trade["tp"] != tp:
                current_trade["sl"] = sl
                current_trade["tp"] = tp
                logger.info("[%s] %s trade TP and SL changed to %.6f | %.6f | Entry=%.6f | qty=%.2f | Balance=%.2f",
                            timestamp_str, sig, tp, sl, current_trade["entry"], current_trade["qty"], balance)
                logger.info("    RAW O/H/L/C = %.6f / %.6f / %.6f / %.6f | HA O/H/L/C = %.6f / %.6f / %.6f / %.6f",
                            ha_candle["raw_open"], ha_candle["raw_high"], ha_candle["raw_low"], ha_candle["raw_close"],
                            ha_candle["ha_open"], ha_candle["ha_high"], ha_candle["ha_low"], ha_candle["ha_close"])

        # Mini-sim: check TP/SL hit and update balance
        for side, t in pos.items():
            if not t:
                continue
            last_low, last_high = ha_candle["raw_low"], ha_candle["raw_high"]
            pnl = 0
            hit = None

            if t["side"] == "Buy" and last_low <= t["sl"]:
                pnl = -abs(t["entry"] - t["sl"]) * t["qty"]
                hit = "SL"
            elif t["side"] == "Sell" and last_high >= t["sl"]:
                pnl = -abs(t["entry"] - t["sl"]) * t["qty"]
                hit = "SL"
            elif t["side"] == "Buy" and last_high >= t["tp"]:
                pnl = abs(t["tp"] - t["entry"]) * t["qty"]
                hit = "TP"
            elif t["side"] == "Sell" and last_low <= t["tp"]:
                pnl = abs(t["entry"] - t["tp"]) * t["qty"]
                hit = "TP"

            if hit:
                balance += pnl
                logger.info("[%s] %s trade %s hit | Entry=%.6f | SL=%.6f | TP=%.6f | qty=%.2f | Balance=%.2f",
                            timestamp_str, t["side"], hit, t["entry"], t["sl"], t["tp"], t["qty"], balance)
                logger.info("    RAW O/H/L/C = %.6f / %.6f / %.6f / %.6f | HA O/H/L/C = %.6f / %.6f / %.6f / %.6f",
                            ha_candle["raw_open"], ha_candle["raw_high"], ha_candle["raw_low"], ha_candle["raw_close"],
                            ha_candle["ha_open"], ha_candle["ha_high"], ha_candle["ha_low"], ha_candle["ha_close"])
                pos[side] = None

    logger.info("Backtest finished. Total trades opened = %d | Final Balance=%.2f", len(trades), balance)
    return trades

if __name__ == "__main__":
    backtest(balance=100)
