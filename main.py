# Full edited bot: 1H (levels) + 5m (triggers) + trade execution + pre-trade rebalance + profit siphon
# + multi-symbol (TRX & 1000BONK), per-account role, same-type replacement, ReduceOnly LIMIT TP/SL,
# SL-cross watchdog, UUID transferIds, 2:1+0.07% TP after loss
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

# -------- Public Market Config --------
BASE_URL = "https://api.bybit.com"
MARKET_URL = BASE_URL + "/v5/market"
ORDER_URL  = BASE_URL + "/v5/order"
ACCOUNT_URL= BASE_URL + "/v5/account"
ASSET_URL  = BASE_URL + "/v5/asset"

TIMEOUT = 15                      # HTTP timeout (seconds)
CATEGORY = "linear"               # USDT perpetuals

# -------- Symbols (multi) --------
# default changed to TRX and 1000BONK (per request)
SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", "TRXUSDT,1000BONKUSDT").split(",") if s.strip()]

# -------- Strategy Config --------
FIVE_M_HISTORY = 500               # keep HA candles for context
EXTRA_TP_PCT = Decimal("0.0007")   # +0.07% of entry price when using 2:1+extra
LEVERAGE = Decimal("75")
RISK_FRACTION = Decimal("0.10")    # 10% of combined balance risk
BAL_CAP = Decimal("0.90")          # cap to 90% of that account balance (for margin)

# -------- API keys (set your real keys or environment vars) --------
API_KEY_MAIN    = os.getenv("BYBIT_MAIN_KEY",    "PUT_MAIN_KEY_HERE")
API_SECRET_MAIN = os.getenv("BYBIT_MAIN_SECRET", "PUT_MAIN_SECRET_HERE")
API_KEY_SUB     = os.getenv("BYBIT_SUB_KEY",     "PUT_SUB_KEY_HERE")
API_SECRET_SUB  = os.getenv("BYBIT_SUB_SECRET",  "PUT_SUB_SECRET_HERE")
SUB_UID = os.getenv("BYBIT_SUB_UID", "PUT_SUB_UID_HERE")  # numeric string of your trading SUB

# ---- Profit siphon ----
PROFIT_UID = os.getenv("BYBIT_PROFIT_UID", "PUT_PROFIT_UID_HERE")   # numeric string
SIPHON_BASE_USD = Decimal(os.getenv("BYBIT_SIPHON_BASE_USD", "10"))

# =========================
# Helpers: HA & scheduling
# =========================
def market_kline_url():
    return MARKET_URL + "/kline"

def get_candles(symbol, interval="60", limit=200):
    params = {"category": CATEGORY, "symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(market_kline_url(), params=params, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if "result" not in data or "list" not in data["result"]:
        raise RuntimeError(f"Bad response for {symbol} interval {interval}: {data}")
    return data["result"]["list"]  # newest first

def to_heikin_ashi(raw):
    ha = []
    D2 = Decimal("2"); D4 = Decimal("4")
    for i, c in enumerate(reversed(raw)):  # chronological
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
# Private REST auth/sign
# =========================
def _ts_ms():
    return str(int(time.time() * 1000))

def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

def private_request(api_key, api_secret, method, path, params=None, body=None):
    if params is None: params = {}
    if body is None: body = {}
    url = BASE_URL + path
    ts = _ts_ms()
    recv_window = "5000"
    if method.upper() == "GET":
        query = "&".join([f"{k}={params[k]}" for k in sorted(params)]) if params else ""
        payload = ts + api_key + recv_window + query
        sign = _sign(api_secret, payload)
        headers = {"X-BAPI-API-KEY": api_key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": recv_window, "X-BAPI-SIGN": sign}
        r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
    else:
        query = "&".join([f"{k}={params[k]}" for k in sorted(params)]) if params else ""
        body_json = json.dumps(body) if body else ""
        payload = ts + api_key + recv_window + query + body_json
        sign = _sign(api_secret, payload)
        headers = {"X-BAPI-API-KEY": api_key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": recv_window, "X-BAPI-SIGN": sign, "Content-Type": "application/json"}
        r = requests.post(url, params=params, data=body_json, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit error {data.get('retCode')}: {data.get('retMsg')} | {data}")
    return data

# =========================
# Instrument precision
# =========================
def get_instrument_info(symbol):
    params = {"category": CATEGORY, "symbol": symbol}
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
# Balances & rebalancing
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

def universal_transfer_main_to_uid(api_key_master, api_secret_master, amount_usdt: Decimal, to_uid: str):
    body = {"transferId": _uuid(), "coin": "USDT", "amount": str(amount_usdt.quantize(Decimal('0.01'), rounding=ROUND_DOWN)), "fromAccountType": "UNIFIED", "toAccountType": "UNIFIED", "toMemberId": to_uid}
    try:
        private_request(api_key_master, api_secret_master, "POST", "/v5/asset/transfer", body=body)
        logging.info(f"🔁 Transfer MAIN -> UID({to_uid}) {body['amount']} USDT")
        return True
    except Exception as e:
        logging.warning(f"Transfer MAIN->UID({to_uid}) failed: {e}")
        return False

def universal_transfer_sub_to_uid(api_key_master, api_secret_master, amount_usdt: Decimal, from_sub_uid: str, to_uid: str):
    body = {"transferId": _uuid(), "coin": "USDT", "amount": str(amount_usdt.quantize(Decimal('0.01'), rounding=ROUND_DOWN)), "fromAccountType": "UNIFIED", "toAccountType": "UNIFIED", "fromMemberId": from_sub_uid, "toMemberId": to_uid}
    try:
        private_request(api_key_master, api_secret_master, "POST", "/v5/asset/transfer", body=body)
        logging.info(f"🔁 Transfer SUB({from_sub_uid}) -> UID({to_uid}) {body['amount']} USDT")
        return True
    except Exception as e:
        logging.warning(f"Transfer SUB({from_sub_uid})->UID({to_uid}) failed: {e}")
        return False

def universal_transfer_main_to_sub(api_key_master, api_secret_master, amount_usdt, to_sub_uid):
    body = {"transferId": _uuid(), "coin": "USDT", "amount": str(amount_usdt), "fromAccountType": "UNIFIED", "toAccountType": "UNIFIED", "toMemberId": to_sub_uid}
    try:
        private_request(api_key_master, api_secret_master, "POST", "/v5/asset/transfer/inter-transfer", body=body)
        return True
    except Exception as e:
        logging.warning(f"Transfer MAIN->SUB failed: {e}")
        return False

def universal_transfer_sub_to_main(api_key_master, api_secret_master, amount_usdt, from_sub_uid):
    body = {"transferId": _uuid(), "coin": "USDT", "amount": str(amount_usdt), "fromAccountType": "UNIFIED", "toAccountType": "UNIFIED", "fromMemberId": from_sub_uid}
    try:
        private_request(api_key_master, api_secret_master, "POST", "/v5/asset/transfer/inter-transfer", body=body)
        logging.info(f"🔁 Transfer SUB -> MAIN {amount_usdt} USDT")
        return True
    except Exception as e:
        logging.warning(f"Transfer SUB->MAIN failed: {e}")
        return False

def rebalance_equal(api_key_main, api_secret_main, api_key_sub, api_secret_sub, sub_uid):
    try:
        bal_main = get_wallet_balance(api_key_main, api_secret_main)
        bal_sub  = get_wallet_balance(api_key_sub,  api_secret_sub)
        total = bal_main + bal_sub
        target_each = total / Decimal("2")
        delta_main = target_each - bal_main
        if abs(delta_main) < Decimal("0.05"):
            return
        amt = abs(delta_main)
        if delta_main > 0:
            if sub_uid and sub_uid != "PUT_SUB_UID_HERE":
                universal_transfer_sub_to_main(api_key_main, api_secret_main, float(amt), sub_uid)
        else:
            if sub_uid and sub_uid != "PUT_SUB_UID_HERE":
                universal_transfer_main_to_sub(api_key_main, api_secret_main, float(amt), sub_uid)
    except Exception as e:
        logging.warning(f"Rebalance equal failed: {e}")

# ---- Profit siphon helper ----
def siphon_profits_if_needed(api_key_main, api_secret_main, api_key_sub, api_secret_sub, sub_uid: str, dest_uid: str, milestone_ref: dict):
    if not dest_uid or dest_uid == "PUT_PROFIT_UID_HERE":
        return
    bal_main = get_wallet_balance(api_key_main, api_secret_main)
    bal_sub  = get_wallet_balance(api_key_sub,  api_secret_sub)
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
# Orders & helpers
# =========================
def place_market_order(api_key, api_secret, symbol, side, qty):
    body = {"category": CATEGORY, "symbol": symbol, "side": side, "orderType": "Market", "qty": str(qty), "timeInForce": "IOC"}
    data = private_request(api_key, api_secret, "POST", "/v5/order/create", body=body)
    logging.info(f"Placing MARKET {side} {symbol} qty={qty} -> {data}")
    return data.get("result", {}).get("orderId")

def place_reduce_only_tp(api_key, api_secret, symbol, side, tp_price, qty):
    opp = "Sell" if side == "Buy" else "Buy"
    body = {"category": CATEGORY, "symbol": symbol, "side": opp, "orderType": "Limit", "price": str(tp_price), "qty": str(qty), "reduceOnly": True, "timeInForce": "GTC"}
    data = private_request(api_key, api_secret, "POST", "/v5/order/create", body=body)
    logging.info(f"Placing TP LIMIT ReduceOnly {opp} {symbol} qty={qty} price={tp_price} -> {data}")
    return data.get("result", {}).get("orderId")

def place_reduce_only_sl_limit(api_key, api_secret, symbol, side, sl_price, qty):
    opp = "Sell" if side == "Buy" else "Buy"
    body = {"category": CATEGORY, "symbol": symbol, "side": opp, "orderType": "Limit", "price": str(sl_price), "qty": str(qty), "reduceOnly": True, "timeInForce": "GTC"}
    data = private_request(api_key, api_secret, "POST", "/v5/order/create", body=body)
    logging.info(f"Placing SL LIMIT ReduceOnly {opp} {symbol} qty={qty} price={sl_price} -> {data}")
    return data.get("result", {}).get("orderId")

def cancel_all_orders_for_symbol(api_key, api_secret, symbol):
    try:
        private_request(api_key, api_secret, "POST", "/v5/order/cancel-all", body={"category": CATEGORY, "symbol": symbol})
        logging.info(f"🧹 Cancelled all orders on this account for {symbol}.")
    except Exception as e:
        logging.warning(f"Cancel orders failed for {symbol}: {e}")

def market_close_all_positions_for_symbol(api_key, api_secret, symbol):
    try:
        pos_data = private_request(api_key, api_secret, "GET", "/v5/position/list", params={"category": CATEGORY, "symbol": symbol})
        for pos in pos_data["result"]["list"]:
            side = pos["side"]
            size = Decimal(pos["size"])
            if size > 0:
                opp = "Sell" if side == "Buy" else "Buy"
                private_request(api_key, api_secret, "POST", "/v5/order/create", body={"category": CATEGORY, "symbol": symbol, "side": opp, "orderType": "Market", "qty": str(size), "reduceOnly": True, "timeInForce": "IOC"})
                logging.info(f"🔒 Closed {side} position of {size} {symbol}.")
    except Exception as e:
        logging.warning(f"Close positions failed for {symbol}: {e}")

def close_account_symbol(api_key, api_secret, symbol):
    cancel_all_orders_for_symbol(api_key, api_secret, symbol)
    market_close_all_positions_for_symbol(api_key, api_secret, symbol)

def compute_qty(entry: Decimal, sl: Decimal, trading_bal: Decimal, combined_bal: Decimal, lot_step: Decimal, min_qty: Decimal) -> Decimal:
    per_coin_risk = abs(entry - sl)
    if per_coin_risk <= Decimal("0"):
        return Decimal("0")
    risk_usd = combined_bal * RISK_FRACTION
    qty_risk = (risk_usd / per_coin_risk)
    qty_cap  = (trading_bal * BAL_CAP * LEVERAGE) / entry
    qty = min(qty_risk, qty_cap)
    qty = round_qty(qty, lot_step)
    if qty < min_qty:
        return Decimal("0")
    return qty

def fallback_qty_by_account_balance(entry: Decimal, trading_bal: Decimal, lot_step: Decimal, min_qty: Decimal) -> Decimal:
    # fallback uses 90% of trading balance * leverage
    if trading_bal <= 0 or entry <= 0:
        return Decimal("0")
    qty_cap = (trading_bal * Decimal("0.90") * LEVERAGE) / entry
    qty_cap = round_qty(qty_cap, lot_step)
    if qty_cap < min_qty:
        return Decimal("0")
    return qty_cap

def try_place_market_order_with_fallback(api_key, api_secret, symbol, side, qty, entry_price, trading_bal, lot_step, min_qty):
    """
    Tries to place market order with 'qty'. If it fails due to insufficient funds, compute fallback qty
    equal to what 90% of trading_bal can support and retry once.
    """
    try:
        return place_market_order(api_key, api_secret, symbol, side, qty)
    except Exception as e:
        logging.warning(f"{symbol} initial market order failed: {e}. Attempting fallback sizing based on 90% of account balance.")
        fallback_qty = fallback_qty_by_account_balance(entry_price, trading_bal, lot_step, min_qty)
        if fallback_qty <= 0:
            logging.info(f"{symbol} fallback qty too small ({fallback_qty}) -> skipping trade.")
            raise
        if fallback_qty >= qty:
            # Fallback wouldn't reduce the size (rare) — just re-raise original error
            logging.info(f"{symbol} fallback qty {fallback_qty} >= original qty {qty}, re-raising.")
            raise
        try:
            logging.info(f"{symbol} retrying MARKET order with fallback qty={fallback_qty}")
            return place_market_order(api_key, api_secret, symbol, side, fallback_qty)
        except Exception as e2:
            logging.error(f"{symbol} fallback market order also failed: {e2}")
            raise

# =========================
# MAIN BOT
# =========================
def main():
    # per-symbol precision cache
    precision = {}
    for sym in SYMBOLS:
        tick, lot_step, min_qty = get_instrument_info(sym)
        precision[sym] = {"tick": tick, "lot": lot_step, "min": min_qty}
        logging.info(f"{sym}: tick={tick} lotStep={lot_step} minQty={min_qty}")

    # state per symbol
    buy_level   = {sym: None for sym in SYMBOLS}
    sell_level  = {sym: None for sym in SYMBOLS}
    last_hour_processed = None

    # open trades (per symbol)
    open_buy  = {sym: None for sym in SYMBOLS}      # sub account only
    open_sell = {sym: None for sym in SYMBOLS}      # main account only

    ha_5m_history = {sym: deque(maxlen=FIVE_M_HISTORY) for sym in SYMBOLS}
    siphon_state = {"value": None}

    last_result = {"sub": {sym: None for sym in SYMBOLS}, "main": {sym: None for sym in SYMBOLS}}

    while True:
        now = datetime.utcnow()

        # hourly arms
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
                        last_1h = ha_1h[-1]
                    except Exception as e:
                        logging.error(f"{sym} 1H fetch/convert error: {e}")
                        continue
                    _, h1, l1, c1, col1, _ = last_1h
                    logging.info(f"{sym} 1H closed: {col1} | HA-H={h1} L={l1} C={c1}")
                    if col1 == "RED":
                        buy_level[sym]  = type("A",(object,),{"price":Decimal(str(h1)),"expiry":now + timedelta(hours=1)})()
                        sell_level[sym] = None
                        logging.info(f"{sym} 🎯 Buy Level set @ {buy_level[sym].price} (valid 1h)")
                    else:
                        sell_level[sym] = type("A",(object,),{"price":Decimal(str(l1)),"expiry":now + timedelta(hours=1)})()
                        buy_level[sym]  = None
                        logging.info(f"{sym} 🎯 Sell Level set @ {sell_level[sym].price} (valid 1h)")

        # every 5m per symbol
        for sym in SYMBOLS:
            tick = precision[sym]["tick"]; lot = precision[sym]["lot"]; min_qty = precision[sym]["min"]

            try:
                raw_5m = get_candles(sym, interval="5", limit=3)
                ha_5m = to_heikin_ashi(raw_5m)
                if ha_5m:
                    ha_5m_history[sym].append(ha_5m[-1])
                last_5m = ha_5m[-1]
                _, h5, l5, ha_close, col5, ts5 = last_5m
                raw_latest = raw_5m[0]
                raw_close = Decimal(str(raw_latest[4]))
                c5 = raw_close
            except Exception as e:
                logging.error(f"{sym} 5M fetch/convert error: {e}")
                continue

            h5 = Decimal(str(h5)); l5 = Decimal(str(l5)); c5 = Decimal(str(c5))

            # watchdog: SL crossed but position still open? close for affected account only
            ob = open_buy[sym]
            if ob is not None and l5 <= ob.sl:
                logging.info(f"🛡️ {sym} BUY SL crossed (low {l5} <= SL {ob.sl}); force-closing SUB {sym}.")
                close_account_symbol(API_KEY_SUB, API_SECRET_SUB, sym)
                open_buy[sym] = None
                last_result["sub"][sym] = "loss"

            osell = open_sell[sym]
            if osell is not None and h5 >= osell.sl:
                logging.info(f"🛡️ {sym} SELL SL crossed (high {h5} >= SL {osell.sl}); force-closing MAIN {sym}.")
                close_account_symbol(API_KEY_MAIN, API_SECRET_MAIN, sym)
                open_sell[sym] = None
                last_result["main"][sym] = "loss"

            # BUY (SUB account only)
            if buy_level[sym] is not None:
                if datetime.utcnow() >= buy_level[sym].expiry:
                    logging.info(f"{sym} ⌛ Buy Level expired")
                    buy_level[sym] = None
                else:
                    if h5 >= buy_level[sym].price:
                        entry = c5; sl = l5
                        if sl >= entry:
                            logging.info(f"{sym} ⚠️ Invalid BUY (SL >= entry); skipping")
                        else:
                            risk = entry - sl
                            use_2to1_plus = (last_result["sub"][sym] == "loss")
                            tp = (entry + (risk*Decimal("2")) + (entry * EXTRA_TP_PCT)) if use_2to1_plus else (entry + risk)
                            entry_r = round_price(entry, tick); sl_r = round_price(sl, tick); tp_r = round_price(tp, tick)

                            try:
                                rebalance_equal(API_KEY_MAIN, API_SECRET_MAIN, API_KEY_SUB, API_SECRET_SUB, SUB_UID)
                            except Exception as e:
                                logging.warning(f"{sym} Pre-trade rebalance failed: {e}")

                            bal_main = get_wallet_balance(API_KEY_MAIN, API_SECRET_MAIN)
                            bal_sub  = get_wallet_balance(API_KEY_SUB,  API_SECRET_SUB)
                            combined = bal_main + bal_sub

                            qty = compute_qty(entry_r, sl_r, bal_sub, combined, lot, min_qty)
                            if qty <= 0:
                                # try fallback by 90% of sub account balance
                                fallback = fallback_qty_by_account_balance(entry_r, bal_sub, lot, min_qty)
                                if fallback <= 0:
                                    logging.info(f"{sym} ⚠️ BUY sizing too small even after fallback; skipping")
                                    buy_level[sym] = None
                                    continue
                                qty = fallback
                                logging.info(f"{sym} Using fallback BUY qty based on 90% sub balance: {qty}")

                            # If same-type BUY open on SUB for this symbol: close only SUB's symbol orders/positions first then open new
                            if open_buy[sym] is not None:
                                logging.info(f"{sym} 🔁 New BUY signal while BUY open on SUB -> closing existing SUB {sym} orders/position first.")
                                close_account_symbol(API_KEY_SUB, API_SECRET_SUB, sym)
                                open_buy[sym] = None

                            logging.info(f"{sym} ✅ BUY signal | Entry={entry_r} SL={sl_r} TP={tp_r} | qty={qty}")
                            try:
                                # attempt market order, if fails due to insufficient funds retry with 90% fallback qty
                                entry_id = try_place_market_order_with_fallback(API_KEY_SUB, API_SECRET_SUB, sym, "Buy", qty, entry_r, get_wallet_balance(API_KEY_SUB, API_SECRET_SUB), lot, min_qty)
                                tp_id = place_reduce_only_tp(API_KEY_SUB, API_SECRET_SUB, sym, "Buy", tp_r, qty)
                                sl_id = place_reduce_only_sl_limit(API_KEY_SUB, API_SECRET_SUB, sym, "Buy", sl_r, qty)
                                logging.info(f"{sym} 📦 BUY opened (orderId={entry_id}); TP({tp_id}) & SL({sl_id}) LIMIT-ReduceOnly")
                                open_buy[sym] = type("OT",(object,),{"symbol":sym,"side":"Buy","qty":qty,"entry":entry_r,"sl":sl_r,"tp":tp_r,"entry_order_id":entry_id,"sl_order_id":sl_id,"tp_order_id":tp_id,"api_key":API_KEY_SUB,"api_secret":API_SECRET_SUB})()
                                last_result["sub"][sym] = None
                            except Exception as e:
                                logging.error(f"{sym} BUY order error: {e}")
                            finally:
                                try:
                                    rebalance_equal(API_KEY_MAIN, API_SECRET_MAIN, API_KEY_SUB, API_SECRET_SUB, SUB_UID)
                                except Exception as e:
                                    logging.warning(f"{sym} Post-trade rebalance failed: {e}")

                        buy_level[sym] = None

            # SELL (MAIN account only)
            if sell_level[sym] is not None:
                if datetime.utcnow() >= sell_level[sym].expiry:
                    logging.info(f"{sym} ⌛ Sell Level expired")
                    sell_level[sym] = None
                else:
                    if l5 <= sell_level[sym].price:
                        entry = c5; sl = h5
                        if sl <= entry:
                            logging.info(f"{sym} ⚠️ Invalid SELL (SL <= entry); skipping")
                        else:
                            risk = sl - entry
                            use_2to1_plus = (last_result["main"][sym] == "loss")
                            tp = (entry - (risk*Decimal("2")) - (entry * EXTRA_TP_PCT)) if use_2to1_plus else (entry - risk)
                            entry_r = round_price(entry, tick); sl_r = round_price(sl, tick); tp_r = round_price(tp, tick)

                            try:
                                rebalance_equal(API_KEY_MAIN, API_SECRET_MAIN, API_KEY_SUB, API_SECRET_SUB, SUB_UID)
                            except Exception as e:
                                logging.warning(f"{sym} Pre-trade rebalance failed: {e}")

                            bal_main = get_wallet_balance(API_KEY_MAIN, API_SECRET_MAIN)
                            bal_sub  = get_wallet_balance(API_KEY_SUB, API_SECRET_SUB)
                            combined = bal_main + bal_sub

                            qty = compute_qty(entry_r, sl_r, bal_main, combined, lot, min_qty)
                            if qty <= 0:
                                fallback = fallback_qty_by_account_balance(entry_r, bal_main, lot, min_qty)
                                if fallback <= 0:
                                    logging.info(f"{sym} ⚠️ SELL sizing too small even after fallback; skipping")
                                    sell_level[sym] = None
                                    continue
                                qty = fallback
                                logging.info(f"{sym} Using fallback SELL qty based on 90% main balance: {qty}")

                            if open_sell[sym] is not None:
                                logging.info(f"{sym} 🔁 New SELL signal while SELL open on MAIN -> closing existing MAIN {sym} orders/position first.")
                                close_account_symbol(API_KEY_MAIN, API_SECRET_MAIN, sym)
                                open_sell[sym] = None

                            logging.info(f"{sym} ✅ SELL signal | Entry={entry_r} SL={sl_r} TP={tp_r} | qty={qty}")
                            try:
                                entry_id = try_place_market_order_with_fallback(API_KEY_MAIN, API_SECRET_MAIN, sym, "Sell", qty, entry_r, get_wallet_balance(API_KEY_MAIN, API_SECRET_MAIN), lot, min_qty)
                                tp_id = place_reduce_only_tp(API_KEY_MAIN, API_SECRET_MAIN, sym, "Sell", tp_r, qty)
                                sl_id = place_reduce_only_sl_limit(API_KEY_MAIN, API_SECRET_MAIN, sym, "Sell", sl_r, qty)
                                logging.info(f"{sym} 📦 SELL opened (orderId={entry_id}); TP({tp_id}) & SL({sl_id}) LIMIT-ReduceOnly")
                                open_sell[sym] = type("OT",(object,),{"symbol":sym,"side":"Sell","qty":qty,"entry":entry_r,"sl":sl_r,"tp":tp_r,"entry_order_id":entry_id,"sl_order_id":sl_id,"tp_order_id":tp_id,"api_key":API_KEY_MAIN,"api_secret":API_SECRET_MAIN})()
                                last_result["main"][sym] = None
                            except Exception as e:
                                logging.error(f"{sym} SELL order error: {e}")
                            finally:
                                try:
                                    rebalance_equal(API_KEY_MAIN, API_SECRET_MAIN, API_KEY_SUB, API_SECRET_SUB, SUB_UID)
                                except Exception as e:
                                    logging.warning(f"{sym} Post-trade rebalance failed: {e}")

                        sell_level[sym] = None

        # profit siphon
        try:
            siphon_profits_if_needed(API_KEY_MAIN, API_SECRET_MAIN, API_KEY_SUB, API_SECRET_SUB, SUB_UID, PROFIT_UID, siphon_state)
        except Exception as e:
            logging.warning(f"Siphon check failed: {e}")

        wait_until_next_5m()

if __name__ == "__main__":
    main()
