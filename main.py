
# Full edited bot: 1H (levels) + 5m (triggers) + trade execution + pre-trade rebalance + profit siphon
import os
import hmac
import hashlib
import time
import json
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

URL = MARKET_URL + "/kline"
SYMBOL = os.getenv("SYMBOL", "TRXUSDT")  # change if needed
CATEGORY = "linear"               # USDT perpetuals
TIMEOUT = 15                      # HTTP timeout (seconds)

# -------- Strategy Config --------
FIVE_M_HISTORY = 500              # keep HA candles for context
RR_EXTRA = Decimal("0.0007")       # +0.5% of entry price
LEVERAGE = Decimal("75")          # 75x leverage
RISK_FRACTION = Decimal("0.10")   # 10% of combined balance risk
BAL_CAP = Decimal("0.90")         # cap to 90% of that account balance (for margin)

# -------- API keys (set your real keys or environment vars) --------
# MAIN account (used for SELL) — also used as master for transfers
API_KEY_MAIN    = os.getenv("BYBIT_MAIN_KEY",    "PUT_MAIN_KEY_HERE")
API_SECRET_MAIN = os.getenv("BYBIT_MAIN_SECRET", "PUT_MAIN_SECRET_HERE")

# SUB account (used for BUY)
API_KEY_SUB     = os.getenv("BYBIT_SUB_KEY",     "PUT_SUB_KEY_HERE")
API_SECRET_SUB  = os.getenv("BYBIT_SUB_SECRET",  "PUT_SUB_SECRET_HERE")

# For main<->sub rebalancing (equalize after trades)
SUB_UID = os.getenv("BYBIT_SUB_UID", "PUT_SUB_UID_HERE")  # numeric string of your trading SUB

# ---- Profit siphon (new) ----
# Destination "vault" sub-account UID (where profit is sent)
PROFIT_UID = os.getenv("BYBIT_PROFIT_UID", "PUT_PROFIT_UID_HERE")   # numeric string

# Starting milestone in USD for doubling rule (default $10)
SIPHON_BASE_USD = Decimal(os.getenv("BYBIT_SIPHON_BASE_USD", "10"))

# =========================
# Helpers: HA & scheduling
# =========================
def get_candles(symbol=SYMBOL, interval="60", limit=200):
    """Fetch candles from Bybit (newest first)."""
    params = {
        "category": CATEGORY,
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }
    r = requests.get(URL, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if "result" not in data or "list" not in data["result"]:
        raise RuntimeError(f"Bad response for interval {interval}: {data}")
    return data["result"]["list"]  # newest first

def to_heikin_ashi(raw):
    """
    Convert raw candles (newest first) to Heikin Ashi tuples in CHRONO order:
    (ha_open, ha_high, ha_low, ha_close, color, ts_ms)
    Uses Decimal arithmetic to avoid mixing float and Decimal.
    """
    ha = []
    D2 = Decimal("2")
    D4 = Decimal("4")
    for i, c in enumerate(reversed(raw)):  # chronological
        ts = int(c[0])
        # use Decimal for all OHLC values (avoid float)
        o = Decimal(str(c[1]))
        h = Decimal(str(c[2]))
        l = Decimal(str(c[3]))
        cl = Decimal(str(c[4]))

        ha_close = (o + h + l + cl) / D4
        if i == 0:
            ha_open = (o + cl) / D2
        else:
            p_open, _, _, p_close, _, _ = ha[-1]  # p_open, p_close are Decimal
            ha_open = (p_open + p_close) / D2

        ha_high = max(h, ha_open, ha_close)
        ha_low  = min(l, ha_open, ha_close)
        color = "GREEN" if ha_close >= ha_open else "RED"

        ha.append((ha_open, ha_high, ha_low, ha_close, color, ts))
    return ha

def wait_until_next_5m():
    """Block until the next exact 5-minute mark (UTC)."""
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
        headers = {
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN": sign,
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

# =========================
# Instrument precision
# =========================
def get_instrument_info():
    params = {"category": CATEGORY, "symbol": SYMBOL}
    r = requests.get(MARKET_URL + "/instruments-info", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"instruments-info error: {data}")
    info = data["result"]["list"][0]
    tick = Decimal(info["priceFilter"]["tickSize"])
    step = Decimal(info["lotSizeFilter"]["qtyStep"])
    min_qty = Decimal(info["lotSizeFilter"].get("minOrderQty", "0"))
    return tick, step, min_qty

def round_price(px: Decimal, tick: Decimal) -> Decimal:
    # round down to tick
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
    if not lst: return Decimal("0")
    return Decimal(str(lst[0]["totalEquity"]))

def universal_transfer_main_to_uid(api_key_master, api_secret_master, amount_usdt: Decimal, to_uid: str):
    body = {
        "transferId": str(int(time.time()*1000)),
        "coin": "USDT",
        "amount": str(amount_usdt.quantize(Decimal('0.01'), rounding=ROUND_DOWN)),
        "fromAccountType": "UNIFIED",
        "toAccountType": "UNIFIED",
        "toMemberId": to_uid
    }
    try:
        private_request(api_key_master, api_secret_master, "POST", "/v5/asset/transfer", body=body)
        logging.info(f"🔁 Transfer MAIN -> UID({to_uid}) {body['amount']} USDT")
        return True
    except Exception as e:
        logging.warning(f"Transfer MAIN->UID({to_uid}) failed: {e}")
        return False

def universal_transfer_sub_to_uid(api_key_master, api_secret_master, amount_usdt: Decimal, from_sub_uid: str, to_uid: str):
    body = {
        "transferId": str(int(time.time()*1000)),
        "coin": "USDT",
        "amount": str(amount_usdt.quantize(Decimal('0.01'), rounding=ROUND_DOWN)),
        "fromAccountType": "UNIFIED",
        "toAccountType": "UNIFIED",
        "fromMemberId": from_sub_uid,
        "toMemberId": to_uid
    }
    try:
        private_request(api_key_master, api_secret_master, "POST", "/v5/asset/transfer", body=body)
        logging.info(f"🔁 Transfer SUB({from_sub_uid}) -> UID({to_uid}) {body['amount']} USDT")
        return True
    except Exception as e:
        logging.warning(f"Transfer SUB({from_sub_uid})->UID({to_uid}) failed: {e}")
        return False

def universal_transfer_main_to_sub(api_key_master, api_secret_master, amount_usdt, to_sub_uid):
    body = {
        "transferId": str(int(time.time()*1000)),
        "coin": "USDT",
        "amount": str(amount_usdt),
        "fromAccountType": "UNIFIED",
        "toAccountType": "UNIFIED",
        "toMemberId": to_sub_uid
    }
    try:
        private_request(api_key_master, api_secret_master, "POST", "/v5/asset/transfer", body=body)
        logging.info(f"🔁 Transfer MAIN -> SUB {amount_usdt} USDT")
        return True
    except Exception as e:
        logging.warning(f"Transfer MAIN->SUB failed: {e}")
        return False

def universal_transfer_sub_to_main(api_key_master, api_secret_master, amount_usdt, from_sub_uid):
    body = {
        "transferId": str(int(time.time()*1000)),
        "coin": "USDT",
        "amount": str(amount_usdt),
        "fromAccountType": "UNIFIED",
        "toAccountType": "UNIFIED",
        "fromMemberId": from_sub_uid
    }
    try:
        private_request(api_key_master, api_secret_master, "POST", "/v5/asset/transfer", body=body)
        logging.info(f"🔁 Transfer SUB -> MAIN {amount_usdt} USDT")
        return True
    except Exception as e:
        logging.warning(f"Transfer SUB->MAIN failed: {e}")
        return False

def rebalance_equal(api_key_main, api_secret_main, api_key_sub, api_secret_sub, sub_uid):
    """
    Make main and sub balances equal by moving funds one way.
    """
    try:
        bal_main = get_wallet_balance(api_key_main, api_secret_main)
        bal_sub  = get_wallet_balance(api_key_sub,  api_secret_sub)
        total = bal_main + bal_sub
        target_each = total / Decimal("2")
        delta_main = target_each - bal_main
        if abs(delta_main) < Decimal("0.5"):  # ignore tiny
            return
        amt = abs(delta_main)
        if delta_main > 0:
            # move from sub -> main
            if sub_uid and sub_uid != "PUT_SUB_UID_HERE":
                universal_transfer_sub_to_main(api_key_main, api_secret_main, float(amt), sub_uid)
        else:
            # move from main -> sub
            if sub_uid and sub_uid != "PUT_SUB_UID_HERE":
                universal_transfer_main_to_sub(api_key_main, api_secret_main, float(amt), sub_uid)
    except Exception as e:
        logging.warning(f"Rebalance equal failed: {e}")

# ---- Profit siphon helper (new) ----
def siphon_profits_if_needed(api_key_main, api_secret_main, api_key_sub, api_secret_sub,
                             sub_uid: str, dest_uid: str, milestone_ref: dict):
    """
    When combined balance >= 2 * milestone, send 25% of combined to dest_uid.
    Pull from MAIN first, then SUB for any remainder. Update milestone to remaining total.
    `milestone_ref` is a dict holding {'value': Decimal}
    """
    if not dest_uid or dest_uid == "PUT_PROFIT_UID_HERE":
        return  # not configured
    # balances
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
        logging.info(f"💰 Siphon trigger! Combined=${total:.2f} ≥ ${trigger:.2f}. Sending 25% = ${send_amt} to UID({dest_uid})")

        remaining = send_amt

        # Send from MAIN first
        take_main = min(bal_main, remaining)
        if take_main > 0:
            ok = universal_transfer_main_to_uid(api_key_main, api_secret_main, take_main, dest_uid)
            if ok:
                remaining -= take_main

        # Then from SUB if needed
        if remaining > 0 and sub_uid and sub_uid != "PUT_SUB_UID_HERE":
            take_sub = min(bal_sub, remaining)
            if take_sub > 0:
                universal_transfer_sub_to_uid(api_key_main, api_secret_main, take_sub, sub_uid, dest_uid)
                remaining -= take_sub

        # Refresh balances and set new milestone to what remains in main+sub
        bal_main2 = get_wallet_balance(api_key_main, api_secret_main)
        bal_sub2  = get_wallet_balance(api_key_sub, api_secret_sub)
        new_total = bal_main2 + bal_sub2
        milestone_ref["value"] = new_total
        logging.info(f"✅ Siphon done. New milestone set to current combined balance = ${new_total:.2f}. Next trigger at ${ (new_total*2):.2f }.")

# =========================
# Orders
# =========================
def place_market_order(api_key, api_secret, side, qty):
    body = {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "side": side,                   # "Buy" or "Sell"
        "orderType": "Market",
        "qty": str(qty),
        "timeInForce": "IOC",
    }
    data = private_request(api_key, api_secret, "POST", "/v5/order/create", body=body)
    return data["result"]["orderId"]

def place_reduce_only_tp(api_key, api_secret, side, tp_price, qty):
    opp = "Sell" if side == "Buy" else "Buy"
    body = {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "side": opp,
        "orderType": "Limit",
        "price": str(tp_price),
        "qty": str(qty),
        "reduceOnly": True,
        "timeInForce": "GTC"
    }
    data = private_request(api_key, api_secret, "POST", "/v5/order/create", body=body)
    return data["result"]["orderId"]

def place_reduce_only_sl(api_key, api_secret, side, sl_trigger, qty):
    opp = "Sell" if side == "Buy" else "Buy"
    # Stop-Market reduceOnly with triggerPrice
    body = {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "side": opp,
        "orderType": "Market",
        "qty": str(qty),
        "reduceOnly": True,
        "timeInForce": "GTC",
        "triggerBy": "LastPrice",
        "triggerPrice": str(sl_trigger),
        "triggerDirection": 1 if opp=="Buy" else 2  # heuristic
    }
    data = private_request(api_key, api_secret, "POST", "/v5/order/create", body=body)
    return data["result"]["orderId"]

def amend_order(api_key, api_secret, order_id, price=None, triggerPrice=None):
    body = {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "orderId": order_id
    }
    if price is not None:
        body["price"] = str(price)
    if triggerPrice is not None:
        body["triggerPrice"] = str(triggerPrice)
    private_request(api_key, api_secret, "POST", "/v5/order/amend", body=body)

# =========================
# Strategy state
# =========================
class ActiveLevel:
    def __init__(self, price: Decimal, expiry: datetime):
        self.price  = Decimal(str(price))
        self.expiry = expiry

class OpenTrade:
    """
    Track one open trade per side. Hedging allowed (both BUY and SELL may exist).
    """
    def __init__(self, side: str, qty: Decimal, entry: Decimal, sl_price: Decimal, tp_price: Decimal,
                 entry_order_id: str, sl_order_id: str, tp_order_id: str,
                 api_key: str, api_secret: str):
        self.side = side
        self.qty = qty
        self.entry = entry
        self.sl = sl_price
        self.tp = tp_price
        self.entry_order_id = entry_order_id
        self.sl_order_id = sl_order_id
        self.tp_order_id = tp_order_id
        self.api_key = api_key
        self.api_secret = api_secret

# =========================
# Position sizing
# =========================
def compute_qty(entry: Decimal, sl: Decimal, trading_bal: Decimal, combined_bal: Decimal,
                lot_step: Decimal, min_qty: Decimal) -> Decimal:
    """
    Risk 10% of combined balance with 75x leverage, but cap so initial margin uses <= 90% of trading balance.
    qty_risk = (risk$ / per-coin-risk)
    per-coin-risk = |entry - sl|
    margin per coin ≈ entry / leverage
    qty_cap_by_margin = (0.9 * trading_bal * leverage) / entry
    """
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

# =========================
# MAIN BOT
# =========================
def main():
    # precision
    tick, lot_step, min_qty = get_instrument_info()
    logging.info(f"Instrument: tick={tick} lotStep={lot_step} minQty={min_qty}")

    buy_level = None    # ActiveLevel or None
    sell_level = None
    last_hour_processed = None

    # open trades state (hedging allowed)
    open_buy: OpenTrade = None
    open_sell: OpenTrade = None

    # history deque (not needed for SL now, but keep if you want to inspect)
    ha_5m_history = deque(maxlen=FIVE_M_HISTORY)

    # profit siphon milestone (persist in memory across loop)
    siphon_state = {"value": None}

    while True:
        now = datetime.utcnow()

        # ---- HOURLY LOGIC at exact top of hour ----
        if now.minute == 0 and now.second == 0:
            hour_tag = now.replace(minute=0, second=0, microsecond=0)
            if last_hour_processed != hour_tag:
                last_hour_processed = hour_tag
                try:
                    raw_1h = get_candles(interval="60", limit=3)
                    ha_1h = to_heikin_ashi(raw_1h)
                    if not ha_1h:
                        raise RuntimeError("No 1H HA data")
                    last_1h = ha_1h[-1]
                except Exception as e:
                    logging.error(f"1H fetch/convert error: {e}")
                else:
                    _, h1, l1, c1, col1, _ = last_1h
                    logging.info(f"1H closed: {col1} | HA-H={h1} L={l1} C={c1}")

                    # If RED -> set Buy Level to HA High (valid 1h), cancel sell level arming
                    if col1 == "RED":
                        buy_level = ActiveLevel(price=h1, expiry=now + timedelta(hours=1))
                        sell_level = None
                        logging.info(f"🎯 Buy Level set @ {buy_level.price} (valid until {buy_level.expiry} UTC)")
                    else:
                        # GREEN -> set Sell Level to HA Low (valid 1h), cancel buy level arming
                        sell_level = ActiveLevel(price=l1, expiry=now + timedelta(hours=1))
                        buy_level = None
                        logging.info(f"🎯 Sell Level set @ {sell_level.price} (valid until {sell_level.expiry} UTC)")

        # ---- EVERY 5 MINUTES at exact :00/:05/:10/... to check 5m candle ----
        try:
            raw_5m = get_candles(interval="5", limit=3)
            ha_5m = to_heikin_ashi(raw_5m)
            if ha_5m:
                ha_5m_history.append(ha_5m[-1])
            last_5m = ha_5m[-1]
            _, h5, l5, c5, col5, ts5 = last_5m
        except Exception as e:
            logging.error(f"5M fetch/convert error: {e}")
            wait_until_next_5m()
            continue

        # convert to Decimal (they are already Decimal from to_heikin_ashi, but keep for safety)
        h5 = Decimal(str(h5)); l5 = Decimal(str(l5)); c5 = Decimal(str(c5))

        # -------- BUY logic (can run even if a SELL is open) --------
        if buy_level is not None:
            if datetime.utcnow() >= buy_level.expiry:
                logging.info("⌛ Buy Level expired")
                buy_level = None
            else:
                # trigger = 5m HA high >= Buy level
                if h5 >= buy_level.price:
                    # Entry = close of the signal candle; SL = low of the signal candle; TP = 1:1 + 0.5% of entry
                    entry = c5
                    sl    = l5
                    if sl >= entry:
                        logging.info("⚠️ Invalid BUY (SL >= entry); skipping")
                    else:
                        risk = entry - sl
                        tp   = entry + risk + (entry * RR_EXTRA)

                        # round to precision
                        entry_r = round_price(entry, tick)
                        sl_r    = round_price(sl,    tick)
                        tp_r    = round_price(tp,    tick)

                        # PRE-TRADE: rebalance main/sub before sizing (user requested)
                        try:
                            rebalance_equal(API_KEY_MAIN, API_SECRET_MAIN, API_KEY_SUB, API_SECRET_SUB, SUB_UID)
                        except Exception as e:
                            logging.warning(f"Pre-trade rebalance failed: {e}")

                        # Balances after rebalance
                        bal_main = get_wallet_balance(API_KEY_MAIN, API_SECRET_MAIN)
                        bal_sub  = get_wallet_balance(API_KEY_SUB,  API_SECRET_SUB)
                        combined = bal_main + bal_sub

                        # Sizing from SUB (BUY)
                        qty = compute_qty(entry_r, sl_r, bal_sub, combined, lot_step, min_qty)
                        if qty <= 0:
                            logging.info("⚠️ BUY sizing too small; skipping")
                        else:
                            # If a same-side BUY is already open: update existing SL only (don't open additional market position)
                            if open_buy is not None:
                                try:
                                    amend_order(API_KEY_SUB, API_SECRET_SUB, open_buy.sl_order_id, triggerPrice=sl_r)
                                    open_buy.sl = sl_r
                                    logging.info(f"🔧 Amended existing BUY SL to {sl_r}")
                                except Exception as e:
                                    logging.warning(f"Amend existing BUY SL failed: {e}")
                            else:
                                logging.info(f"✅ BUY signal | Entry={entry_r} SL={sl_r} TP={tp_r} | qty={qty}")
                                # Place market BUY, then reduce-only TP/SL on SUB
                                try:
                                    entry_id = place_market_order(API_KEY_SUB, API_SECRET_SUB, "Buy", qty)
                                    tp_id = place_reduce_only_tp(API_KEY_SUB, API_SECRET_SUB, "Buy", tp_r, qty)
                                    sl_id = place_reduce_only_sl(API_KEY_SUB, API_SECRET_SUB, "Buy", sl_r, qty)
                                    logging.info(f"📦 BUY opened (orderId={entry_id}); TP({tp_id}) & SL({sl_id}) placed")
                                    open_buy = OpenTrade("Buy", qty, entry_r, sl_r, tp_r, entry_id, sl_id, tp_id,
                                                         API_KEY_SUB, API_SECRET_SUB)
                                except Exception as e:
                                    logging.error(f"BUY order error: {e}")
                                finally:
                                    # After trade: rebalance main/sub to equal balances
                                    try:
                                        rebalance_equal(API_KEY_MAIN, API_SECRET_MAIN, API_KEY_SUB, API_SECRET_SUB, SUB_UID)
                                    except Exception as e:
                                        logging.warning(f"Post-trade rebalance failed: {e}")
                            buy_level = None  # clear armed level

        # -------- SELL logic (can run even if a BUY is open) --------
        if sell_level is not None:
            if datetime.utcnow() >= sell_level.expiry:
                logging.info("⌛ Sell Level expired")
                sell_level = None
            else:
                # trigger = 5m HA low <= Sell level
                if l5 <= sell_level.price:
                    entry = c5
                    sl    = h5
                    if sl <= entry:
                        logging.info("⚠️ Invalid SELL (SL <= entry); skipping")
                    else:
                        risk = sl - entry
                        tp   = entry - risk - (entry * RR_EXTRA)

                        entry_r = round_price(entry, tick)
                        sl_r    = round_price(sl,    tick)
                        tp_r    = round_price(tp,    tick)

                        # PRE-TRADE: rebalance main/sub before sizing
                        try:
                            rebalance_equal(API_KEY_MAIN, API_SECRET_MAIN, API_KEY_SUB, API_SECRET_SUB, SUB_UID)
                        except Exception as e:
                            logging.warning(f"Pre-trade rebalance failed: {e}")

                        bal_main = get_wallet_balance(API_KEY_MAIN, API_SECRET_MAIN)
                        bal_sub  = get_wallet_balance(API_KEY_SUB,  API_SECRET_SUB)
                        combined = bal_main + bal_sub

                        # Sizing from MAIN (SELL)
                        qty = compute_qty(entry_r, sl_r, bal_main, combined, lot_step, min_qty)
                        if qty <= 0:
                            logging.info("⚠️ SELL sizing too small; skipping")
                        else:
                            # If same-side SELL already open: update existing SL only (don't open additional market position)
                            if open_sell is not None:
                                try:
                                    amend_order(API_KEY_MAIN, API_SECRET_MAIN, open_sell.sl_order_id, triggerPrice=sl_r)
                                    open_sell.sl = sl_r
                                    logging.info(f"🔧 Amended existing SELL SL to {sl_r}")
                                except Exception as e:
                                    logging.warning(f"Amend existing SELL SL failed: {e}")
                            else:
                                logging.info(f"✅ SELL signal | Entry={entry_r} SL={sl_r} TP={tp_r} | qty={qty}")
                                try:
                                    entry_id = place_market_order(API_KEY_MAIN, API_SECRET_MAIN, "Sell", qty)
                                    tp_id = place_reduce_only_tp(API_KEY_MAIN, API_SECRET_MAIN, "Sell", tp_r, qty)
                                    sl_id = place_reduce_only_sl(API_KEY_MAIN, API_SECRET_MAIN, "Sell", sl_r, qty)
                                    logging.info(f"📦 SELL opened (orderId={entry_id}); TP({tp_id}) & SL({sl_id}) placed")
                                    open_sell = OpenTrade("Sell", qty, entry_r, sl_r, tp_r, entry_id, sl_id, tp_id,
                                                           API_KEY_MAIN, API_SECRET_MAIN)
                                except Exception as e:
                                    logging.error(f"SELL order error: {e}")
                                finally:
                                    # After trade: rebalance main/sub to equal balances
                                    try:
                                        rebalance_equal(API_KEY_MAIN, API_SECRET_MAIN, API_KEY_SUB, API_SECRET_SUB, SUB_UID)
                                    except Exception as e:
                                        logging.warning(f"Post-trade rebalance failed: {e}")
                            sell_level = None

        # ---- Profit siphon check (runs every loop) ----
        try:
            siphon_profits_if_needed(API_KEY_MAIN, API_SECRET_MAIN,
                                     API_KEY_SUB, API_SECRET_SUB,
                                     SUB_UID, PROFIT_UID, siphon_state)
        except Exception as e:
            logging.warning(f"Siphon check failed: {e}")

        # align to next :00/:05/:10/...
        wait_until_next_5m()

if __name__ == "__main__":
    main()
