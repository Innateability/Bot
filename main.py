#!/usr/bin/env python3
"""
Live Heikin-Ashi Bot (paper/testnet by default) — One-Way Mode

Features:
- Hourly fetch of last closed 1h candle
- Persisted HA open across runs (state file)
- Logs raw & HA OHLC for candles (including the first candle time to set INITIAL_HA_OPEN)
- Signal: Buy if HA green and ha_open ≈ ha_low; Sell if HA red and ha_open ≈ ha_high
- SL uses the HA low/high of the *previous* candle (not the candle being checked) +/- 1 tick
- TP is 1:1 RR + 0.1% of entry
- Only update TP/SL if it reduces loss or increases profit
- Simulated balance (START_BALANCE) by default; optional LIVE mode to place orders (env LIVE=1)
- Enforce minimum 16 contracts for NEW trades only (env override)
"""

import os
import time
import json
import logging
from math import floor
from datetime import datetime, timezone

import requests

# If you want real testnet order placement, install pybit and set LIVE=1 with keys.
try:
    from pybit.unified_trading import HTTP as BybitHTTP
except Exception:
    BybitHTTP = None

# ---------------- CONFIG ----------------
SYMBOL = os.environ.get("SYMBOL", "TRXUSDT")
TIMEFRAME = os.environ.get("TIMEFRAME", "60")   # minutes (string because API expects "60")
INITIAL_HA_OPEN = float(os.environ.get("INITIAL_HA_OPEN", "0.34957"))  # default per your request
TICK_SIZE = float(os.environ.get("TICK_SIZE", "0.00001"))
QTY_STEP = float(os.environ.get("QTY_STEP", "1"))
LEVERAGE = int(os.environ.get("LEVERAGE", "75"))
RISK_PERCENT = float(os.environ.get("RISK_PERCENT", "0.10"))
FALLBACK_PERCENT = float(os.environ.get("FALLBACK_PERCENT", "0.90"))
STATE_FILE = os.environ.get("STATE_FILE", "ha_state.json")
MIN_NEW_ORDER_QTY = float(os.environ.get("MIN_NEW_ORDER_QTY", "16"))

# Simulation / live toggle
LIVE = os.environ.get("LIVE", "0") in ("1", "true", "yes")
START_BALANCE = float(os.environ.get("START_BALANCE", "10.0"))  # simulated dollars

# Bybit API keys (only required when LIVE=1)
API_KEY = os.environ.get("BYBIT_API_KEY", "")
API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
TESTNET = os.environ.get("BYBIT_TESTNET", "true").lower() in ("1", "true", "yes")

# ---------------- LOG ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger("ha_live_bot")

# ---------------- CLIENT ----------------
if LIVE:
    if BybitHTTP is None:
        raise RuntimeError("pybit is required for LIVE mode. Install pybit and set BYBIT_API_KEY/SECRET.")
    session = BybitHTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)
else:
    session = None

# ---------------- STATE ----------------
def load_state():
    if not os.path.exists(STATE_FILE):
        # initial state
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
    """Fetch klines from Bybit public v5 kline endpoint (oldest->newest)."""
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
    persisted_open: used as HA-open for THE LAST candle (most recent closed).
    Returns list of HA candles (oldest->newest).
    """
    ha = []
    prev_ha_open = None
    prev_ha_close = None
    n = len(raw_candles)
    for i, c in enumerate(raw_candles):
        ro, rh, rl, rc = c["open"], c["high"], c["low"], c["close"]
        ha_close = (ro + rh + rl + rc) / 4.0
        # Use persisted_open only for the *last* candle (most recent closed) per your persisted logic
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
    """Return 'Buy'|'Sell' or None based on last closed HA candle."""
    green = ha_candle["ha_close"] > ha_candle["ha_open"]
    red = ha_candle["ha_close"] < ha_candle["ha_open"]
    # Compare with tolerance of one tick
    if green and abs(ha_candle["ha_low"] - ha_candle["ha_open"]) <= TICK_SIZE:
        return "Buy"
    if red and abs(ha_candle["ha_high"] - ha_candle["ha_open"]) <= TICK_SIZE:
        return "Sell"
    return None

# ---------------- QTY / RISK ----------------
def compute_qty(entry, sl, balance):
    """Compute contract qty based on RISK_PERCENT and fallback rule."""
    risk_usd = balance * RISK_PERCENT
    per_contract_risk = abs(entry - sl)
    if per_contract_risk <= 0:
        return 0.0
    qty = risk_usd / per_contract_risk
    # check margin estimate; if > balance, fallback to (balance * FALLBACK_PERCENT * LEVERAGE) / entry
    est_margin = (qty * entry) / LEVERAGE
    if est_margin > balance:
        qty = (balance * FALLBACK_PERCENT * LEVERAGE) / entry
    return floor_to_step(qty, QTY_STEP)

# ---------------- TRADE SIMULATION / EXECUTION ----------------
def check_candle_hit_levels(trade, candle):
    """
    Given an open trade and a closed candle (with high/low), determine if TP or SL was hit during that candle.
    Returns 'tp', 'sl', or None.
    For buys: TP hit if candle.high >= tp; SL hit if candle.low <= sl.
    For sells: TP hit if candle.low <= tp; SL hit if candle.high >= sl.
    Note: This is a simplified intrabar check using only high/low of candle.
    """
    side = trade["side"]
    h = candle["raw_high"]
    l = candle["raw_low"]
    if side == "Buy":
        if h >= trade["tp"]:
            return "tp"
        if l <= trade["sl"]:
            return "sl"
    else:  # Sell
        if l <= trade["tp"]:
            return "tp"
        if h >= trade["sl"]:
            return "sl"
    return None

def maybe_place_live_market(side, qty):
    """Place a market order in one-way mode if LIVE=1. Returns True on success in this wrapper (or False)."""
    if not LIVE:
        return True  # simulated success
    # Ensure one-way mode and leverage
    try:
        session.switch_position_mode(category="linear", symbol=SYMBOL, mode=0)  # 0 = one-way
    except Exception:
        pass
    try:
        session.set_leverage(category="linear", symbol=SYMBOL, buyLeverage=LEVERAGE, sellLeverage=LEVERAGE)
    except Exception:
        pass
    side_str = "Buy" if side == "Buy" else "Sell"
    try:
        resp = session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=side_str,
            orderType="Market",
            qty=str(qty),
            timeInForce="ImmediateOrCancel",
            reduceOnly=False
        )
        logger.info("LIVE order resp: %s", resp)
        return True
    except Exception as e:
        logger.exception("Live order placement failed: %s", e)
        return False

# ---------------- MAIN RUN ONCE ----------------
def run_once():
    logger.info("=== hourly check ===")
    state = load_state()
    persisted_ha_open = state.get("last_ha_open")
    baseline_balance = state.get("baseline_balance", START_BALANCE)
    open_pos = state.get("open_position")  # dict or None

    if persisted_ha_open is not None:
        logger.info("Loaded persisted last_ha_open = %.8f", float(persisted_ha_open))
    else:
        logger.info("No persisted last_ha_open found — using INITIAL_HA_OPEN = %.8f", float(INITIAL_HA_OPEN))
        persisted_ha_open = INITIAL_HA_OPEN

    # fetch candles
    try:
        raw = fetch_candles_bybit(SYMBOL, TIMEFRAME, limit=200)
    except Exception as e:
        logger.exception("Failed fetching candles: %s", e)
        return

    if not raw or len(raw) < 2:
        logger.warning("Not enough candles; skipping this run.")
        return

    # drop in-progress candle if present
    period = timeframe_ms()
    now_ms = int(datetime.utcnow().timestamp() * 1000)
    if raw and (raw[-1]["ts"] + period) > now_ms + 1000:
        logger.info("Detected in-progress candle at ts=%d; dropping it.", raw[-1]["ts"])
        raw = raw[:-1]
        if not raw:
            logger.warning("No closed candles remain after drop; skipping.")
            return

    # compute HA (use persisted_ha_open for last closed candle)
    ha_list = compute_heikin_ashi(raw, persisted_open=persisted_ha_open)
    # ensure ascending order
    ha_list.sort(key=lambda x: x["ts"])

    # Log first candle UTC (so you can cross-check HA-open in TradingView)
    first = ha_list[0]
    logger.info("Fetched %d closed candles. First candle UTC = %s", len(ha_list),
                datetime.fromtimestamp(first["ts"] / 1000, tz=timezone.utc).isoformat())
    logger.info("⚠️ Use this first candle to cross-check/set INITIAL_HA_OPEN in TradingView.")
    logger.info("First Candle RAW O=%.8f H=%.8f L=%.8f C=%.8f | HA H=%.8f L=%.8f C=%.8f",
                first["raw_open"], first["raw_high"], first["raw_low"], first["raw_close"],
                first["ha_high"], first["ha_low"], first["ha_close"])

    # We'll evaluate signal on the last closed candle (most recent)
    last = ha_list[-1]
    # Get previous HA candle (for SL purposes)
    prev = ha_list[-2] if len(ha_list) >= 2 else None

    # log raw + ha for last closed
    logger.info("Candle UTC %s | Raw O=%.8f H=%.8f L=%.8f C=%.8f | HA O=%.8f H=%.8f L=%.8f C=%.8f",
                datetime.fromtimestamp(last["ts"] / 1000, tz=timezone.utc).isoformat(),
                last["raw_open"], last["raw_high"], last["raw_low"], last["raw_close"],
                last["ha_open"], last["ha_high"], last["ha_low"], last["ha_close"])

    # compute next HA open based on this last closed candle and persist
    next_ha_open = (last["ha_open"] + last["ha_close"]) / 2.0
    state["last_ha_open"] = float(next_ha_open)
    save_state(state)
    logger.info("Persisted next_ha_open = %.8f", next_ha_open)

    # evaluate signal
    sig = evaluate_signal_from_ha(last)
    if not sig:
        logger.info("No signal detected this hour.")
        # if position exists, still check whether it was stopped/taken during this candle (simulate)
        if open_pos:
            outcome = check_candle_hit_levels(open_pos, last)
            if outcome == "tp":
                logger.info("Simulated TP hit for existing %s trade at candle %s", open_pos["side"],
                            datetime.fromtimestamp(last["ts"]/1000, tz=timezone.utc).isoformat())
                # adjust balance
                baseline_balance += abs(open_pos["entry"] - open_pos["sl"])  # risk gained (1:1)
                state["open_position"] = None
                state["baseline_balance"] = baseline_balance
                save_state(state)
            elif outcome == "sl":
                logger.info("Simulated SL hit for existing %s trade at candle %s", open_pos["side"],
                            datetime.fromtimestamp(last["ts"]/1000, tz=timezone.utc).isoformat())
                baseline_balance -= abs(open_pos["entry"] - open_pos["sl"])
                state["open_position"] = None
                state["baseline_balance"] = baseline_balance
                save_state(state)
        return

    logger.info("Signal detected: %s", sig)

    # Entry is raw close of last closed candle
    entry = float(last["raw_close"])

    # Use HA of previous candle (prev) for SL per your instruction; if no prev, fallback to last candle's HA low/high
    if prev:
        if sig == "Buy":
            sl_base = prev["ha_low"]
        else:
            sl_base = prev["ha_high"]
    else:
        if sig == "Buy":
            sl_base = last["ha_low"]
        else:
            sl_base = last["ha_high"]

    # Add/remove one pip to/from SL per side (you said "add one pip to the ha low" — we interpret as tick offset)
    if sig == "Buy":
        sl = round_price(sl_base - TICK_SIZE)
        risk = entry - sl
        tp = round_price(entry + (risk + 0.001 * entry))
    else:
        sl = round_price(sl_base + TICK_SIZE)
        risk = sl - entry
        tp = round_price(entry - (risk + 0.001 * entry))

    sl = round_price(sl)
    tp = round_price(tp)

    logger.info("Calculated trade levels -> Entry=%.8f | SL=%.8f | TP=%.8f | risk=%.8f", entry, sl, tp, abs(risk))

    # compute qty based on current baseline_balance
    try:
        bal = float(baseline_balance)
    except Exception:
        bal = START_BALANCE

    qty = compute_qty(entry, sl, bal)
    logger.info("Calculated qty (before min/new-order enforcement) = %.8f", qty)

    # enforce minimum qtty for new trades only
    final_qty = qty
    if final_qty < MIN_NEW_ORDER_QTY:
        logger.info("Qty %.8f < minimum %.0f -> using minimum for NEW trade", final_qty, MIN_NEW_ORDER_QTY)
        final_qty = MIN_NEW_ORDER_QTY
    final_qty = floor_to_step(final_qty, QTY_STEP)
    if final_qty <= 0:
        logger.warning("Final qty <= 0 after step adjustment; aborting")
        return

    # If there's an open position:
    if open_pos:
        logger.info("Existing open position detected: side=%s entry=%.8f sl=%.8f tp=%.8f qty=%.8f",
                    open_pos["side"], open_pos["entry"], open_pos["sl"], open_pos["tp"], open_pos["qty"])

        # Check if last candle hit the open trade's TP/SL
        outcome = check_candle_hit_levels(open_pos, last)
        if outcome == "tp":
            logger.info("Existing trade TP hit during last candle. Closing trade and updating balance.")
            baseline_balance += abs(open_pos["entry"] - open_pos["sl"])  # +risk
            state["open_position"] = None
            state["baseline_balance"] = baseline_balance
            save_state(state)
            return
        if outcome == "sl":
            logger.info("Existing trade SL hit during last candle. Closing trade and updating balance.")
            baseline_balance -= abs(open_pos["entry"] - open_pos["sl"])
            state["open_position"] = None
            state["baseline_balance"] = baseline_balance
            save_state(state)
            return

        # Otherwise, we may consider adjusting TP/SL for the existing position if same side
        if open_pos["side"] == sig:
            # compute prospective new levels if we wanted to modify using current calculation
            new_sl = sl
            new_tp = tp
            old_sl = open_pos["sl"]
            old_tp = open_pos["tp"]
            old_risk = abs(open_pos["entry"] - old_sl)
            new_risk = abs(open_pos["entry"] - new_sl)
            old_reward = abs(old_tp - open_pos["entry"])
            new_reward = abs(new_tp - open_pos["entry"])

            logger.info("Considering update for existing %s trade: Old SL=%.8f TP=%.8f -> Candidate SL=%.8f TP=%.8f",
                        open_pos["side"], old_sl, old_tp, new_sl, new_tp)

            # Only update if new_risk < old_risk (reduces potential loss) OR new_reward > old_reward (increases potential profit)
            if (new_risk < old_risk) or (new_reward > old_reward):
                # update
                logger.info("🔄 Update %s trade | Old SL=%.8f TP=%.8f -> New SL=%.8f TP=%.8f",
                            open_pos["side"], old_sl, old_tp, new_sl, new_tp)
                open_pos["sl"] = round_price(new_sl)
                open_pos["tp"] = round_price(new_tp)
                state["open_position"] = open_pos
                save_state(state)
            else:
                logger.info("No beneficial update to SL/TP; leaving existing levels.")
        else:
            # Open pos exists and opposite side signal: per your instruction, DO NOT close current trade on opposite signal
            logger.info("Opposite-side signal detected but current position exists -> NOT closing per rules.")
        return

    # No open position: open one
    logger.info("Opening NEW %s trade: Entry=%.8f SL=%.8f TP=%.8f qty=%.8f", sig, entry, sl, tp, final_qty)

    # Place simulated or live market order
    placed = maybe_place_live_market(sig, final_qty)
    if not placed:
        logger.warning("Order placement failed (LIVE). Aborting.")
        return

    # Create open position record (simulated)
    open_rec = {
        "side": sig,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "qty": final_qty,
        "open_time": last["ts"]
    }
    state["open_position"] = open_rec
    state["baseline_balance"] = baseline_balance
    save_state(state)
    logger.info("📈 New %s trade recorded (simulated).", sig)

# ---------------- SCHEDULER ----------------
def wait_until_next_hour():
    now = datetime.utcnow()
    seconds = now.minute * 60 + now.second
    to_wait = 3600 - seconds
    if to_wait <= 0:
        to_wait = 1
    logger.info("Sleeping %d seconds until next top-of-hour (UTC). Now=%s", to_wait, now.strftime("%Y-%m-%d %H:%M:%S"))
    time.sleep(to_wait)

# ---------------- ENTRY POINT ----------------
if __name__ == "__main__":
    logger.info("Starting HA live bot (one-way behavior). LIVE=%s", LIVE)
    # Align to top-of-hour before first run (so persisted INITIAL_HA_OPEN will be used for first closed candle)
    logger.info("Initial alignment: sleeping until next top-of-hour before first run")
    wait_until_next_hour()
    while True:
        try:
            run_once()
        except Exception:
            logger.exception("Error during run_once()")
        wait_until_next_hour()
        
    
