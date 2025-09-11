#!/usr/bin/env python3
"""
Bybit Heikin-Ashi live/sim bot (hedge mode, isolated margin) — Custom Rules
- Simulation mode runs through Bybit candles locally and updates sim balance when TP/SL hit.
- Does NOT modify TP/SL once placed. Does NOT open same-side trade if one already open.
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
TIMEFRAME = os.environ.get("TIMEFRAME", "60")           # minutes
INITIAL_HA_OPEN = float(os.environ.get("INITIAL_HA_OPEN", "0.34149"))
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
RISK_PERCENT = 0.045        # 4.5% of total balance risk per trade
BALANCE_USE_PERCENT = 0.45  # use 45% of balance for position sizing
FALLBACK_PERCENT = 0.45     # fallback if margin too high

# Simulation
SIMULATION_MODE = os.environ.get("SIMULATION_MODE", "true").lower() in ("1", "true", "yes")
INITIAL_BALANCE = float(os.environ.get("INITIAL_BALANCE", "5.0"))
sim_balance = INITIAL_BALANCE
sim_positions = []  # list of dicts: {side, entry, qty, sl, tp, entry_ts}

# ---------------- LOG ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ha_bot")

# ---------------- CLIENT ----------------
# Create session always (we only place real orders when SIMULATION_MODE==False)
session = HTTP(testnet=TESTNET, api_key=API_KEY or None, api_secret=API_SECRET or None)
if SIMULATION_MODE:
    logger.info("Running in SIMULATION MODE with starting balance %.2f USDT", sim_balance)

# ---------------- HELPERS ----------------
def round_price(p: float) -> float:
    ticks = round(p / TICK_SIZE)
    return round(ticks * TICK_SIZE, 8)

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
def fetch_candles(symbol: str, interval: str = TIMEFRAME, limit: int = 1000):
    """Fetch candles from Bybit public kline. Returns list oldest->newest of dicts with ts/open/high/low/close."""
    out = session.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
    res = out.get("result", {}) or out
    # v3 shape: result -> list
    rows = []
    if isinstance(res, dict) and "list" in res:
        rows = res["list"]
    elif isinstance(res, list):
        rows = res
    else:
        rows = []

    candles = []
    for r in rows:
        try:
            # r shape may be [ts, open, high, low, close, ...]
            ts = int(r[0])
            o = float(r[1]); h = float(r[2]); l = float(r[3]); c = float(r[4])
            candles.append({"ts": ts, "open": o, "high": h, "low": l, "close": c})
        except Exception:
            continue
    candles.sort(key=lambda x: x["ts"])
    return candles

# ---------------- HEIKIN-ASHI ----------------
def compute_heikin_ashi(raw_candles, persisted_open=None):
    """Compute HA candles for raw_candles (oldest->newest). If persisted_open provided, use it for the LAST closed candle's ha_open."""
    ha = []
    prev_ha_open, prev_ha_close = None, None
    n = len(raw_candles)
    for i, c in enumerate(raw_candles):
        ro, rh, rl, rc = c["open"], c["high"], c["low"], c["close"]
        ha_close = (ro + rh + rl + rc) / 4.0
        # For the most recent (last) closed candle, use persisted_open if given OR INITIAL_HA_OPEN (manual seed).
        if i == n - 1:
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

# ---------------- ENTRY SIGNAL ----------------
def evaluate_signal(ha_list):
    """Return {'signal':'Buy'|'Sell'} or None using last two HA candles per rules."""
    if len(ha_list) < 2:
        return None
    prev_candle, last_candle = ha_list[-2], ha_list[-1]
    # Buy: previous HA was red and next candle's high > previous high
    if prev_candle["ha_close"] < prev_candle["ha_open"] and last_candle["ha_high"] > prev_candle["ha_high"]:
        return {"signal": "Buy"}
    # Sell: previous HA was green and next candle's low < previous low
    if prev_candle["ha_close"] > prev_candle["ha_open"] and last_candle["ha_low"] < prev_candle["ha_low"]:
        return {"signal": "Sell"}
    return None

# ---------------- BALANCE ----------------
def get_balance_usdt():
    """Return real wallet balance or simulated balance."""
    global sim_balance
    if SIMULATION_MODE:
        return sim_balance
    out = session.get_wallet_balance(accountType=ACCOUNT_TYPE, coin="USDT")
    res = out.get("result", {}) or out
    # robust search for USDT balance
    if isinstance(res, dict) and "list" in res:
        for item in res["list"]:
            coins = item.get("coin")
            if isinstance(coins, list):
                for c in coins:
                    if isinstance(c, dict) and c.get("coin") == "USDT":
                        for key in ("availableToWithdraw","availableBalance","walletBalance","usdValue","equity"):
                            if key in c and c[key] not in (None,""," "):
                                try:
                                    return float(c[key])
                                except Exception:
                                    pass
                        # fallback numeric
                        for v in c.values():
                            try:
                                return float(v)
                            except Exception:
                                pass
    # fallback error
    raise RuntimeError("Could not fetch balance")

# ---------------- POSITIONS ----------------
def get_open_positions(symbol):
    """Return live positions or simulated positions list."""
    global sim_positions
    if SIMULATION_MODE:
        return sim_positions
    out = session.get_positions(category="linear", symbol=symbol)
    res = out.get("result", {}) or out
    return res.get("list", [])

def has_open_position(side, positions):
    """Check if positions list contains an open position for the given side."""
    for p in positions:
        # For sim: p dict uses 'side' and 'qty'; for live: p has keys from Bybit
        if SIMULATION_MODE:
            if p.get("side") == side and float(p.get("qty", p.get("size", 0) or 0)) > 0:
                return True
        else:
            if p.get("side") == side and float(p.get("size", 0)) > 0:
                return True
    return False

# ---------------- ORDER HELPERS ----------------
def place_market_with_tp_sl(signal_side, symbol, qty, sl, tp, entry_ts, entry_price):
    """Place real order (live) or record simulated order (sim)."""
    global sim_positions
    side = "Buy" if signal_side == "Buy" else "Sell"
    if SIMULATION_MODE:
        # record entry timestamp so we only check candles after entry for TP/SL hits
        sim_positions.append({
            "side": side,
            "entry": float(entry_price),
            "qty": float(qty),
            "sl": float(sl),
            "tp": float(tp),
            "entry_ts": int(entry_ts)
        })
        logger.info("[SIM] Placed %s | entry=%.8f | qty=%.2f | SL=%.8f | TP=%.8f", side, entry_price, qty, sl, tp)
        return {"sim": True}
    try:
        resp = session.place_order(
            category="linear",
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=str(qty),
            timeInForce="IOC",
            reduceOnly=False
        )
        logger.info("Placed market %s qty=%s resp=%s", side, qty, resp)
        resp2 = session.set_trading_stop(
            category="linear",
            symbol=symbol,
            takeProfit=str(round_price(tp)),
            stopLoss=str(round_price(sl)),
            tpTriggerBy="LastPrice",
            slTriggerBy="LastPrice"
        )
        logger.info("Attached TP=%s SL=%s resp=%s", tp, sl, resp2)
        return resp
    except Exception as e:
        logger.exception("Failed to place order: %s", e)
        return None

# ---------------- SIM CLOSE / PnL ----------------
def _close_sim_position(pos, exit_price, exit_ts):
    """Close simulated position pos at exit_price and update sim_balance."""
    global sim_balance, sim_positions
    side = pos["side"]
    entry = float(pos["entry"])
    qty = float(pos["qty"])
    # PnL in USDT (contracts * price difference)
    if side == "Buy":
        pnl = (exit_price - entry) * qty
    else:  # Sell
        pnl = (entry - exit_price) * qty
    sim_balance += pnl
    logger.info("[SIM] CLOSED %s | entry=%.8f exit=%.8f qty=%.2f pnl=%.6f new_balance=%.6f (exit_ts=%d)",
                side, entry, exit_price, qty, pnl, sim_balance, exit_ts)
    # remove pos from sim_positions
    sim_positions = [p for p in sim_positions if not (p is pos)]

# ---------------- RISK / QTY ----------------
def compute_qty(entry, sl, balance):
    """Compute qty (contracts) based on RISK_PERCENT of total balance and fallback cap."""
    avail_balance = balance * BALANCE_USE_PERCENT
    risk_usd = balance * RISK_PERCENT
    per_contract_risk = abs(entry - sl)
    if per_contract_risk <= 0:
        return 0.0
    qty = risk_usd / per_contract_risk
    est_margin = (qty * entry) / LEVERAGE
    if est_margin > avail_balance:
        qty = (avail_balance * FALLBACK_PERCENT * LEVERAGE) / entry
    return floor_to_step(qty, QTY_STEP)

# ---------------- MAIN RUN-ONCE (works for live and simulation) ----------------
def run_once(raw=None):
    """
    If raw provided: treat as candle list (oldest->newest).
    If raw is None and SIMULATION_MODE is False: fetch current candles from API.
    """
    global sim_positions
    logger.info("=== New cycle ===")
    state = load_state()
    persisted_open = state.get("last_ha_open")

    # get candles
    if raw is None:
        # live mode: fetch candles directly
        raw = fetch_candles(SYMBOL, TIMEFRAME, limit=200)
    # else raw passed by simulator stepping

    if not raw or len(raw) < 2:
        logger.warning("Not enough candles")
        return

    # drop in-progress candle in live mode
    if not SIMULATION_MODE:
        now_ms = int(datetime.utcnow().timestamp() * 1000)
        if raw[-1]["ts"] + timeframe_ms() > now_ms:
            raw = raw[:-1]

    # compute HA using persisted_open for last closed candle (to match TV)
    ha_list = compute_heikin_ashi(raw, persisted_open)
    last_closed = ha_list[-1]   # last closed HA candle
    next_open = (last_closed["ha_open"] + last_closed["ha_close"]) / 2.0
    state["last_ha_open"] = float(next_open)
    save_state(state)

    # --- FIRST: check simulated open positions against this last_closed candle for TP/SL hits ---
    if SIMULATION_MODE and sim_positions:
        lc_raw_high = last_closed["raw_high"]
        lc_raw_low = last_closed["raw_low"]
        lc_ts = last_closed["ts"]
        # iterate over copy because we may remove during iteration
        for pos in sim_positions[:]:
            # only check candles after entry
            if lc_ts <= int(pos.get("entry_ts", 0)):
                continue
            side = pos["side"]
            entry_price = float(pos["entry"])
            sl = float(pos["sl"])
            tp = float(pos["tp"])
            # For buys: TP > entry, SL < entry
            if side == "Buy":
                hit_tp = lc_raw_high >= tp
                hit_sl = lc_raw_low <= sl
                if hit_tp and hit_sl:
                    # Ambiguous intrabar: choose TP first (profit)
                    _close_sim_position(pos, tp, lc_ts)
                elif hit_tp:
                    _close_sim_position(pos, tp, lc_ts)
                elif hit_sl:
                    _close_sim_position(pos, sl, lc_ts)
            else:  # Sell
                # For sells: TP < entry, SL > entry
                hit_tp = lc_raw_low <= tp
                hit_sl = lc_raw_high >= sl
                if hit_tp and hit_sl:
                    # ambiguous: choose TP first
                    _close_sim_position(pos, tp, lc_ts)
                elif hit_tp:
                    _close_sim_position(pos, tp, lc_ts)
                elif hit_sl:
                    _close_sim_position(pos, sl, lc_ts)

    # now evaluate signal based on HA candles
    sig = evaluate_signal(ha_list)
    if not sig:
        logger.info("No valid signal this cycle")
        return

    logger.info("Signal detected: %s", sig["signal"])

    # entry price is raw close of last closed candle (per your rule)
    entry_price = float(last_closed["raw_close"])
    if sig["signal"] == "Buy":
        sl = last_closed["ha_low"] - PIP
        risk = abs(entry_price - sl)
        tp = entry_price + (2 * risk) + (0.001 * entry_price)
    else:
        sl = last_closed["ha_high"] + PIP
        risk = abs(entry_price - sl)
        tp = entry_price - (2 * risk + (0.001 * entry_price))

    balance = get_balance_usdt()
    qty = compute_qty(entry_price, sl, balance)

    logger.info("Balance=%.6f entry=%.8f sl=%.8f tp=%.8f qty=%.4f", balance, entry_price, sl, tp, qty)

    # check existing positions and skip entry if same side exists
    positions = get_open_positions(SYMBOL)
    if has_open_position(sig["signal"], positions):
        logger.info("A %s position is already open — skipping new entry", sig["signal"])
        return

    # open trade (live or sim)
    if qty > 0:
        # entry timestamp: use last_closed timestamp so simulation checks only later candles
        entry_ts = last_closed["ts"]
        resp = place_market_with_tp_sl(sig["signal"], SYMBOL, qty, sl, tp, entry_ts, entry_price)
        # do NOT modify TP/SL after placement per your rule
    else:
        logger.info("Computed qty <= 0 — skipping entry")

# ---------------- SIMULATION DRIVER ----------------
def run_simulation_from_api(limit=1000):
    """
    Fetch candles from Bybit and step through them one-by-one,
    calling run_once(raw=prefix_candles) each step.
    """
    logger.info("Fetching up to %d candles for symbol %s interval %s", limit, SYMBOL, TIMEFRAME)
    candles = fetch_candles(SYMBOL, TIMEFRAME, limit=limit)
    if not candles:
        logger.error("No candles fetched")
        return
    # We'll iterate from index 2 ... len-1 (need at least 2 candles to evaluate)
    start_idx = 2
    total = len(candles)
    logger.info("Simulating %d candles (from idx %d to %d)", total - start_idx, start_idx, total - 1)
    for i in range(start_idx, total):
        # use prefix up to i (inclusive) as the dataset of closed candles
        prefix = candles[: i + 1]
        run_once(raw=prefix)
        # small sleep so logs are readable; remove or lower for faster runs
        time.sleep(0.01)
    logger.info("Simulation complete. Final balance=%.6f", sim_balance)
    logger.info("Remaining open positions (if any): %s", sim_positions)

# ---------------- SCHEDULER ----------------
def wait_until_next_cycle(hours=4, offset=1):
    now = datetime.utcnow()
    cycle_seconds = hours * 3600
    # shift the cycles by 'offset' hours
    elapsed = (now.hour - offset) * 3600 + now.minute * 60 + now.second
    to_wait = cycle_seconds - (elapsed % cycle_seconds)
    if to_wait <= 0:
        to_wait += cycle_seconds
    logger.info("Sleeping %d seconds until next cycle (offset %d)", to_wait, offset)
    time.sleep(to_wait)
# ---------------- ENTRY POINT ----------------
if __name__ == "__main__":
    logger.info("Starting HA bot — hedge mode, isolated margin | SIMULATION=%s", SIMULATION_MODE)
    if SIMULATION_MODE:
        # run a full simulation using Bybit candle history
        run_simulation_from_api(limit=1000)
    else:
        # live mode: align to 4-hour cycle and run periodically
        wait_until_next_cycle(4)
        while True:
            try:
                run_once()
            except Exception:
                logger.exception("Error in run_once")
            wait_until_next_cycle(4, offset=1)
