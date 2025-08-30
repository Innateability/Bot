#!/usr/bin/env python3
"""
main.py — Bybit TRXUSDT 1H Heikin-Ashi bot (One-way, main account)

See comments in conversation for behavior summary.
"""

import os
import time
import hmac
import json
import hashlib
import requests
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple

# --------------------- CONFIG ---------------------
API_KEY    = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
BASE_URL   = os.getenv("BYBIT_BASE_URL", "https://api.bybit.com")

SYMBOL     = "TRXUSDT"
CATEGORY   = "linear"
LEVERAGE   = 75

RISK_PCT       = Decimal("0.10")     # 10% equity risk
FALLBACK_RATIO = Decimal("0.90")     # 90% affordability fallback

PRICE_TICK = Decimal("0.00001")      # TRXUSDT tick (adjust if needed)
QTY_STEP   = Decimal("1")            # TRX step
MIN_QTY    = Decimal("1")

TP_BUFFER_PCT = Decimal("0.0007")    # +0.07% buffer

TICK_TOL = PRICE_TICK                # 1-tick tolerance for equality

# Siphon config
SIPHON_START = Decimal("4")
SIPHON_RATE  = Decimal("0.25")
SIPHON_FROM  = "UNIFIED"
SIPHON_TO    = "FUND"
SIPHON_COIN  = "USDT"

# KLINE fetch
KLINE_INTERVAL = "60"
KLINE_LIMIT = 3

# ----------------- Helpers (Bybit v5 signing) -----------------
def _ts_ms() -> str:
    return str(int(time.time() * 1000))

def _sign_v5(msg: str) -> str:
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

def _headers_for_get(params: Dict[str, Any]) -> Dict[str, str]:
    ts = _ts_ms()
    recv = "5000"
    qs = "&".join([f"{k}={params[k]}" for k in sorted(params)]) if params else ""
    sign_str = ts + (API_KEY or "") + recv + qs
    return {
        "X-BAPI-API-KEY": API_KEY or "",
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv,
        "X-BAPI-SIGN": _sign_v5(sign_str),
        "X-BAPI-SIGN-TYPE": "2",
        "Content-Type": "application/json",
    }

def _headers_and_body_for_post(body: Dict[str, Any]) -> Tuple[Dict[str, str], str]:
    ts = _ts_ms()
    recv = "5000"
    body_str = json.dumps(body, separators=(",", ":"))
    sign_str = ts + (API_KEY or "") + recv + body_str
    headers = {
        "Content-Type": "application/json",
        "X-BAPI-API-KEY": API_KEY or "",
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv,
        "X-BAPI-SIGN": _sign_v5(sign_str),
        "X-BAPI-SIGN-TYPE": "2",
    }
    return headers, body_str

def _get(path: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
    if params is None:
        params = {}
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

# ----------------- Exchange helpers -----------------
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
    try:
        j = _get("/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": coin})
        lst = j.get("result", {}).get("list", [])
        if not lst:
            return {"equity": Decimal("0"), "available": Decimal("0")}
        for c in lst[0].get("coin", []):
            if c.get("coin") == coin:
                equity = Decimal(str(c.get("equity", "0")))
                available = Decimal(str(c.get("availableToWithdraw") or c.get("availableBalance") or c.get("equity") or "0"))
                return {"equity": equity, "available": available}
    except Exception as e:
        print("get_wallet_info error:", e)
    return {"equity": Decimal("0"), "available": Decimal("0")}

def fetch_klines(interval: str = KLINE_INTERVAL, limit: int = KLINE_LIMIT) -> List[List[Any]]:
    j = _get("/v5/market/kline", {"category": CATEGORY, "symbol": SYMBOL, "interval": interval, "limit": limit})
    rows = j.get("result", {}).get("list", [])
    rows.sort(key=lambda r: int(r[0]))
    return rows

def get_open_position() -> Dict[str, Any]:
    try:
        j = _get("/v5/position/list", {"category": CATEGORY, "symbol": SYMBOL})
        pos_list = j.get("result", {}).get("list", [])
        if pos_list:
            return pos_list[0]
    except Exception as e:
        print("get_open_position error:", e)
    return {}

# ----------------- Heikin-Ashi -----------------
def compute_heikin_ashi(klines: List[List[Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
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
            "start": start,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "ha_open": ha_open,
            "ha_close": ha_close,
            "ha_high": ha_high,
            "ha_low": ha_low
        })
    return out

def approx_equal(a: Decimal, b: Decimal, tol: Decimal = TICK_TOL) -> bool:
    return abs(a - b) <= tol

# ----------------- Sizing & order helpers -----------------
def round_down_qty(qty: Decimal) -> Decimal:
    steps = (qty / QTY_STEP).to_integral_value(rounding=ROUND_DOWN)
    q = steps * QTY_STEP
    return q if q >= MIN_QTY else Decimal("0")

def quantize_price(p: Decimal) -> Decimal:
    steps = (p / PRICE_TICK).to_integral_value(rounding=ROUND_DOWN)
    return steps * PRICE_TICK

def compute_qty(entry: Decimal, sl: Decimal, equity: Decimal, avail: Decimal) -> Decimal:
    risk = abs(entry - sl)
    if risk <= 0:
        return Decimal("0")
    risk_amt = equity * RISK_PCT
    qty_by_risk = (risk_amt / risk)  # units
    qty_max = (avail * Decimal(LEVERAGE)) / entry
    qty = qty_by_risk if qty_by_risk <= qty_max else (qty_max * FALLBACK_RATIO)
    q = round_down_qty(qty)
    if q == Decimal("0") and qty > Decimal("0"):
        # ensure we at least try min qty instead of silently skipping
        return MIN_QTY
    return q

def place_market_with_tpsl(side: str, qty: Decimal, tp: Decimal, sl: Decimal) -> Dict[str, Any]:
    body = {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "side": side,
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
    body = {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "takeProfit": str(tp),
        "stopLoss": str(sl),
        "positionIdx": 0
    }
    return _post("/v5/position/trading-stop", body)

# ----------------- Siphoning -----------------
def siphon_if_needed(state: Dict[str, Any]):
    wallet = get_wallet_info(SIPHON_COIN)
    equity = wallet["equity"]

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
            remaining = equity - siphon_amt
            if remaining < 0:
                remaining = Decimal("0")
            state["checkpoint"] = (remaining * Decimal("2")).quantize(Decimal("0.01"))

# ----------------- Initial OHLC helpers -----------------
def load_initial_ohlc_from_file(file_path: str = "initial_ohlc.json") -> Optional[List[str]]:
    if os.path.exists(file_path):
        try:
            with open(file_path, "r") as f:
                data = json.load(f)
            # accept numeric or string values; return string forms
            return [str(data["open"]), str(data["high"]), str(data["low"]), str(data["close"])]
        except Exception as e:
            print("Failed to load initial_ohlc.json:", e)
            return None
    return None

def prompt_manual_current_ohlc() -> Optional[List[str]]:
    try:
        s = input("Optional: paste current forming candle RAW OHLC (open high low close) or press Enter to skip: ").strip()
        if not s:
            return None
        parts = s.split()
        if len(parts) != 4:
            print("Expected 4 numbers. Skipping manual input.")
            return None
        return parts
    except Exception:
        return None

# ----------------- Main loop -----------------
def run():
    if not API_KEY or not API_SECRET:
        print("Set BYBIT_API_KEY and BYBIT_API_SECRET environment variables.")
        return

    set_leverage_once()
    state: Dict[str, Any] = {"checkpoint": None}
    last_hour_key = None

    # try to load initial OHLC from file, else prompt interactive (file preference)
    manual_current = load_initial_ohlc_from_file("initial_ohlc.json")
    if manual_current:
        print("Loaded initial OHLC from initial_ohlc.json:", manual_current)
    else:
        manual_current = prompt_manual_current_ohlc()
        if manual_current:
            print("Using interactive initial OHLC seed:", manual_current)

    print("Starting bot. Will operate at UTC top-of-hour. Press Ctrl+C to stop.")
    while True:
        now = datetime.now(timezone.utc)
        # wait until top-of-hour (small grace)
        if not (now.minute == 0 and now.second < 6):
            time.sleep(2)
            continue

        hour_key = now.strftime("%Y-%m-%d %H")
        if hour_key == last_hour_key:
            time.sleep(1)
            continue
        last_hour_key = hour_key

        # small pause to ensure exchange finalized the candle
        time.sleep(2)

        try:
            kl = fetch_klines("60", 3)  # ascending: prev_closed, closed, current-forming
            if len(kl) < 3:
                print("Not enough klines; skipping this hour.")
                time.sleep(2)
                continue

            # if manual_current provided, replace last kline's OHLC (only once)
            if manual_current:
                try:
                    kl[-1][1] = manual_current[0]
                    kl[-1][2] = manual_current[1]
                    kl[-1][3] = manual_current[2]
                    kl[-1][4] = manual_current[3]
                    manual_current = None  # use only once
                except Exception as e:
                    print("Failed to apply manual current OHLC:", e)

            ha = compute_heikin_ashi(kl)
            closed_ha = ha[-2]   # last CLOSED HA candle (we check this candle for signal)
            current_ha = ha[-1]  # just-opened HA candle
            # raw open of current candle (entry price)
            entry = Decimal(str(kl[-1][1]))

            # Log raw OHLC (the closed raw candle) and HA
            raw_prev = kl[-2]
            raw_o = Decimal(str(raw_prev[1])); raw_h = Decimal(str(raw_prev[2])); raw_l = Decimal(str(raw_prev[3])); raw_c = Decimal(str(raw_prev[4]))
            color = "GREEN" if closed_ha['ha_close'] > closed_ha['ha_open'] else "RED"
            print(f"\n🕛 {hour_key}Z | RAW(prev) O={raw_o} H={raw_h} L={raw_l} C={raw_c} | "
                  f"HA(prev) open={closed_ha['ha_open']} close={closed_ha['ha_close']} high={closed_ha['ha_high']} low={closed_ha['ha_low']} | {color}")

            # detect signal (1-tick tolerance)
            signal = None
            if closed_ha["ha_close"] > closed_ha["ha_open"] and approx_equal(closed_ha["ha_low"], closed_ha["ha_open"]):
                signal = "Buy"
            elif closed_ha["ha_close"] < closed_ha["ha_open"] and approx_equal(closed_ha["ha_high"], closed_ha["ha_open"]):
                signal = "Sell"

            if signal:
                print(f"🔔 New signal detected: {signal}")

                sl = current_ha["ha_open"]
                if signal == "Buy":
                    risk_dist = entry - sl
                    if risk_dist <= 0:
                        print("Skip trade: non-positive risk distance.")
                        siphon_if_needed(state)
                        continue
                    tp = entry + risk_dist + (entry * TP_BUFFER_PCT)
                else: # Sell
                    risk_dist = sl - entry
                    if risk_dist <= 0:
                        print("Skip trade: non-positive risk distance.")
                        siphon_if_needed(state)
                        continue
                    tp = entry - risk_dist - (entry * TP_BUFFER_PCT)

                # quantize prices
                sl_q = quantize_price(sl)
                tp_q = quantize_price(tp)

                # check existing position
                pos = get_open_position()
                pos_size = Decimal(str(pos.get("size", "0") or "0"))
                old_tp = Decimal(str(pos.get("takeProfit") or "0")) if pos_size > 0 else Decimal("0")
                old_sl = Decimal(str(pos.get("stopLoss") or "0")) if pos_size > 0 else Decimal("0")

                if pos_size > 0:
                    # modify only: update TP/SL to new values and log old→new
                    try:
                        res = modify_position_tpsl(tp_q, sl_q)
                        print(f"✏️ Modified TP/SL | old TP={old_tp}, old SL={old_sl} -> new TP={tp_q}, new SL={sl_q} | resp={res.get('retMsg','')}")
                    except Exception as e:
                        print("modify_position_tpsl error:", e)
                else:
                    # compute size & open
                    wallet = get_wallet_info("USDT")
                    equity = wallet["equity"]
                    avail = wallet["available"]
                    qty = compute_qty(entry, sl_q, equity, avail)
                    if qty <= 0:
                        print("Qty computed 0; skipping open.")
                        siphon_if_needed(state)
                        continue

                    try:
                        res = place_market_with_tpsl(signal, qty, tp_q, sl_q)
                        # Bybit v5 sometimes returns retCode or ret_code; check both
                        ok = isinstance(res, dict) and (res.get("retCode") == 0 or res.get("ret_code") == 0 or res.get("ret_code") is None and res.get("retMsg") is None)
                        if ok:
                            print(f"✅ Trade opened successfully | side={signal}, qty={qty}, entry≈{entry}, SL={sl_q}, TP={tp_q}")
                        else:
                            print("Order create response:", res)
                    except Exception as e:
                        print("place_market_with_tpsl error:", e)

            else:
                # no signal this hour: do nothing to trades per spec
                pass

            # siphon after trading actions
            siphon_if_needed(state)

        except Exception as e:
            print("Loop error:", e)

        # small sleep to avoid double-run in the same minute
        time.sleep(5)

if __name__ == "__main__":
    run()
   
