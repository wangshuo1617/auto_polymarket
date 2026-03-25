import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

# 项目根目录下的 tick / 共享 SQLite（与 new_trade/config.py 一致）
_PROJECT_ROOT = Path(__file__).resolve().parent

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
POLYMARKET_KEY = os.getenv("POLYMARKET_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
TO_EMAIL = os.getenv("TO_EMAIL")
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = os.getenv("SMTP_PORT")
FROM_EMAIL = os.getenv("FROM_EMAIL")
FROM_EMAIL_PASSWORD = os.getenv("FROM_EMAIL_PASSWORD")

# Shared SQLite database path used by 5m_trade and btc_1s_market_monitor.
SQLITE_DB_PATH = os.getenv(
    "SQLITE_DB_PATH", str(_PROJECT_ROOT / "tmp" / "trade.sqlite3")
)

REPORT_INTERVAL = 3600
GEMINI_MODEL_ID = os.getenv("GEMINI_MODEL_ID")