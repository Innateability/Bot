#!/usr/bin/env python3
"""
Bybit Heikin-Ashi live/sim bot — Custom Rules
- Simulation mode runs through Bybit candles locally and updates sim balance when TP/SL hit.
- Uses persisted HA open for TradingView consistency.
"""

import os
import time
import json
import logging
from math import floor
from datetime import datetime
from pybit.unified_trading import HTTP

# ---------------- CONFIG ----------------
SYMBOL = os.environ.get("SYMBOL", "TRXUSDT")
TIMEFRAME = os.environ.get("TIMEFRAME", "240")  # 4h
INITIAL_HA_OPEN = float(os.environ.get("INITIAL_HA_OPEN", "0.34379"))
PIP = float(os.environ.get("PIP", "0.0001"))
TICK_SIZE = float(os.environ.get("TICK_SIZE", "0.00001"))
QTY_STEP = float(os.environ.get("QTY_STEP", "1"))
LEVERAGE = int(os.environ.get("LEVERAGE", "75"))

API_KEY = os.environ.get("BYBIT_API_KEY", "")
API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
TESTNET = os.environ.get("BYBIT_TESTNET", "false").lower() in ("1", "true", "yes")
ACCOUNT_TYPE = os.environ.get("BYBIT_ACCOUNT_TYPE", "UNIFIED")

STATE_FILE = os.environ.get("STATE_FILE", "ha_state.json")

# Risk rules
RISK_PERCENT = 0.045
BALANCE_USE_PERCENT = 0.45
FALLBACK_PERCENT = 0.45

# Simulation
SIMULATION_MODE = os.environ.get("SIMULATION_MODE", "false").lower() in ("1", "true", "yes")
INITIAL_BALANCE = float(os.environ.get("INITIAL_BALANCE", "100.0"))
sim_balance = INITIAL_BALANCE
sim_positions = []

# ---------------- LOG ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ha_bot")

# ---------------- CLIENT ----------------
session = HTTP(testnet=TESTNET, api_key=API_KEY or None, api_secret=API_SECRET or None)
if SIMULATION_MODE:
    logger.info("Running in SIMULATION MODE with starting balance %.2f USDT", sim_balance)

# ---------------- HELPERS ----------------
def round_price(p: float) -> float:
    return round(round(p / TICK_SIZE) * TICK_SIZE, 8)

def floor_to_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return floor(x / step) * step

def timeframe_ms() -> int:
    return int(TIMEFRAME) * 60 * 1000

# ---------------- STATE ----------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ---------------- KLINES ----------------
def fetch_candles(symbol: str, interval: str = TIMEFRAME, limit: int = 200):
    out = session.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
    rows = out.get("result", {}).get("list", []) if isinstance(out.get("result"), dict) else []
    candles = []
    for r in rows:
        try:
            ts = int(r[0])
            o = float(r[1]); h = float(r[2]); l = float(r[3]); c = float(r[4])
            candles.append({"ts": ts, "open": o, "high": h, "low": l, "close": c})
        except Exception:
            continue
    candles.sort(key=lambda x: x["ts"])
    return candles

# ---------------- HEIKIN-ASHI ----------------
def compute_heikin_ashi(raw_candles, persisted_open=None):
    ha = []
    prev_ha_open, prev_ha_close = None, None
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

# ---------------- ENTRY SIGNAL ----------------
def evaluate_signal(ha_list):
    if len(ha_list) < 2:
        return None
    prev_candle, last_candle = ha_list[-2], ha_list[-1]

    # BUY: last is green + higher high
    if last_candle["ha_close"] > last_candle["ha_open"] and last_candle["ha_high"] > prev_candle["ha_high"]:
        return {"signal": "Buy"}

    # SELL: last is red + lower low
    if last_candle["ha_close"] < last_candle["ha_open"] and last_candle["ha_low"] < prev_candle["ha_low"]:
        return {"signal": "Sell"}

    return None

# ---------------- MAIN RUN-ONCE ----------------
def run_once(raw=None):
    logger.info("=== New cycle ===")
    state = load_state()
    persisted_open = state.get("last_ha_open")

    # get candles
    if raw is None:
        raw = fetch_candles(SYMBOL, TIMEFRAME, limit=200)
        retrieval_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        logger.info("Candles retrieved at (real-world time): %s", retrieval_time)

    if not raw:
        logger.warning("No candles retrieved")
        return

    first_ts = raw[0]["ts"] / 1000
    logger.info("First candle retrieved starts at: %s", datetime.utcfromtimestamp(first_ts).strftime("%Y-%m-%d %H:%M:%S UTC"))

    # cut last incomplete
    now_ms = int(datetime.utcnow().timestamp() * 1000)
    if raw[-1]["ts"] + timeframe_ms() > now_ms:
        raw = raw[:-1]

    ha_list = compute_heikin_ashi(raw, persisted_open)
    last_closed = ha_list[-1]
    logger.info("Last closed candle time: %s", datetime.utcfromtimestamp(last_closed["ts"]/1000).strftime("%Y-%m-%d %H:%M:%S UTC"))

    # persist open for next run
    next_open = (last_closed["ha_open"] + last_closed["ha_close"]) / 2.0
    state["last_ha_open"] = float(next_open)
    save_state(state)

    sig = evaluate_signal(ha_list)
    if sig:
        logger.info("Signal detected: %s", sig["signal"])
    else:
        logger.info("No valid signal this cycle")

# ---------------- ENTRY POINT ----------------
def wait_until_next_cycle(hours=4):
    now = datetime.utcnow()
    cycle_seconds = hours * 3600
    elapsed = now.hour * 3600 + now.minute * 60 + now.second
    to_wait = cycle_seconds - (elapsed % cycle_seconds)
    if to_wait <= 0:
        to_wait += cycle_seconds
    logger.info("Sleeping %d seconds until next cycle", to_wait)
    time.sleep(to_wait)

if __name__ == "__main__":
    logger.info("Starting HA bot | SIMULATION=%s", SIMULATION_MODE)
    if SIMULATION_MODE:
        candles = fetch_candles(SYMBOL, TIMEFRAME, limit=200)
        for i in range(2, len(candles)):
            run_once(raw=candles[:i+1])
            time.sleep(0.1)
        logger.info("Simulation complete. Final balance=%.2f", sim_balance)
    else:
        wait_until_next_cycle(4)
        while True:
            try:
                run_once()
            except Exception:
                logger.exception("Error in run_once")
            wait_until_next_cycle(4)
