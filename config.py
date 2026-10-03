
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

# ───────────────────────────── MongoDB ──────────────────────────────
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DATABASE_NAME = os.getenv("DATABASE_NAME", "digital_store")

# ────────────────────── Manual Wallet Payments ──────────────────────
# Static addresses for manual deposit transfers
WALLETS = {
    "USDT_TRC20": os.getenv("USDT_TRC20_WALLET", "TEmFazxBnjuxyQF3ohHReJvSghmG1DW2sX"),
    "USDT_BEP20": os.getenv("USDT_BEP20_WALLET", "0x20c065b69618a09fb8f9ab88e07e09910d9abada"),
    "USDT_ERC20": os.getenv("USDT_ERC20_WALLET", "0x20c065b69618a09fb8f9ab88e07e09910d9abada"),
    "BTC": os.getenv("BTC_WALLET", "1KiZw2SmvhWZCY8Rj3bZMifMkkF82BK6UX"),
    "ETH": os.getenv("ETH_WALLET", "0x20c065b69618a09fb8f9ab88e07e09910d9abada"),
    "SOL": os.getenv("SOL_WALLET", "9bQPXaQqdZe4XzLeZrPbfJMu7e5sbgDWhNKgifDu4M7Q"),
}

# Live rate conversion API settings (e.g. CoinGecko API)
# API IDs used to convert USD deposit amounts to real-time crypto equivalents
PRICE_API_URL = "https://api.coingecko.com/api/v3/simple/price"
CURRENCY_PRICE_IDS = {
    "USDT": "tether",
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
}

SUPPORTED_CURRENCIES = ["USDT_TRC20", "USDT_BEP20", "USDT_ERC20", "BTC", "ETH", "SOL"]
DEFAULT_ENABLED_CURRENCIES = ["USDT_TRC20", "BTC", "ETH", "SOL"]
MIN_DEPOSIT = "1.00"     # default minimum deposit (USD), editable in admin panel
MAX_DEPOSIT = "500.00"   # default maximum deposit (USD), editable in admin panel

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
    "4. Wallet funds are credited only after the payment is verified by the payment provider or store admins.\n"
    "5. We may refuse service or restrict accounts that violate these terms.\n\n"
    "Contact support if you have any questions."
)
