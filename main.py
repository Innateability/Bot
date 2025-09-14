#!/usr/bin/env python3
"""
Live Heikin-Ashi Bot for Bybit USDT Perpetual (Hedge Mode, Isolated Margin)

- Uses 4h timeframe (TIMEFRAME="240")
- Logs last two raw + HA candles
- Evaluates Buy/Sell signals using most-recent individual red/green candles
- Confirmation filter over last 8 closed HA candles (skip if 5+ opposite color)
- SL = lower/high of last green+red (per rules) ± 1 pip
- TP split: 50% at 1:1 + 1 pip, 50% at 2:1 + 1 pip
- Robust USDT balance fetch
- Hedge mode + isolated margin (best-effort)
- Enforces MIN_NEW_ORDER_QTY for new trades
- Does NOT duplicate same-side trades (opposite-side allowed due to hedge)
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
INITIAL_HA_OPEN = float(os.environ.get("INITIAL_HA_OPEN", "0.34997"))
TICK_SIZE = float(os.environ.get("TICK_SIZE", "0.00001"))
QTY_STEP = float(os.environ.get("QTY_STEP", "1"))
LEVERAGE = int(os.environ.get("LEVERAGE", "75"))
RISK_PERCENT = float(os.environ.get("RISK_PERCENT", "0.5"))   # e.g. 0.045 -> 4.5%
FALLBACK_PERCENT = float(os.environ.get("FALLBACK_PERCENT", "0.95"))  # e.g. 0.45 -> 45%
MIN_NEW_ORDER_QTY = float(os.environ.get("MIN_NEW_ORDER_QTY", "16"))
PIP = float(os.environ.get("PIP", "0.00001"))  # 1 pip (adjustable)
STATE_FILE = os.environ.get("STATE_FILE", "ha_state.json")
TRADE_HISTORY_FILE = os.environ.get("TRADE_HISTORY_FILE", "trade_history.json")
TEST_MODE = os.environ.get("TEST_MODE", "false").lower() in ("1", "true", "yes")

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

def log_trade(signal, entry, sl, tp1, tp2, qty, balance, status="pending"):
    history = load_trade_history()
    history.append({
        "timestamp": int(datetime.utcnow().timestamp()),
        "signal": signal,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "qty": qty,
        "balance": balance,
        "status": status
    })
    save_trade_history(history)
    logger.info("Logged trade: %s %s qty=%.8f entry=%.8f SL=%.8f TP1=%.8f TP2=%.8f balance=%.8f",
                datetime.utcnow().isoformat(), signal, qty, entry if entry is not None else 0.0,
                sl if sl is not None else 0.0, tp1 if tp1 is not None else 0.0, tp2 if tp2 is not None else 0.0,
                balance if balance is not None else 0.0)

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

# ---------------- SIGNAL (new) ----------------
def evaluate_signal(ha_list):
    """
    Uses most recent individual red & green candles (not sequences).
    Rules:
     - Buy: last HA candle is green AND last_ha.ha_low > last_red.ha_low (last individual red candle)
     - Sell: last HA candle is red AND last_ha.ha_high < last_green.ha_high (last individual green candle)
    Returns None or dict:
      {"signal":"Buy"|"Sell", "entry": raw_close, "sl_raw":..., "tp1_raw":..., "tp2_raw":..., "last_red":..., "last_green":...}
    """
    if len(ha_list) < 2:
        return None

    last = ha_list[-1]       # last closed HA
    # find most recent prior red candle (individual)
    last_red = None
    last_green = None
    # search backwards (excluding last)
    for c in reversed(ha_list[:-1]):
        if last_red is None and c["ha_close"] < c["ha_open"]:
            last_red = c
        if last_green is None and c["ha_close"] > c["ha_open"]:
            last_green = c
        if last_red is not None and last_green is not None:
            break

    # If no prior required candle, we cannot confirm
    # BUY candidate
    if last["ha_close"] > last["ha_open"]:
        if last_red is None:
            return None
        # confirmation: last low must be higher than most recent red low
        if last["ha_low"] > last_red["ha_low"]:
            # SL = lower of the two candles' lows minus 1 pip
            sl_raw = min(last["ha_low"], last_red["ha_low"]) - PIP
            entry = float(last["raw_close"])
            per_contract_risk = abs(entry - sl_raw)
            if per_contract_risk <= 0:
                return None
            # two TP levels: tp1 = entry + risk + 1 pip (1:1 + 1 pip), tp2 = entry + 2*risk + 1 pip
            tp1_raw = entry + per_contract_risk + PIP
            tp2_raw = entry + 2.0 * per_contract_risk + PIP
            return {"signal":"Buy", "entry":entry, "sl_raw":sl_raw, "tp1_raw":tp1_raw, "tp2_raw":tp2_raw, "last_red":last_red, "last_green":last}
        return None

    # SELL candidate
    if last["ha_close"] < last["ha_open"]:
        if last_green is None:
            return None
        # confirmation: last high must be lower than most recent green high
        if last["ha_high"] < last_green["ha_high"]:
            # SL = higher of the two candles' highs plus 1 pip
            sl_raw = max(last["ha_high"], last_green["ha_high"]) + PIP
            entry = float(last["raw_close"])
            per_contract_risk = abs(entry - sl_raw)
            if per_contract_risk <= 0:
                return None
            tp1_raw = entry - per_contract_risk - PIP
            tp2_raw = entry - 2.0 * per_contract_risk - PIP
            return {"signal":"Sell", "entry":entry, "sl_raw":sl_raw, "tp1_raw":tp1_raw, "tp2_raw":tp2_raw, "last_green":last_green, "last_red":last}
        return None

    return None

# ---------------- CONFIRMATION FILTER ----------------
def confirmation_filter(ha_list, signal):
    """
    Look at last 8 closed HA candles (or fewer if not available).
    If a Buy signal and 5+ of last 8 were RED -> skip.
    If a Sell signal and 5+ of last 8 were GREEN -> skip.
    """
    n = min(len(ha_list), 8)
    if n < 1:
        return True  # nothing to block
    recent = ha_list[-n:]
    greens = sum(1 for c in recent if c["ha_close"] > c["ha_open"])
    reds = sum(1 for c in recent if c["ha_close"] < c["ha_open"])
    logger.info("Confirmation check (last %d): greens=%d reds=%d", n, greens, reds)
    if signal == "Buy" and reds >= 5:
        return False
    if signal == "Sell" and greens >= 5:
        return False
    return True

# ---------------- BALANCE & POSITIONS ----------------
def get_balance_usdt():
    """
    Robust parsing for Bybit unified response.
    """
    try:
        out = session.get_wallet_balance(accountType=ACCOUNT_TYPE, coin="USDT")
    except Exception as e:
        logger.exception("get_wallet_balance error: %s", e)
        raise

    # prefer 'result' from v5 response shape
    res = out.get("result", {}) if isinstance(out, dict) else {}
    if not res:
        raise RuntimeError(f"Empty result in wallet balance response: {out}")

    # Unified account -> result.list[*].coin[*]
    if isinstance(res, dict) and "list" in res:
        for acct in res["list"]:
            coins = acct.get("coin") or []
            for item in coins:
                if item.get("coin") == "USDT":
                    for key in ("availableToWithdraw", "equity", "walletBalance", "usdValue"):
                        val = item.get(key)
                        if val not in (None, "", "null"):
                            try:
                                return float(val)
                            except Exception:
                                continue

    # Fallback: top-level USDT dict
    if isinstance(res, dict) and "USDT" in res:
        u = res["USDT"]
        for key in ("available_balance", "availableBalance", "walletBalance", "usdValue"):
            if key in u and u[key] not in (None, "", "null"):
                try:
                    return float(u[key])
                except Exception:
                    continue

    logger.error("Unable to parse wallet balance response: %s", json.dumps(out))
    raise RuntimeError("Unable to parse wallet balance response")

def get_open_positions(symbol):
    try:
        out = session.get_positions(category="linear", symbol=symbol)
    except Exception as e:
        logger.exception("get_positions error: %s", e)
        return []
    res = out.get("result", {}) if isinstance(out, dict) else out
    if isinstance(res, dict) and "list" in res:
        return res["list"]
    return []

# ---------------- ORDER HELPERS ----------------
def ensure_hedge_and_isolated(symbol):
    try:
        session.switch_position_mode(category="linear", symbol=symbol, mode=1)  # 1 = hedge
        logger.info("Ensured hedge mode for %s", symbol)
    except Exception as e:
        logger.debug("switch_position_mode ignored: %s", e)
    # Isolated margin per-symbol may require other calls; user to add if necessary.

def set_symbol_leverage(symbol, leverage):
    # In hedge mode Bybit needs positionIdx; try both
    for pos_idx in (1, 2):
        try:
            session.set_leverage(category="linear", symbol=symbol,
                                 buyLeverage=leverage, sellLeverage=leverage,
                                 positionIdx=pos_idx)
            logger.info("Set leverage=%sx for %s (positionIdx=%d)", leverage, symbol, pos_idx)
        except Exception as e:
            logger.debug("set_leverage for posIdx=%d failed/ignored: %s", pos_idx, e)

def place_market_with_tp_sl(signal_side, symbol, qty, sl, tp1, tp2):
    """
    Place market order then:
      - place a reduce-only limit order to close 50% at tp1
      - set_trading_stop with takeProfit=tp2 and stopLoss=sl for the remainder
    Uses positionIdx=1 for Buy, 2 for Sell (hedge mode).
    """
    side = "Buy" if signal_side == "Buy" else "Sell"
    positionIdx = 1 if side == "Buy" else 2

    # place market
    try:
        resp = session.place_order(
            category="linear", symbol=symbol, side=side,
            positionIdx=positionIdx,
            orderType="Market", qty=str(qty),
            timeInForce="ImmediateOrCancel", reduceOnly=False
        )
        logger.info("Placed market order: side=%s qty=%s resp=%s", side, qty, resp)
    except Exception as e:
        logger.exception("place_order failed: %s", e)
        return False

    # place reduce-only limit for half at tp1
    half_qty = floor_to_step(qty / 2.0, QTY_STEP)
    try:
        if half_qty > 0:
            # limit order to close half; reduceOnly True
            resp_limit = session.place_order(
                category="linear", symbol=symbol, side=("Sell" if side == "Buy" else "Buy"),
                positionIdx=positionIdx,
                orderType="Limit",
                qty=str(half_qty),
                price=str(round_price(tp1)),
                timeInForce="PostOnly" if True else "GoodTillCancel",  # PostOnly preferred so it doesn't execute immediately except as maker
                reduceOnly=True
            )
            logger.info("Placed reduce-only limit to close half at tp1=%s resp=%s", tp1, resp_limit)
    except Exception as e:
        logger.exception("placing reduce-only TP1 limit failed: %s", e)

    # set trading stop: SL + TP2 for remaining
    try:
        resp2 = session.set_trading_stop(
            category="linear", symbol=symbol, positionIdx=positionIdx,
            takeProfit=str(round_price(tp2)), stopLoss=str(round_price(sl)),
            tpTriggerBy="LastPrice", slTriggerBy="LastPrice"
        )
        logger.info("Attached TP2=%s SL=%s resp=%s", round_price(tp2), round_price(sl), resp2)
    except Exception as e:
        logger.exception("set_trading_stop failed: %s", e)

    return True

# ---------------- QTY ----------------
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

# ---------------- MAIN ----------------
def run_once():
    logger.info("=== Running 4h check ===")
    state = load_state()
    persisted_ha_open = state.get("last_ha_open")

    try:
        raw = fetch_candles(SYMBOL, TIMEFRAME, limit=200)
    except Exception as e:
        logger.exception("fetch_candles failed: %s", e)
        return

    if not raw or len(raw) < 2:
        logger.warning("Not enough candles; skipping")
        return

    # Drop in-progress candle
    period = timeframe_ms()
    now_ms = int(datetime.utcnow().timestamp() * 1000)
    if (raw[-1]["ts"] + period) > now_ms + 1000:
        logger.info("Detected in-progress candle (dropping last returned candle)")
        raw = raw[:-1]
        if not raw:
            logger.warning("No closed candles remain after drop; skipping")
            return

    # compute HA using persisted open for last closed candle
    ha_list = compute_heikin_ashi(raw, persisted_open=persisted_ha_open)
    last_closed = ha_list[-1]
    prev_closed = ha_list[-2] if len(ha_list) >= 2 else None

    # Log last two candles (raw + HA)
    if prev_closed:
        logger.info("Prev RAW: o/h/l/c = %.8f / %.8f / %.8f / %.8f",
                    prev_closed["raw_open"], prev_closed["raw_high"], prev_closed["raw_low"], prev_closed["raw_close"])
        logger.info("Prev HA : o/h/l/c = %.8f / %.8f / %.8f / %.8f",
                    prev_closed["ha_open"], prev_closed["ha_high"], prev_closed["ha_low"], prev_closed["ha_close"])
    logger.info("Last RAW: o/h/l/c = %.8f / %.8f / %.8f / %.8f",
                last_closed["raw_open"], last_closed["raw_high"], last_closed["raw_low"], last_closed["raw_close"])
    logger.info("Last HA : o/h/l/c = %.8f / %.8f / %.8f / %.8f",
                last_closed["ha_open"], last_closed["ha_high"], last_closed["ha_low"], last_closed["ha_close"])

    # Persist next HA-open (seed next run)
    next_ha_open = (last_closed["ha_open"] + last_closed["ha_close"]) / 2.0
    state["last_ha_open"] = float(next_ha_open)
    save_state(state)
    logger.info("Persisted next_ha_open = %.8f", next_ha_open)

    # evaluate signal using new function (most recent red/green)
    sig = evaluate_signal(ha_list)
    if not sig:
        logger.info("No signal (by HA rules) this cycle")
        return

    # confirmation filter (last 8 candles)
    if len(ha_list) >= 8:
        ok = confirmation_filter(ha_list, sig["signal"])
        if not ok:
            logger.info("Signal %s skipped due to confirmation filter", sig["signal"])
            log_trade(sig["signal"], None, None, None, None, 0.0, None, status="skipped_confirmation")
            return
    else:
        logger.info("Not enough candles for confirmation filter (need 8). Continuing without confirmation.")

    # build SL/TP values from sig
    entry = float(sig["entry"])
    sl_raw = float(sig["sl_raw"])
    tp1_raw = float(sig["tp1_raw"])
    tp2_raw = float(sig["tp2_raw"])

    sl = round_price(sl_raw)
    tp1 = round_price(tp1_raw)
    tp2 = round_price(tp2_raw)

    logger.info("Signal=%s | Entry=%.8f | SL=%.8f (raw=%.8f) | TP1=%.8f TP2=%.8f | per-contract risk=%.8f",
                sig["signal"], entry, sl, sl_raw, tp1, tp2, abs(entry - sl_raw))

    # balance & qty
    try:
        balance = get_balance_usdt()
    except Exception as e:
        logger.exception("Could not fetch balance: %s", e)
        return

    logger.info("Available USDT balance = %.8f", balance)
    qty = compute_qty(entry, sl, balance)
    if qty <= 0:
        logger.warning("Computed qty <= 0; aborting trade placement")
        return

    # enforce minimum for NEW trades (16)
    if qty < MIN_NEW_ORDER_QTY:
        logger.info("Computed qty %.8f < MIN_NEW_ORDER_QTY %.0f -> using minimum for new order", qty, MIN_NEW_ORDER_QTY)
        qty = MIN_NEW_ORDER_QTY

    qty = floor_to_step(qty, QTY_STEP)
    if qty <= 0:
        logger.warning("Final qty after step rounding <= 0; aborting")
        return

    # ensure hedge + set leverage
    ensure_hedge_and_isolated(SYMBOL)
    set_symbol_leverage(SYMBOL, LEVERAGE)

    # check open positions and decide
    open_positions = get_open_positions(SYMBOL)
    side_open = {}
    for p in open_positions:
        try:
            size = float(p.get("size", 0) or 0)
        except Exception:
            size = 0.0
        side = p.get("side") or p.get("positionSide") or ""
        if size > 0:
            side_open[side] = side_open.get(side, 0.0) + size

    # don't duplicate same-side trades
    if sig["signal"] == "Buy":
        if side_open.get("Buy", 0) > 0 or side_open.get("LONG", 0) > 0:
            logger.info("Buy already open -> skipping new buy")
            log_trade("Buy", entry, sl, tp1, tp2, qty, balance, status="skipped_same_side")
            return
    else:
        if side_open.get("Sell", 0) > 0 or side_open.get("SHORT", 0) > 0:
            logger.info("Sell already open -> skipping new sell")
            log_trade("Sell", entry, sl, tp1, tp2, qty, balance, status="skipped_same_side")
            return

    placed = place_market_with_tp_sl(sig["signal"], SYMBOL, qty, sl, tp1, tp2)
    log_trade(sig["signal"], entry, sl, tp1, tp2, qty, balance, status="placed" if placed else "failed")

# ---------------- TEST FUNCTION ----------------
def test_buy_trade():
    logger.info("=== Running test buy trade ===")
    try:
        balance = get_balance_usdt()
    except Exception as e:
        logger.exception("Cannot get balance for test: %s", e)
        return
    logger.info("Balance before test trade: %.8f USDT", balance)

    # Use latest closed candle's raw close if available for realistic entry
    try:
        candles = fetch_candles(SYMBOL, TIMEFRAME, limit=3)
        if candles and (candles[-1]["ts"] + timeframe_ms()) > int(datetime.utcnow().timestamp()*1000) + 1000:
            candles = candles[:-1]
        entry = candles[-1]["close"] if candles else 0.0
    except Exception:
        entry = 0.348  # fallback

    if entry <= 0:
        entry = 0.348

    sl = entry - PIP * 2  # crude test SL
    tp1 = entry + PIP * 1
    tp2 = entry + PIP * 2
    qty = max(16, floor_to_step(16, QTY_STEP))

    ensure_hedge_and_isolated(SYMBOL)
    set_symbol_leverage(SYMBOL, LEVERAGE)
    placed = place_market_with_tp_sl("Buy", SYMBOL, qty, sl, tp1, tp2)
    log_trade("Buy", entry, sl, tp1, tp2, qty, balance, status="test_placed" if placed else "test_failed")

# ---------------- SCHEDULER ----------------
def wait_until_next_4h():
    now = datetime.utcnow()
    seconds = now.minute * 60 + now.second
    elapsed_hours = now.hour % 4
    to_wait = (4 - elapsed_hours) * 3600 - seconds
    if to_wait <= 0:
        to_wait = 1
    logger.info("Sleeping %d seconds until next 4h UTC block (UTC now=%s)", to_wait, now.strftime("%Y-%m-%d %H:%M:%S"))
    time.sleep(to_wait)

# ---------------- ENTRY ----------------
if __name__ == "__main__":
    logger.info("Starting HA 4h live bot — testnet=%s, symbol=%s", TESTNET, SYMBOL)
    if TEST_MODE:
        test_buy_trade()
    else:
        # align to next 4h boundary before first run
        wait_until_next_4h()
        while True:
            try:
                run_once()
            except Exception:
                logger.exception("run_once failed")
            wait_until_next_4h()
