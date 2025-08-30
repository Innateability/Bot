"""
Live Heikin-Ashi Bot for Bybit USDT Perpetual (One-Way Mode)
Features:
- Initialize HA open = 0.3 on first deploy (modifiable)
- Update HA open each hour: HA_open = (prev_HA_open + prev_HA_close)/2
- Detect buy/sell signals from HA candles
- Market orders with TP/SL attached
- Risk 10% of balance, fallback 90%
- Incremental position sizing if new qty > open qty
- 75x leverage, one-way mode
- USDT Perpetual
- Hourly execution using system clock
- Siphon 25% if balance doubles and >= $4
"""

import os, time, math, json, logging
from datetime import datetime
from pybit.unified_trading import HTTP


# ----------------- CONFIG -----------------
SYMBOL = "TRXUSDT"
TIMEFRAME = "60"
INITIAL_HA_OPEN = 0.33824       # You can change this
TICK_SIZE = 0.00001
LEVERAGE = 75
RISK_PERCENT = 0.10
FALLBACK_PERCENT = 0.90
START_SIP_BALANCE = 4.0
SIP_PERCENT = 0.25
STATE_FILE = "ha_state.json"
USE_BYBIT_API = True

API_KEY = os.environ.get("BYBIT_API_KEY")
API_SECRET = os.environ.get("BYBIT_API_SECRET")
BASE_URL = "https://api.bybit.com"

# ----------------- LOGGING -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger()

# ----------------- STATE -----------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, 'r') as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

 
session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)
# ----------------- HA COMPUTE -----------------
def compute_ha(raw_candles, persisted_open=None):
    ha_list = []
    prev_ha_open = None
    prev_ha_close = None

    for idx, c in enumerate(raw_candles):
        ro, rh, rl, rc = c['open'], c['high'], c['low'], c['close']
        ha_close = (ro + rh + rl + rc)/4
        if idx == len(raw_candles)-1:
            ha_open = persisted_open if persisted_open is not None else INITIAL_HA_OPEN
        else:
            ha_open = (prev_ha_open + prev_ha_close)/2 if prev_ha_open is not None else (ro+rc)/2
        ha_high = max(rh, ha_open, ha_close)
        ha_low = min(rl, ha_open, ha_close)
        ha_list.append({'ts':c['ts'], 'raw_open':ro, 'raw_high':rh, 'raw_low':rl, 'raw_close':rc,
                        'ha_open':ha_open, 'ha_high':ha_high, 'ha_low':ha_low, 'ha_close':ha_close})
        prev_ha_open, prev_ha_close = ha_open, ha_close
    return ha_list

# ----------------- SIGNAL -----------------
def evaluate_signal(ha_list):
    if len(ha_list)<2:
        return None
    prev, curr = ha_list[-2], ha_list[-1]
    prev_green = prev['ha_close']>prev['ha_open']
    prev_red = prev['ha_close']<prev['ha_open']

    if prev_green and abs(prev['ha_low']-prev['ha_open'])<=TICK_SIZE:
        entry = curr['raw_open']
        sl = curr['ha_open']
        tp = entry + (entry - sl) + 0.001*entry
        return {'signal':'Buy','entry':entry,'sl':sl,'tp':tp}
    if prev_red and abs(prev['ha_high']-prev['ha_open'])<=TICK_SIZE:
        entry = curr['raw_open']
        sl = curr['ha_open']
        tp = entry - ((sl-entry)+0.001*entry)
        return {'signal':'Sell','entry':entry,'sl':sl,'tp':tp}
    return None

# ----------------- BALANCE & QTY -----------------
def get_balance():
    res = session.get_wallet_balance(coin="USDT")
    return float(res['result']['USDT']['available_balance'])

def compute_qty(entry, sl, balance):
    risk_usd = balance * RISK_PERCENT
    per_unit = abs(entry-sl)
    if per_unit<=0: return 0
    qty = risk_usd/per_unit
    margin = qty*entry/LEVERAGE
    if margin<=balance:
        return qty
    fallback_qty = (balance*FALLBACK_PERCENT*LEVERAGE)/entry
    return fallback_qty

# ----------------- ORDER -----------------
def place_order(signal, qty, entry, sl, tp):
    side = "Buy" if signal=='Buy' else "Sell"
    res = session.place_active_order(symbol=SYMBOL, side=side, order_type="Market", qty=qty,
                                     time_in_force="PostOnly", reduce_only=False, take_profit=tp, stop_loss=sl)
    logger.info(f"Placed order: {side} qty={qty} entry={entry} TP={tp} SL={sl}")
    return res

def modify_open_position(new_sl, new_tp, new_qty):
    pos = session.get_position(symbol=SYMBOL)['result'][0]
    current_qty = float(pos['size'])
    side = pos['side']

    # Always update TP/SL
    session.set_trading_stop(symbol=SYMBOL, take_profit=new_tp, stop_loss=new_sl)
    logger.info(f"Modified open position TP/SL: SL={new_sl} TP={new_tp}")

    # Increase quantity if needed
    if new_qty > current_qty:
        additional_qty = new_qty - current_qty
        balance = get_balance()
        max_affordable_qty = (balance * FALLBACK_PERCENT * LEVERAGE) / pos['entry_price']
        qty_to_open = min(additional_qty, max_affordable_qty)
        if qty_to_open > 0:
            # Open extra contracts with same side
            place_order("Buy" if side=="Buy" else "Sell", qty_to_open, pos['entry_price'], new_sl, new_tp)
            logger.info(f"Increased position by {qty_to_open} contracts with updated TP/SL")
    return True

def siphon_balance(balance):
    amount = round(balance*SIP_PERCENT)
    if amount>=1:
        logger.info(f"Siphoning {amount} USD to fund account")
        # Implement transfer via Bybit transfer API if live
        return amount

# ----------------- MAIN -----------------
def run_bot():
    state = load_state()
    persisted_ha_open = state.get('last_ha_open', None)
    baseline_balance = state.get('baseline_balance', None)

    candles = session.get_kline(symbol=SYMBOL, interval=TIMEFRAME, limit=200)['result']
    raw_candles = [{'ts':c['open_time'],'open':float(c['open']),'high':float(c['high']),
                    'low':float(c['low']),'close':float(c['close'])} for c in candles]

    ha_list = compute_ha(raw_candles, persisted_open=persisted_ha_open)
    latest_ha_open = ha_list[-1]['ha_open']
    save_state({**state,'last_ha_open':latest_ha_open})

    for c in ha_list[-2:]:
        logger.info(f"RAW OHL: {c['raw_open']} {c['raw_high']} {c['raw_low']} {c['raw_close']}")
        logger.info(f"HA OHL: {c['ha_open']} {c['ha_high']} {c['ha_low']} {c['ha_close']}")

    signal = evaluate_signal(ha_list)
    if not signal:
        logger.info("No signal detected")
        return

    balance = get_balance()
    if baseline_balance is None:
        baseline_balance = balance
        state['baseline_balance']=balance
        save_state(state)

    qty = compute_qty(signal['entry'], signal['sl'], balance)
    if qty<=0: return

    # Set leverage
    session.set_leverage(symbol=SYMBOL, leverage=LEVERAGE)

    # Check open position
    pos_info = session.get_position(symbol=SYMBOL)['result'][0]
    if float(pos_info['size'])>0:
        modify_open_position(signal['sl'], signal['tp'], qty)
    else:
        place_order(signal['signal'], qty, signal['entry'], signal['sl'], signal['tp'])

    balance_after = get_balance()
    if baseline_balance>=START_SIP_BALANCE and balance_after>=2*baseline_balance:
        siphon_balance(balance_after)
        state['baseline_balance']=balance_after
        save_state(state)

# ----------------- SCHEDULER -----------------
def wait_for_next_hour():
    now = datetime.utcnow()
    sec = now.minute*60 + now.second
    wait = 3600 - sec
    logger.info(f"Waiting {wait} seconds until next hour")
    time.sleep(wait)

if __name__=="__main__":
    while True:
        run_bot()
        wait_for_next_hour()
