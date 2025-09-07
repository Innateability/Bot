import os
from pybit.unified_trading import HTTP

# === CONFIG ===
API_KEY = os.getenv("BYBIT_API_KEY")     # or replace with "your_key"
API_SECRET = os.getenv("BYBIT_API_SECRET")  # or replace with "your_secret"

AMOUNT = "0.1"   # amount of USDT to send
COIN = "USDT"

# Accounts:
#  UNIFIED (trading) -> Funding = 7
#  UNIFIED (trading) = 6
FROM_ACCT = "6"
TO_ACCT = "7"

# === INIT SESSION ===
session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

# === EXECUTE TRANSFER ===
try:
    resp = session.create_internal_transfer(
        transferId="transfer_001",  # must be unique each run
        coin=COIN,
        amount=AMOUNT,
        fromAccountType=FROM_ACCT,
        toAccountType=TO_ACCT
    )
    print("Transfer successful:", resp)
except Exception as e:
    print("Error:", e)
