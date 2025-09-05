import logging
from datetime import datetime, timezone

# -----------------------
# Config
# -----------------------
PAIR = "TRXUSDT"
INTERVAL = "1h"
INITIAL_HA_OPEN = 0.34957  # <-- Set this manually from TradingView
RISK_PER_TRADE = 0.1
ACCOUNT_BALANCE = 100  # example

# -----------------------
# Logging setup
# -----------------------
logging.basicConfig(
    format="%(asctime)s | %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger()

# -----------------------
# Heikin Ashi conversion
# -----------------------
def ha_convert(candles, initial_ha_open):
    ha_candles = []
    ha_open = initial_ha_open

    for i, c in enumerate(candles):
        ha_close = (c["raw_open"] + c["raw_high"] + c["raw_low"] + c["raw_close"]) / 4
        if i == 0:
            ha_open = initial_ha_open
        else:
            ha_open = (ha_open + ha_candles[-1]["ha_close"]) / 2
        ha_high = max(c["raw_high"], ha_open, ha_close)
        ha_low = min(c["raw_low"], ha_open, ha_close)

        ha_candle = {
            "ts": c["ts"],
            "raw_open": c["raw_open"],
            "raw_high": c["raw_high"],
            "raw_low": c["raw_low"],
            "raw_close": c["raw_close"],
            "ha_open": ha_open,
            "ha_high": ha_high,
            "ha_low": ha_low,
            "ha_close": ha_close
        }
        ha_candles.append(ha_candle)

    return ha_candles

# -----------------------
# Signal detection (dummy example)
# -----------------------
def detect_signal(candle):
    # Example rule: buy if HA close > HA open, sell if HA close < HA open
    if candle["ha_close"] > candle["ha_open"]:
        return {
            "action": "new",
            "side": "Buy",
            "entry": candle["raw_close"],
            "sl": candle["ha_low"],
            "tp": candle["raw_close"] + (candle["raw_close"] - candle["ha_low"]),
            "qty": (ACCOUNT_BALANCE * RISK_PER_TRADE) / candle["raw_close"]
        }
    elif candle["ha_close"] < candle["ha_open"]:
        return {
            "action": "new",
            "side": "Sell",
            "entry": candle["raw_close"],
            "sl": candle["ha_high"],
            "tp": candle["raw_close"] - (candle["ha_high"] - candle["raw_close"]),
            "qty": (ACCOUNT_BALANCE * RISK_PER_TRADE) / candle["raw_close"]
        }
    return None

# -----------------------
# Candle processing
# -----------------------
def process_candles(candles):
    candles.sort(key=lambda x: x["ts"])  # ensure chronological

    for c in candles:
        # 1. Log candle
        logger.info(
            "Candle UTC %s | Raw O=%.5f H=%.5f L=%.5f C=%.5f | HA O=%.5f H=%.5f L=%.5f C=%.5f",
            datetime.fromtimestamp(c["ts"]/1000, timezone.utc),
            c["raw_open"], c["raw_high"], c["raw_low"], c["raw_close"],
            c["ha_open"], c["ha_high"], c["ha_low"], c["ha_close"]
        )

        # 2. Trade logic after candle
        signal = detect_signal(c)
        if signal:
            if signal["action"] == "new":
                logger.info("📈 New %s trade | Entry=%.6f | SL=%.6f | TP=%.6f | qty=%.2f",
                            signal["side"], signal["entry"],
                            signal["sl"], signal["tp"], signal["qty"])

# -----------------------
# Example run
# -----------------------
if __name__ == "__main__":
    # Example raw candles (replace with Bybit fetch later)
    raw_candles = [
        {"ts": 1693142400000, "raw_open": 0.35055, "raw_high": 0.35058, "raw_low": 0.34911, "raw_close": 0.34997},
        {"ts": 1693146000000, "raw_open": 0.34997, "raw_high": 0.35020, "raw_low": 0.34880, "raw_close": 0.34950},
        {"ts": 1693149600000, "raw_open": 0.34950, "raw_high": 0.35000, "raw_low": 0.34850, "raw_close": 0.34890},
    ]

    logger.info("⚠️ Use this first candle UTC to set INITIAL_HA_OPEN from TradingView: %s",
                datetime.fromtimestamp(raw_candles[0]["ts"]/1000, timezone.utc))

    ha_candles = ha_convert(raw_candles, INITIAL_HA_OPEN)
    process_candles(ha_candles)
    
