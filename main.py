#!/usr/bin/env python3
"""
Live Heikin-Ashi Bot for Bybit USDT Perpetual (One-Way Mode)

Fixes:
- Robust wallet balance parsing
- Align to top-of-hour on startup (so initial persisted/INITIAL_HA_OPEN is used for the first closed candle)
- Detect & drop in-progress candle so OHLC/HA are computed only on closed candles
- Detailed logging (raw + HA OHLC, balance, absolute qty, order events)
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
TIMEFRAME = os.environ.get("TIMEFRAME", "60")   # minutes
INITIAL_HA_OPEN = float(os.environ.get("INITIAL_HA_OPEN", "0.34108"))
TICK_SIZE = float(os.environ.get("TICK_SIZE", "0.00001"))
QTY_STEP = float(os.environ.get("QTY_STEP", "1"))
LEVERAGE = int(os.environ.get("LEVERAGE", "75"))
RISK_PERCENT = float(os.environ.get("RISK_PERCENT", "0.10"))
FALLBACK_PERCENT = float(os.environ.get("FALLBACK_PERCENT", "0.90"))
START_SIP_BALANCE = float(os.environ.get("START_SIP_BALANCE", "4.0"))
SIP_PERCENT = float(os.environ.get("SIP_PERCENT", "0.25"))
STATE_FILE = os.environ.get("STATE_FILE", "ha_state.json")

API_KEY = os.environ.get("BYBIT_API_KEY")
API_SECRET = os.environ.get("BYBIT_API_SECRET")
TESTNET = os.environ.get("BYBIT_TESTNET", "false").lower() in ("1", "true", "yes")
ACCOUNT_TYPE = os.environ.get("BYBIT_ACCOUNT_TYPE", "UNIFIED")  # 'UNIFIED' or 'CONTRACT'

# ---------------- LOG ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ha_bot")

# ---------------- CLIENT ----------------
session = HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)

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

# ---------------- HELPERS ----------------
def round_price(p: float) -> float:
    if TICK_SIZE <= 0:
        return p
    ticks = round(p / TICK_SIZE)
    return round(ticks * TICK_SIZE, 8)

def floor_to_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return floor(x / step) * step

def timeframe_ms() -> int:
    """Return timeframe in milliseconds (TIMEFRAME is minutes)."""
    try:
        return int(TIMEFRAME) * 60 * 1000
    except Exception:
        return 60 * 60 * 1000

# ---------------- KLINES ----------------
def fetch_candles(symbol: str, interval: str = TIMEFRAME, limit: int = 200):
    """
    Return list of candles (oldest -> newest) parsed to dicts.
    """
    out = session.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
    res = out.get("result", {}) or out

    # v3 shape: {'result': {'list': [[ts, open, high, low, close, ...], ...]}}
    if isinstance(res, dict) and "list" in res and isinstance(res["list"], list):
        rows = res["list"]
        parsed = []
        for r in rows:
            try:
                parsed.append({
                    "ts": int(r[0]),       # startTime in ms
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4])
                })
            except Exception:
                continue
        parsed.sort(key=lambda x: x["ts"])
        return parsed

    # fallback: list of dicts
    if isinstance(res, list):
        parsed = []
        for r in res:
            try:
                parsed.append({
                    "ts": int(r.get("startTime", r.get("t", 0))),
                    "open": float(r.get("open", r.get("openPrice", 0))),
                    "high": float(r.get("high", r.get("highPrice", 0))),
                    "low": float(r.get("low", r.get("lowPrice", 0))),
                    "close": float(r.get("close", r.get("closePrice", 0)))
                })
            except Exception:
                continue
        parsed.sort(key=lambda x: x["ts"])
        return parsed

    raise RuntimeError("Unexpected kline response shape: {}".format(res))

# ---------------- HEIKIN-ASHI ----------------
def compute_heikin_ashi(raw_candles, persisted_open=None):
    """
    raw_candles: list oldest->newest
    persisted_open: used as HA-open for the LAST (most-recent closed) candle
    """
    ha = []
    prev_ha_open = None
    prev_ha_close = None
    n = len(raw_candles)
    for i, c in enumerate(raw_candles):
        ro, rh, rl, rc = c["open"], c["high"], c["low"], c["close"]
        ha_close = (ro + rh + rl + rc) / 4.0
        if i == n - 1:
            ha_open = float(persisted_open) if persisted_open is not None else float(INITIAL_HA_OPEN)
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
    """Return {'signal': 'Buy'|'Sell'} or None (based on last closed HA candle)."""
    if len(ha_list) < 1:
        return None
    last = ha_list[-1]
    green = last["ha_close"] > last["ha_open"]
    red = last["ha_close"] < last["ha_open"]
    if green and abs(last["ha_low"] - last["ha_open"]) <= TICK_SIZE:
        return {"signal": "Buy"}
    if red and abs(last["ha_high"] - last["ha_open"]) <= TICK_SIZE:
        return {"signal": "Sell"}
    return None

# ---------------- BALANCE & POSITIONS ----------------
def get_balance_usdt():
    """Robustly parse different wallet response shapes and return USDT available balance."""
    try:
        out = session.get_wallet_balance(accountType=ACCOUNT_TYPE, coin="USDT")
    except Exception as e:
        logger.exception("get_wallet_balance error: %s", e)
        raise

    res = out.get("result", {}) or out

    # Case A: result -> list -> items that may contain 'coin' which is either str or a list
    if isinstance(res, dict) and "list" in res and isinstance(res["list"], list):
        for item in res["list"]:
            # If item['coin'] is a list of coin dicts (your posted error shape)
            coins = item.get("coin")
            if isinstance(coins, list):
                for c in coins:
                    if isinstance(c, dict) and c.get("coin") == "USDT":
                        # prefer availableToWithdraw/availableBalance then walletBalance/usdValue/equity
                        for key in ("availableToWithdraw", "availableBalance", "walletBalance", "usdValue", "equity"):
                            if key in c and c[key] not in (None, "", " "):
                                try:
                                    return float(c[key])
                                except Exception:
                                    continue
                        # last resort: try any numeric-like value
                        for k, v in c.items():
                            try:
                                return float(v)
                            except Exception:
                                continue
            # If item['coin'] is a string (per-coin record)
            if isinstance(item.get("coin"), str) and item.get("coin") == "USDT":
                for key in ("availableToWithdraw", "availableBalance", "walletBalance", "usdValue", "equity"):
                    if key in item and item[key] not in (None, "", " "):
                        try:
                            return float(item[key])
                        except Exception:
                            continue
                for k, v in item.items():
                    try:
                        return float(v)
                    except Exception:
                        continue

    # Case B: result -> {'USDT': {...}}
    try:
        if isinstance(res, dict) and "USDT" in res:
            u = res["USDT"]
            for key in ("available_balance", "availableBalance", "available_balance_str", "available_balance_usd"):
                if key in u:
                    try:
                        return float(u.get(key))
                    except Exception:
                        pass
            # fallback
            for key in ("available_balance", "walletBalance", "totalWalletBalance", "availableBalance"):
                if key in u:
                    try:
                        return float(u.get(key))
                    except Exception:
                        pass
            # any numeric
            for k, v in u.items():
                try:
                    return float(v)
                except Exception:
                    continue
    except Exception:
        pass

    logger.error("Unable to parse wallet balance response: %s", out)
    raise RuntimeError("Unable to parse wallet balance response")

def get_open_position(symbol):
    try:
        out = session.get_positions(category="linear", symbol=symbol)
    except Exception as e:
        logger.exception("get_positions error: %s", e)
        return None
    res = out.get("result", {}) or out
    if isinstance(res, dict) and "list" in res and len(res["list"]) > 0:
        return res["list"][0]
    return None

# ---------------- ORDER HELPERS ----------------
def ensure_one_way(symbol):
    try:
        session.switch_position_mode(category="linear", symbol=symbol, mode=0)  # 0 = one-way
        logger.info("Ensured one-way mode for %s", symbol)
    except Exception as e:
        logger.debug("switch_position_mode ignored/failed: %s", e)

def set_symbol_leverage(symbol, leverage):
    try:
        session.set_leverage(category="linear", symbol=symbol, buyLeverage=leverage, sellLeverage=leverage)
        logger.info("Set leverage=%sx for %s", leverage, symbol)
    except Exception as e:
        logger.warning("set_leverage failed: %s", e)

def place_market_with_tp_sl(signal_side, symbol, qty, sl, tp):
    side = "Buy" if signal_side == "Buy" else "Sell"
    qty_str = str(qty)
    sl_str = str(round_price(sl))
    tp_str = str(round_price(tp))
    try:
        resp = session.place_order(
            category="linear",
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=qty_str,
            timeInForce="ImmediateOrCancel",
            reduceOnly=False
        )
        logger.info("Placed market order: %s qty=%s resp=%s", side, qty_str, resp)
    except Exception as e:
        logger.exception("place_order failed: %s", e)
        return False
    try:
        resp2 = session.set_trading_stop(
            category="linear",
            symbol=symbol,
            takeProfit=tp_str,
            stopLoss=sl_str,
            tpTriggerBy="LastPrice",
            slTriggerBy="LastPrice"
        )
        logger.info("Attached TP=%s SL=%s resp=%s", tp_str, sl_str, resp2)
    except Exception as e:
        logger.exception("set_trading_stop failed: %s", e)
    return True

def modify_tp_sl_and_maybe_increase(symbol, new_sl, new_tp, new_qty):
    pos = get_open_position(symbol)
    if not pos:
        logger.info("No open position to modify for %s", symbol)
        return False
    try:
        size = float(pos.get("size") or pos.get("qty") or 0)
    except Exception:
        size = 0.0
    try:
        entry_price = float(pos.get("entryPrice") or pos.get("avgEntryPrice") or 0)
    except Exception:
        entry_price = 0.0
    side = pos.get("side") or ("Buy" if size > 0 else "Sell")
    logger.info("Existing position: side=%s size=%.8f entry_price=%.8f", side, size, entry_price)
    # update TP/SL
    try:
        session.set_trading_stop(
            category="linear",
            symbol=symbol,
            takeProfit=str(round_price(new_tp)),
            stopLoss=str(round_price(new_sl)),
            tpTriggerBy="LastPrice",
            slTriggerBy="LastPrice"
        )
        logger.info("Updated TP/SL for existing position: SL=%s TP=%s", new_sl, new_tp)
    except Exception as e:
        logger.exception("Failed to update TP/SL: %s", e)
    # increase if needed
    if new_qty > size:
        additional = new_qty - size
        balance = get_balance_usdt()
        denom = entry_price if entry_price > 0 else 1.0
        max_affordable = (balance * FALLBACK_PERCENT * LEVERAGE) / denom
        qty_to_open = floor_to_step(min(additional, max_affordable), QTY_STEP)
        if qty_to_open <= 0:
            logger.info("Cannot afford additional qty (<=0) — skipping increase")
            return True
        logger.info("Attempting to increase by %s (requested additional %s, max_affordable %s)", qty_to_open, additional, max_affordable)
        placed = place_market_with_tp_sl(side, symbol, qty_to_open, new_sl, new_tp)
        logger.info("Increase placement result: %s", placed)
        return placed
    else:
        logger.info("New qty <= current qty (%.8f <= %.8f) — only TP/SL updated", new_qty, size)
        return True

# ---------------- RISK / QTY ----------------
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

# ---------------- SIPHON ----------------
def siphon_if_needed(baseline_balance):
    bal = get_balance_usdt()
    if baseline_balance is None:
        return baseline_balance
    if baseline_balance >= START_SIP_BALANCE and bal >= 2 * baseline_balance:
        amount = round(bal * SIP_PERCENT)
        logger.info("Siphoning approx %s USDT to fund account (implement transfer API)", amount)
        # Implement transfer if desired
        return bal
    return baseline_balance

# ---------------- MAIN FLOW ----------------
def run_once():
    logger.info("=== Running hourly check ===")
    state = load_state()
    persisted_ha_open = state.get("last_ha_open")
    baseline_balance = state.get("baseline_balance")

    # If persisted HA exists, acknowledge it in logs; otherwise show initial HA open usage
    if persisted_ha_open is not None:
        logger.info("Loaded persisted last_ha_open = %.8f", float(persisted_ha_open))
    else:
        logger.info("No persisted last_ha_open found — using INITIAL_HA_OPEN = %.8f for the last closed candle", float(INITIAL_HA_OPEN))

    # fetch candles
    try:
        raw = fetch_candles(SYMBOL, TIMEFRAME, limit=200)
    except Exception as e:
        logger.exception("Failed fetching candles: %s", e)
        return

    if not raw or len(raw) < 2:
        logger.warning("Not enough candles; skipping this run.")
        return

    # If the most recent returned candle is in-progress (start_ts + timeframe_ms > now), drop it
    period = timeframe_ms()
    now_ms = int(datetime.utcnow().timestamp() * 1000)
    if raw and (raw[-1]["ts"] + period) > now_ms + 1000:
        logger.info("Detected in-progress candle at %s (ts=%d). Dropping it and using prior closed candles.", datetime.utcfromtimestamp(raw[-1]["ts"]/1000).isoformat(), raw[-1]["ts"])
        raw = raw[:-1]
        if not raw:
            logger.warning("After dropping in-progress candle no closed candles remain; skipping run.")
            return

    # compute HA using persisted_open for the last closed candle
    ha_list = compute_heikin_ashi(raw, persisted_open=persisted_ha_open)
    last_closed = ha_list[-1]  # last closed HA candle (signal candle)

    # log raw + HA values for the closed candle
    logger.info("RAW last closed: o/h/l/c = %.8f / %.8f / %.8f / %.8f",
                last_closed["raw_open"], last_closed["raw_high"], last_closed["raw_low"], last_closed["raw_close"])
    logger.info("HA  last closed: o/h/l/c = %.8f / %.8f / %.8f / %.8f",
                last_closed["ha_open"], last_closed["ha_high"], last_closed["ha_low"], last_closed["ha_close"])

    # compute next HA-open (for upcoming new candle) and persist it
    next_ha_open = (last_closed["ha_open"] + last_closed["ha_close"]) / 2.0
    state["last_ha_open"] = float(next_ha_open)
    save_state(state)
    logger.info("Persisted next_ha_open = %.8f", next_ha_open)

    # evaluate signal (based on last closed HA candle)
    sig = evaluate_signal(ha_list)
    if not sig:
        logger.info("No signal detected this hour")
        return
    logger.info("Signal detected: %s", sig["signal"])

    # approximate entry = last raw close (top-of-hour), SL = next_ha_open
    entry = float(last_closed["raw_close"])
    sl = float(next_ha_open)
    risk = abs(entry - sl)
    if risk <= 0:
        logger.info("Zero risk (entry == SL); skipping")
        return

    if sig["signal"] == "Buy":
        tp = entry + risk + 0.001 * entry
    else:
        tp = entry - (risk + 0.001 * entry)

    sl = round_price(sl)
    tp = round_price(tp)

    logger.info("Signal=%s | Entry≈%.8f | SL=%.8f | TP=%.8f", sig["signal"], entry, sl, tp)

    # balance & qty
    try:
        bal = get_balance_usdt()
    except Exception as e:
        logger.exception("Could not fetch balance: %s", e)
        return

    logger.info("Available USDT balance = %.8f", bal)
    if baseline_balance is None:
        baseline_balance = bal
        state["baseline_balance"] = baseline_balance
        save_state(state)
        logger.info("Set baseline_balance = %.8f", baseline_balance)

    qty = compute_qty(entry, sl, bal)
    logger.info("Calculated absolute quantity (base units) = %.8f", qty)
    if qty <= 0:
        logger.warning("Computed qty <= 0; aborting trade this hour")
        return

    # ensure one-way and leverage
    ensure_one_way(SYMBOL)
    set_symbol_leverage(SYMBOL, LEVERAGE)

    # trade logic
    pos = get_open_position(SYMBOL)
    if pos and float(pos.get("size", 0) or 0) != 0:
        logger.info("Existing open position found; attempting to modify TP/SL and increase qty if needed")
        modified = modify_tp_sl_and_maybe_increase(SYMBOL, sl, tp, qty)
        logger.info("Modify/increase result: %s", modified)
    else:
        logger.info("No open position — placing new market order with attached TP/SL")
        placed = place_market_with_tp_sl(sig["signal"], SYMBOL, qty, sl, tp)
        logger.info("Place order result: %s", placed)

    # siphon logic
    new_baseline = siphon_if_needed(baseline_balance)
    if new_baseline != baseline_balance:
        state["baseline_balance"] = new_baseline
        save_state(state)
        logger.info("Updated baseline_balance after siphon to %.8f", new_baseline)

# ---------------- SCHEDULER ----------------
def wait_until_next_hour():
    now = datetime.utcnow()
    seconds = now.minute * 60 + now.second
    to_wait = 3600 - seconds
    if to_wait <= 0:
        to_wait = 1
    logger.info("Sleeping %d seconds until next hour (UTC). Now=%s", to_wait, now.strftime("%Y-%m-%d %H:%M:%S"))
    time.sleep(to_wait)

# ---------------- ENTRY POINT ----------------
if __name__ == "__main__":
    logger.info("Starting HA live bot (Bybit USDT perp) — testnet=%s, accountType=%s", TESTNET, ACCOUNT_TYPE)
    # initial alignment: wait to the top of the next hour before the first run
    logger.info("Initial alignment: sleeping until next top-of-hour before first run")
    wait_until_next_hour()
    while True:
        try:
            run_once()
        except Exception:
            logger.exception("Error during run_once()")
        wait_until_next_hour()
        
