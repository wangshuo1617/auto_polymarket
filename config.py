import os
from dotenv import load_dotenv
load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
POLYMARKET_KEY = os.getenv("POLYMARKET_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
TO_EMAIL = os.getenv("TO_EMAIL")
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = os.getenv("SMTP_PORT")
FROM_EMAIL = os.getenv("FROM_EMAIL")
FROM_EMAIL_PASSWORD = os.getenv("FROM_EMAIL_PASSWORD")

# Polymarket Builder API credentials for gasless relayer
BUILDER_API_KEY = os.getenv("BUILDER_API_KEY")
BUILDER_SECRET = os.getenv("BUILDER_SECRET")
BUILDER_PASSPHRASE = os.getenv("BUILDER_PASSPHRASE")
BUILDER_ADDRESS = os.getenv("BUILDER_ADDRESS")

# Shared SQLite database path used by 5m_trade and btc_1s_market_monitor.
SQLITE_DB_PATH = os.getenv("SQLITE_DB_PATH", "logs/trade.sqlite3")

REPORT_INTERVAL = 3600
GEMINI_MODEL_ID = os.getenv("GEMINI_MODEL_ID")