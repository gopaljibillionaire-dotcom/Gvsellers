"""
Digital product store bot — Aiogram 3.x + MongoDB (Motor) + Crypto API Payments.
Includes complete inventory deletion (Single & Bulk Delete) + Data Usage Tracking + Dynamic Banner Images + Quantity Selection + Full Database Purge + Auto-Space-Split Parsing.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import logging
import re
import secrets
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

import aiohttp
import psutil
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from aiogram.utils.formatting import Bold, CustomEmoji, Text
from cryptography.fernet import Fernet, InvalidToken
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, ReturnDocument

import config

log = logging.getLogger("store")
ADMIN_SET = set(config.ADMIN_IDS)
PAGE_10 = 10
MAX_PRICE_CENTS = 10_000_000

mongo: Any = None
db: Any = None
bot_ref: Optional[Bot] = None

NEW_GV_PRICE_CENTS = 400  # $4.00
OLD_GV_PRICE_CENTS = 600  # $6.00

# Image Banners
IMG_WELCOME = "https://i.ibb.co/3mMm5pk8/file-00000000304481fabc208d5a014f5b11.png"
IMG_BUY_GV = "https://i.ibb.co/qFBDtRMT/file-00000000543c821195d09ed80ad42f1c.png"
IMG_WALLET = "https://i.ibb.co/xKhG0g3D/file-00000000b8b48211b02df47384536e56.png"
IMG_ORDERS = "https://i.ibb.co/Z6NpMbWG/file-0000000056e081fa90ef2c05289f9691.png"
IMG_SUPPORT = "https://i.ibb.co/Bxy6JP8/file-0000000024bc8210a4acf5976393bad9.png"
IMG_TERMS = "https://i.ibb.co/Q3R5YqjS/file-00000000910c8211957902711cb364f9.png"


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


def fmt_bytes(bytes_num: int) -> str:
    val = float(bytes_num)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if val < 1024.0:
            return f"{val:.2f} {unit}"
        val /= 1024.0
    return f"{val:.2f} PB"


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


def parse_gv_lines(text: str) -> list[dict[str, str]]:
    """
    Parses account text inputs. If input contains multiple credentials separated by double spaces
    or distinct newline entries, splits them into individual standalone GV account items.
    """
    accounts = []
    text_clean = text.strip()
    if not text_clean:
        return accounts

    lines = [line.strip() for line in text_clean.splitlines() if line.strip()]

    for line in lines:
        # Check if line contains double spaces or multispaces separating multiple GV credentials
        if "  " in line:
            parts = [p.strip() for p in re.split(r"\s{2,}", line) if p.strip()]
            for p in parts:
                accounts.append({"raw_text": p})
        # Check if line has single-space separated multi-account chunks (e.g., email pass email recovery phone)
        elif len(line.split()) >= 10:
            # Fallback split on double spaces or tab separators
            parts = [p.strip() for p in re.split(r"\s\s+|\t+", line) if p.strip()]
            if len(parts) > 1:
                for p in parts:
                    accounts.append({"raw_text": p})
            else:
                accounts.append({"raw_text": line})
        else:
            accounts.append({"raw_text": line})

    return accounts


async def fetch_crypto_price(coin_id: str) -> Optional[float]:
    try:
        async with aiohttp.ClientSession() as session:
            params = {"ids": coin_id, "vs_currencies": "usd"}
            async with session.get(config.PRICE_API_URL, params=params, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get(coin_id, {}).get("usd")
    except Exception as e:
        log.error("Error fetching crypto price: %s", e)
    return None


async def get_active_wallets() -> dict:
    wallets = await get_setting("wallets", config.WALLETS)
    return wallets


# ════════════════════════════ KEYBOARDS & STYLING ════════════════════════════


def btn(text: str, cb: Optional[str] = None, style: Optional[str] = None, url: Optional[str] = None):
    kw: dict = {"text": text}
    if url:
        kw["url"] = url
    else:
        kw["callback_data"] = cb
    if style:
        kw["style"] = style
    return InlineKeyboardButton(**kw)


def kb(rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back(cb: str = "home", text: str = "⬅️ Back to Menu"):
    return btn(text, cb, "danger")


def cancel_btn(cb: str = "cancel"):
    return btn("❌ Cancel", cb, "danger")


def pager(prefix: str, page: int, pages: int) -> list:
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(btn("◀ Prev", f"{prefix}:{page - 1}", "primary"))
    row.append(btn(f"Page {page + 1}/{pages}", "noop", "primary"))
    if page < pages - 1:
        row.append(btn("Next ▶️", f"{prefix}:{page + 1}", "primary"))
    return [row]


def main_menu(admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [btn("🛍 Buy Google Voice", "pl:0", "success")],
        [btn("💰 Wallet", "w", "success"), btn("📦 My Orders", "ol:0", "primary")],
        [btn("💬 Contact Support", "sup", "success"), btn("📜 Terms", "terms", "primary")],
    ]
    if admin:
        rows.append([btn("⚙️ Admin Panel", "adm:home", "primary")])
    return kb(rows)


async def show(ev, text: Optional[str] = None, markup=None, photo_url: Optional[str] = None):
    """Renders text and edits or sends photo banners smoothly in the exact same message."""
    kwargs = {"caption": text, "reply_markup": markup, "parse_mode": ParseMode.HTML}

    if isinstance(ev, CallbackQuery):
        try:
            if photo_url:
                media = InputMediaPhoto(media=photo_url, caption=text, parse_mode=ParseMode.HTML)
                await ev.message.edit_media(media=media, reply_markup=markup)
            else:
                await ev.message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                if photo_url:
                    await ev.message.answer_photo(photo=photo_url, **kwargs)
                else:
                    await ev.message.answer(text, reply_markup=markup, parse_mode=ParseMode.HTML)
        try:
            await ev.answer()
        except TelegramAPIError:
            pass
    else:
        if photo_url:
            await ev.answer_photo(photo=photo_url, **kwargs)
        else:
            await ev.answer(text, reply_markup=markup, parse_mode=ParseMode.HTML)


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


async def notify_admins(text: str = None, content: Optional[Text] = None, markup=None):
    kw = content.as_kwargs() if content else {"text": text}
    if markup:
        kw["reply_markup"] = markup
    for aid in ADMIN_SET:
        await safe_send(aid, **kw)


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
    await db.inventory.create_index("item_id", unique=True)
    await db.inventory.create_index([("status", A), ("gv_type", A)])
    await db.orders.create_index("order_id", unique=True)
    await db.orders.create_index([("user_id", A), ("created_at", D)])
    await db.payments.create_index("payment_id", unique=True)

    try:
        await db.payments.drop_index("merchant_trade_no_1")
    except Exception:
        pass

    await db.wallets.create_index("user_id", unique=True)
    await db.wallet_transactions.create_index([("user_id", A), ("created_at", D)])
    await db.settings.create_index("key", unique=True)


async def get_setting(key: str, default: Any = None) -> Any:
    doc = await db.settings.find_one({"key": key})
    return doc["value"] if doc and "value" in doc else default


async def set_setting(key: str, value: Any):
    await db.settings.update_one({"key": key}, {"$set": {"value": value, "updated_at": now()}}, upsert=True)


async def get_balance(user_id: int) -> int:
    w = await db.wallets.find_one({"user_id": user_id})
    return w["balance_cents"] if w else 0


# ═══════════════════════════ INVENTORY & PURCHASES ═══════════════════════════


async def bulk_purchase(user_id: int, gv_type: str, qty: int, user_info: str):
    unit_price = NEW_GV_PRICE_CENTS if gv_type == "new" else OLD_GV_PRICE_CENTS
    total_cents = unit_price * qty
    gv_title = "New GV" if gv_type == "new" else "Old GV"
    t = now()

    # Step 1: Check available items
    available_items = await db.inventory.find(
        {"gv_type": gv_type, "status": "available"}
    ).limit(qty).to_list(qty)

    if len(available_items) < qty:
        return None, None, f"Insufficient stock available. Only {len(available_items)} available."

    item_ids = [item["item_id"] for item in available_items]

    # Step 2: Check and deduct wallet balance
    w = await db.wallets.find_one_and_update(
        {"user_id": user_id, "balance_cents": {"$gte": total_cents}},
        {"$inc": {"balance_cents": -total_cents}, "$set": {"updated_at": t}},
        return_document=ReturnDocument.AFTER,
    )
    if not w:
        return None, None, f"Insufficient balance. You need {money(total_cents)}."

    # Step 3: Reserve and update stock status to sold
    oid = new_id("ORD", 5)
    await db.inventory.update_many(
        {"item_id": {"$in": item_ids}},
        {"$set": {"status": "sold", "order_id": oid, "buyer_id": user_id, "sold_at": t}}
    )

    order = {
        "order_id": oid,
        "user_id": user_id,
        "product_name": f"{gv_title} x{qty}",
        "amount_cents": total_cents,
        "quantity": qty,
        "item_ids": item_ids,
        "status": "completed",
        "created_at": t,
    }
    await db.orders.insert_one(order)

    await db.wallet_transactions.insert_one({
        "tx_id": new_id("TX", 6),
        "user_id": user_id,
        "type": "purchase",
        "amount_cents": -total_cents,
        "ref_id": oid,
        "note": f"Purchased {qty}x {gv_title}",
        "applied": True,
        "balance_after_cents": w["balance_cents"],
        "created_at": t,
    })

    admin_alert = Text(
        CustomEmoji("🛍", custom_emoji_id=config.STORE_EMOJI_ID), " ", Bold("New Product Purchase!"), "\n\n",
        f"<b>Order ID:</b> <code>{oid}</code>\n",
        f"<b>Buyer:</b> {user_info} (ID: <code>{user_id}</code>)\n",
        f"<b>Product:</b> {gv_title} x{qty}\n",
        f"<b>Price Paid:</b> {money(total_cents)}\n",
        f"<b>Remaining User Balance:</b> {money(w['balance_cents'])}"
    )
    await notify_admins(content=admin_alert)

    return order, available_items, None


def delivery_block(item: dict) -> str:
    raw_enc = item.get("raw_text_enc")
    if raw_enc:
        content = dec(raw_enc)
    else:
        dt = item.get("details", {})
        if dt:
            email = dec(dt.get("email_enc", ""))
            password = dec(dt.get("pass_enc", ""))
            rec_email = dec(dt.get("rec_enc", ""))
            two_fa = dec(dt.get("two_fa_enc", ""))
            phone_num = dec(dt.get("num_enc", ""))
            lines = [email, password, rec_email]
            if two_fa and two_fa != "N/A":
                lines.append(two_fa)
            lines.append(phone_num)
            content = "\n".join([line for line in lines if line])
        else:
            content = dec(item.get("code_enc", ""))

    return f"<code>{esc(content)}</code>"


# ═══════════════════════ MIDDLEWARES & STATES ═══════════════════════


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


class TopUpSt(StatesGroup):
    amount = State()
    currency = State()
    txn_id = State()
    proof_photo = State()


class BuyGVSt(StatesGroup):
    custom_qty = State()


class AdminWalletSt(StatesGroup):
    currency_key = State()
    address = State()


class BulkAddStockSt(StatesGroup):
    gv_type = State()
    raw_data = State()


class TermsSt(StatesGroup):
    text = State()


class SupportSt(StatesGroup):
    msg = State()


user_router = Router(name="user")
admin_router = Router(name="admin")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())

# ═══════════════════════════════ USER HANDLERS ═══════════════════════════════


@user_router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    bal = await get_balance(m.from_user.id)
    welcome_text = (
        f"🎁 <b>Welcome to {config.STORE_NAME}</b>\n\n"
        f"<b>Your Balance:</b> <b>{money(bal)}</b>\n\n"
        "Select an option below to buy Google Voice accounts or manage your wallet balance."
    )
    await m.answer_photo(photo=IMG_WELCOME, caption=welcome_text, parse_mode=ParseMode.HTML, reply_markup=main_menu(m.from_user.id in ADMIN_SET))


@user_router.callback_query(F.data == "home")
@user_router.callback_query(F.data == "cancel")
async def cb_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    bal = await get_balance(c.from_user.id)
    welcome_text = (
        f"🎁 <b>Welcome to {config.STORE_NAME}</b>\n\n"
        f"<b>Your Balance:</b> <b>{money(bal)}</b>\n\n"
        "Select an option below to browse products or top up your balance."
    )
    await show(c, welcome_text, main_menu(c.from_user.id in ADMIN_SET), photo_url=IMG_WELCOME)


@user_router.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


# ── Products Catalog & Quantity Selection ──


@user_router.callback_query(F.data.startswith("pl:"))
async def cb_products_list(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = [
        [btn(f"🟢 New GV — {money(NEW_GV_PRICE_CENTS)}", "gv_select:new", "success")],
        [btn(f"📜 Old GV — {money(OLD_GV_PRICE_CENTS)}", "gv_select:old", "primary")],
        [back()],
    ]
    text = "📦 <b>Select Google Voice Category:</b>"
    await show(c, text, kb(rows), photo_url=IMG_BUY_GV)


@user_router.callback_query(F.data.startswith("gv_select:"))
async def cb_gv_select(c: CallbackQuery, state: FSMContext):
    await state.clear()
    gv_type = c.data.split(":")[1]
    unit_cents = NEW_GV_PRICE_CENTS if gv_type == "new" else OLD_GV_PRICE_CENTS
    gv_title = "New GV" if gv_type == "new" else "Old GV"

    available_count = await db.inventory.count_documents({"gv_type": gv_type, "status": "available"})
    bal = await get_balance(c.from_user.id)

    text = (
        f"📦 <b>Category: {gv_title}</b>\n\n"
        f"Price per account: <b>{money(unit_cents)}</b>\n"
        f"Available Stock: <b>{available_count}</b> accounts\n"
        f"Your Balance: <b>{money(bal)}</b>\n\n"
        "Select quantity using the buttons below or click <b>Custom Quantity</b> to type an amount:"
    )

    rows = [
        [btn("1", f"gv_confirm:{gv_type}:1"), btn("2", f"gv_confirm:{gv_type}:2"), btn("5", f"gv_confirm:{gv_type}:5")],
        [btn("10", f"gv_confirm:{gv_type}:10"), btn("15", f"gv_confirm:{gv_type}:15")],
        [btn("✏️ Custom Quantity", f"gv_custom:{gv_type}", "primary")],
        [back("pl:0")]
    ]
    await show(c, text, kb(rows), photo_url=IMG_BUY_GV)


@user_router.callback_query(F.data.startswith("gv_custom:"))
async def cb_gv_custom_prompt(c: CallbackQuery, state: FSMContext):
    gv_type = c.data.split(":")[1]
    await state.update_data(gv_type=gv_type)
    await state.set_state(BuyGVSt.custom_qty)

    gv_title = "New GV" if gv_type == "new" else "Old GV"
    await show(
        c,
        f"✏️ <b>Enter Custom Quantity for {gv_title}:</b>\n\nPlease type the number of accounts you wish to purchase (e.g. <code>3</code> or <code>20</code>):",
        kb([[cancel_btn(f"gv_select:{gv_type}")]]),
        photo_url=IMG_BUY_GV
    )


@user_router.message(BuyGVSt.custom_qty, F.text)
async def msg_gv_custom_qty(m: Message, state: FSMContext):
    qty = to_int(m.text.strip())
    if qty <= 0:
        return await m.answer("❌ Please enter a valid positive whole number.", reply_markup=kb([[cancel_btn("pl:0")]]))

    data = await state.get_data()
    gv_type = data.get("gv_type", "new")
    await state.clear()

    await render_gv_checkout(m, m.from_user.id, gv_type, qty)


@user_router.callback_query(F.data.startswith("gv_confirm:"))
async def cb_gv_confirm_qty(c: CallbackQuery, state: FSMContext):
    await state.clear()
    _, gv_type, qty_str = c.data.split(":")
    qty = to_int(qty_str)
    await render_gv_checkout(c, c.from_user.id, gv_type, qty)


async def render_gv_checkout(ev, user_id: int, gv_type: str, qty: int):
    unit_cents = NEW_GV_PRICE_CENTS if gv_type == "new" else OLD_GV_PRICE_CENTS
    total_cents = unit_cents * qty
    gv_title = "New GV" if gv_type == "new" else "Old GV"
    bal = await get_balance(user_id)

    available_count = await db.inventory.count_documents({"gv_type": gv_type, "status": "available"})

    text = (
        f"🛍 <b>Order Summary: {gv_title}</b>\n\n"
        f"Quantity: <b>{qty}</b>\n"
        f"Unit Price: <b>{money(unit_cents)}</b>\n"
        f"Total Price: <b>{money(total_cents)}</b>\n\n"
        f"Your Current Balance: <b>{money(bal)}</b>\n"
        f"Available Stock: <b>{available_count}</b>\n\n"
    )

    rows = []
    if available_count < qty:
        text += f"❌ <b>Not enough stock available!</b> Maximum available is {available_count}."
        rows.append([back(f"gv_select:{gv_type}", "⬅️ Change Quantity")])
    elif bal < total_cents:
        shortfall = total_cents - bal
        text += f"⚠️ <b>Insufficient Balance!</b> You need <b>{money(shortfall)}</b> more."
        rows.append([btn("➕ Top Up Balance", "w", "success")])
        rows.append([back(f"gv_select:{gv_type}", "⬅️ Change Quantity")])
    else:
        text += "<i>Click below to confirm payment and receive your account credentials instantly.</i>"
        rows.append([btn(f"💳 Pay {money(total_cents)}", f"buygv_exec:{gv_type}:{qty}", "success")])
        rows.append([back(f"gv_select:{gv_type}", "⬅️ Change Quantity")])

    await show(ev, text, kb(rows), photo_url=IMG_BUY_GV)


@user_router.callback_query(F.data.startswith("buygv_exec:"))
async def cb_buy_gv_exec(c: CallbackQuery):
    _, gv_type, qty_str = c.data.split(":")
    qty = to_int(qty_str)
    await c.answer("Processing your order...")

    u_info = f"@{c.from_user.username}" if c.from_user.username else c.from_user.first_name
    order, items, err = await bulk_purchase(c.from_user.id, gv_type, qty, u_info)

    if err:
        return await show(c, f"❌ {err}", kb([[back("pl:0")]]), photo_url=IMG_BUY_GV)

    accounts_str = "\n\n".join([f"<b>Account #{idx}:</b>\n{delivery_block(item)}" for idx, item in enumerate(items, 1)])

    text = (
        f"✅ <b>Order Placed Successfully!</b>\n\n"
        f"<b>Order ID:</b> <code>{order['order_id']}</code>\n"
        f"<b>Product:</b> {order['product_name']}\n"
        f"<b>Total Paid:</b> {money(order['amount_cents'])}\n\n"
        f"🔑 <b>Delivered Account Details:</b>\n\n"
        f"{accounts_str}"
    )

    rows = [[btn("📦 My Orders", "ol:0", "primary"), back("home", "🏠 Back to Menu")]]
    await show(c, text, kb(rows), photo_url=IMG_BUY_GV)


# ── Top-Up Wallet Workflow ──


@user_router.callback_query(F.data == "w")
async def cb_wallet(c: CallbackQuery, state: FSMContext):
    await state.clear()
    bal = await get_balance(c.from_user.id)
    text = f"👛 <b>Wallet Management</b>\n\n<b>Current Balance: {money(bal)}</b>"
    await show(c, text, kb([
        [btn("➕ Top Up Balance", "tu_start", "success"), btn("📜 Transactions", "wt:0", "primary")],
        [back()],
    ]), photo_url=IMG_WALLET)


@user_router.callback_query(F.data == "tu_start")
async def cb_topup_start(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(TopUpSt.amount)
    await show(
        c,
        f"💵 <b>Enter Deposit Amount (in USD)</b>\n\nExample: Type <code>10</code> or <code>10.00</code>:\nMinimum Deposit: <b>${config.MIN_DEPOSIT}</b>",
        kb([[cancel_btn("w")]]),
        photo_url=IMG_WALLET
    )


@user_router.message(TopUpSt.amount, F.text)
async def msg_topup_amount(m: Message, state: FSMContext):
    cents = parse_money(m.text)
    min_cents = int(Decimal(config.MIN_DEPOSIT) * 100)
    if cents is None or cents < min_cents:
        return await m.answer(f"❌ Invalid amount. Minimum deposit is ${config.MIN_DEPOSIT}.", reply_markup=kb([[cancel_btn("w")]]))

    await state.update_data(amount_cents=cents)
    await state.set_state(TopUpSt.currency)

    wallets = await get_active_wallets()
    rows = []

    for key, addr in wallets.items():
        if addr and addr.strip():
            rows.append([btn(f"Pay with {key}", f"tu_curr:{key}", "primary")])

    if not rows:
        return await m.answer("❌ No wallet addresses available. Please contact support.", reply_markup=kb([[back("w")]]))

    rows.append([cancel_btn("w")])
    await m.answer(f"💳 You chose <b>{money(cents)}</b>.\n\nSelect payment method:", reply_markup=kb(rows))


@user_router.callback_query(TopUpSt.currency, F.data.startswith("tu_curr:"))
async def cb_topup_currency(c: CallbackQuery, state: FSMContext):
    curr = c.data.split(":")[1]
    data = await state.get_data()
    cents = data["amount_cents"]
    usd_val = cents / 100.0

    wallets = await get_active_wallets()
    wallet_addr = wallets.get(curr)

    if not wallet_addr:
        return await alert(c, "Selected currency unavailable.")

    coin_id = config.CURRENCY_PRICE_IDS.get(curr, "bitcoin")
    price = await fetch_crypto_price(coin_id)

    if price and price > 0:
        crypto_amount = round(usd_val / price, 6)
        formatted_crypto = f"{crypto_amount:.6f} {curr.split('_')[0]}"
    else:
        formatted_crypto = f"Calculate equivalent for {money(cents)}"

    await state.update_data(currency=curr, crypto_amount=formatted_crypto, wallet_addr=wallet_addr)
    await state.set_state(TopUpSt.txn_id)

    text = (
        f"🧾 <b>Payment Invoice Created</b>\n\n"
        f"<b>Amount Owed:</b> <code>{formatted_crypto}</code> (${usd_val:.2f} USD)\n\n"
        f"<b>Send Payment to Address:</b>\n<code>{wallet_addr}</code>\n\n"
        f"<i>Send the exact amount above. After sending, click 'I Have Paid' below.</i>"
    )

    rows = [
        [btn("✅ I Have Paid", "tu_paid", "success")],
        [cancel_btn("w")]
    ]
    await show(c, text, kb(rows), photo_url=IMG_WALLET)


@user_router.callback_query(TopUpSt.txn_id, F.data == "tu_paid")
async def cb_topup_paid(c: CallbackQuery):
    await show(c, "✏️ Please type or paste your <b>Transaction Hash / TXN ID</b>:", kb([[cancel_btn("w")]]), photo_url=IMG_WALLET)


@user_router.message(TopUpSt.txn_id, F.text)
async def msg_topup_txnid(m: Message, state: FSMContext):
    await state.update_data(txn_id=m.text.strip())
    await state.set_state(TopUpSt.proof_photo)

    await m.answer(
        "📸 Please send a <b>screenshot / photo proof</b> of your completed transaction:",
        reply_markup=kb([[cancel_btn("w")]])
    )


@user_router.message(TopUpSt.proof_photo, F.photo | F.document)
async def msg_topup_proof(m: Message, state: FSMContext):
    photo_id = None
    if m.photo:
        photo_id = m.photo[-1].file_id
    elif m.document and m.document.mime_type and m.document.mime_type.startswith("image/"):
        photo_id = m.document.file_id

    if not photo_id:
        return await m.answer(
            "❌ Invalid format. Please send an image/screenshot as proof.",
            reply_markup=kb([[cancel_btn("w")]])
        )

    data = await state.get_data()
    await state.clear()

    pid = new_id("DEP", 6)
    cents = data["amount_cents"]
    curr = data["currency"]
    crypto_amt = data.get("crypto_amount", "N/A")
    txid = data["txn_id"]
    u_info = f"@{m.from_user.username}" if m.from_user.username else m.from_user.first_name

    deposit_doc = {
        "payment_id": pid,
        "merchant_trade_no": pid,
        "user_id": m.from_user.id,
        "user_info": u_info,
        "amount_cents": cents,
        "currency": curr,
        "crypto_amount": crypto_amt,
        "txn_id": txid,
        "photo_id": photo_id,
        "status": "pending",
        "created_at": now(),
    }
    await db.payments.insert_one(deposit_doc)

    support_username = getattr(config, "SUPPORT_USERNAME", "").lstrip("@")
    support_ref = f"@{support_username}" if support_username else "support"

    await m.answer(
        "✅ <b>Payment Proof Submitted!</b>\n\n"
        "⏳ <b>Payment should be processed in 5 to 20 minutes.</b>\n"
        f"If not, send a message to support ID: {support_ref} or contact support.",
        reply_markup=kb([[back("w")]])
    )

    admin_markup = kb([
        [
            btn("✅ Approve", f"adm:app_dep:{pid}", "success"),
            btn("❌ Reject", f"adm:rej_dep:{pid}", "danger")
        ]
    ])

    admin_text = (
        f"💳 <b>New Deposit Verification Request!</b>\n\n"
        f"<b>ID:</b> <code>{pid}</code>\n"
        f"<b>User:</b> {u_info} (ID: <code>{m.from_user.id}</code>)\n"
        f"<b>USD Amount:</b> {money(cents)}\n"
        f"<b>Expected Crypto:</b> {crypto_amt}\n"
        f"<b>Currency:</b> {curr}\n"
        f"<b>TXN ID:</b> <code>{esc(txid)}</code>"
    )

    for aid in ADMIN_SET:
        try:
            if m.photo:
                await bot_ref.send_photo(aid, photo=photo_id, caption=admin_text, reply_markup=admin_markup, parse_mode=ParseMode.HTML)
            else:
                await bot_ref.send_document(aid, document=photo_id, caption=admin_text, reply_markup=admin_markup, parse_mode=ParseMode.HTML)
        except TelegramAPIError as e:
            log.warning("Failed sending payment proof to admin %s: %s", aid, e)


@user_router.message(TopUpSt.proof_photo)
async def msg_topup_proof_invalid(m: Message):
    await m.answer(
        "⚠ Please upload a valid image screenshot of your payment proof.",
        reply_markup=kb([[cancel_btn("w")]])
    )


@user_router.callback_query(F.data.startswith("wt:"))
async def cb_transactions(c: CallbackQuery):
    page = to_int(c.data.split(":")[1])
    docs, page, pages, total = await page_query(db.wallet_transactions, {"user_id": c.from_user.id}, [("created_at", -1)], page, size=PAGE_10)
    if not total:
        text = "📜 <b>Transaction History</b>\n\nNo records found."
    else:
        lines = [f"{'+' if t['amount_cents'] >= 0 else '−'}{money(abs(t['amount_cents']))} · {esc(t['type'].title())} · {fmt_dt(t['created_at'])}" for t in docs]
        text = "📜 <b>Transaction History</b>\n\n" + "\n".join(lines)
    await show(c, text, kb(pager("wt", page, pages) + [[back("w")]]), photo_url=IMG_WALLET)


# ── Orders & Support ──


@user_router.callback_query(F.data.startswith("ol:"))
async def cb_orders(c: CallbackQuery):
    page = to_int(c.data.split(":")[1])
    docs, page, pages, total = await page_query(db.orders, {"user_id": c.from_user.id}, [("created_at", -1)], page, size=PAGE_10)
    if not total:
        return await show(c, "📦 <b>My Orders</b>\n\nNo orders placed yet.", kb([[back()]]), photo_url=IMG_ORDERS)

    rows = [[btn(f"{o['order_id']} · {o['product_name']} · {money(o['amount_cents'])}", f"ov:{o['order_id']}", "primary")] for o in docs]
    rows += pager("ol", page, pages)
    rows.append([back()])
    await show(c, f"📦 <b>My Orders</b> (Total: {total})", kb(rows), photo_url=IMG_ORDERS)


@user_router.callback_query(F.data.startswith("ov:"))
async def cb_order_view(c: CallbackQuery):
    oid = c.data.split(":")[1]
    o = await db.orders.find_one({"order_id": oid, "user_id": c.from_user.id})
    if not o:
        return await alert(c, "Order record missing.")

    text = (
        f"<b>Order ID: {o['order_id']}</b>\n"
        f"<b>Product: {o['product_name']}</b>\n"
        f"<b>Amount Deducted: {money(o['amount_cents'])}</b>\n\n"
        f"<b>Delivered Account Details:</b>\n\n"
    )

    if o.get("item_ids"):
        items = await db.inventory.find({"item_id": {"$in": o["item_ids"]}}).to_list(len(o["item_ids"]))
        for idx, item in enumerate(items, 1):
            text += f"<b>Account #{idx}:</b>\n{delivery_block(item)}\n\n"
    elif o.get("item_id"):
        item = await db.inventory.find_one({"item_id": o["item_id"]})
        if item:
            text += delivery_block(item)

    await show(c, text, kb([[back("ol:0")]]), photo_url=IMG_ORDERS)


@user_router.callback_query(F.data == "sup")
async def cb_support(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text = "🎧 <b>Customer Support</b>\n\nNeed assistance? Open a direct chat or send a message below."
    await show(c, text, kb([
        [btn("💬 Contact Support", url=f"https://t.me/{config.SUPPORT_USERNAME.lstrip('@')}", style="success")],
        [btn("📨 Direct Message", "supm", "primary")],
        [back()],
    ]), photo_url=IMG_SUPPORT)


@user_router.callback_query(F.data == "supm")
async def cb_support_msg(c: CallbackQuery, state: FSMContext):
    await state.set_state(SupportSt.msg)
    await show(c, "📨 Type your support request below:", kb([[cancel_btn("sup")]]), photo_url=IMG_SUPPORT)


@user_router.message(SupportSt.msg, F.text)
async def msg_support(m: Message, state: FSMContext):
    await state.clear()
    body = f"💬 Support message from @{m.from_user.username or 'NoUser'} (ID: {m.from_user.id}):\n\n{m.text}"
    await notify_admins(text=body)
    await m.answer("✅ Support team notified.", reply_markup=kb([[back()]]))


@user_router.callback_query(F.data == "terms")
async def cb_terms(c: CallbackQuery):
    terms = await get_setting("terms", config.TERMS_TEXT)
    text = f"📜 <b>Terms & Conditions</b>\n\n{terms}"
    await show(c, text, kb([[back()]]), photo_url=IMG_TERMS)


# ═══════════════════════════════ ADMIN PANEL ═══════════════════════════════


def admin_menu() -> InlineKeyboardMarkup:
    return kb([
        [btn("➕ Add Stock", "adm:add_choice", "success"), btn("📦 Active Stock", "adm:ai:0", "primary")],
        [btn("🔥 Delete All Stock", "adm:del_all_confirm", "danger"), btn("🛒 Sold Stock", "adm:ss:0", "primary")],
        [btn("💳 Pending Deposits", "adm:pd:0", "primary"), btn("⚙ Manage Wallets", "adm:wallets", "primary")],
        [btn("📊 Statistics & Data Usage", "adm:st", "primary")],
        [btn("📝 Terms", "adm:tm", "primary")],
        [btn("💀 Complete Database Purge", "adm:purge_db_confirm", "danger")],
        [back("home", "🏠 User Menu")],
    ])


@admin_router.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("⚙ <b>Admin Control Panel</b>", reply_markup=admin_menu())


@admin_router.callback_query(F.data.in_({"adm:home", "adm:cancel"}))
async def cb_admin_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, "⚙️ <b>Admin Control Panel</b>", admin_menu())


# ── Complete Database Purge Handler ──


@admin_router.callback_query(F.data == "adm:purge_db_confirm")
async def cb_purge_db_confirm(c: CallbackQuery):
    text = (
        "🚨 <b>WARNING: COMPLETE DATABASE PURGE</b> 🚨\n\n"
        "You are about to completely delete <b>ALL</b> data in the database:\n"
        "• All user profiles & user money/balances\n"
        "• All active inventory stock\n"
        "• All order history\n"
        "• All deposit and transaction records\n\n"
        "<b>THIS ACTION IS IRREVERSIBLE!</b> Are you absolutely sure?"
    )
    rows = [
        [btn("💀 YES, PURGE ENTIRE DATABASE", "adm:purge_db_execute", "danger")],
        [back("adm:home", "❌ Cancel / Go Back")]
    ]
    await show(c, text, kb(rows))


@admin_router.callback_query(F.data == "adm:purge_db_execute")
async def cb_purge_db_execute(c: CallbackQuery):
    # Purge all collections
    await db.users.delete_many({})
    await db.wallets.delete_many({})
    await db.wallet_transactions.delete_many({})
    await db.inventory.delete_many({})
    await db.orders.delete_many({})
    await db.payments.delete_many({})

    await show(
        c,
        "💥 <b>DATABASE COMPLETE PURGE SUCCESSFUL!</b>\n\nAll users, money balances, stock inventory, and transaction histories have been wiped.",
        kb([[back("adm:home")]])
    )


# ── Admin Dynamic Wallet Management ──


@admin_router.callback_query(F.data == "adm:wallets")
async def cb_admin_wallets(c: CallbackQuery, state: FSMContext):
    await state.clear()
    wallets = await get_active_wallets()
    lines = ["⚙ <b>Wallet Addresses Management</b>\n"]
    rows = []

    for key, addr in wallets.items():
        lines.append(f"• <b>{key}:</b>\n<code>{addr}</code>\n")
        rows.append([btn(f"Edit {key}", f"adm:ewallet:{key}", "primary")])

    rows.append([back("adm:home")])
    await show(c, "\n".join(lines), kb(rows))


@admin_router.callback_query(F.data.startswith("adm:ewallet:"))
async def cb_admin_edit_wallet(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[2]
    await state.update_data(currency_key=key)
    await state.set_state(AdminWalletSt.address)
    await show(c, f"✏️ Send new address for <b>{key}</b>:", kb([[cancel_btn("adm:wallets")]]))


@admin_router.message(AdminWalletSt.address, F.text)
async def msg_admin_wallet_save(m: Message, state: FSMContext):
    data = await state.get_data()
    key = data["currency_key"]
    new_addr = m.text.strip()
    await state.clear()

    wallets = await get_active_wallets()
    wallets[key] = new_addr
    await set_setting("wallets", wallets)

    await m.answer(f"✅ Wallet for <b>{key}</b> updated successfully!\n\nNew Address:\n<code>{new_addr}</code>", reply_markup=kb([[back("adm:wallets")]]))


# ── Deposit Approvals ──


@admin_router.callback_query(F.data.startswith("adm:app_dep:"))
async def cb_approve_deposit(c: CallbackQuery):
    pid = c.data.split(":")[2]
    pay = await db.payments.find_one_and_update(
        {"payment_id": pid, "status": "pending"},
        {"$set": {"status": "completed", "approved_at": now()}},
        return_document=ReturnDocument.AFTER,
    )
    if not pay:
        return await alert(c, "Deposit already processed or expired.")

    w = await db.wallets.find_one_and_update(
        {"user_id": pay["user_id"]},
        {"$inc": {"balance_cents": pay["amount_cents"]}, "$set": {"updated_at": now()}},
        upsert=True, return_document=ReturnDocument.AFTER,
    )

    await db.wallet_transactions.insert_one({
        "tx_id": new_id("TX", 6),
        "user_id": pay["user_id"],
        "type": "deposit",
        "amount_cents": pay["amount_cents"],
        "ref_id": pid,
        "note": f"Manual {pay['currency']} Deposit",
        "applied": True,
        "balance_after_cents": w["balance_cents"],
        "created_at": now(),
    })

    text = f"💰 <b>Deposit Approved!</b>\n\n<b>{money(pay['amount_cents'])}</b> added to your wallet.\nNew Balance: <b>{money(w['balance_cents'])}</b>"
    await safe_send(pay["user_id"], text=text, parse_mode=ParseMode.HTML)

    await show(c, f"✅ Deposit <code>{pid}</code> approved and credited successfully!", kb([[back("adm:home")]]))


@admin_router.callback_query(F.data.startswith("adm:rej_dep:"))
async def cb_reject_deposit(c: CallbackQuery):
    pid = c.data.split(":")[2]
    pay = await db.payments.find_one_and_update(
        {"payment_id": pid, "status": "pending"},
        {"$set": {"status": "rejected", "rejected_at": now()}},
        return_document=ReturnDocument.AFTER,
    )
    if not pay:
        return await alert(c, "Deposit already processed.")

    await safe_send(pay["user_id"], text=f"❌ Deposit <code>{pid}</code> was rejected by admin.")
    await show(c, f"❌ Deposit <code>{pid}</code> rejected.", kb([[back("adm:home")]]))


@admin_router.callback_query(F.data.startswith("adm:pd:"))
async def cb_pending_deposits(c: CallbackQuery):
    page = to_int(c.data.split(":")[2])
    docs, page, pages, total = await page_query(db.payments, {"status": "pending"}, [("created_at", -1)], page, size=PAGE_10)

    if not total:
        return await show(c, "💳 <b>Pending Deposits</b>\n\nNo pending top-up requests.", kb([[back("adm:home")]]))

    rows = []
    for p in docs:
        rows.append([btn(f"{p['payment_id']} · {money(p['amount_cents'])} ({p['currency']})", f"adm:vdep:{p['payment_id']}", "primary")])

    rows += pager("adm:pd", page, pages)
    rows.append([back("adm:home")])
    await show(c, f"💳 <b>Pending Deposit Requests</b> ({total}):", kb(rows))


@admin_router.callback_query(F.data.startswith("adm:vdep:"))
async def cb_view_deposit(c: CallbackQuery):
    pid = c.data.split(":")[2]
    p = await db.payments.find_one({"payment_id": pid})
    if not p:
        return await alert(c, "Deposit not found.")

    text = (
        f"💳 <b>Deposit Request:</b> <code>{p['payment_id']}</code>\n\n"
        f"User: {p.get('user_info')} (ID: <code>{p['user_id']}</code>)\n"
        f"USD Amount: <b>{money(p['amount_cents'])}</b>\n"
        f"Currency: {p['currency']}\n"
        f"Expected Crypto: {p.get('crypto_amount', 'N/A')}\n"
        f"TXN ID: <code>{esc(p['txn_id'])}</code>\n"
        f"Status: {p['status']}"
    )

    rows = []
    if p["status"] == "pending":
        rows.append([btn("✅ Approve", f"adm:app_dep:{pid}", "success"), btn("❌ Reject", f"adm:rej_dep:{pid}", "danger")])
    rows.append([back("adm:pd:0")])

    await show(c, text, kb(rows))


# ── Stock Management ──


@admin_router.callback_query(F.data == "adm:add_choice")
async def cb_add_stock_choice(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = [
        [btn(f"🟢 New GV ({money(NEW_GV_PRICE_CENTS)})", "adm:add_stock:new", "success")],
        [btn(f"📜 Old GV ({money(OLD_GV_PRICE_CENTS)})", "adm:add_stock:old", "primary")],
        [back("adm:home")],
    ]
    await show(c, "➕ <b>Select Stock Category to Add:</b>", kb(rows))


@admin_router.callback_query(F.data.startswith("adm:add_stock:"))
async def cb_bulk_add_stock_start(c: CallbackQuery, state: FSMContext):
    gv_type = c.data.split(":")[2]
    await state.update_data(gv_type=gv_type)
    await state.set_state(BulkAddStockSt.raw_data)

    prompt = (
        "➕ <b>Add stock</b>\n\n"
        "<b>Send or paste raw account text:</b>\n"
        "Paste account credentials as they are. The bot will deliver the entire message block exactly as provided to buyers upon purchase.\n"
        "<i>Note: Items separated by double spaces will automatically be imported as separate accounts!</i>\n\n"
        "<i>Send /cancel to abort.</i>"
    )
    await show(c, prompt, kb([[cancel_btn("adm:home")]]))


@admin_router.message(BulkAddStockSt.raw_data, F.text)
async def msg_bulk_add_stock_process(m: Message, state: FSMContext):
    if m.text.strip().lower() == "/cancel":
        await state.clear()
        return await m.answer("❌ Stock addition cancelled.", reply_markup=admin_menu())

    data = await state.get_data()
    gv_type = data.get("gv_type", "new")
    price_cents = NEW_GV_PRICE_CENTS if gv_type == "new" else OLD_GV_PRICE_CENTS

    parsed_items = parse_gv_lines(m.text)
    if not parsed_items:
        return await m.answer("❌ Invalid format or empty text. Please check input.", reply_markup=kb([[cancel_btn("adm:home")]]))

    added = 0
    skipped = 0
    invalid = 0

    for item in parsed_items:
        raw_text = item.get("raw_text", "").strip()
        if not raw_text:
            invalid += 1
            continue

        raw_enc = enc(raw_text)

        existing = await db.inventory.find_one({"raw_text_enc": raw_enc})
        if existing:
            skipped += 1
            continue

        item_prefix = "GVN" if gv_type == "new" else "GVO"
        item_id = new_id(item_prefix, 6)

        doc = {
            "item_id": item_id,
            "gv_type": gv_type,
            "price_cents": price_cents,
            "raw_text_enc": raw_enc,
            "code_enc": raw_enc,
            "status": "available",
            "created_at": now(),
            "added_by": m.from_user.id,
        }
        await db.inventory.insert_one(doc)
        added += 1

    await state.clear()

    live_count = await db.inventory.count_documents({"status": "available"})
    sold_count = await db.inventory.count_documents({"status": "sold"})

    report_text = (
        f"✔ <b>{added} added live</b> · <b>skipped {skipped} duplicates</b> · <b>{invalid} invalid</b>.\n\n"
        f"📦 <b>Supplier panel</b>\n\n"
        f"🟢 Live stock: <b>{live_count}</b> · 🔴 Sold: <b>{sold_count}</b>\n"
        f"🧩 Products: <b>2</b>"
    )

    reply_markup = kb([
        [btn("➕ Add stock", "adm:add_choice", "success")],
        [back("adm:home", "🏠 Admin Menu")]
    ])

    await m.answer(report_text, reply_markup=reply_markup)


@admin_router.callback_query(F.data.startswith("adm:ai:"))
async def cb_active_stock(c: CallbackQuery):
    page = to_int(c.data.split(":")[2])
    docs, page, pages, total = await page_query(db.inventory, {"status": "available"}, [("created_at", -1)], page, size=PAGE_10)

    if not total:
        return await show(c, "📦 <b>Active Inventory</b>\n\nNo stock currently available.", kb([[back("adm:home")]]))

    rows = [[btn(f"{i['item_id']} · {i.get('gv_type', 'gv').upper()} · {money(i['price_cents'])}", f"adm:ii:{i['item_id']}", "primary")] for i in docs]
    rows += pager("adm:ai", page, pages)
    rows.append([btn("🔥 Delete All Available Stock", "adm:del_all_confirm", "danger")])
    rows.append([back("adm:home")])
    await show(c, f"📦 <b>Active Inventory</b> ({total} items):", kb(rows))


@admin_router.callback_query(F.data.startswith("adm:ii:"))
async def cb_active_item_view(c: CallbackQuery):
    item_id = c.data.split(":")[2]
    item = await db.inventory.find_one({"item_id": item_id})
    if not item:
        return await alert(c, "Item not found.")

    text = (
        f"📦 <b>Item Details:</b> <code>{item['item_id']}</code>\n\n"
        f"Type: <b>{item.get('gv_type', 'N/A').upper()}</b>\n"
        f"Price: <b>{money(item['price_cents'])}</b>\n"
        f"Status: <b>{item['status']}</b>\n\n"
        f"🔑 <b>Credentials:</b>\n"
        f"{delivery_block(item)}"
    )

    rows = [
        [btn("🗑 Delete This GV", f"adm:id:{item_id}", "danger")],
        [back("adm:ai:0")]
    ]
    await show(c, text, kb(rows))


@admin_router.callback_query(F.data.startswith("adm:id:"))
async def cb_delete_item(c: CallbackQuery):
    item_id = c.data.split(":")[2]
    await db.inventory.delete_one({"item_id": item_id})
    await alert(c, "✅ Item deleted successfully.")
    await cb_active_stock(c)


@admin_router.callback_query(F.data == "adm:del_all_confirm")
async def cb_delete_all_confirm(c: CallbackQuery):
    active_count = await db.inventory.count_documents({"status": "available"})
    if active_count == 0:
        return await alert(c, "There is no available stock to delete.")

    text = (
        f"⚠️ <b>ARE YOU SURE?</b>\n\n"
        f"You are about to permanently delete <b>{active_count}</b> available Google Voice accounts from the inventory.\n\n"
        f"This action cannot be undone."
    )
    rows = [
        [btn("🔥 Yes, Delete All GV Stock", "adm:del_all_execute", "danger")],
        [back("adm:ai:0", "❌ Cancel")]
    ]
    await show(c, text, kb(rows))


@admin_router.callback_query(F.data == "adm:del_all_execute")
async def cb_delete_all_execute(c: CallbackQuery):
    res = await db.inventory.delete_many({"status": "available"})
    await show(
        c,
        f"✅ <b>Successfully deleted {res.deleted_count} available accounts!</b>",
        kb([[back("adm:home")]])
    )


@admin_router.callback_query(F.data.startswith("adm:ss:"))
async def cb_sold_stock(c: CallbackQuery):
    page = to_int(c.data.split(":")[2])
    docs, page, pages, total = await page_query(db.orders, {"status": "completed"}, [("created_at", -1)], page, size=PAGE_10)
    rows = [[btn(f"{o['order_id']} · {o['product_name']} · {money(o['amount_cents'])}", "noop", "primary")] for o in docs]
    rows += pager("adm:ss", page, pages)
    rows.append([back("adm:home")])
    await show(c, f"🛒 <b>Sold Products History</b> ({total}):", kb(rows))


@admin_router.callback_query(F.data == "adm:st")
async def cb_admin_stats(c: CallbackQuery):
    users = await db.users.count_documents({})
    active = await db.inventory.count_documents({"status": "available"})
    sold = await db.inventory.count_documents({"status": "sold"})
    pending_dep = await db.payments.count_documents({"status": "pending"})

    net_io = psutil.net_io_counters()
    bytes_sent = fmt_bytes(net_io.bytes_sent)
    bytes_recv = fmt_bytes(net_io.bytes_recv)
    packets_sent = net_io.packets_sent
    packets_recv = net_io.packets_recv

    pipeline = [{"$match": {"status": "completed"}}, {"$group": {"_id": None, "total": {"$sum": "$amount_cents"}}}]
    rev_res = await db.orders.aggregate(pipeline).to_list(1)
    rev_cents = rev_res[0]["total"] if rev_res else 0

    text = (
        f"📊 <b>Store Analytics & Server Data Usage</b>\n\n"
        f"👥 Registered Users: <b>{users}</b>\n"
        f"📦 Active In-Stock Items: <b>{active}</b>\n"
        f"🛒 Total Items Sold: <b>{sold}</b>\n"
        f"💰 Total Sales Revenue: <b>{money(rev_cents)}</b>\n"
        f"💳 Pending Deposit Verifications: <b>{pending_dep}</b>\n\n"
        f"📡 <b>Server Data Usage Stats:</b>\n"
        f"⬆️ Total Data Sent: <b>{bytes_sent}</b> ({packets_sent:,} packets)\n"
        f"⬇️ Total Data Received: <b>{bytes_recv}</b> ({packets_recv:,} packets)"
    )
    await show(c, text, kb([[back("adm:home")]]))


@admin_router.callback_query(F.data == "adm:tm")
async def cb_admin_terms(c: CallbackQuery, state: FSMContext):
    terms = await get_setting("terms", config.TERMS_TEXT)
    await state.set_state(TermsSt.text)
    await show(c, f"📝 <b>Edit Terms & Conditions</b>\n\nCurrent terms:\n<i>{terms}</i>\n\nType new text:", kb([[cancel_btn("adm:home")]]))


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
        raise SystemExit("Please configure BOT_TOKEN in config.py.")

    await init_db()
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    bot_ref = bot
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.outer_middleware(UserMiddleware())
    dp.callback_query.outer_middleware(UserMiddleware())

    dp.include_router(admin_router)
    dp.include_router(user_router)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Bot started successfully in Crypto Deposit Mode.")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        mongo.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
