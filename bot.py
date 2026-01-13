import os
import time
import logging
import requests
from decimal import Decimal
import ccxt
import threading
import datetime
from dotenv import load_dotenv


# Load environment variables from .env
load_dotenv()
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', 'YOUR_API_KEY')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', 'YOUR_API_SECRET')
NTFY_URL = 'https://ntfy.sh/mHaneysAlgoBot'

exchange = ccxt.binanceus({
    'apiKey': BINANCE_API_KEY,
    'secret': BINANCE_API_SECRET,
    'enableRateLimit': True,
    'timeout': 10000,  # 10 seconds
})

SYMBOL = 'BNB/USD'
SPREAD_THRESHOLD = Decimal('0.001')  # 0.1%
MAX_USD_RATIO = Decimal('0.9')

# Logging setup
logging.basicConfig(
    filename='trading_bot.log',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)

def log_and_notify(message):
    print(message)
    logging.info(message)
    try:
        requests.post(NTFY_URL, data=message.encode('utf-8'), timeout=1)
    except Exception as e:
        logging.warning(f"ntfy notification failed: {e}")

# 24-hour stats for daily update
stats = {
    'entries': 0,
    'exits': 0,
    'last_entry': None,
    'last_exit': None,
}

def send_daily_update():
    while True:
        now = datetime.datetime.now()
        # Calculate next 8am
        next_8am = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now >= next_8am:
            next_8am += datetime.timedelta(days=1)
        wait_seconds = (next_8am - now).total_seconds()
        time.sleep(wait_seconds)
        # Compose and send update
        msg = (
            f"[24h Update]\n"
            f"Entries: {stats['entries']}\n"
            f"Exits: {stats['exits']}\n"
            f"Last Entry: {stats['last_entry']}\n"
            f"Last Exit: {stats['last_exit']}\n"
        )
        try:
            requests.post(NTFY_URL, data=msg.encode('utf-8'), timeout=5)
        except Exception as e:
            logging.warning(f"ntfy daily update failed: {e}")
        # Reset stats for next 24h
        stats['entries'] = 0
        stats['exits'] = 0
        stats['last_entry'] = None
        stats['last_exit'] = None

# Start daily update thread
threading.Thread(target=send_daily_update, daemon=True).start()

last_price = None
position = None  # {'entry': Decimal, 'qty': Decimal, 'ratchet': Decimal}
sell_trigger = None
last_status_log = 0

try:
    print("\n=== AlgoBot is starting up! ===\n")
    log_and_notify("AlgoBot has started running.")
    while True:
        try:
            # Get order book

            # Add timeouts to all ccxt calls (default 10s)
            try:
                print("[DEBUG] Fetching order book...")
                order_book = exchange.fetch_order_book(SYMBOL, limit=10)
                print("[DEBUG] Order book fetched.")
            except Exception as e:
                print(f"Order book fetch timeout or error: {e}")
                time.sleep(2)
                continue
            bids = order_book['bids']
            asks = order_book['asks']
            if not bids or not asks:
                print('No bids or asks available.')
                time.sleep(2)
                continue
            highest_bid = Decimal(str(bids[0][0]))
            lowest_ask = Decimal(str(asks[0][0]))
            spread = (lowest_ask - highest_bid) / lowest_ask

            try:
                print("[DEBUG] Fetching ticker...")
                ticker = exchange.fetch_ticker(SYMBOL)
                price = Decimal(str(ticker['last']))
                print("[DEBUG] Ticker fetched.")
            except Exception as e:
                print(f"Ticker fetch timeout or error: {e}")
                time.sleep(2)
                continue

            try:
                print("[DEBUG] Fetching balance...")
                balance = exchange.fetch_balance()
                usd_balance = Decimal(str(balance['total'].get('USD', 0)))
                print("[DEBUG] Balance fetched.")
            except Exception as e:
                print(f"Balance fetch timeout or error: {e}")
                time.sleep(2)
                continue

            # Status log every 10 seconds
            now = time.time()
            if now - last_status_log > 10:
                status_msg = f"Status: price={price}, spread={spread*100:.4f}%, highest_bid={highest_bid}, lowest_ask={lowest_ask}, position={position}"
                print(status_msg)
                logging.info(status_msg)
                last_status_log = now

            # Buy logic
            if position is None and spread < SPREAD_THRESHOLD:
                if last_price is not None and price > last_price:
                    ask_qty = Decimal(str(asks[0][1]))
                    max_qty = (usd_balance * MAX_USD_RATIO) / lowest_ask
                    buy_qty = min(ask_qty, max_qty)
                    if buy_qty > 0:
                        msg = f"ENTRY: Market buy {buy_qty} BNB at {lowest_ask} USD (spread: {spread*100:.4f}%)"
                        log_and_notify(msg)
                        order = exchange.create_market_buy_order(SYMBOL, float(buy_qty))
                        position = {
                            'entry': lowest_ask,
                            'qty': buy_qty,
                            'ratchet': Decimal('0.001'),  # +0.1% initial ratchet
                        }
                        sell_trigger = position['entry'] * (Decimal('1.0') - Decimal('0.002'))  # -0.2% stop
                        # Update stats
                        stats['entries'] += 1
                        stats['last_entry'] = f"{buy_qty} BNB at {lowest_ask} USD ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"

            # Sell and ratcheting logic
            if position is not None:
                # Find the highest bid that covers the position size
                cover_bid = None
                running_qty = Decimal('0')
                for bid_price, bid_qty in bids:
                    running_qty += Decimal(str(bid_qty))
                    if running_qty >= position['qty']:
                        cover_bid = Decimal(str(bid_price))
                        break
                if cover_bid is None:
                    cover_bid = highest_bid

                # Ratchet up if cover_bid > entry + ratchet
                ratchet_price = position['entry'] * (Decimal('1.0') + position['ratchet'])
                if cover_bid > ratchet_price:
                    position['ratchet'] += Decimal('0.001')  # move up by 0.1%
                    msg = f"RATCHET: Stop moved to +{position['ratchet']*100:.2f}% of entry."
                    logging.info(msg)

                # Update sell_trigger to new ratchet level
                sell_trigger = position['entry'] * (Decimal('1.0') + position['ratchet'])

                # If cover_bid drops to or below sell_trigger, sell
                if cover_bid <= sell_trigger:
                    msg = f"EXIT: Market sell {position['qty']} BNB at {cover_bid} USD (entry: {position['entry']}, ratchet: {position['ratchet']*100:.2f}%)"
                    log_and_notify(msg)
                    order = exchange.create_market_sell_order(SYMBOL, float(position['qty']))
                    # Update stats
                    stats['exits'] += 1
                    stats['last_exit'] = f"{position['qty']} BNB at {cover_bid} USD ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"
                    position = None
                    sell_trigger = None

            last_price = price
        except Exception as e:
            logging.error(f'Error: {e}')
            print('Error:', e)
        time.sleep(2)
except KeyboardInterrupt:
    print("\n=== AlgoBot is shutting down. ===\n")
    log_and_notify("AlgoBot has stopped running.")
