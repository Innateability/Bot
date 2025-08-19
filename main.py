import requests
import logging
import time
from datetime import datetime, timedelta
from collections import deque

# -------- Logging --------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

# -------- Config --------
URL = "https://api.bybit.com/v5/market/kline"
SYMBOL = "TRXUSDT"            # change if needed
FIVE_M_HISTORY = 500          # how many 5m HA candles to keep for SL lookback
TIMEOUT = 15                  # HTTP timeout (seconds)
RR_EXTRA = 0.0007             # +0.07% buffer

# -------- API / HA helpers --------
def get_candles(symbol=SYMBOL, interval="60", limit=200):
    """Fetch candles from Bybit (newest first)."""
    params = {
        "category": "linear",
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
    (ha_open, ha_high, ha_low, ha_close, color) where color in {"GREEN","RED"}
    """
    ha = []
    for i, c in enumerate(reversed(raw)):  # chronological
        o, h, l, cl = map(float, c[1:5])
        ha_close = (o + h + l + cl) / 4.0
        if i == 0:
            ha_open = (o + cl) / 2.0
        else:
            p_open, _, _, p_close, _ = ha[-1]
            ha_open = (p_open + p_close) / 2.0
        ha_high = max(h, ha_open, ha_close)
        ha_low  = min(l, ha_open, ha_close)
        color = "GREEN" if ha_close >= ha_open else "RED"
        ha.append((ha_open, ha_high, ha_low, ha_close, color))
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

# -------- Stop-loss helpers (from most recent 5m color-change) --------
def last_red_to_green_change(ha_5m_history):
    """
    Find the most recent RED -> GREEN change in history and return
    the lower low of those two candles (for BUY stop-loss).
    History is chronological list/deque of HA tuples.
    """
    if len(ha_5m_history) < 2:
        return None
    # scan from newest backwards to find a RED then its following GREEN
    for i in range(len(ha_5m_history) - 1, 0, -1):
        cur = ha_5m_history[i]     # newer
        prev = ha_5m_history[i-1]  # older
        if prev[4] == "RED" and cur[4] == "GREEN":
            low_prev = prev[2]
            low_cur = cur[2]
            return min(low_prev, low_cur)
    return None

def last_green_to_red_change(ha_5m_history):
    """
    Find the most recent GREEN -> RED change in history and return
    the higher high of those two candles (for SELL stop-loss).
    """
    if len(ha_5m_history) < 2:
        return None
    for i in range(len(ha_5m_history) - 1, 0, -1):
        cur = ha_5m_history[i]
        prev = ha_5m_history[i-1]
        if prev[4] == "GREEN" and cur[4] == "RED":
            high_prev = prev[1]
            high_cur = cur[1]
            return max(high_prev, high_cur)
    return None

# -------- Main bot --------
def main():
    buy_level = None   # {"price": float, "expiry": datetime}
    sell_level = None  # {"price": float, "expiry": datetime}
    last_hour_processed = None

    # keep a rolling history of 5m HA candles for stop-loss detection
    ha_5m_history = deque(maxlen=FIVE_M_HISTORY)

    while True:
        now = datetime.utcnow()

        # ---- HOURLY LOGIC at exact top of hour ----
        # Run once per hour at hh:00:00
        if now.minute == 0 and now.second == 0:
            # prevent double-run within the same hour if loop wakes multiple times
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
                    _, h1, l1, c1, col1 = last_1h
                    logging.info(f"1H closed: {col1} | HA-OHLC=(_, {h1:.6f}, {l1:.6f}, {c1:.6f})")

                    # If RED -> set Buy Level to HA High (valid 1h)
                    if col1 == "RED":
                        buy_level = {"price": h1, "expiry": now + timedelta(hours=1)}
                        sell_level = None  # only one level at a time
                        logging.info(f"🎯 Buy Level set @ {buy_level['price']:.6f} (valid until {buy_level['expiry']:%Y-%m-%d %H:%M:%S} UTC)")

                    # If GREEN -> set Sell Level to HA Low (valid 1h)
                    else:  # GREEN
                        sell_level = {"price": l1, "expiry": now + timedelta(hours=1)}
                        buy_level = None
                        logging.info(f"🎯 Sell Level set @ {sell_level['price']:.6f} (valid until {sell_level['expiry']:%Y-%m-%d %H:%M:%S} UTC)")

        # ---- EVERY 5 MINUTES at exact :00/:05/:10/... ----
        try:
            raw_5m = get_candles(interval="5", limit=3)
            ha_5m = to_heikin_ashi(raw_5m)
            if ha_5m:
                # append most recent closed 5m HA candle to rolling history
                ha_5m_history.append(ha_5m[-1])
            last_5m = ha_5m[-1]
            _, h5, l5, c5, col5 = last_5m
        except Exception as e:
            logging.error(f"5M fetch/convert error: {e}")
            # still align timing
            wait_until_next_5m()
            continue

        # BUY check (only if Buy Level active and not expired)
        if buy_level is not None:
            if datetime.utcnow() >= buy_level["expiry"]:
                logging.info("⌛ Buy Level expired")
                buy_level = None
            else:
                # 5m HA high breaches Buy Level
                if h5 >= buy_level["price"]:
                    entry = buy_level["price"]
                    sl = last_red_to_green_change(list(ha_5m_history))
                    if sl is None or sl >= entry:
                        # fallback: if no valid SL found, skip signal to avoid nonsense RR
                        logging.info("⚠️ No valid 5m RED→GREEN change found for SL; skipping BUY signal this time.")
                    else:
                        risk = entry - sl
                        tp = entry + risk * (1.0 + RR_EXTRA)
                        logging.info(f"✅ BUY | Entry={entry:.6f} | SL={sl:.6f} | TP={tp:.6f}  (1:1 RR + 0.07%)")
                        # clear level after confirmed trade
                        buy_level = None

        # SELL check (only if Sell Level active and not expired)
        if sell_level is not None:
            if datetime.utcnow() >= sell_level["expiry"]:
                logging.info("⌛ Sell Level expired")
                sell_level = None
            else:
                # 5m HA low breaches Sell Level
                if l5 <= sell_level["price"]:
                    entry = sell_level["price"]
                    sl = last_green_to_red_change(list(ha_5m_history))
                    if sl is None or sl <= entry:
                        logging.info("⚠️ No valid 5m GREEN→RED change found for SL; skipping SELL signal this time.")
                    else:
                        risk = sl - entry
                        tp = entry - risk * (1.0 + RR_EXTRA)
                        logging.info(f"✅ SELL | Entry={entry:.6f} | SL={sl:.6f} | TP={tp:.6f}  (1:1 RR + 0.07%)")
                        # clear level after confirmed trade
                        sell_level = None

        # align to next :00/:05/:10/...
        wait_until_next_5m()

if __name__ == "__main__":
    main()
