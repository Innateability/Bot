#!/usr/bin/env python3
"""
Heikin-Ashi Bot (Bybit Live)

Features:
- Live trading mode
- Uses HA-open of the *next* candle (fixes 1-candle lag issue)
- SL = HA-open of the next candle
- TP = 1:1 RR + 0.1% of entry
- Modify SL only if it reduces loss
- Modify TP only if it increases profit
- Logs all old vs new TP/SL values
"""

import os
import time
import json
import logging
from math import floor
from datetime import datetime
from typing import List, Dict, Optional, Any

from pybit.unified_trading import HTTP

# ---------------- CONFIG ----------------
SYMBOL = os.environ.get("SYMBOL", "TRXUSDT")
TIMEFRAME = os.environ.get("TIMEFRAME", "60")  # minutes
INITIAL_HA_OPEN = float(os.environ.get("INITIAL_HA_OPEN", "0.033861"))
TICK_SIZE = float(os.environ.get("TICK_SIZE", "0.00001"))
QTY_STEP = float(os.environ.get("QTY_STEP", "1"))
LEVERAGE = int(os.environ.get("LEVERAGE", "50"))
RISK_PERCENT = float(os.environ.get("RISK_PERCENT", "0.10"))  # 10% risk per trade
STATE_FILE = os.environ.get("STATE_FILE", "ha_state.json")
MIN_NEW_ORDER_QTY = float(os.environ.get("MIN_NEW_ORDER_QTY", "16"))

API_KEY = os.environ.get("BYBIT_API_KEY", "")
API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
TESTNET = False  # <<<<<<<<<<<< LIVE MODE
ACCOUNT_TYPE = "UNIFIED"

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ha_bot")

# ---------------- CLIENT ----------------
session = HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)

# ---------------- HELPERS ----------------
def round_price(p: float) -> float:
    ticks = round(p / TICK_SIZE)
    return round(ticks * TICK_SIZE, 10)

def floor_to_step(x: float, step: float) -> float:
    return floor(x / step) * step

def timeframe_ms() -> int:
    return int(TIMEFRAME) * 60 * 1000

# ---------------- CANDLES ----------------
def fetch_candles(symbol: str, interval: str = TIMEFRAME, limit: int = 200) -> List[Dict[str, float]]:
    out = session.get_kline(category="linear", symbol=symbol, interval=str(interval), limit=limit)
    rows = out.get("result", {}).get("list", [])
    parsed = []
    for r in rows:
        parsed.append({
            "ts": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
        })
    parsed.sort(key=lambda x: x["ts"])
    return parsed

# ---------------- HEIKIN-ASHI ----------------
def compute_heikin_ashi(raw_candles: List[Dict[str, float]],
                        persisted_open: Optional[float] = None) -> List[Dict[str, float]]:
    ha: List[Dict[str, float]] = []
    prev_ha_open, prev_ha_close = None, None
    n = len(raw_candles)

    for i, c in enumerate(raw_candles):
        ro, rh, rl, rc = c["open"], c["high"], c["low"], c["close"]
        ha_close = (ro + rh + rl + rc) / 4.0
        if i == n - 1 and persisted_open is not None:
            ha_open = persisted_open
        else:
            ha_open = (ro + rc) / 2.0 if prev_ha_open is None else (prev_ha_open + prev_ha_close) / 2.0
        ha_high = max(rh, ha_open, ha_close)
        ha_low = min(rl, ha_open, ha_close)
        ha.append({"ts": c["ts"], "ha_open": ha_open, "ha_close": ha_close, "ha_high": ha_high, "ha_low": ha_low,
                   "raw_open": ro, "raw_high": rh, "raw_low": rl, "raw_close": rc})
        prev_ha_open, prev_ha_close = ha_open, ha_close
    return ha

# ---------------- SIGNAL ----------------
def evaluate_signal(ha_list: List[Dict[str, float]]) -> Optional[str]:
    last = ha_list[-1]
    green = last["ha_close"] > last["ha_open"]
    red = last["ha_close"] < last["ha_open"]
    if green and abs(last["ha_low"] - last["ha_open"]) <= TICK_SIZE:
        return "Buy"
    if red and abs(last["ha_high"] - last["ha_open"]) <= TICK_SIZE:
        return "Sell"
    return None

# ---------------- POSITION ----------------
def get_open_position(symbol: str) -> Optional[Dict[str, Any]]:
    res = session.get_positions(category="linear", symbol=symbol)
    pos_list = res.get("result", {}).get("list", [])
    return pos_list[0] if pos_list else None

# ---------------- ORDERS ----------------
def place_market_with_tp_sl(signal: str, qty: float, entry: float, next_ha_open: float):
    side = "Buy" if signal == "Buy" else "Sell"
    sl = round_price(next_ha_open)
    risk = abs(entry - sl)
    if risk <= 0:
        logger.warning("Invalid SL, skipping order")
        return False
    tp = round_price(entry + risk + (0.001 * entry)) if side == "Buy" else round_price(entry - (risk + (0.001 * entry)))
    session.place_order(category="linear", symbol=SYMBOL, side=side, orderType="Market", qty=str(qty))
    session.set_trading_stop(category="linear", symbol=SYMBOL, takeProfit=str(tp), stopLoss=str(sl))
    logger.info("Placed %s order qty=%s TP=%.8f SL=%.8f", side, qty, tp, sl)
    return True

def modify_tp_sl_if_better(entry: float, next_ha_open: float):
    pos = get_open_position(SYMBOL)
    if not pos:
        return
    side = pos.get("side")
    entry_price = float(pos.get("entryPrice", entry))
    current_sl = float(pos.get("stopLoss") or 0)
    current_tp = float(pos.get("takeProfit") or 0)

    new_sl = round_price(next_ha_open)
    risk = abs(entry_price - new_sl)
    if risk <= 0:
        return
    new_tp = round_price(entry_price + risk + (0.001 * entry_price)) if side == "Buy" else round_price(entry_price - (risk + (0.001 * entry_price)))

    logger.info("Current TP=%.8f SL=%.8f | New TP=%.8f SL=%.8f", current_tp, current_sl, new_tp, new_sl)

    update_sl = (side == "Buy" and new_sl > current_sl) or (side == "Sell" and new_sl < current_sl) or current_sl == 0
    update_tp = (side == "Buy" and new_tp > current_tp) or (side == "Sell" and new_tp < current_tp) or current_tp == 0

    if update_sl or update_tp:
        session.set_trading_stop(category="linear", symbol=SYMBOL,
                                 takeProfit=str(new_tp if update_tp else current_tp),
                                 stopLoss=str(new_sl if update_sl else current_sl))
        logger.info("Updated TP/SL -> TP=%.8f SL=%.8f", new_tp if update_tp else current_tp, new_sl if update_sl else current_sl)

# ---------------- QTY ----------------
def compute_qty(entry: float, sl: float, balance: float) -> float:
    risk_usd = balance * RISK_PERCENT
    per_contract_risk = abs(entry - sl)
    if per_contract_risk <= 0:
        return 0.0
    qty = risk_usd / per_contract_risk
    return max(MIN_NEW_ORDER_QTY, floor_to_step(qty, QTY_STEP))

# ---------------- MAIN ----------------
def run_once(balance: float):
    raw = fetch_candles(SYMBOL, TIMEFRAME, limit=200)
    if len(raw) < 2:
        return
    now_ms = int(datetime.utcnow().timestamp() * 1000)
    if raw[-1]["ts"] + timeframe_ms() > now_ms:  # drop in-progress candle
        raw = raw[:-1]
    ha_list = compute_heikin_ashi(raw, INITIAL_HA_OPEN)
    last = ha_list[-1]

    # compute NEXT candle's ha_open (fix lag issue)
    next_ha_open = (last["ha_open"] + last["ha_close"]) / 2.0
    logger.info("Next candle ha_open = %.8f", next_ha_open)

    sig = evaluate_signal(ha_list)
    if sig:
        entry = last["raw_close"]
        qty = compute_qty(entry, next_ha_open, balance)
        place_market_with_tp_sl(sig, qty, entry, next_ha_open)
    else:
        modify_tp_sl_if_better(last["raw_close"], next_ha_open)

if __name__ == "__main__":
    # Here we don’t simulate balance – we’ll fetch from Bybit account balance
    while True:
        try:
            balance_data = session.get_wallet_balance(accountType=ACCOUNT_TYPE, coin="USDT")
            balance = float(balance_data["result"]["list"][0]["coin"][0]["walletBalance"])
        except Exception as e:
            logger.error("Could not fetch balance, using fallback 10 USDT. Error: %s", e)
            balance = 10.0
        run_once(balance)
        time.sleep(5)  # change to 3600 for hourly execution
