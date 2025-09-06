"""
Complete live Bybit hourly Heikin-Ashi trading bot (single-file).
Save as: bybit_hourly_ha_bot.py

Before running:
1. Install dependencies:
   pip install requests python-dotenv

2. Create a .env file in same folder with:
   BYBIT_API_KEY=your_key_here
   BYBIT_API_SECRET=your_secret_here
   USE_TESTNET=True    # optional, default True
   SYMBOL=TRXUSDT      # optional
   INITIAL_HA_OPEN=0.35000  # optional starting HA open
   INITIAL_SIPHON_BASELINE=4.0

3. Start in testnet mode first (USE_TESTNET=True). Test thoroughly.

This script implements:
- Hourly fetch of the most recently closed 1-hour raw candle.
- Heikin-Ashi computation using a saved initial HA open (persisted between restarts).
- Signal detection: Buy if HA green AND ha_open ≈ ha_low; Sell if HA red AND ha_open ≈ ha_high.
- SL = ha_high + 1 pip (buy) or ha_low - 1 pip (sell). TP = 1:1 RR + 0.1% of entry.
- Entry price used for sizing = raw candle close.
- Position sizing: risk 10% of unified balance; fallback to 90% sizing if unaffordable.
- Minimum qty enforcement.
- Places market order, immediately creates ReduceOnly TP (limit) and SL (trigger).
- Modifies TP/SL if a new confirmed signal gives strictly better TP or strictly better SL.
- Siphoning: transfers 25% to fund wallet when unified balance >= baseline*2, updates baseline to remainder.
- Saves HA open/close and siphon baseline to disk for restart recovery.
- One-way mode (positionIdx=0). Works with linear USDT perpetuals.
- Extensive logging to file bybit_ha_bot.log and stdout.

IMPORTANT: Bybit API responses and field names sometimes differ between API versions/accounts.
If an endpoint call fails or fields are missing, inspect the printed JSON and adapt accordingly.
"""

import os
import time
import hmac
import hashlib
import json
import math
import requests
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ---------- CONFIG ----------
API_KEY = os.getenv("BYBIT_API_KEY", "YOUR_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET", "YOUR_API_SECRET")
USE_TESTNET = os.getenv("USE_TESTNET", "True").lower() in ("1", "true", "yes")
BASE_URL = "https://api-testnet.bybit.com" if USE_TESTNET else "https://api.bybit.com"

SYMBOL = os.getenv("SYMBOL", "TRXUSDT")
LEVERAGE = int(os.getenv("LEVERAGE", "75"))
MIN_QTY = int(os.getenv("MIN_QTY", "16"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "0.10"))       # 10% risk
FALLBACK_USAGE = float(os.getenv("FALLBACK_USAGE", "0.90"))  # 90% fallback
PIP_SIZE = float(os.getenv("PIP_SIZE", "0.00001"))
RR = float(os.getenv("RR", "1.0"))
EXTRA_TP_PERCENT = float(os.getenv("EXTRA_TP_PERCENT", "0.001"))  # 0.1% extra

INITIAL_HA_OPEN = float(os.getenv("INITIAL_HA_OPEN", "0.33107"))
INITIAL_SIPHON_BASELINE = float(os.getenv("INITIAL_SIPHON_BASELINE", "4.0"))
FUND_ACCOUNT_TYPE = os.getenv("FUND_ACCOUNT_TYPE", "FUND")  # may need adjustment per account

LOG_FILE = Path("bybit_ha_bot.log")
HA_STATE_FILE = Path("ha_state.json")
SIPHON_FILE = Path("siphon_baseline.json")

# ---------- UTIL ----------
def log(msg):
    ts = datetime.now(timezone.utc).astimezone().isoformat()
    line = f"{ts} | {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def now_ms():
    return str(int(time.time() * 1000))

def hmac_sha256(msg: str):
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

# ---------- HTTP helpers (Bybit v5 best-effort signing) ----------
def bybit_public_get(path, params=None):
    url = BASE_URL + path
    r = requests.get(url, params=params, timeout=15)
    try:
        r.raise_for_status()
    except Exception as e:
        log(f"Public GET error: {e} | {r.text}")
        raise
    return r.json()

def bybit_private_request(path, params=None, method="POST"):
    if params is None:
        params = {}
    timestamp = now_ms()
    body = json.dumps(params, separators=(",", ":"), sort_keys=True) if params else ""
    payload = timestamp + API_KEY + body
    sign = hmac_sha256(payload)
    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": sign,
        "Content-Type": "application/json"
    }
    url = BASE_URL + path
    if method.upper() == "GET":
        r = requests.get(url, headers=headers, params=params, timeout=15)
    else:
        r = requests.post(url, headers=headers, data=body if params else "{}", timeout=15)
    try:
        r.raise_for_status()
    except Exception as e:
        log(f"Private request error: {e} | {r.text}")
        raise
    return r.json()

# ---------- Persistence ----------
def save_ha_state(ha_open, ha_close):
    try:
        HA_STATE_FILE.write_text(json.dumps({"ha_open": float(ha_open), "ha_close": float(ha_close)}))
    except Exception as e:
        log(f"save_ha_state error: {e}")

def load_ha_state():
    if HA_STATE_FILE.exists():
        try:
            d = json.loads(HA_STATE_FILE.read_text())
            return float(d.get("ha_open")), float(d.get("ha_close"))
        except Exception:
            return None, None
    return None, None

def save_siphon_baseline(val):
    try:
        SIPHON_FILE.write_text(json.dumps({"baseline": float(val)}))
    except Exception as e:
        log(f"save_siphon_baseline error: {e}")

def load_siphon_baseline():
    if SIPHON_FILE.exists():
        try:
            d = json.loads(SIPHON_FILE.read_text())
            return float(d.get("baseline", INITIAL_SIPHON_BASELINE))
        except Exception:
            return INITIAL_SIPHON_BASELINE
    return INITIAL_SIPHON_BASELINE

# ---------- HA calculation ----------
def compute_heiken_ashi_series(raw_candles):
    # raw_candles: list oldest->newest of dicts with open/high/low/close
    ha = []
    prev_ha_open = None
    prev_ha_close = None
    for r in raw_candles:
        o, h, l, c = r["open"], r["high"], r["low"], r["close"]
        ha_close = (o + h + l + c) / 4.0
        if prev_ha_open is None:
            ha_open = (o + c) / 2.0
        else:
            ha_open = (prev_ha_open + prev_ha_close) / 2.0
        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)
        ha.append({"open": ha_open, "high": ha_high, "low": ha_low, "close": ha_close})
        prev_ha_open, prev_ha_close = ha_open, ha_close
    return ha

# ---------- Fetch candles ----------
def fetch_recent_1h_raw(symbol=SYMBOL, limit=10):
    # Try v5 market kline
    try:
        path = "/v5/market/kline"
        params = {"category": "linear", "symbol": symbol, "interval": "60", "limit": limit}
        res = bybit_public_get(path, params)
        # typical structure: res["result"]["list"] -> list of arrays [start,open,high,low,close,...]
        rows = None
        if isinstance(res, dict):
            # try variations
            result = res.get("result") or res.get("data") or {}
            if isinstance(result, dict):
                rows = result.get("list") or result.get("data")
            elif isinstance(result, list):
                rows = result
        if rows:
            candles = []
            for r in rows:
                # row format depends; handle array-like and dict-like
                if isinstance(r, list) or isinstance(r, tuple):
                    start = int(r[0])
                    open_p = float(r[1]); high = float(r[2]); low = float(r[3]); close = float(r[4])
                elif isinstance(r, dict):
                    start = int(r.get("start", r.get("t", 0)))
                    open_p = float(r.get("open")); high = float(r.get("high")); low = float(r.get("low")); close = float(r.get("close"))
                else:
                    continue
                candles.append({"open": open_p, "high": high, "low": low, "close": close, "start_at": start})
            # Ensure oldest->newest
            candles = sorted(candles, key=lambda x: x["start_at"])
            return candles
    except Exception as e:
        log(f"v5 kline failed: {e}")

    # Fallback older endpoint
    try:
        path = "/public/linear/kline"
        params = {"symbol": symbol, "interval": "60", "limit": limit}
        res = bybit_public_get(path, params)
        rows = res.get("result") or []
        candles = []
        for r in rows:
            candles.append({"open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]), "close": float(r["close"]), "start_at": int(r["start_at"])})
        candles = sorted(candles, key=lambda x: x["start_at"])
        return candles
    except Exception as e:
        log(f"fallback kline failed: {e}")
        raise

# ---------- Signals ----------
def approx_equal(a, b, tol=PIP_SIZE):
    return abs(a - b) <= tol

def detect_signal_from_ha(ha):
    if ha["close"] > ha["open"] and approx_equal(ha["open"], ha["low"]):
        return "buy"
    if ha["close"] < ha["open"] and approx_equal(ha["open"], ha["high"]):
        return "sell"
    return "none"

def compute_sl_and_tp(signal, ha_candle, entry_price):
    pip = PIP_SIZE
    if signal == "buy":
        sl = ha_candle["high"] + pip
        distance = entry_price - sl
        if distance <= 0:
            distance = entry_price * 0.001
        tp = entry_price + (distance * RR) + (entry_price * EXTRA_TP_PERCENT)
        return sl, tp
    else:
        sl = ha_candle["low"] - pip
        distance = sl - entry_price
        if distance <= 0:
            distance = entry_price * 0.001
        tp = entry_price - (distance * RR) - (entry_price * EXTRA_TP_PERCENT)
        return sl, tp

# ---------- Balance & sizing ----------
def get_unified_balance_usdt():
    try:
        path = "/v5/account/wallet-balance"
        params = {"coin": "USDT"}
        res = bybit_private_request(path, params, method="GET")
        # parse typical shapes
        result = res.get("result") or {}
        if isinstance(result, dict):
            lst = result.get("list") or []
            for item in lst:
                if item.get("coin") == "USDT":
                    return float(item.get("walletBalance", item.get("totalBalance", 0)))
        # fallback search
        if isinstance(result, list):
            for item in result:
                if item.get("coin") == "USDT":
                    return float(item.get("walletBalance", item.get("totalBalance", 0)))
    except Exception as e:
        log(f"get_unified_balance_usdt error: {e}")
    raise RuntimeError("Could not fetch unified balance. Inspect API response.")

def calculate_qty(balance_usdt, entry_price, sl_price, risk_pct=RISK_PERCENT):
    risk_amount = balance_usdt * risk_pct
    stop_distance = abs(entry_price - sl_price)
    if stop_distance <= 0:
        raise ValueError("stop distance must be > 0")
    qty = math.floor(risk_amount / stop_distance)
    if qty < MIN_QTY:
        qty = MIN_QTY
    return int(qty)

# ---------- Orders ----------
def place_market_with_tp_sl(symbol, side, qty, tp_price, sl_price):
    out = {}
    try:
        path = "/v5/order/create"
        body = {
            "category": "linear",
            "symbol": symbol,
            "side": "Buy" if side == "buy" else "Sell",
            "orderType": "Market",
            "qty": str(qty),
            "timeInForce": "ImmediateOrCancel",
            "positionIdx": 0,
            "reduceOnly": False,
            "closeOnTrigger": False
        }
        res = bybit_private_request(path, body, method="POST")
        out["market"] = res
    except Exception as e:
        out["market"] = {"error": str(e)}
        log(f"market order failed: {e}")
        return out

    # Place TP as a Limit reduce-only order opposite side
    try:
        tp_side = "Sell" if side == "buy" else "Buy"
        tp_path = "/v5/order/create"
        tp_body = {
            "category": "linear",
            "symbol": symbol,
            "side": tp_side,
            "orderType": "Limit",
            "qty": str(qty),
            "price": str(round(tp_price, 8)),
            "timeInForce": "PostOnly",
            "positionIdx": 0,
            "reduceOnly": True
        }
        res_tp = bybit_private_request(tp_path, tp_body, method="POST")
        out["tp"] = res_tp
    except Exception as e:
        out["tp"] = {"error": str(e)}
        log(f"tp order failed: {e}")

    # Place SL as a trigger market reduce-only
    try:
        sl_side = "Sell" if side == "buy" else "Buy"
        sl_path = "/v5/trigger/order/create"
        sl_body = {
            "category": "linear",
            "symbol": symbol,
            "side": sl_side,
            "orderType": "Market",
            "triggerPrice": str(round(sl_price, 8)),
            "qty": str(qty),
            "triggerBy": "LastPrice",
            "positionIdx": 0,
            "reduceOnly": True
        }
        res_sl = bybit_private_request(sl_path, sl_body, method="POST")
        out["sl"] = res_sl
    except Exception as e:
        out["sl"] = {"error": str(e)}
        log(f"sl order failed: {e}")

    return out

def cancel_order(order_id):
    try:
        body = {"category": "linear", "orderId": order_id}
        res = bybit_private_request("/v5/order/cancel", body, method="POST")
        return res
    except Exception as e:
        return {"error": str(e)}

def cancel_trigger_order(stop_order_id):
    try:
        body = {"category": "linear", "stopOrderId": stop_order_id}
        res = bybit_private_request("/v5/trigger/order/cancel", body, method="POST")
        return res
    except Exception as e:
        return {"error": str(e)}

def modify_tp_sl(existing_meta, new_tp=None, new_sl=None):
    res = {"modified": []}
    # Cancel and recreate TP
    if new_tp is not None:
        try:
            tp_id = existing_meta.get("tp_order_id")
            if tp_id:
                cancel_order(tp_id)
            tp_side = "Sell" if existing_meta["direction"] == "buy" else "Buy"
            tp_body = {
                "category": "linear",
                "symbol": SYMBOL,
                "side": tp_side,
                "orderType": "Limit",
                "qty": str(existing_meta["qty"]),
                "price": str(round(new_tp, 8)),
                "timeInForce": "PostOnly",
                "positionIdx": 0,
                "reduceOnly": True
            }
            r = bybit_private_request("/v5/order/create", tp_body, method="POST")
            res["modified"].append({"tp": r})
        except Exception as e:
            res["tp_error"] = str(e)

    # Cancel and recreate SL
    if new_sl is not None:
        try:
            sl_id = existing_meta.get("sl_order_id")
            if sl_id:
                cancel_trigger_order(sl_id)
            sl_side = "Sell" if existing_meta["direction"] == "buy" else "Buy"
            sl_body = {
                "category": "linear",
                "symbol": SYMBOL,
                "side": sl_side,
                "orderType": "Market",
                "triggerPrice": str(round(new_sl, 8)),
                "qty": str(existing_meta["qty"]),
                "triggerBy": "LastPrice",
                "positionIdx": 0,
                "reduceOnly": True
            }
            r = bybit_private_request("/v5/trigger/order/create", sl_body, method="POST")
            res["modified"].append({"sl": r})
        except Exception as e:
            res["sl_error"] = str(e)

    return res

# ---------- Siphon ----------
def siphon_check_and_transfer():
    baseline = load_siphon_baseline()
    try:
        bal = get_unified_balance_usdt()
    except Exception as e:
        log(f"Siphon: could not get balance: {e}")
        return
    log(f"SIPHON CHECK: baseline={baseline} unified_balance={bal}")
    if bal >= baseline * 2:
        amount_to_transfer = bal * 0.25
        try:
            body = {
                "coin": "USDT",
                "amount": str(round(amount_to_transfer, 8)),
                "fromAccountType": "CONTRACT",
                "toAccountType": FUND_ACCOUNT_TYPE
            }
            res = bybit_private_request("/v5/asset/transfer", body, method="POST")
            log(f"SIPHON: transferred {amount_to_transfer} USDT to {FUND_ACCOUNT_TYPE}. API result: {res}")
            new_baseline = bal - amount_to_transfer
            save_siphon_baseline(new_baseline)
            log(f"SIPHON: new baseline set to {new_baseline}")
        except Exception as e:
            log(f"SIPHON transfer failed: {e}")

# ---------- Main loop ----------
open_trade_state = {}  # holds {"direction","entry","sl","tp","qty","tp_order_id","sl_order_id",...}

def main_loop():
    # load persisted HA state or use initial
    saved_open, saved_close = load_ha_state()
    if saved_open is None:
        prev_ha = {"open": INITIAL_HA_OPEN, "close": INITIAL_HA_OPEN}
        log(f"Using INITIAL_HA_OPEN = {INITIAL_HA_OPEN}")
    else:
        prev_ha = {"open": float(saved_open), "close": float(saved_close)}
        log(f"Loaded persisted HA state: open={prev_ha['open']} close={prev_ha['close']}")

    if not SIPHON_FILE.exists():
        save_siphon_baseline(INITIAL_SIPHON_BASELINE)

    last_processed_hour = None
    log("Bot started. Waiting for top-of-hour...")

    while True:
        now = datetime.utcnow()
        if now.minute == 0 and now.hour != last_processed_hour:
            try:
                log(f"Top-of-hour: {now.isoformat()} - fetching candles")
                raw = fetch_recent_1h_raw(SYMBOL, limit=10)
                if not raw or len(raw) < 1:
                    log("No candles, skipping this hour")
                    time.sleep(30)
                    continue

                closed = raw[-1]  # just closed candle (oldest->newest)
                ha_series = compute_heiken_ashi_series(raw)

                # ensure HA continuity by forcing first HA open/close to prev_ha where we persist it
                if len(ha_series) >= 1:
                    ha_series[0]["open"] = prev_ha["open"]
                    ha_series[0]["close"] = prev_ha["close"]
                    # recompute forward
                    for i in range(1, len(ha_series)):
                        ha_series[i]["open"] = (ha_series[i-1]["open"] + ha_series[i-1]["close"]) / 2.0
                        ha_series[i]["high"] = max(raw[i]["high"], ha_series[i]["open"], ha_series[i]["close"])
                        ha_series[i]["low"] = min(raw[i]["low"], ha_series[i]["open"], ha_series[i]["close"])

                last_ha = ha_series[-1]
                save_ha_state(last_ha["open"], last_ha["close"])

                signal = detect_signal_from_ha(last_ha)  # 'buy'/'sell'/'none'
                candle_color = "green" if last_ha["close"] > last_ha["open"] else "red"
                log(json.dumps({"symbol": SYMBOL, "raw_last": closed, "ha_last": last_ha, "candle_color": candle_color, "signal": signal}))

                if signal in ("buy", "sell"):
                    entry_price = float(closed["close"])
                    sl_price, tp_price = compute_sl_and_tp(signal, last_ha, entry_price)

                    # sizing
                    try:
                        balance = get_unified_balance_usdt()
                    except Exception as e:
                        log(f"Could not fetch balance: {e}; skipping trade this hour")
                        balance = None

                    qty = MIN_QTY
                    if balance and balance > 0:
                        try:
                            qty = calculate_qty(balance, entry_price, sl_price)
                        except Exception as e:
                            log(f"Sizing error: {e}; using MIN_QTY")
                            qty = MIN_QTY

                        est_cost = qty * entry_price
                        if est_cost > balance * RISK_PERCENT:
                            # fallback sizing using fallback balance
                            fallback_balance = balance * FALLBACK_USAGE
                            try:
                                qty = calculate_qty(fallback_balance, entry_price, sl_price, risk_pct=1.0)
                            except Exception as e:
                                log(f"Fallback sizing error: {e}; using MIN_QTY")
                                qty = MIN_QTY

                    if qty < MIN_QTY:
                        qty = MIN_QTY

                    if not open_trade_state:
                        log(f"Placing new {signal} trade entry={entry_price} sl={sl_price} tp={tp_price} qty={qty}")
                        resp = place_market_with_tp_sl(SYMBOL, signal, qty, tp_price, sl_price)
                        # try parse ids
                        parsed = {"direction": signal, "entry": entry_price, "sl": sl_price, "tp": tp_price, "qty": qty}
                        # get tp id
                        try:
                            tp_res = resp.get("tp") or {}
                            sl_res = resp.get("sl") or {}
                            tp_id = None
                            sl_id = None
                            if isinstance(tp_res, dict):
                                tp_id = tp_res.get("result", {}).get("orderId") or tp_res.get("result", {}).get("order_id") or tp_res.get("result", {}).get("orderId")
                            if isinstance(sl_res, dict):
                                sl_id = sl_res.get("result", {}).get("stopOrderId") or sl_res.get("result", {}).get("orderId")
                            parsed["tp_order_id"] = tp_id
                            parsed["sl_order_id"] = sl_id
                        except Exception:
                            pass
                        parsed["api_response"] = resp
                        open_trade_state.update(parsed)
                        log(f"Trade opened: {open_trade_state}")
                    else:
                        # modify logic: only modify TP or SL (not both) if it improves profit or reduces loss (without worsening the other)
                        cur = open_trade_state
                        modify_tp = False
                        modify_sl = False
                        if cur.get("direction") == signal:
                            if signal == "buy":
                                new_tp_profit = tp_price - cur["entry"]
                                cur_tp_profit = cur["tp"] - cur["entry"]
                                new_sl_loss = cur["entry"] - sl_price
                                cur_sl_loss = cur["entry"] - cur["sl"]
                                if new_tp_profit > cur_tp_profit and new_sl_loss <= cur_sl_loss:
                                    modify_tp = True
                                if new_sl_loss < cur_sl_loss and new_tp_profit >= cur_tp_profit:
                                    modify_sl = True
                            else:
                                new_tp_profit = cur["entry"] - tp_price
                                cur_tp_profit = cur["entry"] - cur["tp"]
                                new_sl_loss = sl_price - cur["entry"]
                                cur_sl_loss = cur["sl"] - cur["entry"]
                                if new_tp_profit > cur_tp_profit and new_sl_loss <= cur_sl_loss:
                                    modify_tp = True
                                if new_sl_loss < cur_sl_loss and new_tp_profit >= cur_tp_profit:
                                    modify_sl = True

                            if modify_tp or modify_sl:
                                log(f"Modifying trade: modify_tp={modify_tp}, modify_sl={modify_sl}")
                                existing = {"tp_order_id": cur.get("tp_order_id"), "sl_order_id": cur.get("sl_order_id"), "direction": cur.get("direction"), "qty": cur.get("qty")}
                                mod_res = modify_tp_sl(existing, new_tp=tp_price if modify_tp else None, new_sl=sl_price if modify_sl else None)
                                log(f"Modify response: {mod_res}")
                                if modify_tp:
                                    open_trade_state["tp"] = tp_price
                                if modify_sl:
                                    open_trade_state["sl"] = sl_price
                        else:
                            log("Opposite signal while trade open. Not auto-closing per settings.")

                else:
                    log("No valid signal this hour.")

                # siphon check
                try:
                    siphon_check_and_transfer()
                except Exception as e:
                    log(f"Siphon check error: {e}")

                # update prev_ha and last_processed_hour
                prev_ha = {"open": last_ha["open"], "close": last_ha["close"]}
                last_processed_hour = now.hour

            except Exception as e:
                log(f"Hourly processing error: {e}")

        time.sleep(15)

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        log("Bot stopped by user (KeyboardInterrupt).")
    except Exception as e:
        log(f"Fatal error: {e}")
