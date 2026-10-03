"""
Digital product store bot — Aiogram 3.x + MongoDB (Motor) + Manual Crypto Payments.

Features:
- New GV ($4.00) & Old GV ($6.00) selection & automated delivery.
- Manual crypto deposit system with TXN ID & proof photo submission.
- Admin approval/rejection system for top-ups.
- Instant admin notifications via bot for purchases & payment verifications.
- Custom Telegram Premium Emoji integration using config.py.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import logging
import secrets
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
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
_tasks: set = set()

# Fixed Prices in Cents
NEW_GV_PRICE_CENTS = 400  # $4.00
OLD_GV_PRICE_CENTS = 600  # $6.00

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


def back(cb: str = "home", text: str = "⬅️ Back"):
    return btn(text, cb, "danger")


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
        [btn("🛍 View Products", "pl:0", "success")],
        [btn("💰 Wallet", "w", "success"), btn("📦 My Orders", "ol:0", "primary")],
        [btn("💬 Contact Support", "sup", "success"), btn("📜 Terms", "terms", "primary")],
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
    await db.products.create_index("product_id", unique=True)
    await db.inventory.create_index("item_id", unique=True)
    await db.inventory.create_index([("status", A), ("gv_type", A)])
    await db.orders.create_index("order_id", unique=True)
    await db.orders.create_index([("user_id", A), ("created_at", D)])
    await db.payments.create_index("payment_id", unique=True)
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


async def get_balance(user_id: int) -> int:
    w = await db.wallets.find_one({"user_id": user_id})
    return w["balance_cents"] if w else 0


# ═══════════════════════════ INVENTORY & PURCHASES ═══════════════════════════


async def release_item(item_id: str):
    await db.inventory.update_one(
        {"item_id": item_id, "status": "reserved"},
        {"$unset": {"order_id": "", "buyer_id": "", "reserved_at": ""}, "$set": {"status": "available"}},
    )


async def purchase(user_id: int, item_id: str, user_info: str):
    item = await db.inventory.find_one({"item_id": item_id, "status": "available"})
    if not item:
        return None, None, "This item is no longer available."

    cents = item["price_cents"]
    gv_title = "New GV" if item.get("gv_type") == "new" else "Old GV"
    oid = new_id("ORD", 5)
    t = now()

    reserved = await db.inventory.find_one_and_update(
        {"item_id": item_id, "status": "available"},
        {"$set": {"status": "reserved", "order_id": oid, "buyer_id": user_id, "reserved_at": t}},
        return_document=ReturnDocument.AFTER,
    )
    if not reserved:
        return None, None, "Item was snatched by another buyer."

    w = await db.wallets.find_one_and_update(
        {"user_id": user_id, "balance_cents": {"$gte": cents}},
        {"$inc": {"balance_cents": -cents}, "$set": {"updated_at": now()}},
        return_document=ReturnDocument.AFTER,
    )
    if not w:
        await release_item(item_id)
        return None, None, f"Insufficient balance. You need {money(cents)}."

    await db.inventory.update_one({"item_id": item_id}, {"$set": {"status": "sold", "sold_at": t}})
    
    order = {
        "order_id": oid,
        "user_id": user_id,
        "product_name": gv_title,
        "amount_cents": cents,
        "status": "completed",
        "item_id": item_id,
        "created_at": t,
    }
    await db.orders.insert_one(order)

    await db.wallet_transactions.insert_one({
        "tx_id": new_id("TX", 6),
        "user_id": user_id,
        "type": "purchase",
        "amount_cents": -cents,
        "ref_id": oid,
        "note": f"Purchased {gv_title}",
        "applied": True,
        "balance_after_cents": w["balance_cents"],
        "created_at": t,
    })

    # Alert Admins when a GV purchase occurs
    admin_alert = Text(
        CustomEmoji("🛍", custom_emoji_id=config.STORE_EMOJI_ID), " ", Bold("New Product Purchase!"), "\n\n",
        f"<b>Order ID:</b> <code>{oid}</code>\n",
        f"<b>Buyer:</b> {user_info} (ID: <code>{user_id}</code>)\n",
        f"<b>Product:</b> {gv_title}\n",
        f"<b>Price Paid:</b> {money(cents)}\n",
        f"<b>Remaining User Balance:</b> {money(w['balance_cents'])}"
    )
    await notify_admins(content=admin_alert)

    return order, reserved, None


def delivery_block(item: dict) -> str:
    dt = item.get("details", {})
    if dt:
        return (
            "📧 <b>Email:</b> <code>" + esc(dec(dt.get("email_enc", ""))) + "</code>\n"
            "🔑 <b>Password:</b> <code>" + esc(dec(dt.get("pass_enc", ""))) + "</code>\n"
            "🔄 <b>Recovery Email:</b> <code>" + esc(dec(dt.get("rec_enc", ""))) + "</code>\n"
            "📞 <b>Phone Number:</b> <code>" + esc(dec(dt.get("num_enc", ""))) + "</code>"
        )
    return f"🔑 <b>Account Details:</b>\n<code>{esc(dec(item.get('code_enc', '')))}</code>"


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


class ManualDepositSt(StatesGroup):
    currency = State()
    amount = State()
    txn_id = State()
    proof_photo = State()


class AddStockFreshGV(StatesGroup):
    email = State()
    password = State()
    rec_email = State()
    number = State()


class AddStockAgedGV(StatesGroup):
    raw_details = State()


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
    welcome = Text(
        CustomEmoji("🛍", custom_emoji_id=config.STORE_EMOJI_ID), " ",
        Bold(f"Welcome to {config.STORE_NAME}"), "\n\n",
        "Select an option below to buy Google Voice accounts or manage your wallet balance."
    )
    await m.answer(**welcome.as_kwargs(), reply_markup=main_menu(m.from_user.id in ADMIN_SET))


@user_router.callback_query(F.data == "home")
@user_router.callback_query(F.data == "cancel")
async def cb_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    welcome = Text(
        CustomEmoji("🛍", custom_emoji_id=config.STORE_EMOJI_ID), " ",
        Bold(f"Welcome to {config.STORE_NAME}"), "\n\n",
        "Select an option below to browse products or top up your balance."
    )
    await show(c, content=welcome, markup=main_menu(c.from_user.id in ADMIN_SET))


@user_router.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


# ── Products Catalog ──


@user_router.callback_query(F.data.startswith("pl:"))
async def cb_products_list(c: CallbackQuery):
    new_count = await db.inventory.count_documents({"gv_type": "new", "status": "available"})
    old_count = await db.inventory.count_documents({"gv_type": "old", "status": "available"})

    rows = [
        [btn(f"🟢 New GV — {money(NEW_GV_PRICE_CENTS)} (Stock: {new_count})", "gvl:new:0", "success")],
        [btn(f"📜 Old GV — {money(OLD_GV_PRICE_CENTS)} (Stock: {old_count})", "gvl:old:0", "primary")],
        [back()],
    ]
    content = Text(CustomEmoji("📦", custom_emoji_id=config.BOX_EMOJI_ID), " ", Bold("Select Google Voice Category:"))
    await show(c, content=content, markup=kb(rows))


@user_router.callback_query(F.data.startswith("gvl:"))
async def cb_gv_list(c: CallbackQuery):
    _, gv_type, page_str = c.data.split(":")
    page = to_int(page_str)

    docs, page, pages, total = await page_query(
        db.inventory, {"gv_type": gv_type, "status": "available"}, [("created_at", -1)], page, size=PAGE_10
    )

    title = "New GV" if gv_type == "new" else "Old GV"
    if not total:
        return await show(c, f"❌ No stock available for <b>{title}</b>.", kb([[back("pl:0")]]))

    msg_text = f"🛍 <b>{title} Stock List</b>\n\nItems {page * 10 + 1}–{min((page + 1) * 10, total)} of <b>{total}</b>:\nSelect an item to buy."

    rows = []
    for idx, item in enumerate(docs, start=1 + (page * 10)):
        rows.append([btn(f"{title} #{idx} — {money(item['price_cents'])}", f"gvi:{item['item_id']}", "success")])

    rows += pager(f"gvl:{gv_type}", page, pages)
    rows.append([back("pl:0")])
    await show(c, msg_text, kb(rows))


@user_router.callback_query(F.data.startswith("gvi:"))
async def cb_gv_item_view(c: CallbackQuery):
    item_id = c.data.split(":")[1]
    item = await db.inventory.find_one({"item_id": item_id, "status": "available"})
    if not item:
        return await alert(c, "Item is no longer available.")

    bal = await get_balance(c.from_user.id)
    cents = item["price_cents"]
    gv_title = "New GV" if item.get("gv_type") == "new" else "Old GV"

    text = (
        f"🛍 <b>Item Selection: {gv_title}</b>\n\n"
        f"Price: <b>{money(cents)}</b>\n"
        f"Your Wallet Balance: <b>{money(bal)}</b>\n\n"
        "<i>Account credentials will be revealed upon checkout.</i>"
    )

    rows = []
    if bal >= cents:
        rows.append([btn(f"💳 Purchase for {money(cents)}", f"buygv:{item_id}", "success")])
    else:
        text += f"\n\n⚠️ You need <b>{money(cents - bal)}</b> more in your balance."
        rows.append([btn("➕ Top Up Balance", "w", "success")])

    rows.append([back(f"gvl:{item.get('gv_type', 'new')}:0")])
    await show(c, text, kb(rows))


@user_router.callback_query(F.data.startswith("buygv:"))
async def cb_buy_gv(c: CallbackQuery):
    item_id = c.data.split(":")[1]
    await c.answer("Processing order...")

    u_info = f"@{c.from_user.username}" if c.from_user.username else c.from_user.first_name
    order, item, err = await purchase(c.from_user.id, item_id, u_info)
    if err:
        return await show(c, f"❌ {err}", kb([[back("pl:0")]]))

    text = (
        f"✅ <b>Purchase Successful!</b>\n\n"
        f"<b>Order ID:</b> <code>{order['order_id']}</code>\n"
        f"<b>Amount Deducted:</b> {money(order['amount_cents'])}\n\n"
        f"<b>Delivered Account Details:</b>\n"
        f"{delivery_block(item)}"
    )

    rows = [[btn("📦 My Orders", "ol:0", "primary"), btn("🏠 Home", "home", "primary")]]
    await show(c, text, kb(rows))


# ── Wallet Management & Manual Crypto Top-Up ──


@user_router.callback_query(F.data == "w")
async def cb_wallet(c: CallbackQuery, state: FSMContext):
    await state.clear()
    bal = await get_balance(c.from_user.id)
    content = Text(
        CustomEmoji("👛", custom_emoji_id=config.WALLET_EMOJI_ID), " ", Bold("Wallet Management"),
        "\n\nCurrent Balance: ", Bold(money(bal)),
    )
    await show(c, content=content, markup=kb([
        [btn("➕ Top Up Balance", "wd_sel", "success"), btn("📜 Transactions", "wt:0", "primary")],
        [back()],
    ]))


@user_router.callback_query(F.data == "wd_sel")
async def cb_deposit_select_currency(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = []
    
    emoji_map = {
        "USDT_TRC20": config.USDT_EMOJI_ID,
        "USDT_BEP20": config.USDT_EMOJI_ID,
        "USDT_ERC20": config.USDT_EMOJI_ID,
        "BTC": config.BTC_EMOJI_ID,
        "ETH": config.ETH_EMOJI_ID,
        "SOL": config.SOL_EMOJI_ID,
    }

    for curr in config.SUPPORTED_CURRENCIES:
        if curr in config.WALLETS:
            rows.append([btn(f"💵 Pay with {curr}", f"mdept:{curr}", "primary")])

    rows.append([back("w")])
    await show(c, "💳 <b>Select Deposit Payment Currency:</b>", kb(rows))


@user_router.callback_query(F.data.startswith("mdept:"))
async def cb_deposit_currency_chosen(c: CallbackQuery, state: FSMContext):
    curr = c.data.split(":")[1]
    wallet_addr = config.WALLETS.get(curr, "Contact Admin")

    await state.update_data(currency=curr)
    await state.set_state(ManualDepositSt.amount)

    text = (
        f"💵 <b>Deposit via {curr}</b>\n\n"
        f"Send funds to address:\n<code>{wallet_addr}</code>\n\n"
        f"Enter deposit amount in USD (e.g. <code>10.00</code>):"
    )
    await show(c, text, kb([[cancel_btn("w")]]))


@user_router.message(ManualDepositSt.amount, F.text)
async def msg_deposit_amount(m: Message, state: FSMContext):
    cents = parse_money(m.text)
    if cents is None or cents < int(Decimal(config.MIN_DEPOSIT) * 100):
        return await m.answer(f"❌ Invalid amount. Minimum deposit is {config.CURRENCY_SYMBOL}{config.MIN_DEPOSIT}.", reply_markup=kb([[cancel_btn("w")]]))

    await state.update_data(amount_cents=cents)
    await state.set_state(ManualDepositSt.txn_id)

    await m.answer(
        "✏️ Please send/paste the <b>Transaction Hash / TXN ID</b> of your payment:",
        reply_markup=kb([[cancel_btn("w")]])
    )


@user_router.message(ManualDepositSt.txn_id, F.text)
async def msg_deposit_txnid(m: Message, state: FSMContext):
    await state.update_data(txn_id=m.text.strip())
    await state.set_state(ManualDepositSt.proof_photo)

    await m.answer(
        "📸 Please upload a <b>screenshot / photo proof</b> of your completed transaction:",
        reply_markup=kb([[cancel_btn("w")]])
    )


@user_router.message(ManualDepositSt.proof_photo, F.photo)
async def msg_deposit_proof(m: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()

    photo_id = m.photo[-1].file_id
    pid = new_id("DEP", 6)
    cents = data["amount_cents"]
    curr = data["currency"]
    txid = data["txn_id"]
    u_info = f"@{m.from_user.username}" if m.from_user.username else m.from_user.first_name

    deposit_doc = {
        "payment_id": pid,
        "user_id": m.from_user.id,
        "user_info": u_info,
        "amount_cents": cents,
        "currency": curr,
        "txn_id": txid,
        "photo_id": photo_id,
        "status": "pending",
        "created_at": now(),
    }
    await db.payments.insert_one(deposit_doc)

    # Confirm to User
    await m.answer(
        "✅ <b>Deposit Request Submitted!</b>\n\n"
        f"<b>ID:</b> <code>{pid}</code>\n"
        f"<b>Amount:</b> {money(cents)} ({curr})\n\n"
        "Admins will verify your payment and credit your balance shortly.",
        reply_markup=kb([[back("w")]])
    )

    # Dispatch Verification Notification to Admin
    admin_markup = kb([
        [
            btn("✅ Approve", f"adm:app_dep:{pid}", "success"),
            btn("❌ Reject", f"adm:rej_dep:{pid}", "danger")
        ]
    ])

    admin_text = (
        f"💳 <b>New Manual Top-Up Request!</b>\n\n"
        f"<b>Deposit ID:</b> <code>{pid}</code>\n"
        f"<b>User:</b> {u_info} (ID: <code>{m.from_user.id}</code>)\n"
        f"<b>Amount:</b> {money(cents)}\n"
        f"<b>Currency:</b> {curr}\n"
        f"<b>TXN ID:</b> <code>{esc(txid)}</code>"
    )

    for aid in ADMIN_SET:
        try:
            await bot_ref.send_photo(aid, photo=photo_id, caption=admin_text, reply_markup=admin_markup)
        except TelegramAPIError as e:
            log.warning("Failed sending photo to admin %s: %s", aid, e)


@user_router.callback_query(F.data.startswith("wt:"))
async def cb_transactions(c: CallbackQuery):
    page = to_int(c.data.split(":")[1])
    docs, page, pages, total = await page_query(db.wallet_transactions, {"user_id": c.from_user.id}, [("created_at", -1)], page, size=PAGE_10)
    if not total:
        text = "📜 <b>Transaction History</b>\n\nNo records found."
    else:
        lines = [f"{'+' if t['amount_cents'] >= 0 else '−'}{money(abs(t['amount_cents']))} · {esc(t['type'].title())} · {fmt_dt(t['created_at'])}" for t in docs]
        text = "📜 <b>Transaction History</b>\n\n" + "\n".join(lines)
    await show(c, text, kb(pager("wt", page, pages) + [[back("w")]]))


# ── Orders & Info ──


@user_router.callback_query(F.data.startswith("ol:"))
async def cb_orders(c: CallbackQuery):
    page = to_int(c.data.split(":")[1])
    docs, page, pages, total = await page_query(db.orders, {"user_id": c.from_user.id}, [("created_at", -1)], page, size=PAGE_10)
    if not total:
        return await show(c, "📦 <b>My Orders</b>\n\nNo orders placed yet.", kb([[back()]]))

    rows = [[btn(f"{o['order_id']} · {o['product_name']} · {money(o['amount_cents'])}", f"ov:{o['order_id']}", "primary")] for o in docs]
    rows += pager("ol", page, pages)
    rows.append([back()])
    await show(c, f"📦 <b>My Orders</b> (Total: {total})", kb(rows))


@user_router.callback_query(F.data.startswith("ov:"))
async def cb_order_view(c: CallbackQuery):
    oid = c.data.split(":")[1]
    o = await db.orders.find_one({"order_id": oid, "user_id": c.from_user.id})
    if not o:
        return await alert(c, "Order record missing.")

    text = (
        f"📦 <b>Order:</b> <code>{o['order_id']}</code>\n\n"
        f"Product: <b>{esc(o['product_name'])}</b>\n"
        f"Amount Paid: <b>{money(o['amount_cents'])}</b>\n"
        f"Date: {fmt_dt(o['created_at'])}\n\n"
    )

    item = await db.inventory.find_one({"item_id": o.get("item_id")})
    if item:
        text += delivery_block(item)

    await show(c, text, kb([[back("ol:0")]]))


@user_router.callback_query(F.data == "sup")
async def cb_support(c: CallbackQuery, state: FSMContext):
    await state.clear()
    content = Text(
        CustomEmoji("🎧", custom_emoji_id=config.SUPPORT_EMOJI_ID), " ", Bold("Customer Support"),
        "\n\nNeed assistance? Open a direct chat or send a message below.",
    )
    await show(c, content=content, markup=kb([
        [btn("💬 Contact Support", url=f"https://t.me/{config.SUPPORT_USERNAME.lstrip('@')}", style="success")],
        [btn("📨 Direct Message", "supm", "primary")],
        [back()],
    ]))


@user_router.callback_query(F.data == "supm")
async def cb_support_msg(c: CallbackQuery, state: FSMContext):
    await state.set_state(SupportSt.msg)
    await show(c, "📨 Type your support request below:", kb([[cancel_btn("sup")]]))


@user_router.message(SupportSt.msg, F.text)
async def msg_support(m: Message, state: FSMContext):
    await state.clear()
    body = f"💬 Support message from @{m.from_user.username or 'NoUser'} (ID: {m.from_user.id}):\n\n{m.text}"
    await notify_admins(text=body)
    await m.answer("✅ Support team notified.", reply_markup=kb([[back()]]))


@user_router.callback_query(F.data == "terms")
async def cb_terms(c: CallbackQuery):
    terms = await get_setting("terms", config.TERMS_TEXT)
    content = Text(CustomEmoji("📜", custom_emoji_id=config.TERMS_EMOJI_ID), " ", Bold("Terms & Conditions"), "\n\n", terms)
    await show(c, content=content, markup=kb([[back()]]))


# ═══════════════════════════════ ADMIN PANEL ═══════════════════════════════


def admin_menu() -> InlineKeyboardMarkup:
    return kb([
        [btn("➕ Add Stock", "adm:add_choice", "success"), btn("📦 Active Stock", "adm:ai:0", "primary")],
        [btn("🛒 Sold Stock", "adm:ss:0", "primary"), btn("💳 Pending Deposits", "adm:pd:0", "primary")],
        [btn("👥 Users", "adm:us:0", "primary"), btn("📊 Statistics", "adm:st", "primary")],
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


# ── Manual Deposit Approvals ──


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

    # Credit Balance
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

    # Notify User
    content = Text(
        CustomEmoji("💰", custom_emoji_id=config.WALLET_EMOJI_ID), " ", Bold("Deposit Approved!"),
        f"\n\n{money(pay['amount_cents'])} added to your wallet.\nNew Balance: ", Bold(money(w["balance_cents"])),
    )
    await safe_send(pay["user_id"], **content.as_kwargs())

    await show(c, f"✅ Deposit <code>{pid}</code> approved and credited successfully!")


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
    await show(c, f"❌ Deposit <code>{pid}</code> rejected.")


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
        f"Amount: <b>{money(p['amount_cents'])}</b>\n"
        f"Currency: {p['currency']}\n"
        f"TXN ID: <code>{esc(p['txn_id'])}</code>\n"
        f"Status: {p['status']}"
    )

    rows = []
    if p["status"] == "pending":
        rows.append([btn("✅ Approve", f"adm:app_dep:{pid}", "success"), btn("❌ Reject", f"adm:rej_dep:{pid}", "danger")])
    rows.append([back("adm:pd:0")])

    if p.get("photo_id"):
        try:
            await bot_ref.send_photo(c.from_user.id, photo=p["photo_id"], caption=text, reply_markup=kb(rows))
            return
        except TelegramAPIError:
            pass

    await show(c, text, kb(rows))


# ── Stock Management (New GV & Old GV) ──


@admin_router.callback_query(F.data == "adm:add_choice")
async def cb_add_stock_choice(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = [
        [btn(f"🟢 New GV ({money(NEW_GV_PRICE_CENTS)})", "adm:add_new", "success")],
        [btn(f"📜 Old GV ({money(OLD_GV_PRICE_CENTS)})", "adm:add_old", "primary")],
        [back("adm:home")],
    ]
    await show(c, "➕ <b>Select Stock Category to Add:</b>", kb(rows))


@admin_router.callback_query(F.data == "adm:add_new")
async def cb_add_new_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddStockFreshGV.email)
    await show(c, f"➕ <b>Adding New GV ({money(NEW_GV_PRICE_CENTS)})</b>\n\n1️⃣ Enter <b>Email</b>:", kb([[cancel_btn("adm:home")]]))


@admin_router.message(AddStockFreshGV.email, F.text)
async def msg_fresh_email(m: Message, state: FSMContext):
    await state.update_data(email=m.text.strip())
    await state.set_state(AddStockFreshGV.password)
    await m.answer("2️⃣ Enter <b>Password</b>:", reply_markup=kb([[cancel_btn("adm:home")]]))


@admin_router.message(AddStockFreshGV.password, F.text)
async def msg_fresh_pass(m: Message, state: FSMContext):
    await state.update_data(password=m.text.strip())
    await state.set_state(AddStockFreshGV.rec_email)
    await m.answer("3️⃣ Enter <b>Recovery Email</b>:", reply_markup=kb([[cancel_btn("adm:home")]]))


@admin_router.message(AddStockFreshGV.rec_email, F.text)
async def msg_fresh_rec(m: Message, state: FSMContext):
    await state.update_data(rec_email=m.text.strip())
    await state.set_state(AddStockFreshGV.number)
    await m.answer("4️⃣ Enter <b>Phone Number</b>:", reply_markup=kb([[cancel_btn("adm:home")]]))


@admin_router.message(AddStockFreshGV.number, F.text)
async def msg_fresh_num(m: Message, state: FSMContext):
    d = await state.get_data()
    await state.clear()

    item_id = new_id("GVN", 6)
    doc = {
        "item_id": item_id,
        "gv_type": "new",
        "price_cents": NEW_GV_PRICE_CENTS,
        "details": {
            "email_enc": enc(d["email"]),
            "pass_enc": enc(d["password"]),
            "rec_enc": enc(d["rec_email"]),
            "num_enc": enc(m.text.strip()),
        },
        "status": "available",
        "created_at": now(),
        "added_by": m.from_user.id,
    }
    await db.inventory.insert_one(doc)

    await m.answer(
        f"✅ <b>New GV Account Added!</b>\nPrice: {money(NEW_GV_PRICE_CENTS)}\nEmail: <code>{esc(d['email'])}</code>",
        reply_markup=kb([[btn("➕ Add Another New GV", "adm:add_new", "success")], [back("adm:home")]]),
    )


@admin_router.callback_query(F.data == "adm:add_old")
async def cb_add_old_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddStockAgedGV.raw_details)
    await show(c, f"➕ <b>Adding Old GV ({money(OLD_GV_PRICE_CENTS)})</b>\n\nPaste credentials string:", kb([[cancel_btn("adm:home")]]))


@admin_router.message(AddStockAgedGV.raw_details, F.text)
async def msg_aged_details(m: Message, state: FSMContext):
    code = m.text.strip()
    await state.clear()

    item_id = new_id("GVO", 6)
    doc = {
        "item_id": item_id,
        "gv_type": "old",
        "price_cents": OLD_GV_PRICE_CENTS,
        "code_enc": enc(code),
        "status": "available",
        "created_at": now(),
        "added_by": m.from_user.id,
    }
    await db.inventory.insert_one(doc)

    await m.answer(
        f"✅ <b>Old GV Account Listed!</b>\nPrice: {money(OLD_GV_PRICE_CENTS)}",
        reply_markup=kb([[btn("➕ Add Another Old GV", "adm:add_old", "success")], [back("adm:home")]]),
    )


@admin_router.callback_query(F.data.startswith("adm:ai:"))
async def cb_active_stock(c: CallbackQuery):
    page = to_int(c.data.split(":")[2])
    docs, page, pages, total = await page_query(db.inventory, {"status": "available"}, [("created_at", -1)], page, size=PAGE_10)
    rows = [[btn(f"{i['item_id']} · {i.get('gv_type', 'gv').upper()} · {money(i['price_cents'])}", f"adm:ii:{i['item_id']}", "primary")] for i in docs]
    rows += pager("adm:ai", page, pages)
    rows.append([back("adm:home")])
    await show(c, f"📦 <b>Active Inventory</b> ({total} items):", kb(rows))


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
    await show(c, "✅ Item deleted from inventory.", kb([[back("adm:ai:0")]]))


@admin_router.callback_query(F.data.startswith("adm:ss:"))
async def cb_sold_stock(c: CallbackQuery):
    page = to_int(c.data.split(":")[2])
    docs, page, pages, total = await page_query(db.orders, {"status": "completed"}, [("created_at", -1)], page, size=PAGE_10)
    rows = [[btn(f"{o['order_id']} · {o['product_name']} · {money(o['amount_cents'])}", "noop", "primary")] for o in docs]
    rows += pager("adm:ss", page, pages)
    rows.append([back("adm:home")])
    await show(c, f"🛒 <b>Sold Products History</b> ({total}):", kb(rows))


@admin_router.callback_query(F.data.startswith("adm:us:"))
async def cb_admin_users(c: CallbackQuery):
    page = to_int(c.data.split(":")[2])
    docs, page, pages, total = await page_query(db.users, {}, [("created_at", -1)], page, size=PAGE_10)
    rows = [[btn(f"User {u['user_id']} (@{u.get('username', 'N/A')})", "noop", "primary")] for u in docs]
    rows += pager("adm:us", page, pages)
    rows.append([back("adm:home")])
    await show(c, f"👥 <b>Total Registered Users:</b> {total}", kb(rows))


@admin_router.callback_query(F.data == "adm:st")
async def cb_admin_stats(c: CallbackQuery):
    users = await db.users.count_documents({})
    active = await db.inventory.count_documents({"status": "available"})
    sold = await db.inventory.count_documents({"status": "sold"})
    pending_dep = await db.payments.count_documents({"status": "pending"})

    text = (
        f"📊 <b>Store Analytics</b>\n\n"
        f"👥 Registered Users: <b>{users}</b>\n"
        f"📦 Active In-Stock Items: <b>{active}</b>\n"
        f"🛒 Total Items Sold: <b>{sold}</b>\n"
        f"💳 Pending Deposit Verifications: <b>{pending_dep}</b>"
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
        log.info("Bot started successfully in Manual Payment Mode.")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        mongo.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
