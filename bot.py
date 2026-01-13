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
    last_move = ' '
    last_price_seen = None
    last_logged_price = None
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

            # Log every time the price moves
            now = time.time()
            if last_logged_price is None:
                last_logged_price = price
            if price != last_logged_price:
                now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                move_dir = '+' if price > last_logged_price else '-'
                move_msg = f"[{now_str}] Price move: {move_dir} {last_logged_price} -> {price}"
                print(move_msg)
                logging.info(move_msg)
                last_logged_price = price
            # Status log every 10 seconds (no move info)
            if now - last_status_log > 10:
                now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                status_msg = f"[{now_str}] Status: spread={spread*100:.4f}%, position={position}"
                print(status_msg)
                logging.info(status_msg)
                last_status_log = now

            # Updated entry logic: use VWAP of cumulative bids to cover lowest ask size
            if position is None:
                ask_qty = Decimal(str(asks[0][1]))
                cumulative_bid_qty = Decimal('0')
                weighted_bid_sum = Decimal('0')
                for bid_price, bid_qty in bids:
                    bid_price_dec = Decimal(str(bid_price))
                    bid_qty_dec = Decimal(str(bid_qty))
                    if cumulative_bid_qty + bid_qty_dec >= ask_qty:
                        needed_qty = ask_qty - cumulative_bid_qty
                        weighted_bid_sum += bid_price_dec * needed_qty
                        cumulative_bid_qty += needed_qty
                        break
                    else:
                        weighted_bid_sum += bid_price_dec * bid_qty_dec
                        cumulative_bid_qty += bid_qty_dec
                if cumulative_bid_qty == 0:
                    vwap_bid_price = Decimal(str(bids[0][0]))
                else:
                    vwap_bid_price = weighted_bid_sum / ask_qty
                # Calculate spread using VWAP bid price
                entry_spread = (lowest_ask - vwap_bid_price) / lowest_ask
                # Only enter if spread < threshold and price increased
                debug_entry = False
                if entry_spread < SPREAD_THRESHOLD and last_price is not None and price > last_price:
                    max_qty = (usd_balance * MAX_USD_RATIO) / lowest_ask
                    buy_qty = min(ask_qty, max_qty)
                    if buy_qty > 0:
                        minus_02 = lowest_ask * Decimal('0.998')
                        plus_01 = lowest_ask * Decimal('1.001')
                        msg = (
                            f"ENTRY: Market buy {buy_qty} BNB at {lowest_ask} USD (spread: {entry_spread*100:.4f}%)\n"
                            f"  -0.2% stop: {minus_02:.4f}  +0.1% ratchet: {plus_01:.4f}"
                        )
                        print(msg)
                        logging.info(msg)
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
                else:
                    debug_entry = True
                if debug_entry:
                    print(f"[DEBUG][ENTRY] entry_spread={entry_spread:.6f}, price={price}, last_price={last_price}, ask_qty={ask_qty}, max_qty={max_qty if 'max_qty' in locals() else 'N/A'}, buy_qty={buy_qty if 'buy_qty' in locals() else 'N/A'}")

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
                    # Calculate P/L
                    exit_price = cover_bid
                    entry_price = position['entry']
                    qty = position['qty']
                    pnl_usd = (exit_price - entry_price) * qty
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                    msg = f"EXIT: Market sell {qty} BNB at {exit_price} USD (entry: {entry_price}, ratchet: {position['ratchet']*100:.2f}%)"
                    print(msg)
                    logging.info(msg)
                    ntfy_msg = f"Position exited\nP/L: {pnl_usd:.2f} USD ({pnl_pct:.2f}%)"
                    try:
                        requests.post(NTFY_URL, data=ntfy_msg.encode('utf-8'), timeout=1)
                    except Exception as e:
                        logging.warning(f"ntfy notification failed: {e}")
                    order = exchange.create_market_sell_order(SYMBOL, float(qty))
                    # Update stats
                    stats['exits'] += 1
                    stats['last_exit'] = f"{qty} BNB at {exit_price} USD ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"
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
