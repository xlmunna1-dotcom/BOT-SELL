"""
অটো-সেল/উইথড্র টেলিগ্রাম বট
==============================
চালানোর আগে BOT_TOKEN এবং ADMIN_IDS কনফিগার করুন।
তারপর: python bot.py
"""

import logging
import sqlite3
import io
import json
import requests as _requests
from datetime import datetime

import openpyxl
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters,
)
from telegram.request import HTTPXRequest

# =====================================================================
# ✅ কনফিগারেশন
# =====================================================================
BOT_TOKEN = "8931921915:AAES5ltNU8R3mhVcK-w8qqzcxP8AHimVvFA"
ADMIN_IDS = [8301781046]
# =====================================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Conversation states ───────────────────────────────────────────
(
    WD_CHOOSE_METHOD, WD_ENTER_ACCOUNT, WD_AMOUNT, WD_CONFIRM,
    BC_CHOOSE_TARGET, BC_ENTER_ID, BC_ENTER_MSG,
    SEARCH_ENTER_ID,
    SET_ADMIN_ENTER_ID,
    WD_REJECT_REASON,
    WD_MANUAL_AMOUNT,
    SHEET_SET_2FA,
    SHEET_SET_COOKIES,
) = range(13)

DB_PATH = "bot.db"

# ══════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT DEFAULT '',
            full_name   TEXT DEFAULT '',
            balance     REAL DEFAULT 0,
            nagad       TEXT,
            bkash       TEXT,
            binance     TEXT,
            is_admin    INTEGER DEFAULT 0,
            is_banned   INTEGER DEFAULT 0,
            joined_at   TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS withdrawals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            method      TEXT,
            account     TEXT,
            amount      REAL,
            status      TEXT DEFAULT 'pending',
            reason      TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        INSERT OR IGNORE INTO settings(key,value) VALUES('min_withdraw','100');

        CREATE TABLE IF NOT EXISTS sell_super_users (
            user_id     INTEGER PRIMARY KEY,
            mode        TEXT DEFAULT 'COOKIES',
            enabled     INTEGER DEFAULT 1,
            saved_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS sell_sheet_settings (
            id              INTEGER PRIMARY KEY CHECK(id=1),
            cookies_link    TEXT DEFAULT '',
            twofa_link      TEXT DEFAULT ''
        );
        INSERT OR IGNORE INTO sell_sheet_settings(id) VALUES(1);
    """)
    for aid in ADMIN_IDS:
        c.execute("INSERT OR IGNORE INTO users(user_id,is_admin) VALUES(?,1)", (aid,))
        c.execute("UPDATE users SET is_admin=1 WHERE user_id=?", (aid,))
    conn.commit()
    conn.close()


def upsert_user(user):
    conn = get_conn()
    conn.execute(
        """INSERT INTO users(user_id,username,full_name) VALUES(?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
               username=excluded.username, full_name=excluded.full_name""",
        (user.id, user.username or "", user.full_name),
    )
    conn.commit()
    conn.close()


def get_user(user_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row


def get_setting(key):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def is_admin(user_id):
    r = get_user(user_id)
    return bool(r and r["is_admin"] == 1)


def get_sell_sheet_settings():
    conn = get_conn()
    row = conn.execute("SELECT * FROM sell_sheet_settings WHERE id=1").fetchone()
    conn.close()
    return row


def set_sell_sheet_link(field, value):
    conn = get_conn()
    conn.execute(f"UPDATE sell_sheet_settings SET {field}=? WHERE id=1", (value,))
    conn.commit()
    conn.close()


def upsert_sell_super_user(user_id, mode):
    conn = get_conn()
    conn.execute(
        """INSERT INTO sell_super_users(user_id, mode) VALUES(?,?)
           ON CONFLICT(user_id) DO UPDATE SET mode=excluded.mode, saved_at=datetime('now')""",
        (user_id, mode),
    )
    conn.commit()
    conn.close()


def test_sheet_writable(sheet_url: str) -> dict:
    """Google Sheet edit link চেক করে test row লিখে দেখে।"""
    try:
        # Sheet ID বের করো
        import re
        m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheet_url)
        if not m:
            return {"ok": False, "reason": "Invalid Google Sheet URL"}
        sheet_id = m.group(1)
        # Google Sheets API (no auth) — public edit link দিলে append করা যায়
        # আমরা শুধু URL valid কিনা check করব
        test_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
        r = _requests.get(test_url, timeout=10, allow_redirects=True)
        if r.status_code == 200:
            return {"ok": True, "sheet_id": sheet_id}
        return {"ok": False, "reason": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def append_to_sheet(sheet_link: str, row_data: list) -> bool:
    """
    Google Sheet Apps Script Web App endpoint এ POST করে row append করে।
    Sheet লিংক থেকে sheet_id বের করে Apps Script URL বানায়।
    আপনাকে sheet এ Apps Script deploy করতে হবে (নিচে README তে আছে)।
    """
    try:
        import re
        m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheet_link)
        if not m:
            return False
        sheet_id = m.group(1)
        # Apps Script Web App URL — sheet_id দিয়ে apps script url বানানো হয়
        # এটা আপনার deploy করা web app url হবে — নিচে config এ রাখুন
        apps_script_url = get_setting("apps_script_url") or ""
        if not apps_script_url:
            return False
        payload = {"sheet_id": sheet_id, "row": row_data}
        r = _requests.post(apps_script_url, json=payload, timeout=15)
        return r.status_code == 200
    except Exception:
        return False


def is_banned(user_id):
    r = get_user(user_id)
    return bool(r and r["is_banned"] == 1)


def get_all_admins():
    conn = get_conn()
    rows = conn.execute("SELECT user_id FROM users WHERE is_admin=1").fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


# ══════════════════════════════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════════════════════════════
def user_kb(uid=None):
    rows = [
        [KeyboardButton("👤 MY PROFILE"), KeyboardButton("📊 CHECK DATA")],
        [KeyboardButton("📈 REPORT STATUS"), KeyboardButton("💸 WITHDRAW")],
        [KeyboardButton("🔥 SELL SUPER")],
    ]
    if uid and is_admin(uid):
        rows.append([KeyboardButton("🛡 ADMIN PANEL")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def admin_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📢 BROADCAST"), KeyboardButton("📁 FILE SEND")],
        [KeyboardButton("💰 PAYMENT REQUEST"), KeyboardButton("🔍 SEARCH USER")],
        [KeyboardButton("👥 ALL USER"), KeyboardButton("🛡 SET ADMIN")],
        [KeyboardButton("📡 LIVE DATA"), KeyboardButton("⚙️ SHEET SETT")],
        [KeyboardButton("🔙 BACK TO BOT")],
    ], resize_keyboard=True)


def method_inline():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💚 Nagad",   callback_data="method_nagad"),
            InlineKeyboardButton("🔵 bKash",   callback_data="method_bkash"),
            InlineKeyboardButton("🟡 Binance", callback_data="method_binance"),
        ],
        [InlineKeyboardButton("❌ বাতিল", callback_data="wd_cancel_all")],
    ])


# ══════════════════════════════════════════════════════════════════
# MENU BUTTONS — সব জায়গা থেকে এই বাটনগুলো কাজ করবে
# ══════════════════════════════════════════════════════════════════
ALL_MENU_BUTTONS = [
    "👤 MY PROFILE", "📊 CHECK DATA", "📈 REPORT STATUS",
    "💸 WITHDRAW", "🔥 SELL SUPER",
    "🛡 ADMIN PANEL", "🔙 BACK TO BOT",
    "📢 BROADCAST", "📁 FILE SEND", "💰 PAYMENT REQUEST",
    "🔍 SEARCH USER", "👥 ALL USER", "🛡 SET ADMIN", "📡 LIVE DATA",
    "⚙️ SHEET SETT",
]
# WITHDRAW বাদে সব মেনু বাটন — wd_conv এর entry_point নিজে এটা হ্যান্ডল করবে
_MENU_FILTER_BUTTONS = [b for b in ALL_MENU_BUTTONS if b != "💸 WITHDRAW"]
MENU_FILTER = filters.Regex(
    "^(" + "|".join(b.replace("(","\\(").replace(")","\\)") for b in _MENU_FILTER_BUTTONS) + ")$"
)
# সব বাটনের জন্য আলাদা filter (wd_conv fallback এ WITHDRAW ও দরকার)
FULL_MENU_FILTER = filters.Regex(
    "^(" + "|".join(b.replace("(","\\(").replace(")","\\)") for b in ALL_MENU_BUTTONS) + ")$"
)


async def cancel_and_route(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Conversation চলার সময় যেকোনো মেনু বাটন চাপলে এখানে আসবে।
    State clear করে সঠিক হ্যান্ডলারে পাঠায়।
    """
    ctx.user_data.clear()
    text = update.message.text if update.message else ""
    # WITHDRAW বাটন চাপলে নতুন করে শুরু হবে — আলাদাভাবে ডাকতে হবে
    if text == "💸 WITHDRAW":
        return await withdraw_start(update, ctx)
    await route_message(update, ctx)
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════════════════════════
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    user = update.effective_user
    upsert_user(user)
    if is_banned(user.id):
        await update.message.reply_text("⛔ আপনি ব্যান্ড। বটটি ব্যবহার করতে পারবেন না।")
        return ConversationHandler.END
    admin = is_admin(user.id)
    note = "\n🛡 আপনি অ্যাডমিন — নিচে <b>ADMIN PANEL</b> বাটন পাবেন।" if admin else ""
    await update.message.reply_text(
        f"👋 স্বাগতম, <b>{user.full_name}</b>!{note}\nনিচের মেনু থেকে অপশন বেছে নিন।",
        parse_mode="HTML",
        reply_markup=user_kb(user.id),
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════
# USER FEATURES
# ══════════════════════════════════════════════════════════════════
async def my_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id)
    if not u:
        await update.message.reply_text("ইউজার পাওয়া যায়নি। /start দিন।")
        return
    nagad   = f"<code>{u['nagad']}</code>"   if u["nagad"]   else "❌ বাইন্ড নেই"
    bkash   = f"<code>{u['bkash']}</code>"   if u["bkash"]   else "❌ বাইন্ড নেই"
    binance = f"<code>{u['binance']}</code>" if u["binance"] else "❌ বাইন্ড নেই"
    await update.message.reply_text(
        f"👤 <b>আমার প্রোফাইল</b>\n\n"
        f"🆔 Telegram ID: <code>{u['user_id']}</code>\n"
        f"📛 নাম: {u['full_name']}\n"
        f"🔖 ইউজারনেম: @{u['username'] or 'নেই'}\n\n"
        f"💚 Nagad: {nagad}\n"
        f"🔵 bKash: {bkash}\n"
        f"🟡 Binance: {binance}\n\n"
        f"💰 মোট ব্যালেন্স: <b>{u['balance']:.2f}৳</b>",
        parse_mode="HTML",
    )


async def check_data(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id)
    if not u:
        await update.message.reply_text("ইউজার পাওয়া যায়নি।")
        return
    uid = u["user_id"]
    today = datetime.now().strftime("%Y-%m-%d")

    # Sheet থেকে আজকের COOKIES data count করা হয়
    settings = get_sell_sheet_settings()
    cookies_link = settings["cookies_link"] if settings else ""
    twofa_link   = settings["twofa_link"]   if settings else ""

    # Sell super user mode চেক
    conn = get_conn()
    ss_row = conn.execute(
        "SELECT mode, enabled FROM sell_super_users WHERE user_id=?", (uid,)
    ).fetchone()
    conn.close()

    mode_text = "❌ সেট নেই"
    if ss_row:
        mode_text = f"{'✅' if ss_row['enabled'] else '⏸'} {ss_row['mode']}"

    # আজকের sheet data fetch করার চেষ্টা
    cookies_today = "—"
    twofa_today   = "—"

    apps_url = get_setting("apps_script_url") or ""
    if apps_url and cookies_link:
        try:
            import re
            m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", cookies_link)
            if m:
                r = _requests.post(
                    apps_url,
                    json={"action": "count_today", "sheet_id": m.group(1), "tg_user_id": uid, "date": today},
                    timeout=10,
                )
                data = r.json()
                cookies_today = str(data.get("sold", 0))
                pending_c     = str(data.get("pending", 0))
                fail_c        = str(data.get("fail", 0))
        except Exception:
            cookies_today = "fetch error"
            pending_c = fail_c = "—"
    else:
        pending_c = fail_c = "—"

    if apps_url and twofa_link:
        try:
            import re
            m2 = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", twofa_link)
            if m2:
                r2 = _requests.post(
                    apps_url,
                    json={"action": "count_today", "sheet_id": m2.group(1), "tg_user_id": uid, "date": today},
                    timeout=10,
                )
                d2 = r2.json()
                twofa_today = str(d2.get("sold", 0))
        except Exception:
            twofa_today = "fetch error"

    text = (
        f"📊 <b>CHECK DATA</b>\n"
        f"{'▰'*20}\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"📛 নাম: {u['full_name']}\n"
        f"💰 ব্যালেন্স: <b>{u['balance']:.2f}৳</b>\n"
        f"🔒 স্ট্যাটাস: {'⛔ ব্যান্ড' if u['is_banned'] else '✅ সক্রিয়'}\n\n"
        f"{'▰'*20}\n"
        f"🍪 <b>COOKIES — আজকের রিপোর্ট</b>\n"
        f"✅ Sold    : <b>{cookies_today}</b>\n"
        f"⏳ Pending : <b>{pending_c}</b>\n"
        f"❌ Failed  : <b>{fail_c}</b>\n\n"
        f"🔐 <b>2FA — আজকের Sold</b>\n"
        f"✅ Sold    : <b>{twofa_today}</b>\n\n"
        f"{'▰'*20}\n"
        f"⚙️ SELL SUPER মোড: {mode_text}"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="check_data_refresh"),
            InlineKeyboardButton("🍪 2FA CHECK DATA", callback_data="check_data_2fa"),
        ]
    ])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def check_data_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⏳ লোড হচ্ছে...")
    # Same as check_data but edit message
    u = get_user(q.from_user.id)
    if not u:
        return
    uid = u["user_id"]
    today = datetime.now().strftime("%Y-%m-%d")
    settings = get_sell_sheet_settings()
    cookies_link = settings["cookies_link"] if settings else ""
    twofa_link   = settings["twofa_link"]   if settings else ""
    apps_url = get_setting("apps_script_url") or ""

    is_2fa = (q.data == "check_data_2fa")
    target_link = twofa_link if is_2fa else cookies_link
    label = "🔐 2FA" if is_2fa else "🍪 COOKIES"

    sold = pending = fail = 0
    if apps_url and target_link:
        try:
            import re
            m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", target_link)
            if m:
                r = _requests.post(
                    apps_url,
                    json={"action": "count_today", "sheet_id": m.group(1), "tg_user_id": uid, "date": today},
                    timeout=10,
                )
                d = r.json()
                sold    = d.get("sold", 0)
                pending = d.get("pending", 0)
                fail    = d.get("fail", 0)
        except Exception:
            pass

    text = (
        f"📊 <b>{label} — CHECK DATA</b>\n"
        f"{'▰'*20}\n\n"
        f"📅 তারিখ: <b>{today}</b>\n"
        f"🆔 আপনার ID: <code>{uid}</code>\n\n"
        f"✅ Sold    : <b>{sold}</b>\n"
        f"⏳ Pending : <b>{pending}</b>\n"
        f"❌ Failed  : <b>{fail}</b>\n\n"
        f"{'▰'*20}"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=q.data),
            InlineKeyboardButton("🍪 COOKIES" if is_2fa else "🔐 2FA", callback_data="check_data_2fa" if not is_2fa else "check_data_refresh"),
        ]
    ])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


# ══════════════════════════════════════════════════════════════════
# SELL SUPER
# ══════════════════════════════════════════════════════════════════
async def sell_super(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_banned(uid):
        await update.message.reply_text("⛔ আপনি ব্যান্ড।")
        return

    conn = get_conn()
    ss_row = conn.execute(
        "SELECT mode, enabled FROM sell_super_users WHERE user_id=?", (uid,)
    ).fetchone()
    conn.close()

    current = f"{ss_row['mode']}" if ss_row else "সেট নেই"

    text = (
        f"🔥 <b>SELL SUPER</b>\n"
        f"{'▰'*20}\n\n"
        f"⚙️ বর্তমান মোড: <b>{current}</b>\n\n"
        f"একাউন্ট টাইপ সিলেক্ট করুন:\n\n"
        f"🔐 <b>2FA + COOKIES</b> — 2FA Key সহ Cookie save হবে\n"
        f"🍪 <b>ONLY COOKIES</b> — শুধু Cookie save হবে"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔐 2FA + COOKIES", callback_data="sell_super_2fa"),
            InlineKeyboardButton("🍪 ONLY COOKIES",  callback_data="sell_super_cookies"),
        ],
        [InlineKeyboardButton("❌ বাতিল", callback_data="sell_super_cancel")],
    ])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def sell_super_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    if q.data == "sell_super_cancel":
        await q.edit_message_text("❌ বাতিল করা হয়েছে।")
        return

    mode_map = {
        "sell_super_2fa":     "2FA_COOKIES",
        "sell_super_cookies": "ONLY_COOKIES",
    }
    mode = mode_map.get(q.data)
    if not mode:
        return

    upsert_sell_super_user(uid, mode)
    u = get_user(uid)

    text = (
        f"✅ <b>SELL SUPER সেভ হয়েছে!</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"📛 নাম: {u['full_name'] if u else '—'}\n"
        f"⚙️ মোড: <b>{mode}</b>\n\n"
        f"এখন আপনার app এ MULTI CREATE করলে account গুলো\n"
        f"স্বয়ংক্রিয়ভাবে admin sheet এ সেভ হবে।"
    )
    await q.edit_message_text(text, parse_mode="HTML")


async def report_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📈 <b>রিপোর্ট স্ট্যাটাস</b>\n\nকোনো রিপোর্ট নেই।", parse_mode="HTML")


# ══════════════════════════════════════════════════════════════════
# WITHDRAW FLOW
# ══════════════════════════════════════════════════════════════════
METHOD_LABEL = {"nagad": "💚 Nagad", "bkash": "🔵 bKash", "binance": "🟡 Binance"}
METHOD_GUIDE = {
    "nagad": (
        "💚 <b>Nagad নম্বর দিন</b>\n\n"
        "📌 আপনার Nagad একাউন্টের মোবাইল নম্বর লিখুন।\n"
        "উদাহরণ: <code>01XXXXXXXXX</code>"
    ),
    "bkash": (
        "🔵 <b>bKash নম্বর দিন</b>\n\n"
        "📌 আপনার bKash একাউন্টের মোবাইল নম্বর লিখুন।\n"
        "উদাহরণ: <code>01XXXXXXXXX</code>"
    ),
    "binance": (
        "🟡 <b>Binance UID দিন</b>\n\n"
        "📌 <b>Binance UID কীভাবে পাবেন?</b>\n\n"
        "1️⃣ Binance অ্যাপ ওপেন করুন\n"
        "2️⃣ নিচে <b>Profile</b> আইকনে ট্যাপ করুন\n"
        "3️⃣ আপনার নামের নিচে <b>UID: XXXXXXXXX</b> দেখবেন\n"
        "4️⃣ সেই সংখ্যাটি কপি করে এখানে পেস্ট করুন\n\n"
        "উদাহরণ: <code>123456789</code>"
    ),
}


async def withdraw_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_banned(uid):
        await update.message.reply_text("⛔ আপনি ব্যান্ড।")
        return ConversationHandler.END
    ctx.user_data.clear()
    await update.message.reply_text(
        "💸 <b>পেমেন্ট পদ্ধতি বেছে নিন:</b>\n\n"
        "❌ বাতিল করতে <b>বাতিল</b> বাটন চাপুন অথবা যেকোনো মেনু বাটন চাপুন।",
        parse_mode="HTML",
        reply_markup=method_inline(),
    )
    return WD_CHOOSE_METHOD


async def wd_choose_method(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "wd_cancel_all":
        ctx.user_data.clear()
        await q.edit_message_text("❌ উইথড্র বাতিল।")
        return ConversationHandler.END

    method_map = {"method_nagad": "nagad", "method_bkash": "bkash", "method_binance": "binance"}
    method = method_map.get(q.data)
    if not method:
        return WD_CHOOSE_METHOD

    ctx.user_data["wd_method"] = method
    u = get_user(q.from_user.id)
    bound = u[method]

    if bound:
        ctx.user_data["wd_account"] = bound
        await q.edit_message_text(
            f"✅ বাইন্ড করা {METHOD_LABEL[method]}:\n<code>{bound}</code>\n\nএটি দিয়ে Continue করবেন নাকি Edit করবেন?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Continue", callback_data="wd_continue"),
                    InlineKeyboardButton("✏️ Edit",    callback_data="wd_edit"),
                ],
                [InlineKeyboardButton("❌ বাতিল", callback_data="wd_cancel_all")],
            ]),
        )
        return WD_AMOUNT
    else:
        await q.edit_message_text(METHOD_GUIDE[method], parse_mode="HTML")
        return WD_ENTER_ACCOUNT


async def wd_enter_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    account = update.message.text.strip()
    method  = ctx.user_data.get("wd_method", "nagad")
    uid     = update.effective_user.id

    # মেনু বাটন অ্যাকাউন্ট নম্বর হিসেবে সেভ হওয়া ঠেকানো
    if account in ALL_MENU_BUTTONS:
        ctx.user_data.clear()
        await route_message(update, ctx)
        return ConversationHandler.END

    # Basic validation
    if method in ("nagad", "bkash"):
        if not account.isdigit() or len(account) != 11:
            await update.message.reply_text(
                f"❌ <b>অবৈধ নম্বর!</b>\n\n১১ সংখ্যার মোবাইল নম্বর দিন।\nউদাহরণ: <code>01XXXXXXXXX</code>",
                parse_mode="HTML",
            )
            return WD_ENTER_ACCOUNT
    elif method == "binance":
        if not account.isdigit():
            await update.message.reply_text(
                "❌ <b>অবৈধ UID!</b>\n\nBinance UID শুধু সংখ্যায় হয়।\nউদাহরণ: <code>123456789</code>",
                parse_mode="HTML",
            )
            return WD_ENTER_ACCOUNT

    conn = get_conn()
    conn.execute(f"UPDATE users SET {method}=? WHERE user_id=?", (account, uid))
    conn.commit()
    conn.close()

    ctx.user_data["wd_account"] = account
    u = get_user(uid)
    min_wd = get_setting("min_withdraw") or "100"
    await update.message.reply_text(
        f"✅ <b>সেভ হয়েছে!</b>\n\n"
        f"💵 <b>উইথড্র পরিমাণ লিখুন</b>\n\n"
        f"💰 আপনার ব্যালেন্স: <b>{u['balance']:.2f}৳</b>\n"
        f"📌 সর্বনিম্ন পরিমাণ: <b>{min_wd}৳</b>\n\n"
        f"শুধু সংখ্যা লিখুন। যেমন: <code>500</code>",
        parse_mode="HTML",
    )
    return WD_AMOUNT


async def wd_amount_stage(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Callback: continue / edit / cancel
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        if q.data == "wd_cancel_all":
            ctx.user_data.clear()
            await q.edit_message_text("❌ উইথড্র বাতিল।")
            return ConversationHandler.END
        if q.data == "wd_edit":
            method = ctx.user_data.get("wd_method", "nagad")
            await q.edit_message_text(
                "✏️ নতুন " + METHOD_GUIDE[method].split("\n")[0].replace("দিন","দিন (নতুন)"),
                parse_mode="HTML",
            )
            return WD_ENTER_ACCOUNT
        if q.data == "wd_continue":
            u      = get_user(q.from_user.id)
            min_wd = get_setting("min_withdraw") or "100"
            await q.edit_message_text(
                f"💵 <b>উইথড্র পরিমাণ লিখুন</b>\n\n"
                f"💰 আপনার ব্যালেন্স: <b>{u['balance']:.2f}৳</b>\n"
                f"📌 সর্বনিম্ন পরিমাণ: <b>{min_wd}৳</b>\n\n"
                f"শুধু সংখ্যা লিখুন। যেমন: <code>500</code>",
                parse_mode="HTML",
            )
            return WD_AMOUNT
        return WD_AMOUNT

    # Text: amount
    text = update.message.text.strip()

    # মেনু বাটন চাপলে এখানে আসা উচিত না (fallback আগেই ধরবে),
    # কিন্তু extra safety হিসেবে চেক রাখা হলো
    if text in ALL_MENU_BUTTONS:
        ctx.user_data.clear()
        await route_message(update, ctx)
        return ConversationHandler.END

    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError("zero or negative")
    except ValueError:
        await update.message.reply_text(
            "❌ <b>ভুল ইনপুট!</b>\n\nশুধু ধনাত্মক সংখ্যা লিখুন। যেমন: <code>500</code>",
            parse_mode="HTML",
        )
        return WD_AMOUNT

    uid    = update.effective_user.id
    min_wd = float(get_setting("min_withdraw") or "100")
    u      = get_user(uid)

    if amount < min_wd:
        await update.message.reply_text(
            f"❌ সর্বনিম্ন পরিমাণ <b>{min_wd:.0f}৳</b>।\nআবার লিখুন:",
            parse_mode="HTML",
        )
        return WD_AMOUNT
    if amount > u["balance"]:
        await update.message.reply_text(
            f"❌ অপর্যাপ্ত ব্যালেন্স!\n\n"
            f"💰 আপনার ব্যালেন্স: <b>{u['balance']:.2f}৳</b>\n"
            f"আপনি চাইছেন: <b>{amount:.2f}৳</b>\n\nছোট পরিমাণ লিখুন:",
            parse_mode="HTML",
        )
        return WD_AMOUNT

    ctx.user_data["wd_amount"] = amount
    method  = ctx.user_data["wd_method"]
    account = ctx.user_data["wd_account"]
    await update.message.reply_text(
        f"📋 <b>উইথড্র সারাংশ</b>\n\n"
        f"পদ্ধতি: {METHOD_LABEL[method]}\n"
        f"অ্যাকাউন্ট: <code>{account}</code>\n"
        f"পরিমাণ: <b>{amount:.2f}৳</b>\n\n"
        f"সব তথ্য সঠিক হলে Submit করুন।",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Submit",   callback_data="wd_submit"),
                InlineKeyboardButton("❌ বাতিল", callback_data="wd_cancel_all"),
            ]
        ]),
    )
    return WD_CONFIRM


async def wd_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "wd_cancel_all":
        ctx.user_data.clear()
        await q.edit_message_text("❌ উইথড্র বাতিল।")
        return ConversationHandler.END

    uid     = q.from_user.id
    method  = ctx.user_data["wd_method"]
    account = ctx.user_data["wd_account"]
    amount  = ctx.user_data["wd_amount"]

    conn = get_conn()
    conn.execute(
        "INSERT INTO withdrawals(user_id,method,account,amount) VALUES(?,?,?,?)",
        (uid, method, account, amount),
    )
    wr_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()

    u = get_user(uid)
    notify = (
        f"🔔 <b>নতুন উইথড্র রিকোয়েস্ট!</b>\n\n"
        f"🆔 ইউজার: <code>{uid}</code>\n"
        f"📛 নাম: {u['full_name']}\n"
        f"🔖 @{u['username'] or 'নেই'}\n"
        f"পদ্ধতি: {METHOD_LABEL[method]}\n"
        f"অ্যাকাউন্ট: <code>{account}</code>\n"
        f"পরিমাণ: <b>{amount:.2f}৳</b>"
    )
    admin_buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Complete",  callback_data=f"wr_complete_{wr_id}"),
            InlineKeyboardButton("❌ Rejected", callback_data=f"wr_reject_{wr_id}"),
        ]
    ])
    for aid in get_all_admins():
        try:
            await ctx.bot.send_message(aid, notify, parse_mode="HTML", reply_markup=admin_buttons)
        except Exception:
            pass

    ctx.user_data.clear()
    await q.edit_message_text(
        "✅ <b>রিকোয়েস্ট সাবমিট হয়েছে!</b>\n\nঅ্যাডমিন রিভিউ করলে আপনাকে নোটিফিকেশন দেওয়া হবে।",
        parse_mode="HTML",
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════
# ADMIN: PAYMENT REQUEST CALLBACKS
# ══════════════════════════════════════════════════════════════════
async def payment_request_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM withdrawals WHERE status='pending' ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📭 কোনো পেন্ডিং রিকোয়েস্ট নেই।")
        return
    for row in rows:
        u = get_user(row["user_id"])
        name = u["full_name"] if u else "অজানা"
        text = (
            f"🆔 ইউজার: <code>{row['user_id']}</code> ({name})\n"
            f"পদ্ধতি: {METHOD_LABEL.get(row['method'], row['method'])}\n"
            f"অ্যাকাউন্ট: <code>{row['account']}</code>\n"
            f"পরিমাণ: <b>{row['amount']:.2f}৳</b>\n"
            f"সময়: {row['created_at']}"
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Complete",  callback_data=f"wr_complete_{row['id']}"),
                InlineKeyboardButton("❌ Rejected", callback_data=f"wr_reject_{row['id']}"),
            ]
        ])
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def wr_complete_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    wr_id = int(q.data.split("_")[-1])
    conn  = get_conn()
    row   = conn.execute("SELECT * FROM withdrawals WHERE id=?", (wr_id,)).fetchone()
    if not row or row["status"] != "pending":
        await q.edit_message_text("⚠️ এই রিকোয়েস্ট আগেই প্রসেস হয়েছে।")
        conn.close()
        return
    u = conn.execute("SELECT balance FROM users WHERE user_id=?", (row["user_id"],)).fetchone()
    if not u or u["balance"] < row["amount"]:
        await q.edit_message_text("❌ ইউজারের ব্যালেন্স অপর্যাপ্ত।")
        conn.close()
        return
    conn.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (row["amount"], row["user_id"]))
    conn.execute("UPDATE withdrawals SET status='completed' WHERE id=?", (wr_id,))
    conn.commit()
    conn.close()
    try:
        await ctx.bot.send_message(
            row["user_id"],
            f"✅ <b>WITHDRAW SUCCESS!</b>\n\n"
            f"পরিমাণ: <b>{row['amount']:.2f}৳</b>\n"
            f"পদ্ধতি: {METHOD_LABEL.get(row['method'], row['method'])}\n"
            f"অ্যাকাউন্ট: <code>{row['account']}</code>",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await q.edit_message_text(f"✅ রিকোয়েস্ট #{wr_id} সম্পন্ন হয়েছে।")


async def wr_reject_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return ConversationHandler.END
    wr_id = int(q.data.split("_")[-1])
    conn  = get_conn()
    row   = conn.execute("SELECT * FROM withdrawals WHERE id=?", (wr_id,)).fetchone()
    conn.close()
    if not row or row["status"] != "pending":
        await q.edit_message_text("⚠️ এই রিকোয়েস্ট আগেই প্রসেস হয়েছে।")
        return ConversationHandler.END
    ctx.user_data["reject_wr_id"] = wr_id
    await q.edit_message_text(
        f"❌ রিকোয়েস্ট #{wr_id} রিজেক্ট করছেন।\n\nরিজেক্টের কারণ লিখুন (অথবা Skip করুন):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭ কারণ ছাড়া Skip", callback_data="wr_skip_reason")]
        ]),
    )
    return WD_REJECT_REASON


async def wr_skip_reason(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    wr_id = ctx.user_data.get("reject_wr_id")
    if wr_id:
        await _do_reject(ctx, q, wr_id, reason=None)
    ctx.user_data.clear()
    return ConversationHandler.END


async def wr_reject_reason_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reason = update.message.text.strip()
    if reason in ALL_MENU_BUTTONS:
        ctx.user_data.clear()
        await route_message(update, ctx)
        return ConversationHandler.END
    wr_id = ctx.user_data.get("reject_wr_id")
    if wr_id:
        await _do_reject(ctx, update, wr_id, reason=reason)
    ctx.user_data.clear()
    return ConversationHandler.END


async def _do_reject(ctx, event, wr_id, reason):
    conn = get_conn()
    row  = conn.execute("SELECT * FROM withdrawals WHERE id=?", (wr_id,)).fetchone()
    conn.execute("UPDATE withdrawals SET status='rejected', reason=? WHERE id=?", (reason, wr_id))
    conn.commit()
    conn.close()
    if row:
        msg = f"❌ <b>উইথড্র রিজেক্ট হয়েছে!</b>\nপরিমাণ: {row['amount']:.2f}৳"
        if reason:
            msg += f"\n📝 কারণ: {reason}"
        try:
            await ctx.bot.send_message(row["user_id"], msg, parse_mode="HTML")
        except Exception:
            pass
    text = f"✅ রিকোয়েস্ট #{wr_id} রিজেক্ট করা হয়েছে।"
    if hasattr(event, "edit_message_text"):
        await event.edit_message_text(text)
    else:
        await event.reply_text(text)


# ══════════════════════════════════════════════════════════════════
# ADMIN: BROADCAST
# ══════════════════════════════════════════════════════════════════
async def broadcast_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    ctx.user_data.clear()
    await update.message.reply_text(
        "📢 <b>ব্রডকাস্ট</b>\nকোথায় পাঠাবেন?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("👥 ALL USER",  callback_data="bc_all"),
                InlineKeyboardButton("👤 ONLY USER", callback_data="bc_only"),
            ],
            [InlineKeyboardButton("❌ বাতিল", callback_data="bc_cancel")],
        ]),
    )
    return BC_CHOOSE_TARGET


async def bc_target_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "bc_cancel":
        ctx.user_data.clear()
        await q.edit_message_text("❌ ব্রডকাস্ট বাতিল।")
        return ConversationHandler.END
    ctx.user_data["bc_target"] = q.data
    if q.data == "bc_all":
        await q.edit_message_text("📝 সবাইকে পাঠানোর মেসেজ লিখুন:")
        return BC_ENTER_MSG
    else:
        await q.edit_message_text("🆔 কোন ইউজারের Telegram ID লিখুন:")
        return BC_ENTER_ID


async def bc_enter_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if raw in ALL_MENU_BUTTONS:
        ctx.user_data.clear()
        await route_message(update, ctx)
        return ConversationHandler.END
    try:
        tid = int(raw)
    except ValueError:
        await update.message.reply_text("❌ সঠিক Telegram ID লিখুন (শুধু সংখ্যা):")
        return BC_ENTER_ID
    u = get_user(tid)
    if not u:
        await update.message.reply_text("❌ এই ID-তে কোনো ইউজার নেই। আবার চেষ্টা করুন:")
        return BC_ENTER_ID
    ctx.user_data["bc_target_id"] = tid
    await update.message.reply_text(
        f"ইউজার: <b>{u['full_name']}</b> (<code>{tid}</code>)\n📝 পাঠানোর মেসেজ লিখুন:",
        parse_mode="HTML",
    )
    return BC_ENTER_MSG


async def bc_send_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text.strip()
    if msg in ALL_MENU_BUTTONS:
        ctx.user_data.clear()
        await route_message(update, ctx)
        return ConversationHandler.END
    target = ctx.user_data.get("bc_target")
    if target == "bc_all":
        conn  = get_conn()
        users = conn.execute("SELECT user_id FROM users WHERE is_banned=0").fetchall()
        conn.close()
        sent = 0
        for u in users:
            try:
                await ctx.bot.send_message(u["user_id"], msg)
                sent += 1
            except Exception:
                pass
        await update.message.reply_text(f"✅ {sent} জনকে মেসেজ পাঠানো হয়েছে।")
    else:
        tid = ctx.user_data.get("bc_target_id")
        try:
            await ctx.bot.send_message(tid, msg)
            await update.message.reply_text("✅ মেসেজ পাঠানো হয়েছে।")
        except Exception:
            await update.message.reply_text("❌ মেসেজ পাঠানো যায়নি।")
    ctx.user_data.clear()
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════
# ADMIN: SEARCH USER
# ══════════════════════════════════════════════════════════════════
async def search_user_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    ctx.user_data.clear()
    await update.message.reply_text("🔍 ইউজারের Telegram ID লিখুন:")
    return SEARCH_ENTER_ID


async def search_user_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if raw in ALL_MENU_BUTTONS:
        ctx.user_data.clear()
        await route_message(update, ctx)
        return ConversationHandler.END
    try:
        tid = int(raw)
    except ValueError:
        await update.message.reply_text("❌ সঠিক Telegram ID লিখুন (শুধু সংখ্যা):")
        return SEARCH_ENTER_ID
    u = get_user(tid)
    if not u:
        await update.message.reply_text("❌ এই ID-তে কোনো ইউজার নেই।")
        return ConversationHandler.END
    nagad   = u["nagad"]   or "নেই"
    bkash   = u["bkash"]   or "নেই"
    binance = u["binance"] or "নেই"
    text = (
        f"👤 <b>ইউজার তথ্য</b>\n\n"
        f"🆔 ID: <code>{u['user_id']}</code>\n"
        f"📛 নাম: {u['full_name']}\n"
        f"🔖 @{u['username'] or 'নেই'}\n"
        f"💰 ব্যালেন্স: <b>{u['balance']:.2f}৳</b>\n"
        f"💚 Nagad: {nagad}\n"
        f"🔵 bKash: {bkash}\n"
        f"🟡 Binance: {binance}\n"
        f"🔒 স্ট্যাটাস: {'⛔ ব্যান্ড' if u['is_banned'] else '✅ সক্রিয়'}\n"
        f"🛡 অ্যাডমিন: {'হ্যাঁ' if u['is_admin'] else 'না'}"
    )
    ban_lbl  = "✅ Unban" if u["is_banned"] else "⛔ Ban"
    ban_data = f"unban_{tid}" if u["is_banned"] else f"ban_{tid}"
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💸 Withdraw",  callback_data=f"admin_wd_{tid}"),
            InlineKeyboardButton(ban_lbl, callback_data=ban_data),
        ],
        [InlineKeyboardButton("👥 ALL USER", callback_data="go_all_user")],
    ])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════
# ADMIN: ALL USER
# ══════════════════════════════════════════════════════════════════
async def all_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    conn  = get_conn()
    count = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    conn.close()
    await update.message.reply_text(
        f"👥 মোট ইউজার: <b>{count}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 USER LIST XLSX", callback_data="user_list_xlsx")]
        ]),
    )


async def user_list_xlsx_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⏳ তৈরি হচ্ছে...")
    await q.edit_message_text("⏳ XLSX তৈরি হচ্ছে...")
    conn = get_conn()
    rows = conn.execute("SELECT * FROM users ORDER BY joined_at DESC").fetchall()
    conn.close()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Users"
    ws.append(["ID", "Username", "নাম", "ব্যালেন্স", "Nagad", "bKash", "Binance", "ব্যান", "অ্যাডমিন", "যোগদান"])
    for r in rows:
        ws.append([
            r["user_id"], r["username"], r["full_name"], r["balance"],
            r["nagad"], r["bkash"], r["binance"],
            "হ্যাঁ" if r["is_banned"] else "না",
            "হ্যাঁ" if r["is_admin"]  else "না",
            r["joined_at"],
        ])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    await ctx.bot.send_document(
        q.from_user.id,
        document=buf,
        filename=f"users_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
        caption=f"✅ ইউজার লিস্ট — মোট {len(rows)} জন",
    )
    await q.edit_message_text(f"✅ XLSX পাঠানো হয়েছে। মোট {len(rows)} জন।")


async def go_all_user_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    conn  = get_conn()
    count = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    conn.close()
    await q.edit_message_text(
        f"👥 মোট ইউজার: <b>{count}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 USER LIST XLSX", callback_data="user_list_xlsx")]
        ]),
    )


# ══════════════════════════════════════════════════════════════════
# ADMIN: SET ADMIN
# ══════════════════════════════════════════════════════════════════
async def set_admin_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    ctx.user_data.clear()
    await update.message.reply_text("🛡 যে ইউজারকে অ্যাডমিন বানাবেন তার Telegram ID লিখুন:")
    return SET_ADMIN_ENTER_ID


async def set_admin_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    if raw in ALL_MENU_BUTTONS:
        ctx.user_data.clear()
        await route_message(update, ctx)
        return ConversationHandler.END
    try:
        tid = int(raw)
    except ValueError:
        await update.message.reply_text("❌ সঠিক Telegram ID লিখুন:")
        return SET_ADMIN_ENTER_ID
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO users(user_id,is_admin) VALUES(?,1)", (tid,))
    conn.execute("UPDATE users SET is_admin=1 WHERE user_id=?", (tid,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ <code>{tid}</code> কে অ্যাডমিন বানানো হয়েছে।", parse_mode="HTML")
    try:
        await ctx.bot.send_message(
            tid,
            "🛡 আপনাকে অ্যাডমিন বানানো হয়েছে!\nনিচে <b>ADMIN PANEL</b> বাটন থেকে অ্যাডমিন মেনুতে যান।",
            parse_mode="HTML",
            reply_markup=user_kb(tid),
        )
    except Exception:
        pass
    ctx.user_data.clear()
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════
# ADMIN: BAN/UNBAN & MANUAL WITHDRAW CALLBACKS
# ══════════════════════════════════════════════════════════════════
async def ban_unban_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    action, tid = q.data.split("_", 1)
    tid = int(tid)
    conn = get_conn()
    if action == "ban":
        conn.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (tid,))
        msg = f"⛔ ইউজার <code>{tid}</code> ব্যান করা হয়েছে।"
    else:
        conn.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (tid,))
        msg = f"✅ ইউজার <code>{tid}</code> আনব্যান করা হয়েছে।"
    conn.commit()
    conn.close()
    await q.edit_message_text(msg, parse_mode="HTML")


async def admin_wd_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tid = int(q.data.split("_")[-1])
    ctx.user_data["admin_wd_target"] = tid
    u = get_user(tid)
    await q.edit_message_text(
        f"💸 ইউজার <code>{tid}</code> ({u['full_name'] if u else '?'}) এর জন্য কত টাকা ব্যালেন্স দিতে/কাটতে চান?\n\n"
        f"যোগ করতে: <code>+500</code>\nকাটতে: <code>-500</code>",
        parse_mode="HTML",
    )
    return WD_MANUAL_AMOUNT


async def admin_wd_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text in ALL_MENU_BUTTONS:
        ctx.user_data.clear()
        await route_message(update, ctx)
        return ConversationHandler.END

    try:
        amount = float(text)
    except ValueError:
        await update.message.reply_text("❌ সংখ্যা লিখুন। যোগ: +500, কাটা: -500")
        return WD_MANUAL_AMOUNT
    tid = ctx.user_data.get("admin_wd_target")
    u   = get_user(tid)
    if not u:
        await update.message.reply_text("❌ ইউজার নেই।")
        return ConversationHandler.END
    new_bal = u["balance"] + amount
    if new_bal < 0:
        await update.message.reply_text(f"❌ ব্যালেন্স {u['balance']:.2f}৳ — এতটা কাটা যাবে না।")
        return WD_MANUAL_AMOUNT
    conn = get_conn()
    conn.execute("UPDATE users SET balance=? WHERE user_id=?", (new_bal, tid))
    conn.commit()
    conn.close()
    sign = "+" if amount >= 0 else ""
    await update.message.reply_text(
        f"✅ ইউজার <code>{tid}</code> এর ব্যালেন্স আপডেট:\n{sign}{amount:.2f}৳ → নতুন ব্যালেন্স: <b>{new_bal:.2f}৳</b>",
        parse_mode="HTML",
    )
    try:
        await ctx.bot.send_message(
            tid,
            f"💰 আপনার ব্যালেন্স আপডেট হয়েছে: {sign}{amount:.2f}৳\nনতুন ব্যালেন্স: <b>{new_bal:.2f}৳</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass
    ctx.user_data.clear()
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════
# ADMIN: FILE SEND / LIVE DATA
# ══════════════════════════════════════════════════════════════════
async def file_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("📁 <b>FILE SEND</b> — আপাতত বন্ধ।", parse_mode="HTML")


async def live_data(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    settings = get_sell_sheet_settings()
    cookies_link = settings["cookies_link"] if settings else ""
    twofa_link   = settings["twofa_link"]   if settings else ""
    apps_url     = get_setting("apps_script_url") or ""

    def fetch_stats(link):
        if not apps_url or not link:
            return None
        try:
            import re
            m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", link)
            if not m:
                return None
            r = _requests.post(
                apps_url,
                json={"action": "count_all_today", "sheet_id": m.group(1), "date": today},
                timeout=10,
            )
            return r.json()
        except Exception:
            return None

    c_data = fetch_stats(cookies_link)
    t_data = fetch_stats(twofa_link)

    def row(d):
        if not d:
            return "❓ Sheet লিংক সেট নেই বা fetch ব্যর্থ"
        return (
            f"  ✅ Sold    : <b>{d.get('sold', 0)}</b>\n"
            f"  ⏳ Pending : <b>{d.get('pending', 0)}</b>\n"
            f"  ❌ Failed  : <b>{d.get('fail', 0)}</b>\n"
            f"  👥 Users   : <b>{d.get('users', 0)}</b>"
        )

    text = (
        f"📡 <b>LIVE DATA — আজকের রিপোর্ট</b>\n"
        f"{'▰'*22}\n"
        f"📅 তারিখ: <b>{today}</b>\n\n"
        f"🍪 <b>COOKIES SHEET</b>\n{row(c_data)}\n\n"
        f"🔐 <b>2FA SHEET</b>\n{row(t_data)}\n"
        f"{'▰'*22}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="live_data_refresh")]
    ])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def live_data_refresh_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("⛔ অ্যাক্সেস নেই", show_alert=True)
        return
    await q.answer("⏳ লোড হচ্ছে...")
    today = datetime.now().strftime("%Y-%m-%d")
    settings = get_sell_sheet_settings()
    cookies_link = settings["cookies_link"] if settings else ""
    twofa_link   = settings["twofa_link"]   if settings else ""
    apps_url     = get_setting("apps_script_url") or ""

    def fetch_stats(link):
        if not apps_url or not link:
            return None
        try:
            import re
            m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", link)
            if not m:
                return None
            r = _requests.post(
                apps_url,
                json={"action": "count_all_today", "sheet_id": m.group(1), "date": today},
                timeout=10,
            )
            return r.json()
        except Exception:
            return None

    c_data = fetch_stats(cookies_link)
    t_data = fetch_stats(twofa_link)

    def row(d):
        if not d:
            return "❓ Sheet লিংক সেট নেই বা fetch ব্যর্থ"
        return (
            f"  ✅ Sold    : <b>{d.get('sold', 0)}</b>\n"
            f"  ⏳ Pending : <b>{d.get('pending', 0)}</b>\n"
            f"  ❌ Failed  : <b>{d.get('fail', 0)}</b>\n"
            f"  👥 Users   : <b>{d.get('users', 0)}</b>"
        )

    text = (
        f"📡 <b>LIVE DATA — আজকের রিপোর্ট</b>\n"
        f"{'▰'*22}\n"
        f"📅 তারিখ: <b>{today}</b>\n\n"
        f"🍪 <b>COOKIES SHEET</b>\n{row(c_data)}\n\n"
        f"🔐 <b>2FA SHEET</b>\n{row(t_data)}\n"
        f"{'▰'*22}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="live_data_refresh")]
    ])
    await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


# ══════════════════════════════════════════════════════════════════
# ADMIN: SHEET SETT
# Flow: ⚙️ SHEET SETT বাটন → মেনু দেখাবে
#        → 🍪 ONLY COOKIES বা 🔐 2FA COOKIES বাটন ক্লিক
#        → লিংক দিন
#        → চেক করবে edit হচ্ছে কিনা
#        → ✅ Done বা ❌ Error দেখাবে
# ══════════════════════════════════════════════════════════════════

def _short_link(link: str) -> str:
    """লিংক ছোট করে দেখাই।"""
    if not link or link == "❌ সেট নেই":
        return "❌ সেট নেই"
    return link[:55] + "..." if len(link) > 55 else link


async def sheet_sett(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """⚙️ SHEET SETT বাটনে এই মেনু আসবে।"""
    if not is_admin(update.effective_user.id):
        return
    settings = get_sell_sheet_settings()
    c_link = settings["cookies_link"] if settings and settings["cookies_link"] else "❌ সেট নেই"
    t_link = settings["twofa_link"]   if settings and settings["twofa_link"]   else "❌ সেট নেই"

    c_status = "✅ সেট আছে" if (settings and settings["cookies_link"]) else "❌ সেট নেই"
    t_status = "✅ সেট আছে" if (settings and settings["twofa_link"])   else "❌ সেট নেই"

    text = (
        f"⚙️ <b>SHEET LINK সেটিংস</b>\n"
        f"{'━'*22}\n\n"
        f"🍪 <b>ONLY COOKIES SHEET</b>\n"
        f"   স্ট্যাটাস: {c_status}\n"
        f"   <code>{_short_link(c_link)}</code>\n\n"
        f"🔐 <b>2FA COOKIES SHEET</b>\n"
        f"   স্ট্যাটাস: {t_status}\n"
        f"   <code>{_short_link(t_link)}</code>\n\n"
        f"কোন sheet লিংক সেট করবেন?"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🍪 ONLY COOKIES", callback_data="sheet_set_cookies"),
            InlineKeyboardButton("🔐 2FA COOKIES",  callback_data="sheet_set_2fa"),
        ]
    ])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def sheet_sett_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """ONLY COOKIES বা 2FA COOKIES বাটন ক্লিক করলে লিংক চাইবে।"""
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return

    if q.data == "sheet_set_cookies":
        ctx.user_data["sheet_set_field"] = "cookies_link"
        await q.edit_message_text(
            "🍪 <b>ONLY COOKIES — Sheet লিংক দিন</b>\n\n"
            "Google Sheet এর লিংক paste করুন:\n\n"
            "<i>উদাহরণ:</i>\n"
            "<code>https://docs.google.com/spreadsheets/d/XXXX/edit</code>\n\n"
            "⚠️ Sheet টি <b>Anyone with link → Editor</b> করা থাকতে হবে।",
            parse_mode="HTML",
        )
        return SHEET_SET_COOKIES

    if q.data == "sheet_set_2fa":
        ctx.user_data["sheet_set_field"] = "twofa_link"
        await q.edit_message_text(
            "🔐 <b>2FA COOKIES — Sheet লিংক দিন</b>\n\n"
            "Google Sheet এর লিংক paste করুন:\n\n"
            "<i>উদাহরণ:</i>\n"
            "<code>https://docs.google.com/spreadsheets/d/XXXX/edit</code>\n\n"
            "⚠️ Sheet টি <b>Anyone with link → Editor</b> করা থাকতে হবে।",
            parse_mode="HTML",
        )
        return SHEET_SET_2FA


async def sheet_set_link_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """লিংক receive করে check করে save করে।"""
    link = update.message.text.strip()

    # মেনু বাটন চাপলে cancel
    if link in ALL_MENU_BUTTONS:
        ctx.user_data.clear()
        await route_message(update, ctx)
        return ConversationHandler.END

    field = ctx.user_data.get("sheet_set_field", "cookies_link")
    label = "🍪 ONLY COOKIES" if field == "cookies_link" else "🔐 2FA COOKIES"

    # checking message
    checking_msg = await update.message.reply_text(
        f"⏳ <b>{label} Sheet চেক করা হচ্ছে...</b>\n"
        f"<i>Sheet access verify করছি, একটু অপেক্ষা করুন।</i>",
        parse_mode="HTML",
    )

    result = test_sheet_writable(link)

    if result["ok"]:
        set_sell_sheet_link(field, link)
        await checking_msg.edit_text(
            f"✅ <b>Done!</b>\n\n"
            f"{'━'*20}\n"
            f"📋 <b>Sheet:</b> {label}\n"
            f"🆔 <b>Sheet ID:</b> <code>{result['sheet_id']}</code>\n"
            f"🔗 <b>লিংক সেভ হয়েছে ✅</b>\n"
            f"{'━'*20}\n\n"
            f"এখন থেকে {label} এর সব data এই sheet এ যাবে।",
            parse_mode="HTML",
        )
    else:
        await checking_msg.edit_text(
            f"❌ <b>Sheet চেক ব্যর্থ হয়েছে!</b>\n\n"
            f"{'━'*20}\n"
            f"📋 <b>Sheet:</b> {label}\n"
            f"⚠️ <b>কারণ:</b> {result['reason']}\n"
            f"{'━'*20}\n\n"
            f"<b>সমাধান:</b>\n"
            f"1️⃣ Sheet খুলুন → Share ক্লিক করুন\n"
            f"2️⃣ <i>Anyone with the link</i> → <b>Editor</b> করুন\n"
            f"3️⃣ আবার লিংক পাঠান।",
            parse_mode="HTML",
        )

    ctx.user_data.clear()
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════
# MAIN MESSAGE ROUTER
# ══════════════════════════════════════════════════════════════════
async def route_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    uid  = update.effective_user.id
    adm  = is_admin(uid)

    if text == "👤 MY PROFILE":
        await my_profile(update, ctx)
    elif text == "📊 CHECK DATA":
        await check_data(update, ctx)
    elif text == "📈 REPORT STATUS":
        await report_status(update, ctx)
    elif text == "🛡 ADMIN PANEL":
        if not adm:
            await update.message.reply_text("⛔ অ্যাক্সেস নেই।")
            return
        await update.message.reply_text("🛡 <b>অ্যাডমিন প্যানেলে স্বাগতম!</b>", parse_mode="HTML", reply_markup=admin_kb())
    elif text == "🔙 BACK TO BOT":
        await update.message.reply_text("✅ ইউজার বটে ফিরে এলেন।", reply_markup=user_kb(uid))
    elif text == "📢 BROADCAST" and adm:
        return await broadcast_start(update, ctx)
    elif text == "📁 FILE SEND" and adm:
        await file_send(update, ctx)
    elif text == "💰 PAYMENT REQUEST" and adm:
        await payment_request_menu(update, ctx)
    elif text == "🔍 SEARCH USER" and adm:
        return await search_user_start(update, ctx)
    elif text == "👥 ALL USER" and adm:
        await all_user(update, ctx)
    elif text == "🛡 SET ADMIN" and adm:
        return await set_admin_start(update, ctx)
    elif text == "🔥 SELL SUPER":
        await sell_super(update, ctx)
    elif text == "📡 LIVE DATA" and adm:
        await live_data(update, ctx)
    elif text == "⚙️ SHEET SETT" and adm:
        await sheet_sett(update, ctx)


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    init_db()
    request = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=30, write_timeout=30,
        connect_timeout=30, pool_timeout=30,
    )
    app = Application.builder().token(BOT_TOKEN).request(request).build()

    # ─── সব conversation-এ সব মেনু বাটন fallback হিসেবে ───
    menu_fallback      = MessageHandler(MENU_FILTER,      cancel_and_route)
    full_menu_fallback = MessageHandler(FULL_MENU_FILTER, cancel_and_route)

    # ── Withdraw ──
    wd_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💸 WITHDRAW$"), withdraw_start)],
        states={
            WD_CHOOSE_METHOD: [CallbackQueryHandler(wd_choose_method)],
            WD_ENTER_ACCOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, wd_enter_account)],
            WD_AMOUNT: [
                CallbackQueryHandler(wd_amount_stage, pattern="^(wd_continue|wd_edit|wd_cancel_all)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wd_amount_stage),
            ],
            WD_CONFIRM: [CallbackQueryHandler(wd_confirm, pattern="^(wd_submit|wd_cancel_all)$")],
        },
        fallbacks=[CommandHandler("start", start), full_menu_fallback],
        per_message=False, allow_reentry=True,
    )

    # ── Broadcast ──
    bc_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📢 BROADCAST$"), broadcast_start)],
        states={
            BC_CHOOSE_TARGET: [CallbackQueryHandler(bc_target_cb, pattern="^bc_(all|only|cancel)$")],
            BC_ENTER_ID:  [MessageHandler(filters.TEXT & ~filters.COMMAND, bc_enter_id)],
            BC_ENTER_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, bc_send_msg)],
        },
        fallbacks=[CommandHandler("start", start), menu_fallback],
        per_message=False, allow_reentry=True,
    )

    # ── Search User ──
    search_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🔍 SEARCH USER$"), search_user_start)],
        states={
            SEARCH_ENTER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_user_id)],
        },
        fallbacks=[CommandHandler("start", start), menu_fallback],
        per_message=False, allow_reentry=True,
    )

    # ── Set Admin ──
    set_admin_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🛡 SET ADMIN$"), set_admin_start)],
        states={
            SET_ADMIN_ENTER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_admin_id)],
        },
        fallbacks=[CommandHandler("start", start), menu_fallback],
        per_message=False, allow_reentry=True,
    )

    # ── Reject Reason ──
    reject_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(wr_reject_start, pattern="^wr_reject_")],
        states={
            WD_REJECT_REASON: [
                CallbackQueryHandler(wr_skip_reason, pattern="^wr_skip_reason$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wr_reject_reason_text),
            ],
        },
        fallbacks=[CommandHandler("start", start), menu_fallback],
        per_message=False, allow_reentry=True,
    )

    # ── Admin Manual Balance ──
    admin_wd_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_wd_cb, pattern="^admin_wd_")],
        states={
            WD_MANUAL_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_wd_amount)],
        },
        fallbacks=[CommandHandler("start", start), menu_fallback],
        per_message=False, allow_reentry=True,
    )

    # ── Sheet SETT conversation ──
    sheet_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(sheet_sett_cb, pattern="^sheet_set_(cookies|2fa)$")],
        states={
            SHEET_SET_COOKIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, sheet_set_link_text)],
            SHEET_SET_2FA:     [MessageHandler(filters.TEXT & ~filters.COMMAND, sheet_set_link_text)],
        },
        fallbacks=[CommandHandler("start", start), menu_fallback],
        per_message=False, allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(wd_conv)
    app.add_handler(bc_conv)
    app.add_handler(search_conv)
    app.add_handler(set_admin_conv)
    app.add_handler(reject_conv)
    app.add_handler(admin_wd_conv)
    app.add_handler(sheet_conv)

    app.add_handler(CallbackQueryHandler(wr_complete_cb,       pattern="^wr_complete_"))
    app.add_handler(CallbackQueryHandler(ban_unban_cb,          pattern="^(ban|unban)_"))
    app.add_handler(CallbackQueryHandler(user_list_xlsx_cb,     pattern="^user_list_xlsx$"))
    app.add_handler(CallbackQueryHandler(go_all_user_cb,        pattern="^go_all_user$"))
    app.add_handler(CallbackQueryHandler(sell_super_cb,         pattern="^sell_super_"))
    app.add_handler(CallbackQueryHandler(check_data_cb,         pattern="^check_data_"))
    app.add_handler(CallbackQueryHandler(live_data_refresh_cb,  pattern="^live_data_refresh$"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_message))

    global _tg_app
    _tg_app = app

    # Internal API server (Mainconnect.py এর call receive করার জন্য)
    _threading.Thread(target=_run_internal_api, daemon=True).start()

    logger.info("✅ বট চালু হচ্ছে...")
    app.run_polling(drop_pending_updates=True)


# ══════════════════════════════════════════════════════════════════
# INTERNAL API — /api/user_connected  (Mainconnect.py থেকে call আসে)
# bot.py কে aiohttp দিয়ে mini server হিসেবে চালান অথবা
# নিচের handle_user_connected কে একটা আলাদা Flask thread এ রাখুন
# ══════════════════════════════════════════════════════════════════
import threading as _threading
from http.server import HTTPServer, BaseHTTPRequestHandler

INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "supersecret123")
_tg_app: "Application | None" = None   # main() এ set হবে


class _InternalHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass   # access log নিরব রাখো

    def do_POST(self):
        if self.path != "/api/user_connected":
            self.send_response(404); self.end_headers(); return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self.send_response(400); self.end_headers(); return

        secret = body.get("secret", "")
        if secret != INTERNAL_SECRET:
            self.send_response(403); self.end_headers(); return

        tg_id       = str(body.get("tg_id", ""))
        tg_username = body.get("tg_username", "")
        tg_name     = body.get("tg_name", "")
        key         = body.get("key", "")

        if tg_id and _tg_app:
            # sell_super_users table এ ইউজার নেই হলে add করো (default ONLY_COOKIES)
            conn = get_conn()
            existing = conn.execute(
                "SELECT user_id FROM sell_super_users WHERE user_id=?", (int(tg_id),)
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT OR IGNORE INTO sell_super_users(user_id, mode) VALUES(?,?)",
                    (int(tg_id), "ONLY_COOKIES")
                )
                conn.commit()
            conn.close()

            # Admin দের notify করো
            msg = (
                f"🔔 <b>নতুন App Login!</b>\n\n"
                f"🆔 TG ID: <code>{tg_id}</code>\n"
                f"👤 নাম: {tg_name or '—'}\n"
                f"🔖 @{tg_username or 'নেই'}\n"
                f"🔑 Key: <code>{key[:20]}…</code>"
            )
            import asyncio as _asyncio

            async def _send():
                for aid in get_all_admins():
                    try:
                        await _tg_app.bot.send_message(aid, msg, parse_mode="HTML")
                    except Exception:
                        pass

            try:
                loop = _asyncio.get_event_loop()
                if loop.is_running():
                    _asyncio.run_coroutine_threadsafe(_send(), loop)
                else:
                    loop.run_until_complete(_send())
            except Exception:
                pass

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')


def _run_internal_api():
    port = int(os.environ.get("SELL_BOT_API_PORT", "8765"))
    server = HTTPServer(("0.0.0.0", port), _InternalHandler)
    print(f"[InternalAPI] Listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
