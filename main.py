import os
import time
import json
import logging
from datetime import datetime, timedelta, timezone
from pybit.unified_trading import HTTP
from decimal import Decimal, ROUND_DOWN

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
logger = logging.getLogger()

# Bybit session (LIVE trading)
session = HTTP(
    testnet=False,
    api_key=os.getenv("BYBIT_API_KEY"),
    api_secret=os.getenv("BYBIT_API_SECRET"),
)

# Symbol & settings
SYMBOL = "TRXUSDT"
MIN_QTY = 16
RISK_PERCENT = 0.10
FALLBACK_PERCENT = 0.90
SIPHON_START = 2.0      # start siphoning at $2
SIPHON_FACTOR = 2.0     # siphon when balance doubles
SIPHON_PORTION = 0.25   # siphon 25% each time
HA_STATE_FILE = "ha_state.json"

# ----------------------------
# State persistence
# ----------------------------
def load_state():
    if os.path.exists(HA_STATE_FILE):
        with open(HA_STATE_FILE, "r") as f:
            return json.load(f)
    # First run → initialize with given HA open and siphon start
    return {"ha_open": 0.33143, "siphon_level": SIPHON_START}

def save_state(state):
    with open(HA_STATE_FILE, "w") as f:
        json.dump(state, f)

state = load_state()
ha_open = state["ha_open"]
siphon_level = state["siphon_level"]

# ----------------------------
# Fetch candles
# ----------------------------
def fetch_last_hour_raw():
    now = datetime.now(timezone.utc)
    end_time = int((now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0).timestamp())
    try:
        resp = session.get_kline(
            category="linear", symbol=SYMBOL, interval="60", startTime=end_time*1000, limit=1
        )
        c = resp["result"]["list"][0]
        return {
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "start_at": int(c[0])
        }
    except Exception as e:
        logger.error(f"fetch error: {e}")
        return None

# ----------------------------
# Heikin Ashi calculation
# ----------------------------
def ha_from_raw(raw, ha_open_prev):
    ha_close = (raw["open"] + raw["high"] + raw["low"] + raw["close"]) / 4
    ha_open_new = (ha_open_prev + ha_close) / 2
    ha_high = max(raw["high"], ha_open_new, ha_close)
    ha_low = min(raw["low"], ha_open_new, ha_close)
    return {"open": ha_open_new, "high": ha_high, "low": ha_low, "close": ha_close}

# ----------------------------
# Get account balance
# ----------------------------
def get_balance():
    try:
        resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        return float(resp["result"]["list"][0]["totalEquity"])
    except Exception as e:
        logger.error(f"balance error: {e}")
        return None

# ----------------------------
# Place trade
# ----------------------------
def place_trade(signal, entry, sl, tp, qty):
    side = "Buy" if signal == "buy" else "Sell"
    try:
        resp = session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=side,
            orderType="Market",
            qty=str(qty),
            takeProfit=str(tp),
            stopLoss=str(sl),
            tpTriggerBy="LastPrice",
            slTriggerBy="LastPrice",
            reduceOnly=False,
        )
        logger.info(f"Order response: {resp}")
    except Exception as e:
        logger.error(f"order error: {e}")

# ----------------------------
# Fund siphoning
# ----------------------------
def siphon_if_needed(balance):
    global siphon_level
    if balance >= siphon_level * SIPHON_FACTOR:
        siphon_amt = balance * SIPHON_PORTION
        try:
            resp = session.create_internal_transfer(
                transferId=str(int(time.time())),
                coin="USDT",
                amount=str(siphon_amt),
                fromAccountType="UNIFIED",
                toAccountType="FUND",
            )
            logger.info(f"Siphoned {siphon_amt:.2f} USDT to Fund wallet: {resp}")
            siphon_level = balance - siphon_amt  # reset level to new balance
            state["siphon_level"] = siphon_level
            save_state(state)
        except Exception as e:
            logger.error(f"siphon error: {e}")

# ----------------------------
# Main loop
# ----------------------------
while True:
    now = datetime.now(timezone.utc)
    if now.minute == 0 and now.second < 10:  # top of hour
        logger.info(f"Top-of-hour: {now}")
        raw = fetch_last_hour_raw()
        if raw:
            ha = ha_from_raw(raw, ha_open)
            candle_color = "green" if ha["close"] > ha["open"] else "red"
            signal = None
            if candle_color == "green" and abs(ha_open - ha["low"]) < 1e-5:
                signal = "buy"
            elif candle_color == "red" and abs(ha_open - ha["high"]) < 1e-5:
                signal = "sell"

            logger.info({
                "symbol": SYMBOL,
                "raw_last": raw,
                "ha_last": ha,
                "candle_color": candle_color,
                "signal": signal
            })

            if signal:
                balance = get_balance()
                if balance:
                    # Siphon check
                    siphon_if_needed(balance)

                    risk_amt = balance * RISK_PERCENT
                    stop_dist = abs(raw["close"] - (ha["low"] - 0.0001 if signal=="buy" else ha["high"] + 0.0001))
                    if stop_dist <= 0:
                        logger.info("Invalid stop distance")
                    else:
                        qty = (risk_amt / stop_dist) * raw["close"]
                        qty = max(MIN_QTY, int(Decimal(qty).to_integral_value(ROUND_DOWN)))

                        # SL and TP
                        if signal == "buy":
                            sl = ha["low"] - 0.0001
                            tp = raw["close"] + (raw["close"] - sl) * 1.001
                        else:
                            sl = ha["high"] + 0.0001
                            tp = raw["close"] - (sl - raw["close"]) * 1.001

                        place_trade(signal, raw["close"], sl, tp, qty)

            # update HA open and persist
            ha_open = (ha_open + ha["close"]) / 2
            state["ha_open"] = ha_open
            save_state(state)

        time.sleep(60)  # wait a minute before rechecking
    else:
        time.sleep(1)
