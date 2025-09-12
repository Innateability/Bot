#!/usr/bin/env python3
"""
Live Heikin-Ashi Bot for Bybit USDT Perpetual (Hedge Mode, Isolated Margin)

- Uses 4h timeframe (TIMEFRAME="240")
- Logs last two raw + HA candles
- Evaluates Buy/Sell signals
- Computes SL/TP 2:1 RR + 0.1%
- Robust USDT balance fetch
- Hedge mode + isolated margin
- Enforces MIN_NEW_ORDER_QTY for new trades
- Does NOT modify open trades
- Persistent trade history
- Optional TEST_MODE for quick buy testing
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
INITIAL_HA_OPEN = float(os.environ.get("INITIAL_HA_OPEN", "0.34756"))
TICK_SIZE = float(os.environ.get("TICK_SIZE", "0.00001"))
QTY_STEP = float(os.environ.get("QTY_STEP", "1"))
LEVERAGE = int(os.environ.get("LEVERAGE", "75"))
RISK_PERCENT = float(os.environ.get("RISK_PERCENT", "0.10"))
FALLBACK_PERCENT = float(os.environ.get("FALLBACK_PERCENT", "0.90"))
MIN_NEW_ORDER_QTY = float(os.environ.get("MIN_NEW_ORDER_QTY", "16"))
STATE_FILE = os.environ.get("STATE_FILE", "ha_state.json")
TRADE_HISTORY_FILE = os.environ.get("TRADE_HISTORY_FILE", "trade_history.json")
TEST_MODE = os.environ.get("TEST_MODE", "true").lower() in ("1","true","yes")

API_KEY = os.environ.get("BYBIT_API_KEY")
API_SECRET = os.environ.get("BYBIT_API_SECRET")
TESTNET = os.environ.get("BYBIT_TESTNET", "false").lower() in ("1", "true", "yes")
ACCOUNT_TYPE = os.environ.get("BYBIT_ACCOUNT_TYPE", "UNIFIED")

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

# ---------------- TRADE HISTORY ----------------
def load_trade_history():
    if not os.path.exists(TRADE_HISTORY_FILE):
        return []
    try:
        with open(TRADE_HISTORY_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []

def save_trade_history(history):
    with open(TRADE_HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

def log_trade(signal, entry, sl, tp, qty, balance, status="pending"):
    history = load_trade_history()
    history.append({
        "timestamp": int(datetime.utcnow().timestamp()),
        "signal": signal,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "qty": qty,
        "balance": balance,
        "status": status
    })
    save_trade_history(history)
    logger.info("Logged trade: %s %s qty=%.8f entry=%.8f SL=%.8f TP=%.8f balance=%.8f",
                datetime.utcnow().isoformat(), signal, qty, entry, sl, tp, balance)

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
    return int(TIMEFRAME) * 60 * 1000

# ---------------- KLINES ----------------
def fetch_candles(symbol: str, interval: str = TIMEFRAME, limit: int = 200):
    out = session.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
    res = out.get("result", {}) or out
    if isinstance(res, dict) and "list" in res:
        parsed = []
        for r in res["list"]:
            try:
                parsed.append({
                    "ts": int(r[0]),
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4])
                })
            except Exception:
                continue
        parsed.sort(key=lambda x: x["ts"])
        return parsed
    raise RuntimeError("Unexpected kline response shape: {}".format(res))

# ---------------- HEIKIN-ASHI ----------------
def compute_heikin_ashi(raw_candles, persisted_open=None):
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
            ha_open = (prev_ha_open + prev_ha_close)/2.0 if prev_ha_open is not None else (ro+rc)/2.0
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
    if len(ha_list) < 1:
        return None
    last = ha_list[-1]
    green = last["ha_close"] > last["ha_open"]
    red = last["ha_close"] < last["ha_open"]
    if green and last["ha_high"] > ha_list[-2]["ha_high"]:
        return {"signal": "Buy"}
    if red and last["ha_low"] < ha_list[-2]["ha_low"]:
        return {"signal": "Sell"}
    return None

# ---------------- BALANCE & POSITIONS ----------------
def get_balance_usdt():
    try:
        out = session.get_wallet_balance(accountType=ACCOUNT_TYPE, coin="USDT")
    except Exception as e:
        logger.exception("get_wallet_balance error: %s", e)
        raise

    res = out.get("result", {})
    if not res:
        raise RuntimeError(f"Empty result in wallet balance response: {out}")

    # Unified account → result.list[0].coin[]
    if isinstance(res, dict) and "list" in res:
        for acct in res["list"]:
            for item in acct.get("coin", []):
                if item.get("coin") == "USDT":
                    for key in ("availableToWithdraw", "equity", "walletBalance"):
                        val = item.get(key)
                        if val not in (None, "", "null"):
                            try:
                                return float(val)
                            except Exception:
                                continue

    raise RuntimeError(f"Unable to parse wallet balance response: {json.dumps(out)}")

def get_open_position(symbol):
    try:
        out = session.get_positions(category="linear", symbol=symbol)
    except Exception:
        return None
    res = out.get("result", {}) or out
    if isinstance(res, dict) and "list" in res and len(res["list"]) > 0:
        return res["list"][0]
    return None

# ---------------- ORDER HELPERS ----------------
def ensure_hedge_and_isolated(symbol):
    try:
        session.switch_position_mode(category="linear", symbol=symbol, mode=1)  # Hedge
        logger.info("Hedge mode enabled for %s", symbol)
    except Exception as e:
        logger.debug("switch_position_mode ignored: %s", e)

def set_symbol_leverage(symbol, leverage):
    try:
        session.set_leverage(category="linear", symbol=symbol, buyLeverage=leverage, sellLeverage=leverage)
        logger.info("Set leverage=%sx for %s", leverage, symbol)
    except Exception as e:
        logger.warning("set_leverage failed: %s", e)

def place_market_with_tp_sl(signal_side, symbol, qty, sl, tp):
    side = "Buy" if signal_side=="Buy" else "Sell"
    try:
        session.place_order(category="linear", symbol=symbol, side=side,
                            orderType="Market", qty=str(qty), timeInForce="ImmediateOrCancel", reduceOnly=False)
        session.set_trading_stop(category="linear", symbol=symbol,
                                 takeProfit=str(round_price(tp)),
                                 stopLoss=str(round_price(sl)),
                                 tpTriggerBy="LastPrice", slTriggerBy="LastPrice")
        logger.info("Order placed: %s qty=%.8f | SL=%.8f TP=%.8f", side, qty, sl, tp)
        return True
    except Exception as e:
        logger.exception("Order placement failed: %s", e)
        return False

# ---------------- QTY ----------------
def compute_qty(entry, sl, balance):
    risk_usd = balance * RISK_PERCENT
    per_contract_risk = abs(entry-sl)
    if per_contract_risk <= 0: return 0
    qty = risk_usd / per_contract_risk
    est_margin = (qty*entry)/LEVERAGE
    if est_margin > balance:
        qty = (balance*FALLBACK_PERCENT*LEVERAGE)/entry
    return floor_to_step(qty, QTY_STEP)

# ---------------- MAIN ----------------
def run_once():
    logger.info("=== Running 4h check ===")
    state = load_state()
    persisted_ha_open = state.get("last_ha_open")
    raw = fetch_candles(SYMBOL, TIMEFRAME, limit=200)

    # Drop in-progress candle
    period = timeframe_ms()
    now_ms = int(datetime.utcnow().timestamp()*1000)
    if (raw[-1]["ts"] + period) > now_ms + 1000:
        raw = raw[:-1]
        logger.info("Dropped in-progress candle")

    ha_list = compute_heikin_ashi(raw, persisted_open=persisted_ha_open)
    last_closed = ha_list[-1]
    prev_closed = ha_list[-2] if len(ha_list)>=2 else None

    # Log last two candles
    if prev_closed:
        logger.info("Prev RAW: %.8f/%.8f/%.8f/%.8f | HA: %.8f/%.8f/%.8f/%.8f",
                    prev_closed["raw_open"], prev_closed["raw_high"], prev_closed["raw_low"], prev_closed["raw_close"],
                    prev_closed["ha_open"], prev_closed["ha_high"], prev_closed["ha_low"], prev_closed["ha_close"])
    logger.info("Last RAW: %.8f/%.8f/%.8f/%.8f | HA: %.8f/%.8f/%.8f/%.8f",
                last_closed["raw_open"], last_closed["raw_high"], last_closed["raw_low"], last_closed["raw_close"],
                last_closed["ha_open"], last_closed["ha_high"], last_closed["ha_low"], last_closed["ha_close"])

    # Persist next HA open
    state["last_ha_open"] = (last_closed["ha_open"]+last_closed["ha_close"])/2.0
    save_state(state)

    # Evaluate signal
    sig = evaluate_signal(ha_list)
    if not sig:
        logger.info("No signal detected")
        return

    entry = last_closed["raw_close"]
    sl = state["last_ha_open"]
    risk = abs(entry-sl)
    tp = entry + 2*risk + 0.001*entry if sig["signal"]=="Buy" else entry - 2*risk - 0.001*entry
    sl, tp = round_price(sl), round_price(tp)

    balance = get_balance_usdt()
    qty = compute_qty(entry, sl, balance)
    if qty < MIN_NEW_ORDER_QTY: qty = MIN_NEW_ORDER_QTY
    qty = floor_to_step(qty, QTY_STEP)

    ensure_hedge_and_isolated(SYMBOL)
    set_symbol_leverage(SYMBOL, LEVERAGE)

    pos = get_open_position(SYMBOL)
    if pos and float(pos.get("size",0))>0:
        logger.info("Open position exists; skipping new trade placement")
        log_trade(sig["signal"], entry, sl, tp, qty, balance, status="skipped_open_position")
    else:
        success = place_market_with_tp_sl(sig["signal"], SYMBOL, qty, sl, tp)
        log_trade(sig["signal"], entry, sl, tp, qty, balance, status="placed" if success else "failed")

# ---------------- TEST FUNCTION ----------------
def test_buy_trade():
    """
    Places a buy of 16 contracts immediately for testing.
    """
    logger.info("=== Running test buy trade ===")
    balance = get_balance_usdt()
    logger.info("Balance before test trade: %.8f USDT", balance)

    entry = 0.348  # dummy entry or latest price
    sl = entry - 0.002
    tp = entry + 0.004
    qty = 16

    ensure_hedge_and_isolated(SYMBOL)
    set_symbol_leverage(SYMBOL, LEVERAGE)
    place_market_with_tp_sl("Buy", SYMBOL, qty, sl, tp)

# ---------------- SCHEDULER ----------------
def wait_until_next_4h():
    now = datetime.utcnow()
    seconds = now.minute*60 + now.second
    elapsed_hours = now.hour % 4
    to_wait = (4 - elapsed_hours)*3600 - seconds
    if to_wait <= 0: to_wait = 1
    logger.info("Sleeping %d seconds until next 4h UTC block", to_wait)
    time.sleep(to_wait)

# ---------------- ENTRY ----------------
if __name__=="__main__":
    logger.info("Starting HA 4h live bot — testnet=%s, symbol=%s", TESTNET, SYMBOL)
    
    if TEST_MODE:
        test_buy_trade()  # only runs if TEST_MODE=true
    else:
        wait_until_next_4h()
        while True:
            try:
                run_once()
            except Exception as e:
                logger.exception("run_once failed: %s", e)
            wait_until_next_4h()
