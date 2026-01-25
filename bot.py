import os
import time
from datetime import datetime
import logging
from decimal import Decimal
import ccxt
import requests
from dotenv import load_dotenv
import uuid

# --- Config ---
load_dotenv()
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', 'YOUR_API_KEY')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', 'YOUR_API_SECRET')
NTFY_URL = os.getenv('NTFY_URL', 'https://ntfy.sh/mHaneysAlgoBot')
SYMBOL = 'BNB/USD'
MIN_NOTIONAL = Decimal('10')
SPREAD_PCT = Decimal('0.0005')  # Spread must be less than 0.05%
LOG_INTERVAL = 10  # seconds
CHECK_INTERVAL = 0.5  # seconds

# --- Setup ---
exchange = ccxt.binanceus({
    'apiKey': BINANCE_API_KEY,
    'secret': BINANCE_API_SECRET,
    'enableRateLimit': True,
    'timeout': 10000,
})

logging.basicConfig(
    filename='trading_bot.log',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)

positions = []  # Each: {'entry': Decimal, 'qty': Decimal}
last_log_time = 0

# --- ntfy notification ---
def send_ntfy(msg):
    try:
        requests.post(NTFY_URL, data=msg.encode('utf-8'), timeout=3)
    except Exception as e:
        logging.warning(f"ntfy notification failed: {e}")

# --- Startup ---
start_msg = "Bot Launched"
print(f"\n{start_msg}\n")
logging.info(start_msg)
send_ntfy(start_msg)

try:
    while True:
        try:
            # Fetch order book
            order_book = exchange.fetch_order_book(SYMBOL, limit=10)
            bids = order_book['bids']
            asks = order_book['asks']
            if not bids or not asks:
                print('No bids or asks available.')
                time.sleep(CHECK_INTERVAL)
                continue

            lowest_ask = Decimal(str(asks[0][0]))
            ask_qty = Decimal(str(asks[0][1]))
            # Find the highest bid that covers the lowest ask quantity
            covered_qty = Decimal('0')
            highest_covering_bid = None
            for bid_price, bid_qty in bids:
                bid_price = Decimal(str(bid_price))
                bid_qty = Decimal(str(bid_qty))
                covered_qty += bid_qty
                if covered_qty >= ask_qty:
                    highest_covering_bid = bid_price
                    break
            if highest_covering_bid is None:
                highest_covering_bid = Decimal(str(bids[0][0]))

            # --- Buy logic ---
            balance = exchange.fetch_balance()
            usd_balance = Decimal(str(balance['free'].get('USD', 0)))
            max_bnb = (usd_balance * Decimal('0.9')) / lowest_ask
            buy_qty = min(ask_qty, max_bnb)
            spread = abs((lowest_ask - highest_covering_bid) / lowest_ask)
            if buy_qty > 0 and (buy_qty * lowest_ask) >= MIN_NOTIONAL and spread < SPREAD_PCT:
                try:
                    order = exchange.create_market_buy_order(SYMBOL, float(buy_qty))
                    filled_qty = Decimal(str(order.get('filled', buy_qty)))
                    pos_uid = str(uuid.uuid4())
                    positions.append({'entry': lowest_ask, 'qty': filled_qty, 'uid': pos_uid})
                    # Initialize highest covering bid for this new position
                    if not hasattr(globals(), '_max_covering_bids'):
                        globals()['_max_covering_bids'] = {}
                    max_covering_bids = globals()['_max_covering_bids']
                    max_covering_bids[pos_uid] = highest_covering_bid
                    usd_value = filled_qty * lowest_ask
                    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    entry_log = f"[{now_str}] ENTERED position: {filled_qty:.4f} BNB @ {lowest_ask:.2f} (USD: ${usd_value:.2f})"
                    print(entry_log)
                    logging.info(entry_log)
                    # send_ntfy(entry_log)  # ntfy notification not needed for entry
                except Exception as e:
                    print(f"Buy error: {e}")
                    logging.error(f"Buy error: {e}")

            # --- Sell logic ---
            # Sell if highest covering bid decreases from previous value
            if not hasattr(globals(), '_prev_covering_bids'):
                globals()['_prev_covering_bids'] = {}
            prev_covering_bids = globals()['_prev_covering_bids']
            new_positions = []
            # Assign a unique ID to each position instance
            if not hasattr(globals(), '_max_covering_bids'):
                globals()['_max_covering_bids'] = {}
            max_covering_bids = globals()['_max_covering_bids']
            for pos in positions:
                entry = pos['entry']
                qty = pos['qty']
                pos_uid = pos.get('uid')
                # Find the highest open bid that covers this position's qty
                covered_qty = Decimal('0')
                highest_covering_bid = None
                for bid_price, bid_qty in bids:
                    bid_price = Decimal(str(bid_price))
                    bid_qty = Decimal(str(bid_qty))
                    covered_qty += bid_qty
                    if covered_qty >= qty:
                        highest_covering_bid = bid_price
                        break
                if highest_covering_bid is None:
                    highest_covering_bid = Decimal(str(bids[0][0]))
                # Always update max_covering_bids to the maximum seen so far
                prev_max = max_covering_bids.get(pos_uid, highest_covering_bid)
                max_covering_bids[pos_uid] = max(prev_max, highest_covering_bid)
                # Only sell if current bid drops below the max seen since entry
                if highest_covering_bid < max_covering_bids[pos_uid]:
                    try:
                        order = exchange.create_market_sell_order(SYMBOL, float(qty))
                        pnl_usd = (highest_covering_bid - entry) * qty
                        pnl_pct = ((highest_covering_bid - entry) / entry) * Decimal('100')
                        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        exit_log = f"[{now_str}] EXITED position: {qty:.4f} BNB @ {highest_covering_bid:.2f} (entry: {entry:.2f}) | P/L: ${pnl_usd:.2f} ({pnl_pct:.2f}%)"
                        print(exit_log)
                        logging.info(exit_log)
                        send_ntfy(exit_log)
                    except Exception as e:
                        print(f"Sell error: {e}")
                        logging.error(f"Sell error: {e}")
                else:
                    new_positions.append(pos)
            positions = new_positions

            # --- Logger ---
            now = time.time()
            if now - last_log_time >= LOG_INTERVAL:
                now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                lowest_ask_amt = Decimal(str(asks[0][1]))
                lowest_ask_price = Decimal(str(asks[0][0]))
                lowest_ask_usd = lowest_ask_amt * lowest_ask_price
                log_lines = [
                    f"[{now_str}] USD: ${usd_balance:.2f}, Spread: {spread*100:.4f}%, Lowest Ask: {lowest_ask_amt:.2f} BNB @ {lowest_ask_price:.2f} (USD: ${lowest_ask_usd:.2f})"
                ]
                if positions:
                    pos_lines = []
                    for pos in positions:
                        entry = pos['entry']
                        qty = pos['qty']
                        # Find the highest open bid that covers this position's qty
                        covered_qty = Decimal('0')
                        highest_covering_bid = None
                        for bid_price, bid_qty in bids:
                            bid_price = Decimal(str(bid_price))
                            bid_qty = Decimal(str(bid_qty))
                            covered_qty += bid_qty
                            if covered_qty >= qty:
                                highest_covering_bid = bid_price
                                break
                        if highest_covering_bid is None:
                            highest_covering_bid = Decimal(str(bids[0][0]))
                        usd_value = qty * entry
                        pos_lines.append(
                            f"Entry: {entry:.2f}, Current: {highest_covering_bid:.2f}, Value: USD: ${usd_value:.2f}"
                        )
                    log_lines.append("Positions:\n" + "\n".join(pos_lines))
                else:
                    log_lines.append("Positions: None")
                log_block = "\n".join(log_lines)
                print(log_block)
                logging.info(log_block)
                last_log_time = now
            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(CHECK_INTERVAL)
except KeyboardInterrupt:
    shutdown_msg = "Bot Shutdown"
    print(f"\n{shutdown_msg}\n")
    logging.info(shutdown_msg)
    try:
        send_ntfy(shutdown_msg)
    except Exception as e:
        print(f"ntfy shutdown notification failed: {e}")
        logging.warning(f"ntfy shutdown notification failed: {e}")
