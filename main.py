#!/usr/bin/env python3
"""
Bybit live trading bot for TRXUSDT (USDT Perpetual)

Strategy:
- Runs on 1-hour timeframe using raw candles for entry, Heikin Ashi for signals
- Signal (evaluate on each CLOSED 1H candle):
    * Long when HA is green (ha_close > ha_open) AND ha_low ~ ha_open (within tolerance)
    * Short when HA is red (ha_close < ha_open) AND ha_high ~ ha_open (within tolerance)
- Entry: NEXT 1H candle raw OPEN (market order at candle open, using raw open for calculations)
- SL: HA open of the signal candle
- TP: 1:1 RR + 0.07% of entry price
- Max holding time: one candle; if neither TP nor SL hit by candle close, force market exit

Risk & Account rules:
- Leverage: 75x
- Position sizing: risk 10% of account equity per trade
- Fallback: if required margin is insufficient, cap size to 90% of what balance can afford
- Profit siphon: starting checkpoint = $4.00. Whenever equity >= 2x checkpoint,
  transfer 25% of current equity from Unified to Funding account (USDT),
  then set checkpoint *= 2

Notes:
- Uses Bybit v5 REST API. One-way mode assumed.
- Tested with Python 3.10+. Requires: requests, pandas, numpy
- Carefully review, paper trade first.

DISCLAIMER: This code is for educational purposes. Use at your own risk.
"""

import os
import time
import hmac
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Any, Tuple, Optional

import requests
import pandas as pd
import numpy as np


# ========================= CONFIG =========================
API_KEY = os.getenv("BYBIT_API_KEY", "YOUR_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET", "YOUR_API_SECRET")
BASE_URL = os.getenv("BYBIT_BASE_URL", "https://api.bybit.com")  # live

SYMBOL = "TRXUSDT"
CATEGORY = "linear"  # USDT perpetual
LEVERAGE = 75
RISK_PCT = Decimal("0.10")       # 10%
EXTRA_TP_PCT = Decimal("0.0007") # 0.07%
TOL_PCT = Decimal("0.0002")      # 0.02% tolerance for open≈low/open≈high
MIN_QTY = Decimal("1")
QTY_STEP = Decimal("1")
PRICE_TICK = Decimal("0.00001")
CHECK_INTERVAL_SEC = 5

# Siphon on doubling
START_CHECKPOINT = Decimal(os.getenv("START_CHECKPOINT", "4"))  # $4.00
TRANSFER_ON_DOUBLING = True
TRANSFER_COIN = "USDT"
FROM_ACCT = "UNIFIED"
TO_ACCT = "FUND"
# ==========================================================


# ----------------- Signing & Requests -----------------
def _ts_ms() -> str:
    return str(int(time.time() * 1000))

def _sign(payload: str) -> str:
    return hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()

def _headers() -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": _ts_ms(),
        "X-BAPI-RECV-WINDOW": "5000",
    }

def _auth_body(body: Dict[str, Any]) -> Tuple[Dict[str, str], str]:
    body_str = json.dumps(body, separators=(",", ":"))
    sign = _sign(_headers()["X-BAPI-TIMESTAMP"] + API_KEY + _headers()["X-BAPI-RECV-WINDOW"] + body_str)
    hdrs = _headers().copy()
    hdrs["X-BAPI-SIGN"] = sign
    return hdrs, body_str

def _get(path: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
    url = BASE_URL + path
    if params is None:
        params = {}
    ts = _ts_ms()
    recv = "5000"
    qs = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
    sign_str = ts + API_KEY + recv + qs
    sign = _sign(sign_str)
    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv,
        "X-BAPI-SIGN": sign,
    }
    r = requests.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()

def _post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    url = BASE_URL + path
    headers, body_str = _auth_body(body)
    r = requests.post(url, data=body_str, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


# ----------------- Exchange Helpers -----------------
def set_leverage(symbol: str, buy: int, sell: int) -> None:
    body = {"category": CATEGORY, "symbol": symbol, "buyLeverage": str(buy), "sellLeverage": str(sell)}
    _post("/v5/position/set-leverage", body)

def get_wallet_equity(coin: str = "USDT") -> Tuple[Decimal, Decimal]:
    j = _get("/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": coin})
    try:
        balances = j["result"]["list"][0]["coin"]
        for c in balances:
            if c["coin"] == coin:
                equity = Decimal(c["equity"])
                avail = Decimal(c.get("availableToWithdraw") or c.get("availableBalance") or c["equity"])
                return equity, avail
    except Exception:
        pass
    return Decimal("0"), Decimal("0")

def transfer_between_accounts(coin: str, amount: Decimal, from_acct: str, to_acct: str) -> Optional[str]:
    body = {
        "transferId": str(int(time.time()*1000)),
        "coin": coin,
        "amount": str(amount),
        "fromAccountType": from_acct,
        "toAccountType": to_acct,
    }
    try:
        j = _post("/v5/asset/transfer", body)
        return j.get("result", {}).get("transferId")
    except Exception as e:
        print(f"Transfer error: {e}")
        return None

def fetch_klines(symbol: str, interval: str = "60", limit: int = 200) -> pd.DataFrame:
    j = _get("/v5/market/kline", {"category": CATEGORY, "symbol": symbol, "interval": interval, "limit": limit})
    rows = j["result"]["list"]
    rows.sort(key=lambda x: int(x[0]))
    data = [[int(r[0]), Decimal(r[1]), Decimal(r[2]), Decimal(r[3]), Decimal(r[4]), Decimal(r[5]), Decimal(r[6] if len(r) > 6 else 0)] for r in rows]
    df = pd.DataFrame(data, columns=["start","open","high","low","close","volume","turnover"])
    df["dt"] = pd.to_datetime(df["start"], unit="ms", utc=True)
    return df

def to_heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    o,h,l,c = df["open"].astype(float).values, df["high"].astype(float).values, df["low"].astype(float).values, df["close"].astype(float).values
    ha_close = (o+h+l+c)/4.0
    ha_open = np.zeros_like(ha_close)
    ha_open[0] = (o[0]+c[0])/2.0
    for i in range(1,len(ha_open)):
        ha_open[i] = (ha_open[i-1]+ha_close[i-1])/2.0
    ha_high = np.maximum.reduce([h,ha_open,ha_close])
    ha_low = np.minimum.reduce([l,ha_open,ha_close])
    out = df.copy()
    out["ha_open"], out["ha_close"], out["ha_high"], out["ha_low"] = ha_open, ha_close, ha_high, ha_low
    return out

def is_green(row) -> bool: return Decimal(str(row.ha_close)) > Decimal(str(row.ha_open))
def is_red(row) -> bool: return Decimal(str(row.ha_close)) < Decimal(str(row.ha_open))

def approx_equal(a: Decimal, b: Decimal, tol_pct: Decimal) -> bool:
    return (a == b) or (abs(a-b) <= (tol_pct*((a+b)/Decimal("2"))))

def detect_signal(ha_df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    if len(ha_df) < 2: return None
    row = ha_df.iloc[-1]
    ha_open = Decimal(str(row.ha_open)).quantize(PRICE_TICK)
    ha_low  = Decimal(str(row.ha_low)).quantize(PRICE_TICK)
    ha_high = Decimal(str(row.ha_high)).quantize(PRICE_TICK)
    if is_green(row) and approx_equal(ha_low,ha_open,TOL_PCT):
        return {"side":"Buy","ha_open":ha_open,"signal_ts":int(row.start)}
    if is_red(row) and approx_equal(ha_high,ha_open,TOL_PCT):
        return {"side":"Sell","ha_open":ha_open,"signal_ts":int(row.start)}
    return None

def round_down_qty(qty: Decimal) -> Decimal:
    steps = (qty/QTY_STEP).to_integral_value(rounding=ROUND_DOWN)
    q = steps*QTY_STEP
    return q if q>=MIN_QTY else Decimal("0")

def get_last_open_price(df: pd.DataFrame) -> Decimal:
    return Decimal(str(df.iloc[-1].open)).quantize(PRICE_TICK)

def place_market_order(symbol: str, side: str, qty: Decimal, reduce_only: bool=False) -> Dict[str, Any]:
    body = {"category":CATEGORY,"symbol":symbol,"side":side,"orderType":"Market","qty":str(qty),"reduceOnly":reduce_only,"timeInForce":"IOC"}
    return _post("/v5/order/create", body)

def place_reduce_only_tp_sl(symbol: str, side: str, qty: Decimal, entry: Decimal, sl: Decimal, extra_tp_pct: Decimal):
    if side=="Buy":
        risk=(entry-sl); tp=(entry+risk+(entry*extra_tp_pct)).quantize(PRICE_TICK)
    else:
        risk=(sl-entry); tp=(entry-risk-(entry*extra_tp_pct)).quantize(PRICE_TICK)
    if risk<=0: return None,None
    tp_body={"category":CATEGORY,"symbol":symbol,"side":"Sell" if side=="Buy" else "Buy","orderType":"Limit","qty":str(qty),"price":str(tp),"reduceOnly":True,"timeInForce":"GTC"}
    tp_res=_post("/v5/order/create",tp_body); tp_id=tp_res.get("result",{}).get("orderId")
    sl_body={"category":CATEGORY,"symbol":symbol,"side":"Sell" if side=="Buy" else "Buy","orderType":"Market","qty":str(qty),"reduceOnly":True,"timeInForce":"GTC","triggerPrice":str(sl),"triggerDirection":2 if side=="Buy" else 1,"tpslMode":"Full"}
    sl_res=_post("/v5/order/create",sl_body); sl_id=sl_res.get("result",{}).get("orderId")
    return tp_id,sl_id

def cancel_all_reduce_only(symbol: str): _post("/v5/order/cancel-all", {"category":CATEGORY,"symbol":symbol})
def close_position_market(symbol: str):
    j=_get("/v5/position/list",{"category":CATEGORY,"symbol":symbol}); pos=j.get("result",{}).get("list",[])
    if not pos: return
    p=pos[0]; size=Decimal(p.get("size","0"))
    if size<=0: return
    side=p.get("side"); close_side="Sell" if side=="Buy" else "Buy"
    place_market_order(symbol, close_side, size, reduce_only=True)

def compute_qty(entry: Decimal, sl: Decimal, equity: Decimal, avail: Decimal) -> Decimal:
    risk=abs(entry-sl)
    if risk<=0: return Decimal("0")
    risk_amt=equity*RISK_PCT
    qty_by_risk=(risk_amt/risk)
    qty_max=(avail*Decimal(LEVERAGE))/entry
    qty=qty_max*Decimal("0.90") if qty_by_risk>qty_max else qty_by_risk
    return round_down_qty(qty)

def next_hour_start(ts_ms: int) -> int:
    dt=datetime.fromtimestamp(ts_ms/1000, tz=timezone.utc)
    nh=(dt+timedelta(hours=1)).replace(minute=0,second=0,microsecond=0)
    return int(nh.timestamp()*1000)

def sleep_until(ts_ms: int):
    while True:
        now=int(time.time()*1000)
        if now>=ts_ms: return
        time.sleep(min(0.5,(ts_ms-now)/1000))


# ----------------- Main Loop -----------------
def main_loop():
    print("Starting bot...")
    try: set_leverage(SYMBOL,LEVERAGE,LEVERAGE)
    except Exception as e: print("Leverage set error:",e)

    checkpoint=START_CHECKPOINT
    pending_signal=None

    while True:
        try:
            df=fetch_klines(SYMBOL,"60",300)
            ha=to_heikin_ashi(df)
            sig=detect_signal(ha)
            if sig:
                pending_signal={**sig,"enter_at":next_hour_start(sig["signal_ts"])}
                print(f"Signal {sig['side']} at {sig['ha_open']} → enter next hour")

            if pending_signal and int(time.time()*1000)>=pending_signal["enter_at"]:
                df2=fetch_klines(SYMBOL,"60",2)
                entry=get_last_open_price(df2); side=pending_signal["side"]; sl=pending_signal["ha_open"]
                if (side=="Buy" and entry<=sl) or (side=="Sell" and entry>=sl):
                    print("Skip: non-positive risk"); pending_signal=None; continue
                equity,avail=get_wallet_equity("USDT")
                qty=compute_qty(entry,sl,equity,avail)
                if qty<=0: print("Qty too small"); pending_signal=None; continue
                print(f"Entering {side} qty={qty} at ~{entry} SL={sl}")
                place_market_order(SYMBOL,side,qty)
                tp_id,sl_id=place_reduce_only_tp_sl(SYMBOL,side,qty,entry,sl,EXTRA_TP_PCT)
                exit_at=next_hour_start(pending_signal["enter_at"]); sleep_until(exit_at-1500)
                jpos=_get("/v5/position/list",{"category":CATEGORY,"symbol":SYMBOL}); pos_list=jpos.get("result",{}).get("list",[])
                still_open=pos_list and Decimal(pos_list[0].get("size","0"))>0
                if still_open: print("Force exit..."); cancel_all_reduce_only(SYMBOL); close_position_market(SYMBOL)
                if TRANSFER_ON_DOUBLING:
                    eq_now,_=get_wallet_equity("USDT")
                    if eq_now>=checkpoint*2:
                        amt=(eq_now*Decimal("0.25")).quantize(Decimal("0.01"))
                        if amt>0: txid=transfer_between_accounts(TRANSFER_COIN,amt,FROM_ACCT,TO_ACCT); print(f"Siphon {amt} {TRANSFER_COIN} txid={txid}")
                        checkpoint*=2
                pending_signal=None
            time.sleep(CHECK_INTERVAL_SEC)
        except Exception as e:
            print("Loop error:",e); time.sleep(2)

if __name__=="__main__":
    main_loop()
   
