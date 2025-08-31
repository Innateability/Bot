"""
Live Heikin-Ashi Bot for Bybit USDT Perpetual (One-Way Mode)
- Initial HA open configurable
- Hourly run at candle open (UTC)
- Incremental sizing + TP/SL update
- Uses pybit unified_trading (v3.x)
"""

import os
import time
import json
import logging
from datetime import datetime
from math import isclose

# pybit v3 unified client
from pybit.unified_trading import HTTP

# ---------------- CONFIG ----------------
SYMBOL = os.environ.get("SYMBOL", "TRXUSDT")
TIMEFRAME = os.environ.get("TIMEFRAME", "60")   # 1h
INITIAL_HA_OPEN = float(os.environ.get("INITIAL_HA_OPEN", "0.34134"))
TICK_SIZE = float(os.environ.get("TICK_SIZE", "0.00001"))
LEVERAGE = int(os.environ.get("LEVERAGE", "75"))
RISK_PERCENT = float(os.environ.get("RISK_PERCENT", "0.10"))
FALLBACK_PERCENT = float(os.environ.get("FALLBACK_PERCENT", "0.90"))
START_SIP_BALANCE = float(os.environ.get("START_SIP_BALANCE", "4.0"))
SIP_PERCENT = float(os.environ.get("SIP_PERCENT", "0.25"))
STATE_FILE = os.environ.get("STATE_FILE", "ha_state.json")

API_KEY = os.environ.get("BYBIT_API_KEY")
API_SECRET = os.environ.get("BYBIT_API_SECRET")
TESTNET = os.environ.get("BYBIT_TESTNET", "false").lower() in ("1", "true", "yes")

# ---------------- LOG ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ha_bot")

# ---------------- CLIENT ----------------
session = HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)

# ---------------- STATE ----------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ---------------- KLINES ----------------
def fetch_candles(symbol: str, interval: str = TIMEFRAME, limit: int = 200):
    """Return list of candles as dicts sorted oldest->newest.
    Handles pybit v3 response shape (result.list -> array rows).
    """
    # Call unified get_kline (pybit v3)
    out = session.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
    res = out.get("result", {}) or out
    # v3 often returns {'result': {'list': [[ts, open, high, low, close, ...], ...]}}
    if isinstance(res, dict) and "list" in res:
        rows = res["list"]
        parsed = []
        for r in rows:
            # r is array-like: [startTime, open, high, low, close, ...]
            parsed.append({
                "ts": int(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4])
            })
        # sort ascending by ts
        parsed.sort(key=lambda x: x["ts"])
        return parsed
    # fallback: sometimes result is just an array of dicts
    if isinstance(res, list):
        parsed = []
        for r in res:
            parsed.append({
                "ts": int(r.get("startTime", r.get("t", 0))),
                "open": float(r.get("open", r.get("openPrice", 0))),
                "high": float(r.get("high", r.get("highPrice", 0))),
                "low": float(r.get("low", r.get("lowPrice", 0))),
                "close": float(r.get("close", r.get("closePrice", 0)))
            })
        parsed.sort(key=lambda x: x["ts"])
        return parsed
    raise RuntimeError("Unexpected kline response shape")

# ---------------- HEIKIN-ASHI ----------------
def compute_heikin_ashi(raw_candles, persisted_open=None):
    """raw_candles: list of dicts oldest->newest
       persisted_open: if provided, used as HA open for the most recent (incomplete) candle
    """
    ha = []
    prev_ha_open = None
    prev_ha_close = None
    n = len(raw_candles)
    for i, c in enumerate(raw_candles):
        ro, rh, rl, rc = c["open"], c["high"], c["low"], c["close"]
        ha_close = (ro + rh + rl + rc) / 4.0
        if i == n - 1:
            # most recent (in-progress) candle: use persisted_open if provided else INITIAL_HA_OPEN
            if persisted_open is not None:
                ha_open = float(persisted_open)
            else:
                ha_open = float(INITIAL_HA_OPEN)
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

# ---------------- SIGNAL ----------------
def evaluate_signal(ha_list):
    if len(ha_list) < 2:
        return None
    prev = ha_list[-2]
    curr = ha_list[-1]
    prev_green = prev["ha_close"] > prev["ha_open"]
    prev_red = prev["ha_close"] < prev["ha_open"]
    # 1-tick approx (use <= TICK_SIZE)
    if prev_green and abs(prev["ha_low"] - prev["ha_open"]) <= TICK_SIZE:
        entry = curr["raw_open"]
        sl = curr["ha_open"]
        risk = abs(entry - sl)
        tp = entry + risk + 0.001 * entry
        return {"signal": "Buy", "entry": entry, "sl": sl, "tp": tp}
    if prev_red and abs(prev["ha_high"] - prev["ha_open"]) <= TICK_SIZE:
        entry = curr["raw_open"]
        sl = curr["ha_open"]
        risk = abs(entry - sl)
        tp = entry - (risk + 0.001 * entry)
        return {"signal": "Sell", "entry": entry, "sl": sl, "tp": tp}
    return None

# ---------------- BALANCE & POSITIONS ----------------
def get_balance_usdt():
    """Return available USDT balance (wallet)."""
    out = session.get_wallet_balance(coin="USDT")
    # v3 shape: result -> list -> [ { 'coin': 'USDT', 'walletBalance': '..', ...}, ... ]
    res = out.get("result", {}) or out
    # try multiple shapes
    if isinstance(res, dict) and "list" in res:
        lst = res["list"]
        for item in lst:
            # item is dict with 'coin' and balances in v3 shape
            if isinstance(item, dict) and item.get("coin") == "USDT":
                # walletBalance key may be walletBalance or balance
                return float(item.get("walletBalance", item.get("balance", 0)))
    # fallback
    try:
        return float(res["USDT"]["available_balance"])
    except Exception:
        raise RuntimeError("Unable to parse wallet balance response")

def get_open_position(symbol):
    """Return open position info dict or None."""
    out = session.get_positions(category="linear", symbol=symbol)
    res = out.get("result", {}) or out
    if isinstance(res, dict) and "list" in res and len(res["list"]) > 0:
        p = res["list"][0]
        return p
    return None

# ---------------- ORDER HELPERS ----------------
def set_symbol_leverage(symbol, leverage):
    try:
        session.set_leverage(category="linear", symbol=symbol, leverage=leverage)
    except Exception as e:
        logger.warning("set_leverage failed: %s", e)

def place_market_with_tp_sl(signal_side, symbol, qty, sl, tp):
    """Place market order and attach TP/SL (v3 shape)."""
    side = "Buy" if signal_side == "Buy" else "Sell"
    try:
        # place market order
        res = session.place_order(
            category="linear",
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=str(qty),
            timeInForce="ImmediateOrCancel",
            reduceOnly=False
        )
        logger.info("Placed market order: %s qty=%s", side, qty)
    except Exception as e:
        logger.exception("place_order failed: %s", e)
        return False

    # attach TP/SL using set_trading_stop
    try:
        session.set_trading_stop(
            category="linear",
            symbol=symbol,
            takeProfit=str(tp),
            stopLoss=str(sl),
            tpTriggerBy="LastPrice",
            slTriggerBy="LastPrice"
        )
        logger.info("Attached TP=%s SL=%s", tp, sl)
    except Exception as e:
        logger.exception("set_trading_stop failed: %s", e)
    return True

def modify_tp_sl_and_maybe_increase(symbol, new_sl, new_tp, new_qty):
    """Modify TP/SL for existing position and increase qty if new_qty > existing."""
    pos = get_open_position(symbol)
    if not pos:
        logger.info("No open position to modify")
        return False
    # parse current size and side
    size = 0.0
    entry_price = None
    side = None
    try:
        size = float(pos.get("size", pos.get("qty", 0)))
        entry_price = float(pos.get("entryPrice", pos.get("avgEntryPrice", 0)))
        side = "Buy" if float(pos.get("size", 0)) > 0 else "Sell"
    except Exception:
        # fallback keys
        size = float(pos.get("positionValue", 0) or 0)
    # always update TP/SL
    try:
        session.set_trading_stop(category="linear", symbol=symbol, takeProfit=str(new_tp), stopLoss=str(new_sl))
        logger.info("Updated TP/SL for existing position: SL=%s TP=%s", new_sl, new_tp)
    except Exception as e:
        logger.exception("Failed to update TP/SL: %s", e)

    # only increase if new_qty > size
    if new_qty > size:
        additional = new_qty - size
        # check available balance to decide fallback
        balance = get_balance_usdt()
        max_affordable = (balance * FALLBACK_PERCENT * LEVERAGE) / (entry_price if entry_price and entry_price>0 else new_qty)
        qty_to_open = min(additional, max_affordable)
        if qty_to_open <= 0:
            logger.info("Cannot afford additional qty, skipping increase")
            return True
        # place market to increase position (same side)
        side_to_place = side if side in ("Buy", "Sell") else ("Buy" if new_tp > new_sl else "Sell")
        placed = place_market_with_tp_sl(side_to_place, symbol, qty_to_open, new_sl, new_tp)
        logger.info("Increased position by %s (requested %s)", qty_to_open, additional)
        return placed
    else:
        logger.info("New qty <= current qty (%s <= %s) — only TP/SL updated", new_qty, size)
        return True

# ---------------- SIPHON ----------------
def siphon_if_needed(baseline_balance, symbol):
    bal = get_balance_usdt()
    if baseline_balance is None:
        return baseline_balance
    if baseline_balance >= START_SIP_BALANCE and bal >= 2 * baseline_balance:
        amount = round(bal * SIP_PERCENT)
        logger.info("Siphoning approx %s USDT to fund account (implement transfer API)", amount)
        # Implement actual transfer with session.transfer or equivalent if desired
        # session.transfer(...)
        # update baseline after siphon
        return bal
    return baseline_balance

# ---------------- MAIN FLOW ----------------
def run_once():
    state = load_state()
    persisted_ha_open = state.get("last_ha_open")
    baseline_balance = state.get("baseline_balance")

    # fetch candles and compute HA
    raw = fetch_candles(SYMBOL, TIMEFRAME, limit=200)
    ha_list = compute_heikin_ashi(raw, persisted_open=persisted_ha_open)
    # persist latest ha_open for next run
    latest_ha_open = ha_list[-1]["ha_open"]
    state["last_ha_open"] = latest_ha_open
    save_state(state)

    # logging latest two candles
    if len(ha_list) >= 2:
        prev, curr = ha_list[-2], ha_list[-1]
        logger.info("RAW prev: o/h/l/c %s %s %s %s", prev["raw_open"], prev["raw_high"], prev["raw_low"], prev["raw_close"])
        logger.info("HA prev: o/h/l/c %s %s %s %s", prev["ha_open"], prev["ha_high"], prev["ha_low"], prev["ha_close"])
        logger.info("RAW curr: o/h/l/c %s %s %s %s", curr["raw_open"], curr["raw_high"], curr["raw_low"], curr["raw_close"])
        logger.info("HA curr: o/h/l/c %s %s %s %s", curr["ha_open"], curr["ha_high"], curr["ha_low"], curr["ha_close"])

    # evaluate signal
    sig = evaluate_signal(ha_list)
    if not sig:
        logger.info("No signal this hour")
        return

    logger.info("Signal detected: %s", sig)
    bal = get_balance_usdt()
    if baseline_balance is None:
        baseline_balance = bal
        state["baseline_balance"] = baseline_balance
        save_state(state)

    # compute qty in base units
    qty = compute_qty(sig["entry"], sig["sl"], bal)
    if qty <= 0:
        logger.warning("Computed qty 0 — abort")
        return

    # set leverage
    set_symbol_leverage(SYMBOL, LEVERAGE)

    # check open pos
    pos = get_open_position(SYMBOL)
    if pos and float(pos.get("size", 0)) != 0:
        # modify TP/SL and increase qty if needed
        modify_open_position = modify_tp_sl_and_maybe_increase(SYMBOL, sig["sl"], sig["tp"], qty)
    else:
        # open new market order with TP/SL attached
        place_market_with_tp_sl(sig["signal"], SYMBOL, qty, sig["sl"], sig["tp"])

    # siphon if needed
    new_baseline = siphon_if_needed(baseline_balance, SYMBOL)
    if new_baseline != baseline_balance:
        state["baseline_balance"] = new_baseline
        save_state(state)

# ---------------- SCHEDULER ----------------
def wait_until_next_hour():
    now = datetime.utcnow()
    seconds = now.minute * 60 + now.second
    to_wait = 3600 - seconds
    if to_wait <= 0:
        to_wait = 1
    logger.info("Sleeping %s seconds until next hour", to_wait)
    time.sleep(to_wait)

if __name__ == "__main__":
    logger.info("Starting HA live bot (Bybit USDT perp) — testnet=%s", TESTNET)
    while True:
        try:
            run_once()
        except Exception:
            logger.exception("Error during run_once()")
        wait_until_next_hour()
