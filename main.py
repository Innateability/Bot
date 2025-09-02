#!/usr/bin/env python3
"""
Backtester for Heikin-Ashi Strategy (Bybit USDT Perp)
"""

import requests
import math
import logging
from datetime import datetime

# -------- CONFIG --------
SYMBOL = "TRXUSDT"
INTERVAL = "60"       # 1h
LIMIT = 200           # number of candles to fetch
INITIAL_HA_OPEN = 0.3 # set manually at deployment
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

# -------- HEIKIN-ASHI --------
def compute_heikin_ashi(raw_candles, persisted_open=None):
    ha = []
    prev_ha_open = None
    prev_ha_close = None
    n = len(raw_candles)
    for i, c in enumerate(raw_candles):
        ro, rh, rl, rc = c["open"], c["high"], c["low"], c["close"]
        ha_close = (ro + rh + rl + rc) / 4.0
        if i == n - 1:
            ha_open = float(persisted_open) if persisted_open is not None else INITIAL_HA_OPEN
        else:
            if prev_ha_open is None:
                ha_open = (ro + rc) / 2.0
            else:
                ha_open = (prev_ha_open + prev_ha_close) / 2.0
        ha_high = max(rh, ha_open, ha_close)
        ha_low = min(rl, ha_open, ha_close)
        ha.append({
            "ts": c["ts"],
            "raw_open": ro, "raw_high": rh, "raw_low": rl, "raw_close": rc,
            "ha_open": ha_open, "ha_high": ha_high, "ha_low": ha_low, "ha_close": ha_close
        })
        prev_ha_open, prev_ha_close = ha_open, ha_close
    return ha

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
    logger.info("Fetched %d candles. First candle UTC time = %s", len(raw), datetime.utcfromtimestamp(raw[0]['ts']/1000))
    logger.info("⚠️ Use this time to cross-check HA open in TradingView!")

    trades = []
    state_ha_open = INITIAL_HA_OPEN
    pos = {"Buy": None, "Sell": None}  # track one trade per side

    for i in range(1, len(raw)):
        ha_list = compute_heikin_ashi(raw[:i+1], persisted_open=state_ha_open)
        last = ha_list[-1]
        next_ha_open = (last["ha_open"] + last["ha_close"]) / 2.0
        state_ha_open = next_ha_open

        sig = evaluate_signal(last)
        if not sig:
            continue

        entry = last["raw_close"]
        sl = next_ha_open
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        tp = entry + risk + 0.001*entry if sig=="Buy" else entry - (risk + 0.001*entry)
        sl, tp = round_price(sl), round_price(tp)

        qty = compute_qty(entry, sl, balance)
        final_qty = max(qty, MIN_NEW_ORDER_QTY)

        current_trade = pos[sig]

        if not current_trade:
            # No open trade for this side -> open new
            pos[sig] = {"side": sig, "entry": entry, "sl": sl, "tp": tp, "qty": final_qty, "open_time": last["ts"]}
            trades.append(pos[sig])
            logger.info("New %s trade at %.6f | SL=%.6f | TP=%.6f | qty=%.2f", sig, entry, sl, tp, final_qty)
        else:
            # Trade exists -> check for SL/TP update
            modified = False
            if current_trade["sl"] != sl:
                logger.info("%s trade SL updated: %.6f -> %.6f", sig, current_trade["sl"], sl)
                current_trade["sl"] = sl
                modified = True
            if current_trade["tp"] != tp:
                logger.info("%s trade TP updated: %.6f -> %.6f", sig, current_trade["tp"], tp)
                current_trade["tp"] = tp
                modified = True
            if modified:
                logger.info("%s trade now Entry=%.6f | SL=%.6f | TP=%.6f | qty=%.2f", sig, current_trade["entry"], current_trade["sl"], current_trade["tp"], current_trade["qty"])

        # Mini-sim: check if trade hits TP or SL
        for side, t in pos.items():
            if not t:
                continue
            last_low, last_high = last["raw_low"], last["raw_high"]
            if t["side"] == "Buy" and last_low <= t["sl"]:
                logger.info("Buy trade SL hit at %.6f | Entry=%.6f | TP=%.6f", t["sl'], t["entry'], t["tp'])
                pos[side] = None
            elif t["side"] == "Sell" and last_high >= t["sl"]:
                logger.info("Sell trade SL hit at %.6f | Entry=%.6f | TP=%.6f", t["sl'], t["entry'], t["tp'])
                pos[side] = None
            elif t["side"] == "Buy" and last_high >= t["tp"]:
                logger.info("Buy trade TP hit at %.6f | Entry=%.6f | SL=%.6f", t["tp'], t["entry'], t["sl'])
                pos[side] = None
            elif t["side"] == "Sell" and last_low <= t["tp"]:
                logger.info("Sell trade TP hit at %.6f | Entry=%.6f | SL=%.6f", t["tp'], t["entry'], t["sl'])
                pos[side] = None

    logger.info("Backtest finished. Total trades opened = %d", len(trades))
    return trades

if __name__ == "__main__":
    backtest(balance=100)
    
