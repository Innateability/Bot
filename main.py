"""
Bybit Heikin-Ashi backtest script (corrected logic)

- Fetches last 1000 1-hour raw candles for TRXUSDT from Bybit
- Converts them to Heikin-Ashi candles
- Trade rules:
    * If HA is green and HA_low == HA_open → Long entry
    * If HA is red and HA_high == HA_open → Short entry
    * Entry = same raw candle open
    * SL = HA_open of the trigger HA candle
    * TP = 1:1 RR + 0.07% of entry
    * Within the same raw candle, if both TP and SL fall inside, assume SL first
    * If neither TP nor SL hit inside the candle, exit at that candle close
- Balance simulation:
    * TP → balance *= 1.10
    * SL → balance *= 0.90
    * If closed at end without TP/SL → balance *= (1 + pct_diff/100)
- Prints final win rate and balance from $10 start
"""

import requests
import math

BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"

# --------------------- Utilities ---------------------
def fetch_candles(limit: int = 1000):
    params = {
        "symbol": "TRXUSDT",
        "category": "linear",
        "interval": "60",
        "limit": limit
    }
    r = requests.get(BYBIT_KLINE_URL, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    candles = data["result"]["list"]
    candles.sort(key=lambda c: int(c[0]))
    return candles


def heikin_ashi_from_raw(raw_candles):
    ha = []
    prev_ha_open = None
    prev_ha_close = None
    for idx, c in enumerate(raw_candles):
        ts = int(c[0])
        o = float(c[1])
        h = float(c[2])
        l = float(c[3])
        cl = float(c[4])
        ha_close = (o + h + l + cl) / 4.0
        if idx == 0:
            ha_open = (o + cl) / 2.0
        else:
            ha_open = (prev_ha_open + prev_ha_close) / 2.0
        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)
        ha.append({
            "ts": ts,
            "o": o,
            "h": h,
            "l": l,
            "c": cl,
            "ha_o": ha_open,
            "ha_h": ha_high,
            "ha_l": ha_low,
            "ha_c": ha_close,
        })
        prev_ha_open = ha_open
        prev_ha_close = ha_close
    return ha


def is_close(a, b, rel_tol=1e-9, abs_tol=1e-12):
    return math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol)


def resolve_intrabar_result(direction, entry, sl, tp, high, low):
    if direction == 'long':
        tp_hit = high >= tp
        sl_hit = low <= sl
        if tp_hit and not sl_hit:
            return 'TP'
        if sl_hit and not tp_hit:
            return 'SL'
        if tp_hit and sl_hit:
            return 'SL'  # conservative
        return 'NONE'
    else:
        tp_hit = low <= tp
        sl_hit = high >= sl
        if tp_hit and not sl_hit:
            return 'TP'
        if sl_hit and not tp_hit:
            return 'SL'
        if tp_hit and sl_hit:
            return 'SL'
        return 'NONE'


def simulate_trades(raw_candles, ha_candles, start_balance=10.0):
    balance = float(start_balance)
    total_trades = 0
    tp_count = 0
    sl_count = 0
    none_count = 0

    for i in range(len(ha_candles)):
        trigger = ha_candles[i]
        raw = raw_candles[i]

        o = float(raw[1])
        h = float(raw[2])
        l = float(raw[3])
        c = float(raw[4])

        ha_o = trigger['ha_o']
        ha_h = trigger['ha_h']
        ha_l = trigger['ha_l']
        ha_c = trigger['ha_c']

        if ha_c > ha_o and is_close(ha_l, ha_o):
            direction = 'long'
        elif ha_c < ha_o and is_close(ha_h, ha_o):
            direction = 'short'
        else:
            continue

        total_trades += 1
        entry = o
        sl = ha_o
        risk = abs(entry - sl)

        if direction == 'long':
            tp = entry + risk + (entry * 0.0007)
        else:
            tp = entry - risk - (entry * 0.0007)

        outcome = resolve_intrabar_result(direction, entry, sl, tp, h, l)

        if outcome == 'TP':
            tp_count += 1
            balance *= 1.10
        elif outcome == 'SL':
            sl_count += 1
            balance *= 0.90
        else:
            none_count += 1
            pct = (c - entry) / entry * 100.0 if direction == 'long' else (entry - c) / entry * 100.0
            balance *= (1.0 + pct / 100.0)

    win_rate = (tp_count / total_trades * 100.0) if total_trades else 0.0

    print("--- RESULTS ---")
    print(f"Total trades: {total_trades}")
    print(f"Wins (TP): {tp_count}")
    print(f"Losses (SL): {sl_count}")
    print(f"No TP/SL (closed EoH): {none_count}")
    print(f"Win rate: {win_rate:.2f}%")
    print(f"Final balance from $10: ${balance:.6f}")


if __name__ == '__main__':
    print("Fetching last 1000 1h candles for TRXUSDT...")
    raw = fetch_candles(limit=1000)
    ha = heikin_ashi_from_raw(raw)
    simulate_trades(raw, ha, start_balance=10.0)
