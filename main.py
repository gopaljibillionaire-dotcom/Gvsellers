"""
Digital product store bot — Aiogram 3.x + MongoDB (Motor) + Binance Pay.

Features:
- Fresh GV & Aged GV product creation & stock management.
- Multi-field Fresh GV intake (Email, Password, Recovery Email, Number).
- Paginated catalog display (10 per row/page) with styled action buttons.
- Wallet balance purchase & Binance Pay crypto deposits.
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
PAGE_10 = 10  # Enforced 10 items per row/page for GV lists
MAX_PRICE_CENTS = 10_000_000  # $100,000

mongo: Any = None
db: Any = None
bot_ref: Optional[Bot] = None
_http: Optional[aiohttp.ClientSession] = None
_tasks: set = set()

# ════════════════════════════ HELPERS ════════════════════════════


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


# ════════════════════════════ KEYBOARDS & STYLING ════════════════════════════


def btn(text: str, cb: Optional[str] = None, style: Optional[str] = None, url: Optional[str] = None):
    kw: dict = {"text": text}
    if url:
        kw["url"] = url
    else:
        kw["callback_data"] = cb
    if style:
        kw["style"] = style  # 'success' = Green, 'danger' = Red, 'primary' = Neutral
    return InlineKeyboardButton(**kw)


def kb(rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back(cb: str = "home", text: str = "⬅️ Back"):
    return btn(text, cb, "danger")  # Red for all Back buttons


def cancel_btn(cb: str = "cancel"):
    return btn("❌ Cancel", cb, "danger")


def pager(prefix: str, page: int, pages: int) -> list:
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn("◀️ Prev", f"{prefix}:{page - 1}", "primary"))
    row.append(btn(f"Page {page + 1}/{pages}", "noop", "primary"))
    if page < pages - 1:
        row.append(btn("Next ▶️", f"{prefix}:{page + 1}", "primary"))
    return [row]


def main_menu(admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [btn("🛍 View Products", "pl:0", "success")],  # Green button for Buy Product / View
        [btn("💰 Wallet", "w", "success"), btn("📦 My Orders", "ol:0", "primary")],  # Green button for Wallet
        [btn("💬 Contact Support", "sup", "success"), btn("📜 Terms", "terms", "primary")],  # Green button for Support
    ]
    if admin:
        rows.append([btn("⚙️ Admin Panel", "adm:home", "primary")])
    return kb(rows)


async def show(ev, text: Optional[str] = None, markup=None, content: Optional[Text] = None, plain: bool = False):
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


async def page_query(coll, flt: dict, sort: list, page: int, size: int = PAGE_10):
    total = await coll.count_documents(flt)
    pages = max(1, -(-total // size))
    page = min(max(page, 0), pages - 1)
    docs = await coll.find(flt).sort(sort).skip(page * size).limit(size).to_list(size)
    return docs, page, pages, total


# ════════════════════════════ DATABASE ════════════════════════════


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
    await db.inventory.create_index("order_id", sparse=True)
    await db.orders.create_index("order_id", unique=True)
    await db.orders.create_index([("user_id", A), ("created_at", D)])
    await db.payments.create_index("payment_id", unique=True)
    await db.payments.create_index("merchant_trade_no", unique=True)
    await db.wallets.create_index("user_id", unique=True)
    await db.wallet_transactions.create_index([("user_id", A), ("created_at", D)])
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


# ═════════════════════════ BINANCE PAY ═════════════════════════


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
        "goods": {"goodsType": "02", "goodsCategory": "Z000", "referenceGoodsId": "wallet_deposit", "goodsName": "Wallet deposit"},
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


# ═══════════════════════════ PAYMENTS LOGIC ═══════════════════════════

PAY_LABEL = {
    "pending": "⏳ Pending", "crediting": "⏳ Processing", "completed": "✅ Completed",
    "expired": "⌛ Expired", "cancelled": "🚫 Cancelled", "failed": "❌ Failed", "mismatch": "⚠️ Mismatch",
}


async def create_deposit(user_id: int, cents: int, currency: str) -> dict:
    s = await get_pay_settings()
    if not binance_ready(s):
        raise BinanceError("Deposits are currently unavailable")
    if currency not in s["currencies"] or not (s["min_cents"] <= cents <= s["max_cents"]):
        raise BinanceError("Invalid amount or currency")
    payment_id = new_id("PAY", 8)
    amount = f"{Decimal(cents) / 100:.2f}"
    data = await binance_create(payment_id, amount, currency)
    t = now()
    doc = {
        "payment_id": payment_id,
        "merchant_trade_no": payment_id,
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
    }
    await db.payments.insert_one(doc)
    return doc


async def credit_deposit(pay: dict) -> bool:
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
    pay = await db.payments.find_one({"payment_id": payment_id})
    if not pay or pay["status"] != "pending":
        return pay, False
    try:
        data = await binance_query(pay["merchant_trade_no"])
    except BinanceError:
        return pay, False
    status = str(data.get("status", "")).upper()
    t = now()

    if status == "PAID":
        claimed = await db.payments.find_one_and_update(
            {"payment_id": payment_id, "status": "pending"},
            {"$set": {"status": "crediting", "verified_at": t}},
            return_document=ReturnDocument.AFTER,
        )
        if not claimed:
            return await db.payments.find_one({"payment_id": payment_id}), False
        credited = await credit_deposit(claimed)
        final = await db.payments.find_one({"payment_id": payment_id})
        if credited and notify:
            bal = await get_balance(claimed["user_id"])
            content = Text(
                CustomEmoji("💰", custom_emoji_id=config.WALLET_EMOJI_ID), " ",
                Bold("Deposit confirmed"), f"\n\n{money(claimed['amount_cents'])} was credited to your wallet.\nNew Balance: ", Bold(money(bal)),
            )
            await safe_send(claimed["user_id"], **content.as_kwargs())
        return final, credited

    if status == "EXPIRED" or t > pay["expires_at"]:
        await db.payments.update_one({"payment_id": payment_id, "status": "pending"}, {"$set": {"status": "expired", "verified_at": t}})
    return await db.payments.find_one({"payment_id": payment_id}), False


async def background_worker():
    while True:
        try:
            async for p in db.payments.find({"status": "pending"}).limit(100):
                await process_payment(p["payment_id"], notify=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("background worker error")
        await asyncio.sleep(config.PAYMENT_POLL_SECONDS)


async def binance_webhook(request: web.Request) -> web.Response:
    ok = web.json_response({"returnCode": "SUCCESS", "returnMessage": None})
    try:
        body = await request.json()
        data = body.get("data")
        if isinstance(data, str):
            data = json.loads(data)
        trade_no = (data or {}).get("merchantTradeNo") or body.get("bizIdStr")
    except Exception:
        return ok
    if isinstance(trade_no, str):
        spawn(process_payment(trade_no, notify=True))
    return ok


# ═══════════════════════════ INVENTORY & ORDERS ═══════════════════════════


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
        {"$set": {"status": "completed", "payment_status": "paid", "delivery_status": "delivered", "completed_at": t, "item_id": item["item_id"]}},
    )


async def purchase(user_id: int, item_id: str):
    """Purchase a specific item chosen from the Fresh/Aged GV list."""
    item = await db.inventory.find_one({"item_id": item_id, "status": "available"})
    if not item:
        return None, None, "This item is no longer available."

    cents = item["price_cents"]
    p = await db.products.find_one({"product_id": item["product_id"]})
    p_name = p["name"] if p else "GV Account"

    oid = new_id("ORD", 5)
    t = now()

    # Reserve item
    reserved = await db.inventory.find_one_and_update(
        {"item_id": item_id, "status": "available"},
        {"$set": {"status": "reserved", "order_id": oid, "buyer_id": user_id, "reserved_at": t}},
        return_document=ReturnDocument.AFTER,
    )
    if not reserved:
        return None, None, "Item was just snatched by another buyer."

    # Debit balance
    w = await db.wallets.find_one_and_update(
        {"user_id": user_id, "balance_cents": {"$gte": cents}},
        {"$inc": {"balance_cents": -cents}, "$set": {"updated_at": now()}}, return_document=ReturnDocument.AFTER,
    )
    if not w:
        await release_item(item_id)
        return None, None, f"Insufficient balance. You need {money(cents)}."

    await db.orders.insert_one({
        "order_id": oid, "user_id": user_id, "product_id": item["product_id"], "product_name": p_name,
        "amount_cents": cents, "status": "pending", "payment_status": "paid", "delivery_status": "pending",
        "created_at": t,
    })

    await db.wallet_transactions.insert_one({
        "tx_id": new_id("TX", 6), "user_id": user_id, "type": "purchase", "amount_cents": -cents,
        "ref_id": oid, "note": f"Purchase {p_name}", "applied": True, "balance_after_cents": w["balance_cents"], "created_at": now(),
    })

    await finalize_sale(oid, reserved)
    order = await db.orders.find_one({"order_id": oid})
    return order, reserved, None


def delivery_block(item: dict) -> str:
    """Format and present full credential details upon successful purchase."""
    dt = item.get("details", {})
    if dt:
        return (
            "📧 <b>Email:</b> <code>" + esc(dec(dt.get("email_enc", ""))) + "</code>\n"
            "🔑 <b>Password:</b> <code>" + esc(dec(dt.get("pass_enc", ""))) + "</code>\n"
            "🔄 <b>Recovery Email:</b> <code>" + esc(dec(dt.get("rec_enc", ""))) + "</code>\n"
            "📞 <b>Phone Number:</b> <code>" + esc(dec(dt.get("num_enc", ""))) + "</code>"
        )
    code = dec(item.get("code_enc", ""))
    return f"🔑 <b>Account Details:</b>\n<code>{esc(code)}</code>"


# ═══════════════════════ MIDDLEWARE, FILTERS & STATES ═══════════════════════


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
        data["db_user"] = doc
        return await handler(event, data)


class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        u = getattr(event, "from_user", None)
        return bool(u and u.id in ADMIN_SET)


class AddStockFreshGV(StatesGroup):
    email = State()
    password = State()
    rec_email = State()
    number = State()
    price = State()


class AddStockAgedGV(StatesGroup):
    raw_details = State()
    price = State()


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

# ═══════════════════════════════ USER BOT HANDLERS ═══════════════════════════════


@user_router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    welcome = (
        f"🛍 <b>Welcome to {esc(config.STORE_NAME)}</b>\n\n"
        "Your premium hub for Google Voice (GV) accounts — Fresh & Aged options available. "
        "Select an option below to buy or manage your balance."
    )
    await m.answer(welcome, reply_markup=main_menu(m.from_user.id in ADMIN_SET))


@user_router.callback_query(F.data == "home")
@user_router.callback_query(F.data == "cancel")
async def cb_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    welcome = (
        f"🛍 <b>Welcome to {esc(config.STORE_NAME)}</b>\n\n"
        "Your premium hub for Google Voice (GV) accounts — Fresh & Aged options available."
    )
    await show(c, welcome, main_menu(c.from_user.id in ADMIN_SET))


@user_router.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


# ── Products Catalog & Selection ──


@user_router.callback_query(F.data.startswith("pl:"))
async def cb_products_list(c: CallbackQuery):
    """List Fresh vs Aged GV categories."""
    fresh_count = await db.inventory.count_documents({"gv_type": "fresh", "status": "available"})
    aged_count = await db.inventory.count_documents({"gv_type": "aged", "status": "available"})

    rows = [
        [btn(f"🟢 Fresh GV (Stock: {fresh_count})", "gvl:fresh:0", "success")],
        [btn(f"📜 Aged GV (Stock: {aged_count})", "gvl:aged:0", "primary")],
        [back()],
    ]
    await show(c, "🛍 <b>Select Google Voice (GV) Type:</b>", kb(rows))


@user_router.callback_query(F.data.startswith("gvl:"))
async def cb_gv_list(c: CallbackQuery):
    """Lists stock items in rows of 10 with pagination."""
    _, gv_type, page_str = c.data.split(":")
    page = to_int(page_str)

    docs, page, pages, total = await page_query(
        db.inventory,
        {"gv_type": gv_type, "status": "available"},
        [("created_at", -1)],
        page,
        size=PAGE_10,
    )

    if not total:
        return await show(c, f"❌ No available stock for <b>{gv_type.upper()} GV</b> at the moment.", kb([[back("pl:0")]]))

    title = "🟢 Fresh GV" if gv_type == "fresh" else "📜 Aged GV"
    msg_text = f"🛍 <b>{title} Stock List</b>\n\nShowing items {page * 10 + 1}–{min((page + 1) * 10, total)} of <b>{total}</b>:\nClick any item below to view price & buy."

    rows = []
    # Display 10 per page dynamically
    for idx, item in enumerate(docs, start=1 + (page * 10)):
        item_lbl = f"GV #{idx} — {money(item['price_cents'])}"
        rows.append([btn(item_lbl, f"gvi:{item['item_id']}", "success")])

    rows += pager(f"gvl:{gv_type}", page, pages)
    rows.append([back("pl:0")])

    await show(c, msg_text, kb(rows))


@user_router.callback_query(F.data.startswith("gvi:"))
async def cb_gv_item_view(c: CallbackQuery):
    """Inspect a single item before purchasing."""
    item_id = c.data.split(":")[1]
    item = await db.inventory.find_one({"item_id": item_id, "status": "available"})
    if not item:
        return await alert(c, "Item sold out or unavailable.")

    bal = await get_balance(c.from_user.id)
    cents = item["price_cents"]
    gv_type = item.get("gv_type", "fresh").upper()

    text = (
        f"🛍 <b>Item Details ({gv_type} GV)</b>\n\n"
        f"Price: <b>{money(cents)}</b>\n"
        f"Your Wallet Balance: <b>{money(bal)}</b>\n\n"
        "<i>Details will be revealed instantly upon payment.</i>"
    )

    rows = []
    if bal >= cents:
        rows.append([btn(f"💳 Deduct {money(cents)} & Buy", f"buygv:{item_id}", "success")])
    else:
        text += f"\n\n⚠️ You need <b>{money(cents - bal)}</b> more balance to complete this purchase."
        rows.append([btn("➕ Top Up Balance", "wd", "success")])

    rows.append([back(f"gvl:{item.get('gv_type', 'fresh')}:0")])
    await show(c, text, kb(rows))


@user_router.callback_query(F.data.startswith("buygv:"))
async def cb_buy_gv(c: CallbackQuery):
    """Processes payment and delivers details."""
    item_id = c.data.split(":")[1]
    await c.answer("Processing transaction...")

    order, item, err = await purchase(c.from_user.id, item_id)
    if err:
        return await show(c, f"❌ {err}", kb([[back("pl:0")]]))

    text = (
        f"✅ <b>Purchase Successful!</b>\n\n"
        f"<b>Order ID:</b> <code>{order['order_id']}</code>\n"
        f"<b>Amount Paid:</b> {money(order['amount_cents'])}\n\n"
        f"<b>Delivered Account Details:</b>\n"
        f"{delivery_block(item)}"
    )

    rows = [[btn("📦 My Orders", "ol:0", "primary"), btn("🏠 Home", "home", "primary")]]
    await show(c, text, kb(rows))


# ── Orders View ──


@user_router.callback_query(F.data.startswith("ol:"))
async def cb_orders(c: CallbackQuery):
    page = to_int(c.data.split(":")[1])
    docs, page, pages, total = await page_query(db.orders, {"user_id": c.from_user.id}, [("created_at", -1)], page, size=PAGE_10)
    if not total:
        return await show(c, "📦 <b>My Orders</b>\n\nYou have no order history.", kb([[back()]]))

    rows = [[btn(f"{o['order_id']} · {o['product_name']} · {money(o['amount_cents'])}", f"ov:{o['order_id']}", "primary")] for o in docs]
    rows += pager("ol", page, pages)
    rows.append([back()])
    await show(c, f"📦 <b>My Orders</b> (Total: {total})", kb(rows))


@user_router.callback_query(F.data.startswith("ov:"))
async def cb_order_view(c: CallbackQuery):
    oid = c.data.split(":")[1]
    o = await db.orders.find_one({"order_id": oid, "user_id": c.from_user.id})
    if not o:
        return await alert(c, "Order not found.")

    text = (
        f"📦 <b>Order Details:</b> <code>{o['order_id']}</code>\n\n"
        f"Product: <b>{esc(o['product_name'])}</b>\n"
        f"Amount Paid: <b>{money(o['amount_cents'])}</b>\n"
        f"Date: {fmt_dt(o['created_at'])}\n\n"
    )

    item = await db.inventory.find_one({"item_id": o.get("item_id")})
    if item:
        text += delivery_block(item)

    await show(c, text, kb([[back("ol:0")]]))


# ── Wallet Operations ──


@user_router.callback_query(F.data == "w")
async def cb_wallet(c: CallbackQuery, state: FSMContext):
    await state.clear()
    bal = await get_balance(c.from_user.id)
    content = Text(
        CustomEmoji("💰", custom_emoji_id=config.WALLET_EMOJI_ID), " ", Bold("Wallet Management"),
        "\n\nCurrent Available Balance: ", Bold(money(bal)),
    )
    await show(c, content=content, markup=kb([
        [btn("➕ Top Up Balance", "wd", "success"), btn("📜 Transactions", "wt:0", "primary")],
        [back()],
    ]))


@user_router.callback_query(F.data.startswith("wt:"))
async def cb_transactions(c: CallbackQuery):
    page = to_int(c.data.split(":")[1])
    docs, page, pages, total = await page_query(db.wallet_transactions, {"user_id": c.from_user.id}, [("created_at", -1)], page, size=PAGE_10)
    if not total:
        text = "📜 <b>Transaction History</b>\n\nNo records found."
    else:
        lines = []
        for t in docs:
            sign = "+" if t["amount_cents"] >= 0 else "−"
            lines.append(f"{sign}{money(abs(t['amount_cents']))} · {esc(t['type'].title())} · {fmt_dt(t['created_at'])}")
        text = "📜 <b>Transaction History</b>\n\n" + "\n".join(lines)
    await show(c, text, kb(pager("wt", page, pages) + [[back("w")]]))


@user_router.callback_query(F.data == "wd")
async def cb_deposit(c: CallbackQuery, state: FSMContext):
    await state.clear()
    s = await get_pay_settings()
    if not binance_ready(s):
        return await alert(c, "Deposits are temporarily disabled.")

    presets = [x for x in (500, 1000, 2000, 5000) if s["min_cents"] <= x <= s["max_cents"]]
    rows = []
    if presets:
        rows.append([btn(money(x), f"wda:{x}", "success") for x in presets])
    rows.append([btn("✏️ Custom Amount", "wdc", "primary")])
    rows.append([back("w")])
    await show(c, f"➕ <b>Top Up Balance (Binance Pay)</b>\n\nMin Deposit: {money(s['min_cents'])}\nMax Deposit: {money(s['max_cents'])}", kb(rows))


@user_router.callback_query(F.data == "wdc")
async def cb_deposit_custom(c: CallbackQuery, state: FSMContext):
    await state.set_state(DepositSt.amount)
    await show(c, "✏️ Enter deposit amount in USD (e.g. <code>10</code> or <code>15.50</code>):", kb([[cancel_btn("w")]]))


@user_router.message(DepositSt.amount, F.text)
async def msg_deposit_amount(m: Message, state: FSMContext):
    cents = parse_money(m.text)
    if cents is None:
        return await m.answer("❌ Invalid format. Please enter a valid number (e.g. <code>10.50</code>).", reply_markup=kb([[cancel_btn("w")]]))
    await state.clear()
    await deposit_step(m, cents)


async def deposit_step(ev, cents: int):
    s = await get_pay_settings()
    if not (s["min_cents"] <= cents <= s["max_cents"]):
        return await show(ev, f"Amount must be between {money(s['min_cents'])} and {money(s['max_cents'])}.", kb([[back("wd")]]))
    cur = s["currencies"][0] if s["currencies"] else "USDT"
    pay = await create_deposit(ev.from_user.id, cents, cur)

    text = (
        f"💳 <b>Binance Pay Invoice</b>\n\n"
        f"Invoice ID: <code>{pay['payment_id']}</code>\n"
        f"Amount Due: <b>{pay['amount']} {pay['currency']}</b>\n\n"
        "Click the button below to complete payment via Binance Pay:"
    )
    rows = []
    if pay.get("checkout_url"):
        rows.append([btn("💳 Open Binance Pay", url=pay["checkout_url"], style="success")])
    rows.append([btn("✅ Verify Payment", f"wv:{pay['payment_id']}", "success")])
    rows.append([back("w")])
    await show(ev, text, kb(rows))


@user_router.callback_query(F.data.startswith("wda:"))
async def cb_deposit_preset(c: CallbackQuery):
    cents = to_int(c.data.split(":")[1])
    await deposit_step(c, cents)


@user_router.callback_query(F.data.startswith("wv:"))
async def cb_verify_deposit(c: CallbackQuery):
    pid = c.data.split(":")[1]
    pay, credited = await process_payment(pid)
    if pay and pay["status"] == "completed":
        bal = await get_balance(c.from_user.id)
        await show(c, f"✅ Payment confirmed! Balance updated to <b>{money(bal)}</b>.", kb([[back("w")]]))
    else:
        await alert(c, "Payment not found or still pending in Binance Pay.")


# ── Support & Terms ──


@user_router.callback_query(F.data == "sup")
async def cb_support(c: CallbackQuery, state: FSMContext):
    await state.clear()
    content = Text(
        CustomEmoji("💬", custom_emoji_id=config.SUPPORT_EMOJI_ID), " ", Bold("Customer Support"),
        "\n\nNeed assistance? Reach out to support directly or send a message below.",
    )
    await show(c, content=content, markup=kb([
        [btn("💬 Contact Support", url=f"https://t.me/{config.SUPPORT_USERNAME.lstrip('@')}", style="success")],
        [btn("📨 Direct Message", "supm", "primary")],
        [back()],
    ]))


@user_router.callback_query(F.data == "supm")
async def cb_support_msg(c: CallbackQuery, state: FSMContext):
    await state.set_state(SupportSt.msg)
    await show(c, "📨 Send your message for support:", kb([[cancel_btn("sup")]]))


@user_router.message(SupportSt.msg, F.text)
async def msg_support(m: Message, state: FSMContext):
    await state.clear()
    body = f"💬 Support message from @{m.from_user.username or 'NoUser'} (ID: {m.from_user.id}):\n\n{m.text}"
    await notify_admins(body)
    await m.answer("✅ Message sent to support team.", reply_markup=kb([[back()]]))


@user_router.callback_query(F.data == "terms")
async def cb_terms(c: CallbackQuery):
    terms = await get_setting("terms", config.TERMS_TEXT)
    await show(c, content=Text(Bold("📜 Terms & Conditions"), "\n\n", terms), markup=kb([[back()]]))


# ═══════════════════════════════ ADMIN PANEL HANDLERS ═══════════════════════════════


def admin_menu() -> InlineKeyboardMarkup:
    """Exact admin structure as originally defined, preserving standard style colors."""
    return kb([
        [btn("➕ Add Stock", "adm:add_choice", "success"), btn("📦 Active Stock", "adm:ai:0", "primary")],
        [btn("🛒 Sold Stock", "adm:ss:0", "primary"), btn("🛍 Products", "adm:pr:0", "primary")],
        [btn("👥 Users", "adm:us:0", "primary"), btn("💰 Payments", "adm:py:0", "primary")],
        [btn("📊 Statistics", "adm:st", "primary"), btn("💳 Payment Settings", "adm:ps", "primary")],
        [btn("📝 Terms", "adm:tm", "primary")],
        [back("home")],
    ])


@admin_router.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("⚙️ <b>Admin Control Panel</b>", reply_markup=admin_menu())


@admin_router.callback_query(F.data.in_({"adm:home", "adm:cancel"}))
async def cb_admin_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, "⚙️ <b>Admin Control Panel</b>", admin_menu())


# ── Add Stock Flow: Fresh GV vs Aged GV ──


@admin_router.callback_query(F.data == "adm:add_choice")
async def cb_add_stock_choice(c: CallbackQuery, state: FSMContext):
    """Presents choice: Fresh GV or Aged GV."""
    await state.clear()
    rows = [
        [btn("🟢 Fresh GV", "adm:add_fresh", "success")],
        [btn("📜 Aged GV", "adm:add_aged", "primary")],
        [back("adm:home")],
    ]
    await show(c, "➕ <b>Select Stock Type to Add:</b>", kb(rows))


# --- FRESH GV INTAKE FLOW ---


@admin_router.callback_query(F.data == "adm:add_fresh")
async def cb_add_fresh_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddStockFreshGV.email)
    await show(c, "➕ <b>Adding Fresh GV Account</b>\n\n1️⃣ Please enter the <b>Email</b>:", kb([[cancel_btn("adm:home")]]))


@admin_router.message(AddStockFreshGV.email, F.text)
async def msg_fresh_email(m: Message, state: FSMContext):
    await state.update_data(email=m.text.strip())
    await state.set_state(AddStockFreshGV.password)
    await m.answer("2️⃣ Please enter the <b>Password</b>:", reply_markup=kb([[cancel_btn("adm:home")]]))


@admin_router.message(AddStockFreshGV.password, F.text)
async def msg_fresh_pass(m: Message, state: FSMContext):
    await state.update_data(password=m.text.strip())
    await state.set_state(AddStockFreshGV.rec_email)
    await m.answer("3️⃣ Please enter the <b>Recovery Email</b>:", reply_markup=kb([[cancel_btn("adm:home")]]))


@admin_router.message(AddStockFreshGV.rec_email, F.text)
async def msg_fresh_rec(m: Message, state: FSMContext):
    await state.update_data(rec_email=m.text.strip())
    await state.set_state(AddStockFreshGV.number)
    await m.answer("4️⃣ Please enter the <b>Phone Number</b>:", reply_markup=kb([[cancel_btn("adm:home")]]))


@admin_router.message(AddStockFreshGV.number, F.text)
async def msg_fresh_num(m: Message, state: FSMContext):
    await state.update_data(number=m.text.strip())
    await state.set_state(AddStockFreshGV.price)
    await m.answer("5️⃣ Enter the <b>Price in USD</b> (e.g. <code>5.00</code>):", reply_markup=kb([[cancel_btn("adm:home")]]))


@admin_router.message(AddStockFreshGV.price, F.text)
async def msg_fresh_price(m: Message, state: FSMContext):
    cents = parse_money(m.text)
    if cents is None:
        return await m.answer("❌ Invalid price format. Enter a number like <code>5.00</code>.", reply_markup=kb([[cancel_btn("adm:home")]]))

    d = await state.get_data()
    await state.clear()

    item_id = new_id("GVF", 6)
    # Encrypt all sensitive account details
    doc = {
        "item_id": item_id,
        "product_id": "fresh_gv_prod",
        "gv_type": "fresh",
        "price_cents": cents,
        "details": {
            "email_enc": enc(d["email"]),
            "pass_enc": enc(d["password"]),
            "rec_enc": enc(d["rec_email"]),
            "num_enc": enc(d["number"]),
        },
        "status": "available",
        "created_at": now(),
        "added_by": m.from_user.id,
    }

    await db.inventory.insert_one(doc)

    # Ensure container product entry exists
    await db.products.update_one(
        {"product_id": "fresh_gv_prod"},
        {"$setOnInsert": {"name": "Fresh Google Voice", "category": "Fresh GV", "enabled": True, "deleted": False}},
        upsert=True,
    )

    text = f"✅ <b>Fresh GV Account Listed!</b>\n\nPrice: {money(cents)}\nEmail: <code>{esc(d['email'])}</code>"
    await m.answer(text, reply_markup=kb([[btn("➕ Add Another Fresh GV", "adm:add_fresh", "success")], [back("adm:home")]]))


# --- AGED GV INTAKE FLOW ---


@admin_router.callback_query(F.data == "adm:add_aged")
async def cb_add_aged_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddStockAgedGV.raw_details)
    await show(c, "➕ <b>Adding Aged GV Account</b>\n\nSend full account details string/credentials:", kb([[cancel_btn("adm:home")]]))


@admin_router.message(AddStockAgedGV.raw_details, F.text)
async def msg_aged_details(m: Message, state: FSMContext):
    await state.update_data(code=m.text.strip())
    await state.set_state(AddStockAgedGV.price)
    await m.answer("Enter the <b>Price in USD</b> (e.g. <code>10.00</code>):", reply_markup=kb([[cancel_btn("adm:home")]]))


@admin_router.message(AddStockAgedGV.price, F.text)
async def msg_aged_price(m: Message, state: FSMContext):
    cents = parse_money(m.text)
    if cents is None:
        return await m.answer("❌ Invalid price format. Enter a number like <code>10.00</code>.", reply_markup=kb([[cancel_btn("adm:home")]]))

    d = await state.get_data()
    await state.clear()

    item_id = new_id("GVA", 6)
    doc = {
        "item_id": item_id,
        "product_id": "aged_gv_prod",
        "gv_type": "aged",
        "price_cents": cents,
        "code_enc": enc(d["code"]),
        "status": "available",
        "created_at": now(),
        "added_by": m.from_user.id,
    }

    await db.inventory.insert_one(doc)

    await db.products.update_one(
        {"product_id": "aged_gv_prod"},
        {"$setOnInsert": {"name": "Aged Google Voice", "category": "Aged GV", "enabled": True, "deleted": False}},
        upsert=True,
    )

    await m.answer(f"✅ <b>Aged GV Account Listed!</b>\nPrice: {money(cents)}", reply_markup=kb([[btn("➕ Add Another Aged GV", "adm:add_aged", "success")], [back("adm:home")]]))


# ── Active / Sold Stock & Other Admin Modules ──


@admin_router.callback_query(F.data.startswith("adm:ai:"))
async def cb_active_stock(c: CallbackQuery):
    page = to_int(c.data.split(":")[2])
    docs, page, pages, total = await page_query(db.inventory, {"status": "available"}, [("created_at", -1)], page, size=PAGE_10)
    rows = [[btn(f"{i['item_id']} · {i.get('gv_type', 'gv').upper()} · {money(i['price_cents'])}", f"adm:ii:{i['item_id']}", "primary")] for i in docs]
    rows += pager("adm:ai", page, pages)
    rows.append([back("adm:home")])
    await show(c, f"📦 <b>Active Inventory List</b> ({total} items):", kb(rows))


@admin_router.callback_query(F.data.startswith("adm:ii:"))
async def cb_active_item_view(c: CallbackQuery):
    item_id = c.data.split(":")[2]
    item = await db.inventory.find_one({"item_id": item_id})
    if not item:
        return await alert(c, "Item not found.")

    text = f"📦 <b>Item:</b> <code>{item['item_id']}</code>\nType: {item.get('gv_type', 'N/A').upper()}\nPrice: {money(item['price_cents'])}\nStatus: {item['status']}"
    await show(c, text, kb([[btn("🗑 Delete Item", f"adm:id:{item_id}", "danger")], [back("adm:ai:0")]]))


@admin_router.callback_query(F.data.startswith("adm:id:"))
async def cb_delete_item(c: CallbackQuery):
    item_id = c.data.split(":")[2]
    await db.inventory.delete_one({"item_id": item_id})
    await show(c, "✅ Item deleted from stock.", kb([[back("adm:ai:0")]]))


@admin_router.callback_query(F.data.startswith("adm:ss:"))
async def cb_sold_stock(c: CallbackQuery):
    page = to_int(c.data.split(":")[2])
    docs, page, pages, total = await page_query(db.orders, {"status": "completed"}, [("completed_at", -1)], page, size=PAGE_10)
    rows = [[btn(f"{o['order_id']} · {money(o['amount_cents'])}", f"adm:so:{o['order_id']}", "primary")] for o in docs]
    rows += pager("adm:ss", page, pages)
    rows.append([back("adm:home")])
    await show(c, f"🛒 <b>Sold Stock Items</b> ({total}):", kb(rows))


@admin_router.callback_query(F.data.startswith("adm:us:"))
async def cb_admin_users(c: CallbackQuery):
    page = to_int(c.data.split(":")[2])
    docs, page, pages, total = await page_query(db.users, {}, [("created_at", -1)], page, size=PAGE_10)
    rows = [[btn(f"User {u['user_id']}", "noop", "primary")] for u in docs]
    rows += pager("adm:us", page, pages)
    rows.append([back("adm:home")])
    await show(c, f"👥 <b>Total Registered Users:</b> {total}", kb(rows))


@admin_router.callback_query(F.data == "adm:st")
async def cb_admin_stats(c: CallbackQuery):
    users = await db.users.count_documents({})
    active = await db.inventory.count_documents({"status": "available"})
    sold = await db.inventory.count_documents({"status": "sold"})
    text = f"📊 <b>Store Analytics</b>\n\n👥 Users: <b>{users}</b>\n📦 Active Inventory: <b>{active}</b>\n🛒 Total Items Sold: <b>{sold}</b>"
    await show(c, text, kb([[back("adm:home")]]))


@admin_router.callback_query(F.data == "adm:ps")
async def cb_admin_pay_settings(c: CallbackQuery):
    s = await get_pay_settings()
    text = f"💳 <b>Payment Settings</b>\n\nBinance Pay Status: {'✅ Active' if s['enabled'] else '🚫 Disabled'}"
    await show(c, text, kb([[back("adm:home")]]))


@admin_router.callback_query(F.data == "adm:tm")
async def cb_admin_terms(c: CallbackQuery, state: FSMContext):
    terms = await get_setting("terms", config.TERMS_TEXT)
    await state.set_state(TermsSt.text)
    await show(c, f"📝 <b>Edit Store Terms</b>\n\nCurrent terms:\n<i>{terms}</i>\n\nSend new terms text:", kb([[cancel_btn("adm:home")]]))


@admin_router.message(TermsSt.text, F.text)
async def msg_terms_update(m: Message, state: FSMContext):
    await set_setting("terms", m.text.strip())
    await state.clear()
    await m.answer("✅ Terms updated successfully.", reply_markup=kb([[back("adm:home")]]))


# ═══════════════════════════════ APPLICATION ENTRY ═══════════════════════════════


async def main():
    global bot_ref
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if "REPLACE" in config.BOT_TOKEN:
        raise SystemExit("Please configure BOT_TOKEN in config.py or set environment variables.")

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

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Bot successfully initialized & polling started.")
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
