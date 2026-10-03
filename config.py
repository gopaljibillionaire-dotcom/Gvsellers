"""
config.py — all configuration and secrets for the digital store bot.

Every value can be overridden with an environment variable (handy on Heroku /
Railway / Docker), otherwise edit the defaults below. Secrets live ONLY here
(or in env vars) and are never sent to Telegram.
"""
import os


def _int_list(raw: str, default: list) -> list:
    items = [x.strip() for x in raw.split(",") if x.strip()]
    return [int(x) for x in items] if items else default


# ───────────────────────────── Telegram ─────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "123456:REPLACE_ME")
ADMIN_IDS = _int_list(os.getenv("ADMIN_IDS", ""), [123456789])  # comma-separated Telegram user IDs

# Support. NOTE: this number is the same value as the support custom-emoji ID, and it is
# too large to be a real Telegram user ID, so the "Support" button opens SUPPORT_USERNAME.
# Messages sent through the bot are relayed to SUPPORT_ID if reachable, else to ADMIN_IDS.
SUPPORT_ID = int(os.getenv("SUPPORT_ID", "5395804191769763641"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "your_support_username")  # without @

# Telegram custom (premium) emoji IDs
WALLET_EMOJI_ID = "5256186332669035163"
SUPPORT_EMOJI_ID = "5395804191769763641"

# ───────────────────────────── MongoDB ──────────────────────────────
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DATABASE_NAME = os.getenv("DATABASE_NAME", "digital_store")

# ──────────────────────────── Binance Pay ───────────────────────────
# Merchant API credentials from the Binance Pay merchant dashboard.
# Admins may override them at runtime from the Payment Settings screen
# (stored encrypted in MongoDB, always shown masked).
BINANCE_PAY_API_KEY = os.getenv("BINANCE_PAY_API_KEY", "REPLACE_ME")
BINANCE_PAY_API_SECRET = os.getenv("BINANCE_PAY_API_SECRET", "REPLACE_ME")
BINANCE_MERCHANT_ID = os.getenv("BINANCE_MERCHANT_ID", "REPLACE_ME")
BINANCE_API_BASE = os.getenv("BINANCE_API_BASE", "https://bpay.binanceapi.com")
# Only set True if you are an ISV creating orders for a sub-merchant.
BINANCE_SEND_MERCHANT_ID = os.getenv("BINANCE_SEND_MERCHANT_ID", "0") == "1"

# Stablecoins only (1 USD = 1 coin), so wallet credit is exact.
SUPPORTED_CURRENCIES = ["USDT", "USDC", "FDUSD"]
DEFAULT_ENABLED_CURRENCIES = ["USDT"]
MIN_DEPOSIT = "1.00"     # default minimum deposit (USD), editable in admin panel
MAX_DEPOSIT = "500.00"   # default maximum deposit (USD), editable in admin panel
PAYMENT_EXPIRY_MINUTES = 30
PAYMENT_POLL_SECONDS = 25   # background verification interval

# Optional Binance Pay webhook receiver (a trigger only: the body is never trusted,
# the order is always re-verified through the signed merchant API).
# Set WEBHOOK_PORT (e.g. $PORT on a web dyno) and WEBHOOK_PUBLIC_URL to enable.
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "0"))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/binance/webhook")
WEBHOOK_PUBLIC_URL = os.getenv("WEBHOOK_PUBLIC_URL", "")  # e.g. https://myapp.example.com/binance/webhook

# ───────────────────────────── Security ─────────────────────────────
# Fernet key used to encrypt inventory codes and stored credentials at rest.
# Generate one:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# If empty, a key is derived from BOT_TOKEN (then rotating the token makes old data unreadable!).
SETTINGS_ENCRYPTION_KEY = os.getenv("SETTINGS_ENCRYPTION_KEY", "")

# ─────────────────────────────── Store ──────────────────────────────
STORE_NAME = os.getenv("STORE_NAME", "Digital Store")
CURRENCY_SYMBOL = "$"
PAGE_SIZE = 6

TERMS_TEXT = (
    "By using this store you agree to the following:\n\n"
    "1. All products sold here are legally owned or properly authorized digital products "
    "(license keys, activation codes, vouchers, downloads, subscriptions).\n"
    "2. Fraudulent payments, chargeback abuse, unauthorized access and any attempt to "
    "purchase or sell prohibited goods are strictly forbidden and will result in a permanent ban.\n"
    "3. Delivered digital products are non-refundable once revealed, unless the code is proven invalid.\n"
    "4. Wallet funds are credited only after the payment is verified by the payment provider.\n"
    "5. We may refuse service or restrict accounts that violate these terms.\n\n"
    "Contact support if you have any questions."
)
