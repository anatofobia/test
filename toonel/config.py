import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
ENV_PATH = BASE_DIR / ".env"
STATE_PATH = BASE_DIR / "state.json"
LOG_PATH = BASE_DIR / "tonnel.log"

load_dotenv(ENV_PATH)


def _int(key: str, default: int = 0) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _float(key: str, default: float = 0.0) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


# ── MTProto ──────────────────────────────────────────────
API_ID: int = _int("API_ID")
API_HASH: str = os.getenv("API_HASH", "")
PHONE_NUMBER: str = os.getenv("PHONE_NUMBER", "")
SESSION_STRING: str = os.getenv("SESSION_STRING", "")

# ── Tonnel ───────────────────────────────────────────────
TONNEL_AUTH_DATA: str = os.getenv("TONNEL_AUTH_DATA", "")
WITHDRAW_WALLET: str = os.getenv("WITHDRAW_WALLET", "")
PRICE_MULTIPLIER: float = _float("PRICE_MULTIPLIER", 0.97)
MIN_WITHDRAW_TON: float = _float("MIN_WITHDRAW_TON", 2.0)
CHECK_INTERVAL: int = _int("CHECK_INTERVAL", 30)
AUTO_TRANSFER: bool = _bool("AUTO_TRANSFER", False)

# ── Notifications ────────────────────────────────────────
TG_BOT_TOKEN: str = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID: str = os.getenv("TG_CHAT_ID", "")

# ── Constants ────────────────────────────────────────────
TONNEL_BOT_USERNAME: str = "@Tonnel_Network_bot"


def validate() -> list[str]:
    """Return list of missing required fields."""
    errors = []
    if not API_ID:
        errors.append("API_ID не задан")
    if not API_HASH:
        errors.append("API_HASH не задан")
    if not SESSION_STRING and not PHONE_NUMBER:
        errors.append("Нужен SESSION_STRING или PHONE_NUMBER")
    # TONNEL_AUTH_DATA is optional: fetched automatically from Mini App at startup.
    # Set it in .env only as a fallback if Mini App fetch fails.
    return errors
