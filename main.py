import os
import time
import logging
from datetime import datetime, timezone
from pybit.unified_trading import HTTP

# ================== CONFIG ==================
SYMBOL = "TRXUSDT"
RISK_PER_TRADE = 0.20   # 20% of balance
FALLBACK = 0.90         # fallback % if qty unaffordable
LEVERAGE = 75
INTERVAL = "3"          # timeframe (in minutes, default 4h)
CANDLE_SECONDS = 3 * 60
ROUNDING = 5

# ================== API KEYS ==================
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

# ================== GLOBAL STATE ==================
last_signal = None
last_trade_result = "win"
last_order_id = None

# ================== FUNCTIONS ==================
def fetch_last_closed():
    """Fetch last fully closed raw candle."""
    resp = session.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=3)
    raw = resp["result"]["list"][-2]  # last fully closed
    parsed = {
        "time": int(raw[0]),
        "o": float(raw[1]),
        "h": float(raw[2]),
        "l": float(raw[3]),
        "c": float(raw[4])
    }
    logging.info(f"🕯️ Closed candle → O:{parsed['o']} H:{parsed['h']} L:{parsed['l']} C:{parsed['c']}")
    return parsed


def get_balance():
    resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    balance = float(resp["result"]["list"][0]["coin"][0]["walletBalance"])
    logging.info(f"💰 Wallet balance fetched: {balance:.4f} USDT")
    return balance


def calc_qty(balance, entry, sl):
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return 0, 0
    qty_by_risk = (balance * RISK_PER_TRADE) / sl_dist
    max_affordable = (balance * LEVERAGE * FALLBACK) / entry
    return qty_by_risk, max_affordable


def close_open_positions():
    """Close any open position and check PnL result."""
    global last_trade_result, last_order_id
    try:
        pos = session.get_positions(category="linear", symbol=SYMBOL)
        if pos["result"]["list"]:
            size = float(pos["result"]["list"][0]["size"])
            side = pos["result"]["list"][0]["side"]
            if size > 0:
                logging.info(f"🔻 Closing open {side} position of size {size}")
                session.place_order(
                    category="linear",
                    symbol=SYMBOL,
                    side="Sell" if side == "Buy" else "Buy",
                    orderType="Market",
                    qty=str(size),
                    reduceOnly=True,
                    timeInForce="IOC"
                )
                time.sleep(2)

        # check last closed trade
        resp = session.get_closed_pnl(category="linear", symbol=SYMBOL, limit=1)
        if resp["result"]["list"]:
            last = resp["result"]["list"][0]
            pnl = float(last["closedPnl"])
            last_order_id = last["orderId"]
            if pnl < 0:
                last_trade_result = "loss"
                logging.info(f"📉 Last trade LOSS (PnL={pnl})")
            else:
                last_trade_result = "win"
                logging.info(f"📈 Last trade WIN (PnL={pnl})")
    except Exception as e:
        logging.error(f"❌ Error closing position or fetching pnl: {e}")


def place_order(side, entry, sl, tp, qty, mode):
    try:
        logging.info(f"🚀 Placing {mode.upper()} {side.upper()} order | Entry={entry:.5f} SL={sl:.5f} TP={tp:.5f} Qty={qty}")
        resp = session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=side.capitalize(),
            orderType="Market",
            qty=str(int(qty)),
            timeInForce="IOC",
            reduceOnly=False,
            takeProfit=str(round(tp, ROUNDING)),
            stopLoss=str(round(sl, ROUNDING)),
            positionIdx=0  # One-way mode
        )
        logging.info(f"✅ Order response: {resp}")
    except Exception as e:
        logging.error(f"❌ Error placing order: {e}")


def process_new_candle():
    global last_signal, last_trade_result

    raw = fetch_last_closed()
    color = "green" if raw["c"] > raw["o"] else "red"
    logging.info(f"📊 Candle color → {color.upper()} | O:{raw['o']} H:{raw['h']} L:{raw['l']} C:{raw['c']}")

    signal = "buy" if color == "green" else "sell"

    if last_signal is None:
        last_signal = signal
        logging.info(f"📍 First signal: {signal.upper()} (waiting for color change)")
        return

    if signal != last_signal:
        logging.info(f"📊 New signal detected ({signal.upper()}), previous={last_signal.upper()}")
        # Close current trade and fetch last PnL
        close_open_positions()

        # Determine if recovery trade
        recovery_mode = (last_trade_result == "loss")

        balance = get_balance()
        entry = raw["c"]
        sl = raw["l"] if signal == "buy" else raw["h"]

        qty_by_risk, max_affordable = calc_qty(balance, entry, sl)
        tp_normal = entry * (1 + 0.0021) if signal == "buy" else entry * (1 - 0.0021)

        if recovery_mode:
            # Compute recovery TP using both qty-based and max-affordable recovery targets
            tp_risk = entry + (entry - sl) * 2 if signal == "buy" else entry - (sl - entry) * 2
            tp_affordable = entry + (entry - sl) * (max_affordable / qty_by_risk) if signal == "buy" else entry - (sl - entry) * (max_affordable / qty_by_risk)

            # For BUY, take lower TP; for SELL, take higher TP
            if signal == "buy":
                tp = min(tp_risk, tp_affordable)
            else:
                tp = max(tp_risk, tp_affordable)

            logging.info(f"⚡ Recovery trade mode (based on last loss) → TP Risk={tp_risk:.5f}, TP Affordable={tp_affordable:.5f}, Chosen={tp:.5f}")
        else:
            tp = tp_normal
            logging.info(f"✅ Normal trade mode → TP={tp:.5f} (+/-0.21%)")

        qty_final = int(min(qty_by_risk, max_affordable))
        if qty_final <= 0:
            logging.warning("⚠️ Quantity is zero or less, skipping trade.")
            return

        place_order(signal, entry, sl, tp, qty_final, "recovery" if recovery_mode else "normal")
        last_signal = signal
    else:
        logging.info("⏸️ No color change, no trade triggered.")


# ================== MAIN LOOP ==================
def main():
    logging.info(f"🤖 Bot started | Symbol={SYMBOL} | TF={INTERVAL}m | Leverage={LEVERAGE}x | Cross Margin | One-way mode")
    while True:
        now = datetime.now(timezone.utc)
        sec_into_cycle = (now.hour * 3600 + now.minute * 60 + now.second) % CANDLE_SECONDS
        wait = CANDLE_SECONDS - sec_into_cycle
        if wait <= 0:
            wait += CANDLE_SECONDS
        logging.info(f"⏳ Waiting {wait}s for next candle close...")
        time.sleep(wait + 3)
        try:
            process_new_candle()
        except Exception as e:
            logging.error(f"Error in main loop: {e}")


if __name__ == "__main__":
    main()
