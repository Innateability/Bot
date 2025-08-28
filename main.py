#!/usr/bin/env python3
"""
Bybit Heikin-Ashi strategy bot
- One-way mode (not hedge)
- Max trade life = 1h
- Runs on main account only
"""

import os
import hmac
import hashlib
import time
import json
import requests
from decimal import Decimal, ROUND_DOWN
from datetime import datetime

# ===== CONFIG =====
API_KEY = os.getenv("BYBIT_API_KEY_MAIN")
API_SECRET = os.getenv("BYBIT_API_SECRET_MAIN")
BASE_URL = "https://api.bybit.com"

SYMBOL = "TRXUSDT"
QTY_RISK = Decimal("0.1")   # 10% of balance
LEVERAGE = 75

# ===== UTILITIES =====
def http_request(method, endpoint, params=None):
    if params is None:
        params = {}
    ts = str(int(time.time() * 1000))
    headers = {"X-BAPI-API-KEY": API_KEY, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": "5000"}
    sign_str = ts + API_KEY + "5000" + (json.dumps(params) if method == "POST" else "")
    signature = hmac.new(API_SECRET.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    headers["X-BAPI-SIGN"] = signature
    url = BASE_URL + endpoint
    if method == "GET":
        r = requests.get(url, headers=headers, params=params)
    else:
        r = requests.post(url, headers=headers, data=json.dumps(params))
    return r.json()

def get_balance():
    res = http_request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT"})
    return Decimal(res["result"]["list"][0]["totalEquity"])

def get_candle():
    res = http_request("GET", "/v5/market/kline", {"category": "linear", "symbol": SYMBOL, "interval": "60", "limit": 2})
    return res["result"]["list"]

def ha_candle(candle, prev_ha):
    o, h, l, c = map(Decimal, [candle[1], candle[2], candle[3], candle[4]])
    ha_close = (o + h + l + c) / 4
    ha_open = (prev_ha["open"] + prev_ha["close"]) / 2 if prev_ha else (o + c) / 2
    ha_high = max(h, ha_open, ha_close)
    ha_low = min(l, ha_open, ha_close)
    return {"open": ha_open, "close": ha_close, "high": ha_high, "low": ha_low}

def close_trade():
    pos = http_request("GET", "/v5/position/list", {"category": "linear", "symbol": SYMBOL})
    if not pos["result"]["list"]:
        return
    size = Decimal(pos["result"]["list"][0]["size"])
    side = pos["result"]["list"][0]["side"]
    if size > 0:
        http_request("POST", "/v5/order/create", {
            "category": "linear", "symbol": SYMBOL, "side": "Sell" if side == "Buy" else "Buy",
            "orderType": "Market", "qty": str(size), "reduceOnly": True
        })
        print("✅ Trade closed successfully")

def open_trade(side, entry, sl, tp):
    balance = get_balance()
    risk_amount = balance * QTY_RISK
    risk = abs(entry - sl)
    qty = (risk_amount / risk * entry).quantize(Decimal("0.1"), rounding=ROUND_DOWN)

    http_request("POST", "/v5/order/create", {
        "category": "linear", "symbol": SYMBOL, "side": side, "orderType": "Market", "qty": str(qty)
    })
    http_request("POST", "/v5/order/create", {
        "category": "linear", "symbol": SYMBOL, "side": "Sell" if side == "Buy" else "Buy",
        "orderType": "Limit", "qty": str(qty), "price": str(tp), "reduceOnly": True
    })
    http_request("POST", "/v5/order/create", {
        "category": "linear", "symbol": SYMBOL, "side": "Sell" if side == "Buy" else "Buy",
        "orderType": "StopMarket", "triggerPrice": str(sl), "qty": str(qty), "reduceOnly": True
    })
    print("✅ Trade opened successfully")

# ===== MAIN LOOP =====
prev_ha = None
last_hour = None

while True:
    now = datetime.utcnow()
    if last_hour != now.hour:
        close_trade()  # close trade at new hour
        candles = get_candle()
        curr = ha_candle(candles[0], prev_ha)
        prev_ha = curr
        # Signal check
        if curr["close"] > curr["open"] and curr["low"] == curr["open"]:  # buy
            sl = curr["open"]
            tp = curr["close"] + (curr["close"] - sl) + (curr["close"] * Decimal("0.001"))
            print("ℹ️ New signal detected: BUY")
            open_trade("Buy", curr["close"], sl, tp)
        elif curr["close"] < curr["open"] and curr["high"] == curr["open"]:  # sell
            sl = curr["open"]
            tp = curr["close"] - (sl - curr["close"]) - (curr["close"] * Decimal("0.001"))
            print("ℹ️ New signal detected: SELL")
            open_trade("Sell", curr["close"], sl, tp)
        last_hour = now.hour
    time.sleep(10)
    
