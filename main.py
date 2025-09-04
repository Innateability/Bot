#!/usr/bin/env python3
"""
Live Heikin-Ashi Bot for Bybit USDT Perpetual (One-Way Mode)

Fixes
- Robust wallet balance parsing
- Align to top-of-hour on startup (so INITIAL_HA_OPEN/persisted HA open is used for the first closed candle)
- Detect & drop in-progress candle so OHLC/HA are computed only on closed candles
- Detailed logging (raw + HA OHLC, balance, absolute qty, order events)

Adds
- Enforce minimum quantity of 16 contracts for NEW trades only (env override: MIN_NEW_ORDER_QTY)

Custom rules (per user request)
- SL uses the HA-open of the previous closed candle
- TP is 1:1 RR + 0.1% (of entry) regardless of situation
- Modify SL only if it reduces potential loss
- Modify TP only if it increases potential profit
"""

import os
import time
import json
import logging
from math import floor
from datetime import datetime
from typing import List, Dict, Optional, Any

from pybit.unified_trading import HTTP

# ---------------- CONFIG --------------
SYMBOL = os.environ.get("SYMBOL", "TRXUSDT")
TIMEFRAME = os.environ.get("TIMEFRAME", "60")  # minutes (string or int)
INITIAL_HA_OPEN = float(os.environ.get("INITIAL_HA_OPEN", "0.33889"))
TICK_SIZE = float(os.environ.get("TICK_SIZE", "0.00001"))
QTY_STEP = float(os.environ.get("QTY_STEP", "1"))
LEVERAGE = int(os.environ.get("LEVERAGE", "75"))
RISK_PERCENT = float(os.environ.get("RISK_PERCENT", "0.10"))          # risk per trade fraction of balance (e.g., 0.10 = 10%)
FALLBACK_PERCENT = float(os.environ.get("FALLBACK_PERCENT", "0.90"))  # safety factor when sizing by margin cap
START_SIP_BALANCE = float(os.environ.get("START_SIP_BALANCE", "4.0"))
SIP_PERCENT = float(os.environ.get("SIP_PERCENT", "0.25"))
STATE_FILE = os.environ.get("STATE_FILE", "ha_state.json")
MIN_NEW_ORDER_QTY = float(os.environ.get("MIN_NEW_ORDER_QTY", "16"))  # minimum contracts for NEW trades only

API_KEY = os.environ.get("BYBIT_API_KEY", "")
API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
TESTNET = os.environ.get("BYBIT_TESTNET", "false").lower() in ("1", "true", "yes")
ACCOUNT_TYPE = os.environ.get("BYBIT_ACCOUNT_TYPE", "UNIFIED")  # 'UNIFIED' or 'CONTRACT'

# ---------------- LOG --------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ha_bot")

# ---------------- CLIENT --------------
session = HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)

# ---------------- STATE --------------
def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ---------------- HELPERS --------------
def round_price(p: float) -> float:
    if TICK_SIZE <= 0:
        return p
    ticks = round(p / TICK_SIZE)
    return round(ticks * TICK_SIZE, 10)

def floor_to_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return floor(x / step) * step

def timeframe_ms() -> int:
    """Return timeframe in milliseconds (TIMEFRAME is minutes)."""
    try:
        return int(TIMEFRAME) * 60 * 1000
    except Exception:
        return 60 * 60 * 1000  # default 1h

# ---------------- KLINES --------------
def fetch_candles(symbol: str, interval: str = TIMEFRAME, limit: int = 200) -> List[Dict[str, float]]:
    """
    Return list of candles (oldest -> newest) parsed to dicts: ts, open, high, low, close
    """
    out = session.get_kline(category="linear", symbol=symbol, interval=str(interval), limit=limit)
    res = out.get("result", {}) or out

    # v3 shape: {'result': {'list': [[ts, open, high, low, close, ...], ...]}}
    if isinstance(res, dict) and "list" in res and isinstance(res["list"], list):
        rows = res["list"]
        parsed: List[Dict[str, float]] = []
        for r in rows:
            try:
                parsed.append({
                    "ts": int(r[0]),               # startTime in ms
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
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
                    "close": float(r.get("close", r.get("closePrice", 0))),
                })
            except Exception:
                continue
        parsed.sort(key=lambda x: x["ts"])
        return parsed

    raise RuntimeError(f"Unexpected kline response shape: {res}")

# ---------------- HEIKIN-ASHI --------------
def compute_heikin_ashi(raw_candles: List[Dict[str, float]],
                        persisted_open: Optional[float] = None) -> List[Dict[str, float]]:
    """
    raw_candles: list oldest->newest
    persisted_open: used as HA-open for the LAST (most-recent closed) candle
    """
    ha: List[Dict[str, float]] = []
    prev_ha_open: Optional[float] = None
    prev_ha_close: Optional[float] = None
    n = len(raw_candles)

    for i, c in enumerate(raw_candles):
        ro, rh, rl, rc = c["open"], c["high"], c["low"], c["close"]
        ha_close = (ro + rh + rl + rc) / 4.0

        if i == n - 1:
            # For the most recent closed candle, use persisted open if provided
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

# ---------------- SIGNAL --------------
def evaluate_signal(ha_list: List[Dict[str, float]]) -> Optional[Dict[str, str]]:
    """Return {'signal': 'Buy'|'Sell'} or None (based on last closed HA candle)."""
    if len(ha_list) < 1:
        return None
    last = ha_list[-1]
    green = last["ha_close"] > last["ha_open"]
    red = last["ha_close"] < last["ha_open"]
    # "No lower wick" for Buy, "No upper wick" for Sell — allow 1 tick tolerance
    if green and abs(last["ha_low"] - last["ha_open"]) <= TICK_SIZE:
        return {"signal": "Buy"}
    if red and abs(last["ha_high"] - last["ha_open"]) <= TICK_SIZE:
        return {"signal": "Sell"}
    return None

# ---------------- BALANCE & POSITIONS --------------
def get_balance_usdt() -> float:
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
            coins = item.get("coin")
            # If item['coin'] is a list of coin dicts
            if isinstance(coins, list):
                for c in coins:
                    if isinstance(c, dict) and c.get("coin") == "USDT":
                        for key in ("availableToWithdraw", "availableBalance", "walletBalance", "usdValue", "equity"):
                            if key in c and c[key] not in (None, "", " "):
                                try:
                                    return float(c[key])
                                except Exception:
                                    continue
                        for k, v in c.items():
                            try:
                                return float(v)
                            except Exception:
                                continue
            # If item['coin'] is a string
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

    # Case B: result -> {'USDT': { ... }}
    try:
        if isinstance(res, dict) and "USDT" in res:
            u = res["USDT"]
            for key in ("available_balance", "availableBalance", "available_balance_str", "available_balance_usd"):
                if key in u:
                    try:
                        return float(u.get(key))
                    except Exception:
                        pass
            for key in ("walletBalance", "totalWalletBalance"):
                if key in u:
                    try:
                        return float(u.get(key))
                    except Exception:
                        pass
            for k, v in u.items():
                try:
                    return float(v)
                except Exception:
                    continue
    except Exception:
        pass

    logger.error("Unable to parse wallet balance response: %s", out)
    raise RuntimeError("Unable to parse wallet balance response")

def get_open_position(symbol: str) -> Optional[Dict[str, Any]]:
    try:
        out = session.get_positions(category="linear", symbol=symbol)
    except Exception as e:
        logger.exception("get_positions error: %s", e)
        return None
    res = out.get("result", {}) or out
    if isinstance(res, dict) and "list" in res and len(res["list"]) > 0:
        # Return first position record (Bybit returns a single row per symbol in one-way)
        return res["list"][0]
    return None

# ---------------- ORDER HELPERS --------------
def ensure_one_way(symbol: str) -> None:
    try:
        session.switch_position_mode(category="linear", symbol=symbol, mode=0)  # 0 = one-way
        logger.info("Ensured one-way mode for %s", symbol)
    except Exception as e:
        logger.debug("switch_position_mode ignored/failed: %s", e)

def set_symbol_leverage(symbol: str, leverage: int) -> None:
    try:
        session.set_leverage(category="linear", symbol=symbol, buyLeverage=leverage, sellLeverage=leverage)
        logger.info("Set leverage=%sx for %s", leverage, symbol)
    except Exception as e:
        logger.warning("set_leverage failed: %s", e)

def place_market_with_tp_sl(signal_side: str, symbol: str, qty: float, entry: float, prev_ha_open: float) -> bool:
    """
    Place a new market order with TP/SL rules:
      - SL = previous candle's HA open
      - TP = 1:1 RR + 0.1% of entry
    """
    side = "Buy" if signal_side == "Buy" else "Sell"
    sl = round_price(prev_ha_open)
    risk = abs(entry - sl)
    if risk <= 0:
        logger.warning("Invalid SL (risk <= 0). Skipping order.")
        return False

    # TP = 1:1 + 0.1% of entry
    if side == "Buy":
        tp = entry + risk + (0.001 * entry)
    else:
        tp = entry - (risk + (0.001 * entry))
    tp = round_price(tp)

    try:
        resp = session.place_order(
            category="linear",
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=str(qty),
            timeInForce="ImmediateOrCancel",
            reduceOnly=False
        )
        logger.info("Placed market order: %s qty=%s resp=%s", side, qty, resp)
    except Exception as e:
        logger.exception("place_order failed: %s", e)
        return False

    try:
        resp2 = session.set_trading_stop(
            category="linear",
            symbol=symbol,
            takeProfit=str(tp),
            stopLoss=str(sl),
            tpTriggerBy="LastPrice",
            slTriggerBy="LastPrice"
        )
        logger.info("Attached TP=%.8f SL=%.8f resp=%s", tp, sl, resp2)
    except Exception as e:
        logger.exception("set_trading_stop failed: %s", e)

    return True

def modify_tp_sl_if_better(symbol: str, entry: float, prev_ha_open: float) -> bool:
    """
    Modify TP/SL only if:
      - SL reduces losses (moves closer to breakeven in a losing trade).
      - TP increases profit potential.
    No quantity change here.
    """
    pos = get_open_position(symbol)
    if not pos:
        logger.info("No open position to modify for %s", symbol)
        return False

    # Current position fields
    try:
        size = float(pos.get("size") or pos.get("qty") or 0.0)
    except Exception:
        size = 0.0
    try:
        entry_price = float(pos.get("entryPrice") or pos.get("avgEntryPrice") or entry or 0.0)
    except Exception:
        entry_price = entry or 0.0
    side = pos.get("side") or ("Buy" if size >= 0 else "Sell")

    # Existing TP/SL if provided (may be 0/None if none set)
    try:
        current_sl = float(pos.get("stopLoss") or 0.0)
    except Exception:
        current_sl = 0.0
    try:
        current_tp = float(pos.get("takeProfit") or 0.0)
    except Exception:
        current_tp = 0.0

    # Recalculate per new rules
    new_sl = round_price(prev_ha_open)
    risk = abs(entry_price - new_sl)
    if risk <= 0:
        logger.info("Invalid recalculated SL (risk <= 0). Skipping update.")
        return False

    if side == "Buy":
        new_tp = round_price(entry_price + risk + (0.001 * entry_price))
    else:
        new_tp = round_price(entry_price - (risk + (0.001 * entry_price)))

    update_sl = False
    update_tp = False

    # SL improvement check (reduce potential loss only)
    if current_sl > 0:
        # For Buy: improving SL means raising SL (closer to entry)
        # For Sell: improving SL means lowering SL (closer to entry)
        if (side == "Buy" and new_sl > current_sl) or (side == "Sell" and new_sl < current_sl):
            update_sl = True
    else:
        # If no SL set yet, set it
        update_sl = True

    # TP improvement check (increase profit only)
    if current_tp > 0:
        if (side == "Buy" and new_tp > current_tp) or (side == "Sell" and new_tp < current_tp):
            update_tp = True
    else:
        # If no TP set yet, set it
        update_tp = True

    if not (update_sl or update_tp):
        logger.info("No TP/SL improvements found. Skipping modification.")
        return True

    final_sl = str(new_sl if update_sl else current_sl or "")
    final_tp = str(new_tp if update_tp else current_tp or "")

    try:
        session.set_trading_stop(
            category="linear",
            symbol=symbol,
            takeProfit=final_tp if final_tp else None,
            stopLoss=final_sl if final_sl else None,
            tpTriggerBy="LastPrice",
            slTriggerBy="LastPrice"
        )
        logger.info("Updated TP/SL -> TP=%s SL=%s", final_tp if final_tp else "(unchanged)", final_sl if final_sl else "(unchanged)")
    except Exception as e:
        logger.exception("Failed to update TP/SL: %s", e)
        return False

    return True

# ---------------- RISK / QTY --------------
def compute_qty(entry: float, sl: float, balance: float) -> float:
    risk_usd = balance * RISK_PERCENT
    per_contract_risk = abs(entry - sl)
    if per_contract_risk <= 0:
        return 0.0
    qty = risk_usd / per_contract_risk
    # Margin check (rough cap)
    est_margin = (qty * entry) / LEVERAGE
    if est_margin > balance:
        qty = (balance * FALLBACK_PERCENT * LEVERAGE) / entry
    return floor_to_step(qty, QTY_STEP)

# ---------------- SIPHON --------------
def siphon_if_needed(baseline_balance: Optional[float]) -> Optional[float]:
    bal = get_balance_usdt()
    if baseline_balance is None:
        return baseline_balance
    if baseline_balance >= START_SIP_BALANCE and bal >= 2 * baseline_balance:
        amount = round(bal * SIP_PERCENT, 6)
        logger.info("Siphoning approx %s USDT to funding (implement transfer API as needed)", amount)
        # Implement transfer if desired
        return bal  # set new baseline to current balance after siphon logic if you implement it
    return baseline_balance

# ---------------- MAIN FLOW --------------
def run_once() -> None:
    logger.info("=== Running hourly check ===")
    state = load_state()
    persisted_ha_open = state.get("last_ha_open")
    baseline_balance = state.get("baseline_balance")

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

    # Drop in-progress newest candle if any
    period = timeframe_ms()
    now_ms = int(datetime.utcnow().timestamp() * 1000)
    if raw and (raw[-1]["ts"] + period) > (now_ms + 1000):
        logger.info(
            "Detected in-progress candle at %s (ts=%d). Dropping it and using prior closed candles.",
            datetime.utcfromtimestamp(raw[-1]["ts"]/1000).isoformat(), raw[-1]["ts"]
        )
        raw = raw[:-1]
        if not raw:
            logger.warning("After dropping in-progress candle no closed candles remain; skipping run.")
            return

    # compute HA using persisted_open for the last closed candle
    ha_list = compute_heikin_ashi(raw, persisted_open=persisted_ha_open)
    last_closed = ha_list[-1]      # last closed HA candle (signal candle)

    # For SL we use the previous candle's HA open; fallback to last if unavailable
    if len(ha_list) >= 2:
        prev_ha_open = ha_list[-2]["ha_open"]
    else:
        prev_ha_open = last_closed["ha_open"]

    # log raw + HA values for the closed candle
    logger.info(
        "RAW last closed: o/h/l/c = %.8f / %.8f / %.8f / %.8f",
        last_closed["raw_open"], last_closed["raw_high"], last_closed["raw_low"], last_closed["raw_close"]
    )
    logger.info(
        "HA  last closed: o/h/l/c = %.8f / %.8f / %.8f / %.8f",
        last_closed["ha_open"], last_closed["ha_high"], last_closed["ha_low"], last_closed["ha_close"]
    )

    # compute next HA-open (for upcoming new candle) and persist
    next_ha_open = (last_closed["ha_open"] + last_closed["ha_close"]) / 2.0
    state["last_ha_open"] = float(next_ha_open)
    save_state(state)
    logger.info("Persisted next_ha_open = %.8f", next_ha_open)

    # evaluate signal (based on last closed HA candle)
    sig = evaluate_signal(ha_list)
    if not sig:
        logger.info("No signal detected this hour")
        # Even without a new signal, if there is a position open we may still attempt to improve TP/SL
        try:
            modify_tp_sl_if_better(SYMBOL, last_closed["raw_close"], prev_ha_open)
        except Exception:
            logger.exception("modify_tp_sl_if_better failed without new signal")
        return

    logger.info("Signal detected: %s", sig["signal"])

    # entry ≈ last raw close (top-of-hour), SL = prev_ha_open (by rule)
    entry = float(last_closed["raw_close"])
    sl_for_qty = float(prev_ha_open)  # use previous HA open for SL distance when sizing
    risk = ((abs(entry - sl_for_qty)) + 0.0001)
    if risk <= 0:
        logger.info("Zero/negative risk (entry == SL); skipping")
        return

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

    qty = compute_qty(entry, sl_for_qty, bal)
    logger.info("Calculated absolute quantity (base units) = %.8f", qty)
    if qty <= 0:
        logger.warning("Computed qty <= 0; aborting trade this hour")
        return

    # ensure one-way and leverage
    ensure_one_way(SYMBOL)
    set_symbol_leverage(SYMBOL, LEVERAGE)

    # trade flow
    pos = get_open_position(SYMBOL)
    if pos and float(pos.get("size", 0) or 0) != 0:
        logger.info("Existing open position found; attempting to improve TP/SL only (no qty increase).")
        modified = modify_tp_sl_if_better(SYMBOL, entry, prev_ha_open)
        logger.info("TP/SL modify result: %s", modified)
    else:
        # Enforce minimum 16 contracts for NEW trades only
        final_qty = qty
        if final_qty < MIN_NEW_ORDER_QTY:
            logger.info("Computed qty %.8f < minimum %.0f; using minimum for new order.", final_qty, MIN_NEW_ORDER_QTY)
            final_qty = MIN_NEW_ORDER_QTY
        # respect exchange lot step
        final_qty = floor_to_step(final_qty, QTY_STEP)
        if final_qty <= 0:
            logger.warning("Final qty after min/step adjustment <= 0; aborting new order")
            return

        logger.info("No open position — placing new market order with attached TP/SL (final_qty=%.8f)", final_qty)
        placed = place_market_with_tp_sl(sig["signal"], SYMBOL, final_qty, entry, prev_ha_open)
        logger.info("Place order result: %s", placed)

    # siphon logic
    new_baseline = siphon_if_needed(baseline_balance)
    if new_baseline is not None and new_baseline != baseline_balance:
        state["baseline_balance"] = new_baseline
        save_state(state)
        logger.info("Updated baseline_balance after siphon to %.8f", new_baseline)

# ---------------- SCHEDULER --------------
def wait_until_next_hour() -> None:
    now = datetime.utcnow()
    seconds = now.minute * 60 + now.second
    to_wait = 3600 - seconds
    if to_wait <= 0:
        to_wait = 1
    logger.info("Sleeping %d seconds until next hour (UTC). Now=%s", to_wait, now.strftime("%Y-%m-%d %H:%M:%S"))
    time.sleep(to_wait)

# ---------------- ENTRY POINT --------------
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
