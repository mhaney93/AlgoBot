
import os
import time
import logging
from decimal import Decimal
import ccxt
import requests
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', 'YOUR_API_KEY')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', 'YOUR_API_SECRET')

exchange = ccxt.binanceus({
    'apiKey': BINANCE_API_KEY,
    'secret': BINANCE_API_SECRET,
    'enableRateLimit': True,
    'timeout': 10000,
})

SYMBOL = 'BNB/USD'
SPREAD_THRESHOLD = Decimal('0.001')  # 0.1%
MAX_USD_RATIO = Decimal('0.9')

logging.basicConfig(
    filename='trading_bot.log',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)


positions = []  # List of {'entry': Decimal, 'qty': Decimal}

# Timing and price history setup
LOG_INTERVAL = 10  # seconds
CHECK_INTERVAL = 0.5  # seconds
from collections import deque
price_history = deque(maxlen=120)  # 120 * 0.5s = 60s, enough for 30s lookback


# ntfy notification setup
NTFY_URL = os.getenv('NTFY_URL', 'https://ntfy.sh/mHaneysAlgoBot')
start_msg = "Bot Launched"
print(f"\n{start_msg}\n")
logging.info(start_msg)
try:
    requests.post(NTFY_URL, data=start_msg.encode('utf-8'), timeout=3)
except Exception as e:
    logging.warning(f"ntfy notification failed: {e}")

try:
    while True:
        try:
            # Fetch order book and ticker
            order_book = exchange.fetch_order_book(SYMBOL, limit=10)
            bids = order_book['bids']
            asks = order_book['asks']
            if not bids or not asks:
                print('No bids or asks available.')
                time.sleep(CHECK_INTERVAL)
                continue

            # Find the lowest open ask
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
                highest_covering_bid = Decimal(str(bids[0][0]))  # fallback to top bid

            spread = (lowest_ask - highest_covering_bid) / lowest_ask

            ticker = exchange.fetch_ticker(SYMBOL)
            price = Decimal(str(ticker['last']))
            now = time.time()
            price_history.append((now, price))

            balance = exchange.fetch_balance()
            usd_balance = Decimal(str(balance['free'].get('USD', 0)))
            bnb_balance = Decimal(str(balance['free'].get('BNB', 0)))

            # --- Buy logic ---
            # Buy if spread < 0.1%
            if spread < Decimal('0.001') and usd_balance > 10:
                ask_qty = Decimal(str(asks[0][1]))
                max_bnb = (usd_balance * Decimal('0.9')) / lowest_ask
                buy_qty = min(ask_qty, max_bnb)
                if buy_qty > 0 and (buy_qty * lowest_ask) >= 10:
                    try:
                        order = exchange.create_market_buy_order(SYMBOL, float(buy_qty))
                        filled_qty = Decimal(str(order.get('filled', buy_qty)))
                        positions.append({'entry': lowest_ask, 'qty': filled_qty})
                        usd_value = filled_qty * lowest_ask
                        print(f"BOUGHT {filled_qty} BNB at {lowest_ask} USD (Value: {usd_value:.2f} USD)")
                        logging.info(f"BOUGHT {filled_qty} BNB at {lowest_ask} USD (Value: {usd_value:.2f} USD)")
                    except Exception as e:
                        print(f"Buy error: {e}")
                        logging.error(f"Buy error: {e}")

            # --- Sell logic ---
            new_positions = []
            for pos in positions:
                entry = pos.get('entry')
                qty = pos.get('qty')
                if qty is None:
                    print(f"ERROR: qty missing in position: {pos}")
                    continue

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
                    highest_covering_bid = Decimal(str(bids[0][0]))  # fallback

                # Sell if highest covering bid is -0.2% or +0.1% from entry
                lower_thresh = entry * Decimal('0.998')  # -0.2%
                upper_thresh = entry * Decimal('1.001')  # +0.1%
                if highest_covering_bid <= lower_thresh or highest_covering_bid >= upper_thresh:
                    try:
                        order = exchange.create_market_sell_order(SYMBOL, float(qty))
                        pnl_usd = (highest_covering_bid - entry) * qty
                        pnl_pct = ((highest_covering_bid - entry) / entry) * Decimal('100')
                        print(f"SOLD {qty} BNB at {highest_covering_bid} USD (entry: {entry}) | P/L: {pnl_usd:.2f} USD ({pnl_pct:.2f}%)")
                        logging.info(f"SOLD {qty} BNB at {highest_covering_bid} USD (entry: {entry}) | P/L: {pnl_usd:.2f} USD ({pnl_pct:.2f}%)")
                        # ntfy notification
                        try:
                            ntfy_msg = f"SOLD {qty} BNB at {highest_covering_bid} USD (entry: {entry})\nP/L: {pnl_usd:.2f} USD ({pnl_pct:.2f}%)"
                            requests.post(NTFY_URL, data=ntfy_msg.encode('utf-8'), timeout=3)
                        except Exception as ne:
                            logging.warning(f"ntfy sale notification failed: {ne}")
                    except Exception as e:
                        print(f"Sell error: {e}")
                        logging.error(f"Sell error: {e}")
                else:
                    new_positions.append(pos)
            positions = new_positions

            # --- Status log ---
            if now - last_log_time >= LOG_INTERVAL:
                print(f"USD: {usd_balance:.2f}, Price: {price}, Spread: {spread*100:.4f}%")
                logging.info(f"USD: {usd_balance:.2f}, Price: {price}, Spread: {spread*100:.4f}%")

                # Positions update
                if positions:
                    pos_lines = []
                    for pos in positions:
                        entry = pos.get('entry')
                        qty = pos.get('qty')
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
                        lower_thresh = entry * Decimal('0.998')  # -0.2%
                        upper_thresh = entry * Decimal('1.001')  # +0.1%
                        usd_value = qty * entry
                        pos_lines.append(
                            f"Entry: {entry}, Highest Covering Bid: {highest_covering_bid}, -0.2%: {lower_thresh}, +0.1%: {upper_thresh}, Value: {usd_value:.2f} USD"
                        )
                    pos_update = "Positions: " + "; ".join(pos_lines)
                    print(pos_update)
                    logging.info(pos_update)
                else:
                    print("Positions: None")
                    logging.info("Positions: None")
                last_log_time = now
            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(CHECK_INTERVAL)
                    # ...existing code...
except KeyboardInterrupt:
    shutdown_msg = "Bot Shutdown"
    print(f"\n{shutdown_msg}\n")
    logging.info(shutdown_msg)
    try:
        requests.post(NTFY_URL, data=shutdown_msg.encode('utf-8'), timeout=3)
    except Exception as e:
        logging.warning(f"ntfy shutdown notification failed: {e}")
