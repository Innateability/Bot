#!/usr/bin/env python3
import requests, math, logging
from datetime import datetime, timezone

# -------- CONFIG --------
SYMBOL = "TRXUSDT"
INTERVAL = "60"       # 1h
LIMIT = 200
INITIAL_HA_OPEN = 0.34957 # ⚠️ set manually from TradingView
ACCOUNT_BALANCE = 100
TICK_SIZE = 0.00001
LEVERAGE = 75
RISK_PERCENT = 0.10
FALLBACK_PERCENT = 0.90
QTY_STEP = 1
MIN_NEW_ORDER_QTY = 16
RR = 1.0  # Risk Reward ratio (1:1 default)

# -------- LOGGING --------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger("backtest")

# -------- HELPERS --------
def floor_to_step(x, step):
    return math.floor(x / step) * step if step > 0 else x

def round_price(p, tick=TICK_SIZE):
    return round(round(p / tick) * tick, 8)

def fetch_bybit_klines(symbol, interval, limit=200):
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params); r.raise_for_status()
    rows = r.json()["result"]["list"]
    candles = [{"ts": int(x[0]), "open": float(x[1]), "high": float(x[2]),
                "low": float(x[3]), "close": float(x[4])} for x in rows]
    candles.sort(key=lambda x: x["ts"])
    return candles

def compute_heikin_ashi(raw_candles, persisted_open=None):
    ha = []
    prev_ha_open, prev_ha_close = None, None
    for i, c in enumerate(raw_candles):
        ro, rh, rl, rc = c["open"], c["high"], c["low"], c["close"]
        ha_close = (ro + rh + rl + rc) / 4.0
        if i == 0 and persisted_open is not None:
            ha_open = persisted_open
        else:
            ha_open = (ro + rc) / 2.0 if prev_ha_open is None else (prev_ha_open + prev_ha_close) / 2.0
        ha_high, ha_low = max(rh, ha_open, ha_close), min(rl, ha_open, ha_close)
        ha.append({"ts": c["ts"], "raw_open": ro, "raw_high": rh, "raw_low": rl, "raw_close": rc,
                   "ha_open": ha_open, "ha_high": ha_high, "ha_low": ha_low, "ha_close": ha_close})
        prev_ha_open, prev_ha_close = ha_open, ha_close
    return ha

def evaluate_signal(last):
    green, red = last["ha_close"] > last["ha_open"], last["ha_close"] < last["ha_open"]
    if green and abs(last["ha_low"] - last["ha_open"]) <= TICK_SIZE: return "Buy"
    if red and abs(last["ha_high"] - last["ha_open"]) <= TICK_SIZE: return "Sell"
    return None

def compute_qty(entry, sl, balance):
    risk_usd = balance * RISK_PERCENT
    per_contract_risk = abs(entry - sl)
    if per_contract_risk <= 0: return 0
    qty = risk_usd / per_contract_risk
    if (qty * entry) / LEVERAGE > balance:
        qty = (balance * FALLBACK_PERCENT * LEVERAGE) / entry
    return floor_to_step(qty, QTY_STEP)

# -------- MAIN BACKTEST --------
def backtest(balance=ACCOUNT_BALANCE):
    raw = fetch_bybit_klines(SYMBOL, INTERVAL, LIMIT)
    logger.info("Fetched %d candles. First candle UTC = %s", len(raw),
                datetime.fromtimestamp(raw[0]['ts']/1000, tz=timezone.utc))

    ha_candles = compute_heikin_ashi(raw, persisted_open=INITIAL_HA_OPEN)

    trade = None  # active trade
    trades = []   # history

    for c in ha_candles:
        ts = datetime.fromtimestamp(c["ts"]/1000, tz=timezone.utc)
        signal = evaluate_signal(c)

        # --- If trade is open, check SL/TP ---
        if trade:
            if trade["type"] == "Buy":
                if c["raw_low"] <= trade["sl"]:
                    logger.info("❌ SL hit for Buy | Entry=%.5f SL=%.5f", trade["entry"], trade["sl"])
                    trades.append({**trade, "exit": trade["sl"], "result": "loss"})
                    trade = None
                elif c["raw_high"] >= trade["tp"]:
                    logger.info("✅ TP hit for Buy | Entry=%.5f TP=%.5f", trade["entry"], trade["tp"])
                    trades.append({**trade, "exit": trade["tp"], "result": "win"})
                    trade = None

            elif trade["type"] == "Sell":
                if c["raw_high"] >= trade["sl"]:
                    logger.info("❌ SL hit for Sell | Entry=%.5f SL=%.5f", trade["entry"], trade["sl"])
                    trades.append({**trade, "exit": trade["sl"], "result": "loss"})
                    trade = None
                elif c["raw_low"] <= trade["tp"]:
                    logger.info("✅ TP hit for Sell | Entry=%.5f TP=%.5f", trade["entry"], trade["tp"])
                    trades.append({**trade, "exit": trade["tp"], "result": "win"})
                    trade = None

        # --- Open new trade if signal ---
        if signal and not trade:
            entry = c["raw_close"]
            if signal == "Buy":
                sl = c["ha_low"]
                tp = entry + (entry - sl) * RR
            else:
                sl = c["ha_high"]
                tp = entry - (sl - entry) * RR
            qty = compute_qty(entry, sl, balance)
            if qty >= MIN_NEW_ORDER_QTY:
                trade = {"type": signal, "entry": entry, "sl": sl, "tp": tp, "qty": qty, "time": ts}
                logger.info("📈 New %s trade | Entry=%.5f SL=%.5f TP=%.5f | qty=%.2f",
                            signal, entry, sl, tp, qty)

    # --- Summary ---
    wins = sum(1 for t in trades if t["result"] == "win")
    losses = sum(1 for t in trades if t["result"] == "loss")
    logger.info("📊 Backtest complete | Total trades=%d Wins=%d Losses=%d", len(trades), wins, losses)

if __name__ == "__main__":
    backtest()
