#!/usr/bin/env python3
"""
bybit_ha_bot_verbose_fixed.py

Verbose Bybit hourly Heikin-Ashi trading bot — FIXED for:
- No Decimal + NoneType arithmetic errors.
- Proper initial HA open initialization (INITIAL_HA_OPEN used on first run).
- Robust TP/SL update via /v5/position/trading-stop with logging.
- Siphoning baseline behavior unchanged (start $2, siphon 25% when balance doubles, baseline set to remaining).
"""

import os
import time
import json
import hmac
import hashlib
import requests
from decimal import Decimal, getcontext, ROUND_DOWN
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

# ---------- CONFIG ----------
getcontext().prec = 12

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")

USE_TESTNET = os.getenv("USE_TESTNET", "False").lower() in ("1", "true", "yes")
BASE_URL = "https://api-testnet.bybit.com" if USE_TESTNET else "https://api.bybit.com"

SYMBOL = os.getenv("SYMBOL", "TRXUSDT")
CATEGORY = "linear"
LEVERAGE = int(os.getenv("LEVERAGE", "75"))

MIN_QTY = int(os.getenv("MIN_QTY", "16"))
RISK_PERCENT = Decimal(os.getenv("RISK_PERCENT", "0.10"))
FALLBACK_USAGE = Decimal(os.getenv("FALLBACK_USAGE", "0.90"))
PIP = Decimal(os.getenv("PIP", "0.0001"))
TP_BUFFER_PCT = Decimal(os.getenv("TP_BUFFER_PCT", "0.001"))  # 0.1%
RR = Decimal("1")

START_BALANCE = Decimal(os.getenv("START_BALANCE", "2"))  # user requested start $2
SIPHON_RATIO = Decimal(os.getenv("SIPHON_RATIO", "0.25"))
FUND_ACCOUNT_TYPE = os.getenv("FUND_ACCOUNT_TYPE", "FUND")

INITIAL_HA_OPEN = Decimal(os.getenv("INITIAL_HA_OPEN", "0.33097"))

# Persistence files
LOG_FILE = Path("bybit_ha_bot.log")
HA_STATE_FILE = Path("ha_state.json")
SIPHON_FILE = Path("siphon_baseline.json")

REQUEST_TIMEOUT = 15

# ---------- UTIL ----------
def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat()

def log(msg: str):
    line = f"{_now_iso()} | {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass

def dec(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")

def fmt_price(d: Decimal) -> str:
    # Bybit typically accepts up to 8 decimals
    return format(d.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN), 'f')

def now_ms() -> str:
    return str(int(time.time() * 1000))

def sign_hmac_sha256(message: str) -> str:
    return hmac.new(BYBIT_API_SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()

# ---------- PERSIST ----------
def save_json_file(path: Path, data: dict):
    try:
        path.write_text(json.dumps(data))
    except Exception as e:
        log(f"save_json_file error writing {path}: {e}")

def load_json_file(path: Path) -> Optional[dict]:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception as e:
        log(f"load_json_file error reading {path}: {e}")
    return None

def save_ha_state(ha_open: Decimal, ha_close: Decimal):
    save_json_file(HA_STATE_FILE, {"ha_open": str(ha_open), "ha_close": str(ha_close)})

def load_ha_state() -> Tuple[Decimal, Decimal]:
    """
    Always return Decimals for ha_open and ha_close.
    If no file, return INITIAL_HA_OPEN for both.
    """
    d = load_json_file(HA_STATE_FILE)
    if not d:
        return INITIAL_HA_OPEN, INITIAL_HA_OPEN
    try:
        ha_open = dec(d.get("ha_open", str(INITIAL_HA_OPEN)))
        ha_close = dec(d.get("ha_close", str(INITIAL_HA_OPEN)))
        # guard: if either is None-like, set to initial
        if ha_open is None:
            ha_open = INITIAL_HA_OPEN
        if ha_close is None:
            ha_close = INITIAL_HA_OPEN
        return ha_open, ha_close
    except Exception as e:
        log(f"load_ha_state parse error: {e}; falling back to INITIAL_HA_OPEN")
        return INITIAL_HA_OPEN, INITIAL_HA_OPEN

def save_siphon_baseline(val: Decimal):
    save_json_file(SIPHON_FILE, {"baseline": str(val)})

def load_siphon_baseline() -> Decimal:
    d = load_json_file(SIPHON_FILE)
    if not d:
        return START_BALANCE
    try:
        baseline = dec(d.get("baseline", str(START_BALANCE)))
        if baseline is None:
            return START_BALANCE
        return baseline
    except Exception as e:
        log(f"load_siphon_baseline parse error: {e}; falling back to START_BALANCE")
        return START_BALANCE

# ---------- HTTP Helpers ----------
def bybit_public_get(path: str, params: dict = None) -> dict:
    url = BASE_URL + path
    try:
        r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"Public GET error path={path} params={params} exc={e} resp_text={getattr(e, 'response', None)}")
        raise

def bybit_private_request(path: str, params: dict = None, method: str = "POST") -> dict:
    if params is None:
        params = {}
    timestamp = now_ms()
    body = json.dumps(params, separators=(',', ':'), sort_keys=True) if params else ""
    prehash = timestamp + BYBIT_API_KEY + body
    signature = sign_hmac_sha256(prehash)
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": signature,
        "Content-Type": "application/json"
    }
    url = BASE_URL + path
    try:
        if method.upper() == "GET":
            r = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        else:
            r = requests.post(url, headers=headers, data=body if params else "{}", timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        # try to include response text if available
        resp_text = None
        try:
            resp_text = r.text
        except Exception:
            resp_text = "no response text"
        log(f"Private request error: path={path} method={method} params={params} exc={e} resp_text={resp_text}")
        raise

# ---------- MARKET DATA & HEIKIN-ASHI ----------
def fetch_recent_1h_raw(symbol: str = SYMBOL, limit: int = 10) -> list:
    """
    Returns list oldest->newest of candles with float values to avoid mixing Decimal/None.
    Each candle: {"open": Decimal, "high": Decimal, "low": Decimal, "close": Decimal, "start_at": int}
    """
    path = "/v5/market/kline"
    params = {"category": CATEGORY, "symbol": symbol, "interval": "60", "limit": limit}
    res = bybit_public_get(path, params)
    rows = None
    if isinstance(res, dict):
        result = res.get("result") or res.get("data") or {}
        if isinstance(result, dict):
            rows = result.get("list") or result.get("data")
        elif isinstance(result, list):
            rows = result
    if not rows:
        raise RuntimeError("No kline rows from Bybit.")
    candles = []
    for r in rows:
        if isinstance(r, (list, tuple)):
            start = int(r[0]); open_p = dec(r[1]); high = dec(r[2]); low = dec(r[3]); close = dec(r[4])
        elif isinstance(r, dict):
            start = int(r.get("start", r.get("t", 0)))
            open_p = dec(r.get("open")); high = dec(r.get("high")); low = dec(r.get("low")); close = dec(r.get("close"))
        else:
            continue
        # ensure no None; coerce to Decimal
        open_p = open_p if open_p is not None else Decimal("0")
        high = high if high is not None else Decimal("0")
        low = low if low is not None else Decimal("0")
        close = close if close is not None else Decimal("0")
        candles.append({"open": open_p, "high": high, "low": low, "close": close, "start_at": start})
    candles = sorted(candles, key=lambda x: x["start_at"])
    return candles

def compute_heiken_ashi_series(raw_candles: list, initial_ha_open: Decimal, initial_ha_close: Decimal) -> list:
    """
    raw_candles: list oldest->newest with Decimal prices
    initial_ha_open & initial_ha_close: Decimals (used as prev_ha_open/prev_ha_close for the first computation)
    Always returns Decimal fields and never uses None in arithmetic.
    """
    ha = []
    prev_ha_open = dec(initial_ha_open)
    prev_ha_close = dec(initial_ha_close)
    # We'll compute HA for each raw candle sequentially.
    for idx, r in enumerate(raw_candles):
        o, h, l, c = dec(r["open"]), dec(r["high"]), dec(r["low"]), dec(r["close"])
        ha_close = (o + h + l + c) / Decimal(4)
        if idx == 0 and (prev_ha_open is None or prev_ha_close is None):
            # Fallback safe compute
            ha_open = (o + c) / Decimal(2)
        else:
            # Use prev values (we ensured they are Decimal above)
            ha_open = (prev_ha_open + prev_ha_close) / Decimal(2)
        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)
        ha.append({"open": ha_open, "high": ha_high, "low": ha_low, "close": ha_close})
        prev_ha_open, prev_ha_close = ha_open, ha_close
    return ha

# ---------- SIGNALS & SIZING ----------
def approx_equal(a: Decimal, b: Decimal, tol: Decimal = PIP) -> bool:
    return abs(a - b) <= tol

def detect_signal_from_ha(last_ha: dict) -> str:
    if last_ha["close"] > last_ha["open"] and approx_equal(last_ha["open"], last_ha["low"]):
        return "buy"
    if last_ha["close"] < last_ha["open"] and approx_equal(last_ha["open"], last_ha["high"]):
        return "sell"
    return "none"

def compute_sl_tp_using_ha(signal: str, ha_candle: dict, entry_price: Decimal) -> Tuple[Decimal, Decimal]:
    if signal == "buy":
        sl = dec(ha_candle["low"]) - PIP
        if sl >= entry_price:
            sl = entry_price - PIP
        risk = entry_price - sl
        tp = entry_price + (risk * RR) + (entry_price * TP_BUFFER_PCT)
        return sl, tp
    elif signal == "sell":
        sl = dec(ha_candle["high"]) + PIP
        if sl <= entry_price:
            sl = entry_price + PIP
        risk = sl - entry_price
        tp = entry_price - (risk * RR) - (entry_price * TP_BUFFER_PCT)
        return sl, tp
    else:
        raise ValueError("signal must be buy or sell")

def calculate_qty_from_risk(balance_usdt: Decimal, entry: Decimal, sl: Decimal, risk_pct: Decimal = RISK_PERCENT) -> int:
    stop_dist = abs(entry - sl)
    if stop_dist <= Decimal("0"):
        raise ValueError("stop distance must be > 0")
    risk_amount = balance_usdt * risk_pct
    qty = int((risk_amount / stop_dist).to_integral_value(rounding=ROUND_DOWN))
    if qty < MIN_QTY:
        qty = MIN_QTY
    return qty

# ---------- BALANCE & POSITIONS ----------
def get_unified_balance_usdt() -> Decimal:
    try:
        res = bybit_private_request("/v5/account/wallet-balance", {"coin": "USDT"}, method="GET")
        result = res.get("result") or {}
        # Try several shapes
        if isinstance(result, dict):
            lst = result.get("list") or []
            for item in lst:
                if item.get("coin") == "USDT":
                    wb = item.get("walletBalance") or item.get("totalBalance") or item.get("availableToWithdraw")
                    if wb is not None:
                        return dec(wb)
            # nested
            for it in lst:
                if isinstance(it, dict):
                    for k, v in it.items():
                        if isinstance(v, list):
                            for coin_item in v:
                                if coin_item.get("coin") == "USDT":
                                    wb = coin_item.get("walletBalance") or coin_item.get("totalBalance")
                                    if wb is not None:
                                        return dec(wb)
        if isinstance(result, list):
            for it in result:
                if it.get("coin") == "USDT":
                    wb = it.get("walletBalance") or it.get("totalBalance")
                    if wb is not None:
                        return dec(wb)
    except Exception as e:
        log(f"get_unified_balance_usdt error: {e}")
    raise RuntimeError("Could not fetch unified balance; inspect API response")

def get_current_position(symbol: str = SYMBOL) -> dict:
    try:
        res = bybit_private_request("/v5/position/list", {"category": CATEGORY, "symbol": symbol}, method="GET")
        result = res.get("result") or {}
        lst = result.get("list") or []
        for p in lst:
            if p.get("symbol") == symbol:
                qty = dec(p.get("size") or p.get("positionQty") or p.get("position") or 0)
                side = p.get("side") or p.get("positionSide")
                return {"qty": qty, "side": side, "raw": p}
        return {"qty": Decimal("0"), "side": None, "raw": {}}
    except Exception as e:
        log(f"get_current_position error: {e}")
        return {"qty": Decimal("0"), "side": None, "raw": {}}

# ---------- ORDERS & TP/SL UPDATE ----------
def place_market_order_attach_tp_sl(symbol: str, side: str, qty: int, tp_price: Decimal, sl_price: Decimal) -> dict:
    body = {
        "category": CATEGORY,
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": str(int(qty)),
        "timeInForce": "ImmediateOrCancel",
        "positionIdx": 0,
        "takeProfit": fmt_price(tp_price),
        "stopLoss": fmt_price(sl_price),
        "tpTriggerBy": "LastPrice",
        "slTriggerBy": "LastPrice",
        "reduceOnly": False,
        "closeOnTrigger": False
    }
    log(f"place_market_order_attach_tp_sl body: {json.dumps(body)}")
    res = bybit_private_request("/v5/order/create", body, method="POST")
    log(f"place_market_order_attach_tp_sl response: {res}")
    return res

def place_market_order_raw(symbol: str, side: str, qty: int) -> dict:
    body = {
        "category": CATEGORY,
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": str(int(qty)),
        "timeInForce": "ImmediateOrCancel",
        "positionIdx": 0,
        "reduceOnly": False,
        "closeOnTrigger": False
    }
    log(f"place_market_order_raw body: {json.dumps(body)}")
    res = bybit_private_request("/v5/order/create", body, method="POST")
    log(f"place_market_order_raw response: {res}")
    return res

def update_position_trading_stop(symbol: str, take_profit: Optional[Decimal] = None, stop_loss: Optional[Decimal] = None) -> dict:
    body = {"category": CATEGORY, "symbol": symbol}
    if take_profit is not None:
        body["takeProfit"] = fmt_price(take_profit)
        body["tpTriggerBy"] = "LastPrice"
    if stop_loss is not None:
        body["stopLoss"] = fmt_price(stop_loss)
        body["slTriggerBy"] = "LastPrice"
    log(f"update_position_trading_stop body: {json.dumps(body)}")
    res = bybit_private_request("/v5/position/trading-stop", body, method="POST")
    log(f"update_position_trading_stop response: {res}")
    return res

# ---------- SIPHON ----------
def siphon_check_and_transfer_before_trade():
    baseline = load_siphon_baseline()
    try:
        bal = get_unified_balance_usdt()
    except Exception as e:
        log(f"Siphon: could not get balance: {e}")
        return
    log(f"SIPHON CHECK: baseline={baseline} unified_balance={bal}")
    if bal >= baseline * 2:
        amount = (bal * SIPHON_RATIO).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
        body = {
            "coin": "USDT",
            "amount": fmt_price(amount),
            "fromAccountType": "UNIFIED",
            "toAccountType": FUND_ACCOUNT_TYPE
        }
        try:
            res = bybit_private_request("/v5/asset/transfer", body, method="POST")
            remaining = (bal - amount).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
            log(f"SIPHON EVENT: before={bal} siphoned={amount} remaining={remaining} api_resp={res}")
            save_siphon_baseline(remaining)
            log(f"SIPHON: new baseline persisted = {remaining}")
        except Exception as e:
            log(f"SIPHON transfer failed: {e}")

# ---------- HIGH-LEVEL TRADE HANDLING ----------
def should_modify_tp_sl(existing: dict, new_tp: Decimal, new_sl: Decimal) -> Tuple[bool, bool]:
    """
    Decide whether to modify TP and/or SL for an existing trade.
    existing: dict with keys 'entry','tp','sl','direction'
    Returns (modify_tp, modify_sl)
    Logic:
      - For buy:
          modify TP if new TP profit > current TP profit AND new SL loss <= current SL loss
          modify SL if new SL loss < current SL loss AND new TP profit >= current TP profit
      - For sell: symmetric
    """
    if not existing:
        return False, False
    direction = existing.get("direction")
    entry = dec(existing.get("entry"))
    cur_tp = dec(existing.get("tp"))
    cur_sl = dec(existing.get("sl"))
    modify_tp = False
    modify_sl = False
    if direction == "buy":
        new_tp_profit = new_tp - entry
        cur_tp_profit = cur_tp - entry
        new_sl_loss = entry - new_sl
        cur_sl_loss = entry - cur_sl
        if new_tp_profit > cur_tp_profit and new_sl_loss <= cur_sl_loss:
            modify_tp = True
        if new_sl_loss < cur_sl_loss and new_tp_profit >= cur_tp_profit:
            modify_sl = True
    elif direction == "sell":
        new_tp_profit = entry - new_tp
        cur_tp_profit = entry - cur_tp
        new_sl_loss = new_sl - entry
        cur_sl_loss = cur_sl - entry
        if new_tp_profit > cur_tp_profit and new_sl_loss <= cur_sl_loss:
            modify_tp = True
        if new_sl_loss < cur_sl_loss and new_tp_profit >= cur_tp_profit:
            modify_sl = True
    return modify_tp, modify_sl

def open_trade_if_signal(signal: str, entry_price: Decimal, sl_price: Decimal, tp_price: Decimal, balance: Decimal) -> Optional[dict]:
    # Siphon before opening
    try:
        siphon_check_and_transfer_before_trade()
    except Exception as e:
        log(f"Siphon pre-check failed: {e}")

    # sizing
    try:
        qty = calculate_qty_from_risk(balance, entry_price, sl_price, RISK_PERCENT)
    except Exception as e:
        log(f"Sizing error: {e}; using MIN_QTY")
        qty = MIN_QTY

    est_cost = dec(qty) * entry_price
    if est_cost > balance * RISK_PERCENT:
        fallback_balance = (balance * FALLBACK_USAGE)
        try:
            qty = calculate_qty_from_risk(fallback_balance, entry_price, sl_price, Decimal("1.0"))
        except Exception as e:
            log(f"Fallback sizing error: {e}; using MIN_QTY")
            qty = MIN_QTY

    if qty < MIN_QTY:
        qty = MIN_QTY

    log(f"SIZING: balance={balance} entry={entry_price} sl={sl_price} qty={qty}")

    # one-way: close opposite if present
    pos = get_current_position(SYMBOL)
    pos_qty = pos.get("qty", Decimal("0"))
    pos_side = pos.get("side")
    if pos_qty and pos_qty != 0 and pos_side:
        # close if opposite
        if pos_side.lower().startswith("buy") and signal == "sell":
            log(f"Existing LONG position detected (qty={pos_qty}). Closing before SELL.")
            try:
                close_position_reduce_only(SYMBOL, pos_qty, pos_side)
                time.sleep(1)
            except Exception as e:
                log(f"Failed to close existing long: {e}; abort")
                return None
        elif pos_side.lower().startswith("sell") and signal == "buy":
            log(f"Existing SHORT position detected (qty={pos_qty}). Closing before BUY.")
            try:
                close_position_reduce_only(SYMBOL, pos_qty, pos_side)
                time.sleep(1)
            except Exception as e:
                log(f"Failed to close existing short: {e}; abort")
                return None

    # place market order with attached TP/SL
    side_str = "Buy" if signal == "buy" else "Sell"
    try:
        resp = place_market_order_attach_tp_sl(SYMBOL, side_str, qty, tp_price, sl_price)
        log(f"Order create response: {resp}")
        return {"direction": signal, "entry": entry_price, "sl": sl_price, "tp": tp_price, "qty": qty, "api_response": resp}
    except Exception as e:
        log(f"place_market_order_attach_tp_sl failed: {e}")
        # fallback
        try:
            resp_raw = place_market_order_raw(SYMBOL, side_str, qty)
            log(f"Raw market order response: {resp_raw}")
            time.sleep(1)
            try:
                attach_resp = update_position_trading_stop(SYMBOL, take_profit=tp_price, stop_loss=sl_price)
                log(f"Attached via trading-stop: {attach_resp}")
            except Exception as ee:
                log(f"Attach via trading-stop failed: {ee}")
            return {"direction": signal, "entry": entry_price, "sl": sl_price, "tp": tp_price, "qty": qty, "api_response": resp_raw}
        except Exception as e2:
            log(f"Fallback raw market order failed: {e2}")
            return None

# ---------- MAIN LOOP ----------
def main_loop():
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        log("ERROR: BYBIT_API_KEY and BYBIT_API_SECRET must be set in environment.")
        raise SystemExit("Missing API credentials")

    saved_open, saved_close = load_ha_state()
    # load_ha_state always returns Decimals and defaults to INITIAL_HA_OPEN
    prev_ha_open = saved_open
    prev_ha_close = saved_close
    log(f"Starting with prev_ha_open={prev_ha_open} prev_ha_close={prev_ha_close}")

    if not SIPHON_FILE.exists():
        save_siphon_baseline(START_BALANCE)
        log(f"Persisted initial siphon baseline = {START_BALANCE}")

    last_processed_hour = None
    open_trade_state: Dict[str, Any] = {}

    log("Bot started (fixed). Waiting for top-of-hour...")

    while True:
        now = datetime.now(timezone.utc)
        if now.minute == 0 and now.hour != last_processed_hour:
            try:
                log(f"Top-of-hour: {now.isoformat()} - fetching candles")
                raw = fetch_recent_1h_raw(SYMBOL, limit=6)
                if not raw or len(raw) < 1:
                    log("No candles returned; skipping hour")
                    last_processed_hour = now.hour
                    time.sleep(10)
                    continue

                # compute HA series using persisted prev_ha_open & prev_ha_close
                ha_series = compute_heiken_ashi_series(raw, initial_ha_open=prev_ha_open, initial_ha_close=prev_ha_close)
                last_ha = ha_series[-1]
                # persist HA
                save_ha_state(last_ha["open"], last_ha["close"])
                prev_ha_open, prev_ha_close = last_ha["open"], last_ha["close"]

                closed = raw[-1]
                entry_price = dec(closed["close"])
                signal = detect_signal_from_ha(last_ha)
                candle_color = "green" if last_ha["close"] > last_ha["open"] else "red"

                log(json.dumps({
                    "symbol": SYMBOL,
                    "raw_last": {"open": str(closed["open"]), "high": str(closed["high"]), "low": str(closed["low"]), "close": str(closed["close"])},
                    "ha_last": {"open": str(last_ha["open"]), "high": str(last_ha["high"]), "low": str(last_ha["low"]), "close": str(last_ha["close"])},
                    "candle_color": candle_color,
                    "detected_signal": signal
                }))

                if signal in ("buy", "sell"):
                    sl_price, tp_price = compute_sl_tp_using_ha(signal, last_ha, entry_price)
                    log(f"Computed (entry={entry_price}) sl={sl_price} tp={tp_price}")

                    # siphon before trade
                    try:
                        siphon_check_and_transfer_before_trade()
                    except Exception as e:
                        log(f"Siphon pre-check failed: {e}")

                    # fetch balance
                    try:
                        balance = get_unified_balance_usdt()
                        log(f"Unified balance: {balance}")
                    except Exception as e:
                        log(f"Could not fetch unified balance: {e}; skipping trade this hour")
                        balance = None

                    if balance is None:
                        log("Skipping trade due to missing balance.")
                    else:
                        if not open_trade_state:
                            # no open trade — try to open
                            opened = open_trade_if_signal(signal, entry_price, sl_price, tp_price, balance)
                            if opened:
                                open_trade_state = opened
                                log(f"Trade opened: {opened}")
                            else:
                                log("Trade opening failed or aborted.")
                        else:
                            # trade open: consider updating TP/SL if improved
                            modify_tp, modify_sl = should_modify_tp_sl(open_trade_state, tp_price, sl_price)
                            if modify_tp or modify_sl:
                                tp_arg = tp_price if modify_tp else None
                                sl_arg = sl_price if modify_sl else None
                                try:
                                    log(f"Modifying position trading-stop modify_tp={modify_tp} modify_sl={modify_sl} tp={tp_arg} sl={sl_arg}")
                                    mod_res = update_position_trading_stop(SYMBOL, take_profit=tp_arg, stop_loss=sl_arg)
                                    log(f"Trading-stop update response: {mod_res}")
                                    if modify_tp:
                                        open_trade_state["tp"] = tp_price
                                    if modify_sl:
                                        open_trade_state["sl"] = sl_price
                                except Exception as e:
                                    log(f"trading-stop modification failed: {e}")
                            else:
                                log("Existing trade present and no beneficial TP/SL update detected.")

                else:
                    log("No signal this hour.")

                last_processed_hour = now.hour

            except Exception as e:
                log(f"Hourly processing error: {e}")

        time.sleep(5)

if __name__ == "__main__":
    main_loop()
