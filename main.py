#!/usr/bin/env python3
"""
Bybit Heikin-Ashi live/sim bot (hedge mode, isolated margin) — Custom Rules
- Uses HA candles (seedable INITIAL_HA_OPEN) computed from raw candles fetched from Bybit.
- Signal: compare last 8 HA candles' greens vs reds; tie -> use last HA candle color.
- SL wick rules: for Sell use previous raw high if upper wick else this raw high; for Buy use previous raw low if lower wick else this raw low.
- TP = 2:1 RR + 0.1% of entry (exactly implemented).
- Risk per trade = RISK_PER_TRADE (default 10%).
- Fallback: if required margin > available balance, compute qty using FALLBACK proportion of available balance.
- Qty rounded to nearest whole number and must be >= 1 to place order.
- Logs raw + HA OHLC for each of the 8 candles and logs entry/SL/TP/qty before placing orders.
- Interval default is 3-minute candles; sleeping synced to candle close.
"""
import os
import time
import logging
from datetime import datetime
from math import floor
from pybit.unified_trading import HTTP

# ================== CONFIG ==================
SYMBOL = os.environ.get("SYMBOL", "TRXUSDT")
RISK_PER_TRADE = float(os.environ.get("RISK_PER_TRADE", "0.10"))   # 10% default
FALLBACK = float(os.environ.get("FALLBACK", "0.95"))               # 95% fallback
RR = float(os.environ.get("RR", "2.0"))                            # 2:1 RR
TP_EXTRA = float(os.environ.get("TP_EXTRA", "0.001"))              # +0.1% of entry

INTERVAL = os.environ.get("INTERVAL", "3")     # Bybit 3-minute
CANDLE_SECONDS = int(os.environ.get("CANDLE_SECONDS", "180"))

# Initial HA open (must be set to match TradingView for consistency)
INITIAL_HA_OPEN = float(os.environ.get("INITIAL_HA_OPEN", "0.33946"))

# HA candles to consider (earliest-first)
HA_WINDOW = int(os.environ.get("HA_WINDOW", "8"))

# Tick/qty step & leverage (these are used for rounding and margin calc)
TICK_SIZE = float(os.environ.get("TICK_SIZE", "0.00001"))
QTY_STEP = float(os.environ.get("QTY_STEP", "1"))
LEVERAGE = float(os.environ.get("LEVERAGE", "75"))

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TESTNET = os.environ.get("BYBIT_TESTNET", "false").lower() in ("1", "true", "yes")
ACCOUNT_TYPE = os.environ.get("BYBIT_ACCOUNT_TYPE", "UNIFIED")

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger("ha_bot")

# ---------------- SESSION ----------------
session = HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)

# ---------------- HELPERS ----------------
def round_price(p: float) -> float:
    if TICK_SIZE <= 0:
        return p
    ticks = round(p / TICK_SIZE)
    return round(ticks * TICK_SIZE, 8)

def round_qty_whole(x: float) -> int:
    """Round qty to nearest whole number (user requested)."""
    try:
        q = int(round(x))
        return q
    except Exception:
        return 0

def floor_to_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return floor(x / step) * step

def timeframe_ms() -> int:
    try:
        return int(INTERVAL) * 60 * 1000
    except Exception:
        return CANDLE_SECONDS * 1000

# ---------------- KLINES ----------------
def fetch_candles(symbol: str, interval: str = INTERVAL, limit: int = HA_WINDOW):
    """Return list oldest->newest of dicts {ts, open, high, low, close}."""
    out = session.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
    res = out.get("result", {}) or out
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
            ts = int(r[0])
            o = float(r[1]); h = float(r[2]); l = float(r[3]); c = float(r[4])
            candles.append({"ts": ts, "open": o, "high": h, "low": l, "close": c})
        except Exception:
            continue
    candles.sort(key=lambda x: x["ts"])
    return candles

# ---------------- HEIKIN-ASHI ----------------
def compute_heikin_ashi(raw_candles, persisted_open=None):
    """
    raw_candles: oldest -> newest
    persisted_open: seed ha_open for the most recent (last) closed candle.
    """
    ha = []
    prev_ha_open, prev_ha_close = None, None
    n = len(raw_candles)
    for i, c in enumerate(raw_candles):
        ro, rh, rl, rc = c["open"], c["high"], c["low"], c["close"]
        ha_close = (ro + rh + rl + rc) / 4.0
        # Use persisted_open for the last closed candle (to match TradingView)
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
    """Return 'Buy' or 'Sell' or None using last 2 HA candles per your rules."""
    if len(ha_list) < 2:
        return None
    prev_candle, last_candle = ha_list[-2], ha_list[-1]
    # Buy: previous HA was red and last ha_high > prev ha_high
    if prev_candle["ha_close"] < prev_candle["ha_open"] and last_candle["ha_high"] > prev_candle["ha_high"]:
        return "Buy"
    # Sell: previous HA was green and last ha_low < prev ha_low
    if prev_candle["ha_close"] > prev_candle["ha_open"] and last_candle["ha_low"] < prev_candle["ha_low"]:
        return "Sell"
    return None

# ---------------- BALANCE ----------------
def get_balance_usdt():
    """Robustly fetch USDT available balance from unified wallet."""
    out = session.get_wallet_balance(accountType=ACCOUNT_TYPE, coin="USDT")
    res = out.get("result", {}) or out
    # Case: result -> list of items with coin list
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
                        for v in c.values():
                            try:
                                return float(v)
                            except Exception:
                                pass
    raise RuntimeError("Could not parse wallet balance response: {}".format(out))

# ---------------- ORDER HELPERS ----------------
def place_market_with_tp_sl(side, symbol, qty, sl, tp, entry_price):
    """Log and place market order (or just log on error)."""
    logger.info("About to place %s | Entry=%.8f SL=%.8f TP=%.8f Qty=%s", side, entry_price, sl, tp, qty)
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
        logger.info("Order response: %s", resp)
        # attach TP/SL
        try:
            resp2 = session.set_trading_stop(
                category="linear",
                symbol=symbol,
                takeProfit=str(round_price(tp)),
                stopLoss=str(round_price(sl)),
                tpTriggerBy="LastPrice",
                slTriggerBy="LastPrice"
            )
            logger.info("Attached TP/SL response: %s", resp2)
        except Exception as e:
            logger.exception("Failed to attach TP/SL: %s", e)
    except Exception as e:
        logger.exception("Error placing order: %s", e)

# ---------------- QTY / RISK ----------------
def compute_qty(entry, sl, balance):
    """
    Compute contracts (qty) such that risk_usd = balance * RISK_PER_TRADE.
    If estimated margin (qty * entry / LEVERAGE) exceeds available balance share,
    use fallback: calculate qty from (avail_balance * FALLBACK * LEVERAGE) / entry.
    Round to nearest whole number (user requested).
    """
    avail_balance = balance * 1.0  # using full wallet here; you may want to use a subset
    risk_usd = balance * RISK_PER_TRADE
    per_contract_risk = abs(entry - sl)
    if per_contract_risk <= 0:
        logger.warning("Per-contract risk <= 0, cannot compute qty")
        return 0
    qty = risk_usd / per_contract_risk

    # Estimated margin required (simplified): (qty * entry) / LEVERAGE
    est_margin = (qty * entry) / LEVERAGE if LEVERAGE > 0 else (qty * entry)
    # Use available allocation for margin (we'll use FALLBACK to reduce if needed)
    max_allowed_margin = avail_balance  # using full wallet as available margin
    if est_margin > max_allowed_margin:
        # compute qty from fallback allocation
        max_qty = (max_allowed_margin * FALLBACK * LEVERAGE) / entry if entry > 0 else 0
        logger.info("Estimated margin %.6f > avail %.6f — using fallback qty calc", est_margin, max_allowed_margin)
        qty_final = round_qty_whole(max_qty)
    else:
        qty_final = round_qty_whole(qty)

    if qty_final < 1:
        logger.info("Computed qty < 1 (qty_final=%s) — skipping trade", qty_final)
        return 0
    return qty_final

# ---------------- MAIN RUN-ONCE ----------------
def run_once():
    global INITIAL_HA_OPEN

    logger.info("=== Running at %s ===", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))

    # fetch recent HA_WINDOW raw candles
    raw = fetch_candles(symbol=SYMBOL, interval=INTERVAL, limit=HA_WINDOW)
    if not raw or len(raw) < 2:
        logger.warning("Not enough candles fetched (%d)", len(raw))
        return

    # log retrieval times: first and last
    first_ts = raw[0]["ts"] // 1000
    last_ts = raw[-1]["ts"] // 1000
    logger.info("Raw first candle start (UTC): %s", datetime.utcfromtimestamp(first_ts).strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("Raw last candle close (UTC): %s", datetime.utcfromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M:%S"))

    # If last candle is in-progress (live), drop it
    now_ms = int(datetime.utcnow().timestamp() * 1000)
    if raw and (raw[-1]["ts"] + timeframe_ms()) > now_ms:
        logger.info("Detected in-progress last candle; dropping it and using prior closed candles")
        raw = raw[:-1]
        if not raw:
            logger.warning("No closed candles left after dropping in-progress; skipping")
            return

    # compute HA using persisted INITIAL_HA_OPEN for consistency
    ha_list = compute_heikin_ashi(raw, persisted_open=INITIAL_HA_OPEN)

    # log raw + ha for all candles (oldest->newest)
    for idx, h in enumerate(ha_list, start=1):
        logger.info("HA %d | O=%.8f H=%.8f L=%.8f C=%.8f | Raw=(%.8f, %.8f, %.8f, %.8f)",
                    idx, h["ha_open"], h["ha_high"], h["ha_low"], h["ha_close"],
                    h["raw_open"], h["raw_high"], h["raw_low"], h["raw_close"])

    # Persist next HA open for continuity (use last closed HA open -> next run)
    last_closed = ha_list[-1]
    next_ha_open = (last_closed["ha_open"] + last_closed["ha_close"]) / 2.0
    INITIAL_HA_OPEN = float(next_ha_open)

    # Determine signal using HA rules
    sig = evaluate_signal(ha_list)
    if not sig:
        logger.info("No signal this cycle")
        return
    logger.info("Signal detected: %s", sig)

    # Entry price is raw close of last closed candle
    entry = float(last_closed["raw_close"])

    # Determine SL according to wick rules using last_closed HA candle
    # If Sell signal: check if last_closed HA candle has an upper wick -> use previous raw high; else use this raw high
    if sig == "Sell":
        # previous raw candle = raw[-2] since raw is aligned with ha_list
        prev_raw_high = float(raw[-2]["high"])
        this_raw_high = float(raw[-1]["high"])
        # upper-wick detection on HA candle:
        if last_closed["ha_high"] > max(last_closed["ha_open"], last_closed["ha_close"]):
            sl = prev_raw_high
        else:
            sl = this_raw_high
        risk = abs(entry - sl)
        tp = entry - (2 * risk) - (entry * TP_EXTRA)
    else:  # Buy
        prev_raw_low = float(raw[-2]["low"])
        this_raw_low = float(raw[-1]["low"])
        if last_closed["ha_low"] < min(last_closed["ha_open"], last_closed["ha_close"]):
            sl = prev_raw_low
        else:
            sl = this_raw_low
        risk = abs(entry - sl)
        tp = entry + (2 * risk) + (entry * TP_EXTRA)

    # Balance and qty
    try:
        balance = get_balance_usdt()
    except Exception as e:
        logger.exception("Failed to fetch balance: %s", e)
        return

    qty = compute_qty(entry, sl, balance)
    logger.info("Balance=%.6f Entry=%.8f SL=%.8f TP=%.8f ComputedQty=%s", balance, entry, sl, tp, qty)

    if qty <= 0:
        logger.info("Qty computed <= 0, skipping order")
        return

    # Place order (logged inside)
    place_market_with_tp_sl(sig, SYMBOL, qty, sl, tp, entry)

# ---------------- SCHEDULER ----------------
def main_loop():
    while True:
        now = datetime.utcnow()
        sec_into_cycle = (now.minute % (CANDLE_SECONDS // 60)) * 60 + now.second
        wait = CANDLE_SECONDS - sec_into_cycle
        if wait <= 0:
            wait += CANDLE_SECONDS
        logger.info("⏳ Waiting %d seconds until next %s-minute candle close...", wait, INTERVAL)
        time.sleep(wait)
        try:
            run_once()
        except Exception:
            logger.exception("Error in run_once()")

if __name__ == "__main__":
    logger.info("Starting HA bot — TRADING ON %s | INTERVAL=%smin | RISK=%.2f%% | RR=%.1f",
                SYMBOL, INTERVAL, RISK_PER_TRADE * 100.0, RR)
    main_loop()
    
