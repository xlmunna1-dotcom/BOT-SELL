import asyncio
import logging
import time
import sqlite3
import threading
import re
import requests
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)
from telegram.request import HTTPXRequest

# ==========================================
# ⚙️ CONFIGURATION
# ==========================================
BOT_TOKEN   = "9096281814:AAEtcBkdO9xC_isPdplICKM78VTUKK5RO8Y"
ADMIN_IDS   = [8301781046]
OTP_GROUP_ID = -1004339378904
OTP_GROUP_LINK = "https://t.me/SUPERWORKERSOTP"

SMS_USER = "munnayt556"
SMS_PASS = "MDmunna123@"
BOT_NAME = "SUPER WORKER"

# ==========================================
# 🌐 FAKE WEB SERVER (Render Port Binding)
# ==========================================
def run_dummy_server():
    port = int(os.environ.get("PORT", 8080))
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot is running!")
        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()
    server = HTTPServer(('0.0.0.0', port), Handler)
    server.serve_forever()

# ==========================================
# 🗄️ DATABASE SETUP (লোকাল ফাইল ডাটাবেস)
# ==========================================
DB = "bot_database.db" 
_lock = threading.Lock()

def db_init():
    with _lock, sqlite3.connect(DB) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            tg_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            joined_at INTEGER, is_admin INTEGER DEFAULT 0, last_active INTEGER
        );
        CREATE TABLE IF NOT EXISTS numbers(
            number TEXT PRIMARY KEY, is_mine INTEGER DEFAULT 1, assigned_to INTEGER DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, service TEXT, numbers TEXT, created_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS otps(
            id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT, service TEXT, otp TEXT, sms TEXT, tg_id INTEGER, created_at INTEGER
        );
        """)

def db_add_user(tg_id, username, first_name):
    with _lock, sqlite3.connect(DB) as c:
        c.execute("""INSERT INTO users(tg_id, username, first_name, joined_at, last_active)
                     VALUES(?,?,?,?,?) ON CONFLICT(tg_id) DO UPDATE SET
                     username=excluded.username, first_name=excluded.first_name, last_active=excluded.last_active""",
                  (tg_id, username, first_name, int(time.time()), int(time.time())))

def db_get_free_numbers(limit=3):
    with _lock, sqlite3.connect(DB) as c:
        rows = c.execute("SELECT number FROM numbers WHERE assigned_to IS NULL LIMIT ?", (limit,)).fetchall()
        return [r[0] for r in rows]

def db_assign_numbers(tg_id, numbers, service):
    with _lock, sqlite3.connect(DB) as c:
        c.executemany("UPDATE numbers SET assigned_to=? WHERE number=?", [(tg_id, n) for n in numbers])
        c.execute("INSERT INTO requests(tg_id, service, numbers, created_at) VALUES(?,?,?,?)",
                  (tg_id, service, ",".join(numbers), int(time.time())))

def db_number_for_user(num):
    with _lock, sqlite3.connect(DB) as c:
        r = c.execute("SELECT assigned_to FROM numbers WHERE number=?", (num,)).fetchone()
        return r[0] if r else None

def db_log_otp(number, service, otp, sms, tg_id):
    with _lock, sqlite3.connect(DB) as c:
        c.execute("INSERT INTO otps(number, service, otp, sms, tg_id, created_at) VALUES(?,?,?,?,?,?)",
                  (number, service, otp, sms, tg_id, int(time.time())))

def db_stats():
    with _lock, sqlite3.connect(DB) as c:
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_numbers = c.execute("SELECT COUNT(*) FROM numbers").fetchone()[0]
        used_numbers  = c.execute("SELECT COUNT(*) FROM numbers WHERE assigned_to IS NOT NULL").fetchone()[0]
        otps          = c.execute("SELECT COUNT(*) FROM otps").fetchone()[0]
        today = int(time.time()) - 86400
        otps_today    = c.execute("SELECT COUNT(*) FROM otps WHERE created_at>?", (today,)).fetchone()[0]
    return dict(users=users, total_numbers=total_numbers, used_numbers=used_numbers,
                free_numbers=total_numbers-used_numbers, otps=otps, otps_today=otps_today)

def db_all_users():
    with _lock, sqlite3.connect(DB) as c:
        return c.execute("SELECT tg_id FROM users").fetchall()

def db_all_users_detail():
    with _lock, sqlite3.connect(DB) as c:
        return c.execute("SELECT tg_id, username, first_name, joined_at, is_admin FROM users ORDER BY joined_at DESC").fetchall()

# ==========================================
# 🌐 SMS HADI AUTO FETCH & MONITOR
# ==========================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

sms_session = requests.Session()
H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "http://smshadi.net/agent/SMSCDRReports",
    "X-Requested-With": "XMLHttpRequest",
}

def sms_login():
    r = sms_session.get("http://smshadi.net/login", headers=H)
    m = re.search(r'What is (\d+) \+ (\d+)', r.text)
    capt = int(m.group(1)) + int(m.group(2)) if m else 13
    sms_session.post("http://smshadi.net/signin",
        data={"username": SMS_USER, "password": SMS_PASS, "capt": str(capt)}, headers=H, allow_redirects=True)

def sms_get_sesskey():
    page = sms_session.get("http://smshadi.net/agent/SMSCDRReports", headers=H)
    m = re.search(r'sesskey=([^"&]+)', page.text)
    return m.group(1) if m else ""

def sms_fetch_and_save_all_numbers():
    log.info("📡 MySMSNumbers থেকে নাম্বার আনা হচ্ছে...")
    try:
        sms_session.get("http://smshadi.net/agent/MySMSNumbers", headers=H)
        params = {
            "frange": "", "fclient": "", "sEcho": "1", "iColumns": "8", "sColumns": ",,,,,,,",
            "iDisplayStart": "0", "iDisplayLength": "5000",
            "mDataProp_0": "0", "mDataProp_1": "1", "mDataProp_2": "2", "mDataProp_3": "3",
            "mDataProp_4": "4", "mDataProp_5": "5", "mDataProp_6": "6", "mDataProp_7": "7",
            "sSearch": "", "bRegex": "false", "iSortCol_0": "0", "sSortDir_0": "asc", "iSortingCols": "1",
        }
        r = sms_session.get("http://smshadi.net/agent/res/data_smsnumbers.php", params=params, headers=H, timeout=30)
        
        if "aaData" not in r.text: return
        rows = r.json().get("aaData", [])
        numbers = [re.sub(r'<[^>]+>', '', str(row[3])).strip() for row in rows if row[3]]
            
        if numbers:
            with _lock, sqlite3.connect(DB) as c:
                c.executemany("INSERT OR IGNORE INTO numbers(number) VALUES(?)", [(n,) for n in numbers])
            log.info(f"✅ ডাটাবেসে {len(numbers)} টি নাম্বার সেভ হয়েছে!")
    except Exception as e:
        log.error(f"❌ নাম্বার ফেচ এরর: {e}")

def sms_fetch_sms(sesskey):
    today = datetime.now().strftime("%Y-%m-%d")
    r = sms_session.get("http://smshadi.net/agent/res/data_smscdr.php",
        params={"fdate1": f"{today} 00:00:00", "fdate2": f"{today} 23:59:59", "frange": "", "fclient": "", "fnum": "", "fcli": "",
                "fg": "0", "sesskey": sesskey, "iDisplayLength": 500}, headers=H, timeout=30)
    if "aaData" not in r.text: raise Exception("Session expire")
    return [row for row in r.json()['aaData'] if isinstance(row[0], str) and '-' in row[0]]

def sms_detect_service(msg):
    m = msg.lower()
    if "facebook" in m or "fb-" in m: return "facebook"
    if "whatsapp" in m: return "whatsapp"
    return "other"

async def sms_poll_loop(bot):
    sms_login()
    sms_fetch_and_save_all_numbers()
    sesskey = sms_get_sesskey()
    seen = set()
    try:
        for row in sms_fetch_sms(sesskey): seen.add(row[0] + row[2] + row[5])
    except: pass

    log.info("✅ SMS monitor চালু হয়েছে")
    while True:
        try:
            for row in sms_fetch_sms(sesskey):
                dt, rang, num, cli, _, msg, _, payout, _ = row
                key = row[0] + row[2] + row[5]
                if key in seen: continue
                seen.add(key)
                if len(seen) > 10000: seen = set(list(seen)[-5000:])

                otp_m = re.search(r'\b\d{4,8}\b', msg)
                otp = otp_m.group() if otp_m else "N/A"
                service = sms_detect_service(msg)
                owner_id = db_number_for_user(num)
                db_log_otp(num, service, otp, msg.strip(), owner_id)

                if owner_id:
                    text = (f"🔔 <b>নতুন OTP এসেছে!</b>\n\n📱 নাম্বার: <code>{num}</code>\n"
                            f"🎯 Service: <b>{service.title()}</b>\n🔑 OTP: <code>{otp}</code>\n"
                            f"⏰ সময়: {dt}\n\n💬 <i>{msg.strip()[:200]}</i>")
                    try: await bot.send_message(owner_id, text, parse_mode="HTML")
                    except: pass

                if OTP_GROUP_ID:
                    g = f"🔑 <b>OTP</b>\n📱 <code>{num}</code>\n🎯 {service.title()}\n🔑 <code>{otp}</code>\n⏰ {dt}"
                    try: await bot.send_message(OTP_GROUP_ID, g, parse_mode="HTML")
                    except: pass
        except Exception as e:
            try:
                sms_login()
                sms_fetch_and_save_all_numbers()
                sesskey = sms_get_sesskey()
            except: await asyncio.sleep(60)

        await asyncio.sleep(15)

# ==========================================
# 🤖 TELEGRAM BOT HANDLERS
# ==========================================
def user_main_kb(tg_id):
    buttons = [[KeyboardButton("GET NUMBER")]]
    if tg_id in ADMIN_IDS: buttons.append([KeyboardButton("ADMIN PANEL")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def service_inline_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Facebook", callback_data="svc:facebook")]])

def numbers_inline_kb(numbers):
    rows = [[InlineKeyboardButton(f"📋 {n}", callback_data=f"copy:{n}")] for n in numbers]
    rows.append([
        InlineKeyboardButton("👥 OTP GROUP", url=OTP_GROUP_LINK),
        InlineKeyboardButton("🔄 NEXT NUMBER", callback_data="svc:facebook")
    ])
    return InlineKeyboardMarkup(rows)

def number_message_text(service, numbers):
    lines = [f"✨ <b>{BOT_NAME}</b>", "", f"🎯 Service: <b>{service.title()}</b>", "", f"📱 আপনার নাম্বারগুলো:"]
    for n in numbers: lines.append(f"  • <code>{n}</code>")
    lines += ["", "👇 নাম্বার ট্যাপ করলেই কপি হবে"]
    return "\n".join(lines)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db_add_user(u.id, u.username or "", u.first_name or "")
    await update.message.reply_text(f"👋 স্বাগতম <b>{u.first_name}</b>!\n\nনাম্বার নিতে GET NUMBER এ ক্লিক করুন।",
        parse_mode="HTML", reply_markup=user_main_kb(u.id))

async def handle_get_number(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("কোন service-এর জন্য নাম্বার চান?", reply_markup=service_inline_kb())

async def handle_service_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    service = q.data.split(":", 1)[1]
    await q.edit_message_text(f"⏳ <b>WAIT NEW REQUEST</b>\n\n{service.title()} এর জন্য নাম্বার খোঁজা হচ্ছে...", parse_mode="HTML")

    numbers = db_get_free_numbers(3)
    if not numbers:
        await q.edit_message_text(f"❌ <b>দুঃখিত!</b> এখন নাম্বার নেই।", parse_mode="HTML")
        return

    db_assign_numbers(q.from_user.id, numbers, service)
    await asyncio.sleep(1.2)
    await q.edit_message_text(number_message_text(service, numbers), parse_mode="HTML", reply_markup=numbers_inline_kb(numbers))

async def handle_copy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer(f"✅ কপি হয়েছে!", show_alert=False)

async def handle_admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    await update.message.reply_text("👨‍💼 <b>ADMIN PANEL</b>", parse_mode="HTML", reply_markup=admin_kb())

def admin_kb():
    return ReplyKeyboardMarkup([[KeyboardButton("📊 STATS"), KeyboardButton("👥 ALL USERS")],
        [KeyboardButton("📨 BROADCAST"), KeyboardButton("🔙 BACK TO USER")]], resize_keyboard=True)

async def handle_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    s = db_stats()
    text = f"📊 <b>STATS</b>\n\n👥 Users: <b>{s['users']}</b>\n📱 নাম্বার: <b>{s['total_numbers']}</b>\n✅ ব্যবহৃত: <b>{s['used_numbers']}</b>\n🆓 ফ্রি: <b>{s['free_numbers']}</b>\n🔑 মোট OTP: <b>{s['otps']}</b>\n📅 আজকের OTP: <b>{s['otps_today']}</b>"
    await update.message.reply_text(text, parse_mode="HTML")

async def handle_all_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    rows = db_all_users_detail()
    lines = [f"👥 <b>সব Users ({len(rows)})</b>\n"] + [f"👤 <code>{r[0]}</code> — {r[2]}" for r in rows[:50]]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

BCAST_STATE = {}
async def handle_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    BCAST_STATE[update.effective_user.id] = True
    await update.message.reply_text("📨 Broadcast message লিখুন।\nবাতিল করতে /cancel")

async def do_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    if tg_id not in BCAST_STATE: return
    BCAST_STATE.pop(tg_id, None)
    users = [r[0] for r in db_all_users()]
    status = await update.message.reply_text(f"📤 পাঠানো হচ্ছে 0/{len(users)}...")
    for i, uid in enumerate(users, 1):
        try: await ctx.bot.send_message(uid, update.message.html_text, parse_mode="HTML")
        except: pass
        if i % 20 == 0:
            try: await status.edit_text(f"📤 পাঠানো হচ্ছে {i}/{len(users)}...")
            except: pass
        await asyncio.sleep(0.05)
    await status.edit_text(f"✅ Done!")

async def handle_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    BCAST_STATE.pop(update.effective_user.id, None)
    await update.message.reply_text("❌ বাতিল হয়েছে।")

async def handle_back_to_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔙 User Menu", reply_markup=user_main_kb(update.effective_user.id))

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    u = update.effective_user
    db_add_user(tg_id, u.username or "", u.first_name or "")
    text = update.message.text
    if tg_id in BCAST_STATE and BCAST_STATE[tg_id]: return await do_broadcast(update, ctx)
    
    routes = {"GET NUMBER": handle_get_number, "ADMIN PANEL": handle_admin_panel, 
              "📊 STATS": handle_stats, "👥 ALL USERS": handle_all_users,
              "📨 BROADCAST": handle_broadcast, "🔙 BACK TO USER": handle_back_to_user}
    if text in routes: return await routes[text](update, ctx)
    await update.message.reply_text("বাটন ব্যবহার করুন 👇", reply_markup=user_main_kb(tg_id))

async def post_init(app):
    asyncio.create_task(sms_poll_loop(app.bot))

# ==========================================
# 🚀 MAIN RUNNER
# ==========================================
def main():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)

    db_init()
    threading.Thread(target=run_dummy_server, daemon=True).start()
    
    app = (Application.builder().token(BOT_TOKEN)
           .get_updates_request(HTTPXRequest(connection_pool_size=1, read_timeout=30.0, connect_timeout=15.0))
           .request(HTTPXRequest(connection_pool_size=8, read_timeout=15.0, connect_timeout=10.0))
           .post_init(post_init).build())

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", handle_cancel))
    app.add_handler(CallbackQueryHandler(handle_service_choice, pattern=r"^svc:"))
    app.add_handler(CallbackQueryHandler(handle_copy, pattern=r"^copy:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("✅ Bot চালু হচ্ছে...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
