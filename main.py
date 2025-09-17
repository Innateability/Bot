import requests
from datetime import datetime, timedelta, timezone

# ===== Nigeria Timezone =====
NIGERIA_TZ = timezone(timedelta(hours=1))  # UTC+1

# ===== Config =====
PAIR = "TRXUSDT"
BASE_URL = "https://api.bybit.com"

# Hardcoded initial HA open values (adjust these for consistency with TradingView)
initial_open_4h = 0.33
initial_open_1h = 0.35

# Separate balances
balance_4h = 1000.0
balance_1h = 1000.0

# ===== Candle Fetch =====
def fetch_candles(symbol, interval, limit=200):
    url = f"{BASE_URL}/v5/market/kline"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    res = requests.get(url, params=params).json()
    candles = []
    for c in res["result"]["list"][::-1]:  # reverse to oldest → newest
        candles.append({
            "time": datetime.fromtimestamp(int(c[0]) / 1000, tz=NIGERIA_TZ),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4])
        })
    return candles

# ===== Heikin Ashi =====
def heikin_ashi(candles, initial_open=None):
    ha_candles = []
    ha_open = initial_open if initial_open is not None else candles[0]["open"]

    for i, c in enumerate(candles):
        ha_close = (c["open"] + c["high"] + c["low"] + c["close"]) / 4
        if i == 0 and initial_open is None:
            ha_open = (candles[0]["open"] + candles[0]["close"]) / 2
            print(f"[INIT] time={c['time']} raw_open={c['open']} raw_close={c['close']} "
                  f"ha_open={ha_open} ha_close={ha_close}")
        else:
            ha_open = (ha_open + ha_close) / 2
            print(f"[HA] time={c['time']} raw_open={c['open']} raw_close={c['close']} "
                  f"ha_open={ha_open} ha_close={ha_close}")

        ha_high = max(c["high"], ha_open, ha_close)
        ha_low = min(c["low"], ha_open, ha_close)

        ha_candles.append({
            "time": c["time"],
            "ha_open": ha_open,
            "ha_close": ha_close,
            "ha_high": ha_high,
            "ha_low": ha_low,
            "raw_open": c["open"],
            "raw_close": c["close"],
            "raw_high": c["high"],
            "raw_low": c["low"],
        })
    return ha_candles

# ===== Strategy Signal (simplified) =====
def check_signals(ha_candles, timeframe, balance):
    active_trade = None
    for i in range(2, len(ha_candles)):
        prev = ha_candles[i - 1]
        curr = ha_candles[i]

        # Example: Buy if color flips to green
        if curr["ha_close"] > curr["ha_open"] and prev["ha_close"] < prev["ha_open"]:
            print(f"[{timeframe}] Signal: BUY at {curr['time']} (ha_open={curr['ha_open']}, ha_close={curr['ha_close']})")
            print(f"[{timeframe}] Balance before trade: {balance}")
            if active_trade:
                print(f"[{timeframe}] Skipped BUY (active trade exists)")
            else:
                active_trade = {"type": "buy", "entry": curr["ha_close"], "sl": curr["ha_low"], "tp": curr["ha_close"] * 1.015}
                print(f"[{timeframe}] Opened BUY trade: {active_trade}")

        # Example: Sell if color flips to red
        elif curr["ha_close"] < curr["ha_open"] and prev["ha_close"] > prev["ha_open"]:
            print(f"[{timeframe}] Signal: SELL at {curr['time']} (ha_open={curr['ha_open']}, ha_close={curr['ha_close']})")
            print(f"[{timeframe}] Balance before trade: {balance}")
            if active_trade:
                print(f"[{timeframe}] Skipped SELL (active trade exists)")
            else:
                active_trade = {"type": "sell", "entry": curr["ha_close"], "sl": curr["ha_high"], "tp": curr["ha_close"] * 0.985}
                print(f"[{timeframe}] Opened SELL trade: {active_trade}")

# ===== Runner =====
def run_bot():
    global balance_4h, balance_1h

    c4h = fetch_candles(PAIR, "240", 200)
    c1h = fetch_candles(PAIR, "60", 200)

    print(f"\n[4H] First candle time: {c4h[0]['time']}  open={c4h[0]['open']}")
    print(f"[1H] First candle time: {c1h[0]['time']}  open={c1h[0]['open']}\n")

    ha_4h = heikin_ashi(c4h, initial_open=initial_open_4h)
    ha_1h = heikin_ashi(c1h, initial_open=initial_open_1h)

    check_signals(ha_4h, "4H", balance_4h)
    check_signals(ha_1h, "1H", balance_1h)


if __name__ == "__main__":
    run_bot()
