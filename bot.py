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
positions = []  # List of {'entry': Decimal, 'qty': Decimal, 'ratchet': Decimal}
last_status_log = 0

try:
    print("\n=== AlgoBot is starting up! ===\n")
    log_and_notify("AlgoBot has started running.")
    last_move = ' '
    last_price_seen = None
    last_logged_price = None
    last_diff_price = None
    while True:
        try:
            # Save previous price before fetching new one
            prev_price = last_price

            try:
                order_book = exchange.fetch_order_book(SYMBOL, limit=10)
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
                ticker = exchange.fetch_ticker(SYMBOL)
                price = Decimal(str(ticker['last']))
            except Exception as e:
                print(f"Ticker fetch timeout or error: {e}")
                time.sleep(2)
                continue
            last_price = price

            try:
                balance = exchange.fetch_balance()
                usd_balance = Decimal(str(balance['free'].get('USD', 0)))
                bnb_balance = Decimal(str(balance['free'].get('BNB', 0)))
            except Exception as e:
                print(f"Balance fetch timeout or error: {e}")
                time.sleep(2)
                continue

            # Log every time the price moves
            now = time.time()
            if last_logged_price is None:
                last_logged_price = price
            if last_diff_price is None:
                last_diff_price = price

            price_moved = False
            prev_logged_price = last_logged_price
            prev_diff_price = last_diff_price
            if price != last_logged_price:
                now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                move_dir = '+' if price > last_logged_price else '-'
                move_msg = f"[{now_str}] Price move: {move_dir} {last_logged_price} -> {price}"
                print(move_msg)
                logging.info(move_msg)
                price_moved = True
                last_logged_price = price
                last_diff_price = price

            # Entry logic: check every loop, but only log diagnostics on price move or actual buy
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
            entry_spread = (lowest_ask - vwap_bid_price) / lowest_ask
            # ...removed [DIAG][ENTRY] diagnostic logging...
            # Allow repeated buys after a positive price change, as long as spread remains < threshold
            if prev_diff_price is not None:
                if price > prev_diff_price:
                    buy_trigger_active = True
                elif price < prev_diff_price:
                    buy_trigger_active = False
                # If buy trigger is active and spread is favorable, keep buying
                if 'buy_trigger_active' in locals() and buy_trigger_active and entry_spread < SPREAD_THRESHOLD:
                    max_qty = (usd_balance * MAX_USD_RATIO) / lowest_ask
                    buy_qty = min(ask_qty, max_qty)
                    min_notional = Decimal('10')  # Binance.us minimum notional for BNB/USD is typically $10
                    notional_value = buy_qty * lowest_ask
                    if buy_qty > 0 and notional_value >= min_notional:
                        minus_02 = lowest_ask * Decimal('0.998')
                        plus_01 = lowest_ask * Decimal('1.001')
                        msg = (
                            f"ENTRY: Market buy {buy_qty} BNB at {lowest_ask} USD (spread: {entry_spread*100:.4f}%)\n"
                            f"  -0.2% stop: {minus_02:.4f}  +0.1% ratchet: {plus_01:.4f}"
                        )
                        print(msg)
                        logging.info(msg)
                        order = exchange.create_market_buy_order(SYMBOL, float(buy_qty))
                        actual_qty = Decimal(str(order.get('filled', order.get('amount', buy_qty))))
                        positions.append({
                            'entry': lowest_ask,
                            'qty': actual_qty,
                            'ratchet': Decimal('0.001'),  # +0.1% initial ratchet
                        })
                        # Update stats
                        stats['entries'] += 1
                        stats['last_entry'] = f"{buy_qty} BNB at {lowest_ask} USD ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"
                # ...do not log skipped buys under min notional...
            # Status log every 10 seconds (VWAP-based spread)
            if now - last_status_log > 10:
                # Calculate VWAP bid price for ask_qty (same as entry logic)
                ask_qty_status = Decimal(str(asks[0][1]))
                cumulative_bid_qty_status = Decimal('0')
                weighted_bid_sum_status = Decimal('0')
                for bid_price, bid_qty in bids:
                    bid_price_dec = Decimal(str(bid_price))
                    bid_qty_dec = Decimal(str(bid_qty))
                    if cumulative_bid_qty_status + bid_qty_dec >= ask_qty_status:
                        needed_qty = ask_qty_status - cumulative_bid_qty_status
                        weighted_bid_sum_status += bid_price_dec * needed_qty
                        cumulative_bid_qty_status += needed_qty
                        break
                    else:
                        weighted_bid_sum_status += bid_price_dec * bid_qty_dec
                        cumulative_bid_qty_status += bid_qty_dec
                if cumulative_bid_qty_status == 0:
                    vwap_bid_price_status = Decimal(str(bids[0][0]))
                else:
                    vwap_bid_price_status = weighted_bid_sum_status / ask_qty_status
                vwap_spread = (lowest_ask - vwap_bid_price_status) / lowest_ask
                now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                # Build custom positions status string
                if positions:
                    pos_statuses = []
                    for pos in positions:
                        entry_price = float(pos['entry'])
                        needed_qty = pos['qty']
                        cumulative_qty = Decimal('0')
                        weighted_bid_sum = Decimal('0')
                        # Use same cover_bid calculation as sell logic
                        for bid_price, bid_qty in bids:
                            bid_price_dec = Decimal(str(bid_price))
                            bid_qty_dec = Decimal(str(bid_qty))
                            if cumulative_qty + bid_qty_dec >= needed_qty:
                                fill_qty = needed_qty - cumulative_qty
                                weighted_bid_sum += bid_price_dec * fill_qty
                                cumulative_qty += fill_qty
                                break
                            else:
                                weighted_bid_sum += bid_price_dec * bid_qty_dec
                                cumulative_qty += bid_qty_dec
                        if cumulative_qty == 0:
                            cover_bid = float(bids[0][0])
                        else:
                            cover_bid = float(weighted_bid_sum / needed_qty)
                        lower_thresh = float(entry_price * (1 + float(pos['ratchet']) - 0.002))
                        upper_thresh = float(entry_price * (1 + float(pos['ratchet'])))
                        usd_value = float(pos['qty']) * entry_price
                        pos_statuses.append(f"entry: {entry_price:.2f} (USD value: {usd_value:.2f}), lower threshold: {lower_thresh:.2f}, upper threshold: {upper_thresh:.2f}, current: {cover_bid:.2f}")
                    pos_status = ' | '.join(pos_statuses)
                else:
                    pos_status = "None"
                status_msg = f"[{now_str}] Status: USD={usd_balance:.2f}, spread={vwap_spread*100:.4f}%, position={pos_status}"
                print(status_msg)
                logging.info(status_msg)
                last_status_log = now

            # Updated entry logic: use VWAP of cumulative bids to cover lowest ask size
            # Always consider new entries
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
                # ...removed [DIAG][ENTRY] diagnostic logging...
                if entry_spread < SPREAD_THRESHOLD and prev_price is not None and price > prev_price:
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
                        positions.append({
                            'entry': lowest_ask,
                            'qty': buy_qty,
                            'ratchet': Decimal('0.001'),  # +0.1% initial ratchet
                        })
                        # Update stats
                        stats['entries'] += 1
                        stats['last_entry'] = f"{buy_qty} BNB at {lowest_ask} USD ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"
                else:
                    debug_entry = True
                # ...removed debug entry logger...

            # Sell and ratcheting logic
            # Manage all open positions
            new_positions = []
            for pos in positions:
                needed_qty = pos['qty']
                cumulative_qty = Decimal('0')
                weighted_bid_sum = Decimal('0')
                # Recalculate cover_bid for each position independently
                for bid_price, bid_qty in bids:
                    bid_price_dec = Decimal(str(bid_price))
                    bid_qty_dec = Decimal(str(bid_qty))
                    if cumulative_qty + bid_qty_dec >= needed_qty:
                        fill_qty = needed_qty - cumulative_qty
                        weighted_bid_sum += bid_price_dec * fill_qty
                        cumulative_qty += fill_qty
                        break
                    else:
                        weighted_bid_sum += bid_price_dec * bid_qty_dec
                        cumulative_qty += bid_qty_dec
                if cumulative_qty == 0:
                    cover_bid = float(bids[0][0])
                else:
                    cover_bid = float(weighted_bid_sum / needed_qty)

                entry_price = Decimal(str(pos['entry']))
                ratchet_level = entry_price * (Decimal('1.0') + Decimal(str(pos['ratchet'])))
                lower_thresh = entry_price * (Decimal('1.0') + Decimal(str(pos['ratchet'])) - Decimal('0.002'))

                # Ratchet up if cover_bid > ratchet_level
                if Decimal(str(cover_bid)) > ratchet_level:
                    pos['ratchet'] += Decimal('0.001')
                    lower_thresh = entry_price * (Decimal('1.0') + Decimal(str(pos['ratchet'])) - Decimal('0.002'))
                    msg = f"RATCHET: Stop moved to {lower_thresh:.4f} (+{float(pos['ratchet'])*100:.2f}% of entry)"
                    logging.info(msg)

                # Diagnostic logging for sell check
                # If cover_bid drops to or below lower_thresh, sell (rounded to 2 decimals)
                if round(Decimal(str(cover_bid)), 2) <= round(lower_thresh, 2):
                    exit_price = Decimal(str(cover_bid))
                    qty = pos['qty']
                    if qty > bnb_balance:
                        msg = f"SKIP EXIT: Not enough BNB to sell {qty} (balance: {bnb_balance})"
                        print(msg)
                        logging.warning(msg)
                        new_positions.append(pos)
                        continue
                    pnl_usd = (exit_price - entry_price) * qty
                    pnl_pct = ((exit_price - entry_price) / entry_price) * Decimal('100')
                    msg = f"EXIT: Market sell {qty} BNB at {float(exit_price)} USD (entry: {float(entry_price)}, ratchet: {float(pos['ratchet'])*100:.2f}%)"
                    print(msg)
                    logging.info(msg)
                    ntfy_msg = f"Position exited\nP/L: {pnl_usd:.2f} USD ({pnl_pct:.2f}%)"
                    try:
                        requests.post(NTFY_URL, data=ntfy_msg.encode('utf-8'), timeout=1)
                    except Exception as e:
                        logging.warning(f"ntfy notification failed: {e}")
                    try:
                        order = exchange.create_market_sell_order(SYMBOL, float(qty))
                        # Refresh BNB balance after successful sell
                        balance = exchange.fetch_balance()
                        bnb_balance = Decimal(str(balance['free'].get('BNB', 0)))
                    except Exception as e:
                        print(f"Error: {e}")
                        logging.error(f"Sell order failed: {e}")
                    # Update stats
                    stats['exits'] += 1
                    stats['last_exit'] = f"{qty} BNB at {float(exit_price)} USD ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"
                    # Always remove this position after a sell attempt
                else:
                    new_positions.append(pos)
            positions = new_positions

            last_price = price
        except Exception as e:
            logging.error(f'Error: {e}')
            print('Error:', e)
        time.sleep(2)
except KeyboardInterrupt:
    print("\n=== AlgoBot is shutting down. ===\n")
    try:
        log_and_notify("AlgoBot has stopped running.")
    except Exception as e:
        print(f"Shutdown notification failed: {e}")
