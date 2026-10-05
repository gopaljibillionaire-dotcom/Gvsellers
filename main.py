"""
Digital product store bot — Aiogram 3.x + MongoDB (Motor) + OxaPay Auto Payments + Gemini AI Metadata Parser.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import hashlib
import html
import json
import logging
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
from aiohttp import web
from cryptography.fernet import Fernet, InvalidToken
from google import genai
from google.genai import types
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

# Banners
IMG_WELCOME = "https://i.ibb.co/3mMm5pk8/file-00000000304481fabc208d5a014f5b11.png"
IMG_BUY_GV = "https://i.ibb.co/qFBDtRMT/file-00000000543c821195d09ed80ad42f1c.png"
IMG_ORDERS = "https://i.ibb.co/Z6NpMbWG/file-0000000056e081fa90ef2c05289f9691.png"
IMG_SUPPORT = "https://i.ibb.co/Bxy6JP8/file-0000000024bc8210a4acf5976393bad9.png"
IMG_TERMS = "https://i.ibb.co/Q3R5YqjS/file-00000000910c8211957902711cb364f9.png"


# ════════════════════════════ GEMINI AI SETUP ════════════════════════════

gemini_client: Optional[genai.Client] = None
if getattr(config, "GEMINI_API_KEY", None) and config.GEMINI_API_KEY != "YOUR_GEMINI_API_KEY_HERE":
    gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)


async def parse_gv_lines_with_gemini(text: str) -> list[dict[str, Any]]:
    """Uses Gemini AI to parse raw GV dumps into detailed structured records."""
    if not gemini_client or not text.strip():
        return parse_gv_lines_fallback(text)

    prompt = (
        "Extract Google Voice / Google Account details from the provided raw text.\n"
        "Analyze each account block/line and return a JSON list of objects. Each object MUST contain:\n"
        "- email: primary google email\n"
        "- password: primary email password\n"
        "- rec_email: recovery email (or 'N/A')\n"
        "- rec_pass_2fa: recovery password or 2FA key (or 'N/A')\n"
        "- phone: phone number attached (or 'N/A')\n\n"
        "Raw text to parse:\n" + text
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            )
        )
        data = json.loads(response.text)
        if isinstance(data, list):
            parsed_accounts = []
            for item in data:
                parsed_accounts.append({
                    "email": item.get("email", "N/A"),
                    "password": item.get("password", "N/A"),
                    "rec_email": item.get("rec_email", "N/A"),
                    "rec_pass_2fa": item.get("rec_pass_2fa", "N/A"),
                    "phone": item.get("phone", "N/A")
                })
            return parsed_accounts
    except Exception as e:
        log.error("Gemini AI Parsing failed, falling back: %s", e)

    return parse_gv_lines_fallback(text)


def parse_gv_lines_fallback(text: str) -> list[dict[str, Any]]:
    """Fallback manual line parser."""
    accounts = []
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    for line in lines:
        parts = line.split()
        email = parts[0] if len(parts) > 0 else "N/A"
        password = parts[1] if len(parts) > 1 else "N/A"
        rec_email = parts[2] if len(parts) > 2 else "N/A"
        phone = parts[-1] if len(parts) > 3 else "N/A"
        accounts.append({
            "email": email,
            "password": password,
            "rec_email": rec_email,
            "rec_pass_2fa": "N/A",
            "phone": phone
        })
    return accounts


# ════════════════════════════ OXAPAY API INTEGRATION ════════════════════════════


async def create_oxapay_static_address(user_id: int, currency: str, amount_usd: float) -> Optional[dict]:
    """Generates an automatic white-label payment address pre-selected for a specific currency."""
    payload = {
        "merchant": config.OXAPAY_MERCHANT_KEY,
        "amount": amount_usd,
        "currency": "USD",
        "payCurrency": currency.upper(),
        "lifeTime": 60,
        "feePaidByPayer": 0,
        "callbackUrl": f"{config.WEBHOOK_URL}/oxapay/callback",
        "description": f"Order payment for User {user_id}",
        "orderId": f"USER_{user_id}_{int(datetime.now().timestamp())}"
    }

    url = getattr(config, "OXAPAY_WHITE_LABEL_URL", "https://api.oxapay.com/merchants/request/whitelabel")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=12) as resp:
                data = await resp.json()
                if data.get("result") == 100:
                    return data
                log.error("OxaPay Address Error details: %s", data)
    except Exception as e:
        log.error("OxaPay API Error (White Label): %s", e)
    return None


async def create_oxapay_full_invoice(user_id: int, amount_usd: float, currency: Optional[str] = None) -> Optional[dict]:
    """Generates a full OxaPay Hosted Checkout Panel invoice link (supporting full panel pre-fills)."""
    payload = {
        "merchant": config.OXAPAY_MERCHANT_KEY,
        "amount": amount_usd,
        "currency": "USD",
        "lifeTime": 60,
        "callbackUrl": f"{config.WEBHOOK_URL}/oxapay/callback",
        "description": f"Multi-currency checkout for User {user_id}",
        "orderId": f"USER_{user_id}_{int(datetime.now().timestamp())}"
    }
    
    if currency:
        payload["payCurrency"] = currency.upper()

    url = getattr(config, "OXAPAY_CREATE_INVOICE_URL", "https://api.oxapay.com/merchants/request")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=12) as resp:
                data = await resp.json()
                if data.get("result") == 100:
                    return data
                log.error("OxaPay Invoice Error details: %s", data)
    except Exception as e:
        log.error("OxaPay API Error (Invoice): %s", e)
    return None


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
        [btn("📦 My Orders", "ol:0", "primary")],
        [btn("💬 Contact Support", "sup", "success"), btn("📜 Terms", "terms", "primary")],
    ]
    if admin:
        rows.append([btn("⚙️ Admin Panel", "adm:home", "primary")])
    return kb(rows)


async def show(ev, text: Optional[str] = None, markup=None, photo_url: Optional[str] = None):
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
    await db.payments.create_index("track_id", unique=True)
    await db.settings.create_index("key", unique=True)


async def get_setting(key: str, default: Any = None) -> Any:
    doc = await db.settings.find_one({"key": key})
    return doc["value"] if doc and "value" in doc else default


async def set_setting(key: str, value: Any):
    await db.settings.update_one({"key": key}, {"$set": {"value": value, "updated_at": now()}}, upsert=True)


# ═══════════════════════════ INVENTORY & FULFILLMENT ═══════════════════════════


async def fulfill_order(user_id: int, gv_type: str, qty: int, track_id: str) -> tuple[Optional[dict], Optional[list], Optional[str]]:
    """Fulfills order after automatic payment confirmation by fetching requested stock items."""
    unit_price = NEW_GV_PRICE_CENTS if gv_type == "new" else OLD_GV_PRICE_CENTS
    total_cents = unit_price * qty
    gv_title = "New GV" if gv_type == "new" else "Old GV"
    t = now()

    available_items = await db.inventory.find(
        {"gv_type": gv_type, "status": "available"}
    ).limit(qty).to_list(qty)

    if len(available_items) < qty:
        return None, None, f"Insufficient stock. Available: {len(available_items)}"

    item_ids = [item["item_id"] for item in available_items]

    oid = new_id("ORD", 5)
    await db.inventory.update_many(
        {"item_id": {"$in": item_ids}},
        {"$set": {"status": "sold", "order_id": oid, "buyer_id": user_id, "sold_at": t}}
    )

    order = {
        "order_id": oid,
        "track_id": track_id,
        "user_id": user_id,
        "product_name": f"{gv_title} x{qty}",
        "amount_cents": total_cents,
        "quantity": qty,
        "item_ids": item_ids,
        "status": "completed",
        "created_at": t,
    }
    await db.orders.insert_one(order)

    admin_alert = Text(
        CustomEmoji("🛍", custom_emoji_id=getattr(config, "STORE_EMOJI_ID", "5373142232980331089")), " ", Bold("Auto-Payment Received & Fulfilled!"), "\n\n",
        f"<b>Order ID:</b> <code>{oid}</code>\n",
        f"<b>Buyer ID:</b> <code>{user_id}</code>\n",
        f"<b>Product:</b> {gv_title} x{qty}\n",
        f"<b>Amount Paid:</b> {money(total_cents)}"
    )
    await notify_admins(content=admin_alert)

    return order, available_items, None


def delivery_block(item: dict) -> str:
    """Formats full GV account metadata parsed by Gemini AI."""
    dt = item.get("details", {})
    if dt:
        email = dec(dt.get("email_enc", ""))
        password = dec(dt.get("pass_enc", ""))
        rec_email = dec(dt.get("rec_enc", ""))
        rec_pass_2fa = dec(dt.get("rec_pass_2fa_enc", ""))
        phone = dec(dt.get("phone_enc", ""))

        content = (
            f"📧 <b>Gmail:</b> <code>{esc(email)}</code>\n"
            f"🔑 <b>Password:</b> <code>{esc(password)}</code>\n"
            f"📩 <b>Recovery Email:</b> <code>{esc(rec_email)}</code>\n"
            f"🔐 <b>2FA / Recovery Pass:</b> <code>{esc(rec_pass_2fa)}</code>\n"
            f"📱 <b>Phone:</b> <code>{esc(phone)}</code>"
        )
    else:
        content = f"<code>{esc(dec(item.get('code_enc', '')))}</code>"

    return content


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
        data["db_user"] = doc
        return await handler(event, data)


class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        u = getattr(event, "from_user", None)
        return bool(u and u.id in ADMIN_SET)


class BuyGVSt(StatesGroup):
    custom_qty = State()


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
    welcome_text = (
        f"🎁 <b>Welcome to {config.STORE_NAME}</b>\n\n"
        "Select an option below to buy Google Voice accounts instantly via automatic Crypto payments."
    )
    await m.answer_photo(photo=IMG_WELCOME, caption=welcome_text, parse_mode=ParseMode.HTML, reply_markup=main_menu(m.from_user.id in ADMIN_SET))


@user_router.callback_query(F.data == "home")
@user_router.callback_query(F.data == "cancel")
async def cb_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    welcome_text = (
        f"🎁 <b>Welcome to {config.STORE_NAME}</b>\n\n"
        "Select an option below to browse products or complete orders."
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

    text = (
        f"📦 <b>Category: {gv_title}</b>\n\n"
        f"Price per account: <b>{money(unit_cents)}</b>\n"
        f"Available Stock: <b>{available_count}</b> accounts\n\n"
        "Select quantity using the buttons below or click <b>Custom Quantity</b>:"
    )

    rows = [
        [btn("1", f"gv_checkout:{gv_type}:1"), btn("2", f"gv_checkout:{gv_type}:2"), btn("5", f"gv_checkout:{gv_type}:5")],
        [btn("10", f"gv_checkout:{gv_type}:10"), btn("15", f"gv_checkout:{gv_type}:15")],
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
        f"✏️ <b>Enter Custom Quantity for {gv_title}:</b>\n\nPlease type the number of accounts you wish to purchase:",
        kb([[cancel_btn(f"gv_select:{gv_type}")] ]),
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

    await render_gv_payment_options(m, m.from_user.id, gv_type, qty)


@user_router.callback_query(F.data.startswith("gv_checkout:"))
async def cb_gv_checkout(c: CallbackQuery, state: FSMContext):
    await state.clear()
    _, gv_type, qty_str = c.data.split(":")
    qty = to_int(qty_str)
    await render_gv_payment_options(c, c.from_user.id, gv_type, qty)


# ── Automatic Payment Generation ──


async def render_gv_payment_options(ev, user_id: int, gv_type: str, qty: int):
    unit_cents = NEW_GV_PRICE_CENTS if gv_type == "new" else OLD_GV_PRICE_CENTS
    total_cents = unit_cents * qty
    total_usd = total_cents / 100.0
    gv_title = "New GV" if gv_type == "new" else "Old GV"

    available_count = await db.inventory.count_documents({"gv_type": gv_type, "status": "available"})

    if available_count < qty:
        text = f"❌ <b>Not enough stock!</b> Required: <b>{qty}</b>, Available: <b>{available_count}</b>."
        rows = [[back(f"gv_select:{gv_type}", "⬅️ Change Quantity")]]
        return await show(ev, text, kb(rows), photo_url=IMG_BUY_GV)

    text = (
        f"🛍 <b>Order Summary: {gv_title}</b>\n\n"
        f"Quantity: <b>{qty}</b>\n"
        f"Total Amount: <b>{money(total_cents)}</b> (${total_usd:.2f} USD)\n\n"
        "⚡ <b>Select Payment Method:</b>\n"
        "Choose a crypto coin below to generate a deposit address or open the full OxaPay panel:"
    )

    rows = []
    top_coins = getattr(config, "TOP_10_CURRENCIES", ["BTC", "LTC", "USDT", "TRX", "ETH", "BNB"])
    for i in range(0, len(top_coins), 2):
        pair = top_coins[i:i+2]
        row_btns = [btn(f"Pay in {coin}", f"pay_auto:{gv_type}:{qty}:{coin}", "primary") for coin in pair]
        rows.append(row_btns)

    rows.append([btn("🌐 Pay with Other Currency (Full Panel)", f"pay_panel:{gv_type}:{qty}", "success")])
    rows.append([back(f"gv_select:{gv_type}", "⬅️ Back")])

    await show(ev, text, kb(rows), photo_url=IMG_BUY_GV)


@user_router.callback_query(F.data.startswith("pay_auto:"))
async def cb_pay_auto(c: CallbackQuery):
    _, gv_type, qty_str, currency = c.data.split(":")
    qty = to_int(qty_str)
    unit_cents = NEW_GV_PRICE_CENTS if gv_type == "new" else OLD_GV_PRICE_CENTS
    total_usd = (unit_cents * qty) / 100.0

    await c.answer("Generating OxaPay address...")

    # First attempt White-Label static address API
    resp = await create_oxapay_static_address(c.from_user.id, currency, total_usd)

    if resp and resp.get("address"):
        addr = resp["address"]
        crypto_amount = resp.get("payAmount", "N/A")
        track_id = str(resp.get("trackId", ""))

        await db.payments.update_one(
            {"track_id": track_id},
            {"$set": {
                "track_id": track_id,
                "user_id": c.from_user.id,
                "gv_type": gv_type,
                "quantity": qty,
                "amount_cents": unit_cents * qty,
                "currency": currency,
                "status": "pending",
                "created_at": now()
            }},
            upsert=True
        )

        text = (
            f"⚡ <b>Automatic OxaPay Payment ({currency.upper()})</b>\n\n"
            f"💰 Send Amount: <code>{crypto_amount}</code> <b>{currency.upper()}</b>\n"
            f"📍 Send To Address:\n<code>{addr}</code>\n\n"
            f"⌛ <i>This address expires in 60 minutes. Once payment is confirmed on the blockchain, your accounts will be delivered instantly!</i>"
        )
        rows = [[back("home", "🏠 Return to Main Menu")]]
        return await show(c, text, kb(rows), photo_url=IMG_BUY_GV)

    # Fallback to hosted checkout pre-filled with selected currency
    invoice = await create_oxapay_full_invoice(c.from_user.id, total_usd, currency=currency)
    if invoice and invoice.get("payLink"):
        pay_link = invoice["payLink"]
        track_id = str(invoice.get("trackId", ""))

        await db.payments.update_one(
            {"track_id": track_id},
            {"$set": {
                "track_id": track_id,
                "user_id": c.from_user.id,
                "gv_type": gv_type,
                "quantity": qty,
                "amount_cents": unit_cents * qty,
                "currency": currency,
                "status": "pending",
                "created_at": now()
            }},
            upsert=True
        )

        text = (
            f"⚡ <b>OxaPay Hosted Checkout Panel</b>\n\n"
            f"Amount: <b>${total_usd:.2f} USD</b>\n"
            f"Selected Coin: <b>{currency.upper()}</b>\n\n"
            "Click <b>Pay Now</b> below to open the payment gateway panel:"
        )

        rows = [
            [btn(f"💵 Pay {currency.upper()} Now", url=pay_link, style="success")],
            [back("home", "🏠 Main Menu")]
        ]
        return await show(c, text, kb(rows), photo_url=IMG_BUY_GV)

    await alert(c, "Failed to generate OxaPay payment. Check API Keys or try again.")


@user_router.callback_query(F.data.startswith("pay_panel:"))
async def cb_pay_panel(c: CallbackQuery):
    _, gv_type, qty_str = c.data.split(":")
    qty = to_int(qty_str)
    unit_cents = NEW_GV_PRICE_CENTS if gv_type == "new" else OLD_GV_PRICE_CENTS
    total_usd = (unit_cents * qty) / 100.0

    await c.answer("Creating payment invoice...")
    invoice = await create_oxapay_full_invoice(c.from_user.id, total_usd)

    if not invoice or not invoice.get("payLink"):
        return await alert(c, "Failed to connect to OxaPay. Please try again.")

    pay_link = invoice["payLink"]
    track_id = str(invoice.get("trackId", ""))

    await db.payments.update_one(
        {"track_id": track_id},
        {"$set": {
            "track_id": track_id,
            "user_id": c.from_user.id,
            "gv_type": gv_type,
            "quantity": qty,
            "amount_cents": unit_cents * qty,
            "currency": "MULTI",
            "status": "pending",
            "created_at": now()
        }},
        upsert=True
    )

    text = (
        f"🌐 <b>OxaPay Multi-Currency Checkout</b>\n\n"
        f"Amount: <b>${total_usd:.2f} USD</b>\n\n"
        "Click the button below to open the full payment panel:"
    )

    rows = [
        [btn("💳 Open OxaPay Payment Gateway", url=pay_link, style="success")],
        [back("home", "🏠 Main Menu")]
    ]
    await show(c, text, kb(rows), photo_url=IMG_BUY_GV)


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
        f"<b>Amount Paid: {money(o['amount_cents'])}</b>\n\n"
        f"🔑 <b>Delivered Account Details:</b>\n\n"
    )

    if o.get("item_ids"):
        items = await db.inventory.find({"item_id": {"$in": o["item_ids"]}}).to_list(len(o["item_ids"]))
        for idx, item in enumerate(items, 1):
            text += f"<b>Account #{idx}:</b>\n{delivery_block(item)}\n\n"

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
    terms = await get_setting("terms", getattr(config, "TERMS_TEXT", "Standard terms apply."))
    text = f"📜 <b>Terms & Conditions</b>\n\n{terms}"
    await show(c, text, kb([[back()]]), photo_url=IMG_TERMS)


# ═══════════════════════════════ ADMIN PANEL ═══════════════════════════════


def admin_menu() -> InlineKeyboardMarkup:
    return kb([
        [btn("➕ Add Stock", "adm:add_choice", "success"), btn("📦 Active Stock", "adm:ai:0", "primary")],
        [btn("🔥 Delete All Stock", "adm:del_all_confirm", "danger"), btn("🛒 Sold Stock", "adm:ss:0", "primary")],
        [btn("📊 Statistics & Data Usage", "adm:st", "primary")],
        [btn("📝 Terms", "adm:tm", "primary")],
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


# ── Stock Management (Integrated with Gemini AI Parser) ──


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
        "➕ <b>Add Bulk Stock (Gemini AI Enabled)</b>\n\n"
        "<b>Send or paste bulk account dump:</b>\n"
        "Gemini AI will parse, structure, and format each account with Gmail, Password, Recovery Email, 2FA, and Phone Number separately."
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

    processing_msg = await m.answer("🧠 <i>Gemini AI is parsing Gmail, Recovery Email, 2FA & Phone Numbers...</i>", parse_mode=ParseMode.HTML)

    parsed_items = await parse_gv_lines_with_gemini(m.text)
    if not parsed_items:
        await processing_msg.delete()
        return await m.answer("❌ Invalid format or empty text.", reply_markup=kb([[cancel_btn("adm:home")]]))

    added = 0
    skipped = 0

    for item in parsed_items:
        email = item.get("email", "")
        if not email or email == "N/A":
            continue

        email_enc = enc(email)
        existing = await db.inventory.find_one({"details.email_enc": email_enc})
        if existing:
            skipped += 1
            continue

        item_prefix = "GVN" if gv_type == "new" else "GVO"
        item_id = new_id(item_prefix, 6)

        doc = {
            "item_id": item_id,
            "gv_type": gv_type,
            "price_cents": price_cents,
            "details": {
                "email_enc": email_enc,
                "pass_enc": enc(item.get("password", "N/A")),
                "rec_enc": enc(item.get("rec_email", "N/A")),
                "rec_pass_2fa_enc": enc(item.get("rec_pass_2fa", "N/A")),
                "phone_enc": enc(item.get("phone", "N/A"))
            },
            "status": "available",
            "created_at": now(),
            "added_by": m.from_user.id,
        }
        await db.inventory.insert_one(doc)
        added += 1

    await state.clear()
    await processing_msg.delete()

    live_count = await db.inventory.count_documents({"status": "available"})
    sold_count = await db.inventory.count_documents({"status": "sold"})

    report_text = (
        f"🤖 <b>Gemini AI Parsing Complete!</b>\n\n"
        f"✔ <b>{added} accounts added</b> · <b>{skipped} duplicates skipped</b>.\n\n"
        f"🟢 Total Live Stock: <b>{live_count}</b> · 🔴 Sold: <b>{sold_count}</b>"
    )

    await m.answer(report_text, reply_markup=kb([[btn("➕ Add More Stock", "adm:add_choice", "success")], [back("adm:home")]]))


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
        [btn("🗑 Delete This Account", f"adm:id:{item_id}", "danger")],
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
        f"⚠ <b>ARE YOU SURE?</b>\n\n"
        f"You are about to permanently delete <b>{active_count}</b> available Google Voice accounts.\n\n"
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
    await show(c, f"✅ <b>Deleted {res.deleted_count} available accounts!</b>", kb([[back("adm:home")]]))


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

    net_io = psutil.net_io_counters()
    bytes_sent = fmt_bytes(net_io.bytes_sent)
    bytes_recv = fmt_bytes(net_io.bytes_recv)

    pipeline = [{"$match": {"status": "completed"}}, {"$group": {"_id": None, "total": {"$sum": "$amount_cents"}}}]
    rev_res = await db.orders.aggregate(pipeline).to_list(1)
    rev_cents = rev_res[0]["total"] if rev_res else 0

    text = (
        f"📊 <b>Store Analytics</b>\n\n"
        f"👥 Registered Users: <b>{users}</b>\n"
        f"📦 Active In-Stock Items: <b>{active}</b>\n"
        f"🛒 Total Items Sold: <b>{sold}</b>\n"
        f"💰 Total Sales Revenue: <b>{money(rev_cents)}</b>\n\n"
        f"📡 <b>Data Usage:</b>\n"
        f"⬆️ Sent: <b>{bytes_sent}</b> · ⬇️ Recv: <b>{bytes_recv}</b>"
    )
    await show(c, text, kb([[back("adm:home")]]))


@admin_router.callback_query(F.data == "adm:tm")
async def cb_admin_terms(c: CallbackQuery, state: FSMContext):
    terms = await get_setting("terms", getattr(config, "TERMS_TEXT", "Standard terms apply."))
    await state.set_state(TermsSt.text)
    await show(c, f"📝 <b>Edit Terms & Conditions</b>\n\nCurrent terms:\n<i>{terms}</i>\n\nType new text:", kb([[cancel_btn("adm:home")]]))


@admin_router.message(TermsSt.text, F.text)
async def msg_terms_update(m: Message, state: FSMContext):
    await set_setting("terms", m.text.strip())
    await state.clear()
    await m.answer("✅ Terms updated successfully.", reply_markup=kb([[back("adm:home")]]))


# ════════════════════════ OXAPAY WEBHOOK SERVER ════════════════════════


def verify_oxapay_hmac(body_bytes: bytes, hmac_header: Optional[str]) -> bool:
    """Verifies HMAC signature sent by OxaPay webhooks."""
    if not hmac_header:
        return True
    api_key = getattr(config, "OXAPAY_API_KEY", config.OXAPAY_MERCHANT_KEY)
    calculated = hmac.new(api_key.encode('utf-8'), body_bytes, hashlib.sha512).hexdigest()
    return hmac.compare_digest(calculated, hmac_header)


async def handle_oxapay_webhook(request):
    """Processes incoming payment webhooks from OxaPay and fulfills orders automatically."""
    try:
        raw_body = await request.read()
        hmac_hdr = request.headers.get("HMAC") or request.headers.get("X-HMAC-SHA512")
        
        if not verify_oxapay_hmac(raw_body, hmac_hdr):
            log.warning("Unauthorized webhook request received.")
            return web.json_response({"status": "unauthorized"}, status=401)

        data = json.loads(raw_body.decode('utf-8'))
        status = str(data.get("status", "")).lower()
        track_id = str(data.get("trackId", ""))

        if status in ("paid", "completed", "100") and track_id:
            payment = await db.payments.find_one({"track_id": track_id, "status": "pending"})
            if payment:
                await db.payments.update_one({"track_id": track_id}, {"$set": {"status": "completed"}})
                order, items, err = await fulfill_order(
                    payment["user_id"], payment["gv_type"], payment["quantity"], track_id
                )
                if order and items:
                    accounts_str = "\n\n".join([f"<b>Account #{i}:</b>\n{delivery_block(item)}" for i, item in enumerate(items, 1)])
                    delivered_text = (
                        f"✅ <b>Payment Received! Order Delivered!</b>\n\n"
                        f"<b>Order ID:</b> <code>{order['order_id']}</code>\n\n"
                        f"{accounts_str}"
                    )
                    await safe_send(payment["user_id"], text=delivered_text, parse_mode=ParseMode.HTML)
        return web.json_response({"status": "ok"})
    except Exception as e:
        log.error("OxaPay Webhook Error: %s", e)
        return web.json_response({"status": "error"}, status=400)


# ════════════════════════ APPLICATION ENTRY ════════════════════════


async def main():
    global bot_ref
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    await init_db()
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    bot_ref = bot
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.outer_middleware(UserMiddleware())
    dp.callback_query.outer_middleware(UserMiddleware())

    dp.include_router(admin_router)
    dp.include_router(user_router)

    app = web.Application()
    app.router.add_post("/oxapay/callback", handle_oxapay_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", getattr(config, "WEBHOOK_PORT", 8080))
    await site.start()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Bot started with OxaPay Automatic Crypto Processing and Gemini AI Parser.")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        mongo.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
