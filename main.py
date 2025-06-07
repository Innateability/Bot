import hmac
import hashlib
import time
import requests
import json
from fastapi import FastAPI, Request
import uvicorn

app = FastAPI()

# Bybit API credentials
MAIN_API_KEY = "F8pzB34lP6PvReF7Q8"
MAIN_API_SECRET = "dlfmBMFWp2FRkVPnDLp6U5wM6Ox4PZXPWtRD"
SUB_API_KEY = "kLVdNO7VFki6dgBGQE"
SUB_API_SECRET = "rRyfnNCPsS7bnh61cjkhuYYo1e30ORVro9bX"

BASE_URL = "https://api.bybit.com"

def sign_request(api_secret, params):
    query_string = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
    return hmac.new(api_secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()

def make_request(api_key, api_secret, method, endpoint, params=None):
    if params is None:
        params = {}
    timestamp = str(int(time.time() * 1000))
    params.update({
        "api_key": api_key,
        "timestamp": timestamp,
    })
    params["sign"] = sign_request(api_secret, params)

    if method == "GET":
        response = requests.get(BASE_URL + endpoint, params=params)
    else:
        response = requests.post(BASE_URL + endpoint, data=params)
    return response.json()

def get_balance(api_key, api_secret):
    data = make_request(api_key, api_secret, "GET", "/v2/private/wallet/balance", {"coin": "USDT"})
    return float(data["result"]["USDT"]["available_balance"])

def get_combined_balance():
    main = get_balance(MAIN_API_KEY, MAIN_API_SECRET)
    sub = get_balance(SUB_API_KEY, SUB_API_SECRET)
    return main + sub, main, sub

def calculate_qty(entry, sl, combined_balance, concerned_balance):
    risk = 0.10 * combined_balance
    sl_diff = abs(entry - sl)
    max_loss_per_contract = sl_diff * entry
    qty = risk / max_loss_per_contract
    max_qty = (0.4 * concerned_balance) / entry
    return min(qty, max_qty)

def place_order(api_key, api_secret, side, qty, symbol="TRXUSDT", sl=None, tp=None):
    params = {
        "symbol": symbol,
        "side": side,
        "order_type": "Market",
        "qty": round(qty, 0),
        "time_in_force": "GoodTillCancel",
        "reduce_only": False
    }
    if sl:
        params["stop_loss"] = round(sl, 5)
    if tp:
        params["take_profit"] = round(tp, 5)
    return make_request(api_key, api_secret, "POST", "/v2/private/order/create", params)

def close_position(api_key, api_secret, side, symbol="TRXUSDT"):
    pos = make_request(api_key, api_secret, "GET", "/v2/private/position/list", {"symbol": symbol})
    qty = abs(float(pos["result"][0]["size"]))
    if qty > 0:
        opposite = "Buy" if side == "Sell" else "Sell"
        return place_order(api_key, api_secret, opposite, qty, symbol)
    return {"message": "No open position"}

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.body()
    text = data.decode().strip()

    if text.lower() == "close main":
        return close_position(MAIN_API_KEY, MAIN_API_SECRET, "Sell")
    if text.lower() == "close sub":
        return close_position(SUB_API_KEY, SUB_API_SECRET, "Buy")

    try:
        lines = text.splitlines()
        symbol = lines[0].strip()
        trade_type = lines[1].split(":")[1].strip().lower()
        entry = float(lines[2].split(":")[1].strip())
        sl = float(lines[3].split(":")[1].strip())
        tp = float(lines[4].split(":")[1].strip())
    except Exception as e:
        return {"error": f"Invalid format: {e}"}

    combined, main, sub = get_combined_balance()
    if trade_type == "buy":
        concerned = sub
        key = SUB_API_KEY
        secret = SUB_API_SECRET
        side = "Buy"
    else:
        concerned = main
        key = MAIN_API_KEY
        secret = MAIN_API_SECRET
        side = "Sell"

    qty = calculate_qty(entry, sl, combined, concerned)
    return place_order(key, secret, side, qty, symbol, sl, tp)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
