"""
bybit_hourly_ha_bot_full.py
Complete live Bybit hourly Heikin-Ashi trading bot.

- Toggles testnet/mainnet via USE_TESTNET.
- Attaches takeProfit & stopLoss directly to the Market order when opening.
- Updates TP/SL using /v5/position/trading-stop for existing position.
- Persists HA open/close and siphon baseline to disk.
- Logs to stdout and bybit_ha_bot.log.

IMPORTANT: Test on Testnet first.
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

# ----------------- CONFIG -----------------
API_KEY = os.getenv("BYBIT_API_KEY", "YOUR_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET", "YOUR_API_SECRET")

# Toggle: True -> Testnet, False -> Mainnet
USE_TESTNET = False
BASE_URL = "https://api-testnet.bybit.com" if USE_TESTNET else "https://api.bybit.com"

SYMBOL = os.getenv("SYMBOL", "TRXUSDT")        # e.g. TRXUSDT
LEVERAGE = int(os.getenv("LEVERAGE", "75"))
MIN_QTY = int(os.getenv("MIN_QTY", "16"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "0.10"))   # 10%
FALLBACK_USAGE = float(os.getenv("FALLBACK_USAGE", "0.90"))
PIP_SIZE = float(os.getenv("PIP_SIZE", "0.0001"))
RR = float(os.getenv("RR", "1.0"))
EXTRA_TP_PERCENT = float(os.getenv("EXTRA_TP_PERCENT", "0.001"))  # 0.1%

INITIAL_HA_OPEN = float(os.getenv("INITIAL_HA_OPEN", "0.3313"))

INITIAL_SIPHON_BASELINE = float(os.getenv("INITIAL_SIPHON_BASELINE", "4.0"))
FUND_ACCOUNT_TYPE = os.getenv("FUND_ACCOUNT_TYPE", "FUND")  # may need adjustment

LOG_FILE = Path("bybit_ha_bot.log")
HA_STATE_FILE = Path("ha_state.json")
SIPHON_FILE = Path("siphon_baseline.json")

# ----------------- UTIL -----------------
def log(msg):
    ts = datetime.now(timezone.utc).astimezone().isoformat()
    line = f"{ts} | {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def now_ms():
    return str(int(time.time() * 1000))

def sign_hmac_sha256(message: str):
    return hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()

# ----------------- HTTP / Bybit helpers -----------------
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
    """
    Best-effort Bybit v5 signing (timestamp + apiKey + body) HMAC SHA256.
    If your account requires a different signing scheme, adapt here.
    """
    if params is None:
        params = {}
    timestamp = now_ms()
    body = json.dumps(params, separators=(',', ':'), sort_keys=True) if params else ""
    prehash = timestamp + API_KEY + body
    signature = sign_hmac_sha256(prehash)
    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": signature,
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

# ----------------- Persistence -----------------
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

# ----------------- Market Data & HA -----------------
def fetch_recent_1h_raw(symbol=SYMBOL, limit=10):
    # Attempt v5 kline
    try:
        path = "/v5/market/kline"
        params = {"category": "linear", "symbol": symbol, "interval": "60", "limit": limit}
        res = bybit_public_get(path, params)
        rows = None
        if isinstance(res, dict):
            result = res.get("result") or res.get("data") or {}
            if isinstance(result, dict):
                rows = result.get("list") or result.get("data")
            elif isinstance(result, list):
                rows = result
        if rows:
            candles = []
            for r in rows:
                if isinstance(r, (list, tuple)):
                    start = int(r[0]); open_p = float(r[1]); high = float(r[2]); low = float(r[3]); close = float(r[4])
                elif isinstance(r, dict):
                    start = int(r.get("start", r.get("t", 0))); open_p = float(r.get("open")); high = float(r.get("high")); low = float(r.get("low")); close = float(r.get("close"))
                else:
                    continue
                candles.append({"open": open_p, "high": high, "low": low, "close": close, "start_at": start})
            candles = sorted(candles, key=lambda x: x["start_at"])
            return candles
    except Exception as e:
        log(f"v5 kline failed: {e}")

    # Fallback to older endpoint if v5 not available
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

def compute_heiken_ashi_series(raw):
    ha = []
    prev_ha_open = None
    prev_ha_close = None
    for r in raw:
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

# ----------------- Signals & sizing -----------------
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

def get_unified_balance_usdt():
    try:
        path = "/v5/account/wallet-balance"
        params = {"coin": "USDT"}
        res = bybit_private_request(path, params, method="GET")
        result = res.get("result") or {}
        if isinstance(result, dict):
            lst = result.get("list") or []
            for item in lst:
                if item.get("coin") == "USDT":
                    return float(item.get("walletBalance", item.get("totalBalance", 0)))
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

# ----------------- Orders (attach TP/SL on create, update via trading-stop) -----------------
def place_market_order_attach_tp_sl(symbol, side, qty, tp_price, sl_price):
    """
    Place market order + attach takeProfit & stopLoss on create.
    Uses v5 /v5/order/create with takeProfit and stopLoss fields (best-effort).
    Returns API response dict.
    """
    path = "/v5/order/create"
    body = {
        "category": "linear",
        "symbol": symbol,
        "side": "Buy" if side == "buy" else "Sell",
        "orderType": "Market",
        "qty": str(qty),
        "timeInForce": "ImmediateOrCancel",
        "positionIdx": 0,
        "takeProfit": str(round(tp_price, 8)),
        "stopLoss": str(round(sl_price, 8)),
        "tpTriggerBy": "LastPrice",
        "slTriggerBy": "LastPrice",
        "reduceOnly": False,
        "closeOnTrigger": False
    }
    res = bybit_private_request(path, body, method="POST")
    return res

def update_position_trading_stop(symbol, take_profit=None, stop_loss=None):
    """
    Update position TP/SL using v5 position/trading-stop endpoint.
    Provide take_profit and/or stop_loss as floats (prices).
    """
    path = "/v5/position/trading-stop"
    body = {"category": "linear", "symbol": symbol}
    if take_profit is not None:
        body["takeProfit"] = str(round(take_profit, 8))
        body["tpTriggerBy"] = "LastPrice"
    if stop_loss is not None:
        body["stopLoss"] = str(round(stop_loss, 8))
        body["slTriggerBy"] = "LastPrice"
    res = bybit_private_request(path, body, method="POST")
    return res

# ----------------- Siphon -----------------
def siphon_check_and_transfer():
    baseline = load_siphon_baseline()
    try:
        bal = get_unified_balance_usdt()
    except Exception as e:
        log(f"Siphon: could not get balance: {e}")
        return
    log(f"SIPHON CHECK: baseline={baseline} unified_balance={bal}")
    if bal >= baseline * 2:
        amount = bal * 0.25
        try:
            body = {
                "coin": "USDT",
                "amount": str(round(amount, 8)),
                "fromAccountType": "CONTRACT",
                "toAccountType": FUND_ACCOUNT_TYPE
            }
            res = bybit_private_request("/v5/asset/transfer", body, method="POST")
            log(f"SIPHON: transferred {amount} USDT to {FUND_ACCOUNT_TYPE}. API result: {res}")
            new_baseline = bal - amount
            save_siphon_baseline(new_baseline)
            log(f"SIPHON: new baseline set to {new_baseline}")
        except Exception as e:
            log(f"SIPHON transfer failed: {e}")

# ----------------- Bot state -----------------
open_trade_state = {}  # store {"direction","entry","sl","tp","qty",...}

# ----------------- Main loop -----------------
def main_loop():
    saved_open, saved_close = load_ha_state()
    if saved_open is None:
        prev_ha = {"open": INITIAL_HA_OPEN, "close": INITIAL_HA_OPEN}
        log(f"Using INITIAL_HA_OPEN = {INITIAL_HA_OPEN}")
    else:
        prev_ha = {"open": float(saved_open), "close": float(saved_close)}
        log(f"Loaded HA state prev_open={prev_ha['open']} prev_close={prev_ha['close']}")

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
                    log("No candles returned; skipping hour")
                    time.sleep(30)
                    continue

                closed = raw[-1]
                ha_series = compute_heiken_ashi_series(raw)

                # apply persisted prev_ha to maintain consistency
                if len(ha_series) >= 1:
                    ha_series[0]["open"] = prev_ha["open"]
                    ha_series[0]["close"] = prev_ha["close"]
                    for i in range(1, len(ha_series)):
                        ha_series[i]["open"] = (ha_series[i-1]["open"] + ha_series[i-1]["close"]) / 2.0
                        ha_series[i]["high"] = max(raw[i]["high"], ha_series[i]["open"], ha_series[i]["close"])
                        ha_series[i]["low"] = min(raw[i]["low"], ha_series[i]["open"], ha_series[i]["close"])

                last_ha = ha_series[-1]
                # persist ha
                save_ha_state(last_ha["open"], last_ha["close"])

                signal = detect_signal_from_ha(last_ha)  # buy/sell/none
                candle_color = "green" if last_ha["close"] > last_ha["open"] else "red"
                log(json.dumps({"symbol": SYMBOL, "raw_last": closed, "ha_last": last_ha, "candle_color": candle_color, "signal": signal}))

                if signal in ("buy", "sell"):
                    entry_price = float(closed["close"])
                    sl_price, tp_price = compute_sl_and_tp(signal, last_ha, entry_price)

                    # sizing
                    try:
                        balance = get_unified_balance_usdt()
                    except Exception as e:
                        log(f"Could not fetch unified balance: {e}; skipping trade this hour")
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
                            # fallback
                            fallback_balance = balance * FALLBACK_USAGE
                            try:
                                qty = calculate_qty(fallback_balance, entry_price, sl_price, risk_pct=1.0)
                            except Exception as e:
                                log(f"Fallback sizing error: {e}; using MIN_QTY")
                                qty = MIN_QTY

                    if qty < MIN_QTY:
                        qty = MIN_QTY

                    # If no open trade -> place market with attached tp/sl
                    if not open_trade_state:
                        log(f"Placing new {signal} trade entry={entry_price} sl={sl_price} tp={tp_price} qty={qty}")
                        try:
                            res = place_market_order_attach_tp_sl(SYMBOL, signal, qty, tp_price, sl_price)
                            log(f"Order create response: {res}")
                            # store basic state; try parse result position or order ids
                            open_trade_state.clear()
                            open_trade_state.update({
                                "direction": signal,
                                "entry": entry_price,
                                "sl": sl_price,
                                "tp": tp_price,
                                "qty": qty,
                                "api_response": res
                            })
                        except Exception as e:
                            log(f"Placing order failed: {e}")
                    else:
                        # trade open: consider modifying TP or SL if it improves profit or reduces loss (without worsening other)
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
                                log(f"Modifying existing position: modify_tp={modify_tp} modify_sl={modify_sl}")
                                tp_arg = tp_price if modify_tp else None
                                sl_arg = sl_price if modify_sl else None
                                try:
                                    mod_res = update_position_trading_stop(SYMBOL, take_profit=tp_arg, stop_loss=sl_arg)
                                    log(f"trading-stop update response: {mod_res}")
                                    if modify_tp:
                                        open_trade_state["tp"] = tp_price
                                    if modify_sl:
                                        open_trade_state["sl"] = sl_price
                                except Exception as e:
                                    log(f"trading-stop modification failed: {e}")
                        else:
                            log("Opposite signal while trade open. Not auto-closing per settings.")

                else:
                    log("No signal this hour.")

                # siphon
                try:
                    siphon_check_and_transfer()
                except Exception as e:
                    log(f"Siphon check error: {e}")

                # persist prev_ha and mark processed hour
                prev_ha = {"open": last_ha["open"], "close": last_ha["close"]}
                last_processed_hour = now.hour

            except Exception as e:
                log(f"Hourly processing error: {e}")

        time.sleep(15)

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        log("Bot stopped by user.")
    except Exception as e:
        log(f"Fatal error: {e}")
