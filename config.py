import os
from dotenv import load_dotenv
load_dotenv()

os.makedirs("logs", exist_ok=True)
os.makedirs("output", exist_ok=True)

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
POLYMARKET_KEY = os.getenv("POLYMARKET_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")


def _first_non_empty(*values: str | None) -> str | None:
    for value in values:
        if value is None:
            continue
        cleaned = str(value).strip()
        if cleaned:
            return cleaned
    return None


# Multi-account profile configs (prefer unified .env naming)
# 5m account envs: FIVE_M_ACCOUNT_KEY / FIVE_M_ACCOUNT_WALLET_ADDRESS
# monthly/analyze account envs: MONTHLY_ACCOUNT_KEY / MONTHLY_ACCOUNT_WALLET_ADDRESS
# Backward compatible aliases are still supported.
PM_TRADE_KEY = _first_non_empty(
    os.getenv("FIVE_M_ACCOUNT_KEY"),
    os.getenv("PM_TRADE_KEY"),
    POLYMARKET_KEY,
)
PM_TRADE_WALLET_ADDRESS = _first_non_empty(
    os.getenv("FIVE_M_ACCOUNT_WALLET_ADDRESS"),
    os.getenv("PM_TRADE_WALLET_ADDRESS"),
    WALLET_ADDRESS,
)
PM_ANALYZE_KEY = _first_non_empty(
    os.getenv("MONTHLY_ACCOUNT_KEY"),
    os.getenv("PM_ANALYZE_KEY"),
    POLYMARKET_KEY,
)
PM_ANALYZE_WALLET_ADDRESS = _first_non_empty(
    os.getenv("MONTHLY_ACCOUNT_WALLET_ADDRESS"),
    os.getenv("PM_ANALYZE_WALLET_ADDRESS"),
    WALLET_ADDRESS,
)
POLYMARKET_PROFILE = _first_non_empty(os.getenv("POLYMARKET_PROFILE"), "trade") or "trade"
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


def _normalize_builder_code(raw: str | None) -> str | None:
    """规范化 Polymarket builder code 为 bytes32 hex。"""
    if not raw:
        return None
    code = raw.strip()
    if code.startswith("0x") and len(code) == 66:
        try:
            int(code, 16)
            return code.lower()
        except ValueError:
            pass
    hex_body = code[2:] if code.startswith("0x") else code
    try:
        int(hex_body, 16)
        if len(hex_body) <= 64:
            return "0x" + hex_body.rjust(64, "0").lower()
    except ValueError:
        pass
    try:
        from eth_utils import keccak
        return "0x" + keccak(text=code).hex()
    except Exception:
        return None


POLYMARKET_BUILDER_CODE = _normalize_builder_code(os.getenv("POLYMARKET_BUILDER_CODE"))
# bytes32 全零, 表示无 builder code (CLOB 默认值)
_BUILDER_CODE_ZERO = "0x" + "0" * 64

# PostgreSQL 连接字符串 (TimescaleDB)。
# 格式: postgresql://user:password@host:port/dbname
PG_DSN = os.getenv("PG_DSN", "")

REPORT_INTERVAL = 3600
GEMINI_MODEL_ID = os.getenv("GEMINI_MODEL_ID")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


# 邮件开关：默认关闭小时报告类邮件，只保留分析/预警邮件
ENABLE_BTC_HOURLY_EMAIL = _env_bool("ENABLE_BTC_HOURLY_EMAIL", False)
ENABLE_5M_TRADE_SUMMARY_EMAIL = _env_bool("ENABLE_5M_TRADE_SUMMARY_EMAIL", False)