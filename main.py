import requests
import logging
import time
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

URL = "https://api.bybit.com/v5/market/kline"

def get_candles(symbol="TRXUSDT", interval="60", limit=200):
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }
    r = requests.get(URL, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if "result" not in data or "list" not in data["result"] or not data["result"]["list"]:
        raise RuntimeError(f"Empty kline response for interval {interval}")
    return data["result"]["list"]

def convert_to_heikin_ashi(candles):
    """
    Input: raw candles (latest first) -> Output: HA tuples (O,H,L,C,color) in chronological order
    """
    ha_candles = []
    for i, c in enumerate(reversed(candles)):  # make chronological
        open_, high, low, close = map(float, c[1:5])
        ha_close = (open_ + high + low + close) / 4.0
        if i == 0:
            ha_open = (open_ + close) / 2.0
        else:
            prev_open, _, _, prev_close, _ = ha_candles[-1]
            ha_open = (prev_open + prev_close) / 2.0

        ha_high = max(high, ha_open, ha_close)
        ha_low = min(low, ha_open, ha_close)
        color = "GREEN" if ha_close >= ha_open else "RED"

        ha_candles.append((ha_open, ha_high, ha_low, ha_close, color))
    return ha_candles

def count_consecutive_colors(ha_candles):
    """Count consecutive same-color HA candles ending at the last one (chronological list)."""
    if not ha_candles:
        return None, 0
    last_color = ha_candles[-1][4]
    count = 1
    for i in range(len(ha_candles) - 2, -1, -1):
        if ha_candles[i][4] == last_color:
            count += 1
        else:
            break
    return last_color, count

def wait_until_next_5m():
    """Sleep until the next exact 5-minute mark: xx:00, :05, :10, ..."""
    now = datetime.utcnow()
    minute_block = (now.minute // 5 + 1) * 5
    if minute_block == 60:
        next_time = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    else:
        next_time = now.replace(minute=minute_block, second=0, microsecond=0)
    wait_seconds = max(0.0, (next_time - now).total_seconds())
    logging.info(f"⏳ Waiting {wait_seconds:.0f}s until {next_time} UTC (next 5m mark)")
    time.sleep(wait_seconds)

def main_loop():
    last_higher_level = None   # updated from just-closed RED candle high on GREEN->RED change
    last_lower_level = None    # updated from just-closed GREEN candle low on RED->GREEN change
    active_buy = None          # {"price": float, "expiry": datetime}
    active_sell = None         # {"price": float, "expiry": datetime}

    while True:
        now = datetime.utcnow()

        # -------- 1H logic at exact top of the hour --------
        if now.minute == 0 and now.second == 0:
            try:
                candles_1h = get_candles(interval="60")
                ha_1h = convert_to_heikin_ashi(candles_1h)
            except Exception as e:
                logging.error(f"1H fetch/convert error: {e}")
                # still proceed to 5m check timing
            else:
                last_candle = ha_1h[-1]    # (O,H,L,C,color) just closed
                prev_candle = ha_1h[-2]    # previous 1h HA candle
                _, last_high, last_low, _, last_color = last_candle
                prev_color = prev_candle[4]

                # Streak info (if you still want it for entry rules)
                streak_color, streak_len = count_consecutive_colors(ha_1h)
                logging.info(f"1H streak: {streak_color} x{streak_len}")

                # ----- UPDATE LEVELS using JUST-CLOSED candle on color change -----
                # GREEN -> RED: higher level = high of the just-closed RED candle
                if prev_color == "GREEN" and last_color == "RED":
                    last_higher_level = last_high
                    logging.info(f"🔺 Updated Higher Level (just-closed RED high): {last_higher_level:.5f}")

                # RED -> GREEN: lower level = low of the just-closed GREEN candle
                if prev_color == "RED" and last_color == "GREEN":
                    last_lower_level = last_low
                    logging.info(f"🔻 Updated Lower Level (just-closed GREEN low): {last_lower_level:.5f}")

                # ----- ENTRY RULES (examples keeping your earlier intent) -----
                # SELL entry: after a GREEN streak and current high < last_higher_level
                if (streak_color == "GREEN" and streak_len >= 2 and
                    last_higher_level is not None and last_high < last_higher_level):
                    entry_price = last_low  # low of the just-closed GREEN candle
                    active_sell = {"price": entry_price, "expiry": now + timedelta(hours=1)}
                    logging.info(f"📉 SELL ENTRY at {entry_price:.5f} (valid until {active_sell['expiry']:%Y-%m-%d %H:%M:%S} UTC)")

                # BUY entry: after a RED streak and current low > last_lower_level
                if (streak_color == "RED" and streak_len >= 2 and
                    last_lower_level is not None and last_low > last_lower_level):
                    entry_price = last_high  # high of the just-closed RED candle
                    active_buy = {"price": entry_price, "expiry": now + timedelta(hours=1)}
                    logging.info(f"📈 BUY ENTRY at {entry_price:.5f} (valid until {active_buy['expiry']:%Y-%m-%d %H:%M:%S} UTC)")

        # -------- 5M execution checks at exact 5m marks --------
        try:
            candles_5m = get_candles(interval="5", limit=2)
            ha_5m = convert_to_heikin_ashi(candles_5m)
            last_5m = ha_5m[-1]
            _, high5, low5, _, _ = last_5m
        except Exception as e:
            logging.error(f"5M fetch/convert error: {e}")
        else:
            # Trigger SELL if 5m low <= sell entry (and still valid)
            if active_sell is not None:
                if now < active_sell["expiry"]:
                    if low5 <= active_sell["price"]:
                        logging.info(f"✅ SELL TRIGGERED at {active_sell['price']:.5f}")
                        active_sell = None
                else:
                    logging.info("⌛ SELL entry expired")
                    active_sell = None

            # Trigger BUY if 5m high >= buy entry (and still valid)
            if active_buy is not None:
                if now < active_buy["expiry"]:
                    if high5 >= active_buy["price"]:
                        logging.info(f"✅ BUY TRIGGERED at {active_buy['price']:.5f}")
                        active_buy = None
                else:
                    logging.info("⌛ BUY entry expired")
                    active_buy = None

        # always align to the next exact 5-minute mark
        wait_until_next_5m()

if __name__ == "__main__":
    main_loop()
    
