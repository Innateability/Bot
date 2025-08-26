here#!/usr/bin/env python3
"""
Trading bot implementing your rules:

- 1H Heikin-Ashi breakout levels:
    * If 1H HA is RED  -> buy breakout level = HIGH of that HA candle
    * If 1H HA is GREEN -> sell breakout level = LOW of that HA candle

- 5m raw-candle trigger logic:
    * Entry when a 5m candle's HIGH >= buy_level AND its RAW CLOSE > buy_level  -> Buy (entry = raw close)
    * Entry when a 5m candle's LOW <= sell_level AND its RAW CLOSE < sell_level -> Sell (entry = raw close)

- SL derived from the triggering 5m candle:
    * Buy SL = LOW of that triggering candle
    * Sell SL = HIGH of that triggering candle

- TP: RR-based as before (1:1 default, 2:1 when previous trade was loss -> small extra)
- No forced closes on breaches — only logs
- Transfers: logs planned transfer; if FUND transfer fails due to insufficient balance it retries using UNIFIED as source
- SL stop-market orders include triggerDirection (v5 order/create)
"""
import os
import hmac
import hashlib
import time
import json
import uuid
import requests
import logging
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta
from collections import deque

# -------- Logging --------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

# -------- Config --------
BASE_URL = "https://api.bybit.com"
MARKET_URL = BASE_URL + "/v5/market"
TIMEOUT = 15

# Symbols (comma-separated env var SYMBOLS)
SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", "TRXUSDT,1000BONKUSDT").split(",") if s.strip()]

# Strategy params
FIVE_M_HISTORY = 500
EXTRA_TP_PCT = Decimal("0.0007")  # +0.07% extra when using 2:1+extra
DEFAULT_LEVERAGE = Decimal("75")  # default for most symbols (TRX)
BONK_LEVERAGE = Decimal("50")     # for 1000BONK
RISK_FRACTION = Decimal("0.10")   # 10% of combined balance risk
BAL_CAP = Decimal("0.90")         # cap initial margin usage to 90% of trading balance

# API keys / ids (set in environment)
API_KEY_MAIN    = os.getenv("BYBIT_MAIN_KEY",    "PUT_MAIN_KEY_HERE")
API_SECRET_MAIN = os.getenv("BYBIT_MAIN_SECRET", "PUT_MAIN_SECRET_HERE")
API_KEY_SUB     = os.getenv("BYBIT_SUB_KEY",     "PUT_SUB_KEY_HERE")
API_SECRET_SUB  = os.getenv("BYBIT_SUB_SECRET",  "PUT_SUB_SECRET_HERE")
SUB_UID         = os.getenv("BYBIT_SUB_UID",     "PUT_SUB_UID_HERE")
PROFIT_UID      = os.getenv("BYBIT_PROFIT_UID",  "PUT_PROFIT_UID_HERE")
SIPHON_BASE_USD = Decimal(os.getenv("BYBIT_SIPHON_BASE_USD", "10"))

# =========================
# Helpers
# =========================
def _ts_ms():
    return str(int(time.time() * 1000))

def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

def private_request(api_key, api_secret, method, path, params=None, body=None):
    """Generic Bybit V5 signed request helper. Raises RuntimeError when retCode != 0."""
    if params is None:
        params = {}
    if body is None:
        body = {}
    url = BASE_URL + path
    ts = _ts_ms()
    recv_window = "5000"
    if method.upper() == "GET":
        query = "&".join([f"{k}={params[k]}" for k in sorted(params)]) if params else ""
        payload = ts + api_key + recv_window + query
        sign = _sign(api_secret, payload)
        headers = {
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN": sign
        }
        r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
    else:
        query = "&".join([f"{k}={params[k]}" for k in sorted(params)]) if params else ""
        body_json = json.dumps(body) if body else ""
        payload = ts + api_key + recv_window + query + body_json
        sign = _sign(api_secret, payload)
        headers = {
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN": sign,
            "Content-Type": "application/json"
        }
        r = requests.post(url, params=params, data=body_json, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit error {data.get('retCode')}: {data.get('retMsg')} | {data}")
    return data

def market_kline_url():
    return MARKET_URL + "/kline"

def get_candles(symbol, interval="60", limit=200):
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(market_kline_url(), params=params, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if "result" not in data or "list" not in data["result"]:
        raise RuntimeError(f"Bad response for {symbol} interval {interval}: {data}")
    return data["result"]["list"]

def to_heikin_ashi(raw):
    """
    raw: list of candles newest-first from API.
    Return list of HA candles oldest->newest: (open, high, low, close, color, ts)
    """
    ha = []
    D2 = Decimal("2"); D4 = Decimal("4")
    for i, c in enumerate(reversed(raw)):  # iterate oldest->newest
        ts = int(c[0])
        o = Decimal(str(c[1])); h = Decimal(str(c[2])); l = Decimal(str(c[3])); cl = Decimal(str(c[4]))
        ha_close = (o + h + l + cl) / D4
        if i == 0:
            ha_open = (o + cl) / D2
        else:
            p_open, _, _, p_close, _, _ = ha[-1]
            ha_open = (p_open + p_close) / D2
        ha_high = max(h, ha_open, ha_close)
        ha_low  = min(l, ha_open, ha_close)
        color = "GREEN" if ha_close >= ha_open else "RED"
        ha.append((ha_open, ha_high, ha_low, ha_close, color, ts))
    return ha

def wait_until_next_5m():
    now = datetime.utcnow()
    m = (now.minute // 5 + 1) * 5
    if m == 60:
        nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    else:
        nxt = now.replace(minute=m, second=0, microsecond=0)
    sleep_s = max(0.0, (nxt - now).total_seconds())
    logging.info(f"⏳ Waiting {sleep_s:.0f}s until {nxt:%Y-%m-%d %H:%M:%S} UTC (next 5m mark)")
    time.sleep(sleep_s)

# =========================
# Instrument info & rounding
# =========================
def get_instrument_info(symbol):
    params = {"category": "linear", "symbol": symbol}
    r = requests.get(MARKET_URL + "/instruments-info", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0 or not data["result"]["list"]:
        raise RuntimeError(f"instruments-info error for {symbol}: {data}")
    info = data["result"]["list"][0]
    tick = Decimal(info["priceFilter"]["tickSize"])
    step = Decimal(info["lotSizeFilter"]["qtyStep"])
    min_qty = Decimal(info["lotSizeFilter"].get("minOrderQty", "0"))
    return tick, step, min_qty

def round_price(px: Decimal, tick: Decimal) -> Decimal:
    return (px // tick) * tick

def round_qty(qty: Decimal, step: Decimal) -> Decimal:
    return (qty // step) * step

# =========================
# Balances & transfers (adjusted for UNIFIED main/sub)
# - handles same-account-type transfers safely and retries using UNIFIED if FUND insufficient
# =========================
def get_wallet_balance(api_key, api_secret):
    params = {"accountType": "UNIFIED", "coin": "USDT"}
    data = private_request(api_key, api_secret, "GET", "/v5/account/wallet-balance", params=params)
    lst = data["result"]["list"]
    if not lst:
        return Decimal("0")
    return Decimal(str(lst[0]["totalEquity"]))

def _uuid():
    return str(uuid.uuid4())

def _attempt_inter_transfer(api_key_master, api_secret_master, body):
    """Call inter-transfer endpoint; returns result or raises."""
    return private_request(api_key_master, api_secret_master, "POST", "/v5/asset/transfer/inter-transfer", body=body)

def universal_transfer_with_fallback(api_key_master, api_secret_master, amount_usdt: Decimal, fromAccountType: str, toAccountType: str, fromMemberId: str = None, toMemberId: str = None):
    """
    Generic inter-transfer that logs the intended transfer and tries first with provided fromAccountType.
    If it fails due to insufficient balance in FUND, it'll retry using UNIFIED as source.
    Defensive checks:
      - If fromAccountType == toAccountType AND (fromMemberId == toMemberId OR both None),
        there's nothing to do (invalid transfer) -> log and return False.
    """
    # Defensive: invalid same-account same-member transfer
    if fromAccountType == toAccountType and ((fromMemberId is None and toMemberId is None) or (fromMemberId == toMemberId)):
        logging.warning(f"Transfer skipped: fromAccountType==toAccountType and fromMemberId==toMemberId (no-op). "
                        f"from={fromAccountType} to={toAccountType} fromMemberId={fromMemberId} toMemberId={toMemberId}")
        return False

    amt_str = str(amount_usdt.quantize(Decimal('0.01'), rounding=ROUND_DOWN))
    body = {
        "transferId": _uuid(),
        "coin": "USDT",
        "amount": amt_str,
        "fromAccountType": fromAccountType,
        "toAccountType": toAccountType
    }
    if fromMemberId:
        body["fromMemberId"] = fromMemberId
    if toMemberId:
        body["toMemberId"] = toMemberId

    from_desc = f"{body['fromAccountType']}{' UID:'+fromMemberId if fromMemberId else ''}"
    to_desc = f"{body['toAccountType']}{' UID:'+toMemberId if toMemberId else ''}"
    logging.info(f"🔁 Rebalance plan: {from_desc} -> {to_desc} amount={amt_str} USDT")

    try:
        _attempt_inter_transfer(api_key_master, api_secret_master, body)
        logging.info(f"🔁 Transfer executed: {from_desc} -> {to_desc} amount={amt_str} USDT")
        return True
    except Exception as e:
        msg = str(e)
        logging.warning(f"Transfer attempt failed ({from_desc} -> {to_desc}): {msg}")
        # If failure indicates insufficient balance in FUND, retry using UNIFIED as source
        if ("insufficient balance" in msg.lower()) or ("131212" in msg):
            if body["fromAccountType"] != "UNIFIED":
                body_retry = body.copy()
                body_retry["fromAccountType"] = "UNIFIED"
                retry_from_desc = f"{body_retry['fromAccountType']}{' UID:'+fromMemberId if fromMemberId else ''}"
                logging.info(f"🔁 Retrying transfer using UNIFIED as source: {retry_from_desc} -> {to_desc} amount={amt_str} USDT")
                try:
                    _attempt_inter_transfer(api_key_master, api_secret_master, body_retry)
                    logging.info(f"🔁 Transfer executed on retry: {retry_from_desc} -> {to_desc} amount={amt_str} USDT")
                    return True
                except Exception as e2:
                    logging.warning(f"Retry transfer (UNIFIED) failed: {e2}")
                    return False
        return False

# EXPLICIT: main and sub are both UNIFIED account type in your setup
def universal_transfer_main_to_sub(api_key_master, api_secret_master, amount_usdt: Decimal, to_sub_uid: str):
    # master (no member id) UNIFIED -> sub UNIFIED (toMemberId=sub uid)
    return universal_transfer_with_fallback(api_key_master, api_secret_master, amount_usdt,
                                            fromAccountType="UNIFIED", toAccountType="UNIFIED", toMemberId=to_sub_uid)

def universal_transfer_sub_to_main(api_key_master, api_secret_master, amount_usdt: Decimal, from_sub_uid: str):
    # sub UNIFIED -> master UNIFIED
    return universal_transfer_with_fallback(api_key_master, api_secret_master, amount_usdt,
                                            fromAccountType="UNIFIED", toAccountType="UNIFIED", fromMemberId=from_sub_uid)

def universal_transfer_main_to_uid(api_key_master, api_secret_master, amount_usdt: Decimal, to_uid: str):
    # master UNIFIED -> another UID UNIFIED
    return universal_transfer_with_fallback(api_key_master, api_secret_master, amount_usdt,
                                            fromAccountType="UNIFIED", toAccountType="UNIFIED", toMemberId=to_uid)

def universal_transfer_sub_to_uid(api_key_master, api_secret_master, amount_usdt: Decimal, from_sub_uid: str, to_uid: str):
    # sub UNIFIED -> other UNIFIED UID
    return universal_transfer_with_fallback(api_key_master, api_secret_master, amount_usdt,
                                            fromAccountType="UNIFIED", toAccountType="UNIFIED", fromMemberId=from_sub_uid, toMemberId=to_uid)

def rebalance_equal(api_key_main, api_secret_main, api_key_sub, api_secret_sub, sub_uid):
    try:
        bal_main = get_wallet_balance(api_key_main, api_secret_main)
        bal_sub  = get_wallet_balance(api_key_sub, api_secret_sub)
        total = bal_main + bal_sub
        target_each = total / Decimal("2")
        delta_main = target_each - bal_main
        if abs(delta_main) < Decimal("0.05"):
            return
        amt = abs(delta_main).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if delta_main > 0:
            # Need SUB -> MAIN
            if sub_uid and sub_uid != "PUT_SUB_UID_HERE":
                logging.info(f"🔁 Rebalance need: SUB -> MAIN amount={amt} USDT")
                universal_transfer_sub_to_main(api_key_main, api_secret_main, amt, sub_uid)
        else:
            # Need MAIN -> SUB
            if sub_uid and sub_uid != "PUT_SUB_UID_HERE":
                logging.info(f"🔁 Rebalance need: MAIN -> SUB amount={amt} USDT")
                universal_transfer_main_to_sub(api_key_main, api_secret_main, amt, sub_uid)
    except Exception as e:
        logging.warning(f"Rebalance equal failed: {e}")

# Profit siphon helper
def siphon_profits_if_needed(api_key_main, api_secret_main, api_key_sub, api_secret_sub, sub_uid: str, dest_uid: str, milestone_ref: dict):
    if not dest_uid or dest_uid == "PUT_PROFIT_UID_HERE":
        return
    bal_main = get_wallet_balance(api_key_main, api_secret_main)
    bal_sub  = get_wallet_balance(api_key_sub, api_secret_sub)
    total = bal_main + bal_sub
    if milestone_ref.get("value") is None:
        milestone_ref["value"] = SIPHON_BASE_USD
        logging.info(f"💾 Profit siphon milestone set to ${milestone_ref['value']}")
    milestone = milestone_ref["value"]
    trigger = milestone * Decimal("2")
    if total >= trigger:
        send_amt = (total * Decimal("0.25")).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        logging.info(f"💰 Siphon trigger! Combined=${total:.2f} ≥ ${trigger:.2f}. Send 25% = ${send_amt} to UID({dest_uid})")
        remaining = send_amt
        take_main = min(bal_main, remaining)
        if take_main > 0:
            ok = universal_transfer_main_to_uid(api_key_main, api_secret_main, take_main, dest_uid)
            if ok:
                remaining -= take_main
        if remaining > 0 and sub_uid and sub_uid != "PUT_SUB_UID_HERE":
            take_sub = min(bal_sub, remaining)
            if take_sub > 0:
                universal_transfer_sub_to_uid(api_key_main, api_secret_main, take_sub, sub_uid, dest_uid)
                remaining -= take_sub
        bal_main2 = get_wallet_balance(api_key_main, api_secret_main)
        bal_sub2  = get_wallet_balance(api_key_sub, api_secret_sub)
        milestone_ref["value"] = bal_main2 + bal_sub2
        logging.info(f"✅ Siphon done. New milestone ${milestone_ref['value']:.2f} (next at ${ (milestone_ref['value']*2):.2f }).")

# =========================
# Orders & execution
# =========================
def set_leverage(api_key, api_secret, symbol, leverage: Decimal):
    try:
        body = {"category": "linear", "symbol": symbol, "leverage": str(int(leverage))}
        private_request(api_key, api_secret, "POST", "/v5/position/set-leverage", body=body)
        logging.info(f"Set leverage {leverage}x for {symbol} on account.")
        return True
    except Exception as e:
        # log and continue — sometimes exchanges reject certain leverage settings
        logging.warning(f"Set leverage failed for {symbol}: {e}")
        return False

def get_leverage_for_symbol(symbol):
    return BONK_LEVERAGE if symbol.upper().startswith("1000BONK") else DEFAULT_LEVERAGE

def place_market_order(api_key, api_secret, symbol, side, qty):
    body = {"category": "linear", "symbol": symbol, "side": side, "orderType": "Market", "qty": str(qty), "timeInForce": "IOC"}
    data = private_request(api_key, api_secret, "POST", "/v5/order/create", body=body)
    logging.info(f"Placing MARKET {side} {symbol} qty={qty} -> {data}")
    return data.get("result", {}).get("orderId")

def place_reduce_only_tp(api_key, api_secret, symbol, side, tp_price, qty):
    opp = "Sell" if side == "Buy" else "Buy"
    body = {"category": "linear", "symbol": symbol, "side": opp, "orderType": "Limit", "price": str(tp_price), "qty": str(qty), "reduceOnly": True, "timeInForce": "GTC"}
    data = private_request(api_key, api_secret, "POST", "/v5/order/create", body=body)
    logging.info(f"Placing TP LIMIT ReduceOnly {opp} {symbol} qty={qty} price={tp_price} -> {data}")
    return data.get("result", {}).get("orderId")

def _trigger_direction_for_sl(side: str) -> int:
    # Long SL triggers on price falling to/below -> triggerDirection=2
    # Short SL triggers on price rising to/above -> triggerDirection=1
    return 2 if side == "Buy" else 1

def place_reduce_only_sl_stopmarket(api_key, api_secret, symbol, side, sl_price, qty, trigger_by="LastPrice"):
    opp = "Sell" if side == "Buy" else "Buy"
    body = {
        "category": "linear",
        "symbol": symbol,
        "side": opp,
        "orderType": "Market",
        "qty": str(qty),
        "reduceOnly": True,
        "triggerPrice": str(sl_price),
        "triggerBy": trigger_by,
        "triggerDirection": _trigger_direction_for_sl(side),
        "timeInForce": "GTC"
    }
    data = private_request(api_key, api_secret, "POST", "/v5/order/create", body=body)
    logging.info(f"Placing SL StopMarket ReduceOnly {opp} {symbol} qty={qty} trigger={sl_price} -> {data}")
    return data.get("result", {}).get("orderId")

def cancel_all_orders_for_symbol(api_key, api_secret, symbol):
    try:
        private_request(api_key, api_secret, "POST", "/v5/order/cancel-all", body={"category": "linear", "symbol": symbol})
        logging.info(f"🧹 Cancelled all orders on this account for {symbol}.")
    except Exception as e:
        logging.warning(f"Cancel orders failed for {symbol}: {e}")

def market_close_all_positions_for_symbol(api_key, api_secret, symbol):
    try:
        pos_data = private_request(api_key, api_secret, "GET", "/v5/position/list", params={"category": "linear", "symbol": symbol})
        for pos in pos_data["result"]["list"]:
            side = pos["side"]; size = Decimal(pos["size"])
            if size > 0:
                opp = "Sell" if side == "Buy" else "Buy"
                private_request(api_key, api_secret, "POST", "/v5/order/create",
                                body={"category": "linear", "symbol": symbol, "side": opp, "orderType": "Market", "qty": str(size), "reduceOnly": True, "timeInForce": "IOC"})
                logging.info(f"🔒 Closed {side} position of {size} {symbol}.")
    except Exception as e:
        logging.warning(f"Close positions failed for {symbol}: {e}")

def close_account_symbol(api_key, api_secret, symbol):
    cancel_all_orders_for_symbol(api_key, api_secret, symbol)
    market_close_all_positions_for_symbol(api_key, api_secret, symbol)

# =========================
# Sizing
# =========================
def compute_qty(entry: Decimal, sl: Decimal, trading_bal: Decimal, combined_bal: Decimal, lot_step: Decimal, min_qty: Decimal, leverage: Decimal) -> Decimal:
    per_coin_risk = abs(entry - sl)
    if per_coin_risk <= Decimal("0"):
        return Decimal("0")
    risk_usd = combined_bal * RISK_FRACTION
    qty_risk = (risk_usd / per_coin_risk)
    qty_cap  = (trading_bal * BAL_CAP * leverage) / entry
    qty = min(qty_risk, qty_cap)
    qty = round_qty(qty, lot_step)
    if qty < min_qty:
        return Decimal("0")
    return qty

def fallback_qty_by_account_balance(entry: Decimal, trading_bal: Decimal, leverage: Decimal, lot_step: Decimal, min_qty: Decimal) -> Decimal:
    if trading_bal <= 0 or entry <= 0:
        return Decimal("0")
    qty_cap = (trading_bal * Decimal("0.90") * leverage) / entry
    qty_cap = round_qty(qty_cap, lot_step)
    if qty_cap < min_qty:
        return Decimal("0")
    return qty_cap

def try_place_market_order_with_fallback(api_key, api_secret, symbol, side, qty, entry_price, trading_bal, leverage, lot_step, min_qty):
    # Try to set leverage, but proceed even if set_leverage fails (we log failure)
    try:
        set_leverage(api_key, api_secret, symbol, leverage)
    except Exception:
        pass
    try:
        return place_market_order(api_key, api_secret, symbol, side, qty)
    except Exception as e:
        logging.warning(f"{symbol} initial market order failed: {e}. Attempting fallback sizing.")
        fallback_qty = fallback_qty_by_account_balance(entry_price, trading_bal, leverage, lot_step, min_qty)
        if fallback_qty <= 0:
            logging.info(f"{symbol} fallback qty too small ({fallback_qty}) -> skipping trade.")
            raise
        if fallback_qty >= qty:
            logging.info(f"{symbol} fallback qty {fallback_qty} >= original qty {qty}, re-raising.")
            raise
        try:
            logging.info(f"{symbol} retrying MARKET order with fallback qty={fallback_qty}")
            try:
                set_leverage(api_key, api_secret, symbol, leverage)
            except Exception:
                pass
            return place_market_order(api_key, api_secret, symbol, side, fallback_qty)
        except Exception as e2:
            logging.error(f"{symbol} fallback market order also failed: {e2}")
            raise

# =========================
# Trigger/candle helpers
# =========================
def parse_raw_candle(c):
    # c expected like [ts, open, high, low, close, volume] (newest-first)
    ts = int(c[0])
    o = Decimal(str(c[1])); h = Decimal(str(c[2])); l = Decimal(str(c[3])); cl = Decimal(str(c[4])); vol = Decimal(str(c[5])) if len(c) > 5 else Decimal("0")
    return {"ts": ts, "open": o, "high": h, "low": l, "close": cl, "volume": vol}

def find_triggering_5m_candle_for_side(raw_5m_list, buy_level, sell_level):
    """
    raw_5m_list: list of raw 5m candles (newest-first)
    Scan oldest->newest and return first triggering candle (side, parsed, entry, sl_candidate).
    For buy trigger: candle.high >= buy_level AND candle.close > buy_level -> entry=candle.close, sl=candle.low
    For sell trigger: candle.low <= sell_level AND candle.close < sell_level -> entry=candle.close, sl=candle.high
    """
    for c in reversed(raw_5m_list):  # oldest -> newest
        parsed = parse_raw_candle(c)
        if buy_level is not None:
            if parsed["high"] >= buy_level and parsed["close"] > buy_level:
                return ("Buy", parsed, parsed["close"], parsed["low"])
        if sell_level is not None:
            if parsed["low"] <= sell_level and parsed["close"] < sell_level:
                return ("Sell", parsed, parsed["close"], parsed["high"])
    return None

# =========================
# Main loop & state
# =========================
def main():
    precision = {}
    for sym in SYMBOLS:
        tick, lot_step, min_qty = get_instrument_info(sym)
        precision[sym] = {"tick": tick, "lot": lot_step, "min": min_qty}
        logging.info(f"{sym}: tick={tick} lotStep={lot_step} minQty={min_qty}")

    buy_level   = {sym: None for sym in SYMBOLS}
    sell_level  = {sym: None for sym in SYMBOLS}
    last_hour_processed = None

    open_buy  = {sym: None for sym in SYMBOLS}
    open_sell = {sym: None for sym in SYMBOLS}

    ha_5m_history = {sym: deque(maxlen=FIVE_M_HISTORY) for sym in SYMBOLS}
    siphon_state = {"value": None}
    last_result = {"sub": {sym: None for sym in SYMBOLS}, "main": {sym: None for sym in SYMBOLS}}

    while True:
        now = datetime.utcnow()

        # hourly arms: compute breakout levels from 1H HA per your rules
        if now.minute == 0 and now.second == 0:
            hour_tag = now.replace(minute=0, second=0, microsecond=0)
            if last_hour_processed != hour_tag:
                last_hour_processed = hour_tag
                for sym in SYMBOLS:
                    try:
                        raw_1h = get_candles(sym, interval="60", limit=3)
                        ha_1h = to_heikin_ashi(raw_1h)
                        if not ha_1h:
                            raise RuntimeError("No 1H HA data")
                        last_1h = ha_1h[-1]  # oldest->newest
                    except Exception as e:
                        logging.error(f"{sym} 1H fetch/convert error: {e}")
                        continue
                    _, h1, l1, c1, col1, ts1 = last_1h
                    logging.info(f"{sym} 1H closed HA: {col1} | HA-H={h1} L={l1} C={c1}")
                    # Per your rule:
                    # - If 1H HA is RED -> buy breakout level = HIGH of that HA candle
                    # - If 1H HA is GREEN -> sell breakout level = LOW of that HA candle
                    if col1 == "RED":
                        buy_level[sym]  = Decimal(str(h1))
                        sell_level[sym] = None
                        logging.info(f"{sym} 🎯 Buy Level set @ {buy_level[sym]} (1H HA red)")
                    else:
                        sell_level[sym] = Decimal(str(l1))
                        buy_level[sym]  = None
                        logging.info(f"{sym} 🎯 Sell Level set @ {sell_level[sym]} (1H HA green)")

        # every 5m processing
        for sym in SYMBOLS:
            tick = precision[sym]["tick"]; lot = precision[sym]["lot"]; min_qty = precision[sym]["min"]
            try:
                # fetch multiple 5m candles to find the triggering candle
                raw_5m = get_candles(sym, interval="5", limit=10)
                ha_5m = to_heikin_ashi(raw_5m)
                if ha_5m:
                    ha_5m_history[sym].append(ha_5m[-1])
                last_5m = ha_5m[-1]
                _, h5, l5, ha_close, col5, ts5 = last_5m
                raw_latest = raw_5m[0]  # newest-first
                raw_close = Decimal(str(raw_latest[4]))
                c5 = raw_close
            except Exception as e:
                logging.error(f"{sym} 5M fetch/convert error: {e}")
                continue

            h5 = Decimal(str(h5)); l5 = Decimal(str(l5)); c5 = Decimal(str(c5))

            # 5m watchdog: detect TP/SL breaches by candle H/L, but DO NOT force close — only log
            ob = open_buy[sym]
            if ob is not None:
                if l5 <= ob.sl:
                    logging.info(f"🛡️ {sym} BUY SL breached (5m low {l5} <= SL {ob.sl}). NOTE: not forcing close per config.")
                if h5 >= ob.tp:
                    logging.info(f"🎯 {sym} BUY TP breached (5m high {h5} >= TP {ob.tp}). NOTE: not forcing close per config.")

            osell = open_sell[sym]
            if osell is not None:
                if h5 >= osell.sl:
                    logging.info(f"🛡️ {sym} SELL SL breached (5m high {h5} >= SL {osell.sl}). NOTE: not forcing close per config.")
                if l5 <= osell.tp:
                    logging.info(f"🎯 {sym} SELL TP breached (5m low {l5} <= TP {osell.tp}). NOTE: not forcing close per config.")

            # Check for triggering candle among recent raw 5m candles (oldest->newest)
            trigger = find_triggering_5m_candle_for_side(raw_5m, buy_level[sym], sell_level[sym])
            if trigger:
                side, trig_candle, entry, sl_candidate = trigger

                if side == "Buy" and buy_level[sym] is not None:
                    sl = sl_candidate
                    tp = None
                    risk = entry - sl
                    if risk <= 0:
                        logging.info(f"{sym} ⚠️ Computed BUY SL not below entry (entry={entry} sl={sl}); skipping.")
                    else:
                        use_2to1_plus = (last_result["sub"][sym] == "loss")
                        if use_2to1_plus:
                            tp = entry + (risk * Decimal("2")) + (entry * EXTRA_TP_PCT)
                        else:
                            tp = entry + risk
                        entry_r = round_price(entry, tick); sl_r = round_price(sl, tick); tp_r = round_price(tp, tick)

                        logging.info(f"{sym} ✅ BUY trigger found at 5m candle ts={datetime.utcfromtimestamp(trig_candle['ts']/1000):%Y-%m-%d %H:%M:%S} "
                                     f"| raw OHL C={trig_candle['open']},{trig_candle['high']},{trig_candle['low']},{trig_candle['close']} "
                                     f"| Entry={entry_r} SL={sl_r} TP={tp_r}")

                        try:
                            rebalance_equal(API_KEY_MAIN, API_SECRET_MAIN, API_KEY_SUB, API_SECRET_SUB, SUB_UID)
                        except Exception as e:
                            logging.warning(f"{sym} Pre-trade rebalance failed: {e}")

                        bal_main = get_wallet_balance(API_KEY_MAIN, API_SECRET_MAIN)
                        bal_sub  = get_wallet_balance(API_KEY_SUB, API_SECRET_SUB)
                        combined = bal_main + bal_sub

                        leverage = get_leverage_for_symbol(sym)
                        qty = compute_qty(entry_r, sl_r, bal_sub, combined, lot, min_qty, leverage)
                        if qty <= 0:
                            fallback = fallback_qty_by_account_balance(entry_r, bal_sub, leverage, lot, min_qty)
                            if fallback <= 0:
                                logging.info(f"{sym} ⚠️ BUY sizing too small even after fallback; skipping")
                                buy_level[sym] = None
                                continue
                            qty = fallback
                            logging.info(f"{sym} Using fallback BUY qty based on 90% sub balance: {qty}")

                        if open_buy[sym] is not None:
                            logging.info(f"{sym} 🔁 New BUY trigger while BUY already recorded -> leaving existing open record.")
                        else:
                            try:
                                entry_id = try_place_market_order_with_fallback(API_KEY_SUB, API_SECRET_SUB, sym, "Buy", qty, entry_r, get_wallet_balance(API_KEY_SUB, API_SECRET_SUB), leverage, lot, min_qty)
                                tp_id = place_reduce_only_tp(API_KEY_SUB, API_SECRET_SUB, sym, "Buy", tp_r, qty)
                                sl_id = place_reduce_only_sl_stopmarket(API_KEY_SUB, API_SECRET_SUB, sym, "Buy", sl_r, qty, trigger_by="LastPrice")
                                logging.info(f"{sym} 📦 BUY opened (orderId={entry_id}); TP({tp_id}) & SL(stopMarket:{sl_id}) ReduceOnly")
                                open_buy[sym] = type("OT",(object,),{"symbol":sym,"side":"Buy","qty":qty,"entry":entry_r,"sl":sl_r,"tp":tp_r,"entry_order_id":entry_id,"sl_order_id":sl_id,"tp_order_id":tp_id,"api_key":API_KEY_SUB,"api_secret":API_SECRET_SUB})()
                                last_result["sub"][sym] = None
                            except Exception as e:
                                logging.error(f"{sym} BUY order error: {e}")

                        buy_level[sym] = None

                if side == "Sell" and sell_level[sym] is not None:
                    sl = sl_candidate
                    tp = None
                    risk = sl - entry
                    if risk <= 0:
                        logging.info(f"{sym} ⚠️ Computed SELL SL not above entry (entry={entry} sl={sl}); skipping.")
                    else:
                        use_2to1_plus = (last_result["main"][sym] == "loss")
                        if use_2to1_plus:
                            tp = entry - (risk * Decimal("2")) - (entry * EXTRA_TP_PCT)
                        else:
                            tp = entry - risk
                        entry_r = round_price(entry, tick); sl_r = round_price(sl, tick); tp_r = round_price(tp, tick)

                        logging.info(f"{sym} ✅ SELL trigger found at 5m candle ts={datetime.utcfromtimestamp(trig_candle['ts']/1000):%Y-%m-%d %H:%M:%S} "
                                     f"| raw OHL C={trig_candle['open']},{trig_candle['high']},{trig_candle['low']},{trig_candle['close']} "
                                     f"| Entry={entry_r} SL={sl_r} TP={tp_r}")

                        try:
                            rebalance_equal(API_KEY_MAIN, API_SECRET_MAIN, API_KEY_SUB, API_SECRET_SUB, SUB_UID)
                        except Exception as e:
                            logging.warning(f"{sym} Pre-trade rebalance failed: {e}")

                        bal_main = get_wallet_balance(API_KEY_MAIN, API_SECRET_MAIN)
                        bal_sub  = get_wallet_balance(API_KEY_SUB, API_SECRET_SUB)
                        combined = bal_main + bal_sub

                        leverage = get_leverage_for_symbol(sym)
                        qty = compute_qty(entry_r, sl_r, bal_main, combined, lot, min_qty, leverage)
                        if qty <= 0:
                            fallback = fallback_qty_by_account_balance(entry_r, bal_main, leverage, lot, min_qty)
                            if fallback <= 0:
                                logging.info(f"{sym} ⚠️ SELL sizing too small even after fallback; skipping")
                                sell_level[sym] = None
                                continue
                            qty = fallback
                            logging.info(f"{sym} Using fallback SELL qty based on 90% main balance: {qty}")

                        if open_sell[sym] is not None:
                            logging.info(f"{sym} 🔁 New SELL trigger while SELL already recorded -> leaving existing open record.")
                        else:
                            try:
                                entry_id = try_place_market_order_with_fallback(API_KEY_MAIN, API_SECRET_MAIN, sym, "Sell", qty, entry_r, get_wallet_balance(API_KEY_MAIN, API_SECRET_MAIN), leverage, lot, min_qty)
                                tp_id = place_reduce_only_tp(API_KEY_MAIN, API_SECRET_MAIN, sym, "Sell", tp_r, qty)
                                sl_id = place_reduce_only_sl_stopmarket(API_KEY_MAIN, API_SECRET_MAIN, sym, "Sell", sl_r, qty, trigger_by="LastPrice")
                                logging.info(f"{sym} 📦 SELL opened (orderId={entry_id}); TP({tp_id}) & SL(stopMarket:{sl_id}) ReduceOnly")
                                open_sell[sym] = type("OT",(object,),{"symbol":sym,"side":"Sell","qty":qty,"entry":entry_r,"sl":sl_r,"tp":tp_r,"entry_order_id":entry_id,"sl_order_id":sl_id,"tp_order_id":tp_id,"api_key":API_KEY_MAIN,"api_secret":API_SECRET_MAIN})()
                                last_result["main"][sym] = None
                            except Exception as e:
                                logging.error(f"{sym} SELL order error: {e}")

                        sell_level[sym] = None

        # profit siphon check
        try:
            siphon_profits_if_needed(API_KEY_MAIN, API_SECRET_MAIN, API_KEY_SUB, API_SECRET_SUB, SUB_UID, PROFIT_UID, siphon_state)
        except Exception as e:
            logging.warning(f"Siphon check failed: {e}")

        wait_until_next_5m()

if __name__ == "__main__":
    main()
   
