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

REPORT_INTERVAL = 3600
GEMINI_MODEL_ID = "gemini-3.1-pro-preview"