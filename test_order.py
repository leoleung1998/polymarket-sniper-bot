"""Quick test: place a $5 GTC maker order on the current BTC UP/DOWN market."""
import os, ssl_patch
from dotenv import load_dotenv
load_dotenv()

from trader import init_client
from crypto_markets import discover_market
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
FUNDER      = os.getenv("POLYMARKET_FUNDER") or os.getenv("FUNDER")
SIG_TYPE    = int(os.getenv("SIGNATURE_TYPE", "1"))

client = init_client(PRIVATE_KEY, signature_type=SIG_TYPE, funder=FUNDER)

# Find current BTC window
market = discover_market("BTC")
if not market:
    print("No BTC market found for current window — try again mid-window")
    exit(1)

print(f"Market: {market.question}")
print(f"  UP token:   {market.up_token_id}")
print(f"  DOWN token: {market.down_token_id}")
print(f"  UP price:   ${market.up_price:.3f}")
print(f"  DOWN price: ${market.down_price:.3f}")

# Place $5 bid on UP at $0.50 (GTC maker — won't fill unless market drops to 0.50)
BID_PRICE = 0.50
SIZE = round(5.0 / BID_PRICE, 0)

print(f"\nPlacing test GTC maker order: BUY UP @ ${BID_PRICE} x {SIZE} shares (~$5)")

order_args = OrderArgs(
    token_id=market.up_token_id,
    price=BID_PRICE,
    size=SIZE,
    side=BUY,
)
signed = client.create_order(order_args)

try:
    resp = client.post_order(signed, OrderType.GTC)
    print(f"\nSUCCESS: {resp}")
except Exception as e:
    status = getattr(e, 'status_code', None)
    msg = getattr(e, 'error_msg', str(e))
    print(f"\nFAILED: HTTP {status} — {msg}")
