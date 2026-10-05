"""
config.py — all configuration and secrets for the digital store bot.
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
# OxaPay Merchant Key used for generating payment invoices and white-label addresses
OXAPAY_MERCHANT_KEY = os.getenv("OXAPAY_MERCHANT_KEY", "YOUR_OXAPAY_MERCHANT_KEY")
OXAPAY_API_KEY = os.getenv("OXAPAY_API_KEY", "YOUR_OXAPAY_API_KEY")

# Webhook Callback URL for OxaPay Payment Notifications
# (Set this to your public domain/Heroku/Railway URL e.g. https://your-domain.com/oxapay/callback)
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))

# OxaPay Endpoints
OXAPAY_CREATE_INVOICE_URL = "https://api.oxapay.com/merchants/request"
OXAPAY_WHITE_LABEL_URL = "https://api.oxapay.com/merchants/request/whitelabel"

# Top 10 Supported Automatic OxaPay Cryptocurrencies
TOP_10_CURRENCIES = [
    "USDT", "BTC", "LTC", "SOL", "TRX", "TON", "ETH", "BNB", "DOGE", "XRP"
]

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
    "4. Payments are verified automatically via OxaPay upon blockchain confirmation.\n\n"
    "Contact support if you have any questions."
)
