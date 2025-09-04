import logging
from datetime import datetime, timedelta
from pybit.unified_trading import HTTP
import time

# === CONFIG ===
SYMBOL = "TRXUSDT"
INTERVAL = 60  # 1h candles
CANDLE_LIMIT = 200
INITIAL_HA_OPEN = None  # <-- put your first HA open here after reading logs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger()

# === BYBIT CLIENT ===
session = HTTP(testnet=True)  # set testnet=False for real account

def fetch_raw_candles(symbol, interval, limit=200):
    """Fetch raw OHLC candles from Bybit"""
    resp = session.get_kline(
        category="linear",
        symbol=symbol,
        interval=str(interval),
        limit=limit
    )
    return resp["result"]["list"][::-1]  # oldest → newest

def heikin_ashi_transform(candles, initial_ha_open=None):
    """Convert raw candles into Heikin-Ashi candles"""
    ha_candles = []
    ha_open = initial_ha_open

    for i, c in enumerate(candles):
        ts = int(c[0]) // 1000
        o, h, l, c_close = map(float, c[1:5])
        raw = {"ts": ts, "o": o, "h": h, "l": l, "c": c_close}

        ha_close = (o + h + l + c_close) / 4

        if ha_open is None:  # first candle
            ha_open = (o + c_close) / 2

        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)

        ha = {"ts": ts, "o": ha_open, "h": ha_high, "l": ha_low, "c": ha_close}
        ha_candles.append({"raw": raw, "ha": ha})

        ha_open = (ha_open + ha_close) / 2  # persist HA open

    return ha_candles

def backtest(symbol, interval, limit=200, initial_ha_open=None):
    candles = fetch_raw_candles(symbol, interval, limit)
    ha_candles = heikin_ashi_transform(candles, initial_ha_open)

    for i, c in enumerate(ha_candles):
        ts = datetime.utcfromtimestamp(c["raw"]["ts"])
        raw = c["raw"]
        ha = c["ha"]

        if i == 0:
            logger.info(
                "FIRST CANDLE UTC %s | Raw O=%.5f H=%.5f L=%.5f C=%.5f | "
                "HA O=%.5f H=%.5f L=%.5f C=%.5f",
                ts, raw["o"], raw["h"], raw["l"], raw["c"],
                ha["o"], ha["h"], ha["l"], ha["c"]
            )
            logger.info("👉 Use HA O=%.5f as your INITIAL_HA_OPEN", ha["o"])
        else:
            logger.info(
                "Candle UTC %s | Raw O=%.5f H=%.5f L=%.5f C=%.5f | "
                "HA O=%.5f H=%.5f L=%.5f C=%.5f",
                ts, raw["o"], raw["h"], raw["l"], raw["c"],
                ha["o"], ha["h"], ha["l"], ha["c"]
            )

if __name__ == "__main__":
    backtest(SYMBOL, INTERVAL, CANDLE_LIMIT, INITIAL_HA_OPEN)
