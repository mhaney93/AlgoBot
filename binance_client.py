import os
from binance.client import Client

BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', 'YOUR_API_KEY')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', 'YOUR_API_SECRET')

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# Test connection
try:
    status = client.ping()
    print('Binance.us API connection successful:', status)
except Exception as e:
    print('Error connecting to Binance.us API:', e)
