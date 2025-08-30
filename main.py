#!/usr/bin/env python3
"""
main.py — Bybit TRXUSDT 1H Heikin-Ashi bot (One-way, main account)

What it does (exactly per your spec):
- At the start of every UTC hour, fetch the latest 1H klines and compute Heikin-Ashi.
- Check the last *closed* HA candle:
    * BUY signal if ha_close > ha_open AND ha_low ~== ha_open (within 1 tick)
    * SELL signal if ha_close < ha_open AND ha_high ~== ha_open (within 1 tick)
- If there is NO open position: open Market order immediately with TP & SL ATTACHED:
    * Entry = raw open of the just-opened (current) 1H candle
    * SL = HA open of the just-opened candle
    * TP = 1:1 RR relative to SL + 0.07% buffer (of entry)
    * Position size: risk 10% of equity; fallback to 90% of max affordable if insufficient margin
- If there IS an open position: DO NOT close; just MODIFY the position’s TP & SL to the new signal’s values.
- Always use 75x leverage (set once at start).
- One-way mode (positionIdx=0). Both buy and sell are on the main (UNIFIED) account.
- Siphoning: when equity >= $4 initially, siphon 25% (rounded to nearest whole USDT) to the FUND account, and set next checkpoint to double the *post-siphon* equity. Repeat on each doubling.
- Logging:
    * Every hour: print raw OHLC and HA values used
    * On “new signal detected”
    * On “trade opened successfully” (with entry/SL/TP)
    * On “SL/TP modified” (with old vs new SL/TP)
    * On “siphoned X USDT ...” events
"""

import os
import time
import hmac
import json
import hashlib
import requests
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List

# ========= CONFIG (adjust if needed) =========
API_KEY   = os.getenv("BYBIT_API_KEY")      # required
API_SECRET= os.getenv("BYBIT_API_SECRET")   # required
BASE_URL  = os.getenv("BYBIT_BASE_URL", "https://api.bybit.com")

SYMBOL     = "TRXUSDT"
CATEGORY   = "linear"
LEVERAGE   = 75

# Risk & sizing
RISK_PCT        = Decimal("0.10")   # risk 10% equity
FALLBACK_RATIO  = Decimal("0.90")   # 90% affordability fallback if insufficient

# Instrument precision (TRXUSDT on Bybit)
PRICE_TICK = Decimal("0.00001")
QTY_STEP   = Decimal("1")
MIN_QTY    = Decimal("1")

# TP extras
TP_BUFFER_PCT = Decimal("0.0007")  # +0.07% of entry

# Signal tolerance for "HA open equals high/low"
TICK_TOL = PRICE_TICK

# Siphon settings
SIPHON_START   = Decimal("4")      # start siphoning when equity >= 4
SIPHON_RATE    = Decimal("0.25")   # siphon 25% of equity when threshold hit
SIPHON_FROM    = "UNIFIED"
SIPHON_TO      = "FUND"
SIPHON_COIN    = "USDT"

# ============================================

# ---------- v5 signing helpers ----------
def _ts_ms() -> str:
    return str(int(time.time() * 1000))

def _sign_v5(msg: str) -> str:
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def _headers_for_get(params: Dict[str, Any]) -> Dict[str, str]:
    ts = _ts_ms()
    recv = "5000"
    qs = "&".join([f"{k}={params[k]}" for k in sorted(params)]) if params else ""
    sign_str = ts + API_KEY + recv + qs
    return {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv,
        "X-BAPI-SIGN": _sign_v5(sign_str),
        "X-BAPI-SIGN-TYPE": "2",
        "Content-Type": "application/json",
    }

def _headers_and_body_for_post(body: Dict[str, Any]) -> (Dict[str, str], str):
    ts = _ts_ms()
    recv = "5000"
    body_str = json.dumps(body, separators=(",", ":"))
    sign_str = ts + API_KEY + recv + body_str
    headers = {
        "Content-Type": "application/json",
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv,
        "X-BAPI-SIGN": _sign_v5(sign_str),
        "X-BAPI-SIGN-TYPE": "2",
    }
    return headers, body_str

def _get(path: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
    if params is None: params = {}
    url = BASE_URL + path
    r = requests.get(url, params=params, headers=_headers_for_get(params), timeout=20)
    r.raise_for_status()
    return r.json()

def _post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    url = BASE_URL + path
    headers, body_str = _headers_and_body_for_post(body)
    r = requests.post(url, data=body_str, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()

# ---------- Exchange helpers ----------
def set_leverage_once():
    try:
        _post("/v5/position/set-leverage", {
            "category": CATEGORY,
            "symbol": SYMBOL,
            "buyLeverage": str(LEVERAGE),
            "sellLeverage": str(LEVERAGE),
        })
    except Exception as e:
        print("set_leverage error:", e)

def get_wallet_info(coin: str = "USDT") -> Dict[str, Decimal]:
    """
    Returns {"equity": Decimal, "available": Decimal} best effort.
    """
    try:
        j = _get("/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": coin})
        lst = j.get("result", {}).get("list", [])
        if not lst:
            return {"equity": Decimal("0"), "available": Decimal("0")}
        for c in lst[0].get("coin", []):
            if c.get("coin") == coin:
                equity = Decimal(str(c.get("equity", "0")))
                available = Decimal((str(c.get("availableToWithdraw") or c.get("availableBalance") or c.get("equity") or "0")))
                return {"equity": equity, "available": available}
    except Exception as e:
        print("get_wallet_info error:", e)
    return {"equity": Decimal("0"), "available": Decimal("0")}

def fetch_klines(interval: str = "60", limit: int = 3) -> List[List[Any]]:
    """
    Returns ascending v5 klines rows: [start, open, high, low, close, volume, ...]
    """
    j = _get("/v5/market/kline", {"category": CATEGORY, "symbol": SYMBOL, "interval": interval, "limit": limit})
    rows = j.get("result", {}).get("list", [])
    rows.sort(key=lambda r: int(r[0]))
    return rows

def get_open_position() -> Dict[str, Any]:
    """
    Returns first position dict or {} if none.
    """
    try:
        j = _get("/v5/position/list", {"category": CATEGORY, "symbol": SYMBOL})
        pos_list = j.get("result", {}).get("list", [])
        if pos_list:
            return pos_list[0]
    except Exception as e:
        print("get_open_position error:", e)
    return {}

# ---------- Heikin-Ashi ----------
def compute_heikin_ashi(klines: List[List[Any]]) -> List[Dict[str, Any]]:
    out = []
    for i, r in enumerate(klines):
        start = int(r[0])
        o = Decimal(str(r[1])); h = Decimal(str(r[2])); l = Decimal(str(r[3])); c = Decimal(str(r[4]))
        ha_close = (o + h + l + c) / Decimal("4")
        if i == 0:
            ha_open = (o + c) / Decimal("2")
        else:
            ha_open = (out[i-1]["ha_open"] + out[i-1]["ha_close"]) / Decimal("2")
        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)
        out.append({
            "start": start, "open": o, "high": h, "low": l, "close": c,
            "ha_open": ha_open, "ha_close": ha_close, "ha_high": ha_high, "ha_low": ha_low
        })
    return out

def approx_equal(a: Decimal, b: Decimal, tol: Decimal = TICK_TOL) -> bool:
    return abs(a - b) <= tol

# ---------- Sizing & placing ----------
def round_down_qty(qty: Decimal) -> Decimal:
    steps = (qty / QTY_STEP).to_integral_value(rounding=ROUND_DOWN)
    q = steps * QTY_STEP
    return q if q >= MIN_QTY else Decimal("0")

def quantize_price(p: Decimal) -> Decimal:
    # snap to tick
    steps = (p / PRICE_TICK).to_integral_value(rounding=ROUND_DOWN)
    return steps * PRICE_TICK

def compute_qty(entry: Decimal, sl: Decimal, equity: Decimal, avail: Decimal) -> Decimal:
    risk = abs(entry - sl)
    if risk <= 0:
        return Decimal("0")
    risk_amt = equity * RISK_PCT
    qty_by_risk = (risk_amt / risk)  # units of TRX
    qty_max = (avail * Decimal(LEVERAGE)) / entry
    qty = qty_by_risk if qty_by_risk <= qty_max else (qty_max * FALLBACK_RATIO)
    qty = qty.quantize(QTY_STEP, rounding=ROUND_DOWN)
    return round_down_qty(qty)

def place_market_with_tpsl(side: str, qty: Decimal, tp: Decimal, sl: Decimal) -> Dict[str, Any]:
    """
    Place Market order with TP/SL attached (v5 /order/create).
    one-way mode (positionIdx=0).
    """
    body = {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "side": side,                   # "Buy" or "Sell"
        "orderType": "Market",
        "qty": str(qty),
        "timeInForce": "IOC",
        "reduceOnly": False,
        "positionIdx": 0,
        "takeProfit": str(tp),
        "stopLoss": str(sl),
    }
    return _post("/v5/order/create", body)

def modify_position_tpsl(tp: Decimal, sl: Decimal) -> Dict[str, Any]:
    """
    Modify the position's TP/SL (v5 /position/trading-stop).
    """
    body = {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "takeProfit": str(tp),
        "stopLoss": str(sl),
        "positionIdx": 0
    }
    return _post("/v5/position/trading-stop", body)

# ---------- Siphoning ----------
def siphon_if_needed(state: Dict[str, Any]):
    """
    Siphon 25% to FUND when equity >= checkpoint.
    After siphoning, set checkpoint = (equity_after - siphon) * 2
    (equivalent to "next time equity doubles from what remains").
    """
    wallet = get_wallet_info(SIPHON_COIN)
    equity = wallet["equity"]

    # initialize checkpoint
    if state.get("checkpoint") is None:
        state["checkpoint"] = SIPHON_START

    if equity >= state["checkpoint"]:
        siphon_amt = (equity * SIPHON_RATE).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        if siphon_amt > 0:
            body = {
                "transferId": str(int(time.time() * 1000)),
                "coin": SIPHON_COIN,
                "amount": str(siphon_amt),
                "fromAccountType": SIPHON_FROM,
                "toAccountType": SIPHON_TO,
            }
            try:
                res = _post("/v5/asset/transfer", body)
                print(f"💸 Siphoned {siphon_amt} {SIPHON_COIN} to {SIPHON_TO}. Response: {res.get('retMsg','')}")
            except Exception as e:
                print("siphon transfer error:", e)

            # After siphon, recompute new checkpoint based on the remaining equity
            # (we don't know instant new equity; approximate: equity_after ≈ equity - siphon_amt)
            remaining = equity - siphon_amt
            if remaining < 0:
                remaining = Decimal("0")
            state["checkpoint"] = (remaining * Decimal("2")).quantize(Decimal("0.01"))

# ---------- Main loop ----------
def run():
    if not API_KEY or not API_SECRET:
        print("Set BYBIT_API_KEY and BYBIT_API_SECRET environment variables.")
        return

    # set leverage once
    set_leverage_once()

    # siphon checkpoint state
    state = {"checkpoint": None}

    # ensure we run exactly once per top-of-hour
    last_hour_key = None

    while True:
        now = datetime.now(timezone.utc)
        # wait until top-of-hour (second 0..5 grace)
        if not (now.minute == 0 and now.second < 6):
            time.sleep(2)
            continue

        hour_key = now.strftime("%Y-%m-%d %H")
        if hour_key == last_hour_key:
            time.sleep(1)
            continue
        last_hour_key = hour_key

        # tiny pause to ensure kline is finalized
        time.sleep(2)

        try:
            # 1) fetch klines, compute HA
            kl = fetch_klines("60", 3)
            if len(kl) < 3:
                time.sleep(2)
                continue

            ha = compute_heikin_ashi(kl)
            closed_ha = ha[-2]    # last CLOSED HA candle
            current_ha = ha[-1]   # just-opened HA candle
            # raw open of current candle
            entry = Decimal(str(kl[-1][1]))

            # Log raw OHLC + HA values
            print(
                f"🕛 {hour_key}Z | RAW O={Decimal(str(kl[-2][1]))} H={Decimal(str(kl[-2][2]))} "
                f"L={Decimal(str(kl[-2][3]))} C={Decimal(str(kl[-2][4]))}  | "
                f"HA(open/close/high/low)={closed_ha['ha_open']}/{closed_ha['ha_close']}/"
                f"{closed_ha['ha_high']}/{closed_ha['ha_low']}"
            )

            # 2) detect signal (with 1-tick tolerance)
            signal = None
            if closed_ha["ha_close"] > closed_ha["ha_open"] and approx_equal(closed_ha["ha_low"], closed_ha["ha_open"]):
                signal = "Buy"
            elif closed_ha["ha_close"] < closed_ha["ha_open"] and approx_equal(closed_ha["ha_high"], closed_ha["ha_open"]):
                signal = "Sell"

            if signal:
                print(f"🔔 New signal detected: {signal}")

                # 3) compute SL/TP based on current HA open and entry
                sl = current_ha["ha_open"]
                if signal == "Buy":
                    risk = entry - sl
                    if risk <= 0:
                        print("Skip: non-positive risk distance.")
                        siphon_if_needed(state)
                        continue
                    tp = entry + risk + (entry * TP_BUFFER_PCT)
                else:
                    risk = sl - entry
                    if risk <= 0:
                        print("Skip: non-positive risk distance.")
                        siphon_if_needed(state)
                        continue
                    tp = entry - risk - (entry * TP_BUFFER_PCT)

                # snap prices to tick
                sl = quantize_price(sl)
                tp = quantize_price(tp)

                # 4) check if position exists
                pos = get_open_position()
                size = Decimal(str(pos.get("size", "0") or "0"))
                side_now = (pos.get("side") or "").capitalize() if size > 0 else ""
                old_tp = Decimal(str(pos.get("takeProfit") or "0")) if size > 0 else Decimal("0")
                old_sl = Decimal(str(pos.get("stopLoss") or "0")) if size > 0 else Decimal("0")

                if size > 0:
                    # There is an open position -> modify TP/SL only
                    try:
                        res = modify_position_tpsl(tp, sl)
                        print(f"✏️  Modified TP/SL | old TP={old_tp}, old SL={old_sl} -> new TP={tp}, new SL={sl} | resp={res.get('retMsg','')}")
                    except Exception as e:
                        print("modify_position_tpsl error:", e)

                else:
                    # No open position -> open new market order with attached TP/SL
                    wallet = get_wallet_info("USDT")
                    equity = wallet["equity"]
                    avail  = wallet["available"]
                    qty = compute_qty(entry, sl, equity, avail)
                    if qty <= 0:
                        print("Qty=0 after sizing; skip.")
                        siphon_if_needed(state)
                        continue

                    try:
                        res = place_market_with_tpsl(signal, qty, tp, sl)
                        if res.get("retCode") == 0:
                            print(f"✅ Trade opened successfully | side={signal}, qty={qty}, entry≈{entry}, SL={sl}, TP={tp}")
                        else:
                            print("Order create response:", res)
                    except Exception as e:
                        print("place_market_with_tpsl error:", e)

            # 5) Siphon if needed
            siphon_if_needed(state)

        except Exception as e:
            print("Loop error:", e)

        # small sleep so we don’t double-run inside the same minute
        time.sleep(5)

if __name__ == "__main__":
    run()
    
