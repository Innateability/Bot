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
INTERVAL = "240"         # timeframe in minutes (can be changed)
CANDLE_SECONDS = 4 * 60 * 60
ROUNDING = 5

# ================== API KEYS ==================
API_KEY = os.getenv("BYBIT_SUB_API_KEY")
API_SECRET = os.getenv("BYBIT_SUB_API_SECRET")
session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

# ================== GLOBAL STATE ==================
last_signal = None
last_trade_result = "win"
last_order_id = None

# ================== FUNCTIONS ==================
def fetch_last_closed():
    """Fetch last fully closed raw candle from Bybit."""
    resp = session.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=3)
    raw = resp["result"]["list"][-2]  # last fully closed
    parsed = {
        "time": int(raw[0]),
        "o": float(raw[1]),
        "h": float(raw[2]),
        "l": float(raw[3]),
        "c": float(raw[4])
    }
    logging.info(f"Parsed candle → O:{parsed['o']} H:{parsed['h']} L:{parsed['l']} C:{parsed['c']}")
    return parsed

def get_balance():
    resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    balance = float(resp["result"]["list"][0]["coin"][0]["walletBalance"])
    logging.info(f"💰 Wallet balance fetched: {balance:.4f} USDT")
    return balance

def calc_qty(balance, entry, sl, risk_amount):
    sl_dist = abs(entry - sl)
    if sl_dist <= 0:
        return 0, 0
    qty_by_risk = (risk_amount / sl_dist)
    max_affordable = (balance * LEVERAGE * FALLBACK) / entry
    logging.info(f"📐 Qty calc → SL Dist={sl_dist:.6f}, QtyByRisk={qty_by_risk:.2f}, MaxAffordable={max_affordable:.2f}")
    return qty_by_risk, max_affordable

def close_open_positions():
    """Close any open position and check last PnL result."""
    global last_trade_result, last_order_id
    try:
        pos = session.get_positions(category="linear", symbol=SYMBOL)
        if pos["result"]["list"]:
            for p in pos["result"]["list"]:
                size = float(p["size"])
                side = p["side"]
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

        # Check last closed trade PnL
        resp = session.get_closed_pnl(category="linear", symbol=SYMBOL, limit=1)
        if resp["result"]["list"]:
            last = resp["result"]["list"][0]
            pnl = float(last["closedPnl"])
            last_order_id = last["orderId"]
            if pnl < 0:
                last_trade_result = "loss"
                logging.info(f"📉 Last trade loss detected (PnL={pnl})")
            else:
                last_trade_result = "win"
                logging.info(f"📈 Last trade win detected (PnL={pnl})")
    except Exception as e:
        logging.error(f"❌ Error closing positions or fetching pnl: {e}")

def place_order(side, entry, sl, tp, qty, mode):
    global last_order_id
    try:
        logging.info(f"🚀 Placing {mode.upper()} {side.upper()} order | Entry={entry:.5f} SL={sl:.5f} TP={tp:.5f} Qty={qty}")
        resp = session.place_order(
            category="linear",
            symbol=SYMBOL,
            side=side.capitalize(),
            orderType="Market",
            qty=str(qty),
            timeInForce="IOC",
            reduceOnly=False,
            takeProfit=str(round(tp, ROUNDING)),
            stopLoss=str(round(sl, ROUNDING)),
            positionIdx=0
        )
        if "result" in resp and "orderId" in resp["result"]:
            last_order_id = resp["result"]["orderId"]
        logging.info("✅ Order response: %s", resp)
    except Exception as e:
        logging.error("❌ Error placing order: %s", e)

def process_new_candle():
    global last_signal, last_trade_result

    raw = fetch_last_closed()
    color = "green" if raw["c"] > raw["o"] else "red"

    logging.info(f"Candle {datetime.fromtimestamp(raw['time']/1000)} "
                 f"| Color={color.upper()} | O:{raw['o']} H:{raw['h']} L:{raw['l']} C:{raw['c']}")

    if last_signal is None:
        last_signal = color
        logging.info(f"📊 First signal detected ({color}), waiting for next change.")
        return

    # Detect color flip
    if color != last_signal:
        logging.info(f"📊 Color flip detected ({last_signal} → {color})")
        close_open_positions()

        balance = get_balance()
        risk_amount = balance * RISK_PER_TRADE
        entry = raw["c"]
        sl = raw["l"] if color == "green" else raw["h"]

        # Default (normal) TP calculation
        if color == "green":
            tp_normal = entry * (1 + 0.0021)
        else:
            tp_normal = entry * (1 - 0.0021)

        # Recovery mode check
        recovery_mode = (last_trade_result == "loss")

        qty_by_risk, max_affordable = calc_qty(balance, entry, sl, risk_amount)
        qty_final = int(min(qty_by_risk, max_affordable))
        if qty_final <= 0:
            logging.warning("⚠️ Quantity is zero or less, skipping trade.")
            return

        if recovery_mode:
            logging.info("⚡ Recovery trade triggered (last trade was loss).")
            # Calculate recovery TP for both qty scenarios
            last_pnl_resp = session.get_closed_pnl(category="linear", symbol=SYMBOL, limit=1)
            if last_pnl_resp["result"]["list"]:
                pnl_loss = abs(float(last_pnl_resp["result"]["list"][0]["closedPnl"]))
                logging.info(f"🔍 Last trade loss amount to recover: {pnl_loss:.5f} USDT")

                tp_by_risk = entry + (pnl_loss / (qty_by_risk * entry)) if color == "green" else entry - (pnl_loss / (qty_by_risk * entry))
                tp_max_affordable = entry + (pnl_loss / (max_affordable * entry)) if color == "green" else entry - (pnl_loss / (max_affordable * entry))
                tp_final = min(tp_by_risk, tp_max_affordable) if color == "green" else max(tp_by_risk, tp_max_affordable)

                logging.info(f"📈 TP calc (Recovery) → TP_ByRisk={tp_by_risk:.5f}, TP_MaxAff={tp_max_affordable:.5f}, FinalTP={tp_final:.5f}")
            else:
                tp_final = tp_normal
                logging.warning("⚠️ No last trade PnL found, using normal TP.")
        else:
            tp_final = tp_normal
            logging.info(f"✅ Normal mode TP = {tp_final:.5f}")

        place_order("buy" if color == "green" else "sell", entry, sl, tp_final, qty_final, "recovery" if recovery_mode else "normal")
        last_signal = color

# ================== MAIN LOOP ==================
def main():
    logging.info(f"🤖 Bot started on {INTERVAL}m timeframe (RAW candle analysis mode)")
    while True:
        now = datetime.now(timezone.utc)
        sec_into_cycle = (now.hour * 3600 + now.minute * 60 + now.second) % CANDLE_SECONDS
        wait = CANDLE_SECONDS - sec_into_cycle
        if wait <= 0:
            wait += CANDLE_SECONDS
        logging.info(f"⏳ Waiting {wait}s for next candle close...")
        time.sleep(wait + 2)
        try:
            process_new_candle()
        except Exception as e:
            logging.error(f"Error: {e}")

if __name__ == "__main__":
    main()
    
