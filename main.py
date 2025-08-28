#!/usr/bin/env python3
"""
Bybit TRXUSDT 1H Heikin-Ashi bot (One-way, main account)
- Runs at the start of every hour (UTC)
- Closes any open position at hour tick (max life = 1h)
- Checks last CLOSED HA candle for signal:
    * Buy if ha_close > ha_open AND ha_low == ha_open
    * Sell if ha_close < ha_open AND ha_high == ha_open
- If signal: enter immediately at current raw open (current candle open)
  SL = HA open of the candle that just opened
  TP = 1:1 RR + 0.1% of entry
- TP & SL attached to the market order
- Position sizing: risk 10% equity; fallback to 90% affordability
"""

import os
import time
import hmac
import hashlib
import json
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
import requests
from typing import Dict, Any, List

# ---------------- CONFIG ----------------
API_KEY = os.getenv("BYBIT_API_KEY")             # set this
API_SECRET = os.getenv("BYBIT_API_SECRET")       # set this
BASE_URL = os.getenv("BYBIT_BASE_URL", "https://api.bybit.com")

SYMBOL = "TRXUSDT"
CATEGORY = "linear"
LEVERAGE = 75
RISK_PCT = Decimal("0.10")        # 10% risk
FALLBACK_RATIO = Decimal("0.90")  # 90% fallback
MIN_QTY = Decimal("1")            # adjust if your exchange allows fractional
QTY_STEP = Decimal("1")
PRICE_TICK = Decimal("0.00001")   # adjust if necessary
TP_BUFFER_PCT = Decimal("0.001")  # 0.1% (0.001)
TOLERANCE = Decimal("0")         # exact equality per your request

# ---------------- Helpers (Bybit v5 signing) ----------------
def _ts_ms() -> str:
    return str(int(time.time() * 1000))

def _sign(msg: str) -> str:
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
        "X-BAPI-SIGN": _sign(sign_str),
    }

def _headers_for_post(body: Dict[str, Any]) -> Dict[str, str]:
    ts = _ts_ms()
    recv = "5000"
    body_str = json.dumps(body, separators=(",", ":"))
    sign_str = ts + API_KEY + recv + body_str
    hdrs = {
        "Content-Type": "application/json",
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv,
        "X-BAPI-SIGN": _sign(sign_str),
    }
    return hdrs

def _get(path: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
    if params is None:
        params = {}
    headers = _headers_for_get(params)
    url = BASE_URL + path
    r = requests.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()

def _post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    headers = _headers_for_post(body)
    url = BASE_URL + path
    r = requests.post(url, data=json.dumps(body), headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()

# ---------------- Exchange helpers ----------------
def get_wallet_equity(coin: str = "USDT") -> Decimal:
    """Return total equity (USDT) from unified wallet."""
    try:
        j = _get("/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": coin})
        lst = j.get("result", {}).get("list", [])
        if not lst:
            return Decimal("0")
        for entry in lst[0].get("coin", []):
            if entry.get("coin") == coin:
                return Decimal(str(entry.get("equity", "0")))
    except Exception as e:
        print("get_wallet_equity error:", e)
    return Decimal("0")

def fetch_klines(symbol: str = SYMBOL, interval: str = "60", limit: int = 3) -> List[List[Any]]:
    """Return list of kline rows as returned by v5 /market/kline (ascending)."""
    j = _get("/v5/market/kline", {"category": CATEGORY, "symbol": symbol, "interval": interval, "limit": limit})
    rows = j.get("result", {}).get("list", [])
    # rows may already be ascending; ensure ascending by timestamp
    rows.sort(key=lambda r: int(r[0]))
    return rows

def close_open_position():
    """Close any open position on SYMBOL (one-way mode)."""
    try:
        j = _get("/v5/position/list", {"category": CATEGORY, "symbol": SYMBOL})
        pos_list = j.get("result", {}).get("list", [])
        if not pos_list:
            return False
        p = pos_list[0]
        size = Decimal(str(p.get("size", "0")))
        side = p.get("side")
        if size > 0:
            close_side = "Sell" if side == "Buy" else "Buy"
            body = {
                "category": CATEGORY,
                "symbol": SYMBOL,
                "side": close_side,
                "orderType": "Market",
                "qty": str(size),
                "reduceOnly": True,
                "timeInForce": "IOC"
            }
            _post("/v5/order/create", body)
            print("✅ Trade closed successfully")
            return True
    except Exception as e:
        print("close_open_position error:", e)
    return False

# ---------------- Heikin Ashi ----------------
def compute_heikin_ashi(klines: List[List[Any]]) -> List[Dict[str, Any]]:
    """
    klines: list of rows [start, open, high, low, close, volume, ...] ascending
    return list of dicts with ha_open, ha_close, ha_high, ha_low and original open/high/low/close/start
    """
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

# ---------------- Utility math ----------------
def approx_equal(a: Decimal, b: Decimal) -> bool:
    if TOLERANCE == 0:
        return a == b
    return abs(a - b) <= (TOLERANCE * ((a + b) / Decimal("2")))

def round_down_qty(qty: Decimal) -> Decimal:
    steps = (qty / QTY_STEP).to_integral_value(rounding=ROUND_DOWN)
    q = steps * QTY_STEP
    return q if q >= MIN_QTY else Decimal("0")

# ---------------- Order sizing & placing ----------------
def compute_qty(entry: Decimal, sl: Decimal, equity: Decimal, avail: Decimal) -> Decimal:
    # risk per trade in USD
    risk_amt = equity * RISK_PCT
    risk = abs(entry - sl)
    if risk <= 0:
        return Decimal("0")
    qty_by_risk = (risk_amt / risk)  # number of contracts (TRX)
    qty_max_by_balance = (avail * Decimal(LEVERAGE)) / entry
    if qty_by_risk > qty_max_by_balance:
        qty = (qty_max_by_balance * FALLBACK_RATIO).quantize(QTY_STEP, rounding=ROUND_DOWN)
    else:
        qty = qty_by_risk.quantize(QTY_STEP, rounding=ROUND_DOWN)
    return round_down_qty(qty)

def place_market_with_tpsl(side: str, qty: Decimal, entry: Decimal, sl: Decimal, tp: Decimal):
    """
    Place market order with takeProfit and stopLoss attached (v5).
    Uses tpsl fields in the create order body.
    """
    try:
        body = {
            "category": CATEGORY,
            "symbol": SYMBOL,
            "side": side,
            "orderType": "Market",
            "qty": str(qty),
            "timeInForce": "IOC",
            # Attach TP/SL to the order:
            "takeProfit": str(tp),
            "stopLoss": str(sl),
            "reduceOnly": False
        }
        res = _post("/v5/order/create", body)
        # check success
        if res.get("retCode") == 0:
            print("✅ Trade opened successfully")
        else:
            print("Order create response:", res)
        return res
    except Exception as e:
        print("place_market_with_tpsl error:", e)
        return None

# ---------------- Main loop ----------------
def sleep_until_next_hour():
    now = datetime.now(timezone.utc)
    next_hour = (now.replace(minute=0, second=0, microsecond=0) + 
                 timedelta(hours=1))
    to_sleep = (next_hour - now).total_seconds()
    if to_sleep > 0:
        time.sleep(to_sleep)

def run():
    if not API_KEY or not API_SECRET:
        print("Set BYBIT API_KEY and API_SECRET environment variables.")
        return

    last_checked_hour = None
    while True:
        try:
            # wait until start of hour
            now = datetime.now(timezone.utc)
            if now.minute != 0 or now.second != 0:
                # sleep short until exact hour
                # compute next hour boundary
                next_hour = (now.replace(minute=0, second=0, microsecond=0) + 
                             timedelta(hours=1))
                time_to_wait = (next_hour - now).total_seconds()
                time.sleep(min(5, time_to_wait))
                continue

            # ensure we only run once per hour
            current_hour = now.hour
            if current_hour == last_checked_hour:
                time.sleep(1)
                continue
            last_checked_hour = current_hour

            # --- 1) Close any open position (expire max 1h) ---
            close_open_position()

            # --- 2) Fetch last 3 klines and compute HA ---
            klines = fetch_klines(SYMBOL, interval="60", limit=3)
            if len(klines) < 3:
                print("Not enough klines fetched, skipping this hour.")
                time.sleep(2)
                continue
            ha = compute_heikin_ashi(klines)
            # ha[-2] is the last CLOSED HA candle, ha[-1] is the just-opened/current candle
            closed_ha = ha[-2]
            current_ha = ha[-1]
            # raw open of the current candle (entry price)
            entry_price = Decimal(str(klines[-1][1]))

            # --- 3) Check signal on closed HA candle ---
            signal = None
            if closed_ha["ha_close"] > closed_ha["ha_open"] and approx_equal(closed_ha["ha_low"], closed_ha["ha_open"]):
                signal = "Buy"
            elif closed_ha["ha_close"] < closed_ha["ha_open"] and approx_equal(closed_ha["ha_high"], closed_ha["ha_open"]):
                signal = "Sell"

            if not signal:
                # nothing to do this hour
                time.sleep(1)
                continue

            # log detected signal
            print(f"ℹ️ New signal detected: {signal} (closed candle start={datetime.fromtimestamp(closed_ha['start']/1000, tz=timezone.utc).isoformat()})")

            # --- 4) Build SL and TP based on the just-opened candle ---
            sl_price = current_ha["ha_open"]  # SL is HA open of the candle that just opened
            # risk distance
            if signal == "Buy":
                risk_distance = entry_price - sl_price
                tp_price = (entry_price + risk_distance + (entry_price * TP_BUFFER_PCT)).quantize(PRICE_TICK)
            else:
                risk_distance = sl_price - entry_price
                tp_price = (entry_price - risk_distance - (entry_price * TP_BUFFER_PCT)).quantize(PRICE_TICK)

            if risk_distance <= 0:
                print("Skip trade: non-positive risk distance.")
                time.sleep(1)
                continue

            # --- 5) Position sizing ---
            equity = get_wallet_equity("USDT")
            # For margin affordability, check available balance in unified wallet (use equity as proxy)
            # Better would be availableBalance; v5 wallet response structure can be more detailed.
            # We'll fetch availableToWithdraw if present, else use equity.
            # Simpler: treat avail = equity for sizing fallback check
            avail = equity

            qty = compute_qty(entry_price, sl_price, equity, avail)
            if qty <= 0:
                print("Qty computed as zero; skipping trade.")
                time.sleep(1)
                continue

            # --- 6) Place market order with attached TP/SL ---
            place_market_with_tpsl(signal, qty, entry_price, sl_price, tp_price)

            # done for this hour; wait until next hour tick
            time.sleep(1)

        except Exception as e:
            print("Loop error:", e)
            time.sleep(2)

if __name__ == "__main__":
    # small local imports used in run
    from datetime import timedelta
    run()
