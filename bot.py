
import os
import time
from datetime import datetime
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
last_log_time = 0


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
            # Fetch order book
            # ...existing code...
            order_book = exchange.fetch_order_book(SYMBOL, limit=10)
            # ...existing code...
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

            # ...existing code...
            ticker = exchange.fetch_ticker(SYMBOL)
            # ...existing code...
            price = Decimal(str(ticker['last']))
            now = time.time()
            price_history.append((now, price))

            # ...existing code...
            balance = exchange.fetch_balance()
            # ...existing code...
            usd_balance = Decimal(str(balance['free'].get('USD', 0)))
            bnb_balance = Decimal(str(balance['free'].get('BNB', 0)))

            # --- Buy logic ---
            # Buy if spread < 0.1%, but account for bids already attributed to open positions
            min_notional = Decimal('10')
            agg_qty = Decimal('0')
            agg_usd = Decimal('0')
            weighted_sum = Decimal('0')
            for price_, qty_ in asks:
                price_ = Decimal(str(price_))
                qty_ = Decimal(str(qty_))
                if agg_usd < min_notional:
                    take_qty = min(qty_, ((min_notional - agg_usd) / price_))
                    agg_qty += take_qty
                    weighted_sum += take_qty * price_
                    agg_usd += take_qty * price_
                else:
                    break
            if agg_usd >= min_notional:
                weighted_avg_price = weighted_sum / agg_qty
                max_bnb = (usd_balance * Decimal('0.9')) / weighted_avg_price
                buy_qty = min(agg_qty, max_bnb)

                # Simulate bid consumption by open positions (price-priority)
                bids_remaining = [(Decimal(str(bp)), Decimal(str(bq))) for bp, bq in bids]
                open_positions = [p for p in positions if p.get('qty')]
                # Sort open positions by exit price (higher first)
                def exit_price(pos):
                    entry = pos.get('entry')
                    return max(entry * Decimal('0.998'), entry * Decimal('1.001'))
                sorted_positions = sorted(open_positions, key=exit_price, reverse=True)
                for pos in sorted_positions:
                    qty = pos.get('qty')
                    covered = Decimal('0')
                    for i, (bid_price, bid_qty) in enumerate(bids_remaining):
                        if bid_qty <= 0:
                            continue
                        take_qty = min(qty - covered, bid_qty)
                        covered += take_qty
                        bids_remaining[i] = (bid_price, bid_qty - take_qty)
                        if covered >= qty:
                            break

                # Now, for the new buy, find the highest open bid that covers the buy_qty using remaining bids
                covered_qty = Decimal('0')
                highest_covering_bid = None
                for bid_price, bid_qty in bids_remaining:
                    if bid_qty <= 0:
                        continue
                    take_qty = min(buy_qty - covered_qty, bid_qty)
                    covered_qty += take_qty
                    if covered_qty >= buy_qty:
                        highest_covering_bid = bid_price
                        break
                if highest_covering_bid is None and bids_remaining:
                    highest_covering_bid = bids_remaining[0][0]
                elif highest_covering_bid is None:
                    highest_covering_bid = Decimal('0')

                # Recalculate spread using weighted_avg_price and the true available highest covering bid
                spread_for_buy = (weighted_avg_price - highest_covering_bid) / weighted_avg_price
                if buy_qty > 0 and agg_usd >= min_notional and spread_for_buy < Decimal('0.001'):
                    try:
                        order = exchange.create_market_buy_order(SYMBOL, float(buy_qty))
                        filled_qty = Decimal(str(order.get('filled', buy_qty)))
                        positions.append({'entry': weighted_avg_price, 'qty': filled_qty})
                        usd_value = filled_qty * weighted_avg_price
                        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    except Exception as e:
                        print(f"Buy error: {e}")
                        logging.error(f"Buy error: {e}")

            # --- Sell logic ---
            # Sort positions by exit threshold (higher price first)
            def exit_price(pos):
                entry = pos.get('entry')
                return max(entry * Decimal('0.998'), entry * Decimal('1.001'))
            sorted_positions = sorted(positions, key=exit_price, reverse=True)

            # Copy bids so we can decrement as we "sell" positions
            bids_remaining = [(Decimal(str(bp)), Decimal(str(bq))) for bp, bq in bids]
            new_positions = []
            for pos in sorted_positions:
                entry = pos.get('entry')
                qty = pos.get('qty')
                if qty is None:
                    print(f"ERROR: qty missing in position: {pos}")
                    continue

                # Find the highest open bid that covers this position's qty, decrementing bids as we go
                covered_qty = Decimal('0')
                highest_covering_bid = None
                for i, (bid_price, bid_qty) in enumerate(bids_remaining):
                    if bid_qty <= 0:
                        continue
                    take_qty = min(qty - covered_qty, bid_qty)
                    covered_qty += take_qty
                    bids_remaining[i] = (bid_price, bid_qty - take_qty)
                    if covered_qty >= qty:
                        highest_covering_bid = bid_price
                        break
                if highest_covering_bid is None and bids_remaining:
                    highest_covering_bid = bids_remaining[0][0]  # fallback
                elif highest_covering_bid is None:
                    highest_covering_bid = Decimal('0')

                # Sell if highest covering bid is -0.2% or +0.1% from entry
                lower_thresh = entry * Decimal('0.998')  # -0.2%
                upper_thresh = entry * Decimal('1.001')  # +0.1%
                if highest_covering_bid <= lower_thresh or highest_covering_bid >= upper_thresh:
                    try:
                        order = exchange.create_market_sell_order(SYMBOL, float(qty))
                        pnl_usd = (highest_covering_bid - entry) * qty
                        pnl_pct = ((highest_covering_bid - entry) / entry) * Decimal('100')
                        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        # ntfy notification
                        try:
                            ntfy_msg = f"SOLD {qty} BNB at {highest_covering_bid} $ (entry: {entry})\nP/L: ${pnl_usd:.2f} ({pnl_pct:.2f}%)"
                            ntfy_msg = ntfy_msg.replace(f"$ (entry: {entry})", f" (entry: {entry})")
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
                now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                lowest_ask_amt = Decimal(str(asks[0][1]))
                lowest_ask_price = Decimal(str(asks[0][0]))
                lowest_ask_usd = lowest_ask_amt * lowest_ask_price
                log_lines = [
                    f"[{now_str}] USD: ${usd_balance:.2f}, Spread: {spread*100:.4f}%, Lowest Ask: {lowest_ask_amt} BNB @ {lowest_ask_price} (USD: ${lowest_ask_usd:.2f})"
                ]
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
                            f"Entry: {entry}, Current: {highest_covering_bid}, Low: {lower_thresh}, High: {upper_thresh}, Value: USD: ${usd_value:.2f}"
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
                    # ...existing code...
except KeyboardInterrupt:
    shutdown_msg = "Bot Shutdown"
    print(f"\n{shutdown_msg}\n")
    logging.info(shutdown_msg)
    try:
        requests.post(NTFY_URL, data=shutdown_msg.encode('utf-8'), timeout=3)
    except Exception as e:
        logging.warning(f"ntfy shutdown notification failed: {e}")
