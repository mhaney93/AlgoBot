import os
import time
from datetime import datetime
import logging
from decimal import Decimal
import ccxt
import requests
from dotenv import load_dotenv
import uuid
from collections import deque

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

# --- Persistent state for position tracking ---
if not hasattr(globals(), '_max_covering_bids'):
    globals()['_max_covering_bids'] = {}
if not hasattr(globals(), '_pending_sell_times'):
    globals()['_pending_sell_times'] = {}

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



# --- SMA setup ---
SMA7_PERIOD = 7
SMA25_PERIOD = 25
SMA99_PERIOD = 99
SHARPNESS_LOOKBACK = 3  # periods to look back for sharpness
SHARPNESS_THRESHOLD = 0.5  # gap must shrink by at least 50%


# Store 1m candle closes
closes = deque(maxlen=SMA99_PERIOD + SHARPNESS_LOOKBACK + 2)  # +2 for crossover lookback

try:
    while True:
        try:

            # Fetch 1m candles (OHLCV)
            ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe='1m', limit=SMA99_PERIOD + SHARPNESS_LOOKBACK + 2)
            closes.clear()
            for candle in ohlcv:
                closes.append(float(candle[4]))  # candle[4] is close price

            # Fetch order book for sizing and execution
            order_book = exchange.fetch_order_book(SYMBOL, limit=10)
            bids = order_book['bids']
            asks = order_book['asks']
            if not bids or not asks:
                print('No bids or asks available.')
                time.sleep(CHECK_INTERVAL)
                continue

            lowest_ask = Decimal(str(asks[0][0]))
            ask_qty = Decimal(str(asks[0][1]))


            # Calculate SMAs if enough data
            sma7 = None
            sma25 = None
            sma99 = None
            if len(closes) >= SMA99_PERIOD:
                closes_list = list(closes)
                sma7 = sum(closes_list[-SMA7_PERIOD:]) / SMA7_PERIOD
                sma25 = sum(closes_list[-SMA25_PERIOD:]) / SMA25_PERIOD
                sma99 = sum(closes_list[-SMA99_PERIOD:]) / SMA99_PERIOD

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

            # --- Buy logic: SMA7 approaches SMA99 sharply from below ---
            balance = exchange.fetch_balance()
            usd_balance = Decimal(str(balance['free'].get('USD', 0)))


            # Calculate total USD value of all trades in the last SMA7_PERIOD 1m candles
            total_usd_in_ma7 = Decimal('0')
            try:
                # Fetch 1m candles with volume
                # ohlcv: [timestamp, open, high, low, close, volume]
                ohlcv_list = list(ohlcv)
                for candle in ohlcv_list[-SMA7_PERIOD:]:
                    close = Decimal(str(candle[4]))
                    volume = Decimal(str(candle[5]))
                    total_usd_in_ma7 += close * volume
            except Exception as e:
                print(f"Candle volume fetch error: {e}")
                logging.warning(f"Candle volume fetch error: {e}")

            max_usd = usd_balance * Decimal('0.9')
            buy_usd = min(max_usd, total_usd_in_ma7)
            buy_qty = buy_usd / lowest_ask if lowest_ask > 0 else Decimal('0')

            buy_signal = False
            if (
                sma7 is not None and sma99 is not None
            ):
                closes_list = list(closes)
                # Only consider if ma7 is below ma99
                if sma7 < sma99:
                    # Calculate gap now and SHARPNESS_LOOKBACK periods ago
                    gap_now = sma99 - sma7
                    gap_then = None
                    if len(closes) >= SMA99_PERIOD + SHARPNESS_LOOKBACK:
                        sma7_then = sum(closes_list[-SMA7_PERIOD-SHARPNESS_LOOKBACK:-SHARPNESS_LOOKBACK]) / SMA7_PERIOD
                        sma99_then = sum(closes_list[-SMA99_PERIOD-SHARPNESS_LOOKBACK:-SHARPNESS_LOOKBACK]) / SMA99_PERIOD
                        gap_then = sma99_then - sma7_then
                        # If the gap has shrunk by at least SHARPNESS_THRESHOLD (e.g., 50%)
                        if gap_then > 0 and gap_now / gap_then <= (1 - SHARPNESS_THRESHOLD):
                            buy_signal = True

            if buy_signal and buy_qty > 0 and (buy_qty * lowest_ask) >= MIN_NOTIONAL:
                try:
                    order = exchange.create_market_buy_order(SYMBOL, float(buy_qty))
                    filled_qty = Decimal(str(order.get('filled', buy_qty)))
                    pos_uid = str(uuid.uuid4())
                    positions.append({'entry': lowest_ask, 'qty': filled_qty, 'uid': pos_uid})
                    # Initialize highest covering bid for this new position
                    if not hasattr(globals(), '_max_covering_bids'):
                        globals()['_max_covering_bids'] = {}
                    max_covering_bids = globals()['_max_covering_bids']
                    max_covering_bids[pos_uid] = lowest_ask  # Initialize to entry price
                    usd_value = filled_qty * lowest_ask
                    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    entry_log = f"[{now_str}] ENTERED position: {filled_qty:.4f} BNB @ {lowest_ask:.2f} (USD: ${usd_value:.2f})"
                    print(entry_log)
                    logging.info(entry_log)
                    # send_ntfy(entry_log)  # ntfy notification not needed for entry
                except Exception as e:
                    print(f"Buy error: {e}")
                    logging.error(f"Buy error: {e}")

            # --- Sell logic: sell when SMA7 crosses from above to below SMA25 ---
            new_positions = []
            for pos in positions:
                pos_uid = pos.get('uid')
                # Only check for crossover if enough data
                if len(closes) >= SMA25_PERIOD + 2:
                    closes_list = list(closes)
                    sma7_now = sum(closes_list[-SMA7_PERIOD:]) / SMA7_PERIOD
                    sma25_now = sum(closes_list[-SMA25_PERIOD:]) / SMA25_PERIOD
                    sma7_prev = sum(closes_list[-SMA7_PERIOD-1:-1]) / SMA7_PERIOD
                    sma25_prev = sum(closes_list[-SMA25_PERIOD-1:-1]) / SMA25_PERIOD
                    # Crossover: was above, now below
                    if sma7_prev > sma25_prev and sma7_now < sma25_now:
                        try:
                            qty = pos['qty']
                            entry = pos['entry']
                            order = exchange.create_market_sell_order(SYMBOL, float(qty))
                            pnl_usd = (lowest_ask - entry) * qty
                            pnl_pct = ((lowest_ask - entry) / entry) * Decimal('100')
                            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            exit_log = f"[{now_str}] SOLD: {qty:.4f} BNB @ {lowest_ask:.2f} (entry: {entry:.2f}) | P/L: ${pnl_usd:.2f} ({pnl_pct:.2f}%)"
                            ntfy_msg = f"SOLD: {qty:.4f} BNB @ {lowest_ask:.2f} (entry: {entry:.2f}) | P/L: ${pnl_usd:.2f} ({pnl_pct:.2f}%)"
                            print(exit_log)
                            logging.info(exit_log)
                            send_ntfy(ntfy_msg)
                            # Remove max and pending sell tracking after sell
                            if not hasattr(globals(), '_max_covering_bids'):
                                globals()['_max_covering_bids'] = {}
                            if not hasattr(globals(), '_pending_sell_times'):
                                globals()['_pending_sell_times'] = {}
                            max_covering_bids = globals()['_max_covering_bids']
                            pending_sell_times = globals()['_pending_sell_times']
                            if pos_uid in max_covering_bids:
                                del max_covering_bids[pos_uid]
                            if pos_uid in pending_sell_times:
                                del pending_sell_times[pos_uid]
                            continue  # Do not append to new_positions
                        except Exception as e:
                            print(f"Sell error: {e}")
                            logging.error(f"Sell error: {e}")
                            # Remove position from tracking to avoid repeated failed attempts
                            if not hasattr(globals(), '_max_covering_bids'):
                                globals()['_max_covering_bids'] = {}
                            if not hasattr(globals(), '_pending_sell_times'):
                                globals()['_pending_sell_times'] = {}
                            max_covering_bids = globals()['_max_covering_bids']
                            pending_sell_times = globals()['_pending_sell_times']
                            if pos_uid in max_covering_bids:
                                del max_covering_bids[pos_uid]
                            if pos_uid in pending_sell_times:
                                del pending_sell_times[pos_uid]
                            continue
                    else:
                        new_positions.append(pos)
                else:
                    new_positions.append(pos)
            positions = new_positions
            # ...existing code...
            for pos in positions:
                # ...existing code...
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
                # Initialize max_covering_bids to entry if missing
                if pos_uid not in max_covering_bids:
                    max_covering_bids[pos_uid] = entry
                prev_max = max_covering_bids.get(pos_uid, entry)
                now_time = time.time()
                if highest_covering_bid > prev_max:
                    max_covering_bids[pos_uid] = highest_covering_bid
                    # Cancel any pending sell if price recovers
                    if pos_uid in pending_sell_times:
                        del pending_sell_times[pos_uid]
                    # ...existing code...
                    new_positions.append(pos)
                elif highest_covering_bid < prev_max:
                    # Start confirmation period only if not already started
                    if pos_uid not in pending_sell_times:
                        pending_sell_times[pos_uid] = now_time
                    elapsed = now_time - pending_sell_times[pos_uid]
                    # ...existing code...
                    if elapsed >= CONFIRMATION_PERIOD:
                        # Enforce min sell size
                        MIN_BNB_SELL = 0.01
                        if qty < MIN_BNB_SELL:
                            if pos_uid in max_covering_bids:
                                del max_covering_bids[pos_uid]
                            if pos_uid in pending_sell_times:
                                del pending_sell_times[pos_uid]
                            continue
                        try:
                            order = exchange.create_market_sell_order(SYMBOL, float(qty))
                            pnl_usd = (highest_covering_bid - entry) * qty
                            pnl_pct = ((highest_covering_bid - entry) / entry) * Decimal('100')
                            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            exit_log = f"[{now_str}] SOLD: {qty:.4f} BNB @ {highest_covering_bid:.2f} (entry: {entry:.2f}) | P/L: ${pnl_usd:.2f} ({pnl_pct:.2f}%)"
                            ntfy_msg = f"SOLD: {qty:.4f} BNB @ {highest_covering_bid:.2f} (entry: {entry:.2f}) | P/L: ${pnl_usd:.2f} ({pnl_pct:.2f}%)"
                            print(exit_log)
                            logging.info(exit_log)
                            send_ntfy(ntfy_msg)
                            # Remove max and pending sell tracking after sell
                            if pos_uid in max_covering_bids:
                                del max_covering_bids[pos_uid]
                            if pos_uid in pending_sell_times:
                                del pending_sell_times[pos_uid]
                            # Do NOT append to new_positions after sell
                        except Exception as e:
                            print(f"Sell error: {e}")
                            logging.error(f"Sell error: {e}")
                            # Remove position from tracking to avoid repeated failed attempts
                            if pos_uid in max_covering_bids:
                                del max_covering_bids[pos_uid]
                            if pos_uid in pending_sell_times:
                                del pending_sell_times[pos_uid]
                            continue
                    else:
                        new_positions.append(pos)
                else:
                    # No drop, no new high, cancel any pending sell
                    if pos_uid in pending_sell_times:
                        del pending_sell_times[pos_uid]
                    new_positions.append(pos)
            positions = new_positions

            # --- Logger ---
            now = time.time()
            if now - last_log_time >= LOG_INTERVAL:
                now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                log_lines = [f"[{now_str}] USD: ${usd_balance:.2f}"]
                if positions:
                    pos_lines = []
                    for pos in positions:
                        entry = pos['entry']
                        qty = pos['qty']
                        usd_value = qty * entry
                        pos_lines.append(
                            f"Entry: {entry:.2f}, Qty: {qty:.4f}, Value: USD: ${usd_value:.2f}"
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
