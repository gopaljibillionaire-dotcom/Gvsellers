"""
Digital product store bot — Aiogram 3.x + MongoDB (Motor) + Binance Pay.

Sells only legitimately owned digital products (license keys, activation codes,
vouchers, downloads). Never collects passwords, OTP/2FA codes, private keys, etc.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.formatting import Bold, CustomEmoji, Text
from aiohttp import web
from cryptography.fernet import Fernet, InvalidToken
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

import config

log = logging.getLogger("store")
ADMIN_SET = set(config.ADMIN_IDS)
PAGE = config.PAGE_SIZE
MAX_PRICE_CENTS = 10_000_000  # $100,000

mongo: Any = None
db: Any = None
bot_ref: Optional[Bot] = None
_http: Optional[aiohttp.ClientSession] = None
_tasks: set = set()

# ════════════════════════════ helpers ════════════════════════════


def now() -> datetime:
    return datetime.now(timezone.utc)


def esc(v: Any) -> str:
    return html.escape(str(v), quote=False)


def new_id(prefix: str, nbytes: int) -> str:
    return prefix + secrets.token_hex(nbytes).upper()


def money(cents: int) -> str:
    return f"{config.CURRENCY_SYMBOL}{cents / 100:,.2f}"


def fmt_dt(d: Optional[datetime]) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if d else "—"


def parse_money(text: str) -> Optional[int]:
    """Parse a user-supplied amount into integer cents (max 2 decimals)."""
    try:
        d = Decimal(text.strip().replace("$", "").replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None
    if not d.is_finite() or d <= 0 or d.as_tuple().exponent < -2:
        return None
    cents = int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return cents if 0 < cents <= MAX_PRICE_CENTS else None


def to_int(s: str, default: int = 0) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


def mask(s: str) -> str:
    return (s[:4] + "…" + s[-2:]) if len(s) > 8 else "••••"


def _make_fernet() -> Fernet:
    key = config.SETTINGS_ENCRYPTION_KEY
    if not key:
        key = base64.urlsafe_b64encode(hashlib.sha256(("store:" + config.BOT_TOKEN).encode()).digest())
    return Fernet(key)


_fernet = _make_fernet()


def enc(s: str) -> str:
    return _fernet.encrypt(s.encode()).decode()


def dec(s: str) -> str:
    try:
        return _fernet.decrypt(s.encode()).decode()
    except (InvalidToken, ValueError):
        return ""


# Authentication secrets must never be stored in inventory fields.
SECRET_PATTERN = re.compile(
    r"\b(password|passwd|pwd|otp|2fa|two[- ]?factor|recovery|private[ _-]?key|seed[ _-]?phrase|"
    r"session[ _-]?string|mnemonic)\b",
    re.I,
)

# ════════════════════════════ keyboards ════════════════════════════


def btn(text: str, cb: Optional[str] = None, style: Optional[str] = None, url: Optional[str] = None):
    kw: dict = {"text": text}
    if url:
        kw["url"] = url
    else:
        kw["callback_data"] = cb
    if style:
        kw["style"] = style  # Bot API 9.4 button style: primary / success / danger
    return InlineKeyboardButton(**kw)


def kb(rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back(cb: str = "home", text: str = "⬅️ Back"):
    return btn(text, cb, "primary")


def cancel_btn(cb: str = "cancel"):
    return btn("❌ Cancel", cb, "danger")


def pager(prefix: str, page: int, pages: int) -> list:
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn("◀️", f"{prefix}:{page - 1}", "primary"))
    row.append(btn(f"{page + 1}/{pages}", "noop", "primary"))
    if page < pages - 1:
        row.append(btn("▶️", f"{prefix}:{page + 1}", "primary"))
    return [row]


def main_menu(admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [btn("🛍 View Products", "pl:0", "primary")],
        [btn("💰 Wallet", "w", "primary"), btn("📦 My Orders", "ol:0", "primary")],
        [btn("💬 Support", "sup", "primary"), btn("📜 Terms", "terms", "primary")],
    ]
    if admin:
        rows.append([btn("⚙️ Admin Panel", "adm:home", "primary")])
    return kb(rows)


def welcome_text() -> str:
    return (
        f"🛍 <b>{esc(config.STORE_NAME)}</b>\n\n"
        "Legitimate digital products — license keys, activation codes and vouchers — "
        "paid from your wallet and delivered instantly."
    )


async def show(ev, text: Optional[str] = None, markup=None, content: Optional[Text] = None, plain: bool = False):
    """Edit the callback message (or answer a message) with consistent error handling."""
    kwargs: dict = content.as_kwargs() if content is not None else {"text": text}
    if plain and content is None:
        kwargs["parse_mode"] = None
    if isinstance(ev, CallbackQuery):
        try:
            await ev.message.edit_text(**kwargs, reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await ev.message.answer(**kwargs, reply_markup=markup)
        try:
            await ev.answer()
        except TelegramAPIError:
            pass
    else:
        await ev.answer(**kwargs, reply_markup=markup)


async def alert(c: CallbackQuery, text: str):
    try:
        await c.answer(text, show_alert=True)
    except TelegramAPIError:
        pass


async def safe_send(chat_id: int, **kw):
    try:
        await bot_ref.send_message(chat_id, **kw)
    except TelegramAPIError as e:
        log.warning("send_message to %s failed: %s", chat_id, e)


async def notify_admins(text: str):
    for aid in ADMIN_SET:
        await safe_send(aid, text=text)


def spawn(coro):
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def page_query(coll, flt: dict, sort: list, page: int, size: int = PAGE):
    total = await coll.count_documents(flt)
    pages = max(1, -(-total // size))
    page = min(max(page, 0), pages - 1)
    docs = await coll.find(flt).sort(sort).skip(page * size).limit(size).to_list(size)
    return docs, page, pages, total


# ════════════════════════════ database ════════════════════════════


async def init_db():
    global mongo, db
    mongo = AsyncIOMotorClient(config.MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=8000)
    db = mongo[config.DATABASE_NAME]
    await mongo.admin.command("ping")
    A, D = ASCENDING, DESCENDING
    await db.users.create_index("user_id", unique=True)
    await db.products.create_index("product_id", unique=True)
    await db.products.create_index([("deleted", A), ("enabled", A)])
    await db.inventory.create_index("item_id", unique=True)
    await db.inventory.create_index([("status", A), ("product_id", A), ("price_cents", A), ("created_at", A)])
    await db.inventory.create_index(
        [("product_id", A), ("code_hash", A)], unique=True, partialFilterExpression={"code_hash": {"$type": "string"}}
    )
    await db.inventory.create_index("order_id", sparse=True)
    await db.orders.create_index("order_id", unique=True)
    await db.orders.create_index([("user_id", A), ("created_at", D)])
    await db.orders.create_index([("status", A), ("completed_at", D)])
    await db.payments.create_index("payment_id", unique=True)
    await db.payments.create_index("merchant_trade_no", unique=True)
    await db.payments.create_index(
        "provider_order_id", unique=True, partialFilterExpression={"provider_order_id": {"$type": "string"}}
    )
    await db.payments.create_index(
        "provider_transaction_id", unique=True, partialFilterExpression={"provider_transaction_id": {"$type": "string"}}
    )
    await db.payments.create_index([("status", A), ("created_at", A)])
    await db.payments.create_index([("user_id", A), ("created_at", D)])
    await db.wallets.create_index("user_id", unique=True)
    await db.wallet_transactions.create_index([("user_id", A), ("created_at", D)])
    await db.wallet_transactions.create_index(
        [("type", A), ("ref_id", A)], unique=True, partialFilterExpression={"ref_id": {"$type": "string"}}
    )
    await db.settings.create_index("key", unique=True)
    await db.admins.create_index("user_id", unique=True)
    for aid in ADMIN_SET:
        await db.admins.update_one({"user_id": aid}, {"$setOnInsert": {"added_at": now()}}, upsert=True)


async def get_setting(key: str, default: Any = None) -> Any:
    doc = await db.settings.find_one({"key": key})
    return doc["value"] if doc and "value" in doc else default


async def set_setting(key: str, value: Any):
    await db.settings.update_one({"key": key}, {"$set": {"value": value, "updated_at": now()}}, upsert=True)


async def patch_pay_settings(**fields):
    await db.settings.update_one(
        {"key": "payment"},
        {"$set": {**{f"value.{k}": v for k, v in fields.items()}, "updated_at": now()}},
        upsert=True,
    )


async def get_pay_settings() -> dict:
    s = await get_setting("payment", {}) or {}
    return {
        "enabled": s.get("enabled", True),
        "currencies": [c for c in s.get("currencies", config.DEFAULT_ENABLED_CURRENCIES) if c in config.SUPPORTED_CURRENCIES],
        "min_cents": s.get("min_cents", int(Decimal(str(config.MIN_DEPOSIT)) * 100)),
        "max_cents": s.get("max_cents", int(Decimal(str(config.MAX_DEPOSIT)) * 100)),
        "merchant_id": s.get("merchant_id") or config.BINANCE_MERCHANT_ID,
        "api_key": dec(s["api_key_enc"]) if s.get("api_key_enc") else config.BINANCE_PAY_API_KEY,
        "api_secret": dec(s["api_secret_enc"]) if s.get("api_secret_enc") else config.BINANCE_PAY_API_SECRET,
    }


def binance_ready(s: dict) -> bool:
    return bool(
        s["enabled"] and s["currencies"] and s["api_key"] and s["api_secret"]
        and "REPLACE" not in s["api_key"] and "REPLACE" not in s["api_secret"]
    )


async def get_balance(user_id: int) -> int:
    w = await db.wallets.find_one({"user_id": user_id})
    return w["balance_cents"] if w else 0


# ═════════════════════════ Binance Pay client ═════════════════════════


class BinanceError(Exception):
    pass


def http() -> aiohttp.ClientSession:
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    return _http


async def binance_call(path: str, payload: dict) -> dict:
    s = await get_pay_settings()
    if not s["api_key"] or not s["api_secret"] or "REPLACE" in s["api_key"]:
        raise BinanceError("Binance Pay credentials are not configured")
    body = json.dumps(payload, separators=(",", ":"))
    ts = str(int(time.time() * 1000))
    nonce = secrets.token_hex(16)
    sig = hmac.new(s["api_secret"].encode(), f"{ts}\n{nonce}\n{body}\n".encode(), hashlib.sha512).hexdigest().upper()
    headers = {
        "Content-Type": "application/json",
        "BinancePay-Timestamp": ts,
        "BinancePay-Nonce": nonce,
        "BinancePay-Certificate-SN": s["api_key"],
        "BinancePay-Signature": sig,
    }
    try:
        async with http().post(config.BINANCE_API_BASE.rstrip("/") + path, data=body, headers=headers) as r:
            data = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        raise BinanceError(f"network error: {type(e).__name__}") from e
    if not isinstance(data, dict) or data.get("status") != "SUCCESS":
        code = data.get("code") if isinstance(data, dict) else "?"
        msg = data.get("errorMessage") if isinstance(data, dict) else ""
        log.warning("Binance Pay error on %s: code=%s msg=%s", path, code, msg)
        raise BinanceError(f"Binance Pay error {code}: {msg}")
    return data.get("data") or {}


async def binance_create(trade_no: str, amount: str, currency: str) -> dict:
    s = await get_pay_settings()
    payload: dict = {
        "env": {"terminalType": "OTHERS"},
        "merchantTradeNo": trade_no,
        "orderAmount": float(amount),
        "currency": currency,
        "description": "Wallet deposit",
        "goods": {
            "goodsType": "02",
            "goodsCategory": "Z000",
            "referenceGoodsId": "wallet_deposit",
            "goodsName": "Wallet deposit",
        },
        "orderExpireTime": int((time.time() + config.PAYMENT_EXPIRY_MINUTES * 60) * 1000),
    }
    if config.WEBHOOK_PUBLIC_URL:
        payload["webhookUrl"] = config.WEBHOOK_PUBLIC_URL
    if config.BINANCE_SEND_MERCHANT_ID and s["merchant_id"] and "REPLACE" not in s["merchant_id"]:
        payload["merchantId"] = s["merchant_id"]
    return await binance_call("/binancepay/openapi/v2/order", payload)


async def binance_query(trade_no: str) -> dict:
    return await binance_call("/binancepay/openapi/v2/order/query", {"merchantTradeNo": trade_no})


async def binance_close(trade_no: str):
    try:
        await binance_call("/binancepay/openapi/order/close", {"merchantTradeNo": trade_no})
    except BinanceError as e:
        log.info("close order %s: %s", trade_no, e)


# ═══════════════════════════ payments logic ═══════════════════════════

PAY_LABEL = {
    "pending": "⏳ Pending", "crediting": "⏳ Processing", "completed": "✅ Completed",
    "expired": "⌛ Expired", "cancelled": "🚫 Cancelled", "failed": "❌ Failed", "mismatch": "⚠️ Amount mismatch",
}


async def create_deposit(user_id: int, cents: int, currency: str) -> dict:
    s = await get_pay_settings()
    if not binance_ready(s):
        raise BinanceError("Deposits are currently unavailable")
    if currency not in s["currencies"] or not (s["min_cents"] <= cents <= s["max_cents"]):
        raise BinanceError("Invalid amount or currency")
    open_count = await db.payments.count_documents({"user_id": user_id, "status": "pending"})
    if open_count >= 3:
        raise BinanceError("You have too many unpaid deposits. Pay or cancel them first")
    payment_id = new_id("PAY", 8)  # also used as Binance merchantTradeNo (alphanumeric, ≤32)
    amount = f"{Decimal(cents) / 100:.2f}"
    data = await binance_create(payment_id, amount, currency)
    t = now()
    doc = {
        "payment_id": payment_id,
        "merchant_trade_no": payment_id,
        "provider": "binance_pay",
        "user_id": user_id,
        "amount_cents": cents,
        "amount": amount,
        "currency": currency,
        "status": "pending",
        "credited": False,
        "checkout_url": data.get("checkoutUrl"),
        "universal_url": data.get("universalUrl"),
        "created_at": t,
        "expires_at": t + timedelta(minutes=config.PAYMENT_EXPIRY_MINUTES),
        "verified_at": None,
    }
    if data.get("prepayId"):
        doc["provider_order_id"] = str(data["prepayId"])
    await db.payments.insert_one(doc)
    return doc


async def credit_deposit(pay: dict) -> bool:
    """Credit a verified deposit exactly once (unique (type, ref_id) index guards against replays)."""
    try:
        await db.wallet_transactions.insert_one({
            "tx_id": new_id("TX", 6), "user_id": pay["user_id"], "type": "deposit",
            "amount_cents": pay["amount_cents"], "ref_id": pay["payment_id"],
            "note": f"Binance Pay {pay['currency']}", "applied": False, "created_at": now(),
        })
    except DuplicateKeyError:
        return False
    w = await db.wallets.find_one_and_update(
        {"user_id": pay["user_id"]},
        {"$inc": {"balance_cents": pay["amount_cents"]}, "$set": {"updated_at": now()}},
        upsert=True, return_document=ReturnDocument.AFTER,
    )
    await db.wallet_transactions.update_one(
        {"type": "deposit", "ref_id": pay["payment_id"]},
        {"$set": {"applied": True, "balance_after_cents": w["balance_cents"]}},
    )
    await db.payments.update_one(
        {"payment_id": pay["payment_id"]}, {"$set": {"status": "completed", "credited": True, "credited_at": now()}}
    )
    return True


async def process_payment(payment_id: str, notify: bool = False):
    """Verify a payment through the merchant API and credit the wallet at most once."""
    pay = await db.payments.find_one({"payment_id": payment_id})
    if not pay or pay["status"] != "pending":
        return pay, False
    try:
        data = await binance_query(pay["merchant_trade_no"])
    except BinanceError as e:
        log.warning("verify %s failed: %s", payment_id, e)
        return pay, False
    status = str(data.get("status", "")).upper()
    t = now()

    if status == "PAID":
        ok = True
        cur = data.get("currency")
        if cur and str(cur).upper() != pay["currency"]:
            ok = False
        if data.get("orderAmount") is not None:
            try:
                ok = ok and Decimal(str(data["orderAmount"])) == Decimal(pay["amount"])
            except InvalidOperation:
                ok = False
        if not ok:
            await db.payments.update_one(
                {"payment_id": payment_id, "status": "pending"},
                {"$set": {"status": "mismatch", "verified_at": t, "provider_status": status}},
            )
            await notify_admins(f"⚠️ Payment {payment_id} was paid but amount/currency does not match. Manual review needed.")
            return await db.payments.find_one({"payment_id": payment_id}), False
        claim_set = {"status": "crediting", "verified_at": t, "provider_status": status}
        if data.get("transactionId"):
            claim_set["provider_transaction_id"] = str(data["transactionId"])
        try:
            claimed = await db.payments.find_one_and_update(
                {"payment_id": payment_id, "status": "pending"}, {"$set": claim_set}, return_document=ReturnDocument.AFTER
            )
        except DuplicateKeyError:  # same provider transaction already used → replay
            await db.payments.update_one({"payment_id": payment_id, "status": "pending"}, {"$set": {"status": "failed", "verified_at": t}})
            await notify_admins(f"⚠️ Replayed provider transaction detected on payment {payment_id}.")
            return await db.payments.find_one({"payment_id": payment_id}), False
        if not claimed:
            return await db.payments.find_one({"payment_id": payment_id}), False
        credited = await credit_deposit(claimed)
        final = await db.payments.find_one({"payment_id": payment_id})
        if credited and notify:
            bal = await get_balance(claimed["user_id"])
            content = Text(
                CustomEmoji("💰", custom_emoji_id=config.WALLET_EMOJI_ID), " ",
                Bold("Deposit confirmed"), f"\n\n{money(claimed['amount_cents'])} was added to your wallet.\nBalance: ", Bold(money(bal)),
            )
            await safe_send(claimed["user_id"], **content.as_kwargs())
        return final, credited

    new = None
    if status == "EXPIRED":
        new = "expired"
    elif status in ("CANCELED", "CANCELLED"):
        new = "cancelled"
    elif status == "ERROR":
        new = "failed"
    elif t > pay["expires_at"] + timedelta(minutes=2):
        new = "expired"
        await binance_close(pay["merchant_trade_no"])
    if new:
        await db.payments.update_one(
            {"payment_id": payment_id, "status": "pending"},
            {"$set": {"status": new, "verified_at": t, "provider_status": status}},
        )
    return await db.payments.find_one({"payment_id": payment_id}), False


async def reconcile_crediting():
    cutoff = now() - timedelta(minutes=2)
    async for p in db.payments.find({"status": "crediting", "verified_at": {"$lt": cutoff}}):
        tx = await db.wallet_transactions.find_one({"type": "deposit", "ref_id": p["payment_id"]})
        if tx is None:
            await credit_deposit(p)
        elif tx.get("applied"):
            await db.payments.update_one(
                {"payment_id": p["payment_id"]}, {"$set": {"status": "completed", "credited": True, "credited_at": now()}}
            )
        elif not p.get("flagged"):
            await db.payments.update_one({"payment_id": p["payment_id"]}, {"$set": {"flagged": True}})
            await notify_admins(f"⚠️ Payment {p['payment_id']} is stuck while crediting. Please review it in Payments.")


async def release_stale_reservations():
    cutoff = now() - timedelta(minutes=5)
    async for it in db.inventory.find({"status": "reserved", "reserved_at": {"$lt": cutoff}}):
        oid = it.get("order_id")
        paid = await db.wallet_transactions.find_one({"type": "purchase", "ref_id": oid}) if oid else None
        if paid:
            await finalize_sale(oid, it)
        else:
            await release_item(it["item_id"])
            if oid:
                await db.orders.update_one({"order_id": oid, "status": "pending"}, {"$set": {"status": "failed", "payment_status": "failed", "fail_reason": "reservation_expired"}})


async def background_worker():
    while True:
        try:
            async for p in db.payments.find({"status": "pending"}).limit(100):
                await process_payment(p["payment_id"], notify=True)
            await reconcile_crediting()
            await release_stale_reservations()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("background worker error")
        await asyncio.sleep(config.PAYMENT_POLL_SECONDS)


async def binance_webhook(request: web.Request) -> web.Response:
    """The callback body is NEVER trusted: it only triggers a signed re-query of the order."""
    ok = web.json_response({"returnCode": "SUCCESS", "returnMessage": None})
    try:
        body = await request.json()
        data = body.get("data")
        if isinstance(data, str):
            data = json.loads(data)
        trade_no = (data or {}).get("merchantTradeNo") or body.get("bizIdStr")
    except Exception:
        return ok
    if isinstance(trade_no, str) and re.fullmatch(r"[A-Za-z0-9]{6,32}", trade_no):
        spawn(process_payment(trade_no, notify=True))
    return ok


# ═══════════════════════════ inventory & orders ═══════════════════════════

ORDER_PAY = {"paid": "✅ Paid", "unpaid": "⏳ Unpaid", "failed": "❌ Failed"}
ORDER_DELIVERY = {"delivered": "✅ Delivered", "pending": "⏳ Pending", "failed": "❌ Failed"}


async def release_item(item_id: str):
    await db.inventory.update_one(
        {"item_id": item_id, "status": "reserved"},
        {"$set": {"status": "available"}, "$unset": {"order_id": "", "buyer_id": "", "reserved_at": ""}},
    )


async def finalize_sale(order_id: str, item: dict):
    t = now()
    await db.inventory.update_one(
        {"item_id": item["item_id"], "status": "reserved", "order_id": order_id},
        {"$set": {"status": "sold", "sold_at": t}},
    )
    await db.orders.update_one(
        {"order_id": order_id},
        {"$set": {"status": "completed", "payment_status": "paid", "delivery_status": "pending", "completed_at": t, "item_id": item["item_id"]}},
    )


async def purchase(user_id: int, pid: str, cents: int):
    """Atomic flow: reserve item → debit wallet → record → finalize. Returns (order, item, error)."""
    p = await db.products.find_one({"product_id": pid, "enabled": True, "deleted": {"$ne": True}})
    if not p:
        return None, None, "This product is no longer available."
    oid = new_id("ORD", 5)
    t = now()
    await db.orders.insert_one({
        "order_id": oid, "user_id": user_id, "product_id": pid, "product_name": p["name"], "amount_cents": cents,
        "status": "pending", "payment_status": "unpaid", "delivery_status": "pending",
        "payment_method": "wallet", "created_at": t,
    })
    item = await db.inventory.find_one_and_update(
        {"product_id": pid, "status": "available", "price_cents": cents},
        {"$set": {"status": "reserved", "order_id": oid, "buyer_id": user_id, "reserved_at": t}},
        sort=[("created_at", ASCENDING)], return_document=ReturnDocument.AFTER,
    )
    if not item:
        await db.orders.update_one({"order_id": oid}, {"$set": {"status": "failed", "payment_status": "failed", "fail_reason": "out_of_stock"}})
        return None, None, "Sorry, this item just sold out or its price changed."
    w = await db.wallets.find_one_and_update(
        {"user_id": user_id, "balance_cents": {"$gte": cents}},
        {"$inc": {"balance_cents": -cents}, "$set": {"updated_at": now()}}, return_document=ReturnDocument.AFTER,
    )
    if not w:
        await release_item(item["item_id"])
        await db.orders.update_one({"order_id": oid}, {"$set": {"status": "failed", "payment_status": "failed", "fail_reason": "insufficient_funds"}})
        return None, None, "Insufficient wallet balance."
    try:
        await db.wallet_transactions.insert_one({
            "tx_id": new_id("TX", 6), "user_id": user_id, "type": "purchase", "amount_cents": -cents,
            "ref_id": oid, "note": p["name"], "applied": True, "balance_after_cents": w["balance_cents"], "created_at": now(),
        })
        await finalize_sale(oid, item)
    except Exception:
        log.exception("purchase %s failed after debit; refunding", oid)
        await db.wallets.update_one({"user_id": user_id}, {"$inc": {"balance_cents": cents}})
        try:
            await db.wallet_transactions.insert_one({
                "tx_id": new_id("TX", 6), "user_id": user_id, "type": "refund", "amount_cents": cents,
                "ref_id": oid, "note": "Failed purchase refund", "applied": True, "created_at": now(),
            })
        except Exception:
            log.exception("refund tx insert failed for %s", oid)
        await release_item(item["item_id"])
        await db.orders.update_one({"order_id": oid}, {"$set": {"status": "failed", "payment_status": "failed", "fail_reason": "error_refunded"}})
        return None, None, "Something went wrong. You were not charged."
    order = await db.orders.find_one({"order_id": oid})
    return order, item, None


def delivery_block(item: dict) -> str:
    code = dec(item.get("code_enc", ""))
    text = f"🔑 <b>Your product:</b>\n<code>{esc(code)}</code>"
    if item.get("metadata"):
        text += f"\n\nℹ️ <b>Details:</b>\n{esc(item['metadata'])}"
    return text


def order_text(o: dict) -> str:
    return (
        f"📦 <b>Order</b> <code>{o['order_id']}</code>\n\n"
        f"Product: <b>{esc(o['product_name'])}</b>\n"
        f"Amount: <b>{money(o['amount_cents'])}</b>\n"
        f"Payment: {ORDER_PAY.get(o['payment_status'], o['payment_status'])}\n"
        f"Delivery: {ORDER_DELIVERY.get(o['delivery_status'], o['delivery_status'])}\n"
        f"Date: {fmt_dt(o.get('completed_at') or o['created_at'])}"
    )


async def catalog():
    rows = await db.inventory.aggregate([
        {"$match": {"status": "available"}},
        {"$group": {"_id": "$product_id", "count": {"$sum": 1}, "min": {"$min": "$price_cents"}}},
    ]).to_list(None)
    stock = {r["_id"]: r for r in rows}
    prods = await db.products.find(
        {"product_id": {"$in": list(stock)}, "enabled": True, "deleted": {"$ne": True}}
    ).sort("name", 1).to_list(None)
    return prods, stock


async def product_stock(pid: str, only_enabled: bool = True):
    flt: dict = {"product_id": pid, "deleted": {"$ne": True}}
    if only_enabled:
        flt["enabled"] = True
    p = await db.products.find_one(flt)
    if not p:
        return None, 0, 0, 0
    r = await db.inventory.aggregate([
        {"$match": {"product_id": pid, "status": "available"}},
        {"$group": {"_id": None, "count": {"$sum": 1}, "min": {"$min": "$price_cents"}, "max": {"$max": "$price_cents"}}},
    ]).to_list(1)
    if not r:
        return p, 0, 0, 0
    return p, r[0]["count"], r[0]["min"], r[0]["max"]


# ═══════════════════════ middleware, filters, states ═══════════════════════


class UserMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u is None or u.is_bot:
            return await handler(event, data)
        doc = await db.users.find_one_and_update(
            {"user_id": u.id},
            {"$set": {"username": u.username, "first_name": u.first_name, "last_seen": now()},
             "$setOnInsert": {"created_at": now(), "banned": False}},
            upsert=True, return_document=ReturnDocument.AFTER,
        )
        if not doc.get("wallet_ready"):
            await db.wallets.update_one({"user_id": u.id}, {"$setOnInsert": {"balance_cents": 0, "created_at": now()}}, upsert=True)
            await db.users.update_one({"user_id": u.id}, {"$set": {"wallet_ready": True}})
        if doc.get("banned") and u.id not in ADMIN_SET:
            if isinstance(event, CallbackQuery):
                await alert(event, "🚫 Your account is restricted.")
            else:
                await event.answer("🚫 Your account is restricted.")
            return None
        data["db_user"] = doc
        return await handler(event, data)


class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        u = getattr(event, "from_user", None)
        return bool(u and u.id in ADMIN_SET)


class AddStock(StatesGroup):
    name = State()
    desc = State()
    cat = State()
    dprice = State()
    price = State()
    code = State()
    meta = State()


class EditProduct(StatesGroup):
    value = State()


class EditItem(StatesGroup):
    value = State()


class DepositSt(StatesGroup):
    amount = State()


class TermsSt(StatesGroup):
    text = State()


class SettingsSt(StatesGroup):
    value = State()


class SupportSt(StatesGroup):
    msg = State()


user_router = Router(name="user")
admin_router = Router(name="admin")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())

# ═══════════════════════════════ USER SIDE ═══════════════════════════════


@user_router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(welcome_text(), reply_markup=main_menu(m.from_user.id in ADMIN_SET))


@user_router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Cancelled.", reply_markup=main_menu(m.from_user.id in ADMIN_SET))


@user_router.callback_query(F.data == "home")
@user_router.callback_query(F.data == "cancel")
async def cb_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, welcome_text(), main_menu(c.from_user.id in ADMIN_SET))


@user_router.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


# ── products ──


@user_router.callback_query(F.data.startswith("pl:"))
async def cb_products(c: CallbackQuery):
    page = to_int(c.data.split(":")[1])
    prods, stock = await catalog()
    if not prods:
        return await show(c, "🛍 <b>Digital Store</b>\n\nNo products are available right now. Please check back soon.", kb([[back()]]))
    pages = max(1, -(-len(prods) // PAGE))
    page = min(max(page, 0), pages - 1)
    rows = [
        [btn(f"{p['name']} — {money(stock[p['product_id']]['min'])}", f"pv:{p['product_id']}", "primary")]
        for p in prods[page * PAGE:(page + 1) * PAGE]
    ]
    rows += pager("pl", page, pages)
    rows.append([back()])
    await show(c, "🛍 <b>Digital Store</b>\n\nChoose a product:", kb(rows))


def product_text(p: dict, count: int, lo: int, hi: int) -> str:
    price = money(lo) if lo == hi else f"from {money(lo)}"
    desc = f"\n\n{esc(p['description'])}" if p.get("description") else ""
    return (
        f"🛍 <b>{esc(p['name'])}</b>\n\n📂 Category: {esc(p.get('category') or 'General')}\n"
        f"💵 Price: <b>{price}</b>\n📦 In stock: <b>{count}</b>{desc}"
    )


@user_router.callback_query(F.data.startswith("pv:"))
async def cb_product(c: CallbackQuery):
    pid = c.data.split(":")[1]
    p, count, lo, hi = await product_stock(pid)
    if not p or count == 0:
        return await alert(c, "This product is no longer available.")
    await show(c, product_text(p, count, lo, hi), kb([
        [btn(f"🛒 Buy — {money(lo)}", f"pb:{pid}", "success")],
        [back("pl:0", "⬅️ Products"), btn("🏠 Home", "home", "primary")],
    ]))


@user_router.callback_query(F.data.startswith("pb:"))
async def cb_buy(c: CallbackQuery):
    pid = c.data.split(":")[1]
    p, count, lo, _ = await product_stock(pid)
    if not p or count == 0:
        return await alert(c, "Out of stock.")
    bal = await get_balance(c.from_user.id)
    text = (
        f"🧾 <b>Confirm purchase</b>\n\nProduct: <b>{esc(p['name'])}</b>\nFinal price: <b>{money(lo)}</b>\n"
        f"Wallet balance: {money(bal)}"
    )
    rows = []
    if bal >= lo:
        rows.append([btn("✅ Confirm purchase", f"pc:{pid}:{lo}", "success")])
    else:
        text += f"\n\n⚠️ You need {money(lo - bal)} more. Please top up your wallet."
        rows.append([btn("➕ Deposit", "wd", "success")])
    rows.append([cancel_btn(f"pv:{pid}")])
    await show(c, text, kb(rows))


@user_router.callback_query(F.data.startswith("pc:"))
async def cb_confirm_buy(c: CallbackQuery):
    parts = c.data.split(":")
    if len(parts) != 3:
        return await c.answer()
    pid, cents = parts[1], to_int(parts[2])
    if cents <= 0:
        return await c.answer()
    await c.answer("Processing…")
    order, item, err = await purchase(c.from_user.id, pid, cents)
    if err:
        return await show(c, f"❌ {err}", kb([[back(f'pv:{pid}', '⬅️ Product'), btn('🏠 Home', 'home', 'primary')]]))
    text = (
        f"✅ <b>Purchase complete</b>\n\nOrder: <code>{order['order_id']}</code>\n"
        f"Product: <b>{esc(order['product_name'])}</b>\nPaid: <b>{money(order['amount_cents'])}</b>\n\n{delivery_block(item)}"
    )
    rows = [[btn("📦 My Orders", "ol:0", "primary"), btn("🏠 Home", "home", "primary")]]
    try:
        await show(c, text, kb(rows))
        await db.orders.update_one({"order_id": order["order_id"]}, {"$set": {"delivery_status": "delivered", "delivered_at": now()}})
    except TelegramAPIError:
        log.exception("delivery message failed for %s", order["order_id"])
        await db.orders.update_one({"order_id": order["order_id"]}, {"$set": {"delivery_status": "pending"}})


# ── orders ──


@user_router.callback_query(F.data.startswith("ol:"))
async def cb_orders(c: CallbackQuery):
    page = to_int(c.data.split(":")[1])
    docs, page, pages, total = await page_query(db.orders, {"user_id": c.from_user.id}, [("created_at", -1)], page)
    if not total:
        return await show(c, "📦 <b>My Orders</b>\n\nYou have no orders yet.", kb([[back()]]))
    rows = [[btn(f"{o['order_id']} · {o['product_name'][:22]} · {money(o['amount_cents'])}", f"ov:{o['order_id']}", "primary")] for o in docs]
    rows += pager("ol", page, pages)
    rows.append([back()])
    await show(c, f"📦 <b>My Orders</b> ({total})", kb(rows))


@user_router.callback_query(F.data.startswith("ov:"))
async def cb_order_view(c: CallbackQuery):
    oid = c.data.split(":")[1]
    o = await db.orders.find_one({"order_id": oid, "user_id": c.from_user.id})  # ownership enforced
    if not o:
        return await alert(c, "Order not found.")
    text = order_text(o)
    if o["status"] == "completed" and o.get("item_id"):
        item = await db.inventory.find_one({"item_id": o["item_id"], "buyer_id": c.from_user.id, "order_id": oid})
        if item:
            text += "\n\n" + delivery_block(item)
            if o["delivery_status"] != "delivered":
                await db.orders.update_one({"order_id": oid}, {"$set": {"delivery_status": "delivered", "delivered_at": now()}})
    await show(c, text, kb([[back("ol:0", "⬅️ Orders"), btn("🏠 Home", "home", "primary")]]))


# ── wallet ──


@user_router.callback_query(F.data == "w")
async def cb_wallet(c: CallbackQuery, state: FSMContext):
    await state.clear()
    bal = await get_balance(c.from_user.id)
    content = Text(
        CustomEmoji("💰", custom_emoji_id=config.WALLET_EMOJI_ID), " ", Bold("Wallet"),
        "\n\nBalance: ", Bold(money(bal)),
    )
    await show(c, content=content, markup=kb([
        [btn("➕ Deposit", "wd", "success"), btn("📜 Transactions", "wt:0", "primary")],
        [back()],
    ]))


@user_router.callback_query(F.data.startswith("wt:"))
async def cb_transactions(c: CallbackQuery):
    page = to_int(c.data.split(":")[1])
    docs, page, pages, total = await page_query(db.wallet_transactions, {"user_id": c.from_user.id}, [("created_at", -1)], page, 8)
    if not total:
        text = "📜 <b>Transactions</b>\n\nNo transactions yet."
    else:
        lines = []
        for t in docs:
            sign = "+" if t["amount_cents"] >= 0 else "−"
            lines.append(f"{sign}{money(abs(t['amount_cents']))} · {esc(t['type'].title())} · {fmt_dt(t['created_at'])}")
        text = "📜 <b>Transactions</b>\n\n" + "\n".join(lines)
    await show(c, text, kb(pager("wt", page, pages) + [[back("w")]]))


async def deposit_confirm_screen(ev, cents: int, cur: str):
    await show(ev, (
        f"➕ <b>Confirm deposit</b>\n\nAmount: <b>{money(cents)}</b>\nPay with: <b>{cur}</b> via Binance Pay\n\n"
        f"You will pay {Decimal(cents) / 100:.2f} {cur}. The wallet is credited only after Binance confirms the payment."
    ), kb([[btn("✅ Confirm", f"wdy:{cents}:{cur}", "success")], [cancel_btn("w")]]))


async def deposit_step(ev, cents: int):
    s = await get_pay_settings()
    if not binance_ready(s):
        return await show(ev, "⚠️ Deposits are currently unavailable. Please contact support.", kb([[back("w")]]))
    if not (s["min_cents"] <= cents <= s["max_cents"]):
        return await show(ev, f"Amount must be between {money(s['min_cents'])} and {money(s['max_cents'])}.", kb([[back("wd")]]))
    if len(s["currencies"]) == 1:
        return await deposit_confirm_screen(ev, cents, s["currencies"][0])
    rows = [[btn(cur, f"wdk:{cents}:{cur}", "primary")] for cur in s["currencies"]]
    rows.append([back("wd")])
    await show(ev, f"➕ Deposit <b>{money(cents)}</b>\n\nChoose the payment currency:", kb(rows))


@user_router.callback_query(F.data == "wd")
async def cb_deposit(c: CallbackQuery, state: FSMContext):
    await state.clear()
    s = await get_pay_settings()
    if not binance_ready(s):
        return await alert(c, "Deposits are currently unavailable.")
    presets = [x for x in (500, 1000, 2000, 5000) if s["min_cents"] <= x <= s["max_cents"]]
    rows = []
    if presets:
        rows.append([btn(money(x), f"wda:{x}", "success") for x in presets])
    rows.append([btn("✏️ Custom amount", "wdc", "primary")])
    rows.append([back("w")])
    await show(c, f"➕ <b>Deposit</b>\n\nPay with Binance Pay.\nMin {money(s['min_cents'])} · Max {money(s['max_cents'])}", kb(rows))


@user_router.callback_query(F.data == "wdc")
async def cb_deposit_custom(c: CallbackQuery, state: FSMContext):
    await state.set_state(DepositSt.amount)
    await show(c, "✏️ Send the deposit amount in USD (e.g. <code>15</code> or <code>12.50</code>).", kb([[cancel_btn("w")]]))


@user_router.message(DepositSt.amount, F.text)
async def msg_deposit_amount(m: Message, state: FSMContext):
    cents = parse_money(m.text)
    if cents is None:
        return await m.answer("❌ Invalid amount. Send a number like <code>15</code> or <code>12.50</code>.", reply_markup=kb([[cancel_btn("w")]]))
    await state.clear()
    await deposit_step(m, cents)


@user_router.callback_query(F.data.startswith("wda:"))
async def cb_deposit_amount(c: CallbackQuery):
    await deposit_step(c, to_int(c.data.split(":")[1]))


@user_router.callback_query(F.data.startswith("wdk:"))
async def cb_deposit_currency(c: CallbackQuery):
    _, cents, cur = c.data.split(":")
    s = await get_pay_settings()
    if cur not in s["currencies"] or not (s["min_cents"] <= to_int(cents) <= s["max_cents"]):
        return await alert(c, "Invalid selection.")
    await deposit_confirm_screen(c, to_int(cents), cur)


def deposit_screen(pay: dict):
    text = (
        f"💳 <b>Binance Pay deposit</b>\n\nPayment ID: <code>{pay['payment_id']}</code>\n"
        f"Amount: <b>{pay['amount']} {pay['currency']}</b>\nStatus: {PAY_LABEL.get(pay['status'], pay['status'])}\n"
        f"Expires: {fmt_dt(pay['expires_at'])}\n\n"
        "1️⃣ Tap <b>Pay with Binance Pay</b> and complete the payment.\n"
        "2️⃣ Come back and tap <b>I've paid — Verify</b>.\n\n"
        "Your wallet is credited automatically once Binance confirms the payment."
    )
    rows = []
    if pay.get("checkout_url"):
        rows.append([btn("💳 Pay with Binance Pay", url=pay["checkout_url"], style="primary")])
    if pay.get("universal_url") and pay.get("universal_url") != pay.get("checkout_url"):
        rows.append([btn("📱 Open in Binance app", url=pay["universal_url"], style="primary")])
    rows.append([btn("✅ I've paid — Verify", f"wv:{pay['payment_id']}", "success")])
    rows.append([cancel_btn(f"wx:{pay['payment_id']}"), back("w", "⬅️ Wallet")])
    return text, kb(rows)


@user_router.callback_query(F.data.startswith("wdy:"))
async def cb_deposit_create(c: CallbackQuery):
    _, cents, cur = c.data.split(":")
    try:
        pay = await create_deposit(c.from_user.id, to_int(cents), cur)
    except BinanceError as e:
        log.warning("create_deposit failed for %s: %s", c.from_user.id, e)
        msg = str(e) if not str(e).startswith("Binance Pay") and "network" not in str(e) else "Could not create the payment. Please try again later."
        return await show(c, f"❌ {esc(msg)}", kb([[back("w")]]))
    text, markup = deposit_screen(pay)
    await show(c, text, markup)


@user_router.callback_query(F.data.startswith("wv:"))
async def cb_verify(c: CallbackQuery):
    pid = c.data.split(":")[1]
    pay = await db.payments.find_one({"payment_id": pid, "user_id": c.from_user.id})  # ownership enforced
    if not pay:
        return await alert(c, "Payment not found.")
    pay, credited = await process_payment(pid)
    if pay["status"] == "pending":
        return await alert(c, "Payment not detected yet. Complete it in Binance Pay, then verify again.")
    if pay["status"] in ("completed", "crediting"):
        bal = await get_balance(c.from_user.id)
        head = "Deposit confirmed" if credited or pay["status"] == "completed" else "Processing"
        content = Text(
            CustomEmoji("💰", custom_emoji_id=config.WALLET_EMOJI_ID), " ", Bold(head),
            f"\n\n{money(pay['amount_cents'])} deposit — {PAY_LABEL[pay['status']]}\nBalance: ", Bold(money(bal)),
        )
        return await show(c, content=content, markup=kb([[back("w", "⬅️ Wallet"), btn("🏠 Home", "home", "primary")]]))
    await show(c, f"Payment <code>{pid}</code>: {PAY_LABEL.get(pay['status'], pay['status'])}.\nIf you were charged, contact support with this ID.",
               kb([[back("w", "⬅️ Wallet")]]))


@user_router.callback_query(F.data.startswith("wx:"))
async def cb_deposit_cancel(c: CallbackQuery):
    pid = c.data.split(":")[1]
    pay = await db.payments.find_one({"payment_id": pid, "user_id": c.from_user.id})
    if not pay:
        return await alert(c, "Payment not found.")
    pay, credited = await process_payment(pid)  # never cancel something that was already paid
    if pay["status"] == "pending":
        await db.payments.update_one({"payment_id": pid, "status": "pending"}, {"$set": {"status": "cancelled", "verified_at": now()}})
        await binance_close(pay["merchant_trade_no"])
        return await show(c, "🚫 Deposit cancelled.", kb([[back("w", "⬅️ Wallet")]]))
    await show(c, f"Payment <code>{pid}</code> is already {PAY_LABEL.get(pay['status'], pay['status'])}.", kb([[back("w", "⬅️ Wallet")]]))


# ── support & terms ──


@user_router.callback_query(F.data == "sup")
async def cb_support(c: CallbackQuery, state: FSMContext):
    await state.clear()
    content = Text(
        CustomEmoji("💬", custom_emoji_id=config.SUPPORT_EMOJI_ID), " ", Bold("Support"),
        "\n\nNeed help with an order or deposit? Contact our team and include your order or payment ID.",
    )
    await show(c, content=content, markup=kb([
        [btn("💬 Contact Support", url=f"https://t.me/{config.SUPPORT_USERNAME.lstrip('@')}", style="primary")],
        [btn("📨 Send a message here", "supm", "primary")],
        [back()],
    ]))


@user_router.callback_query(F.data == "supm")
async def cb_support_msg(c: CallbackQuery, state: FSMContext):
    await state.set_state(SupportSt.msg)
    await show(c, "📨 Type your message for support (max 1000 characters).\nDo NOT share passwords, OTP codes or private keys.", kb([[cancel_btn("sup")]]))


@user_router.message(SupportSt.msg, F.text)
async def msg_support(m: Message, state: FSMContext):
    if len(m.text) > 1000:
        return await m.answer("❌ Too long (max 1000 characters).")
    await state.clear()
    who = f"@{m.from_user.username}" if m.from_user.username else m.from_user.full_name
    body = f"💬 Support message from {who} (ID {m.from_user.id}):\n\n{m.text}"
    try:
        await bot_ref.send_message(config.SUPPORT_ID, body, parse_mode=None)
    except TelegramAPIError:
        for aid in ADMIN_SET:
            await safe_send(aid, text=body, parse_mode=None)
    await m.answer("✅ Your message was sent to support.", reply_markup=kb([[back()]]))


@user_router.callback_query(F.data == "terms")
async def cb_terms(c: CallbackQuery):
    terms = await get_setting("terms", config.TERMS_TEXT)
    await show(c, content=Text(Bold("📜 Terms & Conditions"), "\n\n", terms), markup=kb([[back()]]))


@user_router.callback_query(F.data.startswith("adm:"))
async def cb_admin_denied(c: CallbackQuery):
    if c.from_user.id in ADMIN_SET:
        return await alert(c, "This action expired. Please reopen it from the Admin Panel.")
    await alert(c, "⛔ Admins only.")


@user_router.message(StateFilter(None))
async def msg_fallback(m: Message):
    await m.answer(welcome_text(), reply_markup=main_menu(m.from_user.id in ADMIN_SET))


# ═══════════════════════════════ ADMIN SIDE ═══════════════════════════════


def admin_menu() -> InlineKeyboardMarkup:
    return kb([
        [btn("➕ Add Stock", "adm:as:0", "success"), btn("📦 Active Stock", "adm:ai:0", "primary")],
        [btn("🛒 Sold Stock", "adm:ss:0", "primary"), btn("🛍 Products", "adm:pr:0", "primary")],
        [btn("👥 Users", "adm:us:0", "primary"), btn("💰 Payments", "adm:py:0", "primary")],
        [btn("📊 Statistics", "adm:st", "primary"), btn("💳 Payment Settings", "adm:ps", "primary")],
        [btn("📝 Terms", "adm:tm", "primary")],
        [back("home")],
    ])


def admin_back(cb: str = "adm:home", text: str = "⬅️ Back"):
    return back(cb, text)


@admin_router.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("⚙️ <b>Admin Panel</b>", reply_markup=admin_menu())


@admin_router.callback_query(F.data.in_({"adm:home", "adm:cancel"}))
async def cb_admin_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, "⚙️ <b>Admin Panel</b>", admin_menu())


def cancel_kb():
    return kb([[cancel_btn("adm:cancel")]])


# ── add stock / add product ──


@admin_router.callback_query(F.data.startswith("adm:as:"))
async def cb_as_list(c: CallbackQuery, state: FSMContext):
    await state.clear()
    page = to_int(c.data.split(":")[2])
    docs, page, pages, _ = await page_query(db.products, {"deleted": {"$ne": True}}, [("name", 1)], page)
    rows = [[btn(f"{p['name']} — {money(p['price_cents'])}", f"adm:asp:{p['product_id']}", "primary")] for p in docs]
    rows += pager("adm:as", page, pages)
    rows.append([btn("➕ Add Digital Product", "adm:pn:1", "success")])
    rows.append([admin_back()])
    await show(c, "➕ <b>Add Stock</b>\n\nSelect a product or create a new one:", kb(rows))


@admin_router.callback_query(F.data.startswith("adm:pn:"))
async def cb_new_product(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(then_stock=c.data.endswith(":1"))
    await state.set_state(AddStock.name)
    await show(c, "🆕 <b>New product</b>\n\nSend the product name (2–60 characters).", cancel_kb())


@admin_router.message(AddStock.name, F.text)
async def msg_p_name(m: Message, state: FSMContext):
    t = m.text.strip()
    if not 2 <= len(t) <= 60:
        return await m.answer("❌ Name must be 2–60 characters.", reply_markup=cancel_kb())
    await state.update_data(name=t)
    await state.set_state(AddStock.desc)
    await m.answer("Send a description (max 500 chars) or <code>-</code> to skip.", reply_markup=cancel_kb())


@admin_router.message(AddStock.desc, F.text)
async def msg_p_desc(m: Message, state: FSMContext):
    t = m.text.strip()
    if len(t) > 500:
        return await m.answer("❌ Max 500 characters.", reply_markup=cancel_kb())
    await state.update_data(desc="" if t == "-" else t)
    await state.set_state(AddStock.cat)
    await m.answer("Send the category (e.g. <i>Software</i>, <i>Gift cards</i>, max 30 chars).", reply_markup=cancel_kb())


@admin_router.message(AddStock.cat, F.text)
async def msg_p_cat(m: Message, state: FSMContext):
    t = m.text.strip()
    if not 1 <= len(t) <= 30:
        return await m.answer("❌ Category must be 1–30 characters.", reply_markup=cancel_kb())
    await state.update_data(cat=t)
    await state.set_state(AddStock.dprice)
    await m.answer("Send the default price in USD (e.g. <code>9.99</code>).", reply_markup=cancel_kb())


@admin_router.message(AddStock.dprice, F.text)
async def msg_p_price(m: Message, state: FSMContext):
    cents = parse_money(m.text)
    if cents is None:
        return await m.answer("❌ Invalid price. Example: <code>9.99</code>", reply_markup=cancel_kb())
    d = await state.get_data()
    pid = new_id("PRD", 5)
    await db.products.insert_one({
        "product_id": pid, "name": d["name"], "description": d["desc"], "category": d["cat"], "price_cents": cents,
        "enabled": True, "deleted": False, "created_at": now(), "created_by": m.from_user.id,
    })
    if d.get("then_stock"):
        await state.clear()
        await state.update_data(product_id=pid)
        return await ask_item_price(m, state, pid)
    await state.clear()
    await admin_product_card(m, pid)


@admin_router.callback_query(F.data.startswith("adm:asp:"))
async def cb_as_product(c: CallbackQuery, state: FSMContext):
    pid = c.data.split(":")[2]
    if not await db.products.find_one({"product_id": pid, "deleted": {"$ne": True}}):
        return await alert(c, "Product not found.")
    await state.clear()
    await state.update_data(product_id=pid)
    await ask_item_price(c, state, pid)


async def ask_item_price(ev, state: FSMContext, pid: str):
    p = await db.products.find_one({"product_id": pid})
    await state.set_state(AddStock.price)
    await show(ev, f"💵 <b>{esc(p['name'])}</b>\n\nSend the price for these items (e.g. <code>9.99</code>) or use the default {money(p['price_cents'])}.",
               kb([[btn(f"Use default {money(p['price_cents'])}", "adm:asd", "success")], [cancel_btn("adm:cancel")]]))


async def ask_code(ev, state: FSMContext):
    await state.set_state(AddStock.code)
    await show(ev, (
        "🔑 Send the <b>license / activation code</b>.\nFor several items send one code per line (max 50).\n\n"
        "⚠️ Never send passwords, recovery details, OTP/2FA codes, session strings or private keys. "
        "Only products you are authorized to resell."
    ), cancel_kb())


@admin_router.message(AddStock.price, F.text)
async def msg_item_price(m: Message, state: FSMContext):
    cents = parse_money(m.text)
    if cents is None:
        return await m.answer("❌ Invalid price. Example: <code>9.99</code>", reply_markup=cancel_kb())
    await state.update_data(price_cents=cents)
    await ask_code(m, state)


@admin_router.callback_query(F.data == "adm:asd", AddStock.price)
async def cb_default_price(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    p = await db.products.find_one({"product_id": d.get("product_id")})
    if not p:
        return await alert(c, "Product not found.")
    await state.update_data(price_cents=p["price_cents"])
    await ask_code(c, state)


@admin_router.message(AddStock.code, F.text)
async def msg_item_code(m: Message, state: FSMContext):
    lines = list(dict.fromkeys(l.strip() for l in m.text.splitlines() if l.strip()))
    if not lines or len(lines) > 50:
        return await m.answer("❌ Send between 1 and 50 codes (one per line).", reply_markup=cancel_kb())
    if any(len(l) > 1000 for l in lines):
        return await m.answer("❌ A code is longer than 1000 characters.", reply_markup=cancel_kb())
    if SECRET_PATTERN.search(m.text):
        return await m.answer("⛔ This looks like account credentials. Only license/activation codes are allowed.", reply_markup=cancel_kb())
    await state.update_data(codes=lines)
    await state.set_state(AddStock.meta)
    await m.answer(
        "ℹ️ Optional: send product details (expiry date, region, delivery instructions — max 500 chars) or skip.",
        reply_markup=kb([[btn("Skip ⏭", "adm:asm", "primary")], [cancel_btn("adm:cancel")]]),
    )


async def save_items(ev, state: FSMContext, meta: str, admin_id: int):
    d = await state.get_data()
    await state.clear()
    pid, cents = d["product_id"], d["price_cents"]
    added = dup = 0
    for code in d["codes"]:
        try:
            await db.inventory.insert_one({
                "item_id": new_id("ITM", 6), "product_id": pid, "price_cents": cents, "code_enc": enc(code),
                "code_hash": hashlib.sha256(code.encode()).hexdigest(), "metadata": meta, "status": "available",
                "created_at": now(), "added_by": admin_id,
            })
            added += 1
        except DuplicateKeyError:
            dup += 1
    p = await db.products.find_one({"product_id": pid})
    text = f"✅ Added <b>{added}</b> item(s) to <b>{esc(p['name'])}</b> at {money(cents)}."
    if dup:
        text += f"\n⚠️ {dup} duplicate code(s) skipped."
    await show(ev, text, kb([[btn("➕ Add more", f"adm:asp:{pid}", "success")], [admin_back("adm:home", "⬅️ Admin Panel")]]))


@admin_router.message(AddStock.meta, F.text)
async def msg_item_meta(m: Message, state: FSMContext):
    t = m.text.strip()
    if len(t) > 500:
        return await m.answer("❌ Max 500 characters.", reply_markup=cancel_kb())
    if SECRET_PATTERN.search(t):
        return await m.answer("⛔ Credentials are not allowed in product details.", reply_markup=cancel_kb())
    await save_items(m, state, t, m.from_user.id)


@admin_router.callback_query(F.data == "adm:asm", AddStock.meta)
async def cb_skip_meta(c: CallbackQuery, state: FSMContext):
    await save_items(c, state, "", c.from_user.id)


# ── active stock ──


@admin_router.callback_query(F.data.startswith("adm:ai:"))
async def cb_active(c: CallbackQuery, state: FSMContext):
    await state.clear()
    page = to_int(c.data.split(":")[2])
    summary = await db.inventory.aggregate([
        {"$match": {"status": "available"}}, {"$group": {"_id": "$product_id", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}, {"$limit": 15},
    ]).to_list(15)
    names = {p["product_id"]: p["name"] async for p in db.products.find({"product_id": {"$in": [s["_id"] for s in summary]}})}
    head = "\n".join(f"• {esc(names.get(s['_id'], '?'))}: <b>{s['n']}</b>" for s in summary) or "No active stock."
    docs, page, pages, total = await page_query(db.inventory, {"status": "available"}, [("created_at", -1)], page)
    pn = {p["product_id"]: p["name"] async for p in db.products.find({"product_id": {"$in": [d["product_id"] for d in docs]}})}
    rows = [[btn(f"{pn.get(i['product_id'], '?')[:24]} · {money(i['price_cents'])} · {i['created_at'].strftime('%m-%d')}", f"adm:ii:{i['item_id']}", "primary")] for i in docs]
    rows += pager("adm:ai", page, pages)
    rows.append([admin_back()])
    await show(c, f"📦 <b>Active Stock</b> ({total} items)\n\n{head}", kb(rows))


async def item_card(ev, item_id: str):
    it = await db.inventory.find_one({"item_id": item_id, "status": "available"})
    if not it:
        return await show(ev, "Item not found (it may have been sold or deleted).", kb([[admin_back("adm:ai:0", "⬅️ Active Stock")]]))
    p = await db.products.find_one({"product_id": it["product_id"]})
    code = dec(it["code_enc"])
    text = (
        f"📦 <b>{esc(p['name'] if p else '?')}</b>\n\nItem: <code>{it['item_id']}</code>\nPrice: <b>{money(it['price_cents'])}</b>\n"
        f"Added: {fmt_dt(it['created_at'])}\nCode: <code>{esc(code[:3])}{'•' * 6}</code>\nDetails: {esc(it.get('metadata') or '—')}"
    )
    await show(ev, text, kb([
        [btn("✏️ Edit price", f"adm:ie:{item_id}:p", "primary"), btn("📝 Edit details", f"adm:ie:{item_id}:d", "primary")],
        [btn("🗑 Delete", f"adm:id:{item_id}", "danger")],
        [admin_back("adm:ai:0", "⬅️ Active Stock")],
    ]))


@admin_router.callback_query(F.data.startswith("adm:ii:"))
async def cb_item(c: CallbackQuery):
    await item_card(c, c.data.split(":")[2])


@admin_router.callback_query(F.data.startswith("adm:ie:"))
async def cb_item_edit(c: CallbackQuery, state: FSMContext):
    _, _, item_id, f = c.data.split(":")
    if not await db.inventory.find_one({"item_id": item_id, "status": "available"}):
        return await alert(c, "Item not available.")
    await state.set_state(EditItem.value)
    await state.update_data(item_id=item_id, f=f)
    prompt = "Send the new price (e.g. <code>9.99</code>)." if f == "p" else "Send new details (max 500 chars) or <code>-</code> to clear."
    await show(c, "✏️ " + prompt, cancel_kb())


@admin_router.message(EditItem.value, F.text)
async def msg_item_edit(m: Message, state: FSMContext):
    d = await state.get_data()
    t = m.text.strip()
    if d["f"] == "p":
        v = parse_money(t)
        if v is None:
            return await m.answer("❌ Invalid price.", reply_markup=cancel_kb())
        upd = {"price_cents": v}
    else:
        if len(t) > 500 or SECRET_PATTERN.search(t):
            return await m.answer("❌ Too long or contains credential-like content.", reply_markup=cancel_kb())
        upd = {"metadata": "" if t == "-" else t}
    await db.inventory.update_one({"item_id": d["item_id"], "status": "available"}, {"$set": upd})
    await state.clear()
    await item_card(m, d["item_id"])


@admin_router.callback_query(F.data.startswith("adm:id:"))
async def cb_item_delete(c: CallbackQuery):
    item_id = c.data.split(":")[2]
    await show(c, "🗑 <b>Delete this stock item?</b>\nThis cannot be undone.", kb([
        [btn("🗑 Yes, delete", f"adm:idy:{item_id}", "danger")], [back(f"adm:ii:{item_id}", "⬅️ Keep it")],
    ]))


@admin_router.callback_query(F.data.startswith("adm:idy:"))
async def cb_item_delete_yes(c: CallbackQuery):
    item_id = c.data.split(":")[2]
    r = await db.inventory.update_one(
        {"item_id": item_id, "status": "available"},
        {"$set": {"status": "removed", "removed_at": now(), "removed_by": c.from_user.id}, "$unset": {"code_hash": ""}},
    )
    await show(c, "✅ Item deleted." if r.modified_count else "Item was not available.", kb([[admin_back("adm:ai:0", "⬅️ Active Stock")]]))


# ── sold stock ──


@admin_router.callback_query(F.data.startswith("adm:ss:"))
async def cb_sold(c: CallbackQuery):
    page = to_int(c.data.split(":")[2])
    docs, page, pages, total = await page_query(db.orders, {"status": "completed"}, [("completed_at", -1)], page)
    rows = [[btn(f"{o['order_id']} · {o['product_name'][:20]} · {money(o['amount_cents'])}", f"adm:so:{o['order_id']}", "primary")] for o in docs]
    rows += pager("adm:ss", page, pages)
    rows.append([admin_back()])
    await show(c, f"🛒 <b>Sold Stock</b> ({total})", kb(rows))


@admin_router.callback_query(F.data.startswith("adm:so:"))
async def cb_sold_view(c: CallbackQuery):
    o = await db.orders.find_one({"order_id": c.data.split(":")[2]})
    if not o:
        return await alert(c, "Order not found.")
    text = (
        f"🛒 <b>Sold item</b>\n\nOrder: <code>{o['order_id']}</code>\nUser ID: <code>{o['user_id']}</code>\n"
        f"Product: <b>{esc(o['product_name'])}</b>\nAmount: <b>{money(o['amount_cents'])}</b>\nSale date: {fmt_dt(o.get('completed_at'))}\n"
        f"Payment: {ORDER_PAY.get(o['payment_status'], o['payment_status'])}\nDelivery: {ORDER_DELIVERY.get(o['delivery_status'], o['delivery_status'])}"
    )
    await show(c, text, kb([[admin_back("adm:ss:0", "⬅️ Sold Stock")]]))


# ── products ──


async def admin_product_card(ev, pid: str):
    p, count, lo, hi = await product_stock(pid, only_enabled=False)
    if not p:
        return await show(ev, "Product not found.", kb([[admin_back("adm:pr:0", "⬅️ Products")]]))
    text = (
        f"🛍 <b>{esc(p['name'])}</b>\nID: <code>{pid}</code>\nCategory: {esc(p.get('category') or '—')}\n"
        f"Default price: <b>{money(p['price_cents'])}</b>\nStatus: {'✅ Enabled' if p['enabled'] else '🚫 Disabled'}\n"
        f"Active stock: <b>{count}</b>\n\n{esc(p.get('description') or 'No description')}"
    )
    toggle = btn("🚫 Disable", f"adm:pt:{pid}", "danger") if p["enabled"] else btn("✅ Enable", f"adm:pt:{pid}", "success")
    await show(ev, text, kb([
        [btn("✏️ Name", f"adm:pe:{pid}:n", "primary"), btn("📝 Description", f"adm:pe:{pid}:d", "primary")],
        [btn("📂 Category", f"adm:pe:{pid}:c", "primary"), btn("💵 Price", f"adm:pe:{pid}:p", "primary")],
        [btn("➕ Add stock", f"adm:asp:{pid}", "success"), toggle],
        [btn("🗑 Delete product", f"adm:pd:{pid}", "danger")],
        [admin_back("adm:pr:0", "⬅️ Products")],
    ]))


@admin_router.callback_query(F.data.startswith("adm:pr:"))
async def cb_products_admin(c: CallbackQuery, state: FSMContext):
    await state.clear()
    page = to_int(c.data.split(":")[2])
    docs, page, pages, total = await page_query(db.products, {"deleted": {"$ne": True}}, [("created_at", -1)], page)
    rows = [[btn(f"{'✅' if p['enabled'] else '🚫'} {p['name'][:28]} — {money(p['price_cents'])}", f"adm:pv:{p['product_id']}", "primary")] for p in docs]
    rows += pager("adm:pr", page, pages)
    rows.append([btn("➕ Add Product", "adm:pn:0", "success")])
    rows.append([admin_back()])
    await show(c, f"🛍 <b>Products</b> ({total})", kb(rows))


@admin_router.callback_query(F.data.startswith("adm:pv:"))
async def cb_product_admin(c: CallbackQuery):
    await admin_product_card(c, c.data.split(":")[2])


@admin_router.callback_query(F.data.startswith("adm:pt:"))
async def cb_product_toggle(c: CallbackQuery):
    pid = c.data.split(":")[2]
    p = await db.products.find_one({"product_id": pid, "deleted": {"$ne": True}})
    if not p:
        return await alert(c, "Product not found.")
    await db.products.update_one({"product_id": pid}, {"$set": {"enabled": not p["enabled"]}})
    await admin_product_card(c, pid)


FIELD_NAMES = {"n": "name", "d": "description", "c": "category", "p": "price"}


@admin_router.callback_query(F.data.startswith("adm:pe:"))
async def cb_product_edit(c: CallbackQuery, state: FSMContext):
    _, _, pid, f = c.data.split(":")
    if f not in FIELD_NAMES or not await db.products.find_one({"product_id": pid, "deleted": {"$ne": True}}):
        return await alert(c, "Invalid request.")
    await state.set_state(EditProduct.value)
    await state.update_data(pid=pid, f=f)
    hint = " (or <code>-</code> to clear)" if f == "d" else ""
    await show(c, f"✏️ Send the new <b>{FIELD_NAMES[f]}</b>{hint}.", cancel_kb())


@admin_router.message(EditProduct.value, F.text)
async def msg_product_edit(m: Message, state: FSMContext):
    d = await state.get_data()
    f, t = d["f"], m.text.strip()
    val: Any = None
    if f == "n" and 2 <= len(t) <= 60:
        val = t
    elif f == "d" and len(t) <= 500:
        val = "" if t == "-" else t
    elif f == "c" and 1 <= len(t) <= 30:
        val = t
    elif f == "p":
        val = parse_money(t)
    if val is None:
        return await m.answer("❌ Invalid value, try again.", reply_markup=cancel_kb())
    key = {"n": "name", "d": "description", "c": "category", "p": "price_cents"}[f]
    await db.products.update_one({"product_id": d["pid"]}, {"$set": {key: val}})
    await state.clear()
    await admin_product_card(m, d["pid"])


@admin_router.callback_query(F.data.startswith("adm:pd:"))
async def cb_product_delete(c: CallbackQuery):
    pid = c.data.split(":")[2]
    await show(c, "🗑 <b>Delete this product?</b>\nIts unsold stock will be removed. Past orders are kept.", kb([
        [btn("🗑 Yes, delete", f"adm:pdy:{pid}", "danger")], [back(f"adm:pv:{pid}", "⬅️ Keep it")],
    ]))


@admin_router.callback_query(F.data.startswith("adm:pdy:"))
async def cb_product_delete_yes(c: CallbackQuery):
    pid = c.data.split(":")[2]
    await db.products.update_one({"product_id": pid}, {"$set": {"deleted": True, "enabled": False, "deleted_at": now()}})
    await db.inventory.update_many(
        {"product_id": pid, "status": "available"},
        {"$set": {"status": "removed", "removed_at": now()}, "$unset": {"code_hash": ""}},
    )
    await show(c, "✅ Product deleted (history preserved).", kb([[admin_back("adm:pr:0", "⬅️ Products")]]))


# ── users ──


@admin_router.callback_query(F.data.startswith("adm:us:"))
async def cb_users(c: CallbackQuery):
    page = to_int(c.data.split(":")[2])
    docs, page, pages, total = await page_query(db.users, {}, [("created_at", -1)], page)
    rows = [[btn(f"{'🚫 ' if u.get('banned') else ''}{('@' + u['username']) if u.get('username') else u['user_id']}", f"adm:uv:{u['user_id']}", "primary")] for u in docs]
    rows += pager("adm:us", page, pages)
    rows.append([admin_back()])
    await show(c, f"👥 <b>Users</b> ({total})", kb(rows))


async def user_card(ev, uid: int):
    u = await db.users.find_one({"user_id": uid})
    if not u:
        return await show(ev, "User not found.", kb([[admin_back("adm:us:0", "⬅️ Users")]]))
    bal = await get_balance(uid)
    orders = await db.orders.count_documents({"user_id": uid, "status": "completed"})
    text = (
        f"👤 <b>User</b> <code>{uid}</code>\nUsername: {('@' + esc(u['username'])) if u.get('username') else '—'}\nName: {esc(u.get('first_name') or '—')}\n"
        f"Balance: <b>{money(bal)}</b>\nCompleted orders: {orders}\nJoined: {fmt_dt(u.get('created_at'))}\nStatus: {'🚫 Banned' if u.get('banned') else '✅ Active'}"
    )
    rows = []
    if uid not in ADMIN_SET:
        rows.append([btn("✅ Unban", f"adm:ub:{uid}", "success") if u.get("banned") else btn("🚫 Ban", f"adm:ub:{uid}", "danger")])
    rows.append([admin_back("adm:us:0", "⬅️ Users")])
    await show(ev, text, kb(rows))


@admin_router.callback_query(F.data.startswith("adm:uv:"))
async def cb_user(c: CallbackQuery):
    await user_card(c, to_int(c.data.split(":")[2]))


@admin_router.callback_query(F.data.startswith("adm:ub:"))
async def cb_user_ban(c: CallbackQuery):
    uid = to_int(c.data.split(":")[2])
    u = await db.users.find_one({"user_id": uid})
    if not u or uid in ADMIN_SET:
        return await alert(c, "Not allowed.")
    await db.users.update_one({"user_id": uid}, {"$set": {"banned": not u.get("banned", False)}})
    await user_card(c, uid)


# ── payments ──


@admin_router.callback_query(F.data.startswith("adm:py:"))
async def cb_payments(c: CallbackQuery):
    page = to_int(c.data.split(":")[2])
    docs, page, pages, total = await page_query(db.payments, {}, [("created_at", -1)], page)
    rows = [[btn(f"{PAY_LABEL.get(p['status'], p['status'])[:2]} {p['payment_id'][:12]}… · {p['amount']} {p['currency']}", f"adm:pyv:{p['payment_id']}", "primary")] for p in docs]
    rows += pager("adm:py", page, pages)
    rows.append([admin_back()])
    await show(c, f"💰 <b>Payments</b> ({total})", kb(rows))


async def payment_card(ev, payment_id: str):
    p = await db.payments.find_one({"payment_id": payment_id})
    if not p:
        return await show(ev, "Payment not found.", kb([[admin_back("adm:py:0", "⬅️ Payments")]]))
    text = (
        f"💰 <b>Payment</b> <code>{p['payment_id']}</code>\nBinance ref: <code>{esc(p.get('provider_order_id') or '—')}</code>\n"
        f"User ID: <code>{p['user_id']}</code>\nAmount: <b>{p['amount']} {p['currency']}</b>\nStatus: {PAY_LABEL.get(p['status'], p['status'])}\n"
        f"Credited: {'yes' if p.get('credited') else 'no'}\nCreated: {fmt_dt(p['created_at'])}\nVerified: {fmt_dt(p.get('verified_at'))}"
    )
    rows = []
    if p["status"] == "pending":
        rows.append([btn("🔄 Re-verify", f"adm:pyr:{payment_id}", "success")])
    rows.append([admin_back("adm:py:0", "⬅️ Payments")])
    await show(ev, text, kb(rows))


@admin_router.callback_query(F.data.startswith("adm:pyv:"))
async def cb_payment(c: CallbackQuery):
    await payment_card(c, c.data.split(":")[2])


@admin_router.callback_query(F.data.startswith("adm:pyr:"))
async def cb_payment_reverify(c: CallbackQuery):
    pid = c.data.split(":")[2]
    await process_payment(pid, notify=True)
    await payment_card(c, pid)


# ── statistics ──


async def revenue(since: Optional[datetime] = None) -> int:
    m: dict = {"status": "completed"}
    if since:
        m["completed_at"] = {"$gte": since}
    r = await db.orders.aggregate([{"$match": m}, {"$group": {"_id": None, "s": {"$sum": "$amount_cents"}}}]).to_list(1)
    return r[0]["s"] if r else 0


@admin_router.callback_query(F.data == "adm:st")
async def cb_stats(c: CallbackQuery):
    t = now()
    day = t.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    async def wallet_funds() -> int:
        r = await db.wallets.aggregate([{"$group": {"_id": None, "s": {"$sum": "$balance_cents"}}}]).to_list(1)
        return r[0]["s"] if r else 0
    (users, prods, active, sold, orders, done, pending, failed, rev, rev_d, rev_m, funds) = await asyncio.gather(
        db.users.count_documents({}),
        db.products.count_documents({"deleted": {"$ne": True}}),
        db.inventory.count_documents({"status": "available"}),
        db.inventory.count_documents({"status": "sold"}),
        db.orders.count_documents({}),
        db.orders.count_documents({"status": "completed"}),
        db.payments.count_documents({"status": {"$in": ["pending", "crediting"]}}),
        db.payments.count_documents({"status": {"$in": ["failed", "expired", "cancelled", "mismatch"]}}),
        revenue(), revenue(day), revenue(month), wallet_funds(),
    )
    text = (
        "📊 <b>Statistics</b>\n\n"
        f"👥 Users: <b>{users}</b>\n🛍 Products: <b>{prods}</b>\n📦 Active inventory: <b>{active}</b>\n🛒 Sold items: <b>{sold}</b>\n"
        f"🧾 Orders: <b>{orders}</b> (completed {done})\n⏳ Pending payments: <b>{pending}</b>\n❌ Failed payments: <b>{failed}</b>\n\n"
        f"💵 Total revenue: <b>{money(rev)}</b>\n📅 Today: <b>{money(rev_d)}</b>\n🗓 This month: <b>{money(rev_m)}</b>\n"
        f"💰 User wallet funds: <b>{money(funds)}</b>"
    )
    await show(c, text, kb([[btn("🔄 Refresh", "adm:st", "success"), admin_back()]]))


# ── payment settings ──


async def settings_screen(ev):
    s = await get_pay_settings()
    def state_of(v): return "✅ set" if v and "REPLACE" not in v else "⚠️ not set"
    text = (
        "💳 <b>Payment Settings</b>\n\n"
        f"Binance Pay: {'✅ Enabled' if s['enabled'] else '🚫 Disabled'}\n"
        f"Merchant ID: <code>{esc(mask(s['merchant_id'])) if 'REPLACE' not in s['merchant_id'] else '—'}</code>\n"
        f"API key: <code>{esc(mask(s['api_key'])) if 'REPLACE' not in s['api_key'] else '—'}</code> ({state_of(s['api_key'])})\n"
        f"API secret: <code>••••••••</code> ({state_of(s['api_secret'])})\n"
        f"Currencies: {', '.join(s['currencies']) or '—'}\n"
        f"Min deposit: <b>{money(s['min_cents'])}</b> · Max: <b>{money(s['max_cents'])}</b>\n\n"
        "🔒 Secrets are stored encrypted and are never displayed."
    )
    rows = [[btn("🚫 Disable Binance Pay", "adm:pse", "danger") if s["enabled"] else btn("✅ Enable Binance Pay", "adm:pse", "success")]]
    rows.append([btn(f"{'✅' if cur in s['currencies'] else '⬜'} {cur}", f"adm:psc:{cur}", "primary") for cur in config.SUPPORTED_CURRENCIES])
    rows.append([btn("Min deposit", "adm:psm:min", "primary"), btn("Max deposit", "adm:psm:max", "primary")])
    rows.append([btn("Merchant ID", "adm:psm:mid", "primary"), btn("API key", "adm:psm:key", "primary")])
    rows.append([btn("🔑 API secret", "adm:psm:sec", "primary")])
    rows.append([admin_back()])
    await show(ev, text, kb(rows))


@admin_router.callback_query(F.data == "adm:ps")
async def cb_settings(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await settings_screen(c)


@admin_router.callback_query(F.data == "adm:pse")
async def cb_settings_toggle(c: CallbackQuery):
    s = await get_pay_settings()
    await patch_pay_settings(enabled=not s["enabled"])
    await settings_screen(c)


@admin_router.callback_query(F.data.startswith("adm:psc:"))
async def cb_settings_currency(c: CallbackQuery):
    cur = c.data.split(":")[2]
    if cur not in config.SUPPORTED_CURRENCIES:
        return await alert(c, "Unsupported currency.")
    s = await get_pay_settings()
    cur_list = [x for x in s["currencies"]]
    if cur in cur_list:
        if len(cur_list) == 1:
            return await alert(c, "At least one currency must stay enabled.")
        cur_list.remove(cur)
    else:
        cur_list.append(cur)
    await patch_pay_settings(currencies=cur_list)
    await settings_screen(c)


SETTING_FIELDS = {
    "min": "minimum deposit in USD", "max": "maximum deposit in USD", "mid": "Binance merchant ID",
    "key": "Binance Pay API key", "sec": "Binance Pay API secret",
}


@admin_router.callback_query(F.data.startswith("adm:psm:"))
async def cb_settings_edit(c: CallbackQuery, state: FSMContext):
    f = c.data.split(":")[2]
    if f not in SETTING_FIELDS:
        return await alert(c, "Invalid field.")
    await state.set_state(SettingsSt.value)
    await state.update_data(f=f)
    extra = "\n🔒 Your message will be deleted right after it is read." if f in ("mid", "key", "sec") else ""
    await show(c, f"✏️ Send the new <b>{SETTING_FIELDS[f]}</b>.{extra}", kb([[cancel_btn("adm:ps")]]))


@admin_router.message(SettingsSt.value, F.text)
async def msg_settings_value(m: Message, state: FSMContext):
    d = await state.get_data()
    f, raw = d["f"], m.text.strip()
    if f in ("mid", "key", "sec"):
        try:
            await m.delete()
        except TelegramAPIError:
            pass
        if not re.fullmatch(r"[\w\-+=/.]{8,256}", raw):
            return await m.answer("❌ Invalid value (8–256 chars, no spaces). Try again.", reply_markup=kb([[cancel_btn("adm:ps")]]))
        await state.update_data(pending=enc(raw))
        shown = "••••••••" if f == "sec" else mask(raw)
        return await m.answer(
            f"⚠️ <b>Confirm change</b>\n\nSet {SETTING_FIELDS[f]} to <code>{esc(shown)}</code>?",
            reply_markup=kb([[btn("✅ Confirm", "adm:psy", "success")], [cancel_btn("adm:ps")]]),
        )
    cents = parse_money(raw)
    s = await get_pay_settings()
    if cents is None or (f == "min" and cents > s["max_cents"]) or (f == "max" and cents < s["min_cents"]):
        return await m.answer("❌ Invalid amount (min must not exceed max).", reply_markup=kb([[cancel_btn("adm:ps")]]))
    await patch_pay_settings(**{f"{f}_cents": cents})
    await state.clear()
    await settings_screen(m)


@admin_router.callback_query(F.data == "adm:psy", SettingsSt.value)
async def cb_settings_confirm(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if not d.get("pending"):
        return await alert(c, "Nothing to confirm.")
    f = d["f"]
    if f == "mid":
        await patch_pay_settings(merchant_id=dec(d["pending"]))
    elif f == "key":
        await patch_pay_settings(api_key_enc=d["pending"])
    elif f == "sec":
        await patch_pay_settings(api_secret_enc=d["pending"])
    log.info("Admin %s updated payment setting '%s'", c.from_user.id, f)
    await state.clear()
    await settings_screen(c)


# ── terms ──


@admin_router.callback_query(F.data == "adm:tm")
async def cb_terms_admin(c: CallbackQuery, state: FSMContext):
    terms = await get_setting("terms", config.TERMS_TEXT)
    await state.set_state(TermsSt.text)
    content = Text(Bold("📝 Terms (current)"), "\n\n", terms, "\n\n✏️ Send the new terms text (20–3500 chars) to replace it.")
    await show(c, content=content, markup=kb([[btn("♻️ Reset to default", "adm:tmr", "danger")], [cancel_btn("adm:cancel")]]))


@admin_router.message(TermsSt.text, F.text)
async def msg_terms(m: Message, state: FSMContext):
    t = m.text.strip()
    if not 20 <= len(t) <= 3500:
        return await m.answer("❌ Terms must be 20–3500 characters.", reply_markup=cancel_kb())
    await set_setting("terms", t)
    await state.clear()
    await m.answer("✅ Terms updated.", reply_markup=kb([[admin_back("adm:home", "⬅️ Admin Panel")]]))


@admin_router.callback_query(F.data == "adm:tmr")
async def cb_terms_reset(c: CallbackQuery, state: FSMContext):
    await set_setting("terms", config.TERMS_TEXT)
    await state.clear()
    await show(c, "✅ Terms reset to default.", kb([[admin_back("adm:home", "⬅️ Admin Panel")]]))


# ═══════════════════════════════ startup ═══════════════════════════════


async def main():
    global bot_ref
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if "REPLACE" in config.BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN in config.py (or the BOT_TOKEN environment variable).")
    await init_db()
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    bot_ref = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(UserMiddleware())
    dp.callback_query.outer_middleware(UserMiddleware())
    dp.include_router(admin_router)
    dp.include_router(user_router)

    worker = asyncio.create_task(background_worker())
    runner = None
    if config.WEBHOOK_PORT:
        app = web.Application()
        app.router.add_post(config.WEBHOOK_PATH, binance_webhook)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, config.WEBHOOK_HOST, config.WEBHOOK_PORT).start()
        log.info("Binance webhook receiver listening on %s:%s%s", config.WEBHOOK_HOST, config.WEBHOOK_PORT, config.WEBHOOK_PATH)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Bot started")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        worker.cancel()
        if runner:
            await runner.cleanup()
        if _http and not _http.closed:
            await _http.close()
        await bot.session.close()
        mongo.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
