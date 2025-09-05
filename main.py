#!/usr/bin/env python3
"""
Live Heikin-Ashi Bot for Bybit USDT Perpetual (One-Way Mode)

- Edit INITIAL_HA_OPEN here or set environment variable INITIAL_HA_OPEN.
- Set LIVE=1 and BYBIT_API_KEY/BYBIT_API_SECRET to place orders (testnet/mainnet via BYBIT_TESTNET).
- Persists state to STATE_FILE (last_ha_open, baseline_balance, open_position).
- Updates TP/SL hourly if it reduces risk or increases reward (even without a new signal).
- No siphoning/transfer logic.
"""

import os
import time
import json
import logging
from math import floor
from datetime import datetime, timezone

import requests

# optional: required only for LIVE=1
try:
    from pybit.unified_trading import HTTP as BybitHTTP
except Exception:
    BybitHTTP = None

# ---------------- CONFIG ----------------
SYMBOL = os.environ.get("SYMBOL", "TRXUSDT")
TIMEFRAME = os.environ.get("TIMEFRAME", "60")   # minutes string (e.g. "60")
INITIAL_HA_OPEN = float(os.environ.get("INITIAL_HA_OPEN", "0.33663"))  # change here or via env
TICK_SIZE = float(os.environ.get("TICK_SIZE", "0.00001"))
QTY_STEP = float(os.environ.get("QTY_STEP", "1"))
LEVERAGE = int(os.environ.get("LEVERAGE", "75"))
RISK_PERCENT = float(os.environ.get("RISK_PERCENT", "0.10"))
FALLBACK_PERCENT = float(os.environ.get("FALLBACK_PERCENT", "0.90"))
STATE_FILE = os.environ.get("STATE_FILE", "ha_state.json")
MIN_NEW_ORDER_QTY = float(os.environ.get("MIN_NEW_ORDER_QTY", "16"))

LIVE = os.environ.get("LIVE", "0") in ("1", "true", "yes")
API_KEY = os.environ.get("BYBIT_API_KEY", "")
API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
TESTNET = os.environ.get("BYBIT_TESTNET", "true").lower() in ("1", "true", "yes")

START_BALANCE = float(os.environ.get("START_BALANCE", "10.0"))  # fallback simulation balance

# ---------------- LOG ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger("ha_live_bot")

# ---------------- CLIENT ----------------
session = None
if LIVE:
    if BybitHTTP is None:
        raise RuntimeError("pybit is required for LIVE mode. Install pybit or set LIVE=0.")
    session = BybitHTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)

# ---------------- STATE ----------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_ha_open": None, "baseline_balance": START_BALANCE, "open_position": None}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"last_ha_open": None, "baseline_balance": START_BALANCE, "open_position": None}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ---------------- HELPERS ----------------
def floor_to_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return floor(x / step) * step

def round_price(p: float) -> float:
    if TICK_SIZE <= 0:
        return round(p, 8)
    ticks = round(p / TICK_SIZE)
    return round(ticks * TICK_SIZE, 8)

def timeframe_ms() -> int:
    try:
        return int(TIMEFRAME) * 60 * 1000
    except Exception:
        return 60 * 60 * 1000

# ---------------- FETCH CANDLES ----------------
def fetch_candles_bybit(symbol: str, interval: str = TIMEFRAME, limit: int = 200):
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    rows = data["result"]["list"]
    parsed = []
    for r in rows:
        parsed.append({
            "ts": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4])
        })
    parsed.sort(key=lambda x: x["ts"])
    return parsed

# ---------------- HEIKIN-ASHI ----------------
def compute_heikin_ashi(raw_candles, persisted_open=None):
    """
    raw_candles: list oldest->newest
    persisted_open: used as HA-open for the LAST (most-recent closed) candle
    Returns list of HA candles (oldest->newest)
    """
    ha = []
    prev_ha_open = None
    prev_ha_close = None
    n = len(raw_candles)
    for i, c in enumerate(raw_candles):
        ro, rh, rl, rc = c["open"], c["high"], c["low"], c["close"]
        ha_close = (ro + rh + rl + rc) / 4.0
        if i == n - 1 and persisted_open is not None:
            ha_open = float(persisted_open)
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
def evaluate_signal_from_ha(ha_candle):
    green = ha_candle["ha_close"] > ha_candle["ha_open"]
    red = ha_candle["ha_close"] < ha_candle["ha_open"]
    if green and abs(ha_candle["ha_low"] - ha_candle["ha_open"]) <= TICK_SIZE:
        return "Buy"
    if red and abs(ha_candle["ha_high"] - ha_candle["ha_open"]) <= TICK_SIZE:
        return "Sell"
    return None

# ---------------- BALANCE PARSING (LIVE) ----------------
def get_balance_usdt():
    """Robustly parse unified wallet balance shapes for USDT."""
    if not LIVE:
        raise RuntimeError("get_balance_usdt called in non-LIVE mode")
    try:
        out = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    except Exception as e:
        logger.exception("get_wallet_balance error: %s", e)
        raise
    res = out.get("result", {}) or out
    # Case A: list form
    if isinstance(res, dict) and "list" in res and isinstance(res["list"], list):
        for item in res["list"]:
            coins = item.get("coin")
            if isinstance(coins, list):
                for c in coins:
                    if isinstance(c, dict) and c.get("coin") == "USDT":
                        for key in ("availableToWithdraw", "availableBalance", "walletBalance", "usdValue", "equity"):
                            if key in c and c[key] not in (None, "", " "):
                                try:
                                    return float(c[key])
                                except Exception:
                                    pass
                        for k, v in c.items():
                            try:
                                return float(v)
                            except Exception:
                                pass
            if isinstance(item.get("coin"), str) and item.get("coin") == "USDT":
                for key in ("availableToWithdraw", "availableBalance", "walletBalance", "usdValue", "equity"):
                    if key in item and item[key] not in (None, "", " "):
                        try:
                            return float(item[key])
                        except Exception:
                            pass
                for k, v in item.items():
                    try:
                        return float(v)
                    except Exception:
                        pass
    # Case B: dict keyed by coin
    try:
        if isinstance(res, dict) and "USDT" in res:
            u = res["USDT"]
            for key in ("available_balance", "availableBalance", "available_balance_str", "available_balance_usd"):
                if key in u:
                    try:
                        return float(u.get(key))
                    except Exception:
                        pass
            for key in ("available_balance", "walletBalance", "totalWalletBalance", "availableBalance"):
                if key in u:
                    try:
                        return float(u.get(key))
                    except Exception:
                        pass
            for k, v in u.items():
                try:
                    return float(v)
                except Exception:
                    pass
    except Exception:
        pass
    logger.error("Unable to parse wallet balance response: %s", out)
    raise RuntimeError("Unable to parse wallet balance response")

# ---------------- QTY / RISK ----------------
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

# ---------------- ORDER HELPERS ----------------
def ensure_one_way(symbol):
    if not LIVE:
        return
    try:
        session.switch_position_mode(category="linear", symbol=symbol, mode=0)  # 0 = one-way
        logger.info("Ensured one-way mode for %s", symbol)
    except Exception as e:
        logger.debug("switch_position_mode failed: %s", e)

def set_symbol_leverage(symbol, leverage):
    if not LIVE:
        return
    try:
        session.set_leverage(category="linear", symbol=symbol, buyLeverage=leverage, sellLeverage=leverage)
        logger.info("Set leverage=%sx for %s", leverage, symbol)
    except Exception as e:
        logger.debug("set_leverage failed: %s", e)

def place_live_market_order(side, symbol, qty):
    if not LIVE:
        return {"simulated": True}
    side_str = "Buy" if side == "Buy" else "Sell"
    resp = session.place_order(
        category="linear",
        symbol=symbol,
        side=side_str,
        orderType="Market",
        qty=str(qty),
        timeInForce="ImmediateOrCancel",
        reduceOnly=False
    )
    return resp

def set_trading_stop(symbol, tp, sl):
    if not LIVE:
        return {"simulated": True}
    resp = session.set_trading_stop(
        category="linear",
        symbol=symbol,
        takeProfit=str(round_price(tp)),
        stopLoss=str(round_price(sl)),
        tpTriggerBy="LastPrice",
        slTriggerBy="LastPrice"
    )
    return resp

# ---------------- INTRABAR CHECK ----------------
def check_candle_hit_levels(trade, candle):
    side = trade["side"]
    h = candle["raw_high"]
    l = candle["raw_low"]
    if side == "Buy":
        if h >= trade["tp"]:
            return "tp"
        if l <= trade["sl"]:
            return "sl"
    else:
        if l <= trade["tp"]:
            return "tp"
        if h >= trade["sl"]:
            return "sl"
    return None

# ---------------- MAIN RUN ----------------
def run_once():
    logger.info("=== hourly check ===")
    state = load_state()
    persisted_ha_open = state.get("last_ha_open")
    baseline_balance = state.get("baseline_balance", START_BALANCE)
    open_pos = state.get("open_position")

    if persisted_ha_open is None:
        logger.info("No persisted last_ha_open -> using INITIAL_HA_OPEN = %.8f", INITIAL_HA_OPEN)
        persisted_ha_open = INITIAL_HA_OPEN
    else:
        logger.info("Loaded persisted last_ha_open = %.8f", float(persisted_ha_open))

    # fetch candles
    try:
        raw = fetch_candles_bybit(SYMBOL, TIMEFRAME, limit=200)
    except Exception as e:
        logger.exception("Failed fetching candles: %s", e)
        return

    if not raw or len(raw) < 2:
        logger.warning("Not enough candles; skipping")
        return

    # drop in-progress candle if present
    period = timeframe_ms()
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if raw and (raw[-1]["ts"] + period) > now_ms + 1000:
        logger.info("Detected in-progress candle ts=%d -> dropping it", raw[-1]["ts"])
        raw = raw[:-1]
        if not raw:
            logger.warning("No closed candles left after dropping in-progress -> skipping")
            return

    # compute HA (persisted_open applied to the LAST returned candle)
    ha_list = compute_heikin_ashi(raw, persisted_open=persisted_ha_open)
    ha_list.sort(key=lambda x: x["ts"])

    # log first candle (so you can fetch HA open from TradingView)
    first = ha_list[0]
    logger.info("Fetched %d closed candles. First candle UTC = %s",
                len(ha_list), datetime.fromtimestamp(first["ts"]/1000, tz=timezone.utc).isoformat())
    logger.info("⚠️ Use this first candle to set INITIAL_HA_OPEN in TradingView.")
    logger.info("FIRST Candle RAW O=%.8f H=%.8f L=%.8f C=%.8f | HA H=%.8f L=%.8f C=%.8f",
                first["raw_open"], first["raw_high"], first["raw_low"], first["raw_close"],
                first["ha_high"], first["ha_low"], first["ha_close"])

    # evaluate on the last closed candle
    last = ha_list[-1]
    prev = ha_list[-2] if len(ha_list) >= 2 else None

    logger.info("LAST closed Candle UTC %s | Raw O=%.8f H=%.8f L=%.8f C=%.8f | HA O=%.8f H=%.8f L=%.8f C=%.8f",
                datetime.fromtimestamp(last["ts"]/1000, tz=timezone.utc).isoformat(),
                last["raw_open"], last["raw_high"], last["raw_low"], last["raw_close"],
                last["ha_open"], last["ha_high"], last["ha_low"], last["ha_close"])

    # compute and persist next HA open (for next run)
    next_ha_open = (last["ha_open"] + last["ha_close"]) / 2.0
    state["last_ha_open"] = float(next_ha_open)
    save_state(state)
    logger.info("Persisted next_ha_open = %.8f", next_ha_open)

    # determine signal
    sig = evaluate_signal_from_ha(last)
    # If no signal, we still do update-TP/SL checks below (per your request)
    if not sig:
        logger.info("No new signal on last closed candle.")
    else:
        logger.info("Signal detected: %s", sig)

    entry = float(last["raw_close"])

    # choose SL base from previous candle's HA (if available) else from last
    if prev:
        sl_base = prev["ha_low"] if (sig == "Buy") else prev["ha_high"]
    else:
        sl_base = last["ha_low"] if (sig == "Buy") else last["ha_high"]

    # add one tick offset
    if sig == "Buy":
        sl = round_price(sl_base - TICK_SIZE)
        risk = entry - sl
        tp = round_price(entry + (risk + 0.001 * entry))
    elif sig == "Sell":
        sl = round_price(sl_base + TICK_SIZE)
        risk = sl - entry
        tp = round_price(entry - (risk + 0.001 * entry))
    else:
        # no signal — still compute candidate update levels for existing position using prev HA
        # If no open_pos, nothing to do; candidate levels are computed in the modification section below
        sl = None
        tp = None

    # get current balance (LIVE) or use persisted baseline
    if LIVE:
        try:
            bal = get_balance_usdt()
            baseline_balance = bal
            logger.info("Live USDT balance = %.8f", bal)
        except Exception as e:
            logger.exception("Failed to fetch live balance, using persisted baseline: %s", e)
            bal = float(baseline_balance)
    else:
        bal = float(baseline_balance)
        logger.info("Simulation balance (baseline) = %.8f", bal)

    # compute qty only when opening new trades (or show candidate qty)
    if sl is not None:
        qty = compute_qty(entry, sl, bal)
        logger.info("Computed qty (before min enforcement) = %.8f", qty)
    else:
        qty = None

    # enforce MIN for NEW trades only
    final_qty = qty
    if final_qty is not None:
        if final_qty < MIN_NEW_ORDER_QTY:
            logger.info("Computed qty %.8f < min %.0f -> using min for NEW trade", final_qty, MIN_NEW_ORDER_QTY)
            final_qty = MIN_NEW_ORDER_QTY
        final_qty = floor_to_step(final_qty, QTY_STEP)
        if final_qty <= 0:
            logger.warning("Final qty <= 0 after step -> abort trade open")
            final_qty = None

    # HANDLE EXISTING OPEN POSITION
    if open_pos:
        logger.info("Existing open position: side=%s entry=%.8f sl=%.8f tp=%.8f qty=%.8f",
                    open_pos["side"], open_pos["entry"], open_pos["sl"], open_pos["tp"], open_pos["qty"])

        # Check if last candle hit TP/SL
        outcome = check_candle_hit_levels(open_pos, last)
        if outcome == "tp":
            logger.info("Existing position TP hit -> closing and crediting baseline.")
            baseline_balance += abs(open_pos["entry"] - open_pos["sl"])
            state["open_position"] = None
            state["baseline_balance"] = baseline_balance
            save_state(state)
            return
        if outcome == "sl":
            logger.info("Existing position SL hit -> closing and debiting baseline.")
            baseline_balance -= abs(open_pos["entry"] - open_pos["sl"])
            state["open_position"] = None
            state["baseline_balance"] = baseline_balance
            save_state(state)
            return

        # Candidate update: compute new SL/TP using previous candle HA levels (with one tick offset)
        # Use prev HA for updates; fallback to last HA if prev missing
        if prev:
            if open_pos["side"] == "Buy":
                candidate_sl = round_price(prev["ha_low"] - TICK_SIZE)
                candidate_rr = open_pos["entry"] - candidate_sl
                candidate_tp = round_price(open_pos["entry"] + (candidate_rr + 0.001 * open_pos["entry"]))
            else:
                candidate_sl = round_price(prev["ha_high"] + TICK_SIZE)
                candidate_rr = candidate_sl - open_pos["entry"]
                candidate_tp = round_price(open_pos["entry"] - (candidate_rr + 0.001 * open_pos["entry"]))
        else:
            # fallback: use last candle HA
            if open_pos["side"] == "Buy":
                candidate_sl = round_price(last["ha_low"] - TICK_SIZE)
                candidate_rr = open_pos["entry"] - candidate_sl
                candidate_tp = round_price(open_pos["entry"] + (candidate_rr + 0.001 * open_pos["entry"]))
            else:
                candidate_sl = round_price(last["ha_high"] + TICK_SIZE)
                candidate_rr = candidate_sl - open_pos["entry"]
                candidate_tp = round_price(open_pos["entry"] - (candidate_rr + 0.001 * open_pos["entry"]))

        old_sl = open_pos["sl"]
        old_tp = open_pos["tp"]
        old_risk = abs(open_pos["entry"] - old_sl)
        new_risk = abs(open_pos["entry"] - candidate_sl)
        old_reward = abs(old_tp - open_pos["entry"])
        new_reward = abs(candidate_tp - open_pos["entry"])

        logger.info("Candidate update for open %s trade | Old SL=%.8f TP=%.8f -> Candidate SL=%.8f TP=%.8f",
                    open_pos["side"], old_sl, old_tp, candidate_sl, candidate_tp)

        # Update if reduces risk or increases reward
        if (new_risk < old_risk) or (new_reward > old_reward):
            logger.info("🔄 Updating existing trade (beneficial). Old SL=%.8f TP=%.8f -> New SL=%.8f TP=%.8f",
                        old_sl, old_tp, candidate_sl, candidate_tp)
            if LIVE:
                try:
                    set_trading_stop(SYMBOL, candidate_tp, candidate_sl)
                    logger.info("API set_trading_stop called to update TP/SL")
                except Exception as e:
                    logger.exception("API set_trading_stop failed: %s", e)
            open_pos["sl"] = round_price(candidate_sl)
            open_pos["tp"] = round_price(candidate_tp)
            state["open_position"] = open_pos
            save_state(state)
        else:
            logger.info("No beneficial update to SL/TP; skipping.")
        return

    # NO open position -> open new trade if signal detected and final_qty computed
    if sig and final_qty:
        logger.info("Opening NEW %s trade -> Entry=%.8f SL=%.8f TP=%.8f qty=%.8f", sig, entry, sl, tp, final_qty)
        if LIVE:
            ensure_one_way(SYMBOL)
            set_symbol_leverage(SYMBOL, LEVERAGE)
            try:
                resp = place_live_market_order(sig, SYMBOL, final_qty)
                logger.info("Placed live market order: %s", resp)
            except Exception as e:
                logger.exception("Live market order failed: %s", e)
                return

            # attach TP/SL using API
            try:
                resp2 = set_trading_stop(SYMBOL, tp, sl)
                logger.info("Attached TP/SL via API: %s", resp2)
            except Exception as e:
                logger.exception("set_trading_stop failed: %s", e)
        else:
            logger.info("SIMULATED order placed (LIVE=0)")

        # Persist open position (note: for real live, you'd want to fetch actual filled qty/entry from API)
        open_rec = {
            "side": sig,
            "entry": entry,
            "sl": round_price(sl),
            "tp": round_price(tp),
            "qty": final_qty,
            "open_time": last["ts"]
        }
        state["open_position"] = open_rec
        state["baseline_balance"] = baseline_balance
        save_state(state)
        logger.info("Recorded open position in state (simulated persistence).")

# ---------------- SCHEDULER ----------------
def wait_until_next_hour():
    now = datetime.now(timezone.utc)
    seconds = now.minute * 60 + now.second
    to_wait = 3600 - seconds
    if to_wait <= 0:
        to_wait = 1
    logger.info("Sleeping %d seconds until next top-of-hour (UTC). Now=%s",
                to_wait, now.strftime("%Y-%m-%d %H:%M:%S"))
    time.sleep(to_wait)

# ---------------- ENTRY POINT ----------------
if __name__ == "__main__":
    logger.info("Starting live HA bot. LIVE=%s TESTNET=%s SYMBOL=%s",
                LIVE, TESTNET, SYMBOL)
    logger.info("INITIAL_HA_OPEN default = %.8f "
                "(will be overwritten by persisted last_ha_open if present)",
                INITIAL_HA_OPEN)
    logger.info("Minimum new-order qty = %.0f", MIN_NEW_ORDER_QTY)
    # Initial alignment: wait until top-of-hour so initial persisted / INITIAL_HA_OPEN
    # used for first closed candle
    logger.info("Initial alignment: sleeping until next top-of-hour before first run")
    wait_until_next_hour()
    while True:
        try:
            run_once()
        except Exception:
            logger.exception("Error during run_once()")
        wait_until_next_hour()
