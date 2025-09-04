#!/usr/bin/env python3
"""
Backtester for HA strategy on Bybit 1h candles
Implements:
- Signal from HA candle (no wick condition)
- SL from previous raw candle extreme
- TP = 1:1 RR + 0.1% entry
- Only update SL if tighter, TP if more profit
- Logs raw + HA OHLC, and TP/SL modifications
"""

import requests
import math
import logging
from datetime import datetime
from dataclasses import dataclass
from typing import List, Optional

# -------- CONFIG --------
SYMBOL = "TRXUSDT"
INTERVAL = "60"         # 1h
LIMIT = 200             # number of candles
INITIAL_HA_OPEN = 0.34894   # 🔴 put your HA open starting value here
TICK_SIZE = 0.00001
QTY_STEP = 1
LEVERAGE = 75
RISK_PERCENT = 0.10
FALLBACK_PERCENT = 0.90
MIN_NEW_ORDER_QTY = 16
INITIAL_BALANCE = 100.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("backtest")

# -------- DATA STRUCTS --------
@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float

@dataclass
class HA:
    ts: int
    ha_open: float
    ha_high: float
    ha_low: float
    ha_close: float
    raw: Candle

# -------- HELPERS --------
def floor_to_step(x, step):
    if step <= 0:
        return x
    return math.floor(x / step) * step

def round_price(p, tick=TICK_SIZE):
    return round(round(p / tick) * tick, 8)

def fetch_bybit_klines(symbol, interval, limit=200):
    url = "https://api.bybit.com/v5/market/kline"
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params)
    r.raise_for_status()
    data = r.json()
    rows = data["result"]["list"]
    candles = []
    for r in rows:
        candles.append(Candle(
            ts=int(r[0]),
            open=float(r[1]),
            high=float(r[2]),
            low=float(r[3]),
            close=float(r[4])
        ))
    candles.sort(key=lambda x: x.ts)
    return candles

def compute_qty(entry, sl, balance):
    risk_usd = balance * RISK_PERCENT
    per_contract_risk = abs(entry - sl)
    if per_contract_risk <= 0:
        return 0.0
    qty = risk_usd / per_contract_risk
    est_margin = (qty * entry) / LEVERAGE
    if est_margin > balance:
        qty = (balance * FALLBACK_PERCENT * LEVERAGE) / entry
    return floor_to_step(qty, QTY_STEP)

# -------- HEIKIN-ASHI --------
def heikin_ashi_series(raw: List[Candle]) -> List[HA]:
    out = []
    prev_open = None
    prev_close = None
    for i, c in enumerate(raw):
        ha_close = (c.open + c.high + c.low + c.close) / 4.0
        if prev_open is None:
            ha_open = (c.open + c.close) / 2.0
        else:
            ha_open = (prev_open + prev_close) / 2.0
        ha_high = max(c.high, ha_open, ha_close)
        ha_low = min(c.low, ha_open, ha_close)
        out.append(HA(c.ts, ha_open, ha_high, ha_low, ha_close, c))
        prev_open, prev_close = ha_open, ha_close
    return out

def signal_from_last(ha: HA) -> Optional[str]:
    green = ha.ha_close > ha.ha_open
    red = ha.ha_close < ha.ha_open
    if green and abs(ha.ha_low - ha.ha_open) <= TICK_SIZE:
        return "Buy"
    if red and abs(ha.ha_high - ha.ha_open) <= TICK_SIZE:
        return "Sell"
    return None

# -------- BACKTEST --------
def backtest():
    balance = INITIAL_BALANCE
    raw = fetch_bybit_klines(SYMBOL, INTERVAL, LIMIT)
    ha_all = heikin_ashi_series(raw)

    log.info("Fetched %d candles. First UTC: %s",
             len(raw), datetime.utcfromtimestamp(raw[0].ts/1000))
    log.info("⚠️ Set INITIAL_HA_OPEN = %.4f manually at deployment.", INITIAL_HA_OPEN)

    position = None
    trades = []

    def propose_levels(side, entry, prev_raw: Candle):
        if side == "Buy":
            sl = prev_raw.low
            risk = abs(entry - sl)
            tp = entry + risk + 0.001 * entry
        else:
            sl = prev_raw.high
            risk = abs(entry - sl)
            tp = entry - (risk + 0.001 * entry)
        return round_price(sl), round_price(tp)

    for i in range(1, len(raw)):
        ha_i = ha_all[i]
        sig = signal_from_last(ha_i)

        # log real + HA OHLC
        log.info("Candle UTC %s | Raw O=%.5f H=%.5f L=%.5f C=%.5f | HA O=%.5f H=%.5f L=%.5f C=%.5f",
                 datetime.utcfromtimestamp(ha_i.ts/1000),
                 ha_i.raw.open, ha_i.raw.high, ha_i.raw.low, ha_i.raw.close,
                 ha_i.ha_open, ha_i.ha_high, ha_i.ha_low, ha_i.ha_close)

        if position is None and sig:
            entry = ha_i.raw.close
            prev_raw = raw[i-1]
            sl, tp = propose_levels(sig, entry, prev_raw)
            qty = compute_qty(entry, sl, balance)
            qty = max(qty, MIN_NEW_ORDER_QTY)
            if qty <= 0:
                continue
            position = {"side": sig, "entry": entry, "sl": sl, "tp": tp, "qty": qty}
            trades.append(position.copy())
            log.info("OPEN %s | entry=%.5f sl=%.5f tp=%.5f qty=%.2f",
                     sig, entry, sl, tp, qty)
            continue

        if position:
            side = position["side"]
            entry = position["entry"]
            cur_sl, cur_tp = position["sl"], position["tp"]

            prev_raw = raw[i-1]
            prop_sl, prop_tp = propose_levels(side, entry, prev_raw)

            new_sl, new_tp = cur_sl, cur_tp
            if side == "Buy":
                if prop_sl > cur_sl: new_sl = prop_sl
                if prop_tp > cur_tp: new_tp = prop_tp
            else:
                if prop_sl < cur_sl: new_sl = prop_sl
                if prop_tp < cur_tp: new_tp = prop_tp

            if new_sl != cur_sl or new_tp != cur_tp:
                log.info("UPDATE %s | SL %.5f→%.5f | TP %.5f→%.5f",
                         side, cur_sl, new_sl, cur_tp, new_tp)
                position["sl"], position["tp"] = new_sl, new_tp

            c = raw[i]
            exit_reason, exit_price = None, None
            if side == "Buy":
                if c.low <= position["sl"]:
                    exit_reason, exit_price = "SL", position["sl"]
                elif c.high >= position["tp"]:
                    exit_reason, exit_price = "TP", position["tp"]
            else:
                if c.high >= position["sl"]:
                    exit_reason, exit_price = "SL", position["sl"]
                elif c.low <= position["tp"]:
                    exit_reason, exit_price = "TP", position["tp"]

            if exit_reason:
                pnl = position["qty"] * (exit_price - entry) * (1 if side == "Buy" else -1)
                balance += pnl
                log.info("CLOSE %s | %s at %.5f | PnL=%.4f | Balance=%.4f",
                         side, exit_reason, exit_price, pnl, balance)
                position = None

    log.info("Backtest finished. Trades=%d | Final balance=%.4f", len(trades), balance)

if __name__ == "__main__":
    backtest()
