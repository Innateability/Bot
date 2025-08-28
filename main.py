#!/usr/bin/env python3
"""
Bybit TRXUSDT 1H Heikin-Ashi bot (One-way mode)

Features:
- Runs immediate check at startup and then at each UTC hour boundary
- Closes any open position at the hour tick (max life 1h)
- Uses last CLOSED HA candle for signal:
    Buy if ha_close > ha_open AND ha_low ≈ ha_open
    Sell if ha_close < ha_open AND ha_high ≈ ha_open
  (uses 1 tick tolerance)
- Entry = raw open of the current candle, SL = HA open of the current candle,
  TP = 1:1 risk + 0.1% buffer
- Position sizing: risk 10% equity; fallback to 90% affordability
- Places Market order with takeProfit & stopLoss attached (Bybit v5)
- Logs: New signal detected, Trade opened successfully, Trade closed successfully, errors
- Trade tracking CSV (trades.csv)
- Daily email summary (optional) at UTC 23:59 using SMTP env vars
"""

import os
import time
import hmac
import hashlib
import json
import csv
import smtplib
from email.message import EmailMessage
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone, timedelta
from typing import List, Any, Dict
import requests

# ---------------- CONFIG (tweakable) ----------------
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
BASE_URL = os.getenv("BYBIT_BASE_URL", "https://api.bybit.com")  # set testnet if needed

SYMBOL = os.getenv("SYMBOL", "TRXUSDT")
CATEGORY = "linear"
LEVERAGE = int(os.getenv("LEVERAGE", "75"))

RISK_PCT = Decimal(os.getenv("RISK_PCT", "0.10"))     # 10% risk
FALLBACK_RATIO = Decimal(os.getenv("FALLBACK_RATIO", "0.90"))
TP_BUFFER_PCT = Decimal(os.getenv("TP_BUFFER_PCT", "0.001"))  # 0.1%
PRICE_TICK = Decimal(os.getenv("PRICE_TICK", "0.00001"))
QTY_STEP = Decimal(os.getenv("QTY_STEP", "1"))
MIN_QTY = Decimal(os.getenv("MIN_QTY", "1"))
TICK_TOLERANCE = PRICE_TICK   # treat equality within one tick

TRADE_CSV = os.getenv("TRADE_CSV", "trades.csv")

# Email settings (optional)
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587")) if os.getenv("SMTP_PORT") else None
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
EMAIL_TO = os.getenv("EMAIL_TO")

# ---------------- utils: signing & requests ----------------
def _ts_ms() -> str:
    return str(int(time.time() * 1000))

def _sign_hmac(msg: str) -> str:
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
        "X-BAPI-SIGN": _sign_hmac(sign_str),
        "X-BAPI-SIGN-TYPE": "2",
        "Content-Type": "application/json"
    }

def _headers_and_body_for_post(body: Dict[str, Any]) -> (Dict[str, str], str):
    ts = _ts_ms()
    recv = "5000"
    # must use separators to generate exact body string signed and sent
    body_str = json.dumps(body, separators=(",", ":"))
    sign_str = ts + API_KEY + recv + body_str
    headers = {
        "Content-Type": "application/json",
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv,
        "X-BAPI-SIGN": _sign_hmac(sign_str),
        "X-BAPI-SIGN-TYPE": "2",
    }
    return headers, body_str

def _get(path: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
    if params is None:
        params = {}
    headers = _headers_for_get(params)
    url = BASE_URL + path
    r = requests.get(url, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def _post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    headers, body_str = _headers_and_body_for_post(body)
    url = BASE_URL + path
    r = requests.post(url, headers=headers, data=body_str, timeout=20)
    r.raise_for_status()
    return r.json()

# ---------------- exchange helpers ----------------
def set_leverage():
    try:
        _post("/v5/position/set-leverage", {"category": CATEGORY, "symbol": SYMBOL,
                                            "buyLeverage": str(LEVERAGE), "sellLeverage": str(LEVERAGE)})
    except Exception as e:
        print("set_leverage error:", e)

def get_wallet_info(coin: str = "USDT") -> Dict[str, Decimal]:
    """Return dict with equity and available (best effort)."""
    try:
        j = _get("/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": coin})
        lst = j.get("result", {}).get("list", [])
        if not lst:
            return {"equity": Decimal("0"), "available": Decimal("0")}
        # in v5 the structure is list[0].coin -> list of coin dicts
        coin_list = lst[0].get("coin", [])
        for c in coin_list:
            if c.get("coin") == coin:
                equity = Decimal(str(c.get("equity", "0")))
                # attempt to get an available-like field
                available = Decimal(str(c.get("availableToWithdraw") or c.get("availableBalance") or c.get("equity") or "0"))
                return {"equity": equity, "available": available}
    except Exception as e:
        print("get_wallet_info error:", e)
    return {"equity": Decimal("0"), "available": Decimal("0")}

def fetch_klines(interval: str = "60", limit: int = 3) -> List[List[Any]]:
    j = _get("/v5/market/kline", {"category": CATEGORY, "symbol": SYMBOL, "interval": interval, "limit": limit})
    rows = j.get("result", {}).get("list", [])
    rows.sort(key=lambda r: int(r[0]))
    return rows

def close_open_position() -> bool:
    """Close any open position on the symbol. Returns True if closed."""
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
            body = {"category": CATEGORY, "symbol": SYMBOL, "side": close_side,
                    "orderType": "Market", "qty": str(size), "reduceOnly": True, "timeInForce": "IOC"}
            _post("/v5/order/create", body)
            print("✅ Trade closed successfully")
            return True
    except Exception as e:
        print("close_open_position error:", e)
    return False

# ---------------- Heikin Ashi ----------------
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
        out.append({"start": start, "open": o, "high": h, "low": l, "close": c,
                    "ha_open": ha_open, "ha_close": ha_close, "ha_high": ha_high, "ha_low": ha_low})
    return out

# ---------------- helpers math ----------------
def almost_equal(a: Decimal, b: Decimal, tol: Decimal = TICK_TOLERANCE) -> bool:
    return abs(a - b) <= tol

def round_down_qty(qty: Decimal) -> Decimal:
    steps = (qty / QTY_STEP).to_integral_value(rounding=ROUND_DOWN)
    q = steps * QTY_STEP
    return q if q >= MIN_QTY else Decimal("0")

# ---------------- sizing & placing ----------------
def compute_qty(entry: Decimal, sl: Decimal, equity: Decimal, avail: Decimal) -> Decimal:
    risk_amt = equity * RISK_PCT
    risk = abs(entry - sl)
    if risk <= Decimal("0"):
        return Decimal("0")
    qty_by_risk = (risk_amt / risk)  # contracts/units
    qty_max_by_balance = (avail * Decimal(LEVERAGE)) / entry
    qty = qty_by_risk if qty_by_risk <= qty_max_by_balance else (qty_max_by_balance * FALLBACK_RATIO)
    # round down to step, then ensure at least MIN_QTY if nonzero
    q = round_down_qty(qty.quantize(QTY_STEP, rounding=ROUND_DOWN))
    if q == 0 and qty > 0:
        # fallback to minimum to attempt a trade instead of skipping silently
        return MIN_QTY
    return q

def place_market_with_tpsl(side: str, qty: Decimal, entry: Decimal, sl: Decimal, tp: Decimal) -> Dict[str, Any]:
    body = {"category": CATEGORY, "symbol": SYMBOL, "side": side, "orderType": "Market",
            "qty": str(qty), "timeInForce": "IOC", "reduceOnly": False,
            "takeProfit": str(tp), "stopLoss": str(sl)}
    res = _post("/v5/order/create", body)
    return res

# ---------------- trade CSV & email ----------------
def record_trade(row: Dict[str, Any]):
    header = ["timestamp_utc", "side", "entry", "sl", "tp", "qty", "result"]
    exists = os.path.isfile(TRADE_CSV)
    with open(TRADE_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        if not exists:
            writer.writeheader()
        writer.writerow(row)

def send_daily_summary():
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and EMAIL_TO):
        return
    # Read today's trades from CSV (UTC date)
    today = datetime.now(timezone.utc).date()
    rows = []
    if os.path.isfile(TRADE_CSV):
        with open(TRADE_CSV, "r", newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                ts = datetime.fromisoformat(r["timestamp_utc"])
                if ts.date() == today:
                    rows.append(r)
    body = f"Daily trade summary for {today} UTC\n\n"
    if not rows:
        body += "No trades today."
    else:
        for r in rows:
            body += json.dumps(r) + "\n"
    msg = EmailMessage()
    msg["Subject"] = f"Bybit Bot Daily Summary {today}"
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    except Exception as e:
        print("send_daily_summary error:", e)

# ---------------- main loop ----------------
def run():
    if not API_KEY or not API_SECRET:
        print("Set BYBIT_API_KEY and BYBIT_API_SECRET environment variables.")
        return

    # set leverage once
    set_leverage()

    # On start: do an immediate check for the last closed candle (so we don't miss signals on restart)
    last_hour_key = None

    while True:
        now = datetime.now(timezone.utc)
        # If current time is not at top of hour, do an immediate startup check, else wait for exact top and run
        run_now = False
        # On first iteration we want to run immediately
        if last_hour_key is None:
            run_now = True
        # If we are at top of hour (minute==0, second small window) then run
        if now.minute == 0 and now.second < 10:
            run_now = True

        if not run_now:
            time.sleep(3)
            continue

        # compute current hour key to avoid duplicate runs
        hour_key = datetime.now(timezone.utc).strftime("%Y-%m-%d %H")
        if hour_key == last_hour_key:
            # already done this hour
            time.sleep(2)
            continue
        last_hour_key = hour_key

        # small buffer to ensure exchange finalized the candle
        time.sleep(2)

        # 1) Close any open position (max life = 1h)
        try:
            close_open_position()
        except Exception as e:
            print("Error closing position:", e)

        # 2) fetch klines and compute HA
        try:
            klines = fetch_klines("60", 3)
            if len(klines) < 3:
                # not enough data
                time.sleep(2)
                continue
            ha = compute_heikin_ashi(klines)
            closed_ha = ha[-2]   # last closed HA
            current_ha = ha[-1]  # just-opened/current HA
            # raw open of current candle (entry price)
            entry_price = Decimal(str(klines[-1][1]))
        except Exception as e:
            print("Error fetching klines / computing HA:", e)
            time.sleep(2)
            continue

        # 3) signal detection using 1-tick tolerance
        signal = None
        if closed_ha["ha_close"] > closed_ha["ha_open"] and almost_equal(closed_ha["ha_low"], closed_ha["ha_open"]):
            signal = "Buy"
        elif closed_ha["ha_close"] < closed_ha["ha_open"] and almost_equal(closed_ha["ha_high"], closed_ha["ha_open"]):
            signal = "Sell"

        if not signal:
            # no trade this hour
            continue

        # log signal
        print(f"ℹ️ New signal detected: {signal} (closed candle start={datetime.fromtimestamp(closed_ha['start']/1000, tz=timezone.utc).isoformat()})")

        # 4) prepare SL/TP based on just-opened HA open
        sl_price = current_ha["ha_open"]
        if signal == "Buy":
            risk_distance = entry_price - sl_price
            tp_price = (entry_price + risk_distance + (entry_price * TP_BUFFER_PCT)).quantize(PRICE_TICK)
        else:
            risk_distance = sl_price - entry_price
            tp_price = (entry_price - risk_distance - (entry_price * TP_BUFFER_PCT)).quantize(PRICE_TICK)

        if risk_distance <= Decimal("0"):
            print("Skip trade: non-positive risk distance.")
            continue

        # 5) sizing
        wallet = get_wallet_info("USDT")
        equity = wallet["equity"]
        avail = wallet["available"]
        qty = compute_qty(entry_price, sl_price, equity, avail)
        if qty <= 0:
            print("Qty computed as zero; skipping")
            continue

        # 6) place order with tp/sl attached
        try:
            res = place_market_with_tpsl(signal, qty, entry_price, sl_price, tp_price)
            # check response
            if isinstance(res, dict) and res.get("retCode") == 0:
                print("✅ Trade opened successfully")
                # record trade in CSV with 'result' blank for now
                record_trade({
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "side": signal, "entry": str(entry_price), "sl": str(sl_price),
                    "tp": str(tp_price), "qty": str(qty), "result": ""
                })
            else:
                print("Order create response:", res)
        except Exception as e:
            print("place order error:", e)

        # wait a bit then continue loop
        time.sleep(2)

        # if it's 23:59 UTC, send daily summary
        now2 = datetime.now(timezone.utc)
        if now2.hour == 23 and now2.minute >= 59:
            try:
                send_daily_summary()
            except Exception as e:
                print("daily summary error:", e)

# ---------------- run ----------------
if __name__ == "__main__":
    run()
   
