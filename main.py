import requests
import logging
import time
from datetime import datetime, timedelta

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

# Bybit endpoint for kline data
URL = "https://api.bybit.com/v5/market/kline"

def get_1h_candle(symbol="TRXUSDT", interval="60", limit=2):
    """
    Fetch latest 1-hour candles (limit=2 so we can calculate HA open properly).
    """
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": interval,  # 60 minutes
        "limit": limit
    }
    response = requests.get(URL, params=params)
    data = response.json()
    return data["result"]["list"]

def convert_to_heikin_ashi(candles):
    """
    Convert standard OHLC candles to Heikin Ashi candles.
    """
    ha_candles = []

    for i, c in enumerate(reversed(candles)):  # API returns latest first
        open_, high, low, close = map(float, c[1:5])

        # Heikin Ashi close = (O + H + L + C) / 4
        ha_close = (open_ + high + low + close) / 4

        # Heikin Ashi open = (prev_ha_open + prev_ha_close) / 2
        if i == 0:
            ha_open = (open_ + close) / 2  # first candle approximation
        else:
            prev_open, _, _, prev_close = ha_candles[-1]
            ha_open = (prev_open + prev_close) / 2

        ha_high = max(high, ha_open, ha_close)
        ha_low = min(low, ha_open, ha_close)

        ha_candles.append((ha_open, ha_high, ha_low, ha_close))

    return ha_candles

def wait_until_next_hour():
    """Sleep until the start of the next full hour."""
    now = datetime.utcnow()
    next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    wait_seconds = (next_hour - now).total_seconds()
    logging.info(f"Waiting {wait_seconds:.0f} seconds until {next_hour} (UTC)")
    time.sleep(wait_seconds)

def main_loop():
    while True:
        candles = get_1h_candle()
        ha_candles = convert_to_heikin_ashi(candles)
        
        # Last Heikin Ashi candle
        ha_open, ha_high, ha_low, ha_close = ha_candles[-1]

        # Determine color
        color = "GREEN" if ha_close >= ha_open else "RED"

        logging.info(
            f"HA Candle (1h TRXUSDT) -> Open: {ha_open:.5f}, High: {ha_high:.5f}, "
            f"Low: {ha_low:.5f}, Close: {ha_close:.5f} | {color}"
        )

        # Wait until next full hour
        wait_until_next_hour()

if __name__ == "__main__":
    wait_until_next_hour()  # align to the next hour before starting
    main_loop()
