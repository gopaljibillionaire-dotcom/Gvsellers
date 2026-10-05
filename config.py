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

# Support
SUPPORT_ID = int(os.getenv("SUPPORT_ID", "5395804191769763641"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "your_support_username")  # without @

# Telegram custom (premium) emoji IDs
STORE_EMOJI_ID = "5451937962629544243"        # 🛍 Store / General
BOX_EMOJI_ID = "5355193051193059834"          # 📦 Inventory / Products / Delivery
TERMS_EMOJI_ID = "5258500400918587241"        # 📜 Terms & Conditions
TRANSACTION_EMOJI_ID = "5258500400918587241"  # 💳 Transactions / History
USDT_EMOJI_ID = "6035288280562404083"         # 💵 USDT
BTC_EMOJI_ID = "5465465383035083768"          # ₿ Bitcoin
ETH_EMOJI_ID = "5830292326202741807"          # ⟠ Ethereum
LTC_EMOJI_ID = "5116097208281727613"          # 🪙 Litecoin
SOL_EMOJI_ID = "5449658099499547173"          # ☀️ Solana
WALLET_EMOJI_ID = "5256186332669035163"       # 👛 Wallet
SUPPORT_EMOJI_ID = "5395804191769763641"      # 🎧 Support
GIFTS_EMOJI_ID = "5368324170671202286"        # 🎁 Buy Gifts / Special

# ───────────────────────────── MongoDB ──────────────────────────────
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DATABASE_NAME = os.getenv("DATABASE_NAME", "digital_store")

# ──────────────────────────── AI / Gemini ───────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# ──────────────────────── OxaPay Integration ────────────────────────
# Merchant key used for generating invoices/payments
OXAPAY_MERCHANT_KEY = os.getenv("OXAPAY_MERCHANT_KEY", "")

# API key used for general OxaPay API operations (payouts, info, etc.)
OXAPAY_API_KEY = os.getenv("OXAPAY_API_KEY", "")

# OxaPay Endpoints
OXAPAY_CREATE_INVOICE_URL = "https://api.oxapay.com/merchants/request"
OXAPAY_WHITE_LABEL_URL = "https://api.oxapay.com/merchants/request/whitelabel"

MIN_DEPOSIT = "1.00"     # default minimum deposit (USD)
MAX_DEPOSIT = "500.00"   # default maximum deposit (USD)

# ───────────────────────────── Security ─────────────────────────────
SETTINGS_ENCRYPTION_KEY = os.getenv("SETTINGS_ENCRYPTION_KEY", "")

# ─────────────────────────────── Store ──────────────────────────────
STORE_NAME = os.getenv("STORE_NAME", "Digital Store")
CURRENCY_SYMBOL = "$"
PAGE_SIZE = 6

TERMS_TEXT = (
    "By using this store you agree to the following:\n\n"
    "1. All products sold here are legally owned or properly authorized digital products.\n"
    "2. Fraudulent payments, chargeback abuse, and unauthorized access will result in a permanent ban.\n"
    "3. Delivered digital products are non-refundable once revealed, unless proven invalid.\n"
    "4. Wallet funds are credited only after payment verification.\n\n"
    "Contact support if you have any questions."
)
