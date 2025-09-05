#!/usr/bin/env python3
import requests, math, logging
from datetime import datetime, timezone

# -------- CONFIG --------
SYMBOL = "TRXUSDT"
INTERVAL = "60"       # 1h
LIMIT = 200
INITIAL_HA_OPEN = 0.34996 # ⚠️ set manually from TradingView
ACCOUNT_BALANCE = 100
TICK_SIZE = 0.00001
LEVERAGE = 75
RISK_PERCENT = 0.10
FALLBACK_PERCENT = 0.90
QTY_STEP = 1
MIN_NEW_ORDER_QTY = 16

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

# -------- TRADE SIM --------
class Trade:
    def __init__(self, side, entry, sl, tp, qty):
        self.side, self.entry, self.sl, self.tp, self.qty = side, entry, sl, tp, qty

    def update(self, new_sl, new_tp):
        old_sl, old_tp = self.sl, self.tp
        improved = False
        if self.side == "Buy":
            if new_sl > self.sl: self.sl, improved = new_sl, True
            if new_tp > self.tp: self.tp, improved = new_tp, True
        else:
            if new_sl < self.sl: self.sl, improved = new_sl, True
            if new_tp < self.tp: self.tp, improved = new_tp, True
        if improved:
            logger.info("🔄 Update %s trade | Old SL=%.6f TP=%.6f -> New SL=%.6f TP=%.6f",
                        self.side, old_sl, old_tp, self.sl, self.tp)

# -------- MAIN BACKTEST --------
def backtest(balance=ACCOUNT_BALANCE):
    raw = fetch_bybit_klines(SYMBOL, INTERVAL, LIMIT)
    logger.info("Fetched %d candles. First candle UTC = %s", len(raw),
                datetime.fromtimestamp(raw[0]['ts']/1000, tz=timezone.utc))

    ha_candles = compute_heikin_ashi(raw, persisted_open=INITIAL_HA_OPEN)

    # ⚠️ Special log: the candle to match TradingView HA open
    first = ha_candles[0]
    logger.info("⚠️ Use this candle for INITIAL_HA_OPEN")
    logger.info("First Candle UTC %s | Raw O=%.5f H=%.5f L=%.5f C=%.5f | HA H=%.5f L=%.5f C=%.5f",
        datetime.fromtimestamp(first["ts"]/1000, tz=timezone.utc),
        first["raw_open"], first["raw_high"], first["raw_low"], first["raw_close"],
        first["ha_high"], first["ha_low"], first["ha_close"])

    trade = None

    for i, c in enumerate(ha_candles):
        ts = datetime.fromtimestamp(c["ts"]/1000, tz=timezone.utc)
        logger.info("Candle UTC %s | Raw O=%.5f H=%.5f L=%.5f C=%.5f | HA O=%.5f H=%.5f L=%.5f C=%.5f",
            ts, c["raw_open"], c["raw_high"], c["raw_low"], c["raw_close"],
            c["ha_open"], c["ha_high"], c["ha_low"], c["ha_close"])

        if i == 0: continue  # skip first for signals (need prev candle for SL)

        signal = evaluate_signal(c)

        if trade is None and signal:
            entry = c["ha_close"]
            if signal == "Buy":
                sl = ha_candles[i-1]["ha_low"]
                tp = entry + (entry - sl) * 1.001
            else:
                sl = ha_candles[i-1]["ha_high"]
                tp = entry - (sl - entry) * 1.001
            qty = compute_qty(entry, sl, balance)
            if qty >= MIN_NEW_ORDER_QTY:
                trade = Trade(signal, entry, sl, tp, qty)
                logger.info("📈 New %s trade | Entry=%.6f | SL=%.6f | TP=%.6f | qty=%.2f",
                            signal, entry, sl, tp, qty)

        elif trade:
            if trade.side == "Buy":
                new_sl = max(trade.sl, ha_candles[i-1]["ha_low"])
                new_tp = max(trade.tp, c["ha_close"] + (c["ha_close"] - new_sl) * 1.001)
            else:
                new_sl = min(trade.sl, ha_candles[i-1]["ha_high"])
                new_tp = min(trade.tp, c["ha_close"] - (new_sl - c["ha_close"]) * 1.001)
            trade.update(new_sl, new_tp)

    logger.info("✅ Backtest finished")

if __name__ == "__main__":
    backtest()
