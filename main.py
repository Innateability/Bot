"""
Live Heikin-Ashi Bot for Bybit USDT Perpetual (One-Way Mode)
- Initial HA open configurable (used only for the very first candle you deploy into)
- Hourly run at candle open (UTC)
- Proper HA-open update each hour: next_HA_open = (prev_HA_open + prev_HA_close)/2
- Incremental sizing + TP/SL update if new size > current size
- One-way mode, 75x leverage
- Uses pybit unified_trading (v3.x)
"""

import os
import time
import json
import logging
from math import floor
from datetime import datetime

# ---- pybit v3 unified client ----
from pybit.unified_trading import HTTP

# ---------------- CONFIG ----------------
SYMBOL = os.environ.get("SYMBOL", "TRXUSDT")
TIMEFRAME = os.environ.get("TIMEFRAME", "60")   # 1h klines
INITIAL_HA_OPEN = float(os.environ.get("INITIAL_HA_OPEN", "0.34185"))
TICK_SIZE = float(os.environ.get("TICK_SIZE", "0.00001"))   # price tick
QTY_STEP = float(os.environ.get("QTY_STEP", "1"))           # contract step
LEVERAGE = int(os.environ.get("LEVERAGE", "75"))
RISK_PERCENT = float(os.environ.get("RISK_PERCENT", "0.10"))
FALLBACK_PERCENT = float(os.environ.get("FALLBACK_PERCENT", "0.90"))
START_SIP_BALANCE = float(os.environ.get("START_SIP_BALANCE", "4.0"))
SIP_PERCENT = float(os.environ.get("SIP_PERCENT", "0.25"))
STATE_FILE = os.environ.get("STATE_FILE", "ha_state.json")

API_KEY = os.environ.get("BYBIT_API_KEY")
API_SECRET = os.environ.get("BYBIT_API_SECRET")
TESTNET = os.environ.get("BYBIT_TESTNET", "false").lower() in ("1", "true", "yes")
ACCOUNT_TYPE = os.environ.get("BYBIT_ACCOUNT_TYPE", "UNIFIED")  # UNIFIED or CONTRACT (main account = UNIFIED for UTA)

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

# ---------------- HELPERS ----------------
def round_price(p: float) -> float:
    if TICK_SIZE <= 0:
        return p
    return round(round(p / TICK_SIZE) * TICK_SIZE, 8)

def floor_to_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return floor(x / step) * step

# ---------------- KLINES ----------------
def fetch_candles(symbol: str, interval: str = TIMEFRAME, limit: int = 200):
    """Return CLOSED candles oldest->newest as dicts."""
    out = session.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
    res = out.get("result", {}) or out
    if isinstance(res, dict) and "list" in res:
        rows = res["list"]
        parsed = []
        for r in rows:
            # [startTime(ms), open, high, low, close, volume, turnover]
            parsed.append({
                "ts": int(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4])
            })
        parsed.sort(key=lambda x: x["ts"])
        return parsed
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
    """
    - Use persisted_open (from state) for the LAST (most recent, closed) candle's HA open.
    - For earlier candles, use recursive (prev_ha_open + prev_ha_close)/2 chain.
    - Return list oldest->newest with HA fields.
    """
    ha = []
    prev_ha_open = None
    prev_ha_close = None
    n = len(raw_candles)
    for i, c in enumerate(raw_candles):
        ro, rh, rl, rc = c["open"], c["high"], c["low"], c["close"]
        ha_close = (ro + rh + rl + rc) / 4.0
        if i == n - 1:
            # last closed candle gets persisted_open (or INITIAL_HA_OPEN on very first run)
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
    """
    Use the last CLOSED candle as the signal candle, and the upcoming new hour (entry/SL)
    will use:
      - entry: raw open of the NEXT candle (approx use last close as proxy at top-of-hour)
      - SL: HA open of the NEW candle (computed as next_ha_open)
    We compute entry later once we know next_ha_open; here we just flag direction.
    """
    if len(ha_list) < 2:
        return None
    prev = ha_list[-1]  # last CLOSED candle
    prev_green = prev["ha_close"] > prev["ha_open"]
    prev_red = prev["ha_close"] < prev["ha_open"]

    if prev_green and abs(prev["ha_low"] - prev["ha_open"]) <= TICK_SIZE:
        return {"signal": "Buy"}
    if prev_red and abs(prev["ha_high"] - prev["ha_open"]) <= TICK_SIZE:
        return {"signal": "Sell"}
    return None

# ---------------- BALANCE & POSITIONS ----------------
def get_balance_usdt():
    """Return available USDT balance from main (UNIFIED/CONTRACT) account."""
    out = session.get_wallet_balance(accountType=ACCOUNT_TYPE, coin="USDT")
    res = out.get("result", {}) or out
    if isinstance(res, dict) and "list" in res:
        for item in res["list"]:
            if isinstance(item, dict) and item.get("coin") == "USDT":
                # Prefer availableToWithdraw / availableBalance; fallback walletBalance
                vals = item.get("walletBalance"), item.get("availableBalance"), item.get("availableToWithdraw")
                for key in ("availableToWithdraw", "availableBalance", "walletBalance"):
                    if key in item:
                        return float(item[key])
                # final fallback
                return float([v for v in vals if v is not None][0])
    raise RuntimeError("Unable to parse wallet balance response")

def get_open_position(symbol):
    """Return first open position (one-way) or None."""
    out = session.get_positions(category="linear", symbol=symbol)
    res = out.get("result", {}) or out
    if isinstance(res, dict) and "list" in res and len(res["list"]) > 0:
        return res["list"][0]
    return None

# ---------------- ORDER HELPERS ----------------
def ensure_one_way(symbol):
    """Try to enforce one-way mode (mode=0)."""
    try:
        session.switch_position_mode(category="linear", symbol=symbol, mode=0)  # 0=One-way, 3=Hedge
    except Exception as e:
        logger.debug("switch_position_mode ignored: %s", e)

def set_symbol_leverage(symbol, leverage):
    try:
        session.set_leverage(category="linear", symbol=symbol, buyLeverage=leverage, sellLeverage=leverage)
    except Exception as e:
        logger.warning("set_leverage failed: %s", e)

def place_market_with_tp_sl(signal_side, symbol, qty, sl, tp):
    side = "Buy" if signal_side == "Buy" else "Sell"
    qty = str(qty)
    sl = str(round_price(sl))
    tp = str(round_price(tp))
    try:
        # place market order
        session.place_order(
            category="linear",
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=qty,
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
            takeProfit=tp,
            stopLoss=sl,
            tpTriggerBy="LastPrice",
            slTriggerBy="LastPrice"
        )
        logger.info("Attached TP=%s SL=%s", tp, sl)
    except Exception as e:
        logger.exception("set_trading_stop failed: %s", e)
    return True

def modify_tp_sl_and_maybe_increase(symbol, new_sl, new_tp, new_qty):
    pos = get_open_position(symbol)
    if not pos:
        logger.info("No open position to modify")
        return False

    # parse current size & entry
    try:
        size = float(pos.get("size") or pos.get("qty") or 0)
    except Exception:
        size = 0.0
    try:
        entry_price = float(pos.get("entryPrice") or pos.get("avgEntryPrice") or 0)
    except Exception:
        entry_price = 0.0
    try:
        side = pos.get("side") or ("Buy" if size > 0 else "Sell")
    except Exception:
        side = "Buy"

    # always update TP/SL
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

    # only increase if new_qty > size
    if new_qty > size:
        additional = new_qty - size
        balance = get_balance_usdt()
        denom = entry_price if entry_price > 0 else 1.0
        max_affordable = (balance * FALLBACK_PERCENT * LEVERAGE) / denom
        qty_to_open = min(additional, max_affordable)
        qty_to_open = floor_to_step(qty_to_open, QTY_STEP)
        if qty_to_open <= 0:
            logger.info("Cannot afford additional qty, skipping increase")
            return True
        placed = place_market_with_tp_sl(side, symbol, qty_to_open, new_sl, new_tp)
        logger.info("Increased position by %s (requested %s)", qty_to_open, additional)
        return placed
    else:
        logger.info("New qty <= current qty (%.8f <= %.8f) — only TP/SL updated", new_qty, size)
        return True

# ---------------- RISK/QTY ----------------
def compute_qty(entry, sl, balance):
    risk_usd = balance * RISK_PERCENT
    per_contract_risk = abs(entry - sl)
    if per_contract_risk <= 0:
        return 0.0
    # qty by risk
    qty = risk_usd / per_contract_risk
    # if margin would exceed balance, fallback to 90% balance
    est_margin = (qty * entry) / LEVERAGE
    if est_margin > balance:
        qty = (balance * FALLBACK_PERCENT * LEVERAGE) / entry
    qty = floor_to_step(qty, QTY_STEP)
    return max(qty, 0.0)

# ---------------- SIPHON ----------------
def siphon_if_needed(baseline_balance):
    bal = get_balance_usdt()
    if baseline_balance is None:
        return baseline_balance
    if baseline_balance >= START_SIP_BALANCE and bal >= 2 * baseline_balance:
        amount = round(bal * SIP_PERCENT)
        logger.info("Siphoning approx %s USDT to fund account (implement transfer API)", amount)
        # Implement actual transfer with session.transfer(...) if you want
        return bal
    return baseline_balance

# ---------------- MAIN FLOW ----------------
def run_once():
    state = load_state()
    persisted_ha_open = state.get("last_ha_open")  # this is the HA open to use for the *last closed* candle
    baseline_balance = state.get("baseline_balance")

    # 1) Fetch closed candles and compute HA, forcing last candle HA-open = persisted_ha_open (or INITIAL_HA_OPEN on first run)
    raw = fetch_candles(SYMBOL, TIMEFRAME, limit=200)
    if not raw:
        logger.warning("No klines; skipping this run.")
        return

    ha_list = compute_heikin_ashi(raw, persisted_open=persisted_ha_open)
    last_closed = ha_list[-1]  # signal candle
    logger.info("RAW last: o/h/l/c %.8f %.8f %.8f %.8f", last_closed["raw_open"], last_closed["raw_high"], last_closed["raw_low"], last_closed["raw_close"])
    logger.info("HA  last: o/h/l/c %.8f %.8f %.8f %.8f", last_closed["ha_open"], last_closed["ha_high"], last_closed["ha_low"], last_closed["ha_close"])

    # 2) Calculate NEXT hour's HA open and persist it (THIS FIXES THE 'stuck at initial' ISSUE)
    next_ha_open = (last_closed["ha_open"] + last_closed["ha_close"]) / 2.0
    state["last_ha_open"] = float(next_ha_open)
    save_state(state)

    # 3) Evaluate signal on the last closed candle
    sig = evaluate_signal(ha_list)
    if not sig:
        logger.info("No signal this hour")
        return

    # 4) Entry is raw open of the NEW candle; at the top of hour we approximate with last close
    entry = float(last_closed["raw_close"])  # best available at t=00:00
    sl = float(next_ha_open)                 # SL = HA open of NEW candle
    risk = abs(entry - sl)
    if risk <= 0:
        logger.info("Zero risk (entry == SL); skipping")
        return

    if sig["signal"] == "Buy":
        tp = entry + risk + 0.001 * entry
    else:
        tp = entry - (risk + 0.001 * entry)

    # round prices
    sl = round_price(sl)
    tp = round_price(tp)

    logger.info("Signal=%s | Next_HA_Open(SL)=%.8f | Entry≈%.8f | TP=%.8f", sig["signal"], sl, entry, tp)

    # 5) Balance baseline and qty
    bal = get_balance_usdt()
    if baseline_balance is None:
        baseline_balance = bal
        state["baseline_balance"] = baseline_balance
        save_state(state)

    qty = compute_qty(entry, sl, bal)
    if qty <= 0:
        logger.warning("Computed qty 0 — abort")
        return

    # 6) Ensure one-way + leverage
    ensure_one_way(SYMBOL)
    set_symbol_leverage(SYMBOL, LEVERAGE)

    # 7) Trade logic: if open position exists → update TP/SL and increase qty if new>old; else place new
    pos = get_open_position(SYMBOL)
    if pos and float(pos.get("size", 0) or 0) != 0:
        modify_tp_sl_and_maybe_increase(SYMBOL, sl, tp, qty)
    else:
        place_market_with_tp_sl(sig["signal"], SYMBOL, qty, sl, tp)

    # 8) Siphon if doubled and baseline >= $4
    new_baseline = siphon_if_needed(baseline_balance)
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
    logger.info("Starting HA live bot (Bybit USDT perp) — testnet=%s, accountType=%s", TESTNET, ACCOUNT_TYPE)
    while True:
        try:
            run_once()
        except Exception:
            logger.exception("Error during run_once()")
        wait_until_next_hour()
        
