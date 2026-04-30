# save as test_indices2.py
from kiteconnect import KiteConnect
from dotenv import load_dotenv
import os

load_dotenv("config/.env")

kite = KiteConnect(api_key=os.getenv("API_KEY"))
kite.set_access_token(os.getenv("ACCESS_TOKEN"))

targets = ["NIFTY IND DEFENCE", "NIFTY EV"]

instruments = kite.instruments("NSE")
for target in targets:
    matches = [i for i in instruments 
               if i["tradingsymbol"] == target]
    for m in matches:
        print(f"Symbol: {m['tradingsymbol']}")
        print(f"Name:   {m['name']}")
        print(f"Token:  {m['instrument_token']}")
        print(f"Exch:   {m['exchange']}")
        print()