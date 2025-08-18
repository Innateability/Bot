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
    response = requests.get(URL, params=params)
    data = response.json()
    return data["result"]["list"]

def convert_to_heikin_ashi(candles):
    ha_candles = []
    for i, c in enumerate(reversed(candles)):  # latest last
        open_, high, low, close = map(float, c[1:5])
        ha_close = (open_ + high + low + close) / 4
        if i == 0:
            ha_open = (open_ + close) / 2
        else:
            prev_open, _, _, prev_close, _ = ha_candles[-1]
            ha_open = (prev_open + prev_close) / 2

        ha_high = max(high, ha_open, ha_close)
        ha_low = min(low, ha_open, ha_close)
        color = "GREEN" if ha_close >= ha_open else "RED"

        ha_candles.append((ha_open, ha_high, ha_low, ha_close, color))
    return ha_candles

def wait_until_next(interval_minutes=5):
    now = datetime.utcnow()
    next_time = (now + timedelta(minutes=interval_minutes)).replace(second=0, microsecond=0)
    wait_seconds = (next_time - now).total_seconds()
    logging.info(f"Waiting {wait_seconds:.0f}s until {next_time} UTC")
    time.sleep(wait_seconds)

def count_consecutive_colors(candles):
    """Count consecutive same-color candles ending at the last one"""
    if not candles:
        return None, 0
    last_color = candles[-1][4]
    count = 1
    for i in range(len(candles) - 2, -1, -1):
        if candles[i][4] == last_color:
            count += 1
        else:
            break
    return last_color, count

def main_loop():
    last_higher_level = None
    last_lower_level = None
    active_buy = None   # {"price": float, "expiry": datetime}
    active_sell = None  # {"price": float, "expiry": datetime}

    while True:
        now = datetime.utcnow()

        # --- 1H logic ---
        if now.minute == 0:  # run on the hour
            candles_1h = get_candles(interval="60")
            ha_candles_1h = convert_to_heikin_ashi(candles_1h)
            last_candle = ha_candles_1h[-1]
            prev_candle = ha_candles_1h[-2]
            _, high, low, _, color = last_candle

            # Count consecutive streak
            streak_color, streak_len = count_consecutive_colors(ha_candles_1h)

            # SELL ENTRY condition: after green streak, but highs below last higher level
            if streak_color == "GREEN" and streak_len >= 2 and last_higher_level and high < last_higher_level:
                entry_price = low
                active_sell = {"price": entry_price, "expiry": now + timedelta(hours=1)}
                logging.info(f"📉 SELL ENTRY at {entry_price:.5f}, valid until {active_sell['expiry']}")

            # BUY ENTRY condition: after red streak, but lows above last lower level
            if streak_color == "RED" and streak_len >= 2 and last_lower_level and low > last_lower_level:
                entry_price = high
                active_buy = {"price": entry_price, "expiry": now + timedelta(hours=1)}
                logging.info(f"📈 BUY ENTRY at {entry_price:.5f}, valid until {active_buy['expiry']}")

            # Update levels on color change
            if prev_candle[4] == "GREEN" and color == "RED":
                last_higher_level = max(prev_candle[1], high)
                logging.info(f"Updated Higher Level: {last_higher_level:.5f}")
            if prev_candle[4] == "RED" and color == "GREEN":
                last_lower_level = min(prev_candle[2], low)
                logging.info(f"Updated Lower Level: {last_lower_level:.5f}")

        # --- 5M check for triggers ---
        candles_5m = get_candles(interval="5", limit=2)
        ha_candles_5m = convert_to_heikin_ashi(candles_5m)
        last_5m = ha_candles_5m[-1]
        _, high5, low5, _, _ = last_5m

        if active_sell and now < active_sell["expiry"]:
            if low5 <= active_sell["price"]:
                logging.info(f"✅ SELL TRIGGERED at {active_sell['price']:.5f}")
                active_sell = None

        if active_buy and now < active_buy["expiry"]:
            if high5 >= active_buy["price"]:
                logging.info(f"✅ BUY TRIGGERED at {active_buy['price']:.5f}")
                active_buy = None

        # Wait until next 5m
        wait_until_next(5)

if __name__ == "__main__":
    main_loop()

