import ccxt
import pandas as pd
import pytz
from datetime import datetime, timedelta

# =============================
# SETTINGS
# =============================
EXCHANGE = ccxt.bybit()
SYMBOL = 'TRX/USDT'
TIMEFRAMES = ['1h', '4h']
RISK = {'1h': 0.10, '4h': 0.50}
FALLBACK = 0.95
BALANCE_START = 10  # starting balance in USDT
LOOKBACK = 30  # days
NIGERIA_TZ = pytz.timezone('Africa/Lagos')

# =============================
# FETCH CANDLES
# =============================
def fetch_candles(symbol, timeframe, days):
    since = EXCHANGE.milliseconds() - days * 24 * 60 * 60 * 1000
    ohlcv = EXCHANGE.fetch_ohlcv(symbol, timeframe=timeframe, since=since)
    df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
    df['time'] = pd.to_datetime(df['timestamp'], unit='ms').dt.tz_localize('UTC').dt.tz_convert(NIGERIA_TZ)
    return df

# =============================
# STRATEGY LOGIC
# =============================
def backtest(df, timeframe):
    balance = BALANCE_START
    trades = []
    risk_pct = RISK[timeframe]
    rr = 1 if timeframe == '1h' else 2

    for i in range(8, len(df)):
        last8 = df.iloc[i-8:i]
        candle_prev = df.iloc[i-1]
        candle = df.iloc[i]

        # BUY CONDITION
        if candle['close'] > candle['open'] and candle_prev['close'] < candle_prev['open']:
            if (last8['close'] > last8['open']).sum() >= 5:
                entry = candle['close']
                sl = last8[last8['close'] < last8['open']]['low'].min() - 0.0001
                tp = entry + (entry - sl) * rr + entry * 0.001

                risk_amount = balance * risk_pct
                qty = risk_amount / abs(entry - sl)
                if qty * entry > balance:
                    qty = (balance * FALLBACK) / entry

                result, balance = simulate_trade(entry, sl, tp, qty, balance)
                trades.append([timeframe, candle['time'], entry, sl, tp, qty, result, balance])

        # SELL CONDITION
        elif candle['close'] < candle['open'] and candle_prev['close'] > candle_prev['open']:
            if (last8['close'] < last8['open']).sum() >= 5:
                entry = candle['close']
                sl = last8[last8['close'] > last8['open']]['high'].max() + 0.0001
                tp = entry - (sl - entry) * rr - entry * 0.001

                risk_amount = balance * risk_pct
                qty = risk_amount / abs(entry - sl)
                if qty * entry > balance:
                    qty = (balance * FALLBACK) / entry

                result, balance = simulate_trade(entry, sl, tp, qty, balance, sell=True)
                trades.append([timeframe, candle['time'], entry, sl, tp, qty, result, balance])

    return trades, balance

# =============================
# SIMULATE TRADE
# =============================
def simulate_trade(entry, sl, tp, qty, balance, sell=False):
    # Simplified: Assume next candles decide hit SL or TP first
    # In real backtest, you’d check each candle high/low until exit
    risk = abs(entry - sl) * qty
    reward = abs(tp - entry) * qty

    if not sell:  # BUY
        if tp > entry:
            balance += reward
            return 'WIN', balance
        else:
            balance -= risk
            return 'LOSS', balance
    else:  # SELL
        if tp < entry:
            balance += reward
            return 'WIN', balance
        else:
            balance -= risk
            return 'LOSS', balance

# =============================
# RUN BACKTEST
# =============================
all_trades = []
for tf in TIMEFRAMES:
    df = fetch_candles(SYMBOL, tf, LOOKBACK)
    trades, final_balance = backtest(df, tf)
    all_trades.extend(trades)
    print(f"{tf} Final Balance: {final_balance:.2f} USDT")

# SAVE TO CSV
cols = ['timeframe','time','entry','sl','tp','qty','result','balance']
pd.DataFrame(all_trades, columns=cols).to_csv('backtest_results.csv', index=False)
print("Results saved to backtest_results.csv")
